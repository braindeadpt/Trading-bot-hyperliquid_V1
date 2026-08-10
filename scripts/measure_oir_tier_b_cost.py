"""Measure Tier-B OIR cost: M1 proxy vs M2 OIR-off vs M3 partial oracle.

Measurement only — does not modify production settings.yaml or strategy code.
Overrides are in-memory; M3 monkeypatches BacktestEngine._build_market_event.

Usage:
  python scripts/measure_oir_tier_b_cost.py
"""
from __future__ import annotations

import copy
import json
import logging
import sys
import time
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.validate_phase08_ruleset_12w import (  # noqa: E402
    FOLDS,
    _summarize,
    ms_from_date,
)
from src.backtest.engine import BacktestEngine, build_backtest_config_from_yaml
from src.data.database import Database
from src.strategies.factory import build_backtest_strategy
from src.utils.config import Config, load_config

logging.basicConfig(level=logging.ERROR)
for n in (
    "src.core.volatility_circuit",
    "src.backtest.engine",
    "src.strategies",
    "src.core.risk_manager",
    "src.core.funding_blackout",
):
    logging.getLogger(n).setLevel(logging.ERROR)

LIVE_WR = 0.42  # reference from user (185 live trades)
ORACLE_MATCH_MS = 60_000  # inject real OIR within ±1m of a live entry timestamp


def _clone_cfg(cfg: Config) -> Config:
    return Config(copy.deepcopy(cfg._data))


def _apply_m2_oir_off(cfg: Config) -> Config:
    """w_oir=0 and require_oir_alignment=false (in-memory only)."""
    out = _clone_cfg(cfg)
    out.set("strategy.checklist_meta.w_oir", 0.0)
    out.set("strategy.checklist_meta.require_oir_alignment", False)
    return out


def _load_oracle_oir(db: Database) -> Dict[str, List[Tuple[int, float]]]:
    """symbol -> sorted [(entry_time_ms, entry_oir), ...] from live trades."""
    conn = db._conn()
    rows = conn.execute(
        """
        SELECT symbol, entry_time, entry_oir, sub_strategy, strategy
        FROM trades
        WHERE entry_oir IS NOT NULL
        """
    ).fetchall()
    by_sym: Dict[str, List[Tuple[int, float]]] = defaultdict(list)
    for r in rows:
        d = dict(r)
        sym = str(d["symbol"])
        ts = int(d["entry_time"])
        oir = float(d["entry_oir"])
        by_sym[sym].append((ts, oir))
    for sym in by_sym:
        by_sym[sym].sort(key=lambda x: x[0])
    return dict(by_sym)


def _lookup_oracle(
    series: List[Tuple[int, float]],
    ts: int,
    tol_ms: int = ORACLE_MATCH_MS,
) -> Optional[float]:
    if not series:
        return None
    # nearest timestamp
    best: Optional[Tuple[int, float]] = None
    best_dt = None
    for t, oir in series:
        dt = abs(t - ts)
        if best_dt is None or dt < best_dt:
            best_dt = dt
            best = (t, oir)
        if t > ts + tol_ms:
            break
    if best is None or best_dt is None or best_dt > tol_ms:
        return None
    return best[1]


def _install_oracle_hook(engine: BacktestEngine, oracle: Dict[str, List[Tuple[int, float]]]) -> Dict[str, int]:
    """Monkeypatch _build_market_event: real entry_oir near live entries, else proxy."""
    stats = {"oracle_hits": 0, "proxy_fallback": 0}
    orig = engine._build_market_event

    def wrapped(symbol: str, ts: int, c1m: Any, data: Dict[str, Any]) -> Any:
        event = orig(symbol, ts, c1m, data)
        oir = _lookup_oracle(oracle.get(symbol, []), ts)
        if oir is None:
            stats["proxy_fallback"] += 1
            return event
        stats["oracle_hits"] += 1
        return replace(event, orderbook_oir=oir)

    engine._build_market_event = wrapped  # type: ignore[method-assign]
    return stats


def run_window(
    cfg: Config,
    db: Database,
    symbols: List[str],
    start: str,
    end: str,
    *,
    mode: str,
    oracle: Optional[Dict[str, List[Tuple[int, float]]]] = None,
) -> Dict[str, Any]:
    strategy = build_backtest_strategy(cfg)
    bt = build_backtest_config_from_yaml(cfg)
    bt.use_volatility_circuit = False
    bt.use_funding_blackout = False
    bt.max_daily_trades = 0
    bt.tca_enabled = True
    # M1/M3 keep proxy; M2 still builds proxy into event but w_oir=0 + gate off
    # so orderbook_oir is ignored by ChecklistMeta scoring/gate.
    bt.use_microstructure_proxy = True
    engine = BacktestEngine(
        database=db,
        strategy=strategy,
        config=bt,
        symbols=symbols,
        risk_config=cfg,
    )
    oracle_stats: Dict[str, int] = {}
    if mode == "M3_oracle_partial" and oracle is not None:
        oracle_stats = _install_oracle_hook(engine, oracle)

    t0 = time.time()
    result = engine.run(start_ms=ms_from_date(start), end_ms=ms_from_date(end, end=True))
    summary = _summarize(result)
    summary["elapsed_s"] = round(time.time() - t0, 1)
    if oracle_stats:
        summary["oracle_hits"] = oracle_stats.get("oracle_hits", 0)
        summary["proxy_fallback_bars"] = oracle_stats.get("proxy_fallback", 0)
    return summary


