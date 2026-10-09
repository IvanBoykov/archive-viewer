"""Tests for folders that do not fit the archive layout.

Real buckets accumulate directories the layout never describes - a ``latest``
pointer written by another tool, a stray four-digit camera name, a 31st day in a
30-day month. Such a folder must not crash a page and must not be hidden, so it
renders as plain text instead of a link.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from app.config import Settings
from app.main import create_app

BUCKET = "malformed-probe"
COMMON = "home"
ENDPOINT = "https://s3.us-east-1.amazonaws.com"

STRAY_KEYS = [
    "home/front/2026/10/04/19/14-14.mp4",
    "home/front/latest/clip.mp4",          # non-numeric folder under a camera
    "home/front/2026/notes.txt",           # file sitting in a year folder
    "home/front/2026/13/01/clip.mp4",      # month 13 does not exist
    "home/front/2026/02/31/x/clip.mp4",    # 31 February does not exist
    "home/2026/2026/10/04/19/clip.mp4",    # camera named like a year
]


@pytest.fixture()
def malformed_client():
    with mock_aws():
        import boto3

        client = boto3.client("s3", region_name="us-east-1", endpoint_url=ENDPOINT,
                              aws_access_key_id="test", aws_secret_access_key="test")
        client.create_bucket(Bucket=BUCKET)
        for key in STRAY_KEYS:
            client.put_object(Bucket=BUCKET, Key=key, Body=b"x" * 32,
                              ContentType="video/mp4")
        settings = Settings(bucket=BUCKET, region="us-east-1", common_prefix=COMMON)
        yield TestClient(create_app(settings), raise_server_exceptions=False)


def linked(body):
    return [u for u in re.findall(r'href="(/[^"]*)"', body) if not u.startswith("/static")]


def unlinked(body):
    return re.findall(r'tree__plain">([^<]*)<', body)


def test_stray_folder_under_camera_does_not_crash(malformed_client):
    response = malformed_client.get("/home/front/")
    assert response.status_code == 200
    body = response.text
    assert "/home/front/2026/" in body      # the real year is still offered
    assert "latest" in unlinked(body)       # the stray one is shown, not linked
    assert "/home/front/latest/" not in linked(body)


def test_ambiguous_four_digit_camera_is_not_linked(malformed_client):
    body = malformed_client.get("/").text
    assert "2026" in unlinked(body)
    assert "/home/2026/" not in linked(body)


def test_impossible_month_is_listed_but_not_linked(malformed_client):
    body = malformed_client.get("/home/front/2026/").text
    assert body.count(">13<") + len([u for u in unlinked(body) if u == "13"]) >= 1
    assert "/home/front/2026/13/" not in linked(body)


def test_impossible_day_is_listed_but_not_linked(malformed_client):
    body = malformed_client.get("/home/front/2026/02/").text
    assert "31" in unlinked(body)
    assert "/home/front/2026/02/31/" not in linked(body)


def test_non_numeric_hour_is_listed_but_not_linked(malformed_client):
    # The hour folder sits inside an impossible day, reachable only by URL.
    response = malformed_client.get("/home/front/2026/02/31/")
    assert response.status_code == 200
    assert "x" in unlinked(response.text)


def test_malformed_urls_stay_404(malformed_client):
    for path in ["/home/front/20xx/", "/home/front/2026/13/", "/home/front/2026/02/31/x/"]:
        assert malformed_client.get(path).status_code == 404, path


def test_stray_files_are_still_visible(malformed_client):
    body = malformed_client.get("/home/front/2026/").text
    assert "notes.txt" in body
