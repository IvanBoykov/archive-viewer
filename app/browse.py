"""Turns an archive path into everything a template needs to render a page.

Each page performs one S3 listing (plus a bounded calendar walk for the
previous/next hour links on an hour page), so no request walks the bucket
recursively.
"""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from .archive import (
    DAY_RE,
    HOUR_RE,
    LEVEL_CAMERAS,
    LEVEL_CLIPS,
    LEVEL_DAYS,
    LEVEL_HOURS,
    LEVEL_MONTHS,
    LEVEL_YEARS,
    MONTH_RE,
    PERIOD_DAY,
    PERIOD_MONTH,
    YEAR_RE,
    Clip,
    HourRef,
    build_path,
    find_next_hour,
    find_previous_hour,
    join_prefix,
    month_name,
    normalize_common_prefix,
    parse_hour_segments,
    parse_path,
    period_navigation,
    weekday_name,
)
from .s3 import ArchiveStore

logger = logging.getLogger(__name__)

#: Directory names each level of the layout accepts, used to tell a real folder
#: from one the archive layout does not describe.
_LEVEL_NAME_RE = {
    LEVEL_YEARS: YEAR_RE,
    LEVEL_MONTHS: MONTH_RE,
    LEVEL_DAYS: DAY_RE,
    LEVEL_HOURS: HOUR_RE,
}

#: Listing levels that offer previous/next links, and the period they navigate.
#: Levels are named after the folders they list, so LEVEL_DAYS is the month page
#: and LEVEL_HOURS the day page. Cameras and years list everything at once, and
#: the hour page already has its own navigation, so none of them appear here.
_PERIOD_LEVELS = {
    LEVEL_DAYS: PERIOD_MONTH,
    LEVEL_HOURS: PERIOD_DAY,
}


class LevelNotFound(Exception):
    """The requested path does not exist in the archive."""


@dataclass(frozen=True)
class Suggestion:
    """A page the error page can offer instead of only the camera list."""

    url: str
    label: str


_EXPECTED_SEGMENTS = (("year", YEAR_RE), ("month", MONTH_RE),
                      ("day", DAY_RE), ("hour", HOUR_RE))


def _deepest_page(common: str, camera: Optional[str], year: Optional[int],
                  month: Optional[int], day: Optional[int],
                  hour: Optional[int]) -> Suggestion:
    """The deepest page the given (possibly partial) address describes."""

    if camera is None:
        return Suggestion("/", "the camera list")
    if year is None:
        return Suggestion(build_path(common, camera=camera),
                          f"the {pretty_name(camera)} page")
    if month is None:
        return Suggestion(build_path(common, camera=camera, year=year),
                          f"{year} on {pretty_name(camera)}")
    if day is None:
        return Suggestion(build_path(common, camera=camera, year=year, month=month),
                          f"{month_name(month)} {year} on {pretty_name(camera)}")
    ref = HourRef(camera=camera, year=year, month=month, day=day, hour=hour or 0)
    if hour is None:
        return Suggestion(build_path(common, camera=camera, year=year, month=month,
                                     day=day),
                          f"{date_label(ref)} on {pretty_name(camera)}")
    return Suggestion(build_path(common, hour_ref=ref),
                      f"{hour:02d}:00 on {date_label(ref)} at {pretty_name(camera)}")


def _bad_segment_reason(kind: str, segment: str) -> str:
    if kind == "year":
        return f"'{segment}' is not a year; year folders look like 2026."
    if kind == "month":
        return f"'{segment}' is not a month; month folders look like 01 to 12."
    if kind == "day":
        return (f"'{segment}' is not a day of the month; day folders look "
                f"like 01 to 31.")
    return f"'{segment}' is not an hour; hour folders look like 00 to 23."


