"""Configuration contract: required settings and the key-timezone display.

Credentials are intentionally absent from the settings module -- the botocore
chain owns them -- so the only mandatory values are the bucket and the region
used for SigV4 signing.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import ConfigError, Settings
from app.main import create_app

from conftest import BUCKET, COMMON_PREFIX

BASE_ENV = {"S3_BUCKET": "bucket", "AWS_REGION": "us-east-1"}


# -- required settings --------------------------------------------------------


def test_bucket_is_required():
    with pytest.raises(ConfigError, match="S3_BUCKET"):
        Settings.from_env({"AWS_REGION": "us-east-1"})


def test_region_is_required():
    with pytest.raises(ConfigError, match="region"):
        Settings.from_env({"S3_BUCKET": "bucket"})


def test_empty_region_is_rejected():
    with pytest.raises(ConfigError, match="region"):
        Settings.from_env({"S3_BUCKET": "bucket", "AWS_REGION": "   "})


# -- region sources -----------------------------------------------------------


def test_aws_region_is_used():
    assert Settings.from_env(BASE_ENV).region == "us-east-1"


def test_aws_default_region_is_accepted():
    env = {"S3_BUCKET": "bucket", "AWS_DEFAULT_REGION": "eu-central-1"}
    assert Settings.from_env(env).region == "eu-central-1"


def test_aws_region_wins_over_aws_default_region():
    env = {
        "S3_BUCKET": "bucket",
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "eu-central-1",
    }
    assert Settings.from_env(env).region == "us-east-1"


# -- key timezone -------------------------------------------------------------


def test_valid_key_timezone_is_kept():
    env = dict(BASE_ENV, S3_KEY_TIMEZONE="Europe/Berlin")
    assert Settings.from_env(env).key_timezone == "Europe/Berlin"


def test_unset_key_timezone_stays_unset():
    assert Settings.from_env(dict(BASE_ENV)).key_timezone is None


def test_invalid_key_timezone_is_a_config_error():
    env = dict(BASE_ENV, S3_KEY_TIMEZONE="Mars/Olympus_Mons")
    with pytest.raises(ConfigError, match="S3_KEY_TIMEZONE"):
        Settings.from_env(env)


@pytest.fixture()
def tz_client(s3_resource) -> TestClient:
    settings = Settings(
        bucket=BUCKET,
        region="us-east-1",
        common_prefix=COMMON_PREFIX,
        key_timezone="Europe/Berlin",
    )
    return TestClient(create_app(settings))


def test_footer_shows_the_configured_key_timezone(tz_client):
    body = tz_client.get("/").text
    assert "Clip times are in Europe/Berlin." in body


def test_footer_omits_the_line_when_the_timezone_is_unset(client):
    assert "Clip times are in" not in client.get("/").text
