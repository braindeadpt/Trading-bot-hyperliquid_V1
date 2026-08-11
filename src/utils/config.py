"""YAML configuration loader with validation and type coercion.

Supports deep-merge of nested dictionaries, required-field validation,
and safe type coercion (no eval / exec).
"""
from __future__ import annotations

import hashlib
import json
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
        "max_daily_stop_losses": 4,
        "max_daily_loss_pct": 3.0,
        "per_trade_risk_pct": 1.0,
        "max_position_size_pct": 2.0,
        "leverage_max": 10.0,
        "circuit_breaker_drawdown_pct": 10.0,
        "circuit_breaker_recovery_pct": 50.0,
        "symbol_risk_multiplier": {"SOL": 0.5},
        "taker_fee_pct": 0.045,  # HL perps tier-0 taker % per side
        "paper_slippage_pct": 0.02,
        "chase_filter": {
            "enabled": True,
            "lookback_hours": 3.0,
            "max_runup_pct": 0.008,
            "exempt_strategies": ["VolatilityBreakout", "DonchianBreakout"],
        },
    },
    "backtest": {
        "initial_capital": 100_000.0,
        "commission_pct": 0.045,  # HL perps tier-0 taker % per side
        "slippage_bps": 2.0,
        "kelly_override": None,
        "intrabar_conflict_policy": "pessimistic",
        "sizing_version": "phase05-risk-at-equity-v1",
        "tca_mode": "proxy",
        # NOTE: "warmup_15m_bars" (110) and replay_data_quality "parity_mode"
        # (True) are deliberately NOT declared here. They are backtest-only and
        # their consumers already default to the same values
        # (src/backtest/engine.py: cfg.get("backtest.warmup_15m_bars", 110);
        # src/backtest/replay_data_quality.py: qc.get("parity_mode", True)).
        # Declaring them changes the effective config_hash, which the Fase 10
        # frozen-window assert in main.py treats as mid-window drift and
        # refuses to start on. Re-add only when re-registering the window.
        "replay_data_quality": {
            "min_coverage_pct": 95.0,
            "max_bar_gap_ms": 120_000,
            "max_funding_stale_ms": 300_000,
            "max_oi_stale_ms": 300_000,
            "require_funding": True,
            "require_oi": False,
        },
    },
    "research": {
        "database": {
            "path": "data/research/hyperliquid.db",
        },
        "require_hl_venue": False,
        "refuse_insufficient_feeds": True,
        "strict_mode": True,
        "continuous_sampling_enabled": True,
        "ws_microstructure_enabled": True,
        "rest_sampling_enabled": False,
        "sampler_interval_sec": 60.0,
        "l2_min_interval_ms": 250.0,
        "tape_gap_threshold_ms": 5_000,
        "l2_stale_threshold_ms": 10_000,
        "health_report_interval_sec": 30.0,
        "sampler_symbols": ["BTC", "ETH", "SOL", "HYPE"],
        "backfill_symbols": ["BTC", "ETH", "SOL", "HYPE"],
        "backfill_days": 7,
        "backfill_timeframes": ["1m", "5m", "15m", "1h"],
        "min_coverage_pct": 95.0,
        "sample_microstructure": True,
        "gap_intervals": 2,
        "gap_intervals_by_tf": {
            "1m": 2,
            "5m": 2,
            "15m": 2,
            "1h": 2,
        },
    },
    # Research L2 levels (not operational DB). See docs/L2_BOOK_RECORDING.md
    "market_data": {
        "l2_recording": {
            "enabled": True,
            "interval_sec": 1.0,
            "depth_levels": 25,
            "min_mid_change_bps": 1.0,
            # Relative default for CI/VPS; production YAML points at HDD volume.
            "path": "data/research/l2_books",
            "retention_days": 365,
            "prune_interval_sec": 3600.0,
            "queue_max": 5000,
            "flush_interval_sec": 1.0,
        },
        "feed_silence": {
            "l2_book_recording_max_sec": 120.0,
        },
        "top_trader_tracker": {
            "enabled": True,
            "top_n": 10,
            "poll_interval_sec": 60.0,
            "request_delay_sec": 0.15,
            "min_notional_usd": 10000.0,
            "wallets_path": "data/research/top_traders.json",
            "auto_from_leaderboard": True,
            "leaderboard_window": "allTime",
            "leaderboard_refresh_hours": 24.0,
            "min_account_value": 100000.0,
            "min_volume": 5000000.0,
            "require_month_positive": True,
            "require_consistent_windows": True,
            "min_month_volume": 1000000.0,
            "min_all_time_pnl": 1000000.0,
        },
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
        "backfill_funding_days": 30,
        "backfill_perp_days": 7,
        "backfill_min_candles_15m": 80,
        "backfill_timeframes": ["1m", "5m", "15m", "1h"],
    },
    "symbols": ["BTC", "ETH", "SOL", "HYPE"],
    "timeframes": ["1m", "5m", "15m", "1h"],
    "strategies": {
        "trend_follow": {"enabled": True},
        "mean_reversion": {"enabled": True},
    },
    "reconciliation": {
        "enabled": True,
        "interval_sec": 60,
        "stale_threshold_sec": 120,
        "orphan_exchange_policy": "ADOPT_AND_PROTECT",
        "mismatch_policy": "HALT",
        "block_entries_when_stale": True,
    },
    "execution": {
        "native_protection": {
            "enabled": True,
            "software_stop_redundancy": True,
        },
        "market_order": {
            # sdk_market = HL SDK market_open/close (default).
            # limit_slippage_cap = aggressive IoC limit with hard slip band.
            "mode": "sdk_market",
            "max_slippage_pct": 5.0,
        },
        "liquidation_reconcile": {
            "enabled": True,
            "lookback_ms": 86_400_000,
        },
        "oms_poll_interval_s": 5.0,
        "live_order_timeout_s": 60.0,
        "tca_mode": "strict",
        "maker_orders": {
            "enabled": True,
            "maker_fee_pct": 0.015,  # HL perps tier-0 maker % per side
        },
    },
}