def explain_path(common_prefix: str,
                 request_path: str) -> Tuple[Optional[str], Optional[Suggestion]]:
    """Interpret a failing request path for the error page.

    Returns ``(reason, suggestion)``. ``reason`` explains what the layout
    rejected, or ``None`` when every segment is well-formed and the address is
    simply empty of recordings. ``suggestion`` points at the deepest page the
    valid prefix describes - the month page for a day like ``112``, the day
    page for an hour like ``25`` - or, when the whole path is well-formed, at
    its parent, so an empty hour links its day rather than the site root.
    """

    common = normalize_common_prefix(common_prefix)
    segments = [s for s in request_path.strip("/").split("/") if s]
    # The site root always renders the camera list, prefix or no prefix.
    if not segments:
        return None, None
    if common:
        if segments[0] != common:
            return (f"The archive lives under /{common}/, so this address "
                    f"cannot name a folder inside it.",
                    Suggestion("/", "the camera list"))
        segments = segments[1:]
    if not segments:
        return None, None

    camera, *rest = segments
    year = month = day = hour = None
    for index, segment in enumerate(rest):
        if index >= len(_EXPECTED_SEGMENTS):
            target = _deepest_page(common, camera, year, month, day, hour)
            return (f"Nothing in the archive lives below {target.label}.", target)
        kind, pattern = _EXPECTED_SEGMENTS[index]
        if not pattern.match(segment):
            target = _deepest_page(common, camera, year, month, day, hour)
            return _bad_segment_reason(kind, segment), target
        value = int(segment)
        if kind == "year":
            year = value
        elif kind == "month":
            month = value
        elif kind == "day":
            day = value
            try:
                datetime(year, month, day)
            except ValueError:
                target = _deepest_page(common, camera, year, month, None, None)
                return (f"There is no day {segment} in {month_name(month)} "
                        f"{year}.", target)
        else:
            hour = value

    # The whole path is well-formed and simply holds no recordings, so offer
    # the level above it: an empty hour offers its day, an empty day its month.
    parts: List[object] = [camera, year, month, day, hour]
    depth = 1 + sum(1 for value in parts[1:] if value is not None)
    parent = parts[: depth - 1] + [None] * (6 - depth)
    return None, _deepest_page(common, *parent)  # type: ignore[arg-type]


@dataclass
class Entry:
    """One row of a directory listing.

    ``url`` is ``None`` for a folder that does not fit the archive layout, so it
    is shown without pretending it can be opened.
    """

    url: Optional[str]
    label: str
    kind: str = "directory"
    key: Optional[str] = None

    def as_dict(self) -> dict:
        return {"url": self.url, "label": self.label, "kind": self.kind, "key": self.key}


@dataclass(frozen=True)
class PeriodRef:
    """A month or a day of one camera.

    Separate from :class:`HourRef` so a previous/next link from a month or day
    listing can only ever point at a page of the same level.
    """

    camera: str
    year: int
    month: int
    day: Optional[int] = None

    @classmethod
    def from_hour(cls, hour_ref: HourRef, period: str) -> "PeriodRef":
        day = hour_ref.day if period == PERIOD_DAY else None
        return cls(hour_ref.camera, hour_ref.year, hour_ref.month, day)

    @property
    def period(self) -> str:
        return PERIOD_DAY if self.day is not None else PERIOD_MONTH

    def url(self, common_prefix: str) -> str:
        return build_path(common_prefix, camera=self.camera, year=self.year,
                          month=self.month, day=self.day)

    def label(self) -> str:
        if self.day is None:
            return f"{month_name(self.month)} {self.year}"
        ref = HourRef(camera=self.camera, year=self.year, month=self.month,
                      day=self.day, hour=0)
        return date_label(ref)


@dataclass
class YearGroup:
    """One year heading on the camera page, with the months inside it.

    ``url`` targets the year page, which stays reachable as its own listing and
    as a breadcrumb step.
    """

    url: str
    label: str
    entries: List[Entry] = field(default_factory=list)


@dataclass
class Crumb:
    url: str
    label: str


@dataclass
class PageContext:
    """Render context shared by every SSR page."""

    level: str
    title: str
    heading: str
    subheading: Optional[str]
    breadcrumbs: List[Crumb]
    entries: List[Entry] = field(default_factory=list)
    clips: List[Clip] = field(default_factory=list)
    hour: Optional[HourRef] = None
    prev_hour: Optional[HourRef] = None
    next_hour: Optional[HourRef] = None
    period: Optional[str] = None
    prev_period: Optional[PeriodRef] = None
    next_period: Optional[PeriodRef] = None
    year_groups: List[YearGroup] = field(default_factory=list)
    hour_meta: Dict[str, object] = field(default_factory=dict)
    empty_message: Optional[str] = None


