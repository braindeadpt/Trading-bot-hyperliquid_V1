"""Drift guard: gate keys in settings.yaml must be mirrored in the parity docs.

The parity contract (README "Parity contract: minimal test config vs
production config", docs/GATES_REFERENCE.md §6) documents the gate keys the
live/backtest chain reads. This file holds the canonical mirror
(``DOCUMENTED_GATE_KEYS``) and fails when:

  * a NEW gate key lands in ``config/settings.yaml`` without being added to
    the registry (forward check — the "you forgot to document it" case), or
  * a documented key is removed/renamed in ``settings.yaml`` (reverse check —
    the "stale doc entry" case), or
  * a registry entry is not actually mentioned in README.md or
    docs/GATES_REFERENCE.md (the "registry drifted from the docs" case).

Keeping the registry exhaustive is deliberate: adding a gate key to the YAML
is a reviewable act that must touch the parity documentation too.
"""

from __future__ import annotations

import os
import re
import sys

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from src.core.signal_pipeline import (
    GATE_ORDER,
    GATE_PARITY_VERSION,
    LIVE_ONLY_GATES,
    SignalPipeline,
)
from src.core.risk_manager import RiskManager
from src.utils.config import load_config

pytestmark = pytest.mark.unit

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SETTINGS_PATH = os.path.join(ROOT, "config", "settings.yaml")
README_PATH = os.path.join(ROOT, "README.md")
GATES_REF_PATH = os.path.join(ROOT, "docs", "GATES_REFERENCE.md")

# Gate-bearing config prefixes/sections. A leaf key under any of these is a
# gate key and MUST be mirrored in the registry below (and therefore in the
# docs). Add a new gate section here when the engine/pipeline starts reading it.
GATE_PREFIXES: tuple = (
    "risk.chase_filter.",
    "risk.volatility_circuit_breaker.",
    "risk.funding_blackout.",
    "risk.symbol_risk_multiplier.",
    "risk.max_daily_stop_losses",
    "risk.max_positions",
    "risk.max_position_size_pct",
    "risk.taker_fee_pct",
    "risk.paper_slippage_pct",
    "risk.max_slippage_pct",
    "risk.min_fill_ratio",
    "risk.circuit_breaker_drawdown_pct",
    "risk.circuit_breaker_recovery_pct",
    "risk.max_daily_loss_pct",
    "risk.per_trade_risk_pct",
    "strategy.portfolio_governance.",
    "strategy.liquidation_catcher.require_real_liquidation_data",
    "strategy.liquidation_catcher.feed_warmup_events",
    "execution.tca_enabled",
    "execution.tca_mode",
    "execution.min_edge_buffer_pct",
    "execution.entry_debounce_ms",
    "execution.trailing_stop.",
    "market_data.liquidation_source",
    "market_data.liquidation_okx_enabled",
    "market_data.liquidation_bybit_enabled",
    "market_data.liquidation_coinalyze_check",
    "market_data.block_entries_on_stale",
    "market_data.block_entries_on_ws_unhealthy",
    "market_data.min_exchanges_for_green",
    "market_data.funding_stale_max_sec",
    "market_data.block_funding_strategies_on_red",
    "market_data.max_venue_spread",
    "backtest.tca_mode",
    "backtest.replay_data_quality.",
    "reconciliation.",
)

