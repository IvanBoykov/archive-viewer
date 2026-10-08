"""The camera page: year headings with the months of each year underneath.

Structure is asserted from the rendered HTML, because the point of the page is
what a person and a screen reader see: one heading per year, each followed only
by the months recorded in that year.
"""

from __future__ import annotations

import re
from dataclasses import replace

from fastapi.testclient import TestClient

from app.main import create_app

MONTH_URL_RE = re.compile(r"^/[a-z]+/[a-z]+/\d{4}/\d{2}/$")

HEADING_RE = re.compile(r"<h2 class=\"years__heading\">\s*<a class=\"years__link\" href=\"([^\"]+)\">([^<]+)</a>")
GROUP_RE = re.compile(
    r'<section class="years__group">(?P<body>.*?)</section>', re.S)
ENTRY_RE = re.compile(r'<a class="tree__link" href="([^"]+)">([^<]+)</a>')


def groups(body: str):
    """``(year_url, year_label, [(url, label), ...])`` per rendered year group."""

    parsed = []
    for match in GROUP_RE.finditer(body):
        section = match.group("body")
        heading = HEADING_RE.search(section)
        assert heading, "every year group needs a linked heading"
        entries = ENTRY_RE.findall(section[heading.end():])
        parsed.append((heading.group(1), heading.group(2), entries))
    return parsed


# -- structure ---------------------------------------------------------------
def test_camera_page_groups_months_under_year_headings(client):
    body = client.get("/home/front/").text
    assert groups(body) == [
        ("/home/front/2026/", "2026",
         [("/home/front/2026/10/", "October"), ("/home/front/2026/11/", "November")]),
    ]


def test_camera_page_repeats_the_pattern_for_every_year(client):
    """Two years means two headings, each with only its own months."""

    body = client.get("/home/gapcam/").text
    assert groups(body) == [
        ("/home/gapcam/2025/", "2025", [("/home/gapcam/2025/12/", "December")]),
        ("/home/gapcam/2026/", "2026", [("/home/gapcam/2026/01/", "January")]),
    ]


def test_camera_page_keeps_one_page_title(client):
    body = client.get("/home/gapcam/").text
    assert body.count("<h1") == 1
    assert "<h1>Gapcam</h1>" in body


def test_month_links_point_at_month_pages(client):
    body = client.get("/home/gapcam/").text
    for _year_url, _label, entries in groups(body):
        for url, _text in entries:
            assert MONTH_URL_RE.match(url), url


def test_camera_page_headings_come_before_their_months(client):
    """A month listed above the wrong heading would be a misfiled recording."""

    body = client.get("/home/gapcam/").text
    positions = []
    for match in re.finditer(r"<h2[^>]*>.*?</h2>|<a class=\"tree__link\" href=\"([^\"]+)\">", body, re.S):
        positions.append(("heading" if match.group(0).startswith("<h2") else match.group(1)))
    assert positions == [
        "heading", "/home/gapcam/2025/12/",
        "heading", "/home/gapcam/2026/01/",
    ]


# -- what stays unchanged ----------------------------------------------------
def test_camera_page_does_not_list_months_flat(client):
    """The grouped list replaces the flat one; months appear exactly once."""

    body = client.get("/home/gapcam/").text
    assert body.count('href="/home/gapcam/2025/12/"') == 1


def test_year_page_still_lists_only_its_months(client):
    body = client.get("/home/gapcam/2025/").text
    assert "<h1>Gapcam</h1>" in body or "2025" in body
    assert "years__group" not in body
    assert 'href="/home/gapcam/2025/12/"' in body


def test_root_still_lists_cameras_without_grouping(client):
    body = client.get("/").text
    assert "years__group" not in body
    assert 'href="/home/front/"' in body


# -- edge cases --------------------------------------------------------------
def test_empty_bucket_says_so_on_the_root(client, s3_resource, settings):
    """A camera folder that holds nothing 404s, so the only honest empty page
    is the root of a bucket that holds no cameras at all."""

    s3_resource.create_bucket(Bucket="empty-archive")
    blank = replace(settings, bucket="empty-archive")
    body = TestClient(create_app(blank)).get("/").text
    assert "No recordings have been archived here yet." in body


def test_out_of_layout_folder_is_visible_but_unlinked(client, s3_resource, settings):
    """Grouping must not swallow entries that are not years."""

    s3_resource.put_object(Bucket=settings.bucket, Key="home/front/misc/2026/10/04/19/a.mp4",
                         Body=b"x")
    body = client.get("/home/front/").text
    assert 'class="tree__plain"' in body
    assert ">misc<" in body
    assert "href=\"/home/front/misc/\"" not in body


def test_empty_year_still_renders_its_heading(client, s3_resource, settings):
    """A year folder with no months is data, not a page to skip silently."""

    s3_resource.put_object(Bucket=settings.bucket,
                         Key="home/front/2024/.keep", Body=b"")
    body = client.get("/home/front/").text
    assert ">2024<" in body
    assert "No months recorded." in body


# -- cost --------------------------------------------------------------------
def test_grouping_costs_one_list_call_per_year(client, app, s3_resource, settings):
    """Grouping reads one extra level per year, never the whole bucket."""

    store = app.state.store
    calls = []
    original = store.list_level
    store.list_level = lambda prefix: (calls.append(prefix), original(prefix))[1]
    try:
        client.get("/home/gapcam/")
    finally:
        store.list_level = original
    # The camera listing itself plus one listing for each of the two years.
    assert len(calls) == 3, calls
