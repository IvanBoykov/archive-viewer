"""Tests for archives whose cameras sit directly at the bucket root.

This is the ``ARCHIVE_COMMON_PREFIX=""`` configuration: keys look like
``front/2026/10/04/19/14-14.mp4`` with no leading folder. An S3 key that starts
with a slash addresses a different object, so an empty prefix must produce
``front/2026/...`` and never ``/front/2026/...``.
"""

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from app.archive import (
    HourRef,
    InvalidPath,
    build_path,
    join_prefix,
    parse_path,
    url_path,
)
from app.config import Settings
from app.main import create_app
from app.urls import proxy_key_is_allowed

from conftest import BUCKET, ENDPOINT, FIXTURE_HOURS

S = chr(47)
ROOT = ""


def root_key(camera, year, month, day, hour, name):
    """Object key of one clip when no common prefix is configured."""

    return join_prefix(camera, f"{year:04d}", f"{month:02d}", f"{day:02d}",
                       f"{hour:02d}") + name


def root_url(camera, year, month, day, hour):
    return url_path(camera, f"{year:04d}", f"{month:02d}", f"{day:02d}", f"{hour:02d}")


@pytest.fixture()
def root_store():
    """A bucket seeded with the same archive as the main fixture, minus prefix."""

    with mock_aws():
        import boto3

        client = boto3.client("s3", region_name="us-east-1", endpoint_url=ENDPOINT,
                              aws_access_key_id="test", aws_secret_access_key="test")
        client.create_bucket(Bucket=BUCKET)
        for camera, year, month, day, hour, names in FIXTURE_HOURS:
            for name in names:
                key = root_key(camera, year, month, day, hour, name)
                client.put_object(Bucket=BUCKET, Key=key,
                                  Body=(key.encode("utf-8") * 40)[:2048],
                                  ContentType="video/mp4")
        yield client


@pytest.fixture()
def root_settings(root_store) -> Settings:
    return Settings(
        bucket=BUCKET,
        common_prefix=ROOT,
        endpoint_url=ENDPOINT,
        region="us-east-1",
        access_key_id="test",
        secret_access_key="test",
    )


@pytest.fixture()
def root_client(root_settings) -> TestClient:
    return TestClient(create_app(root_settings))


def nav_data(body):
    match = re.search(r'id="hour-nav-data">(.*?)</script>', body, re.S)
    assert match, "hour-nav-data block is missing"
    return json.loads(match.group(1))


# -- key and URL construction -------------------------------------------------

def test_prefix_builders_emit_no_leading_slash():
    ref = HourRef("front", 2026, 12, 31, 23)
    assert ref.prefix(ROOT) == "front/2026/12/31/23/"
    assert ref.day_prefix(ROOT) == "front/2026/12/31/"
    assert ref.month_prefix(ROOT) == "front/2026/12/"
    assert ref.year_prefix(ROOT) == "front/2026/"
    assert ref.camera_prefix(ROOT) == "front/"


def test_url_builders_have_no_double_slash():
    ref = HourRef("front", 2026, 12, 31, 23)
    assert build_path(ROOT, hour_ref=ref) == "/front/2026/12/31/23/"
    assert build_path(ROOT, camera="front") == "/front/"
    assert build_path(ROOT) == "/"
    assert join_prefix(ROOT) == ""


def test_prefixed_builders_are_unchanged():
    ref = HourRef("front", 2026, 12, 31, 23)
    assert ref.prefix("home") == "home/front/2026/12/31/23/"
    assert build_path("home", hour_ref=ref) == "/home/front/2026/12/31/23/"


def test_parse_path_without_prefix_uses_camera_as_first_segment():
    assert parse_path(ROOT, "/") == ("cameras", None)
    assert parse_path(ROOT, "/front/") == ("years", None)
    assert parse_path(ROOT, "/front/2026/") == ("months", None)
    assert parse_path(ROOT, "/front/2026/10/") == ("days", None)
    assert parse_path(ROOT, "/front/2026/10/04/") == ("hours", None)

    level, ref = parse_path(ROOT, "/front/2026/10/04/19/")
    assert level == "clips"
    assert ref == HourRef("front", 2026, 10, 4, 19)

    with pytest.raises(InvalidPath):
        parse_path(ROOT, "/front/20xx/")
    with pytest.raises(InvalidPath):
        parse_path(ROOT, "/front/2026/13/")


# -- routes -------------------------------------------------------------------

