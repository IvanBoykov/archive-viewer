"""HTTP layer: SSR browse routes, the streaming proxy, and template setup."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from .archive import InvalidPath, normalize_common_prefix
from .browse import BrowseService, Crumb, LevelNotFound, PageContext, hour_navigation_urls, pretty_name
from .config import Settings
from .ranges import normalize_range_header, range_is_satisfiable
from .s3 import ArchiveStore, ObjectNotFound, RangeNotSatisfiable
from .urls import (
    MODE_PRESIGNED,
    PROXY_ROUTE_PREFIX,
    build_media_policy,
    proxy_key_is_allowed,
)

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    """Application factory.

    Args:
        settings: optional settings object; loaded from the environment when
            omitted. Tests pass an explicit instance.
    """

    if settings is None:
        load_dotenv()
        settings = Settings.from_env()
        settings.require_complete()

    store = ArchiveStore(settings)
    browse = BrowseService(store)

    app = FastAPI(title="Video Archive Viewer", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.store = store
    app.state.browse = browse

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["tojson_safe"] = _tojson_safe
    templates.env.globals["pretty_name"] = pretty_name
    app.state.templates = templates

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # -- helpers ---------------------------------------------------------
    def render(request: Request, template: str, context: Dict[str, Any]) -> HTMLResponse:
        policy = build_media_policy(store, settings, request.headers.get("host"))
        payload: Dict[str, Any] = {
            "settings": settings,
            "common_prefix": normalize_common_prefix(settings.common_prefix),
            "mode": policy.mode,
            "delivery_note": policy.note,
            "presign_expiry_minutes": settings.presign_expiry // 60,
            "year_span": settings.max_year_span,
        }
        payload.update(context)
        return templates.TemplateResponse(request, template, payload)

    def render_hour(request: Request, context: PageContext) -> HTMLResponse:
        """Attach per-clip video URLs and cross-boundary navigation to an hour page."""

        policy = build_media_policy(store, settings, request.headers.get("host"))
        clips = [
            {**clip.as_dict(), "video_url": policy.url_for(store, clip.key)}
            for clip in context.clips
        ]
        navigation = hour_navigation_urls(context, settings.common_prefix)
        nav_context = {
            **navigation,
            "previous_label": _hour_link_label(context.prev_hour),
            "next_label": _hour_link_label(context.next_hour),
        }
        # The modal needs the cross-boundary URLs as data, not just as links.
        nav_payload = {**context.hour_meta, **nav_context}
        return render(
            request,
            "hour.html",
            {
                "context": context,
                "clips": clips,
                "hour_meta": context.hour_meta,
                "clips_json": _tojson_safe(clips),
                "nav_json": _tojson_safe(nav_payload),
                "navigation": nav_context,
            },
        )

    # -- streaming proxy -------------------------------------------------
    @app.get(f"{PROXY_ROUTE_PREFIX}/{{key:path}}", include_in_schema=False)
    def proxy_media(request: Request, key: str):
        """Stream one archived object from S3, honouring HTTP range requests.

        The application is the authenticated S3 client here: SigV4 signing and
        the S3 ``Host`` header stay server side, and the browser only ever
        talks to this origin.
        """

        if not proxy_key_is_allowed(key, settings):
            return JSONResponse({"detail": "Object is not part of the archive"}, status_code=404)

        try:
            head = store.head(key)
        except ObjectNotFound:
            return JSONResponse({"detail": "Clip not found"}, status_code=404)

        total_size = int(head.get("ContentLength", 0))
        content_type = head.get("ContentType") or "video/mp4"
        etag = head.get("ETag")
        last_modified = head.get("LastModified")

        range_header = normalize_range_header(request.headers.get("range"))
        if_range = request.headers.get("if-range")
        # An If-Range that no longer matches means "send the whole object".
        if if_range and etag and _strip_etag(if_range) != _strip_etag(etag):
            range_header = None

        status_code = 200
        if range_header and not range_is_satisfiable(range_header, total_size):
            return _range_response(
                status_code=416,
                total_size=total_size,
                content_type=content_type,
                etag=etag,
                last_modified=last_modified,
                body=iter(()),
            )
        if range_header:
            status_code = 206

        try:
            upstream = store.open_object(key, byte_range=range_header)
        except ObjectNotFound:
            return JSONResponse({"detail": "Clip not found"}, status_code=404)
        except RangeNotSatisfiable as exc:
            return _range_response(
                status_code=416,
                total_size=exc.total_size or total_size,
                content_type=content_type,
                etag=etag,
                last_modified=last_modified,
                body=iter(()),
            )

        headers = {
            "Content-Type": upstream.content_type or content_type,
            "Accept-Ranges": "bytes",
            "Cache-Control": settings.proxy_cache_control,
        }
        if upstream.etag or etag:
            headers["ETag"] = upstream.etag or etag
        http_last_modified = _http_date(upstream.last_modified or last_modified)
        if http_last_modified:
            headers["Last-Modified"] = http_last_modified
        if upstream.content_range:
            headers["Content-Range"] = upstream.content_range
        if upstream.content_length is not None:
            headers["Content-Length"] = str(upstream.content_length)

        return StreamingResponse(upstream.body,
                                 status_code=upstream.status_code or status_code,
                                 headers=headers,
                                 media_type=headers["Content-Type"])

    # -- pages -----------------------------------------------------------
    @app.get("/healthz", include_in_schema=False)
    def healthz() -> Dict[str, str]:
        return {"status": "ok", "bucket": settings.bucket, "prefix": settings.common_prefix}

    @app.get("/", include_in_schema=False)
    def cameras_page(request: Request):
        return _page(request, "/")

    @app.get("/{full_path:path}", include_in_schema=False)
    def browse_page(request: Request, full_path: str):
        return _page(request, f"/{full_path}")

    def _page(request: Request, path: str):
        # The archive layout always ends a folder in a slash; normalise the
        # extension-less variant so both spellings work.
        if path != "/" and not path.endswith("/"):
            return RedirectResponse(f"{path}/", status_code=308)
        try:
            context = browse.page_for_path(path)
        except InvalidPath as exc:
            return _error(request, 404, "Unknown address", str(exc))
        except LevelNotFound:
            return _error(request, 404, "Nothing archived here",
                          f"No recordings exist at {path}.")
        if context.level == "clips":
            return render_hour(request, context)
        return render(request, "browse.html", {"context": context})

    def _error(request: Request, status_code: int, heading: str, message: str) -> HTMLResponse:
        # The shared layout expects a page context, so give the error page one
        # that points back at the archive root.
        error_context = PageContext(
            level="error",
            title=heading,
            heading=heading,
            subheading=None,
            breadcrumbs=[Crumb(url="/", label="Archive")],
            empty_message=message,
        )
        response = render(request, "error.html",
                          {"status_code": status_code, "heading": heading, "message": message,
                           "context": error_context})
        response.status_code = status_code
        return response

    @app.exception_handler(StarletteHTTPException)
    def http_exception_handler(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404 and not request.url.path.startswith(
            ("/static", PROXY_ROUTE_PREFIX)
        ):
            return _error(request, 404, "Page not found", str(exc.detail))
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    def unhandled_exception_handler(request: Request, exc: Exception):
        logger.exception("Unhandled error while serving %s", request.url.path)
        return _error(request, 500, "Archive temporarily unavailable",
                      "The storage backend could not be reached. Try again shortly.")

    return app


def _hour_link_label(ref) -> Optional[str]:
    if ref is None:
        return None
    return f"{ref.year:04d}-{ref.month:02d}-{ref.day:02d} {ref.hour:02d}:00"


def _range_response(status_code: int, total_size: int, content_type: str, etag, last_modified,
                    body) -> StreamingResponse:
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Range": f"bytes */{total_size}",
        "Content-Type": content_type,
    }
    if etag:
        headers["ETag"] = etag
    http_last_modified = _http_date(last_modified)
    if http_last_modified:
        headers["Last-Modified"] = http_last_modified
    return StreamingResponse(body, status_code=status_code, headers=headers)


def _http_date(value) -> Optional[str]:
    """Format an S3 timestamp as an RFC 7231 HTTP date.

    ``str(datetime)`` would emit an ISO-ish string that HTTP clients cannot
    parse, so convert explicitly; pass through anything already formatted.
    """

    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return format_datetime(moment.astimezone(timezone.utc), usegmt=True)
    return str(value)


def _strip_etag(value: str) -> str:
    value = value.strip().strip('"')
    return value[2:] if value.startswith("W/") else value


def _tojson_safe(value: Any) -> str:
    """Serialize to JSON that is safe to embed inside an HTML element.

    ``<``/``>``/``&`` are escaped so a stored key can never break out of the
    ``<script>`` element it is embedded in, and U+2028/U+2029 are escaped
    because they terminate JavaScript string literals.
    """

    text = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    for raw, replacement in (
        ("<", "\\u003c"),
        (">", "\\u003e"),
        ("&", "\\u0026"),
        ("\u2028", "\\u2028"),
        ("\u2029", "\\u2029"),
    ):
        text = text.replace(raw, replacement)
    return text


# Exposed so templates can render a clip list without re-deriving the fields.
def clips_as_dicts(clips) -> list:
    return [clip.as_dict() for clip in clips]


# Module-level app for ASGI servers: ``uvicorn app.main:app``.
app = create_app()

__all__ = ["app", "create_app", "MAX_PROXY_BYTES"]