def run_mode(
    mode: str,
    base_cfg: Config,
    db: Database,
    symbols: List[str],
    oracle: Dict[str, List[Tuple[int, float]]],
) -> Dict[str, Any]:
    if mode == "M1_proxy":
        cfg = _clone_cfg(base_cfg)
    elif mode == "M2_oir_off":
        cfg = _apply_m2_oir_off(base_cfg)
    elif mode == "M3_oracle_partial":
        cfg = _clone_cfg(base_cfg)
    else:
        raise ValueError(mode)

    print(f"\n######## {mode} ########", flush=True)
    if mode == "M2_oir_off":
        print("  checklist_meta: w_oir=0 require_oir_alignment=false", flush=True)
    if mode == "M3_oracle_partial":
        n_or = sum(len(v) for v in oracle.values())
        print(
            f"  oracle: {n_or} live entry_oir points, match_tol={ORACLE_MATCH_MS}ms; "
            "else proxy (optimistic / limited look-ahead — diagnostic only)",
            flush=True,
        )

    folds: Dict[str, Any] = {}
    for label, start, end in FOLDS:
        print(f"[{mode}] {label} {start}..{end}", flush=True)
        folds[label] = run_window(
            cfg, db, symbols, start, end, mode=mode, oracle=oracle,
        )
        print(json.dumps(folds[label], indent=2), flush=True)

    n = sum(int(v.get("n_trades") or 0) for v in folds.values())
    # trade-weighted WR / PF
    wr_num = sum(float(v.get("win_rate") or 0) * int(v.get("n_trades") or 0) for v in folds.values())
    pf_num = sum(float(v.get("profit_factor") or 0) * int(v.get("n_trades") or 0) for v in folds.values())
    pnl = sum(float(v.get("total_pnl") or 0) for v in folds.values())
    return {
        "mode": mode,
        "folds": folds,
        "aggregate": {
            "n_trades": n,
            "trade_weighted_wr": round(wr_num / n, 4) if n else 0.0,
            "trade_weighted_pf": round(pf_num / n, 4) if n else 0.0,
            "total_pnl": round(pnl, 2),
            "elapsed_s": round(sum(float(v.get("elapsed_s") or 0) for v in folds.values()), 1),
        },
    }


def interpret(results: Dict[str, Any]) -> Dict[str, Any]:
    m1 = results["M1_proxy"]["aggregate"]
    m2 = results["M2_oir_off"]["aggregate"]
    m3 = results["M3_oracle_partial"]["aggregate"]
    wrs = {
        "M1": m1["trade_weighted_wr"],
        "M2": m2["trade_weighted_wr"],
        "M3": m3["trade_weighted_wr"],
        "live_ref": LIVE_WR,
    }
    # Classification
    band_low, band_high = 0.12, 0.20
    all_in_low = all(band_low <= wrs[k] <= band_high for k in ("M1", "M2", "M3"))
    m2_or_m3_near_live = max(wrs["M2"], wrs["M3"]) >= LIVE_WR * 0.7  # within 30% relative of 42%

    if m2_or_m3_near_live and (wrs["M2"] - wrs["M1"] > 0.05 or wrs["M3"] - wrs["M1"] > 0.05):
        verdict = (
            "OIR_EXPLAINS_GAP: M2/M3 lift WR toward live — suspend negative backtest "
            "verdict until OIR can be replicated; prefer recording L2/OIR snapshots."
        )
        default_mode = "M2_oir_off" if wrs["M2"] >= wrs["M3"] else "M3_oracle_partial"
        # M3 is diagnostic-only — default recommendation should not be oracle
        if default_mode == "M3_oracle_partial":
            default_mode = "M2_oir_off"
            default_note = (
                "M3 is optimistic diagnostic only; operational default → M2 "
                "(clean OIR removal) until L2 history exists; keep M1 labeled Tier B."
            )
        else:
            default_note = "Prefer M2 as honest Tier-B default (no random proxy noise)."
    elif all_in_low:
        verdict = (
            "OIR_NOT_PRIMARY: WR stays 12–20% across M1/M2/M3 — gap vs live 42% "
            "is elsewhere (entries/exits, e.g. BE intrabar). Keep investigating exits."
        )
        default_mode = "M1_proxy"
        default_note = (
            "Proxy vs off barely moves WR; keep M1 as current default but do not "
            "treat PF as edge. Exit-parity work remains higher leverage."
        )
    else:
        verdict = (
            "MIXED: OIR mode shifts metrics but does not clearly close the live WR gap. "
            "Report magnitudes; do not claim edge."
        )
        # Prefer cleaner bias if M2 WR >= M1
        default_mode = "M2_oir_off" if wrs["M2"] >= wrs["M1"] else "M1_proxy"
        default_note = "Choose cleaner known bias (M2) if WR not hurt vs noisy proxy (M1)."

    return {
        "win_rates": wrs,
        "delta_wr_M2_minus_M1": round(wrs["M2"] - wrs["M1"], 4),
        "delta_wr_M3_minus_M1": round(wrs["M3"] - wrs["M1"], 4),
        "verdict": verdict,
        "recommended_replay_default": default_mode,
        "recommendation_note": default_note,
        "method_note": (
            "None of M1/M2/M3 makes ChecklistMeta Tier A. This measures limitation "
            "magnitude, not edge. If M3 shows large OIR value, start persisting "
            "L2/OIR snapshots in production for future Tier-A replay."
        ),
    }


