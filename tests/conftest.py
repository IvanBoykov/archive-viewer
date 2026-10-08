"""Shared fixtures: an in-memory S3 (moto) seeded with the archive layout."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from app.config import Settings
from app.main import create_app

BUCKET = "test-archive"
COMMON_PREFIX = "home"
# moto's botocore stubber only intercepts AWS-style endpoints, so the fake S3
# is addressed with a real-looking URL rather than a private host.
ENDPOINT = "https://s3.us-east-1.amazonaws.com"

#: (camera, year, month, day, hour, filename) tuples describing the fixture
#: archive. Gaps are deliberate: empty hours, an empty day, a month boundary
#: (31 Oct -> 01 Nov) and a year boundary in the ``gapcam`` camera.
FIXTURE_HOURS = [
    ("front", 2026, 10, 4, 19, ["14-14.mp4", "14-05-truncated.mp4", "59-01.mp4"]),
    ("front", 2026, 10, 4, 22, ["00-00.mp4"]),
    # 2026-10-05 has no clips at all (empty day).
    ("front", 2026, 10, 6, 3, ["07-09.mp4"]),
    # Month boundary: nothing between 06/03 and 31/12.
    ("front", 2026, 10, 31, 23, ["58-12.mp4"]),
    ("front", 2026, 11, 1, 0, ["01-02.mp4"]),
    # Year boundary only for gapcam.
    ("gapcam", 2025, 12, 31, 23, ["30-00.mp4"]),
    ("gapcam", 2026, 1, 1, 0, ["15-45-truncated.mp4"]),
]


SLASH = chr(47)


def hour_prefix(camera: str, year: int, month: int, day: int, hour: int) -> str:
    """S3 prefix of one hour folder, assembled from numeric parts."""

    return (f"{COMMON_PREFIX}{SLASH}{camera}{SLASH}{year:04d}{SLASH}{month:02d}"
            f"{SLASH}{day:02d}{SLASH}{hour:02d}{SLASH}")


def hour_url(camera: str, year: int, month: int, day: int, hour: int) -> str:
    """URL of one hour page."""

    return SLASH + hour_prefix(camera, year, month, day, hour)


# The clip folder most tests look at.
HOUR_PREFIX = hour_prefix("front", 2026, 10, 4, 19)
HOUR_PAGE = SLASH + HOUR_PREFIX


@pytest.fixture()
def s3_resource():
    with mock_aws():
        import boto3

        client = boto3.client("s3", region_name="us-east-1", endpoint_url=ENDPOINT,
                              aws_access_key_id="test", aws_secret_access_key="test")
        client.create_bucket(Bucket=BUCKET)
        for camera, year, month, day, hour, names in FIXTURE_HOURS:
            for name in names:
                key = hour_prefix(camera, year, month, day, hour) + name
                # Deterministic content so range assertions are exact.
                body = (key.encode("utf-8") * 40)[:2048]
                client.put_object(Bucket=BUCKET, Key=key, Body=body, ContentType="video/mp4")
        yield client


@pytest.fixture()
def settings(s3_resource) -> Settings:
    return Settings(
        bucket=BUCKET,
        common_prefix=COMMON_PREFIX,
        endpoint_url=ENDPOINT,
        region="us-east-1",
        access_key_id="test",
        secret_access_key="test",
    )


@pytest.fixture()
def app(s3_resource, settings):
    return create_app(settings)


@pytest.fixture()
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture()
def mapped_client(app) -> TestClient:
    """Client whose Host header is present in the host map (presigned mode)."""
    app.state.settings.host_map = {"direct.example.com": _mapping()}
    return TestClient(app, headers={"host": "direct.example.com"})


def _mapping():
    from app.config import HostMapping

    return HostMapping(endpoint_url="https://s3-public.example.com", addressing_style="virtual")
