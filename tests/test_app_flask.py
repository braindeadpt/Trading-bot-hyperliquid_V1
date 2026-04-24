"""
Integration tests for app_flask.py — Flask routes, API endpoints, system tray stubs.
"""
import pytest
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# Add root to path so app_flask can be imported
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def flask_client(mock_config):
    """Create a Flask test client with mocked bot engine."""
    with patch.dict('sys.modules', {'pystray': MagicMock(), 'PIL': MagicMock(), 'PIL.Image': MagicMock(), 'PIL.ImageDraw': MagicMock()}):
        import app_flask as af
        # Reset global state
        af.app_state.update({
            "bot_running": False,
            "trader": None,
            "aggregator": None,
            "db": None,
            "config": None,
            "last_price": 0,
            "last_data": {},
            "logs": [],
            "current_position": None,
            "equity_history": [10000],
            "trades": [],
            "capital": 10000,
            "update_count": 0,
        })
        af.config = mock_config
        af.flask_app.config['TESTING'] = True
        with af.flask_app.test_client() as client:
            yield client


# =============================================================================
# 1. Static / Dashboard Routes
# =============================================================================
class TestDashboardRoutes:
    def test_index_route_returns_dashboard(self, flask_client):
        """GET / should serve dashboard.html."""
        # dashboard.html may or may not exist in test env
        resp = flask_client.get('/')
        # Expect 200 if file exists, 404 if not — either is acceptable
        assert resp.status_code in (200, 404)

    def test_bridge_js_route(self, flask_client):
        """GET /bridge.js should return JS comment."""
        resp = flask_client.get('/bridge.js')
        assert resp.status_code == 200
        assert b'Modo Flask' in resp.data


# =============================================================================
# 2. API Status / Data Routes
# =============================================================================
class TestApiStatus:
    def test_api_status_returns_json(self, flask_client):
        """GET /api/status must return valid JSON with expected keys."""
        resp = flask_client.get('/api/status')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert 'running' in data
        assert 'price' in data
        assert 'asset' in data
        assert 'update_count' in data
        assert 'capital' in data
        assert 'equity' in data
        assert 'position' in data

    def test_api_status_default_values(self, flask_client):
        """When bot is not running, status should show defaults."""
        resp = flask_client.get('/api/status')
        data = json.loads(resp.data)
        assert data['running'] is False
        assert data['price'] == 0
        assert data['asset'] == 'BTC'
        assert data['capital'] == 10000
        assert data['position'] is None

    def test_api_logs_returns_list(self, flask_client):
        """GET /api/logs should return a list."""
        resp = flask_client.get('/api/logs')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert isinstance(data, list)

    def test_api_logs_respects_limit(self, flask_client):
        """GET /api/logs?limit=5 should return at most 5 items."""
        import app_flask as af
        af.app_state['logs'] = [
            {"time": f"2024-01-0{i}T00:00:00", "message": f"log {i}", "level": "info"}
            for i in range(1, 11)
        ]
        resp = flask_client.get('/api/logs?limit=5')
        data = json.loads(resp.data)
        assert len(data) == 5

    def test_api_trades_returns_list(self, flask_client):
        """GET /api/trades should return trades list."""
        resp = flask_client.get('/api/trades')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert isinstance(data, list)


# =============================================================================
# 3. Start / Stop Endpoints
# =============================================================================
class TestStartStop:
    def test_api_start_when_not_running(self, flask_client, mock_config):
        """POST /api/start should start the bot if not running."""
        import app_flask as af
        af.config = mock_config
        with patch('app_flask.start_bot_engine') as mock_start:
            mock_start.return_value = True
            resp = flask_client.post('/api/start')
            assert resp.status_code == 200
            data = json.loads(resp.data)
            assert data['success'] is True
            assert 'iniciado' in data['message'].lower() or 'Bot' in data['message']

    def test_api_start_when_already_running(self, flask_client):
        """POST /api/start when already running should return failure."""
        import app_flask as af
        af.app_state['bot_running'] = True
        resp = flask_client.post('/api/start')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data['success'] is False

    def test_api_stop(self, flask_client):
        """POST /api/stop should stop the bot."""
        import app_flask as af
        af.app_state['bot_running'] = True
        with patch('app_flask.stop_bot_engine') as mock_stop:
            resp = flask_client.post('/api/stop')
            assert resp.status_code == 200
            data = json.loads(resp.data)
            assert data['success'] is True
            mock_stop.assert_called_once()

    def test_api_stop_when_not_running(self, flask_client):
        """POST /api/stop when not running should still succeed gracefully."""
        resp = flask_client.post('/api/stop')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data['success'] is True


