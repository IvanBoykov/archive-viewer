"""Runtime configuration for the archive viewer.

Everything is environment driven so the same image can be deployed behind
different virtual hosts. The important piece is :data:`Settings.host_map`,
which maps an incoming HTTP ``Host`` header onto a concrete S3 endpoint plus
an addressing style. Hosts that are absent from the map fall back to the
streaming proxy, which keeps S3 credentials server-side.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, Optional

#: Valid values for ``addressing_style``.
PATH_STYLE = "path"
VIRTUAL_HOSTED_STYLE = "virtual"

#: Presigned URLs are short lived: one hour, as required by the specification.
DEFAULT_PRESIGN_EXPIRY = 3600

#: How many calendar years the previous/next-hour search may walk away from the
#: current hour before giving up. Keeps the number of S3 LIST calls bounded.
DEFAULT_MAX_YEAR_SPAN = 5


class ConfigError(RuntimeError):
    """Raised when the environment does not describe a usable deployment."""


@dataclass(frozen=True)
class HostMapping:
    """A single ``Host`` header -> S3 endpoint binding.

    Attributes:
        endpoint_url: S3 endpoint the browser should be pointed at. It must be
            reachable from the client, because the browser fetches the video
            directly from it.
        addressing_style: Either ``"path"``
            (``https://endpoint/bucket/key``) or ``"virtual"``
            (``https://bucket.endpoint/key``).
    """

    endpoint_url: str
    addressing_style: str = PATH_STYLE

    def __post_init__(self) -> None:
        style = str(self.addressing_style).strip().lower()
        if style in {"virtual-hosted", "virtual_hosted", "virtual", "vhost"}:
            object.__setattr__(self, "addressing_style", VIRTUAL_HOSTED_STYLE)
        elif style in {"path", "path-style", "path_style"}:
            object.__setattr__(self, "addressing_style", PATH_STYLE)
        else:
            raise ConfigError(
                "addressing_style must be 'path' or 'virtual', got "
                f"{self.addressing_style!r}"
            )
        if not self.endpoint_url.startswith(("http://", "https://")):
            raise ConfigError(
                f"endpoint_url for a host mapping must be absolute, got {self.endpoint_url!r}"
            )


def _parse_host_map(raw: str) -> Dict[str, HostMapping]:
    """Build the host map from a JSON document.

    Accepted shapes::

        {"archive.example.com": "https://s3.example.com"}
        {"archive.example.com": {"endpoint_url": "https://s3.example.com",
                                 "addressing_style": "virtual"}}
    """

    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:  # pragma: no cover - config error path
        raise ConfigError(f"ARCHIVE_HOST_MAP is not valid JSON: {exc}") from exc

    if not isinstance(loaded, dict):
        raise ConfigError("ARCHIVE_HOST_MAP must be a JSON object keyed by Host header")

    mapping: Dict[str, HostMapping] = {}
    for host, value in loaded.items():
        key = host.strip().lower()
        if not key:
            continue
        if isinstance(value, str):
            mapping[key] = HostMapping(endpoint_url=value)
        elif isinstance(value, dict):
            endpoint = value.get("endpoint_url") or value.get("endpoint")
            if not endpoint:
                raise ConfigError(f"Host {host!r} in ARCHIVE_HOST_MAP has no endpoint_url")
            mapping[key] = HostMapping(
                endpoint_url=endpoint,
                addressing_style=value.get("addressing_style", PATH_STYLE),
            )
        else:
            raise ConfigError(f"Unsupported ARCHIVE_HOST_MAP value for host {host!r}")
    return mapping


@dataclass
class Settings:
    """Deployment settings, normally produced by :meth:`from_env`."""

    bucket: str
    #: Top level folder inside the bucket, stored without slashes (``archive``).
    common_prefix: str
    endpoint_url: Optional[str] = None
    region: str = "us-east-1"
    access_key_id: Optional[str] = None
    secret_access_key: Optional[str] = None
    session_token: Optional[str] = None
    #: Addressing style used by the server side proxy client.
    addressing_style: str = PATH_STYLE
    #: ``Host`` header -> direct access mapping.
    host_map: Dict[str, HostMapping] = field(default_factory=dict)
    presign_expiry: int = DEFAULT_PRESIGN_EXPIRY
    max_year_span: int = DEFAULT_MAX_YEAR_SPAN
    #: Size of the chunks forwarded by the streaming proxy.
    proxy_chunk_size: int = 256 * 1024
    #: Applied to ``Cache-Control`` of proxied media.
    proxy_cache_control: str = "private, max-age=300"

    @classmethod
    def from_env(cls, environ: Optional[Dict[str, str]] = None) -> "Settings":
        env = os.environ if environ is None else environ

        def get(name: str, default: Optional[str] = None) -> Optional[str]:
            value = env.get(name)
            if value is None or value.strip() == "":
                return default
            return value.strip()

        common_prefix = (get("ARCHIVE_COMMON_PREFIX", "archive") or "archive").strip("/")
        if not common_prefix:
            raise ConfigError("ARCHIVE_COMMON_PREFIX must not be empty")

        settings = cls(
            bucket=get("ARCHIVE_S3_BUCKET", "video-archive"),
            common_prefix=common_prefix,
            endpoint_url=get("ARCHIVE_S3_ENDPOINT_URL"),
            region=get("ARCHIVE_S3_REGION", "us-east-1"),
            access_key_id=get("ARCHIVE_S3_ACCESS_KEY_ID") or get("AWS_ACCESS_KEY_ID"),
            secret_access_key=get("ARCHIVE_S3_SECRET_ACCESS_KEY") or get("AWS_SECRET_ACCESS_KEY"),
            session_token=get("ARCHIVE_S3_SESSION_TOKEN") or get("AWS_SESSION_TOKEN"),
            addressing_style=get("ARCHIVE_S3_ADDRESSING_STYLE", PATH_STYLE),
            presign_expiry=int(get("ARCHIVE_PRESIGN_EXPIRY", str(DEFAULT_PRESIGN_EXPIRY))),
            max_year_span=int(get("ARCHIVE_MAX_YEAR_SPAN", str(DEFAULT_MAX_YEAR_SPAN))),
            proxy_chunk_size=int(get("ARCHIVE_PROXY_CHUNK_SIZE", str(256 * 1024))),
            proxy_cache_control=get("ARCHIVE_PROXY_CACHE_CONTROL", "private, max-age=300"),
        )

        raw_map = get("ARCHIVE_HOST_MAP")
        if raw_map:
            settings.host_map = _parse_host_map(raw_map)

        if not settings.bucket:
            raise ConfigError("ARCHIVE_S3_BUCKET is required")
        # Normalise the default addressing style the same way Host mappings are.
        style = HostMapping(endpoint_url=settings.endpoint_url or "http://localhost",
                            addressing_style=settings.addressing_style).addressing_style
        settings.addressing_style = style
        return settings

    def require_complete(self) -> None:
        """Fail fast when mandatory credentials/bucket information is missing."""

        missing = [
            name
            for name, value in (
                ("ARCHIVE_S3_BUCKET", self.bucket),
                ("ARCHIVE_S3_ACCESS_KEY_ID", self.access_key_id),
                ("ARCHIVE_S3_SECRET_ACCESS_KEY", self.secret_access_key),
            )
            if not value
        ]
        if missing:
            raise ConfigError("Missing required configuration: " + ", ".join(missing))

    def resolve_host(self, host_header: Optional[str]) -> Optional[HostMapping]:
        """Return the direct-access mapping for a request ``Host`` header.

        Matching is case insensitive and tolerates a port suffix, so both
        ``archive.example.com`` and ``archive.example.com:8443`` resolve.
        ``None`` means "proxy the video stream instead of presigning".
        """

        if not host_header or not self.host_map:
            return None
        candidate = host_header.strip().lower()
        if candidate in self.host_map:
            return self.host_map[candidate]
        # ``Host`` normally carries a port; the map is keyed by bare hostname.
        bare_host = candidate.rsplit(":", 1)[0] if ":" in candidate else candidate
        return self.host_map.get(bare_host)
