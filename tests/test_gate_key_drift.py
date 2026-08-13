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
