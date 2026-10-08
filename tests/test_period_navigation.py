"""Previous/next navigation on the month and day listing pages.

Two properties matter and are asserted for every case:

* the link skips periods that hold no clips, crossing day, month and year
  boundaries;
* the link targets a page of the same level, so a month page never lands the
  user on a day page and a day page never lands them on an hour page.
"""

from __future__ import annotations

import re

from app.archive import HourRef, PERIOD_DAY, PERIOD_MONTH, next_period, period_navigation

MONTH_URL_RE = re.compile(r"^/[a-z]+/[a-z]+/\d{4}/\d{2}/$")
DAY_URL_RE = re.compile(r"^/[a-z]+/[a-z]+/\d{4}/\d{2}/\d{2}/$")

NAV_BLOCK_RE = re.compile(r'<nav class="period-nav period-nav--(top|bottom)"[^>]*aria-label="([^"]+)"')
LINK_RE = re.compile(r'<a class="period-nav__link" href="([^"]+)" rel="(prev|next)">')


def nav_links(body: str):
    """``(rel, href)`` pairs of every navigation link on the page."""

    return [(m.group(2), m.group(1)) for m in LINK_RE.finditer(body)]


def links_for(body: str, rel: str):
    return [href for name, href in nav_links(body) if name == rel]


def month_path(camera, year, month):
    return f"/home/{camera}/{year:04d}/{month:02d}/"


def day_path(camera, year, month, day):
    return f"/home/{camera}/{year:04d}/{month:02d}/{day:02d}/"


# -- structure ---------------------------------------------------------------
def test_month_page_has_navigation_at_top_and_bottom(client):
    body = client.get(month_path("front", 2026, 10)).text
    blocks = NAV_BLOCK_RE.findall(body)
    assert [pos for pos, _ in blocks] == ["top", "bottom"]
    assert len({label for _, label in blocks}) == 2, "landmarks need distinct labels"


def test_navigation_brackets_the_listing(client):
    body = client.get(day_path("front", 2026, 10, 31)).text
    top = body.index('class="period-nav period-nav--top"')
    tree = body.index('class="tree"')
    bottom = body.index('class="period-nav period-nav--bottom"')
    assert top < tree < bottom


def test_day_and_month_pages_use_their_own_wording(client):
    assert "Previous day" in client.get(day_path("front", 2026, 10, 31)).text
    assert "Previous month" in client.get(month_path("front", 2026, 11)).text


def test_camera_and_year_pages_have_no_period_navigation(client):
    for url in ["/", "/home/front/", "/home/front/2026/"]:
        assert "period-nav" not in client.get(url).text, url


# -- month level -------------------------------------------------------------
def test_month_page_links_to_the_next_month_holding_clips(client):
    body = client.get(month_path("front", 2026, 10)).text
    assert links_for(body, "next") == [month_path("front", 2026, 11)] * 2


def test_month_page_has_no_previous_link_at_the_start_of_the_archive(client):
    body = client.get(month_path("front", 2026, 10)).text
    assert links_for(body, "prev") == []
    assert "Start of the archive" in body


def test_month_page_links_to_the_previous_month(client):
    body = client.get(month_path("front", 2026, 11)).text
    assert links_for(body, "prev") == [month_path("front", 2026, 10)] * 2
    assert "End of the archive" in body


def test_month_link_crosses_the_year_boundary_and_stays_a_month_page(client):
    body = client.get(month_path("gapcam", 2025, 12)).text
    targets = links_for(body, "next")
    assert targets == [month_path("gapcam", 2026, 1)] * 2
    assert all(MONTH_URL_RE.match(href) for href in targets), targets


def test_month_links_never_target_a_day_page(client):
    for url in [month_path("front", 2026, 10), month_path("front", 2026, 11),
                month_path("gapcam", 2025, 12), month_path("gapcam", 2026, 1)]:
        body = client.get(url).text
        targets = links_for(body, "prev") + links_for(body, "next")
        assert all(MONTH_URL_RE.match(href) for href in targets), (url, targets)


# -- day level ---------------------------------------------------------------
def test_day_page_skips_an_empty_day(client):
    body = client.get(day_path("front", 2026, 10, 4)).text
    # 2026-10-05 holds nothing, so the next day with clips is the 6th.
    assert links_for(body, "next") == [day_path("front", 2026, 10, 6)] * 2