def main() -> int:
    cfg = load_config(ROOT / "config" / "settings.yaml")
    snap = ROOT / "data" / "live" / "bot_ruleset_validate.db"
    db_path = snap if snap.exists() else Path(cfg.get("database.path", "data/live/bot.db"))
    db = Database(str(db_path))
    symbols = list(cfg.get("assets") or ["BTC", "ETH", "SOL", "HYPE"])
    oracle = _load_oracle_oir(db)
    print(f"db={db_path} symbols={symbols}", flush=True)
    print(
        f"oracle points: { {k: len(v) for k, v in oracle.items()} } "
        f"total={sum(len(v) for v in oracle.values())}",
        flush=True,
    )

    results: Dict[str, Any] = {}
    for mode in ("M1_proxy", "M2_oir_off", "M3_oracle_partial"):
        results[mode] = run_mode(mode, cfg, db, symbols, oracle)

    interpretation = interpret(results)
    out = {
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "live_wr_reference": LIVE_WR,
        "modes": results,
        "interpretation": interpretation,
    }
    out_dir = ROOT / "data" / "backtests"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"oir_tier_b_cost_{ts}.json"
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")

    # Human report
    report = out_dir / "parity_diag" / "OIR_TIER_B_COST_REPORT.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# OIR Tier-B cost measurement",
        "",
        f"Measured: {out['measured_at']}",
        f"Artifact: `{path.name}`",
        "",
        "## Aggregate",
        "",
        "| Mode | n | WR (tw) | PF (tw) | PnL | elapsed |",
        "|------|---|---------|---------|-----|---------|",
    ]
    for mode in ("M1_proxy", "M2_oir_off", "M3_oracle_partial"):
        a = results[mode]["aggregate"]
        lines.append(
            f"| {mode} | {a['n_trades']} | {a['trade_weighted_wr']:.1%} | "
            f"{a['trade_weighted_pf']:.3f} | {a['total_pnl']:.0f} | {a['elapsed_s']}s |"
        )
    lines += [
        "",
        f"Live WR reference: **{LIVE_WR:.0%}**",
        "",
        f"Δ WR M2−M1: **{interpretation['delta_wr_M2_minus_M1']:+.1%}**",
        f"Δ WR M3−M1: **{interpretation['delta_wr_M3_minus_M1']:+.1%}**",
        "",
        "## Verdict",
        "",
        interpretation["verdict"],
        "",
        f"**Recommended replay default:** `{interpretation['recommended_replay_default']}`",
        "",
        interpretation["recommendation_note"],
        "",
        "## Methodological note",
        "",
        interpretation["method_note"],
        "",
        "## Per-fold detail",
        "",
    ]
    for mode in ("M1_proxy", "M2_oir_off", "M3_oracle_partial"):
        lines.append(f"### {mode}")
        lines.append("")
        lines.append("| Fold | n | WR | avgW | avgL | exp | PF | Sharpe | maxDD | PnL |")
        lines.append("|------|---|----|------|------|-----|----|--------|-------|-----|")
        for label, _s, _e in FOLDS:
            f = results[mode]["folds"][label]
            lines.append(
                f"| {label} | {f['n_trades']} | {f['win_rate']:.1%} | "
                f"{f['avg_win']:.1f} | {f['avg_loss']:.1f} | {f['expectancy']:.1f} | "
                f"{f['profit_factor']:.3f} | {f['sharpe']:.1f} | {f['max_dd_pct']:.2%} | "
                f"{f['total_pnl']:.0f} |"
            )
        lines.append("")

    report.write_text("\n".join(lines), encoding="utf-8")
    print("\n" + "\n".join(lines), flush=True)
    print(f"\nWrote {path}", flush=True)
    print(f"Wrote {report}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