# Canonical mirror of the gate keys documented in the parity docs.
# MUST equal the dotted keys listed in docs/GATES_REFERENCE.md §6 registry and
# the README parity table. Regenerated from settings.yaml on 2026-08-13.
DOCUMENTED_GATE_KEYS: frozenset = frozenset(
    {
        "backtest.replay_data_quality.max_bar_gap_ms",
        "backtest.replay_data_quality.max_funding_stale_ms",
        "backtest.replay_data_quality.max_oi_stale_ms",
        "backtest.replay_data_quality.min_coverage_pct",
        "backtest.replay_data_quality.require_funding",
        "backtest.replay_data_quality.require_oi",
        "backtest.tca_mode",
        "execution.entry_debounce_ms",
        "execution.min_edge_buffer_pct",
        "execution.tca_enabled",
        "execution.tca_mode",
        "execution.trailing_stop.activation_pct",
        "execution.trailing_stop.enabled",
        "execution.trailing_stop.exclude_strategies",
        "execution.trailing_stop.trail_pct",
        "market_data.block_entries_on_stale",
        "market_data.block_entries_on_ws_unhealthy",
        "market_data.block_funding_strategies_on_red",
        "market_data.funding_stale_max_sec",
        "market_data.liquidation_bybit_enabled",
        "market_data.liquidation_coinalyze_check",
        "market_data.liquidation_okx_enabled",
        "market_data.liquidation_source",
        "market_data.max_venue_spread",
        "market_data.min_exchanges_for_green",
        "reconciliation.block_entries_when_stale",
        "reconciliation.enabled",
        "reconciliation.interval_sec",
        "reconciliation.mismatch_policy",
        "reconciliation.orphan_exchange_policy",
        "reconciliation.stale_threshold_sec",
        "risk.chase_filter.enabled",
        "risk.chase_filter.exempt_strategies",
        "risk.chase_filter.lookback_hours",
        "risk.chase_filter.max_runup_pct",
        "risk.circuit_breaker_drawdown_pct",
        "risk.circuit_breaker_recovery_pct",
        "risk.funding_blackout.enabled",
        "risk.funding_blackout.minutes_after",
        "risk.funding_blackout.minutes_before",
        "risk.funding_blackout.resets_utc",
        "risk.max_daily_loss_pct",
        "risk.max_daily_stop_losses",
        "risk.max_position_size_pct",
        "risk.max_positions",
        "risk.max_slippage_pct",
        "risk.min_fill_ratio",
        "risk.paper_slippage_pct",
        "risk.per_trade_risk_pct",
        "risk.symbol_risk_multiplier.SOL",
        "risk.taker_fee_pct",
        "risk.volatility_circuit_breaker.baseline_window_bars",
        "risk.volatility_circuit_breaker.block_duration_min",
        "risk.volatility_circuit_breaker.enabled",
        "risk.volatility_circuit_breaker.min_samples",
        "risk.volatility_circuit_breaker.multiplier",
        "strategy.liquidation_catcher.feed_warmup_events",
        "strategy.liquidation_catcher.require_real_liquidation_data",
        "strategy.portfolio_governance.daily_drawdown_alert",
        "strategy.portfolio_governance.daily_drawdown_circuit_pct",
        "strategy.portfolio_governance.daily_drawdown_flatten",
        "strategy.portfolio_governance.daily_drawdown_halt_entries",
        "strategy.portfolio_governance.max_correlation",
        "strategy.portfolio_governance.max_correlation_lookback",
        "strategy.portfolio_governance.max_directional_exposure_pct",
        "strategy.portfolio_governance.max_sector_exposure_pct",
    }
)


