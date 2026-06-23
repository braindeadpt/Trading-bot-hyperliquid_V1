"""YAML configuration loader with validation and type coercion.

Supports deep-merge of nested dictionaries, required-field validation,
and safe type coercion (no eval / exec).
"""
from __future__ import annotations

import logging
import math
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml


def _load_dotenv(path: Path) -> int:
    """Tiny .env loader: KEY=value lines into os.environ (no override of existing).

    Returns the number of variables loaded. Skips comments and blank lines.
    Strips optional surrounding quotes. Logs a warning on malformed lines.
    """
    if not path.exists() or not path.is_file():
        return 0
    loaded = 0
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    logger = logging.getLogger(__name__)
                    logger.warning("Malformed .env line in %s: %r", path, line)
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if (value.startswith('"') and value.endswith('"')) or (
                    value.startswith("'") and value.endswith("'")
                ):
                    value = value[1:-1]
                if key and key not in os.environ:
                    os.environ[key] = value
                    loaded += 1
    except OSError as exc:
        logging.getLogger(__name__).warning("Could not read %s: %s", path, exc)
    return loaded

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
    # Load .env from the project root (if present) so YAML can reference
    # env-style secrets without forcing operators to export them in every
    # shell. We only fill in vars that are not already set, so an explicit
    # process-level export always wins.
    env_path = p.parent.parent / ".env" if p.parent.name == "config" else Path(".env")
    if not env_path.exists():
        env_path = Path(".env")
    _load_dotenv(env_path)
    if p.exists():
        with open(p, "r", encoding="utf-8") as fh:
            user_data = yaml.safe_load(fh) or {}
    else:
        # Graceful fallback: use defaults only when file is missing
        user_data = {}

    merged = _deep_merge(dict(DEFAULT_CONFIG), user_data)
    # v3.1.17 C13: mode_overrides runs first, env_overrides second so
    # explicit env vars win over the safer mode defaults.
    _apply_mode_overrides(merged)
    _apply_env_overrides(merged, prefix=env_prefix)
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
    """Map env vars like ``BOT_RISK_MAX_POSITIONS=7`` into the nested dict.

    v3.1.17 C13: previous implementation split the env-var name on ``_``
    and walked the config one segment at a time. That broke for keys
    like ``max_positions`` (one YAML key, two underscore-separated
    segments). The fix uses a *longest-prefix-match* walk: starting
    from the longest possible top-level key, try to find a matching
    dict node in the config; if found, set the remaining path inside
    it. Otherwise fall back to a direct top-level key.
    """
    for key, raw in os.environ.items():
        if not key.startswith(prefix):
            continue
        path_key = key[len(prefix):].lower()
        parts = path_key.split("_")
        matched = False
        # Try the longest top-level match first, then progressively
        # shorter. This handles e.g. "risk_max_positions" → ["risk",
        # "max_positions"] where "max_positions" is a single YAML key.
        for i in range(len(parts), 0, -1):
            candidate = "_".join(parts[:i])
            node = config.get(candidate)
            if isinstance(node, dict):
                remainder = parts[i:]
                if not remainder:
                    config[candidate] = _coerce_scalar(raw)
                    matched = True
                    break
                sub = node
                # Walk any further dict levels (e.g. "max_positions_sub").
                walked = True
                for j in range(len(remainder) - 1):
                    nxt = "_".join(remainder[: j + 1])
                    if nxt in sub and isinstance(sub[nxt], dict):
                        sub = sub[nxt]
                        remainder = remainder[j + 1 :]
                        break
                else:
                    # No further sub-dict to descend — set the remaining
                    # path as a single leaf key joined by underscores.
                    pass
                leaf = "_".join(remainder) if remainder else "_".join(parts[i:])
                sub[leaf] = _coerce_scalar(raw)
                matched = True
                break
        if not matched:
            # Fallback: try the entire path as a single top-level key.
            if path_key in config:
                config[path_key] = _coerce_scalar(raw)


def _coerce_scalar(raw: str) -> Union[str, int, float, bool, None]:
    """Safely coerce a string to the most appropriate primitive type.

    v3.1.17 C13: previously every ``"0"`` or ``"1"`` was mapped to a
    bool, which is wrong for numeric keys (``max_positions=0`` should
    stay an ``int``). The function now defaults to int/float when
    possible, and only falls back to bool when no numeric conversion
    succeeds AND the string is a recognized boolean literal.
    """
    lower = raw.strip().lower()
    # Pure digits / signed digits → int (preserves "0", "1", "-3").
    if lower.lstrip("-+").isdigit():
        try:
            return int(lower)
        except ValueError:
            pass
    # Floats (try before bool to avoid "1.5" → True).
    try:
        f = float(raw)
        if math.isfinite(f):
            return f
    except (ValueError, TypeError):
        pass
    # Recognized boolean literals.
    if lower in ("true", "yes", "on"):
        return True
    if lower in ("false", "no", "off"):
        return False
    if lower in ("null", "none", "~"):
        return None
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
