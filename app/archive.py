"""Archive domain model: key layout, human labels, cross-boundary navigation.

Nothing in this module talks to S3. The chronological search receives a
callback that lists the children of a prefix, which keeps the logic testable
and independent of boto3.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

#: ``<2-digit minute>-<2-digit second>[-truncated].mp4``: the offset of a
#: motion-detected recording inside the hour named by the parent folder.
CLIP_NAME_RE = re.compile(
    r"^(?P<minute>[0-5][0-9])-(?P<second>[0-5][0-9])(?P<truncated>-truncated)?\.mp4$"
)

YEAR_RE = re.compile(r"^[0-9]{4}$")
MONTH_RE = re.compile(r"^(0[1-9]|1[0-2])$")
DAY_RE = re.compile(r"^(0[1-9]|[12][0-9]|3[01])$")
HOUR_RE = re.compile(r"^([01][0-9]|2[0-3])$")

#: Breadcrumb/listing levels, ordered from the root down to a single hour.
LEVEL_CAMERAS = "cameras"
LEVEL_YEARS = "years"
LEVEL_MONTHS = "months"
LEVEL_DAYS = "days"
LEVEL_HOURS = "hours"
LEVEL_CLIPS = "clips"

#: ``list_children(prefix) -> sorted child names`` where a child name is the
#: path component between the given prefix and the next ``/``.
ListChildren = Callable[[str], Sequence[str]]


class InvalidPath(ValueError):
    """Raised for URLs that do not match the archive key layout."""


@dataclass(frozen=True)
class HourRef:
    """An absolute point in the archive hierarchy with calendar semantics."""

    camera: str
    year: int
    month: int
    day: int
    hour: int

    @property
    def date(self) -> date:
        return date(self.year, self.month, self.day)

    def prefix(self, common_prefix: str) -> str:
        """S3 prefix for this hour, with a trailing slash."""

        return join_prefix(common_prefix, self.camera, f"{self.year:04d}",
                           f"{self.month:02d}", f"{self.day:02d}", f"{self.hour:02d}")

    def day_prefix(self, common_prefix: str) -> str:
        return join_prefix(common_prefix, self.camera, f"{self.year:04d}",
                           f"{self.month:02d}", f"{self.day:02d}")

    def month_prefix(self, common_prefix: str) -> str:
        return join_prefix(common_prefix, self.camera, f"{self.year:04d}",
                           f"{self.month:02d}")

    def year_prefix(self, common_prefix: str) -> str:
        return join_prefix(common_prefix, self.camera, f"{self.year:04d}")

    def camera_prefix(self, common_prefix: str) -> str:
        return join_prefix(common_prefix, self.camera)

    def sort_key(self) -> Tuple[int, int, int, int]:
        return (self.year, self.month, self.day, self.hour)


@dataclass(frozen=True)
class Clip:
    """A single recording inside an hour folder."""

    key: str
    filename: str
    #: Offset of the recording inside its hour as ``MM:SS``, e.g. ``"14:05"``.
    time: str
    truncated: bool
    minute: int
    second: int
    hour: Optional[int] = None
    size: Optional[int] = None

    @property
    def label(self) -> str:
        """Text used in the UI; marks incomplete recordings."""

        return f"{self.time} (incomplete)" if self.truncated else self.time

    @property
    def absolute_time(self) -> Optional[str]:
        """Wall-clock start ``HH:MM:SS`` when the hour folder is known."""

        if self.hour is None:
            return None
        return f"{self.hour:02d}:{self.minute:02d}:{self.second:02d}"

    def as_dict(self) -> dict:
        """JSON-safe payload for the inline ``hour-clips-data`` script tag."""

        return {
            "key": self.key,
            "filename": self.filename,
            "time": self.time,
            "label": self.label,
            "truncated": self.truncated,
            "hour": self.hour,
            "minute": self.minute,
            "second": self.second,
            "absolute_time": self.absolute_time,
            "size": self.size,
        }


def parse_clip(key: str, size: Optional[int] = None,
               hour: Optional[int] = None) -> Optional[Clip]:
    """Build a :class:`Clip` from an object key, or ``None`` if not a clip.

    ``hour`` is the hour taken from the parent folder, used only to render an
    absolute timestamp for humans and screen readers.
    """

    filename = key.rsplit("/", 1)[-1]
    match = CLIP_NAME_RE.match(filename)
    if not match:
        return None
    minute = int(match.group("minute"))
    second = int(match.group("second"))
    return Clip(
        key=key,
        filename=filename,
        time=f"{minute:02d}:{second:02d}",
        truncated=bool(match.group("truncated")),
        minute=minute,
        second=second,
        hour=int(hour) if hour is not None else None,
        size=size,
    )


def sort_clips(clips: Iterable[Clip]) -> List[Clip]:
    """Order clips chronologically within one hour."""

    return sorted(clips, key=lambda clip: (clip.minute, clip.second, clip.filename))


def month_name(month: int) -> str:
    """``10 -> 'October'``."""

    return calendar.month_name[month]


def weekday_name(hour_ref: HourRef) -> str:
    return calendar.day_name[hour_ref.date.weekday()]


def normalize_common_prefix(common_prefix: str) -> str:
    """Strip stray slashes; an empty result means "cameras live at the bucket root"."""

    return common_prefix.strip("/")


def join_prefix(*parts: Optional[str]) -> str:
    """Join non-empty key parts with ``/`` and a trailing slash.

    Used for S3 prefixes, which must never start with a slash: a leading slash
    is a real key character and would match no object. An empty common prefix
    therefore yields ``camera/2026/`` rather than ``/camera/2026/``.
    """

    pieces = [part for part in parts if part]
    return "/".join(pieces) + "/" if pieces else ""


def url_path(*parts: Optional[str]) -> str:
    """Join non-empty parts into an absolute, trailing-slash URL path."""

    pieces = [part for part in parts if part]
    return "/" + "/".join(pieces) + "/" if pieces else "/"


def parse_path(common_prefix: str, path: str) -> Tuple[str, Optional[HourRef]]:
    """Map a URL path onto a browse level.

    Returns the level name plus, for hour URLs, the parsed :class:`HourRef`.
    ``common_prefix`` is matched but excluded from the returned camera name.

    Raises:
        InvalidPath: when the path does not follow the archive layout.
    """

    common = normalize_common_prefix(common_prefix)
    segments = [segment for segment in path.strip("/").split("/") if segment]

    if not segments:
        return LEVEL_CAMERAS, None
    if common:
        if segments[0] != common:
            raise InvalidPath(f"path must start with the archive prefix {common!r}")
        segments = segments[1:]

    depth = len(segments)
    if depth == 0:
        return LEVEL_CAMERAS, None
    if depth == 1:
        return LEVEL_YEARS, None
    # Index 0 is the camera at every depth, with or without a common prefix.
    if depth == 2:
        if not YEAR_RE.match(segments[1]):
            raise InvalidPath("year segment must be 4 digits")
        return LEVEL_MONTHS, None
    if depth == 3:
        if not YEAR_RE.match(segments[1]) or not MONTH_RE.match(segments[2]):
            raise InvalidPath("month segment must be 2 digits in 01..12")
        return LEVEL_DAYS, None
    if depth == 4:
        if (
            not YEAR_RE.match(segments[1])
            or not MONTH_RE.match(segments[2])
            or not DAY_RE.match(segments[3])
        ):
            raise InvalidPath("day segment must be 2 digits")
        return LEVEL_HOURS, None

    hour_ref = parse_hour_segments(common, segments)
    return LEVEL_CLIPS, hour_ref


def parse_hour_segments(common_prefix_unused: str, segments: Sequence[str]) -> HourRef:
    """Parse ``[camera, year, month, day, hour]`` into an :class:`HourRef`."""

    if len(segments) != 5:
        raise InvalidPath("expected exactly five path segments after the common prefix")
    camera, year_s, month_s, day_s, hour_s = segments
    if not YEAR_RE.match(year_s) or not MONTH_RE.match(month_s):
        raise InvalidPath("invalid year or month segment")
    if not DAY_RE.match(day_s) or not HOUR_RE.match(hour_s):
        raise InvalidPath("invalid day or hour segment")
    hour_ref = HourRef(
        camera=camera,
        year=int(year_s),
        month=int(month_s),
        day=int(day_s),
        hour=int(hour_s),
    )
    # Reject impossible dates such as 31 February.
    try:
        hour_ref.date
    except ValueError as exc:
        raise InvalidPath(str(exc)) from exc
    return hour_ref


def build_path(common_prefix: str, hour_ref: Optional[HourRef] = None,
               camera: Optional[str] = None, year: Optional[int] = None,
               month: Optional[int] = None, day: Optional[int] = None,
               hour: Optional[int] = None) -> str:
    """Build a canonical, trailing-slash URL path for any browse level."""

    common = normalize_common_prefix(common_prefix)
    if hour_ref is not None:
        return url_path(common, hour_ref.camera, f"{hour_ref.year:04d}",
                        f"{hour_ref.month:02d}", f"{hour_ref.day:02d}",
                        f"{hour_ref.hour:02d}")

    if camera is None:
        # The camera list is always served at the site root, whatever the
        # common prefix is.
        return "/"
    parts = [camera]
    for value, width in ((year, 4), (month, 2), (day, 2), (hour, 2)):
        if value is None:
            break
        parts.append(f"{value:0{width}d}")
    return url_path(common, *parts)


def _numeric_children(names: Iterable[str], pattern: re.Pattern) -> List[int]:
    """Keep well-formed numeric directory names, sorted ascending."""

    values = []
    for name in names:
        if pattern.match(name):
            values.append(int(name))
    return sorted(set(values))


def _day_exists(ref: HourRef) -> bool:
    try:
        date(ref.year, ref.month, ref.day)
        return True
    except ValueError:
        return False


def _hours_of(list_children: ListChildren, day_prefix: str) -> List[int]:
    return _numeric_children(list_children(day_prefix), HOUR_RE)


def _days_of(list_children: ListChildren, month_prefix: str, year: int, month: int) -> List[int]:
    days = []
    for day in _numeric_children(list_children(month_prefix), DAY_RE):
        if _day_exists(HourRef(camera="", year=year, month=month, day=day, hour=0)):
            days.append(day)
    return days


def _months_of(list_children: ListChildren, year_prefix: str) -> List[int]:
    return _numeric_children(list_children(year_prefix), MONTH_RE)


def _candidate_days_forward(list_children: ListChildren, camera_prefix: str,
                            start: HourRef) -> Iterable[Tuple[int, int, int]]:
    """Yield ``(year, month, day)`` tuples after ``start``, in date order.

    Only days that actually exist as prefixes are yielded, and the current day
    is excluded so the caller can look at later hours of that day first.
    """

    years = _numeric_children(list_children(camera_prefix), YEAR_RE)
    for year in years:
        if year < start.year:
            continue
        year_prefix = f"{camera_prefix}{year:04d}/"
        for month in _months_of(list_children, year_prefix):
            if year == start.year and month < start.month:
                continue
            month_prefix = f"{year_prefix}{month:02d}/"
            for day in _days_of(list_children, month_prefix, year, month):
                if year == start.year and month == start.month and day <= start.day:
                    continue
                yield year, month, day


def _candidate_days_backward(list_children: ListChildren, camera_prefix: str,
                             start: HourRef) -> Iterable[Tuple[int, int, int]]:
    """Inverse of :func:`_candidate_days_forward`: strictly earlier days, newest first."""

    years = _numeric_children(list_children(camera_prefix), YEAR_RE)
    for year in reversed(years):
        if year > start.year:
            continue
        year_prefix = f"{camera_prefix}{year:04d}/"
        for month in reversed(_months_of(list_children, year_prefix)):
            if year == start.year and month > start.month:
                continue
            month_prefix = f"{year_prefix}{month:02d}/"
            for day in reversed(_days_of(list_children, month_prefix, year, month)):
                if year == start.year and month == start.month and day >= start.day:
                    continue
                yield year, month, day


def _within_span(candidate: HourRef, start: HourRef, max_year_span: int) -> bool:
    return abs(candidate.year - start.year) <= max_year_span


def find_next_hour(list_children: ListChildren, common_prefix: str, current: HourRef,
                   max_year_span: int = 5) -> Optional[HourRef]:
    """Nearest hour after ``current`` that contains clips, crossing day/month/year gaps.

    Empty hours, empty days, empty months and empty years are skipped without
    scanning them one by one: each calendar level is listed once, so the number
    of S3 requests stays small even when the archive has long gaps.

    Returns ``None`` when the archive holds no later hour within
    ``max_year_span`` calendar years.
    """

    common = normalize_common_prefix(common_prefix)
    camera_prefix = current.camera_prefix(common)

    later_hours = [h for h in _hours_of(list_children, current.day_prefix(common)) if h > current.hour]
    if later_hours:
        candidate = HourRef(current.camera, current.year, current.month, current.day, later_hours[0])
        return candidate if _within_span(candidate, current, max_year_span) else None

    for year, month, day in _candidate_days_forward(list_children, camera_prefix, current):
        if not _within_span(HourRef(current.camera, year, month, day, 0), current, max_year_span):
            break
        hours = _hours_of(list_children, f"{camera_prefix}{year:04d}/{month:02d}/{day:02d}/")
        if hours:
            return HourRef(current.camera, year, month, day, hours[0])
    return None


def find_previous_hour(list_children: ListChildren, common_prefix: str, current: HourRef,
                       max_year_span: int = 5) -> Optional[HourRef]:
    """Nearest hour before ``current`` that contains clips, crossing calendar gaps."""

    common = normalize_common_prefix(common_prefix)
    camera_prefix = current.camera_prefix(common)

    earlier_hours = [h for h in _hours_of(list_children, current.day_prefix(common)) if h < current.hour]
    if earlier_hours:
        candidate = HourRef(current.camera, current.year, current.month, current.day, earlier_hours[-1])
        return candidate if _within_span(candidate, current, max_year_span) else None

    for year, month, day in _candidate_days_backward(list_children, camera_prefix, current):
        if not _within_span(HourRef(current.camera, year, month, day, 0), current, max_year_span):
            break
        hours = _hours_of(list_children, f"{camera_prefix}{year:04d}/{month:02d}/{day:02d}/")
        if hours:
            return HourRef(current.camera, year, month, day, hours[-1])
    return None


#: Periods a listing page can offer previous/next links for. A year page has no
#: entry: the camera page lists every year at once.
PERIOD_MONTH = "month"
PERIOD_DAY = "day"


def _period_boundaries(period: str, current: HourRef) -> Tuple[HourRef, HourRef]:
    """Anchor hours whose own period is excluded from the hour search.

    A listing page has no hour, so the anchors sit at the extremes: the search
    looks for hours strictly after the forward anchor and strictly before the
    backward one, which makes both find nothing inside the current period and
    move on to a neighbouring day or month. The forward month anchor is the
    last day, because the day candidates it walks skip every day up to and
    including the anchor's own day.
    """

    if period == PERIOD_MONTH:
        last_day = calendar.monthrange(current.year, current.month)[1]
        back = HourRef(current.camera, current.year, current.month, 1, 0)
        forward = HourRef(current.camera, current.year, current.month, last_day, 23)
    elif period == PERIOD_DAY:
        back = HourRef(current.camera, current.year, current.month, current.day, 0)
        forward = HourRef(current.camera, current.year, current.month, current.day, 23)
    else:  # pragma: no cover - guarded by the callers
        raise ValueError(f"unsupported period: {period!r}")
    return back, forward


def previous_period(list_children: ListChildren, common_prefix: str, period: str,
                    current: HourRef, max_year_span: int = 5) -> Optional[HourRef]:
    """Nearest earlier day or month holding clips, skipping empty ones."""

    back, _ = _period_boundaries(period, current)
    return find_previous_hour(list_children, common_prefix, back,
                              max_year_span=max_year_span)


def next_period(list_children: ListChildren, common_prefix: str, period: str,
                current: HourRef, max_year_span: int = 5) -> Optional[HourRef]:
    """Nearest later day or month holding clips, skipping empty ones."""

    _, forward = _period_boundaries(period, current)
    return find_next_hour(list_children, common_prefix, forward,
                          max_year_span=max_year_span)


def period_navigation(list_children: ListChildren, common_prefix: str, period: str,
                      current: HourRef,
                      max_year_span: int = 5) -> Tuple[Optional[HourRef], Optional[HourRef]]:
    """ ``(previous, next)`` period refs for one listing page."""

    back, forward = _period_boundaries(period, current)
    return (
        find_previous_hour(list_children, common_prefix, back, max_year_span=max_year_span),
        find_next_hour(list_children, common_prefix, forward, max_year_span=max_year_span),
    )