class BrowseService:
    """Builds page contexts for one configured archive."""

    def __init__(self, store: ArchiveStore) -> None:
        self.store = store
        self.settings = store.settings

    # -- public API ------------------------------------------------------
    def page_for_path(self, request_path: str) -> PageContext:
        """Build the page context for a URL path.

        Raises:
            InvalidPath: the path violates the archive key layout.
            LevelNotFound: the path is well-formed but nothing is stored there.
        """

        common = normalize_common_prefix(self.settings.common_prefix)
        level, hour_ref = parse_path(common, request_path)
        if hour_ref is not None:
            return self._hour_page(hour_ref)
        return self._directory_page(common, level, request_path)

    def hour_url(self, ref: Optional[HourRef]) -> Optional[str]:
        """URL of an hour page, used for cross-boundary navigation links."""

        if ref is None:
            return None
        return build_path(self.settings.common_prefix, hour_ref=ref)

    def parse_hour(self, segments: List[str]) -> HourRef:
        """Parse path segments into an :class:`HourRef` (validation helper)."""

        return parse_hour_segments(self.settings.common_prefix, segments)

    # -- directory levels ------------------------------------------------
    def _directory_page(self, common: str, level: str, request_path: str) -> PageContext:
        segments = [s for s in request_path.strip("/").split("/") if s]
        if common and segments and segments[0] == common:
            segments = segments[1:]
        camera = segments[0] if len(segments) >= 1 else None
        year = int(segments[1]) if len(segments) >= 2 else None
        month = int(segments[2]) if len(segments) >= 3 else None
        day = int(segments[3]) if len(segments) >= 4 else None

        prefix = self._prefix_for(common, camera, year, month, day)
        # The root level lists the folder that holds all cameras, which is the
        # bucket itself when the archive has no common prefix.
        list_prefix = prefix
        directories, files = self.store.list_level(list_prefix)

        if not directories and not files and level != LEVEL_CAMERAS:
            raise LevelNotFound(request_path)

        entries = [self._directory_entry(common, level, camera, year, month, day, name)
                   for name in directories]
        # Clips only ever live inside an hour folder, but listing a stray object
        # beats silently hiding data from the browser.
        entries.extend(
            Entry(url=_join_url(prefix, name), label=name, kind="file")
            for name, _size in files
            if level != LEVEL_CAMERAS
        )

        # A camera page groups its months under a heading per year, so it needs
        # one extra listing per year: still one call per level, never a scan.
        groups: List[YearGroup] = []
        if level == LEVEL_YEARS and camera is not None:
            groups, entries = self._year_groups(common, camera, entries)

        prev_ref, next_ref = (None, None)
        if camera is not None and level in _PERIOD_LEVELS:
            prev_ref, next_ref = self._period_neighbours(
                common, _PERIOD_LEVELS[level],
                HourRef(camera=camera, year=year or 0, month=month or 0,
                        day=day or 1, hour=0))

        context = PageContext(
            level=level,
            title=self._title(level, camera, year, month, day),
            heading=self._heading(level, camera, year, month, day),
            subheading=self._subheading(level),
            breadcrumbs=self._breadcrumbs(common, camera, year, month, day),
            entries=entries,
            period=_PERIOD_LEVELS.get(level),
            prev_period=prev_ref,
            next_period=next_ref,
            year_groups=groups,
        )
        if not entries and not groups:
            context.empty_message = "No recordings have been archived here yet."
        return context

    def _year_groups(self, common: str, camera: str,
                     entries: List[Entry]) -> Tuple[List[YearGroup], List[Entry]]:
        """Group the months of each year under a year heading.

        Returns the groups plus any entry that is not a year, so a stray folder
        or object stays visible instead of being dropped with no heading.
        """

        groups: List[YearGroup] = []
        leftovers: List[Entry] = []
        for entry in entries:
            if entry.kind != "directory" or not YEAR_RE.match(entry.label):
                leftovers.append(entry)
                continue
            year = int(entry.label)
            months = self._month_entries(common, camera, year)
            groups.append(YearGroup(url=entry.url or "", label=entry.label,
                                    entries=months))
        return groups, leftovers

    def _month_entries(self, common: str, camera: str, year: int) -> List[Entry]:
        """List the months of one year, labelled and linked like the year page."""

        prefix = join_prefix(common, camera, f"{year:04d}")
        return [self._directory_entry(common, LEVEL_MONTHS, camera, year, None, None, name)
                for name in self.store.list_child_directories(prefix)]

    def _period_neighbours(self, common: str, period: str,
                           current: HourRef) -> Tuple[Optional[PeriodRef], Optional[PeriodRef]]:
        """Previous/next period of the same level, skipping periods with no clips."""

        list_children = self.store.list_child_directories
        span = self.settings.max_year_span
        prev_hour, next_hour = period_navigation(list_children, common, period, current,
                                                 max_year_span=span)
        to_ref = lambda ref: PeriodRef.from_hour(ref, period) if ref else None
        return to_ref(prev_hour), to_ref(next_hour)

    def _directory_entry(self, common: str, level: str, camera: Optional[str],
                         year: Optional[int], month: Optional[int],
                         day: Optional[int], name: str) -> Entry:
        """Build one listing row, flagging folders that do not fit the layout.

        Buckets accumulate folders the layout never describes - a ``latest``
        pointer, a 31st day in a 30-day month, a four-digit camera name that
        would collide with a year. Linking those yields a server error and
        hiding them loses data, so they render as plain text instead.
        """

        pattern = _LEVEL_NAME_RE.get(level)
        if pattern is not None and not pattern.match(name):
            logger.info("folder outside the archive layout at %s: %r", level, name)
            return Entry(url=None, label=name, kind="unknown")
        if level == LEVEL_CAMERAS and YEAR_RE.match(name):
            logger.info("ambiguous four-digit camera name %r, not linking", name)
            return Entry(url=None, label=name, kind="unknown")
        try:
            return Entry(
                url=self._child_url(common, level, camera, year, month, day, name),
                label=self._entry_label(level, name, camera, year, month, day),
            )
        except ValueError as exc:  # includes InvalidPath and impossible dates
            logger.info("cannot link folder %r: %s", name, exc)
            return Entry(url=None, label=name, kind="unknown")

    def _prefix_for(self, common: str, camera: Optional[str], year: Optional[int],
                    month: Optional[int], day: Optional[int]) -> str:
        if camera is None:
            return join_prefix(common)
        parts = [common, camera]
        for value, width in ((year, 4), (month, 2), (day, 2)):
            if value is None:
                break
            parts.append(f"{value:0{width}d}")
        return join_prefix(*parts)

    def _child_url(self, common: str, level: str, camera: Optional[str], year: Optional[int],
                   month: Optional[int], day: Optional[int], name: str) -> str:
        if level == LEVEL_CAMERAS:
            return build_path(common, camera=name)
        if level == LEVEL_YEARS:
            return build_path(common, camera=camera, year=int(name))
        if level == LEVEL_MONTHS:
            return build_path(common, camera=camera, year=year, month=int(name))
        if level == LEVEL_DAYS:
            return build_path(common, camera=camera, year=year, month=month, day=int(name))
        if level == LEVEL_HOURS:
            return build_path(common, camera=camera, year=year, month=month, day=day,
                              hour=int(name))
        return build_path(common)

    def _entry_label(self, level: str, name: str, camera: Optional[str], year: Optional[int],
                     month: Optional[int], day: Optional[int]) -> str:
        if level == LEVEL_CAMERAS:
            return pretty_name(name)
        if level == LEVEL_YEARS:
            return name
        if level == LEVEL_MONTHS:
            return month_name(int(name))
        if level == LEVEL_DAYS:
            ref = HourRef(camera=camera or "", year=year or 0, month=month or 0,
                          day=int(name), hour=0)
            return f"{int(name):02d} {month_name(month or 1)} {year} ({weekday_name(ref)})"
        if level == LEVEL_HOURS:
            return hour_label(int(name))
        return name

    def _breadcrumbs(self, common: str, camera: Optional[str], year: Optional[int],
                     month: Optional[int], day: Optional[int]) -> List[Crumb]:
        crumbs = [Crumb(url="/", label="Archive")]
        if camera is None:
            return crumbs
        crumbs.append(Crumb(url=build_path(common, camera=camera), label=pretty_name(camera)))
        if year is None:
            return crumbs
        crumbs.append(Crumb(url=build_path(common, camera=camera, year=year), label=str(year)))
        if month is None:
            return crumbs
        crumbs.append(
            Crumb(url=build_path(common, camera=camera, year=year, month=month),
                  label=month_name(month))
        )
        if day is None:
            return crumbs
        crumbs.append(
            Crumb(url=build_path(common, camera=camera, year=year, month=month, day=day),
                  label=date_label(HourRef(camera, year or 0, month or 0, day or 0, 0)))
        )
        return crumbs

    def _title(self, level: str, camera: Optional[str], year: Optional[int],
               month: Optional[int], day: Optional[int]) -> str:
        # Same words as the heading, which already names the camera: the base
        # template appends the site name, so the root reads "Cameras · Video
        # archive" instead of "Video archive · Video archive", and a day page
        # tab reads "Dacha - 13 October 2026" instead of a bare "13".
        return self._heading(level, camera, year, month, day)

    def _heading(self, level: str, camera: Optional[str], year: Optional[int],
                 month: Optional[int], day: Optional[int]) -> str:
        if level == LEVEL_CAMERAS:
            return "Cameras"
        if level == LEVEL_YEARS:
            return pretty_name(camera or "")
        if level == LEVEL_MONTHS:
            return f"{pretty_name(camera or '')} - {year}"
        if level == LEVEL_DAYS:
            return f"{pretty_name(camera or '')} - {month_name(month or 1)} {year}"
        if level == LEVEL_HOURS:
            ref = HourRef(camera=camera or "", year=year or 0, month=month or 0, day=day or 0,
                          hour=0)
            return f"{pretty_name(camera or '')} - {date_label(ref)}"
        return "Archive"

    def _subheading(self, level: str) -> Optional[str]:
        hints = {
            LEVEL_CAMERAS: "Select a camera",
            LEVEL_YEARS: "Recorded months, grouped by year",
            LEVEL_MONTHS: "Select a month",
            LEVEL_DAYS: "Select a day",
            LEVEL_HOURS: "Select an hour to list its clips",
        }
        return hints.get(level)

    # -- hour level ------------------------------------------------------
    def _hour_page(self, ref: HourRef) -> PageContext:
        common = normalize_common_prefix(self.settings.common_prefix)
        clips = self.store.list_clips(ref.prefix(common), hour=ref.hour)
        if not clips:
            raise LevelNotFound(ref.prefix(common))

        list_children = self.store.list_child_directories
        span = self.settings.max_year_span
        prev_hour = find_previous_hour(list_children, common, ref, max_year_span=span)
        next_hour = find_next_hour(list_children, common, ref, max_year_span=span)

        breadcrumbs = self._breadcrumbs(common, ref.camera, ref.year, ref.month, ref.day)
        breadcrumbs.append(Crumb(url=build_path(common, hour_ref=ref), label=f"{ref.hour:02d}:00"))

        return PageContext(
            level=LEVEL_CLIPS,
            title=f"{pretty_name(ref.camera)} - {hour_ref_label(ref)}",
            heading=f"{pretty_name(ref.camera)} - {date_label(ref)}",
            subheading=f"Clips recorded between {hour_label(ref.hour)}",
            breadcrumbs=breadcrumbs,
            clips=clips,
            hour=ref,
            prev_hour=prev_hour,
            next_hour=next_hour,
            hour_meta={
                "url": build_path(common, hour_ref=ref),
                "camera": ref.camera,
                "camera_label": pretty_name(ref.camera),
                "date": f"{ref.year:04d}-{ref.month:02d}-{ref.day:02d}",
                "date_label": date_label(ref),
                "weekday": weekday_name(ref),
                "hour": f"{ref.hour:02d}:00",
                "hour_label": hour_label(ref.hour),
                "count": len(clips),
            },
        )


