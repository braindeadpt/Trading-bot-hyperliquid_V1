"""YAML configuration loader with validation and type coercion.

Supports deep-merge of nested dictionaries, required-field validation,
and safe type coercion (no eval / exec).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml

# ---------------------------------------------------------------------------
# Defaults used when a key is missing from the user-provided YAML.
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: Dict[str, Any] = {
    "exchange": {
        "hyperliquid": {
            "ws_url": "wss://api.hyperliquid.xyz/ws",
            "rest_url": "https://api.hyperliquid.xyz",
            "testnet": False,
        },
        "binance": {
            "rest_url": "https://api.binance.com",
            "ws_url": "wss://stream.binance.com:9443/ws",
        },
    },
    "risk": {
        "max_positions": 5,
        "max_daily_trades": 0,
        "max_daily_loss_pct": 3.0,
        "per_trade_risk_pct": 1.0,
        "max_position_size_pct": 5.0,
        "leverage_max": 10.0,
        "circuit_breaker_drawdown_pct": 10.0,
        "circuit_breaker_recovery_pct": 50.0,
    },
    "backtest": {
        "initial_capital": 100_000.0,
        "commission_pct": 0.04,
        "slippage_bps": 2.0,
    },
    "logging": {
        "level": "INFO",
        "json": False,
        "file": "logs/bot.log",
        "max_bytes": 10_485_760,
        "backup_count": 5,
    },
    "dashboard": {
        "host": "127.0.0.1",
        "port": 5000,
        "refresh_ms": 1000,
        "auth_enabled": False,
        "password": "",
        "token": "",
        "secret_key": "",
    },
    "database": {
        "path": "data/live/bot.db",
        "prune_days": 30,
        "auto_backfill_on_start": True,
        "backfill_days": 7,
        "backfill_min_candles_15m": 80,
        "backfill_timeframes": ["1m", "5m", "15m", "1h"],
    },
    "symbols": ["BTC", "ETH", "SOL"],
    "timeframes": ["1m", "5m", "15m", "1h"],
    "strategies": {
        "trend_follow": {"enabled": True},
        "mean_reversion": {"enabled": True},
    },
}

# ---------------------------------------------------------------------------
# Schema used for validation + coercion.
# ---------------------------------------------------------------------------

# Required top-level keys
REQUIRED_KEYS: List[str] = ["exchange", "risk", "backtest", "logging", "symbols"]

# Coercion map: dot-path → target Python type
COERCION_MAP: Dict[str, type] = {
    "risk.max_positions": int,
    "risk.max_daily_loss_pct": float,
    "risk.per_trade_risk_pct": float,
    "risk.leverage_max": float,
    "risk.circuit_breaker_drawdown_pct": float,
    "backtest.initial_capital": float,
    "backtest.commission_pct": float,
    "backtest.slippage_bps": float,
    "logging.port": int,
    "logging.max_bytes": int,
    "logging.backup_count": int,
    "dashboard.port": int,
    "dashboard.refresh_ms": int,
}


class ConfigError(Exception):
    """Raised when configuration is invalid or missing required fields."""
    pass


class Config:
    """Immutable-ish wrapper around the merged configuration dict."""

    def __init__(self, data: Dict[str, Any]) -> None:
        self._data = data

    @property
    def raw(self) -> Dict[str, Any]:
        """Return the underlying configuration dictionary.

        Useful for passing the entire config tree to subsystems that
        expect a plain dict (e.g. RiskManager, ExecutionEngine).
        """
        return self._data

    def get(self, dot_path: str, default: Any = None) -> Any:
        """Dot-notation lookup, e.g. config.get('risk.max_positions')."""
        keys = dot_path.split(".")
        node: Any = self._data
        for key in keys:
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                return default
        return node

    def has_path(self, dot_path: str) -> bool:
        """Return True iff every key in the dot-path exists in the config."""
        node: Any = self._data
        for key in dot_path.split("."):
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                return False
        return True

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def set(self, dot_path: str, value: Any) -> None:
        """Set a config value by dot-path (e.g. config.set('mode', 'paper'))."""
        keys = dot_path.split(".")
        node = self._data
        for key in keys[:-1]:
            if key not in node:
                node[key] = {}
            node = node[key]
        node[keys[-1]] = value

    def to_dict(self) -> Dict[str, Any]:
        """Return a shallow copy of the underlying dict."""
        return dict(self._data)

    def __repr__(self) -> str:
        return f"Config(keys={list(self._data.keys())})"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_config(
    path: Union[str, Path] = "config/settings.yaml",
    env_prefix: str = "BOT_",
) -> Config:
    """Load, merge, validate and coerce the application configuration.

    1. Read user YAML if it exists.
    2. Deep-merge user values over DEFAULT_CONFIG.
    3. Override with environment variables that match *env_prefix*.
    4. Validate required keys are present.
    5. Coerce types according to COERCION_MAP.

    Returns a Config instance ready for use.
    """
    user_data: Dict[str, Any] = {}
    p = Path(path)
    if p.exists():
        with open(p, "r", encoding="utf-8") as fh:
            user_data = yaml.safe_load(fh) or {}
    else:
        # Graceful fallback: use defaults only when file is missing
        user_data = {}

    merged = _deep_merge(dict(DEFAULT_CONFIG), user_data)
    _apply_env_overrides(merged, prefix=env_prefix)
    _apply_mode_overrides(merged)
    _validate_required(merged)
    _coerce_types(merged)

    return Config(merged)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _apply_mode_overrides(config: Dict[str, Any]) -> None:
    """Apply ``mode_overrides.<mode>`` shallow-merge on top of the active mode.

    Enables mainnet/testnet to ship with safer defaults without forcing
    operators to maintain two parallel YAML files.  If the active mode
    is missing or not present in ``mode_overrides`` this is a no-op.
    """
    overrides = config.get("mode_overrides")
    if not isinstance(overrides, dict):
        return
    mode = config.get("mode")
    if not isinstance(mode, str) or mode not in overrides:
        return
    block = overrides[mode]
    if not isinstance(block, dict):
        logger = logging.getLogger(__name__)
        logger.warning("mode_overrides.%s must be a dict, got %s", mode, type(block).__name__)
        return
    for section, values in block.items():
        if not isinstance(values, dict):
            continue
        target = config.get(section)
        if not isinstance(target, dict):
            config[section] = {}
            target = config[section]
        target.update(values)


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge *overlay* into *base*.  Lists are replaced, not merged."""
    result = dict(base)
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _apply_env_overrides(config: Dict[str, Any], prefix: str = "BOT_") -> None:
    """Map env vars like BOT_RISK_MAX_POSITIONS=7 into the nested dict."""
    for key, raw in os.environ.items():
        if not key.startswith(prefix):
            continue
        # Strip prefix, lower, split on underscores to form path
        path_key = key[len(prefix):].lower()
        parts = path_key.split("_")

        # Walk the nested dict; if the path doesn't exist we silently skip
        # (prevents crashes from unrelated env vars).
        node = config
        for part in parts[:-1]:
            if part not in node or not isinstance(node[part], dict):
                break
            node = node[part]
        else:
            # Coerce scalar values to int/float/bool/str
            val = _coerce_scalar(raw)
            leaf = parts[-1]
            node[leaf] = val