def test_day_page_skips_a_run_of_empty_days(client):
    body = client.get(day_path("front", 2026, 10, 6)).text
    assert links_for(body, "next") == [day_path("front", 2026, 10, 31)] * 2
    assert links_for(body, "prev") == [day_path("front", 2026, 10, 4)] * 2


def test_day_link_crosses_the_month_boundary_and_stays_a_day_page(client):
    body = client.get(day_path("front", 2026, 10, 31)).text
    assert links_for(body, "next") == [day_path("front", 2026, 11, 1)] * 2


def test_day_link_crosses_the_year_boundary(client):
    body = client.get(day_path("gapcam", 2025, 12, 31)).text
    assert links_for(body, "next") == [day_path("gapcam", 2026, 1, 1)] * 2


def test_day_links_never_target_an_hour_page(client):
    for url in [day_path("front", 2026, 10, 4), day_path("front", 2026, 10, 6),
                day_path("front", 2026, 10, 31), day_path("front", 2026, 11, 1),
                day_path("gapcam", 2025, 12, 31)]:
        body = client.get(url).text
        targets = links_for(body, "prev") + links_for(body, "next")
        assert all(DAY_URL_RE.match(href) for href in targets), (url, targets)


def test_every_navigation_target_is_a_live_page(client):
    """A link that 404s would be worse than no link at all."""

    for url in [month_path("front", 2026, 10), day_path("front", 2026, 10, 31),
                day_path("gapcam", 2025, 12, 31), month_path("gapcam", 2025, 12)]:
        body = client.get(url).text
        for href in links_for(body, "prev") + links_for(body, "next"):
            assert client.get(href).status_code == 200, (url, href)


# -- domain level ------------------------------------------------------------
TREE = {
    "home/front/": ["2026"],
    "home/front/2026/": ["10", "11"],
    "home/front/2026/10/": ["04", "06", "31"],
    "home/front/2026/10/04/": ["19", "22"],
    "home/front/2026/10/06/": ["03"],
    "home/front/2026/10/31/": ["23"],
    "home/front/2026/11/": ["01"],
    "home/front/2026/11/01/": ["00"],
}


def children_map():
    def list_children(prefix: str):
        return list(TREE.get(prefix, []))

    return list_children


def test_month_anchors_exclude_the_current_period():
    """The search must never return the period the page is already showing."""

    prev_month, next_month = period_navigation(
        children_map(), "home", PERIOD_MONTH,
        HourRef(camera="front", year=2026, month=10, day=1, hour=0))
    assert prev_month is None
    assert (next_month.year, next_month.month) == (2026, 11)


def test_day_anchors_exclude_hours_of_the_current_day():
    prev_day, next_day = period_navigation(
        children_map(), "home", PERIOD_DAY,
        HourRef(camera="front", year=2026, month=10, day=4, hour=0))
    assert prev_day is None
    assert (next_day.day, next_day.hour) == (6, 3)


def test_february_navigates_like_any_other_month():
    """Short months must behave: the forward anchor is the real last day.

    The anchor decides which days count as "still the current month", so a
    February page has to skip every February day and reach March.
    """

    tree = dict(TREE, **{
        "home/front/": ["2026"],
        "home/front/2026/": ["02", "03"],
        "home/front/2026/02/": ["10"],
        "home/front/2026/03/": ["05"],
        "home/front/2026/03/05/": ["07"],
    })

    def list_children(prefix):
        return list(tree.get(prefix, []))

    ref = next_period(list_children, "home", PERIOD_MONTH,
                      HourRef(camera="front", year=2026, month=2, day=1, hour=0))
    assert (ref.year, ref.month, ref.day) == (2026, 3, 5)


def test_period_navigation_stays_within_a_bounded_number_of_list_calls():
    """Navigation must not degrade into walking the calendar folder by folder."""

    calls = []

    def counting(prefix):
        calls.append(prefix)
        return TREE.get(prefix, [])

    period_navigation(counting, "home", PERIOD_DAY,
                      HourRef(camera="front", year=2026, month=10, day=31, hour=0))
    assert len(calls) < 20, calls
