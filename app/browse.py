"""Turns an archive path into everything a template needs to render a page.

Each page performs one S3 listing (plus a bounded calendar walk for the
previous/next hour links on an hour page), so no request walks the bucket
recursively.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

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
        )
        if not entries:
            context.empty_message = "No recordings have been archived here yet."
        return context

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
        parts: List[str] = []
        if camera:
            parts.append(pretty_name(camera))
        if year:
            parts.append(str(year))
        if month:
            parts.append(month_name(month))
        if day:
            parts.append(str(day))
        return " - ".join(parts) if parts else "Video archive"

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
            LEVEL_YEARS: "Select a year",
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