def _coerce_scalar(raw: str) -> Union[str, int, float, bool, None]:
    """Safely coerce a string to the most appropriate primitive type."""
    lower = raw.strip().lower()
    if lower in ("true", "yes", "1", "on"):
        return True
    if lower in ("false", "no", "0", "off"):
        return False
    if lower in ("null", "none", "~"):
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _validate_required(config: Dict[str, Any]) -> None:
    """Raise ConfigError if any required top-level key is missing."""
    missing = [k for k in REQUIRED_KEYS if k not in config]
    if missing:
        raise ConfigError(f"Missing required config keys: {missing}")


def _coerce_types(config: Dict[str, Any]) -> None:
    """Walk COERCION_MAP and cast leaf values to the declared types."""
    for dot_path, target_type in COERCION_MAP.items():
        value = _get_by_path(config, dot_path)
        if value is None:
            continue
        try:
            coerced = target_type(value)
            _set_by_path(config, dot_path, coerced)
        except (ValueError, TypeError) as exc:
            raise ConfigError(
                f"Cannot coerce config value at '{dot_path}' to {target_type.__name__}: {exc}"
            ) from exc


def _get_by_path(data: Dict[str, Any], dot_path: str) -> Any:
    """Return the value at *dot_path* or None if the path doesn't exist."""
    keys = dot_path.split(".")
    node: Any = data
    for key in keys:
        if isinstance(node, dict) and key in node:
            node = node[key]
        else:
            return None
    return node


def _set_by_path(data: Dict[str, Any], dot_path: str, value: Any) -> None:
    """Set a leaf value at *dot_path*, creating intermediate dicts if needed."""
    keys = dot_path.split(".")
    node = data
    for key in keys[:-1]:
        if key not in node:
            node[key] = {}
        node = node[key]
    node[keys[-1]] = value
