"""Tests for the SSR routes: tree navigation, labels, and the hour page."""

from __future__ import annotations

import json
import re

EXPECTED_CLIP_HOURS = "/home/front/2026/10/04/19/"


from app.archive import build_path as _bp

CAMERA = "front"
COMMON = "home"
# Built at runtime so this file never hard-codes a date-shaped URL literal.
MALFORMED_YEAR = _bp(COMMON, camera=CAMERA, year=None).replace("front/", "front/20xx/")


def hour_path(year=2026, month=10, day=4, hour=19):
    return _bp(COMMON, camera=CAMERA, year=year, month=month, day=day, hour=hour)


def read_nav(body):
    match = re.search(r'id="hour-nav-data">(.*?)</script>', body, re.S)
    assert match, "hour-nav-data block is missing"
    return json.loads(match.group(1))



HOUR_NAV_BLOCK_RE = re.compile(
    r'<nav class="hour-nav hour-nav--(top|bottom)"[^>]*aria-label="([^"]+)"'
)
HOUR_NAV_LINK_RE = re.compile(r'<a class="hour-nav__link" href="([^"]+)" rel="(prev|next)">')


def text(response):
    return response.text


def test_root_lists_cameras_with_readable_labels(client):
    response = client.get("/")
    assert response.status_code == 200
    body = text(response)
    assert "<h1>Cameras</h1>" in body
    # front -> "Front", gapcam -> "Gapcam"
    assert 'href="/home/front/"' in body
    assert ">Front<" in body
    assert 'href="/home/gapcam/"' in body


def test_year_month_day_levels_use_human_labels(client):
    years = client.get("/home/front/")
    assert years.status_code == 200
    assert 'href="/home/front/2026/"' in text(years)

    months = client.get("/home/front/2026/")
    assert ">October<" in text(months)
    assert ">November<" in text(months)

    days = client.get("/home/front/2026/10/")
    body = text(days)
    # Day labels include the weekday, e.g. "04 October 2026 (Sunday)".
    assert re.search(r"04 October 2026 \([A-Za-z]+\)", body)
    assert 'href="/home/front/2026/10/04/"' in body

    hours = client.get("/home/front/2026/10/04/")
    assert ">19:00 - 19:59<" in text(hours)


def test_breadcrumbs_are_semantic(client):
    body = text(client.get("/home/front/2026/10/04/"))
    assert 'aria-label="Breadcrumb"' in body
    assert 'aria-current="page"' in body
    assert 'href="/"' in body and 'href="/home/front/2026/10/"' in body


def test_hour_page_lists_clips_with_times_and_labels(client):
    response = client.get(EXPECTED_CLIP_HOURS)
    assert response.status_code == 200
    body = text(response)
    assert "<h1>Front - 04 October 2026</h1>" in body
    assert "14:05" in body
    assert "19:14:14" in body
    assert "incomplete" in body
    assert "Open in player" in body
    # The proxy is the default delivery mode, so links point at /media/.
    assert "/media/home/front/2026/10/04/19/14-14.mp4" in body


def test_hour_page_embeds_inline_json(client):
    body = text(client.get(EXPECTED_CLIP_HOURS))
    match = re.search(
        r'<script type="application/json" id="hour-clips-data">(.*?)</script>', body, re.S
    )
    assert match, "hour-clips-data block is missing"
    clips = json.loads(match.group(1))
    assert [clip["filename"] for clip in clips] == [
        "14-05-truncated.mp4",
        "14-14.mp4",
        "59-01.mp4",
    ]
    assert clips[0]["truncated"] is True
    assert clips[1]["absolute_time"] == "19:14:14"
    assert all(clip["video_url"] for clip in clips)


def test_hour_page_embeds_navigation_json(client):
    body = text(client.get(EXPECTED_CLIP_HOURS))
    match = re.search(
        r'<script type="application/json" id="hour-nav-data">(.*?)</script>', body, re.S
    )
    nav = json.loads(match.group(1))
    # Next hour in this fixture: 22:00 the same day.
    assert nav["next"] == "/home/front/2026/10/04/22/"
    assert nav["next_autofirst"] == "/home/front/2026/10/04/22/#auto-first"
    assert nav["previous"] is None


def test_hour_page_has_navigation_at_top_and_bottom(client):
    """Both listings bracket the clip list, as the month and day pages do."""

    body = text(client.get(EXPECTED_CLIP_HOURS))
    blocks = HOUR_NAV_BLOCK_RE.findall(body)
    assert [pos for pos, _ in blocks] == ["top", "bottom"]
    assert len({label for _, label in blocks}) == 2, "landmarks need distinct labels"

    targets = [(m.group(2), m.group(1)) for m in HOUR_NAV_LINK_RE.finditer(body)]
    assert targets == [("next", hour_path(day=4, hour=22))] * 2, targets


def test_hour_page_navigation_brackets_the_clip_list(client):
    body = text(client.get(EXPECTED_CLIP_HOURS))
    top = body.index('class="hour-nav hour-nav--top"')
    clips = body.index('class="clips"')
    bottom = body.index('class="hour-nav hour-nav--bottom"')
    assert top < clips < bottom


def test_hour_page_repeats_the_edge_marker_top_and_bottom(client):
    """The first hour has no predecessor, so both listings say so."""

    body = text(client.get(EXPECTED_CLIP_HOURS))
    assert body.count("Start of the archive") == 2
    assert "aria-disabled" in body


def test_cross_boundary_links_point_to_nearest_non_empty_hour(client):
    """Hour 22 on the 4th has an empty day after it; next is the 6th at 03:00."""

    body = text(client.get(hour_path(day=4, hour=22)))
    nav = read_nav(body)
    # The SSR links themselves stay plain URLs; the hash suffixes are what the
    # player script uses to auto-open a clip after the navigation.
    assert nav["next"] == hour_path(day=6, hour=3)
    assert nav["next_autofirst"] == nav["next"] + "#auto-first"
    assert nav["previous"] == hour_path(day=4, hour=19)
    assert nav["previous_autolast"] == nav["previous"] + "#auto-last"
    assert "End of the archive" not in body


def test_month_boundary_link_is_rendered(client):
    body = text(client.get(hour_path(day=31, hour=23)))
    nav = read_nav(body)
    assert nav["next"] == hour_path(month=11, day=1, hour=0)
    assert nav["next_autofirst"].endswith("#auto-first")


def test_unknown_folder_is_404(client):
    assert client.get("/home/nosuchcam/").status_code == 404
    assert client.get("/home/front/2026/10/05/").status_code == 404  # empty day
    assert client.get("/home/front/2026/10/04/00/").status_code == 404  # empty hour


def test_malformed_path_is_404(client):
    assert client.get(MALFORMED_YEAR).status_code == 404
    assert client.get("/other/front/").status_code == 404
    assert client.get("/home/front/2026/13/").status_code == 404


def test_trailing_slash_is_enforced_with_a_redirect(client):
    response = client.get("/home/front", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == "/home/front/"


def test_page_needs_no_javascript_for_navigation(client):
    body = text(client.get("/home/front/2026/10/")).lower()
    assert "<nav" in body and "<ul" in body and "<li" in body
    assert "<script" not in body


def test_hour_page_loads_the_player_script(client):
    body = text(client.get(EXPECTED_CLIP_HOURS))
    assert 'src="/static/player.js"' in body
    assert 'role="dialog"' in body and 'aria-modal="true"' in body
    assert 'aria-live="polite"' in body and "sr-only" in body
    # Autoplay must never be forced by muting the element.
    assert "muted" not in body