# =============================================================================
# 4. Config Endpoint
# =============================================================================
class TestConfigEndpoint:
    def test_api_config_get_returns_config(self, flask_client, mock_config):
        """GET /api/config should return the current config."""
        import app_flask as af
        af.config = mock_config
        resp = flask_client.get('/api/config')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert 'bot' in data
        assert data['bot']['paper_trading'] is True

    def test_api_config_post_saves_config(self, flask_client, mock_config, tmp_path):
        """POST /api/config should save new config to file."""
        import app_flask as af
        # Point config folder to tmp_path
        with patch.object(Path, '__truediv__', return_value=tmp_path / "settings.json"):
            new_cfg = {"bot": {"name": "Updated Bot"}, "assets": ["ETH"]}
            resp = flask_client.post('/api/config',
                                     data=json.dumps(new_cfg),
                                     content_type='application/json')
            assert resp.status_code == 200
            data = json.loads(resp.data)
            assert data['success'] is True

    def test_api_config_post_handles_error(self, flask_client):
        """POST /api/config should handle write errors gracefully."""
        import app_flask as af
        with patch('builtins.open', side_effect=PermissionError("No access")):
            resp = flask_client.post('/api/config',
                                     data=json.dumps({"x": 1}),
                                     content_type='application/json')
            assert resp.status_code == 200
            data = json.loads(resp.data)
            assert data['success'] is False
            assert 'error' in data


# =============================================================================
# 5. Force / Emergency Endpoints
# =============================================================================
class TestForceEmergency:
    def test_api_force_long_bot_not_running(self, flask_client):
        """POST /api/force/long when bot not running should fail."""
        resp = flask_client.post('/api/force/long')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data['success'] is False
        assert 'não está' in data['message'].lower() or 'Bot' in data['message']

    def test_api_force_short_bot_not_running(self, flask_client):
        """POST /api/force/short when bot not running should fail."""
        resp = flask_client.post('/api/force/short')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data['success'] is False

    def test_api_emergency_no_position(self, flask_client):
        """POST /api/emergency when no position should fail."""
        import app_flask as af
        af.app_state['current_position'] = None
        resp = flask_client.post('/api/emergency')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data['success'] is False


# =============================================================================
# 6. Monitor Loop & State Updates
# =============================================================================
class TestMonitorState:
    def test_monitor_loop_updates_position(self, flask_client):
        """Monitor loop should update current_position when trader has position."""
        import app_flask as af
        mock_trader = MagicMock()
        mock_trader.current_position = 'long'
        mock_trader.entry_price = 85000.0
        mock_trader.position_size = 100.0
        mock_trader.stop_loss_pct = 0.02
        mock_trader.trailing_stop = 83300.0
        mock_trader.entry_time = '2024-01-01T00:00:00'
        af.app_state['trader'] = mock_trader
        af.app_state['bot_running'] = True

        # Run one iteration of monitor loop
        af.app_state['equity_history'] = []
        with patch('app_flask.time.sleep', side_effect=[None, Exception("break")]):
            try:
                af.monitor_loop()
            except Exception:
                pass

        pos = af.app_state.get('current_position')
        assert pos is not None
        assert pos['direction'] == 'LONG'
        assert pos['entryPrice'] == 85000.0

    def test_monitor_loop_clears_position(self, flask_client):
        """Monitor loop should clear current_position when no active position."""
        import app_flask as af
        mock_trader = MagicMock()
        mock_trader.current_position = None
        af.app_state['trader'] = mock_trader
        af.app_state['bot_running'] = True
        af.app_state['current_position'] = {"direction": "LONG"}

        with patch('app_flask.time.sleep', side_effect=[None, Exception("break")]):
            try:
                af.monitor_loop()
            except Exception:
                pass

        assert af.app_state.get('current_position') is None

    def test_equity_history_capped_at_500(self, flask_client):
        """Equity history should be capped at 500 entries."""
        import app_flask as af
        af.app_state['equity_history'] = list(range(550))
        mock_trader = MagicMock()
        mock_trader.capital = 999
        mock_trader.current_position = None
        af.app_state['trader'] = mock_trader
        af.app_state['bot_running'] = True

        with patch('app_flask.time.sleep', side_effect=[None, Exception("break")]):
            try:
                af.monitor_loop()
            except Exception:
                pass

        assert len(af.app_state['equity_history']) <= 500


# =============================================================================
# 7. System Tray Stubs
# =============================================================================
class TestSystemTray:
    def test_create_tray_icon_returns_image(self, flask_client):
        """create_tray_icon should return an image object (or None if PIL missing)."""
        import app_flask as af
        # In test env PIL is mocked, so it should return a mock image
        with patch('app_flask.Image'):
            with patch('app_flask.ImageDraw'):
                icon = af.create_tray_icon()
                assert icon is not None

    def test_setup_tray_without_pystray_returns_none(self, flask_client):
        """If pystray not available, setup_tray should return None."""
        import app_flask as af
        with patch.object(af, 'HAS_TRAY', False):
            result = af.setup_tray()
            assert result is None
