"""Shadow scoreboard API helpers for the dashboard (read-only)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.research.shadow_outcome_evaluator import (
    IDEALIZED_FILL_DISCLAIMER,
    evaluate_shadow_decisions,
    run_evaluation,
)
from src.research.shadow_recorder import ShadowRecorder
from src.utils.config import Config, load_config

ROOT = Path(__file__).resolve().parents[2]
GATE_ARTIFACTS = ROOT / "data" / "backtests" / "parity_diag"
MIN_TRADES_FOR_GATE = 30
QUARTER_MS = 90 * 86400 * 1000


def _load_gate_verdict(strategy: str) -> Optional[Dict[str, Any]]:
    """Best-effort last known gate result from parity_diag artifacts."""
    # Prefer dedicated gate file, then portfolio latest table
    gate_path = GATE_ARTIFACTS / f"baseline_gate_{strategy}.json"
    if gate_path.exists():
        try:
            data = json.loads(gate_path.read_text(encoding="utf-8"))
            folds = data.get("folds") or {}
            # Prefer any PASS, else first fold with gate
            best = None
            for fr in folds.values():
                g = fr.get("gate") or {}
                if g.get("verdict") == "PASS":
                    return {
                        "verdict": "PASS",
                        "reason": g.get("reason"),
                        "failed_conditions": g.get("failed_conditions") or [],
                        "fold": fr.get("fold") or fr.get("window"),
                        "n_trades": g.get("n_trades"),
                        "pf_percentile": g.get("pf_percentile"),
                        "source": str(gate_path.name),
                    }
                if best is None and g:
                    best = {
                        "verdict": g.get("verdict"),
                        "reason": g.get("reason"),
                        "failed_conditions": g.get("failed_conditions") or [],
                        "fold": fr.get("fold") or fr.get("window"),
                        "n_trades": g.get("n_trades"),
                        "pf_percentile": g.get("pf_percentile"),
                        "source": str(gate_path.name),
                    }
            if best:
                return best
        except (OSError, json.JSONDecodeError, TypeError):
            pass

    latest = GATE_ARTIFACTS / "baseline_portfolio_battery_latest.json"
    if latest.exists():
        try:
            data = json.loads(latest.read_text(encoding="utf-8"))
            rows = [
                r
                for r in (data.get("table") or [])
                if r.get("strategy") == strategy
            ]
            if not rows:
                return None
            # Prefer FAIL/PASS over INCONCLUSIVE if mixed
            for pref in ("PASS", "FAIL", "INCONCLUSIVE"):
                for r in rows:
                    if r.get("verdict") == pref:
                        return {
                            "verdict": pref,
                            "reason": r.get("reason"),
                            "failed_conditions": r.get("failed_conditions") or [],
                            "fold": r.get("fold"),
                            "n_trades": r.get("n_trades"),
                            "pf_percentile": r.get("B1_pf_pctile"),
                            "source": "baseline_portfolio_battery_latest.json",
                        }
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    return None


def _count_signals(
    recorder: ShadowRecorder,
    strategy: str,
    *,
    since_ms: Optional[int],
) -> int:
    rows = recorder.load_decisions(
        strategy=strategy,
        since_ms=since_ms,
        would_enter_only=True,
        limit=None,
    )
    return len(rows)


def build_shadow_panel_payload(
    *,
    shadow_names: List[str],
    config: Optional[Config] = None,
    evaluate: bool = True,
) -> Dict[str, Any]:
    """Assemble per-shadow strategy stats for ``/api/shadow_panel``."""
    cfg = config or load_config(ROOT / "config" / "settings.yaml")
    recorder = ShadowRecorder()
    now_ms = int(time.time() * 1000)
    day_ms = now_ms - 86400 * 1000
    week_ms = now_ms - 7 * 86400 * 1000
    quarter_ms = now_ms - QUARTER_MS

    boards: Dict[str, Any] = {}
    if evaluate:
        try:
            result = run_evaluation(
                since_days=14.0,
                config=cfg,
                persist=False,
            )
            boards = result.get("strategies") or {}
        except Exception as exc:  # noqa: BLE001
            boards = {"_error": str(exc)}

    rows: List[Dict[str, Any]] = []
    for name in shadow_names:
        n_today = _count_signals(recorder, name, since_ms=day_ms)
        n_7d = _count_signals(recorder, name, since_ms=week_ms)
        n_total = _count_signals(recorder, name, since_ms=None)
        n_quarter = _count_signals(recorder, name, since_ms=quarter_ms)

        board = None
        if isinstance(boards, dict):
            board = boards.get(f"{name}::phase08_shadow")
            if board is None:
                # boards keys may be scoreboard_key already
                for k, v in boards.items():
                    if isinstance(v, dict) and v.get("strategy") == name:
                        if v.get("variant") in (None, "phase08_shadow"):
                            board = v
                            break

        n_hyp = int((board or {}).get("n_evaluated") or 0)
        gate = _load_gate_verdict(name)

        # Progress: prefer hypothetical evaluated count; else raw signal count
        progress_n = n_hyp if n_hyp > 0 else n_quarter
        freq_insufficient = n_quarter < MIN_TRADES_FOR_GATE

        fidelity = "tier_a_hl_ohlc"
        if name in (
            "OrderBookScalper",
            "CVDOrderFlow",
            "LeadLag",
            "LiquidationCatcher",
            "FundingArbitrage",
            "FundingMomentum",
            "SpotPerpCarry",
        ):
            fidelity = "tier_b_missing_or_proxy"
        note = None
        if name == "OrderBookScalper":
            note = (
                "L2 not in historical replay — live shadow signals OK; "
                "baseline gate in candle replay remains non-validatable."
            )

        rows.append(
            {
                "strategy": name,
                "signals_today": n_today,
                "signals_7d": n_7d,
                "signals_total": n_total,
                "signals_90d": n_quarter,
                "hypothetical_trades_closed": n_hyp,
                "hypothetical_win_rate": (board or {}).get("win_rate"),
                "hypothetical_expectancy_r": (board or {}).get("expectancy_r"),
                "hypothetical_pnl_pct": (board or {}).get(
                    "gross_hypothetical_pnl_pct"
                ),
                "hypothetical_profit_factor": (board or {}).get("profit_factor"),
                "net_expectancy_r": (board or {}).get("net_expectancy_r"),
                "net_pnl_pct": (board or {}).get("net_hypothetical_pnl_pct"),
                "net_profit_factor": (board or {}).get("net_profit_factor"),
                "mean_fee_cost_pct": (board or {}).get("mean_fee_cost_pct"),
                "mean_funding_coverage": (board or {}).get("mean_funding_coverage"),
                "funding_coverage_ok": (board or {}).get("funding_coverage_ok"),
                "cost_model_label": (board or {}).get("cost_model_label"),
                "gate_progress_n": progress_n,
                "gate_progress_target": MIN_TRADES_FOR_GATE,
                "gate_progress_pct": min(
                    100.0, 100.0 * progress_n / MIN_TRADES_FOR_GATE
                ),
                "frequency_insufficient_90d": freq_insufficient,
                "last_gate": gate,
                "fidelity_tier": fidelity,
                "fidelity_note": note,
            }
        )

    return {
        "disclaimer": IDEALIZED_FILL_DISCLAIMER,
        "min_trades_for_gate": MIN_TRADES_FOR_GATE,
        "generated_ms": now_ms,
        "rows": rows,
    }
