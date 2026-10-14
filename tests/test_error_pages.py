"""Tests for the 404 pages: what broke, and where the page offers to go next.

A 404 that only links the camera list throws away what the address already
said. Day 112 is not a day, but the camera, year and month around it are real,
so the page offers the month; an empty hour offers its day. Paths are built at
runtime so no date-shaped literal appears in this file.
"""

from __future__ import annotations

import re

from conftest import COMMON_PREFIX

from app.archive import build_path, url_path
from app.browse import explain_path

CAMERA = "front"


def camera_page(camera=CAMERA):
    return build_path(COMMON_PREFIX, camera=camera)


def year_page(year=2026, camera=CAMERA):
    return build_path(COMMON_PREFIX, camera=camera, year=year)


def month_page(year=2026, month=10, camera=CAMERA):
    return build_path(COMMON_PREFIX, camera=camera, year=year, month=month)


def day_page(year=2026, month=10, day=4, camera=CAMERA):
    return build_path(COMMON_PREFIX, camera=camera, year=year, month=month, day=day)


def hour_page(year=2026, month=10, day=4, hour=19, camera=CAMERA):
    return build_path(COMMON_PREFIX, camera=camera, year=year, month=month, day=day,
                      hour=hour)


def page_at(**values):
    """Address of a day or hour folder with one level replaced by junk.

    ``build_path`` only ever emits valid digits, so an out-of-range value is
    spliced in as the folder name: ``page_at(day=112)`` addresses the day
    folder ``112`` of the fixture month. Values are padded like real folders.
    """

    parts = {"year": 2026, "month": 10, "day": 4, "hour": 19}
    parts.update(values)
    return url_path(COMMON_PREFIX, CAMERA, f"{parts['year']:04d}",
                    *(f"{parts[name]:02d}" for name in ("month", "day", "hour")))


def malformed_year():
    return camera_page().replace(f"{CAMERA}/", f"{CAMERA}/20xx/")


def error_body(client, path):
    response = client.get(path)
    assert response.status_code == 404, f"{path} should be a 404"
    return response.text


def primary_link(body):
    match = re.search(r'class="btn btn--primary" href="([^"]+)"', body)
    assert match, f"the error page offers no way out: {body}"
    return match.group(1)


# -- what the error page says --------------------------------------------
def test_impossible_day_names_the_day_level(client):
    assert "is not a day of the month" in error_body(client, page_at(day=112))


def test_impossible_hour_names_the_hour_level(client):
    assert "is not an hour" in error_body(client, page_at(hour=25))


def test_impossible_month_names_the_month_level(client):
    assert "is not a month" in error_body(client, page_at(month=13))


def test_impossible_year_names_the_year_level(client):
    assert "is not a year" in error_body(client, malformed_year())


def test_a_day_the_calendar_lacks_is_named(client):
    body = error_body(client, page_at(month=2, day=30))
    assert "February" in body
    assert "is not a day of the month" not in body


def test_a_foreign_prefix_says_where_the_archive_lives(client):
    assert f"under /{COMMON_PREFIX}/" in error_body(client, url_path("other", CAMERA))


def test_an_empty_level_says_so(client):
    assert "No recordings exist" in error_body(client, day_page(day=5))


def test_the_error_page_keeps_one_title(client):
    body = error_body(client, page_at(day=112))
    assert body.count("<h1") == 1
    assert "Video archive" in re.search(r"<title>(.*?)</title>", body).group(1)


# -- where the error page sends you --------------------------------------
def test_an_impossible_day_offers_the_month(client):
    assert primary_link(error_body(client, page_at(day=112))) == month_page()


def test_an_impossible_hour_offers_the_day(client):
    assert primary_link(error_body(client, page_at(hour=25))) == day_page()


def test_an_impossible_month_offers_the_year(client):
    assert primary_link(error_body(client, page_at(month=13))) == year_page()


def test_an_impossible_year_offers_the_camera(client):
    assert primary_link(error_body(client, malformed_year())) == camera_page()


def test_a_day_the_calendar_lacks_offers_the_month(client):
    assert primary_link(error_body(client, page_at(month=2, day=30))) == month_page(month=2)


def test_an_empty_hour_offers_its_day(client):
    assert primary_link(error_body(client, hour_page(hour=0))) == day_page()


def test_an_empty_day_offers_its_month(client):
    assert primary_link(error_body(client, day_page(day=5))) == month_page()


def test_an_unknown_camera_offers_the_camera_list(client):
    assert primary_link(error_body(client, camera_page("nosuchcam"))) == "/"


def test_an_address_below_an_hour_offers_the_hour(client):
    assert primary_link(error_body(client, hour_page() + "clip.mp4/")) == hour_page()


def test_the_fallback_button_only_appears_beside_a_deeper_target(client):
    assert "Back to the camera list" in error_body(client, page_at(day=112))
    assert "Back to the camera list" not in error_body(client, camera_page("nosuchcam"))


# -- the pure helper behind the page -------------------------------------
def explain(path, common=COMMON_PREFIX):
    reason, suggestion = explain_path(common, path)
    return reason, (suggestion.url if suggestion else None)


def test_explain_returns_nothing_for_the_root():
    assert explain("/") == (None, None)
    assert explain(f"/{COMMON_PREFIX}/") == (None, None)


def test_explain_steps_up_one_level_for_a_valid_but_empty_address():
    assert explain(hour_page()) == (None, day_page())
    assert explain(day_page()) == (None, month_page())
    assert explain(month_page()) == (None, year_page())
    assert explain(year_page()) == (None, camera_page())
    assert explain(camera_page()) == (None, "/")


def test_explain_names_the_first_segment_that_cannot_be_right():
    assert explain(malformed_year()) == (
        "'20xx' is not a year; year folders look like 2026.",
        camera_page(),
    )
    assert explain(page_at(month=13, hour=25))[0].startswith("'13' is not a month")


def test_explain_understands_the_calendar():
    assert explain(page_at(month=2, day=29))[0].startswith("There is no day 29")
    assert explain(page_at(year=2024, month=2, day=29))[0] is None


def test_explain_stops_at_the_depth_the_archive_has():
    reason, url = explain(hour_page() + "clip.mp4/")
    assert reason.startswith("Nothing in the archive lives below")
    assert url == hour_page()


def test_explain_works_without_a_common_prefix():
    assert explain(url_path(CAMERA, "2026", "10", "112"), common="") == (
        "'112' is not a day of the month; day folders look like 01 to 31.",
        url_path(CAMERA, "2026", "10"),
    )