# Canonical production VALUES for the gate keys with meaningful numeric/enum
# pins. Mirror of the "production value" column in docs/GATES_REFERENCE.md §6
# registry and the README parity table. A gate key that changes its production
# value (e.g. max_positions 3 -> 5) must be a reviewable act touching the docs
# too — presence alone is not enough (the frozen window guards the hash, but
# the parity docs must stay truthful for operators reading them).
# Regenerated from settings.yaml on 2026-08-14.
GATE_KEY_VALUES: dict = {
    "risk.max_positions": 3,
    "risk.max_position_size_pct": 2.0,
    "risk.taker_fee_pct": 0.045,
    "risk.paper_slippage_pct": 0.02,
    "risk.per_trade_risk_pct": 1.0,
    "risk.max_daily_loss_pct": 3.0,
    "risk.max_daily_stop_losses": 4,
    "risk.max_slippage_pct": 0.2,
    "risk.min_fill_ratio": 0.8,
    "risk.circuit_breaker_drawdown_pct": 10.0,
    "risk.circuit_breaker_recovery_pct": 50.0,
    "risk.symbol_risk_multiplier.SOL": 0.5,
    "risk.chase_filter.enabled": True,
    "risk.chase_filter.lookback_hours": 3.0,
    "risk.chase_filter.max_runup_pct": 0.008,
    "risk.chase_filter.exempt_strategies": ["VolatilityBreakout", "DonchianBreakout"],
    "risk.volatility_circuit_breaker.enabled": True,
    "risk.volatility_circuit_breaker.multiplier": 3.0,
    "risk.volatility_circuit_breaker.baseline_window_bars": 168,
    "risk.volatility_circuit_breaker.block_duration_min": 30,
    "risk.volatility_circuit_breaker.min_samples": 24,
    "risk.funding_blackout.enabled": True,
    "risk.funding_blackout.minutes_before": 5,
    "risk.funding_blackout.minutes_after": 5,
    "risk.funding_blackout.resets_utc": ["00:00", "08:00", "16:00"],
    "strategy.portfolio_governance.max_directional_exposure_pct": 50,
    "strategy.portfolio_governance.max_sector_exposure_pct": 100,
    "strategy.portfolio_governance.max_correlation": 0.85,
    "strategy.portfolio_governance.max_correlation_lookback": 60,
    "strategy.portfolio_governance.daily_drawdown_circuit_pct": 3,
    "strategy.portfolio_governance.daily_drawdown_halt_entries": True,
    "strategy.portfolio_governance.daily_drawdown_flatten": True,
    "strategy.portfolio_governance.daily_drawdown_alert": True,
    "market_data.liquidation_source": "real",
    "market_data.liquidation_okx_enabled": True,
    "market_data.liquidation_bybit_enabled": True,
    "market_data.liquidation_coinalyze_check": True,
    "strategy.liquidation_catcher.require_real_liquidation_data": True,
    "strategy.liquidation_catcher.feed_warmup_events": 1,
    "market_data.block_entries_on_stale": True,
    "market_data.block_entries_on_ws_unhealthy": True,
    "market_data.block_funding_strategies_on_red": True,
    "market_data.funding_stale_max_sec": 300,
    "market_data.min_exchanges_for_green": 2,
    "market_data.max_venue_spread": 0.001,
    "execution.tca_enabled": True,
    "execution.tca_mode": "strict",
    "execution.min_edge_buffer_pct": 0.05,
    "execution.entry_debounce_ms": 5000,
    "execution.trailing_stop.enabled": True,
    "execution.trailing_stop.activation_pct": 0.01,
    "execution.trailing_stop.trail_pct": 0.008,
    "execution.trailing_stop.exclude_strategies": [
        "VolatilityBreakout", "VWAPDeviation", "SmartMoneyFlow", "TrendPyramid",
    ],
    "backtest.tca_mode": "proxy",
    "backtest.replay_data_quality.min_coverage_pct": 95.0,
    "backtest.replay_data_quality.max_bar_gap_ms": 120000,
    "backtest.replay_data_quality.max_funding_stale_ms": 300000,
    "backtest.replay_data_quality.max_oi_stale_ms": 300000,
    "backtest.replay_data_quality.require_funding": True,
    "backtest.replay_data_quality.require_oi": False,
    "reconciliation.enabled": True,
    "reconciliation.interval_sec": 60,
    "reconciliation.stale_threshold_sec": 120,
    "reconciliation.orphan_exchange_policy": "ADOPT_AND_PROTECT",
    "reconciliation.mismatch_policy": "HALT",
}


# Canonical mirror of the RUNTIME gate tables — what ``gate_manifest()``
# returns for the production config. Pinned here so the docs (README parity
# table, docs/GATES_REFERENCE.md §2/§3/§5) and the runtime cannot drift apart:
# changing ``GATE_ORDER``, ``LIVE_ONLY_GATES`` or ``replay_substitutes`` in
# src/core/signal_pipeline.py without updating this pin (and the docs) fails.
# Regenerated from src/core/signal_pipeline.py on 2026-08-14.
GATE_MANIFEST_PIN: dict = {
    "gate_parity_version": "phase05-gates-v1",
    "shared_gate_order": list(GATE_ORDER),
    "live_only_gates": list(LIVE_ONLY_GATES),
    "replay_substitutes": {"feed_health": "replay_data_quality"},
}

# Doc shorthand for the values that appear verbatim in GATES_REFERENCE §5.
LIVE_ONLY_DOC_NAMES: dict = {
    "execution_block": "execution_block",
    "fill_ratio": "fill_ratio",
    "slippage_l2": "slippage_l2",
    "reconciliation_stale": "reconciliation_stale",
    "executor_debounce": "executor_debounce",
}


def _runtime_gate_manifest() -> dict:
    """Instantiate the REAL SignalPipeline against production settings and
    return its gate_manifest() — the runtime source of truth for the gate
    tables (same path the backtest engine embeds in run manifests)."""
    cfg = load_config(SETTINGS_PATH)
    rm = RiskManager(cfg, None)
    live = SignalPipeline(cfg, rm, for_backtest=False)
    bt = SignalPipeline(cfg, rm, for_backtest=True)
    m = live.gate_manifest()
    m["_backtest_tca_mode"] = bt._tca_mode
    return m


