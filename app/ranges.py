"""HTTP byte-range helpers for the video streaming proxy.

The range value is forwarded to S3 verbatim; these helpers only decide whether
a header is syntactically valid and whether an unsatisfiable range should be
answered with 416.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

#: A single ``bytes=<first>-<last>`` spec. Multi-range requests are not allowed
#: by the HTTP spec for single-part responses, so they are ignored upstream.
RANGE_RE = re.compile(r"^bytes=(?P<first>\d*)-(?P<last>\d*)$")


def is_valid_single_range(header: str) -> bool:
    """True when the header is one well-formed byte-range spec."""

    return bool(RANGE_RE.match(header.strip()))


def normalize_range_header(header: Optional[str]) -> Optional[str]:
    """Return the range value to forward, or ``None`` to request the whole object.

    Per RFC 9110 a syntactically invalid or multi-range header must be ignored
    rather than rejected, so such values map to ``None``.
    """

    if not header:
        return None
    header = header.strip()
    if is_valid_single_range(header):
        return header
    return None


def range_is_satisfiable(header: str, total_size: int) -> bool:
    """False when the requested first byte lies at or beyond the object end."""

    match = RANGE_RE.match(header.strip())
    if not match:
        return False
    first = int(match.group("first")) if match.group("first") else 0
    suffix_length = not match.group("first") and bool(match.group("last"))
    if suffix_length:
        return total_size > 0
    return first < total_size


def resolve_range(header: Optional[str], total_size: int) -> Optional[Tuple[int, int]]:
    """Resolve a valid range header against a known object size.

    Returns an inclusive ``(first, last)`` pair, or ``None`` when the header is
    absent or not a valid single range. Mainly useful for tests and diagnostics.
    """

    normalized = normalize_range_header(header)
    if normalized is None:
        return None
    match = RANGE_RE.match(normalized)
    if not match or total_size <= 0:
        return None
    first_s, last_s = match.group("first"), match.group("last")
    if not first_s:
        if not last_s:
            return None
        length = min(int(last_s), total_size)
        return (total_size - length, total_size - 1)
    first = int(first_s)
    last = int(last_s) if last_s else total_size - 1
    return (first, min(last, total_size - 1))
