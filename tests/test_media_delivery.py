"""Tests for video delivery: the Range-aware proxy and presigned URLs."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import json
import re

import pytest

from app.urls import MODE_PRESIGNED, MODE_PROXY, build_media_policy

CLIP_URL = "/media/home/front/2026/10/04/19/14-14.mp4"


def expected_body(key: str) -> bytes:
    """Mirror the deterministic object bodies written by conftest."""

    return (key.encode("utf-8") * 40)[:2048]


# Hour page URL for the same folder as KEY.
KEY = CLIP_URL[len("/media/"):]
CLIP_PAGE = CLIP_URL[len("/media"):].rsplit("/", 1)[0] + "/"


def test_proxy_serves_full_object(client):
    response = client.get(CLIP_URL)
    assert response.status_code == 200
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-type"] == "video/mp4"
    assert int(response.headers["content-length"]) == len(expected_body(KEY))
    assert response.content == expected_body(KEY)


def test_proxy_forwards_range_and_returns_206(client):
    response = client.get(CLIP_URL, headers={"range": "bytes=64-127"})
    assert response.status_code == 206
    assert response.headers["content-range"] == f"bytes 64-127/{len(expected_body(KEY))}"
    assert response.content == expected_body(KEY)[64:128]


def test_proxy_supports_open_ended_range_for_seeking(client):
    full = expected_body(KEY)
    response = client.get(CLIP_URL, headers={"range": "bytes=1000-"})
    assert response.status_code == 206
    assert response.content == full[1000:]


def test_proxy_supports_suffix_range(client):
    full = expected_body(KEY)
    response = client.get(CLIP_URL, headers={"range": "bytes=-16"})
    assert response.status_code == 206
    assert response.content == full[-16:]


def test_proxy_rejects_unsatisfiable_range(client):
    response = client.get(CLIP_URL, headers={"range": "bytes=999999-"})
    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{len(expected_body(KEY))}"


def test_proxy_ignores_malformed_range(client):
    """RFC 9110: an invalid range header must be ignored, not rejected."""

    response = client.get(CLIP_URL, headers={"range": "bytes=abc-xyz"})
    assert response.status_code == 200
    assert response.content == expected_body(KEY)


def test_proxy_honours_mismatched_if_range(client):
    response = client.get(CLIP_URL, headers={"range": "bytes=0-15", "if-range": '"stale-etag"'})
    assert response.status_code == 200
    assert len(response.content) == len(expected_body(KEY))


def test_proxy_sends_rfc7231_last_modified(client):
    """S3 returns a datetime; the header must be an HTTP date, not ISO text."""

    from email.utils import parsedate_to_datetime

    response = client.get(CLIP_URL)
    value = response.headers["last-modified"]
    assert value.endswith("GMT")
    # Raises ValueError if the header is not a valid RFC 7231 date.
    assert parsedate_to_datetime(value).year >= 2020


def test_proxy_404_for_missing_object(client):
    assert client.get("/media/home/front/2026/01/01/00/00-00.mp4").status_code == 404


@pytest.mark.parametrize(
    "url",
    [
        "/media/etc/passwd",
        "/media/other-prefix/front/2026/10/04/19/14-14.mp4",
        "/media/home/front/2026/10/04/19/clip.txt",
    ],
)
def test_proxy_refuses_keys_outside_the_archive(client, url):
    assert client.get(url).status_code == 404


@pytest.mark.parametrize(
    "key",
    [
        "",
        "etc/passwd.mp4",
        "home.mp4",
        "home/front/2026/10/04/19/clip.txt",
        "home/../../etc/passwd.mp4",
        "/home/front/2026/10/04/19/../20/14-14.mp4",
    ],
)
def test_proxy_key_allowlist_rejects_unsafe_keys(settings, key):
    from app.urls import proxy_key_is_allowed

    assert proxy_key_is_allowed(key, settings) is False


def test_proxy_key_allowlist_accepts_archive_clips(settings):
    from app.urls import proxy_key_is_allowed

    assert proxy_key_is_allowed("home/front/2026/10/04/19/14-14.mp4", settings) is True


def test_unknown_host_uses_proxy_mode(client):
    body = client.get("/home/front/2026/10/04/19/").text
    assert "/media/home/front/2026/10/04/19/14-14.mp4" in body
    assert "X-Amz-Signature" not in body


def test_mapped_host_uses_presigned_urls(mapped_client):
    body = mapped_client.get("/home/front/2026/10/04/19/").text
    assert "X-Amz-Signature" in body
    assert "X-Amz-Credential" in body
    assert "https://s3-public.example.com" in body
    # Virtual-hosted style puts the bucket in the hostname, never in the path.
    assert "test-archive/home/front" not in body
    assert "/media/" not in body


def test_presigned_url_expires_after_one_hour(mapped_client):
    """The player links come from the inline JSON payload."""

    body = mapped_client.get(CLIP_PAGE).text
    match = re.search(
        r'<scr' + 'ipt type="application/json" id="hour-clips-data">(.*?)</scr' + 'ipt>',
        body,
        re.S,
    )
    assert match, "hour-clips-data block is missing"
    clips = json.loads(match.group(1))
    parsed = urlparse(clips[0]["video_url"])
    query = parse_qs(parsed.query)
    assert "s3-public.example.com" in parsed.netloc
    assert query["X-Amz-Expires"] == ["3600"]
    assert query["X-Amz-Signature"]
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]


def test_policy_selection_is_driven_only_by_the_host_header(settings):
    store = _StubStore()
    settings.host_map = {}
    assert build_media_policy(store, settings, "whatever.example").mode == MODE_PROXY
    from app.config import HostMapping

    settings.host_map = {"archive.example": HostMapping("https://s3.example.com")}
    assert build_media_policy(store, settings, "archive.example").mode == MODE_PRESIGNED
    # Matching ignores case and an optional port.
    assert build_media_policy(store, settings, "Archive.Example:8443").mode == MODE_PRESIGNED
    assert build_media_policy(store, settings, None).mode == MODE_PROXY
    settings.host_map = {}


class _StubStore:
    """Only ``settings`` is touched by :func:`build_media_policy`."""

    def __init__(self):
        self.settings = None
