"""Backtest run manifest — reproducibility and fidelity labelling."""

from __future__ import annotations

import subprocess
from typing import Any, Dict, List, Optional, Union

from src.core.signal_pipeline import GATE_PARITY_VERSION, GATE_ORDER, LIVE_ONLY_GATES
from src.utils.config import Config, coerce_config, compute_config_hash, get_sizing_version, get_trading_symbols

SIZING_VERSION_DEFAULT = "phase05-risk-at-equity-v1"
PRE_PARITY_SIZING_VERSIONS = frozenset({
    "",
    "unknown",
    "pre-parity",
    "v3.1.19",
    "v3.1.43",
    "v3.1.47",
})


def get_git_commit() -> str:
    """Best-effort current git HEAD (short hash)."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
        return out.strip() or "unknown"
    except Exception:
        return "unknown"


def resolve_fidelity_tier(
    *,
    use_microstructure_proxy: bool,
    tca_mode: str = "proxy",
    oir_gated_strategies_active: bool = False,
    data_contract_tier: Optional[str] = None,
) -> str:
    """Classify replay fidelity. Proxy paths cannot be production-grade."""
    tca_mode = str(tca_mode).lower()
    # TCA proxy never qualifies as Tier A — OOS with proxy slippage is Tier B only.
    if tca_mode == "proxy":
        if use_microstructure_proxy and oir_gated_strategies_active:
            return "proxy_oir_gated_not_production"
        if use_microstructure_proxy:
            return "tier_b_proxy_microstructure"
        return "tier_b_tca_proxy"
    if data_contract_tier:
        if data_contract_tier.startswith("refused"):
            return data_contract_tier
        if data_contract_tier.startswith("tier_a_hl"):
            return data_contract_tier
        if data_contract_tier.startswith("tier_b"):
            return data_contract_tier
    if use_microstructure_proxy and oir_gated_strategies_active:
        return "proxy_oir_gated_not_production"
    if tca_mode == "strict":
        return "tier_a_ohlc_funding_strict_tca"
    if use_microstructure_proxy:
        return "tier_b_proxy_microstructure"
    return "tier_a_ohlc_funding"


def is_pre_parity_sizing(sizing_version: str) -> bool:
    """Return True when sizing version predates Phase 05 parity."""
    sv = str(sizing_version or "").strip().lower()
    if sv == SIZING_VERSION_DEFAULT.lower():
        return False
    if sv in PRE_PARITY_SIZING_VERSIONS:
        return True
    return not sv.startswith("phase05")


def build_run_manifest(
    config: Union[Config, Dict[str, Any]],
    *,
    symbols: Optional[List[str]] = None,
    data_source: str = "sqlite_candles",
    use_microstructure_proxy: bool = False,
    oir_gated_strategies_active: bool = False,
    kelly_effective: bool = True,
    tca_mode: str = "proxy",
    gate_manifest: Optional[Dict[str, Any]] = None,
    data_contract_tier: Optional[str] = None,
    data_contract_summary: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build manifest attached to every backtest result."""
    cfg = coerce_config(config)
    sym_list = symbols or get_trading_symbols(cfg)
    sizing_version = get_sizing_version(cfg)
    pre_parity = is_pre_parity_sizing(sizing_version)
    manifest: Dict[str, Any] = {
        "git_commit": get_git_commit(),
        "config_hash": compute_config_hash(cfg),
        "sizing_version": sizing_version,
        "symbols": sym_list,
        "data_source": data_source,
        "fidelity_tier": resolve_fidelity_tier(
            use_microstructure_proxy=use_microstructure_proxy,
            tca_mode=tca_mode,
            oir_gated_strategies_active=oir_gated_strategies_active,
            data_contract_tier=data_contract_tier,
        ),
        "kelly_effective": kelly_effective,
        "microstructure_proxy": use_microstructure_proxy,
        "tca_mode": tca_mode,
        "gate_parity_version": GATE_PARITY_VERSION,
        "shared_gate_order": list(GATE_ORDER),
        "live_only_gates": list(LIVE_ONLY_GATES),
        "pre_parity_results_invalid": pre_parity,
        "pre_parity_inventory": "docs/PRE_PARITY_BACKTEST_RESULTS.md",
    }
    if gate_manifest:
        manifest["gates"] = gate_manifest
    if data_contract_summary:
        manifest["data_contract"] = data_contract_summary
    if extra:
        manifest.update(extra)
    return manifest
