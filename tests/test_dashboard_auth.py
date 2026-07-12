"""Phase 5: vault signing helpers + dashboard auth (pure unit tests).

Live-client and Flask-app integration tests live in
test_live_auth_integration.py (integration-offline).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.dashboard.auth import resolve_dashboard_auth, validate_dashboard_token
from src.exchanges.hyperliquid_live import normalize_private_key, resolve_private_key


@pytest.mark.unit
def test_normalize_private_key() -> None:
    key = "a" * 64
    assert normalize_private_key(key) == "0x" + key
    assert normalize_private_key("0x" + key) == "0x" + key


@pytest.mark.unit
def test_resolve_private_key_from_env() -> None:
    key = "b" * 64
    with patch.dict(os.environ, {"HYPERLIQUID_PRIVATE_KEY": key}, clear=False):
        resolved = resolve_private_key()
    assert resolved == "0x" + key


@pytest.mark.unit
def test_dashboard_auth_disabled_by_default() -> None:
    cfg = resolve_dashboard_auth({})
    assert cfg.enabled is False
    assert cfg.token is None


@pytest.mark.unit
def test_dashboard_auth_from_password() -> None:
    cfg = resolve_dashboard_auth({"password": "secret-token"})
    assert cfg.enabled is True
    assert cfg.token == "secret-token"


@pytest.mark.unit
def test_validate_dashboard_token() -> None:
    assert validate_dashboard_token("abc", "abc") is True
    assert validate_dashboard_token("wrong", "abc") is False
    assert validate_dashboard_token(None, "abc") is False
    assert validate_dashboard_token("x", None) is True


if __name__ == "__main__":
    test_normalize_private_key()
    test_resolve_private_key_from_env()
    test_dashboard_auth_disabled_by_default()
    test_dashboard_auth_from_password()
    test_validate_dashboard_token()
    print("All dashboard auth tests passed.")
