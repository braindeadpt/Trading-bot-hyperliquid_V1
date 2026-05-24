"""Phase 5: vault signing helpers + dashboard auth."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.dashboard.auth import resolve_dashboard_auth, validate_dashboard_token
from src.exchanges.hyperliquid_live import (
    HyperliquidLiveClient,
    normalize_private_key,
    resolve_private_key,
)


def test_normalize_private_key() -> None:
    key = "a" * 64
    assert normalize_private_key(key) == "0x" + key
    assert normalize_private_key("0x" + key) == "0x" + key


def test_resolve_private_key_from_env() -> None:
    key = "b" * 64
    with patch.dict(os.environ, {"HYPERLIQUID_PRIVATE_KEY": key}, clear=False):
        resolved = resolve_private_key()
    assert resolved == "0x" + key


def test_dashboard_auth_disabled_by_default() -> None:
    cfg = resolve_dashboard_auth({})
    assert cfg.enabled is False
    assert cfg.token is None


def test_dashboard_auth_from_password() -> None:
    cfg = resolve_dashboard_auth({"password": "secret-token"})
    assert cfg.enabled is True
    assert cfg.token == "secret-token"


def test_validate_dashboard_token() -> None:
    assert validate_dashboard_token("abc", "abc") is True
    assert validate_dashboard_token("wrong", "abc") is False
    assert validate_dashboard_token(None, "abc") is False
    assert validate_dashboard_token("x", None) is True


def test_live_client_place_entry_uses_sdk() -> None:
    key = "c" * 64
    client = HyperliquidLiveClient("0x" + key, use_testnet=True)
    mock_exchange = MagicMock()
    mock_exchange.order.return_value = {"status": "ok"}
    client._exchange = mock_exchange

    import asyncio

    async def _run() -> None:
        await client.place_entry(
            "BTC",
            "long",
            0.01,
            order_type="limit_maker",
            limit_price=50_000.0,
            post_only=True,
        )

    asyncio.run(_run())
    mock_exchange.order.assert_called_once()
    args = mock_exchange.order.call_args[0]
    assert args[0] == "BTC"
    assert args[1] is True
    assert args[4] == {"limit": {"tif": "Alo"}}


def test_dashboard_flask_auth() -> None:
    from src.dashboard.web import create_app

    app, socketio, _ = create_app({
        "password": "test-token-123",
        "auth_enabled": True,
    })
    client = app.test_client()

    assert client.get("/health").status_code == 200
    assert client.get("/api/status").status_code == 401

    ok = client.get(
        "/api/status",
        headers={"X-Dashboard-Token": "test-token-123"},
    )
    assert ok.status_code == 200

    check = client.post(
        "/api/auth/check",
        json={"token": "test-token-123"},
    )
    assert check.status_code == 200
    assert check.get_json()["ok"] is True


if __name__ == "__main__":
    test_normalize_private_key()
    test_resolve_private_key_from_env()
    test_dashboard_auth_disabled_by_default()
    test_dashboard_auth_from_password()
    test_validate_dashboard_token()
    test_live_client_place_entry_uses_sdk()
    test_dashboard_flask_auth()
    print("All phase5 tests passed.")
