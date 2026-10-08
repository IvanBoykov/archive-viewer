"""Unit tests for the pure archive logic: labels, parsing, boundary search."""

from __future__ import annotations

import pytest

from app.archive import (
    HourRef,
    InvalidPath,
    build_path,
    find_next_hour,
    find_previous_hour,
    month_name,
    parse_clip,
    parse_path,
)

COMMON = "home"


def children_map(**entries):
    """Fake ``list_children`` from a prefix -> child-name mapping."""

    def list_children(prefix: str):
        return list(entries.get(prefix, []))

    return list_children


def test_month_name_is_human_readable():
    assert month_name(10) == "October"
    assert month_name(1) == "January"


def test_parse_clip_reads_minute_second_and_truncation():
    clip = parse_clip("home/cam/2026/10/04/19/14-05-truncated.mp4", size=10, hour=19)
    assert clip is not None
    assert clip.time == "14:05"
    assert clip.truncated is True
    assert clip.absolute_time == "19:14:05"
    assert clip.label == "14:05 (incomplete)"


def test_parse_clip_rejects_other_names():
    assert parse_clip("home/cam/2026/10/04/19/notes.txt") is None
    assert parse_clip("home/cam/2026/10/04/19/99-99.mp4") is None


@pytest.mark.parametrize(
    "path, expected_level",
    [
        ("/", "cameras"),
        ("/home/", "cameras"),
        ("/home/front/", "years"),
        ("/home/front/2026/", "months"),
        ("/home/front/2026/10/", "days"),
        ("/home/front/2026/10/04/", "hours"),
    ],
)
def test_parse_path_levels(path, expected_level):
    level, hour = parse_path(COMMON, path)
    assert level == expected_level
    assert hour is None


def test_parse_path_hour_level():
    level, hour = parse_path(COMMON, "/home/front/2026/10/04/19/")
    assert level == "clips"
    assert hour == HourRef("front", 2026, 10, 4, 19)


def test_parse_path_rejects_impossible_dates():
    with pytest.raises(InvalidPath):
        parse_path(COMMON, "/home/front/2026/02/31/12/")
    with pytest.raises(InvalidPath):
        parse_path(COMMON, "/home/front/2026-10-04/12/")
    with pytest.raises(InvalidPath):
        parse_path(COMMON, "/other/front/2026/")


def test_build_path_is_canonical():
    assert build_path(COMMON) == "/"
    assert build_path(COMMON, camera="front") == "/home/front/"
    assert build_path(COMMON, camera="front", year=2026, month=1, day=2, hour=3) == (
        "/home/front/2026/01/02/03/"
    )
    ref = HourRef("front", 2026, 12, 31, 23)
    assert build_path(COMMON, hour_ref=ref) == "/home/front/2026/12/31/23/"
    assert ref.prefix(COMMON) == "home/front/2026/12/31/23/"


def test_next_hour_skips_empty_hours_in_same_day():
    list_children = children_map(**{"home/front/2026/10/04/": ["19", "22"]})
    current = HourRef("front", 2026, 10, 4, 19)
    assert find_next_hour(list_children, COMMON, current) == HourRef("front", 2026, 10, 4, 22)


def test_previous_hour_skips_empty_hours_in_same_day():
    list_children = children_map(**{"home/front/2026/10/04/": ["19", "22"]})
    current = HourRef("front", 2026, 10, 4, 22)
    assert find_previous_hour(list_children, COMMON, current) == HourRef("front", 2026, 10, 4, 19)


def test_navigation_crosses_empty_days():
    """06/03/2026 is the only other day; 05/10/04 hour 22 must reach it."""

    tree = {
        "home/front/": ["2026"],
        "home/front/2026/": ["10"],
        "home/front/2026/10/": ["04", "06"],
        "home/front/2026/10/04/": ["22"],
        "home/front/2026/10/05/": [],
        "home/front/2026/10/06/": ["03"],
    }
    list_children = children_map(**tree)
    current = HourRef("front", 2026, 10, 4, 22)
    assert find_next_hour(list_children, COMMON, current) == HourRef("front", 2026, 10, 6, 3)
    assert find_previous_hour(list_children, COMMON, HourRef("front", 2026, 10, 6, 3)) == (
        HourRef("front", 2026, 10, 4, 22)
    )


def test_navigation_crosses_month_boundary():
    tree = {
        "home/front/": ["2026"],
        "home/front/2026/": ["10", "11"],
        "home/front/2026/10/": ["31"],
        "home/front/2026/10/31/": ["23"],
        "home/front/2026/11/": ["01"],
        "home/front/2026/11/01/": ["00"],
    }
    list_children = children_map(**tree)
    assert find_next_hour(list_children, COMMON, HourRef("front", 2026, 10, 31, 23)) == (
        HourRef("front", 2026, 11, 1, 0)
    )
    assert find_previous_hour(list_children, COMMON, HourRef("front", 2026, 11, 1, 0)) == (
        HourRef("front", 2026, 10, 31, 23)
    )


def test_navigation_crosses_year_boundary():
    tree = {
        "home/gapcam/": ["2025", "2026"],
        "home/gapcam/2025/": ["12"],
        "home/gapcam/2025/12/": ["31"],
        "home/gapcam/2025/12/31/": ["23"],
        "home/gapcam/2026/": ["01"],
        "home/gapcam/2026/01/": ["01"],
        "home/gapcam/2026/01/01/": ["00"],
    }
    list_children = children_map(**tree)
    assert find_next_hour(list_children, COMMON, HourRef("gapcam", 2025, 12, 31, 23)) == (
        HourRef("gapcam", 2026, 1, 1, 0)
    )


def test_navigation_skips_empty_months_and_hours():
    """Only a single clip exists two months later, in the 23rd hour."""

    tree = {
        "home/front/": ["2026"],
        "home/front/2026/": ["01", "02", "03"],
        "home/front/2026/01/": ["01"],
        "home/front/2026/01/01/": ["00"],
        "home/front/2026/02/": [],
        "home/front/2026/03/": ["15"],
        "home/front/2026/03/15/": ["23"],
    }
    list_children = children_map(**tree)
    assert find_next_hour(list_children, COMMON, HourRef("front", 2026, 1, 1, 0)) == (
        HourRef("front", 2026, 3, 15, 23)
    )
    assert find_previous_hour(list_children, COMMON, HourRef("front", 2026, 3, 15, 23)) == (
        HourRef("front", 2026, 1, 1, 0)
    )


def test_navigation_returns_none_at_archive_edges():
    tree = {
        "home/front/": ["2026"],
        "home/front/2026/": ["10"],
        "home/front/2026/10/": ["04"],
        "home/front/2026/10/04/": ["00"],
    }
    list_children = children_map(**tree)
    only = HourRef("front", 2026, 10, 4, 0)
    assert find_next_hour(list_children, COMMON, only) is None
    assert find_previous_hour(list_children, COMMON, only) is None


def test_year_span_limit_bounds_the_search():
    far = {
        "home/front/": ["2026", "2040"],
        "home/front/2026/": ["10"],
        "home/front/2026/10/": ["04"],
        "home/front/2026/10/04/": ["00"],
        "home/front/2040/": ["05"],
        "home/front/2040/05/": ["20"],
        "home/front/2040/05/20/": ["11"],
    }
    list_children = children_map(**far)
    current = HourRef("front", 2026, 10, 4, 0)
    assert find_next_hour(list_children, COMMON, current, max_year_span=5) is None
    assert find_next_hour(list_children, COMMON, current, max_year_span=20) == (
        HourRef("front", 2040, 5, 20, 11)
    )
