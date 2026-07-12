"""Integration-offline tests split out of test_phase5_live_auth.py.

Covers HyperliquidLiveClient wired to a mocked SDK exchange, and the
Flask dashboard app wired to its real auth middleware.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.exchanges.hyperliquid_live import HyperliquidLiveClient


@pytest.mark.integration_offline
def test_live_client_place_entry_uses_sdk() -> None:
    key = "c" * 64
    client = HyperliquidLiveClient("0x" + key, use_testnet=True)
    mock_exchange = MagicMock()
    mock_exchange.order.return_value = {"status": "ok"}
    client._exchange = mock_exchange

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


@pytest.mark.integration_offline
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
    test_live_client_place_entry_uses_sdk()
    test_dashboard_flask_auth()
    print("All live auth integration tests passed.")
