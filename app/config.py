"""Runtime configuration for the archive viewer.

Everything is environment driven so the same image can be deployed behind
different virtual hosts. The important piece is :data:`Settings.host_map`,
which maps an incoming HTTP ``Host`` header onto a concrete S3 endpoint plus
an addressing style. Hosts that are absent from the map fall back to the
streaming proxy, which keeps S3 credentials server-side.

Variable names follow the AWS SDK conventions so that one ``.env`` can serve
this viewer and the archiver that writes the keys. Credentials, the S3 endpoint
and every other credential source boto3 understands (``AWS_ACCESS_KEY_ID``,
``AWS_PROFILE``, a shared config file, an instance role) are resolved by the
default botocore chain and are deliberately not modelled here.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: Valid values for ``addressing_style``.
PATH_STYLE = "path"
VIRTUAL_HOSTED_STYLE = "virtual"

#: Top-level folder holding the cameras. Empty by default: the archiver writes
#: ``<S3_PREFIX><camera>/...`` with an empty prefix, so cameras sit at the
#: bucket root unless ``S3_PREFIX`` says otherwise.
DEFAULT_COMMON_PREFIX = ""

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


def _resolve_region(env: Dict[str, str]) -> str:
    """Pick the signing region, requiring an explicit one.

    ``AWS_REGION`` is read first because the archiver's configuration uses that
    name, while botocore's own chain only honours ``AWS_DEFAULT_REGION`` and a
    shared config file. A presigned URL cannot be re-signed after a browser
    received it, and an S3-compatible gateway rejects a region it does not
    serve without revealing the right one, so guessing would fail late and
    opaquely: an unset region is a configuration error instead.
    """

    for name in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        value = (env.get(name) or "").strip()
        if value:
            return value
    return ""


def _validate_timezone(value: Optional[str]) -> Optional[str]:
    """Check an IANA zone name, keeping it as the configured string."""

    if value is None:
        return None
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"S3_KEY_TIMEZONE is not a valid IANA timezone: {value!r}") from exc
    return value


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
    """Deployment settings, normally produced by :meth:`from_env`.

    Credentials are absent by design: every client is built by the default
    botocore chain, so ``AWS_ACCESS_KEY_ID``, ``AWS_PROFILE``, a shared config
    file or an instance role all work without a variable of our own. The S3
    endpoint likewise comes from the chain (``AWS_ENDPOINT_URL_S3``), and the
    addressing style is only configurable per mapped host.
    """

    bucket: str
    #: Region used to sign every request and presigned URL.
    region: str
    #: Top level folder inside the bucket, stored without slashes (empty = root).
    common_prefix: str = DEFAULT_COMMON_PREFIX
    #: ``Host`` header -> direct access mapping.
    host_map: Dict[str, HostMapping] = field(default_factory=dict)
    presign_expiry: int = DEFAULT_PRESIGN_EXPIRY
    max_year_span: int = DEFAULT_MAX_YEAR_SPAN
    #: Size of the chunks forwarded by the streaming proxy.
    proxy_chunk_size: int = 256 * 1024
    #: Applied to ``Cache-Control`` of proxied media.
    proxy_cache_control: str = "private, max-age=300"
    #: IANA zone the archiver used when it built key dates. Display only: the
    #: viewer reads folder names as they are and converts nothing.
    key_timezone: Optional[str] = None

    @classmethod
    def from_env(cls, environ: Optional[Dict[str, str]] = None) -> "Settings":
        env = os.environ if environ is None else environ

        def get(name: str, default: Optional[str] = None) -> Optional[str]:
            value = env.get(name)
            if value is None or value.strip() == "":
                return default
            return value.strip()

        # An empty S3_PREFIX is valid and means "cameras live at the bucket
        # root", which is also what an unset variable means here.
        common_prefix = (env.get("S3_PREFIX") or "").strip().strip("/")

        settings = cls(
            bucket=get("S3_BUCKET", "") or "",
            region=_resolve_region(env),
            common_prefix=common_prefix,
            presign_expiry=int(get("ARCHIVE_PRESIGN_EXPIRY", str(DEFAULT_PRESIGN_EXPIRY))),
            max_year_span=int(get("ARCHIVE_MAX_YEAR_SPAN", str(DEFAULT_MAX_YEAR_SPAN))),
            proxy_chunk_size=int(get("ARCHIVE_PROXY_CHUNK_SIZE", str(256 * 1024))),
            proxy_cache_control=get("ARCHIVE_PROXY_CACHE_CONTROL", "private, max-age=300"),
            key_timezone=_validate_timezone(get("S3_KEY_TIMEZONE")),
        )

        if not settings.bucket:
            raise ConfigError("S3_BUCKET is required")
        if not settings.region:
            raise ConfigError(
                "A signing region is required: set AWS_REGION (or AWS_DEFAULT_REGION). "
                "Presigned URLs cannot be re-signed after the browser receives them, "
                "so the region cannot be guessed."
            )

        raw_map = get("ARCHIVE_HOST_MAP")
        if raw_map:
            settings.host_map = _parse_host_map(raw_map)
        return settings

    def require_complete(self) -> None:
        """Fail fast when mandatory bucket/region information is missing.

        Credentials are not checked: they are resolved by the botocore chain,
        which may use a profile or an instance role this module knows nothing
        about.
        """

        if not self.bucket:
            raise ConfigError("Missing required configuration: S3_BUCKET")
        if not self.region:
            raise ConfigError(
                "Missing required configuration: a signing region "
                "(AWS_REGION or AWS_DEFAULT_REGION)"
            )

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
