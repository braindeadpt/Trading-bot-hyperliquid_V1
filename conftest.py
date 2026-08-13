"""Test-environment hermeticity.

The bot's ``load_config`` intentionally loads the operator's ``.env`` into
``os.environ`` (only filling vars that are not already set). Locally that is
correct for the bot, but it leaks deployment switches into the test process
depending on suite order: e.g. ``DASHBOARD_AUTH_ENABLED=true`` + a
``BOT_DASHBOARD_TOKEN`` in the operator's ``.env`` would silently activate
auth for every dashboard test that assumes it is off.

Scrub those keys before every test and restore them afterwards. Tests that
need them set them explicitly (``patch.dict`` / ``create_app`` config), which
runs after this fixture and restores before it tears down.
"""

from __future__ import annotations

import os
from typing import Dict, Optional

import pytest

# Deployment switches that must never leak from the operator's .env into tests.
_DEPLOYMENT_ENV_KEYS = (
    "DASHBOARD_AUTH_ENABLED",
    "DASHBOARD_RATE_LIMIT_PER_MIN",
    "BOT_DASHBOARD_TOKEN",
    "DASHBOARD_TOKEN",
)


@pytest.fixture(autouse=True)
def _scrub_deployment_env() -> None:
    saved: Dict[str, Optional[str]] = {
        key: os.environ.pop(key, None) for key in _DEPLOYMENT_ENV_KEYS
    }
    yield
    for key, value in saved.items():
        if value is not None:
            os.environ[key] = value
