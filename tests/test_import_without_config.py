"""Importing the application module must not require deployment settings.

Building the app validates configuration and is expected to fail without
credentials, but that has to happen when an ASGI server resolves ``app``, not
when the module is imported. Otherwise a fresh clone with no ``.env`` cannot even
collect the test suite, because ``tests/conftest.py`` imports ``app.main``.

Each check runs a separate interpreter against a copy of the ``app`` package: the
import already happened inside the pytest process, and ``create_app`` calls
``load_dotenv()``, which searches upwards from ``app/main.py`` and would pick up
a developer's local ``.env`` if the copy did not live in an empty directory.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_PACKAGE = PROJECT_ROOT / "app"

# Stand-ins for the values a real deployment supplies.
REQUIRED = {
    "ARCHIVE_S3_BUCKET": "bucket",
    "ARCHIVE_S3_ACCESS_KEY_ID": "id",
    "ARCHIVE_S3_SECRET_ACCESS_KEY": "secret",
}


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """A directory holding only a copy of ``app``, with no ``.env`` alongside."""

    shutil.copytree(
        APP_PACKAGE,
        tmp_path / "app",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    return tmp_path


def run(sandbox: Path, code: str, **config: str) -> subprocess.CompletedProcess:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("ARCHIVE_", "AWS_"))
    }
    env.update(
        {
            "PYTHONPATH": str(sandbox),
            "PYTHONDONTWRITEBYTECODE": "1",
            **config,
        }
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=sandbox,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_import_needs_no_configuration(sandbox):
    result = run(sandbox, "import app.main; print('imported')")

    assert result.returncode == 0, result.stderr
    assert "ConfigError" not in result.stderr


def test_missing_configuration_fails_when_app_is_resolved(sandbox):
    result = run(sandbox, "app = __import__('app.main', fromlist=['app']).app")

    assert result.returncode != 0
    assert "ConfigError" in result.stderr


def test_app_builds_once_configuration_exists(sandbox):
    result = run(
        sandbox,
        "import app.main; print(type(app.main.app).__name__)",
        **REQUIRED,
    )

    assert result.returncode == 0, result.stderr
    assert "FastAPI" in result.stdout


def test_app_attribute_is_a_single_cached_instance(sandbox):
    result = run(
        sandbox,
        "import app.main\n"
        "assert app.main.app is app.main.app\n"
        "print('cached')",
        **REQUIRED,
    )

    assert result.returncode == 0, result.stderr
    assert "cached" in result.stdout


def test_other_attributes_are_still_absent(sandbox):
    result = run(
        sandbox,
        "import app.main\n"
        "try:\n"
        "    app.main.definitely_not_here\n"
        "except AttributeError:\n"
        "    print('AttributeError')\n",
    )

    assert result.returncode == 0, result.stderr
    assert "AttributeError" in result.stdout


def test_every_public_name_resolves(sandbox):
    """Every entry in ``__all__`` has to be importable, including ``app``."""

    result = run(
        sandbox,
        "import app.main\n"
        "from app.main import create_app\n"
        "missing = [n for n in app.main.__all__ if not hasattr(app.main, n)]\n"
        "print('missing:' + ','.join(missing))",
        **REQUIRED,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "missing:"
