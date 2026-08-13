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


# ── Hash-neutral deployment switch (DASHBOARD_AUTH_ENABLED, NOT BOT_-prefixed) ──


@pytest.mark.unit
def test_dashboard_auth_env_switch_enables_over_yaml() -> None:
    """DASHBOARD_AUTH_ENABLED=true in .env wins over auth_enabled: false in YAML."""
    with patch.dict(
        os.environ,
        {"DASHBOARD_AUTH_ENABLED": "true", "BOT_DASHBOARD_TOKEN": "env-tok-123"},
        clear=False,
    ):
        cfg = resolve_dashboard_auth({"auth_enabled": False})
    assert cfg.enabled is True
    assert cfg.token == "env-tok-123"


@pytest.mark.unit
def test_dashboard_auth_env_switch_disables_over_yaml() -> None:
    """DASHBOARD_AUTH_ENABLED=false in .env wins over auth_enabled: true in YAML."""
    with patch.dict(os.environ, {"DASHBOARD_AUTH_ENABLED": "false"}, clear=False):
        cfg = resolve_dashboard_auth({"auth_enabled": True, "password": "pw"})
    assert cfg.enabled is False


@pytest.mark.unit
def test_dashboard_auth_env_switch_absent_uses_config() -> None:
    """No env switch → dashboard.auth_enabled / token presence govern."""
    with patch.dict(os.environ, {"DASHBOARD_AUTH_ENABLED": ""}, clear=False):
        assert resolve_dashboard_auth({"auth_enabled": False}).enabled is False
        assert resolve_dashboard_auth({"auth_enabled": True, "token": "t"}).enabled is True


@pytest.mark.unit
def test_dashboard_auth_token_from_env() -> None:
    """BOT_DASHBOARD_TOKEN resolves the token even when YAML has none."""
    with patch.dict(
        os.environ,
        {"DASHBOARD_AUTH_ENABLED": "1", "BOT_DASHBOARD_TOKEN": "env-tok"},
        clear=False,
    ):
        cfg = resolve_dashboard_auth({"auth_enabled": False})
    assert cfg.token == "env-tok"


class TestDashboardAuthEndpoints:
    """Auth-enabled dashboard: REST 401 without token, 200 with, Socket.IO gated."""

    pytestmark = pytest.mark.integration_offline

    def setup_method(self):
        import src.dashboard.web as web

        self._web = web
        self._orig_engine = web._engine
        web._engine = None
        self._token = "test-dashboard-token-123"
        with patch.dict(
            os.environ,
            {"DASHBOARD_AUTH_ENABLED": "true", "BOT_DASHBOARD_TOKEN": self._token},
            clear=False,
        ):
            self.app, self.sio, self._emit = web.create_app(
                {"mode": "paper", "auth_enabled": False}
            )
        self.client = self.app.test_client()

    def teardown_method(self):
        self._web._engine = self._orig_engine

    def test_health_is_exempt(self):
        assert self.client.get("/health").status_code == 200

    def test_api_401_without_token(self):
        assert self.client.get("/api/status").status_code == 401

    def test_api_401_with_wrong_token(self):
        r = self.client.get("/api/status", headers={"X-Dashboard-Token": "wrong"})
        assert r.status_code == 401

    def test_api_200_with_header_token(self):
        r = self.client.get("/api/status", headers={"X-Dashboard-Token": self._token})
        assert r.status_code == 200

    def test_api_200_with_bearer_token(self):
        r = self.client.get(
            "/api/status", headers={"X-Dashboard-Token": f"Bearer {self._token}"}
        )
        assert r.status_code == 200

    def test_api_200_with_query_token(self):
        r = self.client.get(f"/api/status?token={self._token}")
        assert r.status_code == 200

    def test_auth_check_post_valid_token(self):
        r = self.client.post("/api/auth/check", json={"token": self._token})
        assert r.status_code == 200
        assert r.get_json() == {"ok": True, "auth_required": True}

    def test_auth_check_post_invalid_token(self):
        r = self.client.post("/api/auth/check", json={"token": "nope"})
        assert r.status_code == 401

    # ── Socket.IO connect gate ──
    #
    # NOTE: flask-socketio 5.5.1's ``test_client`` cannot run against Flask 3.1
    # (it does ``ctx.session = ...`` but ``RequestContext.session`` lost its
    # setter). The runtime WSGI path is unaffected; the gate is exercised via
    # the module-level ``_socket_connect_auth`` the runtime actually calls.

    def test_socketio_connect_rejects_missing_token(self):
        from src.dashboard.web import _socket_connect_auth

        with self.app.test_request_context("/socket.io"):
            assert _socket_connect_auth(None, True, self._token) is False

    def test_socketio_connect_rejects_wrong_token(self):
        from src.dashboard.web import _socket_connect_auth

        with self.app.test_request_context("/socket.io"):
            assert _socket_connect_auth({"token": "wrong"}, True, self._token) is False

    def test_socketio_connect_accepts_valid_token(self):
        from src.dashboard.web import _socket_connect_auth

        assert _socket_connect_auth({"token": self._token}, True, self._token) is None

    def test_socketio_connect_accepts_query_token(self):
        from src.dashboard.web import _socket_connect_auth

        with self.app.test_request_context(f"/socket.io?token={self._token}"):
            assert _socket_connect_auth(None, True, self._token) is None

    def test_socketio_connect_gate_not_clobbered_by_later_registration(self):
        """Regression guard: the connect handler registered on the namespace
        must still reject without a token. A second @socketio.on("connect")
        registration used to silently overwrite the auth gate, leaving the
        WebSocket wide open."""
        registered = self.sio.server.handlers["/"]["connect"]
        gate = getattr(registered, "__wrapped__", None)
        assert gate is not None, "registered connect handler is not a wrapped gate"
        with self.app.test_request_context("/socket.io"):
            assert gate(auth=None) is False


