"""S3 access: single-level listing, presigned URLs, and Range-aware streaming.

Every listing call uses ``ListObjectsV2`` with ``Prefix`` + ``Delimiter='/'``,
so a request only ever reads the children of the folder being displayed. The
bucket is never walked recursively.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Tuple

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .archive import Clip, parse_clip, sort_clips
from .config import PATH_STYLE, VIRTUAL_HOSTED_STYLE, HostMapping, Settings

logger = logging.getLogger(__name__)

#: Garage and other S3-compatible gateways reject SigV2 presigns ("anonymous
#: access"), so v4 is forced for the clients that produce browser-facing URLs.
PRESIGN_SIGNATURE_VERSION = "s3v4"

#: Returned when an object key or a byte range cannot be satisfied.
class ObjectNotFound(Exception):
    """The requested S3 object does not exist."""


class RangeNotSatisfiable(Exception):
    """S3 rejected the byte range (HTTP 416); carries the object size."""

    def __init__(self, total_size: Optional[int]) -> None:
        super().__init__("requested range is not satisfiable")
        self.total_size = total_size


@dataclass
class UpstreamResponse:
    """A (possibly partial) object body plus the headers worth forwarding."""

    status_code: int
    content_length: Optional[int]
    content_range: Optional[str]
    content_type: str
    etag: Optional[str]
    last_modified: Optional[str]
    body: Iterator[bytes]


def _head_object_params(key: str, **extra: str) -> dict:
    params = {"Key": key}
    params.update({k: v for k, v in extra.items() if v})
    return params


class ArchiveStore:
    """Read-only S3 client wrapper for the archive browser."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._clients: Dict[Tuple[str, str, str], object] = {}
        self._lock = threading.Lock()
        # Probe once so a misconfiguration fails at start-up rather than on the
        # first page view.
        self._client_for(None, settings.addressing_style, PRESIGN_SIGNATURE_VERSION)

    # -- client plumbing -------------------------------------------------
    def _client_for(self, endpoint_url: Optional[str], addressing_style: str,
                    signature_version: Optional[str] = None):
        cache_key = (endpoint_url or "", addressing_style, signature_version or "")
        with self._lock:
            cached = self._clients.get(cache_key)
            if cached is not None:
                return cached

        cfg_kwargs = {
            "region_name": self.settings.region,
            "retries": {"max_attempts": 3, "mode": "standard"},
            "connect_timeout": 5,
            "read_timeout": 30,
            "s3": {"addressing_style": addressing_style},
        }
        if signature_version:
            cfg_kwargs["signature_version"] = signature_version

        client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=self.settings.access_key_id,
            aws_secret_access_key=self.settings.secret_access_key,
            aws_session_token=self.settings.session_token,
            config=Config(**cfg_kwargs),
        )
        with self._lock:
            self._clients[cache_key] = client
        return client

    @property
    def client(self):
        """Client used for server-side listing and proxying."""

        return self._client_for(self.settings.endpoint_url, self.settings.addressing_style)

    # -- listing ---------------------------------------------------------
    def list_level(self, prefix: str) -> Tuple[List[str], List[Tuple[str, Optional[int]]]]:
        """List immediate children of ``prefix``.

        Returns ``(directory_names, files)`` where directory names keep no
        trailing slash and files are ``(name, size)`` pairs, both sorted.
        Pagination is handled so folders with more than 1000 entries are
        complete, while still touching only this one level.
        """

        directories: List[str] = []
        files: List[Tuple[str, Optional[int]]] = []
        token: Optional[str] = None
        while True:
            kwargs = {
                "Bucket": self.settings.bucket,
                "Prefix": prefix,
                "Delimiter": "/",
                "MaxKeys": 1000,
            }
            if token:
                kwargs["ContinuationToken"] = token
            response = self.client.list_objects_v2(**kwargs)
            for common in response.get("CommonPrefixes", []):
                name = common["Prefix"][len(prefix):].strip("/")
                if name:
                    directories.append(name)
            for content in response.get("Contents", []):
                name = content["Key"][len(prefix):]
                # A delimiter listing can echo the prefix itself (zero-byte
                # folder marker); skip it rather than showing an empty name.
                if not name or name.endswith("/"):
                    continue
                size = content.get("Size")
                files.append((name, int(size) if size is not None else None))
            if not response.get("IsTruncated"):
                break
            token = response.get("NextContinuationToken")
        return sorted(set(directories)), sorted(files, key=lambda item: item[0])

    def list_child_directories(self, prefix: str) -> List[str]:
        """Directory names directly under ``prefix`` (the archive.py callback)."""

        return self.list_level(prefix)[0]

    def list_clips(self, hour_prefix: str, hour: Optional[int] = None) -> List[Clip]:
        """Clips inside an hour folder, ordered by recording time.

        ``hour`` (the parent folder name as an integer) is attached to each clip
        so the UI can show an absolute wall-clock start time.
        """

        clips = []
        for name, size in self.list_level(hour_prefix)[1]:
            clip = parse_clip(hour_prefix + name, size=size, hour=hour)
            if clip is not None:
                clips.append(clip)
        return sort_clips(clips)

    def prefix_exists(self, prefix: str) -> bool:
        """True when a folder holds any child folder or object."""

        response = self.client.list_objects_v2(
            Bucket=self.settings.bucket, Prefix=prefix, Delimiter="/", MaxKeys=1
        )
        return bool(response.get("CommonPrefixes") or response.get("Contents"))

    def head(self, key: str) -> dict:
        """Metadata for an object key, used for size and content type."""

        try:
            return self.client.head_object(Bucket=self.settings.bucket, Key=key)
        except ClientError as exc:
            if _error_code(exc) in {"404", "NoSuchKey", "NotFound"}:
                raise ObjectNotFound(key) from exc
            raise

    # -- browser-facing URLs ---------------------------------------------
    def presigned_url(self, key: str, mapping: HostMapping) -> str:
        """Presigned GET URL valid for one hour, aimed at the mapped endpoint."""

        client = self._client_for(
            mapping.endpoint_url, mapping.addressing_style, PRESIGN_SIGNATURE_VERSION
        )
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.settings.bucket, "Key": key},
            ExpiresIn=self.settings.presign_expiry,
        )

    # -- proxying ---------------------------------------------------------
    def open_object(self, key: str, byte_range: Optional[str] = None) -> UpstreamResponse:
        """Open an object for streaming, honouring an optional HTTP byte range.

        Raises:
            ObjectNotFound: the key does not exist.
            RangeNotSatisfiable: S3 reported the range as unsatisfiable.
        """

        params = {"Key": key}
        if byte_range:
            params["Range"] = byte_range
        try:
            response = self.client.get_object(Bucket=self.settings.bucket, **params)
        except ClientError as exc:
            code = _error_code(exc)
            if code in {"404", "NoSuchKey", "NotFound"}:
                raise ObjectNotFound(key) from exc
            if code in {"416", "InvalidRange"}:
                raise RangeNotSatisfiable(self._size_of(key)) from exc
            raise
        headers = response.get("ResponseMetadata", {}).get("HTTPHeaders", {})
        return UpstreamResponse(
            status_code=int(response.get("ResponseMetadata", {}).get("HTTPStatusCode", 200)),
            content_length=_int_or_none(response.get("ContentLength", headers.get("content-length"))),
            content_range=response.get("ContentRange") or headers.get("content-range"),
            content_type=response.get("ContentType") or headers.get("content-type") or "video/mp4",
            etag=response.get("ETag") or headers.get("etag"),
            last_modified=response.get("LastModified") or headers.get("last-modified"),
            body=_iter_streaming_body(response["Body"], self.settings.proxy_chunk_size),
        )

    def _size_of(self, key: str) -> Optional[int]:
        try:
            return int(self.head(key).get("ContentLength", 0)) or None
        except ObjectNotFound:
            return None


def _iter_streaming_body(stream, chunk_size: int) -> Iterator[bytes]:
    """Yield a boto3 StreamingBody in chunks and always close it."""

    try:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            yield chunk
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()


def _int_or_none(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _error_code(exc: ClientError) -> str:
    error = (exc.response or {}).get("Error", {})
    return str(error.get("Code") or error.get("code") or exc.__class__.__name__)


__all__ = [
    "ArchiveStore",
    "ObjectNotFound",
    "RangeNotSatisfiable",
    "UpstreamResponse",
    "PATH_STYLE",
    "VIRTUAL_HOSTED_STYLE",
]