def pretty_name(raw: str) -> str:
    """Human-readable form of a camera name (``front_door`` -> ``Front door``)."""

    spaced = raw.replace("_", " ").replace("-", " ").strip()
    return (spaced[:1].upper() + spaced[1:]) if spaced else raw


def hour_label(hour: int) -> str:
    """``19`` -> ``19:00 - 19:59``."""

    return f"{hour:02d}:00 - {hour:02d}:59"


def date_label(ref: HourRef) -> str:
    """``2026-10-04`` -> ``04 October 2026``."""

    return f"{ref.day:02d} {month_name(ref.month)} {ref.year}"


def hour_ref_label(ref: HourRef) -> str:
    return f"{date_label(ref)}, {ref.hour:02d}:00"


def hour_navigation_urls(context: PageContext, common_prefix: str) -> Dict[str, Optional[str]]:
    """Plain and hash-suffixed URLs for the previous/next hour of an hour page."""

    def url(ref: Optional[HourRef]) -> Optional[str]:
        return build_path(common_prefix, hour_ref=ref) if ref else None

    return {
        "previous": url(context.prev_hour),
        "next": url(context.next_hour),
        "previous_autolast": _with_fragment(url(context.prev_hour), "auto-last"),
        "next_autofirst": _with_fragment(url(context.next_hour), "auto-first"),
    }


def _with_fragment(url: Optional[str], fragment: str) -> Optional[str]:
    return f"{url}#{fragment}" if url else None


def _join_url(prefix: str, name: str) -> str:
    return f"/{prefix}{name}" if prefix else f"/{name}"