class TestDashboardAuthDisabledSocketIO:
    """With auth disabled, the connect gate must accept without a token."""

    pytestmark = pytest.mark.integration_offline

    def setup_method(self):
        import src.dashboard.web as web

        self._web = web
        self._orig_engine = web._engine
        web._engine = None
        with patch.dict(os.environ, {"DASHBOARD_AUTH_ENABLED": ""}, clear=False):
            self.app, self.sio, _ = web.create_app(
                {"mode": "paper", "auth_enabled": False}
            )
        self.client = self.app.test_client()

    def teardown_method(self):
        self._web._engine = self._orig_engine

    def test_socketio_connect_accepts_without_token(self):
        from src.dashboard.web import _socket_connect_auth

        with self.app.test_request_context("/socket.io"):
            assert _socket_connect_auth(None, False, None) is None
        registered = self.sio.server.handlers["/"]["connect"]
        gate = getattr(registered, "__wrapped__", None)
        assert gate is not None
        with self.app.test_request_context("/socket.io"):
            assert gate(auth=None) is None


class TestDashboardRateLimit:
    """Per-IP rate limiting on REST endpoints (brute-force mitigation)."""

    pytestmark = pytest.mark.integration_offline

    def setup_method(self):
        import src.dashboard.web as web

        self._web = web
        self._orig_engine = web._engine
        web._engine = None
        self.app, self.sio, _ = web.create_app(
            {"mode": "paper", "dashboard": {"rate_limit_per_min": 5}}
        )
        self.client = self.app.test_client()

    def teardown_method(self):
        self._web._engine = self._orig_engine

    def test_returns_429_after_limit(self):
        for _ in range(5):
            assert self.client.get("/api/status").status_code == 200
        r = self.client.get("/api/status")
        assert r.status_code == 429
        assert r.headers.get("Retry-After") == "60"

    def test_limit_is_per_ip(self):
        for _ in range(6):
            self.client.get("/api/status")
        # a different client IP is unaffected
        r = self.client.get("/api/status", environ_overrides={"REMOTE_ADDR": "203.0.113.9"})
        assert r.status_code == 200

    def test_socketio_and_static_exempt_from_limit(self):
        """Socket.IO transport + static assets never consume the budget."""
        for _ in range(6):
            self.client.get("/socket.io/")
            self.client.get("/static/nonexistent.css")
        # budget untouched: api call still allowed
        assert self.client.get("/api/status").status_code == 200

    def test_health_is_counted_like_other_rest_endpoints(self):
        """Rate limit is uniform across REST endpoints (incl. /health)."""
        for _ in range(5):
            assert self.client.get("/health").status_code == 200
        assert self.client.get("/health").status_code == 429

    def test_auth_check_endpoint_also_limited(self):
        for _ in range(5):
            self.client.post("/api/auth/check", json={"token": "x"})
        assert self.client.post("/api/auth/check", json={"token": "x"}).status_code == 429

    def test_rate_limiter_counts_failed_auth_attempts(self):
        """Brute-force vector: wrong-token requests must consume budget."""
        app, _, _ = self._web.create_app(
            {"mode": "paper", "auth_enabled": True, "token": "sekrit",
             "dashboard": {"rate_limit_per_min": 5}}
        )
        client = app.test_client()
        for _ in range(5):
            assert client.get("/api/status", headers={"X-Dashboard-Token": "wrong"}).status_code == 401
        r = client.get("/api/status", headers={"X-Dashboard-Token": "wrong"})
        assert r.status_code == 429

    def test_window_resets_after_minute(self, monkeypatch):
        fake = {"now": 1_000_000.0}
        monkeypatch.setattr(self._web.time, "time", lambda: fake["now"])
        for _ in range(5):
            self.client.get("/api/status")
        assert self.client.get("/api/status").status_code == 429
        fake["now"] += 61.0
        assert self.client.get("/api/status").status_code == 200

    def test_default_limit_applied_when_unconfigured(self):
        app, _, _ = self._web.create_app({"mode": "paper"})
        client = app.test_client()
        for _ in range(100):
            assert client.get("/api/status").status_code == 200
        assert client.get("/api/status").status_code == 429


if __name__ == "__main__":
    test_normalize_private_key()
    test_resolve_private_key_from_env()
    test_dashboard_auth_disabled_by_default()
    test_dashboard_auth_from_password()
    test_validate_dashboard_token()
    test_dashboard_auth_env_switch_enables_over_yaml()
    test_dashboard_auth_env_switch_disables_over_yaml()
    test_dashboard_auth_env_switch_absent_uses_config()
    test_dashboard_auth_token_from_env()
    print("All dashboard auth tests passed.")
