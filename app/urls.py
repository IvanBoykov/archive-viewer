"""Decides how a clip's video URL is produced for a given request host.

Two delivery modes exist:

``presigned``
    The request ``Host`` is present in the configured host map, so the browser
    is given a SigV4 presigned URL that points straight at the mapped S3
    endpoint. The server never sees the video bytes.

``proxy``
    The request ``Host`` is unknown, so the browser is given an on-site
    ``/media/...`` URL and the server streams the object from S3. Credentials
    and the S3 endpoint stay server side.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

from .config import HostMapping, Settings
from .s3 import ArchiveStore

MODE_PRESIGNED = "presigned"
MODE_PROXY = "proxy"

#: Prefix of the streaming proxy route; keep in sync with the route in app.main.
PROXY_ROUTE_PREFIX = "/media"


@dataclass
class MediaPolicy:
    """Per-request policy that turns an S3 key into a browser-facing URL."""

    mode: str
    #: The mapped endpoint when in presigned mode, otherwise ``None``.
    endpoint_url: Optional[str] = None
    #: Human-readable note shown in the UI about link lifetime.
    note: Optional[str] = None

    def url_for(self, store: ArchiveStore, key: str) -> str:  # pragma: no cover - abstract
        raise NotImplementedError


@dataclass
class ProxyPolicy(MediaPolicy):
    """Video is served by the application, which proxies the S3 response."""

    def url_for(self, store: ArchiveStore, key: str) -> str:
        return f"{PROXY_ROUTE_PREFIX}/{quote(key, safe='/')}"


@dataclass
class PresignedPolicy(MediaPolicy):
    """Video is fetched by the browser from the mapped endpoint using a signature."""

    mapping: Optional[HostMapping] = None

    def url_for(self, store: ArchiveStore, key: str) -> str:
        assert self.mapping is not None  # constructed together with a mapping
        return store.presigned_url(key, self.mapping)


def build_media_policy(store: ArchiveStore, settings: Settings,
                       host_header: Optional[str]) -> MediaPolicy:
    """Pick the delivery mode for a request based on its ``Host`` header."""

    mapping = settings.resolve_host(host_header)
    if mapping is not None:
        return PresignedPolicy(
            mode=MODE_PRESIGNED,
            endpoint_url=mapping.endpoint_url,
            note=(
                f"Video links go directly to {mapping.endpoint_url} and stop working "
                f"after {settings.presign_expiry // 60} minutes."
            ),
            mapping=mapping,
        )
    return ProxyPolicy(
        mode=MODE_PROXY,
        note="Video links are served by this application and stream through it.",
    )


def proxy_key_is_allowed(key: str, settings: Settings) -> bool:
    """Constrain the proxy to media objects inside the configured archive prefix."""

    normalized = key.strip("/")
    common = settings.common_prefix.strip("/")
    if not normalized or ".." in normalized.split("/"):
        return False
    if not normalized.startswith(f"{common}/"):
        return False
    return normalized.lower().endswith(".mp4")