def test_root_lists_cameras_at_bucket_root(root_client):
    body = root_client.get("/").text
    assert 'href="/front/"' in body
    assert 'href="/gapcam/"' in body
    # No empty prefix segment leaking into the generated links.
    for href in re.findall(r'href="(/[^"]*)"', body):
        assert "//" not in href, href


def test_every_level_renders_without_a_prefix(root_client):
    for path, needle in [
        ("/front/", "2026"),
        ("/front/2026/", "October"),
        ("/front/2026/10/", "October"),
        ("/front/2026/10/04/", "19:00"),
    ]:
        response = root_client.get(path)
        assert response.status_code == 200, path
        assert needle in response.text


def test_generated_links_have_no_empty_segments(root_client):
    body = root_client.get(root_url("front", 2026, 10, 4, 19)).text
    for href in re.findall(r'href="(/[^"]*)"', body):
        assert "//" not in href, href


def test_hour_page_lists_clips_without_a_prefix(root_client):
    response = root_client.get(root_url("front", 2026, 10, 4, 19))
    assert response.status_code == 200
    clips = json.loads(re.search(r'id="hour-clips-data">(.*?)</script>',
                                 response.text, re.S).group(1))
    assert [c["time"] for c in clips] == ["14:05", "14:14", "59:01"]
    assert clips[0]["truncated"] is True
    assert clips[0]["filename"] == "14-05-truncated.mp4"
    for clip in clips:
        assert clip["video_url"].startswith("/media/")
        assert "//" not in clip["video_url"][1:]


def test_cross_boundary_navigation_without_a_prefix(root_client):
    # 2026-10-05 is empty, so 22:00 on the 4th must skip it and reach the 6th.
    body = root_client.get(root_url("front", 2026, 10, 4, 22)).text
    nav = nav_data(body)
    assert nav["previous"] == "/front/2026/10/04/19/"
    assert nav["next"] == "/front/2026/10/06/03/"


def test_year_boundary_crossing_without_a_prefix(root_client):
    body = root_client.get(root_url("gapcam", 2025, 12, 31, 23)).text
    assert json.loads(re.search(r'id="hour-nav-data">(.*?)</script>', body,
                                re.S).group(1))["next"] == "/gapcam/2026/01/01/00/"


def test_unknown_camera_is_404_without_a_prefix(root_client):
    assert root_client.get("/nosuchcam/").status_code == 404
    assert root_client.get("/front/20xx/").status_code == 404


# -- proxy --------------------------------------------------------------------

def test_proxy_serves_root_level_keys(root_client, root_settings):
    url = "/media" + S + root_key("front", 2026, 10, 4, 19, "14-14.mp4")
    full = root_client.get(url)
    assert full.status_code == 200
    assert full.headers["accept-ranges"] == "bytes"

    partial = root_client.get(url, headers={"range": "bytes=0-99"})
    assert partial.status_code == 206
    assert len(partial.content) == 100


def test_proxy_allowlist_with_empty_prefix(root_settings):
    assert proxy_key_is_allowed("front/2026/10/04/19/14-14.mp4", root_settings)
    assert not proxy_key_is_allowed("front/2026/10/04/19/14-14.txt", root_settings)
    assert not proxy_key_is_allowed("../secret.mp4", root_settings)
    assert not proxy_key_is_allowed("", root_settings)


def test_proxy_allowlist_still_scopes_a_configured_prefix(root_store):
    scoped = Settings(bucket=BUCKET, common_prefix="home", endpoint_url=ENDPOINT,
                      region="us-east-1", access_key_id="test", secret_access_key="test")
    assert proxy_key_is_allowed("home/front/2026/10/04/19/14-14.mp4", scoped)
    assert not proxy_key_is_allowed("other/front/2026/10/04/19/14-14.mp4", scoped)


# -- configuration ------------------------------------------------------------

BASE_ENV = {
    "ARCHIVE_S3_BUCKET": "b",
    "ARCHIVE_S3_ACCESS_KEY_ID": "a",
    "ARCHIVE_S3_SECRET_ACCESS_KEY": "s",
}


@pytest.mark.parametrize(
    "value, expected",
    [("", ""), ("   ", ""), ("/", ""), ("/home/", "home"), ("home", "home")],
)
def test_empty_prefix_is_accepted_from_env(value, expected):
    env = dict(BASE_ENV, ARCHIVE_COMMON_PREFIX=value)
    assert Settings.from_env(env).common_prefix == expected


def test_unset_prefix_falls_back_to_the_default():
    assert Settings.from_env(dict(BASE_ENV)).common_prefix == "archive"