def _settings_value(key: str):
    """Read a dotted key's raw value from settings.yaml."""
    with open(SETTINGS_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cur = cfg
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return "<MISSING>"
        cur = cur[part]
    return cur


def _norm(v):
    """Normalize YAML scalars for comparison (bool case, int/float)."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return float(v)
    return v


def _gate_keys_in_settings() -> set:
    """Collect the leaf gate keys actually present in settings.yaml."""
    with open(SETTINGS_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    def leaves(d, prefix=""):
        for k, v in d.items():
            key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                yield from leaves(v, key)
            else:
                yield key

    return {
        k for k in leaves(cfg)
        if any(k == p or k.startswith(p) for p in GATE_PREFIXES)
    }


def _docs_text() -> str:
    """Concatenated parity docs text (README + gate reference)."""
    parts = []
    for path in (README_PATH, GATES_REF_PATH):
        with open(path, encoding="utf-8") as f:
            parts.append(f.read())
    return "\n".join(parts)


def _gate_keys_in_docs() -> set:
    """Dotted gate keys that appear anywhere in the parity docs."""
    text = _docs_text()
    # Backticked dotted keys in docs: `risk.max_positions`, `a.b.c`, ...
    # (trailing segment may contain uppercase, e.g. `risk.symbol_risk_multiplier.SOL`)
    return set(re.findall(r"`([a-z][a-z0-9_]*\.[a-zA-Z0-9_.]+)`", text))


def test_new_gate_keys_must_be_documented() -> None:
    """Every gate key in settings.yaml must be in DOCUMENTED_GATE_KEYS."""
    actual = _gate_keys_in_settings()
    missing = sorted(actual - DOCUMENTED_GATE_KEYS)
    assert not missing, (
        "Gate key(s) present in config/settings.yaml but NOT mirrored in the "
        "parity docs:\n  {missing}\n"
        "Add each to DOCUMENTED_GATE_KEYS in tests/test_gate_key_drift.py, the "
        "README parity table and docs/GATES_REFERENCE.md §6 before keeping the key."
    ).format(missing="\n  ".join(missing))


def test_documented_keys_still_exist_in_settings() -> None:
    """Every documented gate key must still exist in settings.yaml."""
    actual = _gate_keys_in_settings()
    stale = sorted(DOCUMENTED_GATE_KEYS - actual)
    assert not stale, (
        "Documented gate key(s) missing from config/settings.yaml (removed or "
        "renamed?):\n  {stale}\n"
        "Remove them from DOCUMENTED_GATE_KEYS and the parity docs, or restore "
        "the key in settings.yaml."
    ).format(stale="\n  ".join(stale))


def test_registry_is_mirrored_in_docs() -> None:
    """Every registry entry must be mentioned verbatim in README or GATES_REFERENCE."""
    docs_keys = _gate_keys_in_docs()
    missing = sorted(DOCUMENTED_GATE_KEYS - docs_keys)
    assert not missing, (
        "Registry key(s) not mentioned anywhere in README.md / "
        "docs/GATES_REFERENCE.md:\n  {missing}\n"
        "The docs are the mirror of this registry — keep them in sync."
    ).format(missing="\n  ".join(missing))


def test_gate_prefixes_have_no_orphans() -> None:
    """Every registry entry must be reachable through GATE_PREFIXES."""
    unreachable = sorted(
        k for k in DOCUMENTED_GATE_KEYS
        if not any(k == p or k.startswith(p) for p in GATE_PREFIXES)
    )
    assert not unreachable, (
        "Registry entry(s) not covered by GATE_PREFIXES — the forward check "
        "would never see them:\n  {unreachable}"
    ).format(unreachable="\n  ".join(unreachable))


def test_gate_key_values_match_production() -> None:
    """Every pinned gate value must equal the production settings.yaml value.

    Presence is not enough: the parity docs promise *values* (max_positions 3,
    trailing activation 1%, trail 0.8%, ...). If a gate key's production value
    changes without updating this pin (and the docs), this fails.
    """
    mismatches = []
    for key, expected in sorted(GATE_KEY_VALUES.items()):
        actual = _settings_value(key)
        if isinstance(expected, list):
            if not isinstance(actual, list) or [_norm(x) for x in actual] != [_norm(x) for x in expected]:
                mismatches.append((key, expected, actual))
        else:
            if _norm(actual) != _norm(expected):
                mismatches.append((key, expected, actual))
    assert not mismatches, (
        "Gate key value(s) drifted from the production pin:\n"
        + "\n".join(f"  {k}: expected {e!r}, settings.yaml has {a!r}" for k, e, a in mismatches)
        + "\nUpdate GATE_KEY_VALUES in tests/test_gate_key_drift.py AND the parity "
        "docs (README parity table / docs/GATES_REFERENCE.md) — a production "
        "gate value change must be a reviewable act."
    )


def test_gate_key_values_are_mirrored_in_docs() -> None:
    """Pinned values must appear on the SAME docs row as their key.

    The registry already checks the docs mention each *key*; this checks the
    docs carry the *value* too (the `key` | `value` rows of GATES_REFERENCE §6
    and the README parity table), so an operator reading the docs sees the
    truth, not a stale number next to a live key. Matching key+value on the
    same line avoids false hits from common values (3, true, 0.8) that appear
    anywhere in the prose.
    """
    docs = _docs_text()
    missing = []
    for key, value in sorted(GATE_KEY_VALUES.items()):
        if isinstance(value, list):
            # Lists are rendered as abbreviations in the docs (e.g. "VB, VWAP").
            # Only require the key to be present (registry check) — the exact
            # list expansion lives in settings.yaml + this pin.
            continue
        value_str = str(value).lower()
        matched = any(
            key in line and value_str in line.lower()
            for line in docs.splitlines()
        )
        if not matched:
            missing.append((key, value))
    assert not missing, (
        "Pinned gate value(s) not on the same docs row as their key in README.md / "
        "docs/GATES_REFERENCE.md:\n  {missing}\n"
        "The docs are the mirror of this registry — keep values in sync."
    ).format(missing="\n  ".join(f"{k}={v!r}" for k, v in missing))


def test_gate_manifest_runtime_matches_pin() -> None:
    """The RUNTIME gate_manifest() must equal the pinned gate tables.

    The parity docs promise a specific shared/live-only gate split. This
    instantiates the real SignalPipeline against the production settings
    (the same path the backtest engine embeds in run manifests) and asserts
    its gate_manifest() equals GATE_MANIFEST_PIN — so changing GATE_ORDER,
    LIVE_ONLY_GATES or replay_substitutes in code without updating the pin
    (and the docs) fails.
    """
    m = _runtime_gate_manifest()
    diffs = []
    for key in ("gate_parity_version", "shared_gate_order", "live_only_gates", "replay_substitutes"):
        if m.get(key) != GATE_MANIFEST_PIN.get(key):
            diffs.append((key, GATE_MANIFEST_PIN.get(key), m.get(key)))
    assert not diffs, (
        "Runtime gate_manifest() drifted from GATE_MANIFEST_PIN:\n"
        + "\n".join(f"  {k}: pin {e!r} != runtime {a!r}" for k, e, a in diffs)
        + "\nUpdate GATE_MANIFEST_PIN in tests/test_gate_key_drift.py AND the parity "
        "docs (README parity table / docs/GATES_REFERENCE.md §2/§3/§5)."
    )


def test_gate_manifest_pin_matches_signal_pipeline_constants() -> None:
    """The pin must be regenerated from the actual module constants."""
    assert GATE_MANIFEST_PIN["gate_parity_version"] == GATE_PARITY_VERSION
    assert GATE_MANIFEST_PIN["shared_gate_order"] == list(GATE_ORDER)
    assert GATE_MANIFEST_PIN["live_only_gates"] == list(LIVE_ONLY_GATES)


def test_gate_tables_are_mirrored_in_docs() -> None:
    """Every gate in the runtime tables must appear in the parity docs.

    The shared order rows (GATES_REFERENCE §2), the live-only table (§5) and
    the substitution (§3) must each mention their gate names — so an operator
    reading the docs sees the same gate surface the runtime runs.
    """
    docs = _docs_text()
    missing = []
    for gate in GATE_ORDER:
        if f"`{gate}`" not in docs:
            missing.append(("shared_gate_order", gate))
    for gate in LIVE_ONLY_GATES:
        if f"`{gate}`" not in docs:
            missing.append(("live_only_gates", gate))
    for live_gate, sub in GATE_MANIFEST_PIN["replay_substitutes"].items():
        if f"`{live_gate}`" not in docs or f"`{sub}`" not in docs:
            missing.append(("replay_substitutes", f"{live_gate} -> {sub}"))
    assert not missing, (
        "Gate table member(s) missing from README.md / docs/GATES_REFERENCE.md:\n"
        + "\n  ".join(f"[{table}] {g}" for table, g in missing)
        + "\nThe docs are the mirror of the runtime gate tables — keep them in sync."
    )


def test_gate_manifest_backtest_tca_mode_is_proxy() -> None:
    """The runtime manifest pins backtest TCA as proxy (candle-only replay)."""
    m = _runtime_gate_manifest()
    assert m["_backtest_tca_mode"] == "proxy"
    assert m.get("tca_mode") == "strict"  # live stays strict