# ---------------------------------------------------------------------------
# Schema used for validation + coercion.
# ---------------------------------------------------------------------------

# Required top-level keys
REQUIRED_KEYS: List[str] = ["exchange", "risk", "backtest", "logging", "symbols"]

# Top-level keys accepted in user YAML (unknown keys fail startup validation).
KNOWN_TOP_LEVEL_KEYS: frozenset[str] = frozenset({
    "mode",
    "version",
    "engine",
    "assets",
    "symbols",
    "timeframes",
    "exchange",
    "risk",
    "strategy",
    "reconciliation",
    "execution",
    "market_data",
    "backtest",
    "database",
    "research",
    "dashboard",
    "logging",
    "alerts",
    "mode_overrides",
    "strategies",  # legacy alias kept for older YAML snippets
})

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

    _validate_unknown_top_level_keys(user_data)
    merged = _deep_merge(dict(DEFAULT_CONFIG), user_data)
    # v3.1.17 C13: mode_overrides runs first, env_overrides second so
    # explicit env vars win over the safer mode defaults.
    _apply_mode_overrides(merged)
    _apply_env_overrides(merged, prefix=env_prefix)
    _normalize_trading_symbols(merged)
    _validate_required(merged)
    _coerce_types(merged)

    return Config(merged)


def coerce_config(config: Union["Config", Dict[str, Any], Any]) -> "Config":
    """Duck-typed coercion of a Config-like object (or plain dict) to *this*
    module's ``Config`` class.

    The process consistently runs with BOTH the repo root and ``src/`` on
    ``sys.path`` (main.py's long-standing bare-import convention, e.g.
    ``from utils.config import ...``), while newer modules import via
    ``from src.utils.config import ...``. Python treats ``utils.config`` and
    ``src.utils.config`` as two distinct module objects even though they
    load the identical file, so ``utils.config.Config`` and
    ``src.utils.config.Config`` are two different classes. A nominal
    ``isinstance(config, Config)`` check silently fails whenever a
    ``Config`` built via one import path is handed to a function that
    imported ``Config`` via the other path — the caller then does
    ``Config(config)``, wrapping the *Config object itself* as ``_data``
    instead of its underlying dict, so every subsequent ``.get()`` dot-path
    lookup returns ``None``/defaults.

    Detect "quacks like a Config" via the ``.raw`` property (present on any
    structurally-identical ``Config`` regardless of module identity)
    instead of nominal type, and unwrap through it.
    """
    if isinstance(config, Config):
        return config
    raw = getattr(config, "raw", None)
    if isinstance(raw, dict):
        return Config(raw)
    if isinstance(config, dict):
        return Config(config)
    # Last resort — matches prior behaviour for genuinely unknown types.
    return Config(config)


def get_trading_symbols(config: Union[Config, Dict[str, Any]]) -> List[str]:
    """Return the canonical symbol list used by feeds, engine, and backtest."""
    data = coerce_config(config).raw
    symbols = data.get("symbols")
    assets = data.get("assets")
    if isinstance(symbols, list) and symbols:
        return [str(s) for s in symbols]
    if isinstance(assets, list) and assets:
        return [str(s) for s in assets]
    return ["BTC", "ETH", "SOL"]


