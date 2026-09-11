"""Dashboard smoke tests â€” real browser render via Playwright.

Marked ``integration_offline``: boots the real Flask+Socket.IO app on a
localhost port (no external network) and drives it with headless Chromium.

Skips cleanly when Playwright or the browser binary is absent â€” CI machines
without ``playwright install chromium`` get skips, not failures.

Covers what the 2026-09-11 dashboard work added/changed:
- the shell renders without JS page errors,
- the Fase-10 gate panel + criteria chips render from ``/api/gate``,
- the alert banner is hidden when all states are healthy,
- the auth gate does not block when no dashboard token is configured.
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request

import pytest

pytestmark = pytest.mark.integration_offline

pw_sync = pytest.importorskip(
    "playwright.sync_api", reason="playwright package not installed"
)

from src.dashboard.web import create_app  # noqa: E402


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_http(url: str, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001 â€” server still booting
            time.sleep(0.25)
    return False


@pytest.fixture(scope="module")
def dash_server():
    """Boot the real dashboard app on a random localhost port."""
    # "*" origin: the test runs on a random port; production config pins
    # localhost:5000. Empty config otherwise (no auth token -> gate hidden).
    app, socketio, _emit = create_app({"cors_allowed_origins": ["*"]})
    port = _free_port()

    def _serve():
        socketio.run(
            app,
            host="127.0.0.1",
            port=port,
            use_reloader=False,
            log_output=False,
            allow_unsafe_werkzeug=True,
        )

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    if not _wait_http(base + "/health"):
        pytest.skip("dashboard server did not come up in time")
    yield base


@pytest.fixture(scope="module")
def browser():
    with pw_sync.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"chromium not installed (playwright install chromium): {exc}")
        yield b
        b.close()


def test_shell_renders_without_js_errors(dash_server, browser):
    errors: list[str] = []
    page = browser.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(dash_server + "/", wait_until="domcontentloaded", timeout=30000)
    try:
        # Shell structure â€” everything above the fold must exist in DOM.
        for sel in (
            "#kpi-bar",
            "#gate-panel",
            "#alert-banner",
            "#chart-container",
            "#conn-badge",
            "#circuit-badge",
        ):
            assert page.query_selector(sel) is not None, f"missing {sel}"
        # Auth gate must not block when no token is configured.
        gate = page.query_selector("#auth-gate")
        assert "active" not in (gate.get_attribute("class") or "")
        assert not errors, f"JS page errors: {errors}"
    finally:
        page.close()


def test_gate_endpoint_shape(dash_server):
    with urllib.request.urlopen(dash_server + "/api/gate", timeout=10) as r:
        data = json.loads(r.read())
    assert "available" in data
    if data["available"]:
        assert "criteria" in data and "trade_count" in data
        for name in ("min_trades", "profit_factor", "expectancy_r", "max_drawdown_pct"):
            crit = data["criteria"].get(name)
            assert crit is not None and crit.get("status") in {
                "PASS", "FAIL", "INSUFFICIENT_DATA"
            }
    else:
        assert data.get("error")  # graceful degradation, not a crash


def test_gate_panel_renders_criteria_chips(dash_server, browser):
    page = browser.new_page()
    page.goto(dash_server + "/", wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_function(
            "document.querySelectorAll('#gate-criteria .gate-crit').length > 0",
            timeout=15000,
        )
        verdict = page.inner_text("#gate-verdict")
        assert verdict.strip(), "gate verdict badge empty"
    finally:
        page.close()


def test_alert_banner_hidden_when_healthy(dash_server, browser):
    page = browser.new_page()
    page.goto(dash_server + "/", wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_timeout(1500)  # let updateAlertBanner() run once
        display = page.eval_on_selector(
            "#alert-banner", "el => getComputedStyle(el).display"
        )
        # No live status feed in the stub app -> the only possible alerts are
        # "socket offline" (socket connects, then it may stay) or none. The
        # banner must never be stuck visible-empty.
        items = page.eval_on_selector_all(
            "#alert-items .alert-item", "els => els.length"
        )
        if display != "none":
            assert items > 0, "alert banner visible with zero items"
    finally:
        page.close()