def get_strategy_section(
    config: Union[Config, Dict[str, Any]],
    section: str,
    default: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Read ``strategy.<section>`` with legacy top-level fallback."""
    cfg = coerce_config(config)
    primary = cfg.get(f"strategy.{section}")
    if isinstance(primary, dict):
        return dict(primary)
    legacy = cfg.get(section)
    if isinstance(legacy, dict):
        return dict(legacy)
    return dict(default or {})


def phase08_enabled(config: Union[Config, Dict[str, Any]]) -> bool:
    """Return True when Phase 08 edge-isolation mode is active."""
    p08 = get_strategy_section(config, "phase08")
    return bool(p08.get("enabled", False))


def resolve_kelly_enabled(
    config: Union[Config, Dict[str, Any]],
    *,
    for_backtest: bool = False,
) -> bool:
    """Return effective Kelly flag: ``strategy.kelly.enabled`` with optional backtest override.

  ``backtest.kelly_override`` (``null``/missing → follow live) is the audit-able
  backtest-only knob. Legacy ``backtest.use_kelly`` is honoured when override is unset.
    """
    kelly_cfg = get_strategy_section(config, "kelly")
    base = bool(kelly_cfg.get("enabled", True))
    if not for_backtest:
        return base
    cfg = coerce_config(config)
    override = cfg.get("backtest.kelly_override")
    if override is not None:
        return bool(override)
    legacy = cfg.get("backtest.use_kelly")
    if legacy is not None:
        return bool(legacy)
    return base


def compute_config_hash(config: Union[Config, Dict[str, Any]]) -> str:
    """Stable SHA-256 of the merged config (sorted JSON, no secrets)."""
    data = coerce_config(config).raw
    sanitized = _sanitize_config_for_hash(data)
    payload = json.dumps(sanitized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def get_sizing_version(config: Union[Config, Dict[str, Any]]) -> str:
    """Return the declared sizing/parity schema version for run manifests."""
    cfg = coerce_config(config)
    return str(cfg.get("backtest.sizing_version", "phase05-risk-at-equity-v1"))


def _sanitize_config_for_hash(data: Dict[str, Any]) -> Dict[str, Any]:
    """Drop secret-like keys before hashing."""
    skip_keys = {
        "password", "token", "secret", "secret_key", "api_key", "api_secret",
        "telegram_bot_token", "telegram_chat_id", "coinalyze_api_key",
    }

    def _walk(node: Any) -> Any:
        if isinstance(node, Config) or isinstance(getattr(node, "raw", None), dict):
            return _walk(node.raw)
        if isinstance(node, dict):
            out: Dict[str, Any] = {}
            for k, v in node.items():
                if str(k).lower() in skip_keys or str(k).endswith("_key"):
                    continue
                out[str(k)] = _walk(v)
            return out
        if isinstance(node, (list, tuple)):
            return [_walk(x) for x in node]
        if isinstance(node, (str, int, float, bool)) or node is None:
            return node
        return str(node)

    return _walk(data)


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
        # Deep-merge strategy sub-sections so mode overrides patch keys
        # (e.g. lead_lag.enabled) without wiping the rest of the block.
        if section == "strategy":
            for strat_key, strat_vals in values.items():
                if isinstance(strat_vals, dict):
                    existing = target.get(strat_key)
                    if isinstance(existing, dict):
                        target[strat_key] = _deep_merge(existing, strat_vals)
                    else:
                        target[strat_key] = dict(strat_vals)
                else:
                    target[strat_key] = strat_vals
        else:
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


def _validate_unknown_top_level_keys(user_data: Dict[str, Any]) -> None:
    """Reject user YAML with unrecognized top-level keys at startup."""
    if not user_data:
        return
    unknown = sorted(k for k in user_data.keys() if k not in KNOWN_TOP_LEVEL_KEYS)
    if unknown:
        raise ConfigError(
            f"Unknown config keys: {unknown}. "
            f"Allowed top-level keys: {sorted(KNOWN_TOP_LEVEL_KEYS)}"
        )


def _normalize_trading_symbols(config: Dict[str, Any]) -> None:
    """Unify ``assets`` and ``symbols`` into one canonical list."""
    logger = logging.getLogger(__name__)
    assets = config.get("assets")
    symbols = config.get("symbols")
    assets_list = [str(s) for s in assets] if isinstance(assets, list) else None
    symbols_list = [str(s) for s in symbols] if isinstance(symbols, list) else None

    if assets_list and symbols_list and assets_list != symbols_list:
        logger.warning(
            "Config assets=%s differs from symbols=%s — using assets as canonical",
            assets_list,
            symbols_list,
        )
        canonical = assets_list
    elif assets_list:
        canonical = assets_list
    elif symbols_list:
        canonical = symbols_list
    else:
        canonical = ["BTC", "ETH", "SOL"]

    config["symbols"] = list(canonical)
    config["assets"] = list(canonical)


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
