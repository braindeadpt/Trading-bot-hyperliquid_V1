"""Signal significance test: ChecklistMeta vs random / passive baselines.

Measurement only — does not modify production strategies or config.

Baselines (same window, symbols, SL/TP/BE/max-hold/fees; signal replaced):
  B1 — real timing, random direction
  B2 — real direction mix, random timing
  B3 — random direction + timing (matched trade counts per day×symbol)
  Passiveive — equal-weight buy&hold; always-long / always-short at CM times

Usage:
  python scripts/baseline_signal_test.py
  python scripts/baseline_signal_test.py --seeds 200 --folds W2,W3
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import random
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.backtest.engine import BacktestEngine, build_backtest_config_from_yaml  # noqa: E402
from src.data.database import Database  # noqa: E402
from src.strategies.checklist_meta import ChecklistMeta  # noqa: E402
from src.strategies.factory import DirectStrategyRouter  # noqa: E402
from src.utils.config import load_config  # noqa: E402
from src.utils.helpers import safe_divide, safe_float  # noqa: E402

logging.basicConfig(level=logging.ERROR)
for _n in (
    "src.core.volatility_circuit",
    "src.backtest.engine",
    "src.strategies",
    "src.core.risk_manager",
    "src.core.funding_blackout",
):
    logging.getLogger(_n).setLevel(logging.ERROR)

FOLDS = {
    "W2": ("W2_0613_0710", "2026-06-13", "2026-07-10"),
    "W3": ("W3_0711_0807", "2026-07-11", "2026-08-07"),
}

OUT_DIR = ROOT / "data" / "backtests" / "parity_diag"


def ms_from_date(s: str, end: bool = False) -> int:
    dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp() * 1000)


def utc_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def trade_metrics(pnls: Sequence[float]) -> Dict[str, float]:
    arr = [float(p) for p in pnls]
    n = len(arr)
    if n == 0:
        return {
            "n_trades": 0,
            "total_pnl": 0.0,
            "expectancy": 0.0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "sharpe": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "rr": 0.0,
        }
    wins = [p for p in arr if p > 0]
    losses = [p for p in arr if p <= 0]
    gp = sum(wins)
    gl = abs(sum(losses))
    pf = (gp / gl) if gl > 0 else (10.0 if gp > 0 else 0.0)
    avg = sum(arr) / n
    std = float(np.std(arr, ddof=1)) if n > 1 else 0.0
    # Trade-level Sharpe (not annualised) — comparable across seeds
    sharpe = (avg / std) if std > 1e-12 else 0.0
    avg_w = sum(wins) / len(wins) if wins else 0.0
    avg_l = sum(losses) / len(losses) if losses else 0.0
    rr = abs(avg_w / avg_l) if avg_l < 0 else 0.0
    return {
        "n_trades": n,
        "total_pnl": round(sum(arr), 4),
        "expectancy": round(avg, 4),
        "win_rate": round(len(wins) / n, 4),
        "profit_factor": round(pf, 4),
        "sharpe": round(sharpe, 4),
        "avg_win": round(avg_w, 4),
        "avg_loss": round(avg_l, 4),
        "rr": round(rr, 4),
    }


def percentile_rank(value: float, samples: Sequence[float], *, higher_better: bool) -> float:
    """Fraction of samples that the value beats (0–100)."""
    if not samples:
        return float("nan")
    if higher_better:
        beat = sum(1 for s in samples if value >= s)
    else:
        beat = sum(1 for s in samples if value <= s)
    return round(100.0 * beat / len(samples), 2)


def dist_summary(samples: Sequence[float]) -> Dict[str, float]:
    if not samples:
        return {}
    a = np.asarray(samples, dtype=float)
    return {
        "mean": round(float(np.mean(a)), 4),
        "std": round(float(np.std(a, ddof=1)) if len(a) > 1 else 0.0, 4),
        "p05": round(float(np.percentile(a, 5)), 4),
        "p25": round(float(np.percentile(a, 25)), 4),
        "p50": round(float(np.percentile(a, 50)), 4),
        "p75": round(float(np.percentile(a, 75)), 4),
        "p95": round(float(np.percentile(a, 95)), 4),
        "min": round(float(np.min(a)), 4),
        "max": round(float(np.max(a)), 4),
    }


# ---------------------------------------------------------------------------
# Candle store + ATR
# ---------------------------------------------------------------------------


@dataclass
class Bar:
    ts: int
    o: float
    h: float
    l: float
    c: float


class CandleStore:
    def __init__(self) -> None:
        self.bars_1m: Dict[str, List[Bar]] = {}
        self.idx_1m: Dict[str, Dict[int, int]] = {}
        self.bars_15m: Dict[str, List[Bar]] = {}

    def load(self, db: Database, symbols: List[str], start_ms: int, end_ms: int) -> None:
        # Warmup lookback for ATR
        warm = start_ms - 14 * 15 * 60_000
        for sym in symbols:
            rows = db.get_candles(sym, "1m", start_ms=warm, end_ms=end_ms, limit=500_000)
            bars = [
                Bar(
                    ts=int(r.timestamp_ms),
                    o=float(r.open),
                    h=float(r.high),
                    l=float(r.low),
                    c=float(r.close),
                )
                for r in rows
                if float(r.close) > 0
            ]
            bars.sort(key=lambda b: b.ts)
            self.bars_1m[sym] = bars
            self.idx_1m[sym] = {b.ts: i for i, b in enumerate(bars)}

            rows15 = db.get_candles(sym, "15m", start_ms=warm, end_ms=end_ms, limit=50_000)
            b15 = [
                Bar(
                    ts=int(r.timestamp_ms),
                    o=float(r.open),
                    h=float(r.high),
                    l=float(r.low),
                    c=float(r.close),
                )
                for r in rows15
                if float(r.close) > 0
            ]
            b15.sort(key=lambda b: b.ts)
            self.bars_15m[sym] = b15

    def bar_at_or_before(self, symbol: str, ts: int) -> Optional[Bar]:
        bars = self.bars_1m.get(symbol) or []
        if not bars:
            return None
        # binary search
        lo, hi = 0, len(bars) - 1
        best = None
        while lo <= hi:
            mid = (lo + hi) // 2
            if bars[mid].ts <= ts:
                best = bars[mid]
                lo = mid + 1
            else:
                hi = mid - 1
        return best

    def index_at_or_after(self, symbol: str, ts: int) -> Optional[int]:
        bars = self.bars_1m.get(symbol) or []
        if not bars:
            return None
        lo, hi = 0, len(bars) - 1
        ans = None
        while lo <= hi:
            mid = (lo + hi) // 2
            if bars[mid].ts >= ts:
                ans = mid
                hi = mid - 1
            else:
                lo = mid + 1
        return ans

    def atr_pct(self, symbol: str, ts: int, period: int = 14) -> float:
        bars = self.bars_15m.get(symbol) or []
        # closed 15m bars strictly before ts
        prior = [b for b in bars if b.ts + 15 * 60_000 <= ts]
        if len(prior) < period + 1:
            return 0.01
        window = prior[-(period + 1) :]
        trs: List[float] = []
        for i in range(1, len(window)):
            h, l, pc = window[i].h, window[i].l, window[i - 1].c
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        atr = sum(trs[-period:]) / period
        px = prior[-1].c
        return max(atr / px, 1e-6) if px > 0 else 0.01


# ---------------------------------------------------------------------------
# Exit / risk params (mirror ChecklistMeta YAML — read-only)
# ---------------------------------------------------------------------------


@dataclass
class RiskParams:
    stop_atr_mult: float = 1.5
    tp_atr_mult: float = 3.0
    be_trigger_r: float = 0.6
    be_buffer_pct: float = 0.001
    be_vol_atr_factor: float = 0.75
    max_hold_ms: int = 6 * 3600_000
    fee_pct: float = 0.00035  # per side
    paper_slip_pct: float = 0.0002
    slippage_bps: float = 2.0
    max_positions: int = 5
    base_size_pct: float = 0.0075
    initial_capital: float = 100_000.0
    use_sl_to_be: bool = True


# Config path hint for risk params (display name → yaml section)
_STRATEGY_CFG_PATH: Dict[str, str] = {
    "ChecklistMeta": "strategy.checklist_meta",
    "VWAPDeviation": "strategy.vwap_deviation",
    "VolatilityBreakout": "strategy.volatility_breakout",
    "DonchianBreakout": "strategy.donchian_breakout",
    "CVDOrderFlow": "strategy.cvd_orderflow",
    "CVDOrderFlow_p90": "strategy.cvd_orderflow",
    "OrderBookScalper": "strategy.orderbook_scalper",
    "FundingArbitrage": "strategy.funding_arbitrage",
    "FundingMomentum": "strategy.funding_momentum",
    "SpotPerpCarry": "strategy.spot_perp_carry",
    "LeadLag": "strategy.lead_lag",
    "LiquidationCatcher": "strategy.liquidation_catcher",
    "SFPReversion": "strategy.sfp_reversion",
    "VARejection": "strategy.va_rejection",
    "RangeGrid": "strategy.range_grid",
    "TrendPyramid": "strategy.trend_pyramid",
    "SmartMoneyFlow": "strategy.trend_follow",
    "FundingExtreme": "strategy.mean_reversion",
}


def risk_from_cfg(cfg: Any, strategy_name: str = "ChecklistMeta") -> RiskParams:
    path = _STRATEGY_CFG_PATH.get(strategy_name, "strategy.checklist_meta")
    sec = cfg.get(path) or {}
    raw_fee = float(cfg.get("risk.taker_fee_pct", 0.035))
    fee_pct = raw_fee / 100.0 if raw_fee > 0.001 else raw_fee
    # TP: prefer atr mult; else R-multiple * stop atr
    tp_atr = sec.get("take_profit_atr_multiplier")
    if tp_atr is None and sec.get("take_profit_r_multiple") is not None:
        tp_atr = float(sec.get("take_profit_r_multiple", 2.0)) * float(
            sec.get("stop_loss_atr_multiplier", 1.5)
        )
    use_be = bool(sec.get("use_sl_to_be_after_1r", False))
    hold_h = float(sec.get("max_hold_hours", sec.get("max_hold_minutes", 360) / 60.0))
    if "max_hold_minutes" in sec and "max_hold_hours" not in sec:
        hold_h = float(sec["max_hold_minutes"]) / 60.0
    if "max_hold_seconds" in sec and "max_hold_hours" not in sec:
        hold_h = float(sec["max_hold_seconds"]) / 3600.0
    return RiskParams(
        stop_atr_mult=float(sec.get("stop_loss_atr_multiplier", 1.5)),
        tp_atr_mult=float(tp_atr if tp_atr is not None else 3.0),
        be_trigger_r=float(sec.get("sl_to_be_trigger_r", sec.get("sl_to_be_r_trigger", 0.6))),
        be_buffer_pct=float(sec.get("sl_to_be_buffer_pct", 0.001)),
        be_vol_atr_factor=float(sec.get("sl_to_be_vol_atr_factor", 0.75)),
        max_hold_ms=int(hold_h * 3600_000),
        fee_pct=fee_pct,
        paper_slip_pct=float(cfg.get("risk.paper_slippage_pct", 0.02)) / 100.0,
        slippage_bps=float(cfg.get("backtest.slippage_bps", 2.0)),
        max_positions=int(cfg.get("risk.max_positions", 5)),
        base_size_pct=float(sec.get("base_size_pct", 0.0075)),
        initial_capital=float(cfg.get("backtest.initial_capital", 100_000.0)),
        use_sl_to_be=use_be,
    )


@dataclass
class EntrySpec:
    entry_time_ms: int
    symbol: str
    side: str
    stop_loss_pct: float
    take_profit_pct: float
    size: float
    entry_price_hint: float = 0.0


@dataclass
class SimTrade:
    symbol: str
    side: str
    entry_time: int
    exit_time: int
    entry_price: float
    exit_price: float
    size: float
    pnl_usd: float
    exit_reason: str


def _apply_entry_slip(price: float, side: str, bps: float) -> float:
    f = bps / 10_000.0
    return price * (1.0 + f) if side == "long" else price * (1.0 - f)


def _apply_exit_slip_market(price: float, side: str, bps: float) -> float:
    f = bps / 10_000.0
    return price * (1.0 - f) if side == "long" else price * (1.0 + f)


def _pnl(
    side: str,
    entry: float,
    exit_px: float,
    size: float,
    fee_pct: float,
    *,
    be: bool,
    paper_slip: float,
    slip_bps: float,
) -> Tuple[float, float]:
    """Return (exit_fill, net_pnl)."""
    if be:
        fill = exit_px
        if paper_slip > 0:
            fill = fill * (1.0 - paper_slip) if side == "long" else fill * (1.0 + paper_slip)
    else:
        fill = _apply_exit_slip_market(exit_px, side, slip_bps)
    gross = (fill - entry) * size if side == "long" else (entry - fill) * size
    fees = (entry * size + fill * size) * fee_pct
    return fill, gross - fees


def simulate_one(
    store: CandleStore,
    spec: EntrySpec,
    risk: RiskParams,
) -> Optional[SimTrade]:
    bars = store.bars_1m.get(spec.symbol) or []
    i0 = store.idx_1m.get(spec.symbol, {}).get(spec.entry_time_ms)
    if i0 is None:
        i0 = store.index_at_or_after(spec.symbol, spec.entry_time_ms)
    if i0 is None or i0 >= len(bars):
        return None
    bar0 = bars[i0]
    raw = bar0.c
    if raw <= 0:
        return None
    entry = _apply_entry_slip(raw, spec.side, risk.slippage_bps)
    sl_pct = max(spec.stop_loss_pct, 1e-6)
    tp_pct = max(spec.take_profit_pct, 1e-6)
    if spec.side == "long":
        sl = entry * (1.0 - sl_pct)
        tp = entry * (1.0 + tp_pct)
        be_px = entry * (1.0 + risk.be_buffer_pct)
    else:
        sl = entry * (1.0 + sl_pct)
        tp = entry * (1.0 - tp_pct)
        be_px = entry * (1.0 - risk.be_buffer_pct)

    # Vol-aware BE trigger (approx using atr at entry)
    atr_p = store.atr_pct(spec.symbol, bar0.ts)
    vol_trig = risk.be_vol_atr_factor * atr_p / sl_pct
    be_trigger = min(risk.be_trigger_r, vol_trig)
    r_dist = abs(entry - sl)
    armed = False
    deadline = bar0.ts + risk.max_hold_ms
    use_be = bool(risk.use_sl_to_be)

    for j in range(i0 + 1, len(bars)):
        b = bars[j]
        if b.ts > deadline:
            fill, pnl = _pnl(
                spec.side, entry, b.c, spec.size, risk.fee_pct,
                be=False, paper_slip=risk.paper_slip_pct, slip_bps=risk.slippage_bps,
            )
            return SimTrade(
                spec.symbol, spec.side, bar0.ts, b.ts, entry, fill, spec.size, pnl, "max_hold",
            )

        # Adverse-first path (P2): long O→L→H→C ; short O→H→L→C
        if spec.side == "long":
            path = [b.o, b.l, b.h, b.c]
        else:
            path = [b.o, b.h, b.l, b.c]
        # dedupe
        seq: List[float] = []
        for px in path:
            if px > 0 and (not seq or abs(seq[-1] - px) > 1e-12):
                seq.append(px)

        for px in seq:
            # arm BE
            if use_be and not armed and r_dist > 0:
                fav = (px - entry) / r_dist if spec.side == "long" else (entry - px) / r_dist
                if fav >= be_trigger:
                    armed = True
            if use_be and armed:
                hit_be = (px <= be_px) if spec.side == "long" else (px >= be_px)
                if hit_be:
                    fill, pnl = _pnl(
                        spec.side, entry, be_px, spec.size, risk.fee_pct,
                        be=True, paper_slip=risk.paper_slip_pct, slip_bps=risk.slippage_bps,
                    )
                    return SimTrade(
                        spec.symbol, spec.side, bar0.ts, b.ts, entry, fill, spec.size, pnl,
                        "sl_to_be",
                    )

        # Hard SL/TP after path (pessimistic: SL wins if both)
        sl_hit = (b.l <= sl) if spec.side == "long" else (b.h >= sl)
        tp_hit = (b.h >= tp) if spec.side == "long" else (b.l <= tp)
        if sl_hit and tp_hit:
            fill, pnl = _pnl(
                spec.side, entry, sl, spec.size, risk.fee_pct,
                be=False, paper_slip=risk.paper_slip_pct, slip_bps=risk.slippage_bps,
            )
            return SimTrade(
                spec.symbol, spec.side, bar0.ts, b.ts, entry, fill, spec.size, pnl, "stop_loss",
            )
        if sl_hit:
            fill, pnl = _pnl(
                spec.side, entry, sl, spec.size, risk.fee_pct,
                be=False, paper_slip=risk.paper_slip_pct, slip_bps=risk.slippage_bps,
            )
            return SimTrade(
                spec.symbol, spec.side, bar0.ts, b.ts, entry, fill, spec.size, pnl, "stop_loss",
            )
        if tp_hit:
            fill, pnl = _pnl(
                spec.side, entry, tp, spec.size, risk.fee_pct,
                be=False, paper_slip=risk.paper_slip_pct, slip_bps=risk.slippage_bps,
            )
            return SimTrade(
                spec.symbol, spec.side, bar0.ts, b.ts, entry, fill, spec.size, pnl, "take_profit",
            )

    # EOD: last bar close for this symbol
    last = bars[-1]
    fill, pnl = _pnl(
        spec.side, entry, last.c, spec.size, risk.fee_pct,
        be=False, paper_slip=risk.paper_slip_pct, slip_bps=risk.slippage_bps,
    )
    return SimTrade(
        spec.symbol, spec.side, bar0.ts, last.ts, entry, fill, spec.size, pnl, "force_close_eod",
    )


def simulate_schedule(
    store: CandleStore,
    specs: List[EntrySpec],
    risk: RiskParams,
    *,
    daily_stop_limit: int = 4,
) -> List[SimTrade]:
    """Chronological schedule with max_positions, 1-per-symbol, daily stop streak."""
    ordered = sorted(specs, key=lambda s: (s.entry_time_ms, s.symbol))
    trades: List[SimTrade] = []
    busy_until: Dict[str, int] = {}
    daily_stops: Dict[str, int] = defaultdict(int)

    for spec in ordered:
        if busy_until.get(spec.symbol, 0) > spec.entry_time_ms:
            continue
        day = utc_day(spec.entry_time_ms)
        if daily_stop_limit > 0 and daily_stops[day] >= daily_stop_limit:
            continue
        active_now = sum(1 for u in busy_until.values() if u > spec.entry_time_ms)
        if active_now >= risk.max_positions:
            continue
        tr = simulate_one(store, spec, risk)
        if tr is None:
            continue
        trades.append(tr)
        busy_until[spec.symbol] = tr.exit_time
        if tr.exit_reason == "stop_loss":
            daily_stops[utc_day(tr.exit_time)] += 1
    return trades


def enrich_entries_atr(
    entries: List[EntrySpec],
    store: CandleStore,
    risk: RiskParams,
) -> List[EntrySpec]:
    """Recompute SL/TP pct from ATR at entry (engine risk_usd recovery is lossy)."""
    out: List[EntrySpec] = []
    for e in entries:
        atr_p = store.atr_pct(e.symbol, e.entry_time_ms)
        sl = risk.stop_atr_mult * atr_p
        tp = risk.tp_atr_mult * atr_p
        out.append(
            EntrySpec(
                entry_time_ms=e.entry_time_ms,
                symbol=e.symbol,
                side=e.side,
                stop_loss_pct=sl,
                take_profit_pct=tp,
                size=e.size,
                entry_price_hint=e.entry_price_hint,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Reference CM run (full engine, once)
# ---------------------------------------------------------------------------


def run_strategy_reference(
    cfg: Any,
    db: Database,
    symbols: List[str],
    start_ms: int,
    end_ms: int,
    strategy_name: str,
) -> Dict[str, Any]:
    from src.strategies.factory import _REGISTRY_BY_NAME, _instantiate_from_registry

    entry = _REGISTRY_BY_NAME.get(strategy_name)
    if entry is None:
        raise ValueError(f"Unknown strategy: {strategy_name}")
    path, cls = entry
    strat = _instantiate_from_registry(cfg, path, cls, force=True, shadow=False)
    if strat is None:
        raise RuntimeError(f"Failed to instantiate {strategy_name}")
    bt = build_backtest_config_from_yaml(cfg)
    bt.use_volatility_circuit = False
    bt.use_funding_blackout = False
    bt.max_daily_trades = 0
    bt.use_microstructure_proxy = True
    bt.exit_path_policy = "adverse_first"
    eng = BacktestEngine(
        database=db,
        strategy=DirectStrategyRouter([strat]),
        config=bt,
        symbols=symbols,
        risk_config=cfg,
    )
    result = eng.run(start_ms=start_ms, end_ms=end_ms)
    trades = [
        t
        for t in (result.get("trades") or [])
        if str(t.get("strategy") or "") == strategy_name
    ]
    entries: List[EntrySpec] = []
    for t in trades:
        ep = float(t["entry_price"])
        size = float(t.get("size") or 0.0)
        risk_usd = float(t.get("risk_usd") or 0.0)
        sl_pct = safe_divide(risk_usd, ep * size, 0.01) if size > 0 else 0.01
        tp_pct = sl_pct * 2.0
        entries.append(
            EntrySpec(
                entry_time_ms=int(t["entry_time"]),
                symbol=str(t["symbol"]),
                side=str(t["side"]),
                stop_loss_pct=sl_pct,
                take_profit_pct=tp_pct,
                size=size,
                entry_price_hint=ep,
            )
        )
    pnls = [float(t.get("pnl_usd") or 0.0) for t in trades]
    return {
        "trades": trades,
        "entries": entries,
        "metrics": trade_metrics(pnls),
        "pnls": pnls,
    }


# Back-compat alias
def run_checklist_meta(
    cfg: Any,
    db: Database,
    symbols: List[str],
    start_ms: int,
    end_ms: int,
) -> Dict[str, Any]:
    return run_strategy_reference(cfg, db, symbols, start_ms, end_ms, "ChecklistMeta")


def validate_fast_vs_engine(
    store: CandleStore,
    entries: List[EntrySpec],
    risk: RiskParams,
    engine_pnls: List[float],
) -> Dict[str, Any]:
    """Sanity: fast sim on real schedule should roughly track engine PnL."""
    sim = simulate_schedule(store, entries, risk)
    sp = [t.pnl_usd for t in sim]
    return {
        "engine_n": len(engine_pnls),
        "fast_n": len(sp),
        "engine_pnl": round(sum(engine_pnls), 2),
        "fast_pnl": round(sum(sp), 2),
        "engine_metrics": trade_metrics(engine_pnls),
        "fast_metrics": trade_metrics(sp),
    }


# ---------------------------------------------------------------------------
# Baseline schedule builders
# ---------------------------------------------------------------------------


def build_b1(entries: List[EntrySpec], rng: random.Random) -> List[EntrySpec]:
    out: List[EntrySpec] = []
    for e in entries:
        side = rng.choice(["long", "short"])
        out.append(
            EntrySpec(
                entry_time_ms=e.entry_time_ms,
                symbol=e.symbol,
                side=side,
                stop_loss_pct=e.stop_loss_pct,
                take_profit_pct=e.take_profit_pct,
                size=e.size,
            )
        )
    return out


def _eligible_times(
    store: CandleStore,
    symbol: str,
    start_ms: int,
    end_ms: int,
    step: int = 15,
) -> List[int]:
    bars = store.bars_1m.get(symbol) or []
    # sample every `step` minutes to keep pool manageable
    return [b.ts for i, b in enumerate(bars) if start_ms <= b.ts <= end_ms and i % step == 0]


def build_b2(
    entries: List[EntrySpec],
    store: CandleStore,
    risk: RiskParams,
    start_ms: int,
    end_ms: int,
    rng: random.Random,
) -> List[EntrySpec]:
    pools = {
        sym: _eligible_times(store, sym, start_ms, end_ms)
        for sym in {e.symbol for e in entries}
    }
    out: List[EntrySpec] = []
    for e in entries:
        pool = pools.get(e.symbol) or []
        if not pool:
            continue
        ts = rng.choice(pool)
        atr_p = store.atr_pct(e.symbol, ts)
        sl = risk.stop_atr_mult * atr_p
        tp = risk.tp_atr_mult * atr_p
        out.append(
            EntrySpec(
                entry_time_ms=ts,
                symbol=e.symbol,
                side=e.side,
                stop_loss_pct=sl,
                take_profit_pct=tp,
                size=e.size,
            )
        )
    return out


def build_b3(
    entries: List[EntrySpec],
    store: CandleStore,
    risk: RiskParams,
    start_ms: int,
    end_ms: int,
    rng: random.Random,
) -> List[EntrySpec]:
    # Match counts per (day, symbol); random side + time that day
    counts: Dict[Tuple[str, str], int] = defaultdict(int)
    sizes: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    for e in entries:
        key = (utc_day(e.entry_time_ms), e.symbol)
        counts[key] += 1
        sizes[key].append(e.size)

    out: List[EntrySpec] = []
    for (day, sym), n in counts.items():
        day_start = ms_from_date(day)
        day_end = ms_from_date(day, end=True)
        pool = _eligible_times(store, sym, max(day_start, start_ms), min(day_end, end_ms), step=5)
        if not pool:
            pool = _eligible_times(store, sym, start_ms, end_ms, step=15)
        if not pool:
            continue
        size_pool = sizes[(day, sym)]
        for _ in range(n):
            ts = rng.choice(pool)
            side = rng.choice(["long", "short"])
            atr_p = store.atr_pct(sym, ts)
            sl = risk.stop_atr_mult * atr_p
            tp = risk.tp_atr_mult * atr_p
            sz = rng.choice(size_pool) if size_pool else risk.base_size_pct * risk.initial_capital / 100.0
            out.append(
                EntrySpec(
                    entry_time_ms=ts,
                    symbol=sym,
                    side=side,
                    stop_loss_pct=sl,
                    take_profit_pct=tp,
                    size=sz,
                )
            )
    return out


def always_side(entries: List[EntrySpec], side: str) -> List[EntrySpec]:
    return [
        EntrySpec(
            entry_time_ms=e.entry_time_ms,
            symbol=e.symbol,
            side=side,
            stop_loss_pct=e.stop_loss_pct,
            take_profit_pct=e.take_profit_pct,
            size=e.size,
        )
        for e in entries
    ]


def buy_and_hold(
    store: CandleStore,
    symbols: List[str],
    start_ms: int,
    end_ms: int,
    risk: RiskParams,
) -> Dict[str, Any]:
    """Equal-weight long from first bar ≥ start to last bar ≤ end; RT fees."""
    alloc = risk.initial_capital / max(len(symbols), 1)
    pnls: List[float] = []
    legs: List[Dict[str, Any]] = []
    for sym in symbols:
        bars = [b for b in (store.bars_1m.get(sym) or []) if start_ms <= b.ts <= end_ms]
        if len(bars) < 2:
            continue
        entry_raw = bars[0].c
        exit_raw = bars[-1].c
        entry = _apply_entry_slip(entry_raw, "long", risk.slippage_bps)
        size = alloc / entry if entry > 0 else 0.0
        fill, pnl = _pnl(
            "long", entry, exit_raw, size, risk.fee_pct,
            be=False, paper_slip=risk.paper_slip_pct, slip_bps=risk.slippage_bps,
        )
        pnls.append(pnl)
        legs.append(
            {
                "symbol": sym,
                "entry": entry,
                "exit": fill,
                "pnl": round(pnl, 4),
                "ret_pct": round((fill / entry - 1.0) * 100, 4) if entry else 0.0,
            }
        )
    return {"legs": legs, "metrics": trade_metrics(pnls), "pnls": pnls}


# ---------------------------------------------------------------------------
# Monte Carlo loops
# ---------------------------------------------------------------------------


def run_baseline_seeds(
    name: str,
    builder,
    store: CandleStore,
    risk: RiskParams,
    n_seeds: int,
    base_seed: int,
) -> Dict[str, Any]:
    rows: List[Dict[str, float]] = []
    t0 = time.time()
    for i in range(n_seeds):
        rng = random.Random(base_seed + i * 9973)
        specs = builder(rng)
        trades = simulate_schedule(store, specs, risk)
        m = trade_metrics([t.pnl_usd for t in trades])
        rows.append(m)
        if (i + 1) % 50 == 0:
            print(f"    {name}: {i+1}/{n_seeds} seeds", flush=True)
    elapsed = time.time() - t0
    by_metric = {
        k: dist_summary([r[k] for r in rows])
        for k in ("expectancy", "profit_factor", "sharpe", "total_pnl", "win_rate", "rr")
    }
    return {
        "n_seeds": n_seeds,
        "elapsed_s": round(elapsed, 1),
        "distributions": by_metric,
        "seeds": rows,  # full detail for percentile
    }


def situate(real: Dict[str, float], baseline: Dict[str, Any]) -> Dict[str, Any]:
    seeds = baseline.get("seeds") or []
    out: Dict[str, Any] = {}
    for key, higher in (
        ("expectancy", True),
        ("profit_factor", True),
        ("sharpe", True),
        ("total_pnl", True),
        ("win_rate", True),
        ("rr", True),
    ):
        samples = [float(s[key]) for s in seeds]
        out[key] = {
            "real": real.get(key),
            "percentile": percentile_rank(float(real.get(key) or 0.0), samples, higher_better=higher),
            "above_p50": float(real.get(key) or 0.0) >= float(baseline["distributions"][key]["p50"]),
            "above_p95": float(real.get(key) or 0.0) >= float(baseline["distributions"][key]["p95"]),
            "dist": baseline["distributions"][key],
        }
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run_fold(
    fold_key: str,
    cfg: Any,
    db: Database,
    symbols: List[str],
    risk: RiskParams,
    n_seeds: int,
) -> Dict[str, Any]:
    label, start, end = FOLDS[fold_key]
    start_ms, end_ms = ms_from_date(start), ms_from_date(end, end=True)
    print(f"\n======== {label} ========", flush=True)

    print("  [1] ChecklistMeta reference (full engine)...", flush=True)
    t0 = time.time()
    ref = run_checklist_meta(cfg, db, symbols, start_ms, end_ms)
    print(
        f"      n={ref['metrics']['n_trades']} WR={ref['metrics']['win_rate']} "
        f"PF={ref['metrics']['profit_factor']} PnL={ref['metrics']['total_pnl']} "
        f"({time.time()-t0:.1f}s)",
        flush=True,
    )

    print("  [2] Loading candles for fast sim...", flush=True)
    store = CandleStore()
    store.load(db, symbols, start_ms, end_ms)

    entries = enrich_entries_atr(ref["entries"], store, risk)

    sanity = validate_fast_vs_engine(store, entries, risk, ref["pnls"])
    print(
        f"      fast-sim sanity: engine_pnl={sanity['engine_pnl']} fast_pnl={sanity['fast_pnl']} "
        f"n={sanity['engine_n']}/{sanity['fast_n']}",
        flush=True,
    )

    real_m = ref["metrics"]

    print(f"  [3] B1 random direction ({n_seeds} seeds)...", flush=True)
    b1 = run_baseline_seeds(
        "B1",
        lambda rng: build_b1(entries, rng),
        store,
        risk,
        n_seeds,
        base_seed=1000,
    )
    print(f"  [4] B2 random timing ({n_seeds} seeds)...", flush=True)
    b2 = run_baseline_seeds(
        "B2",
        lambda rng: build_b2(entries, store, risk, start_ms, end_ms, rng),
        store,
        risk,
        n_seeds,
        base_seed=2000,
    )
    print(f"  [5] B3 random both ({n_seeds} seeds)...", flush=True)
    b3 = run_baseline_seeds(
        "B3",
        lambda rng: build_b3(entries, store, risk, start_ms, end_ms, rng),
        store,
        risk,
        n_seeds,
        base_seed=3000,
    )

    print("  [6] Passiveive baselines...", flush=True)
    bh = buy_and_hold(store, symbols, start_ms, end_ms, risk)
    al_trades = simulate_schedule(store, always_side(entries, "long"), risk)
    as_trades = simulate_schedule(store, always_side(entries, "short"), risk)
    always_long = trade_metrics([t.pnl_usd for t in al_trades])
    always_short = trade_metrics([t.pnl_usd for t in as_trades])

    # For fair comparison of CM vs B1/B2/B3, situate engine metrics AND fast-sim
    # replay of the real schedule (same simulator as baselines).
    fast_real_trades = simulate_schedule(store, entries, risk)
    fast_real_m = trade_metrics([t.pnl_usd for t in fast_real_trades])

    return {
        "fold": label,
        "window": {"start": start, "end": end},
        "symbols": symbols,
        "n_seeds": n_seeds,
        "checklist_meta_engine": real_m,
        "checklist_meta_fast_sim": fast_real_m,
        "fast_sim_sanity": sanity,
        "baselines": {
            "B1_random_direction": {
                **{k: v for k, v in b1.items() if k != "seeds"},
                "vs_real_fast": situate(fast_real_m, b1),
                "vs_real_engine": situate(real_m, b1),
            },
            "B2_random_timing": {
                **{k: v for k, v in b2.items() if k != "seeds"},
                "vs_real_fast": situate(fast_real_m, b2),
                "vs_real_engine": situate(real_m, b2),
            },
            "B3_random_both": {
                **{k: v for k, v in b3.items() if k != "seeds"},
                "vs_real_fast": situate(fast_real_m, b3),
                "vs_real_engine": situate(real_m, b3),
            },
        },
        # Keep seed arrays separately for optional deep dive (trimmed in report file)
        "_seed_arrays": {
            "B1": b1["seeds"],
            "B2": b2["seeds"],
            "B3": b3["seeds"],
        },
        "passive": {
            "buy_and_hold_equal_weight": bh,
            "always_long_at_cm_times": always_long,
            "always_short_at_cm_times": always_short,
        },
    }


def interpret(fold_result: Dict[str, Any]) -> Dict[str, Any]:
    real = fold_result["checklist_meta_fast_sim"]
    engine = fold_result["checklist_meta_engine"]
    lines: List[str] = []
    decomp: Dict[str, Any] = {}

    for bname, key in (
        ("B1", "B1_random_direction"),
        ("B2", "B2_random_timing"),
        ("B3", "B3_random_both"),
    ):
        vs = fold_result["baselines"][key]["vs_real_fast"]
        pf_p = vs["profit_factor"]["percentile"]
        exp_p = vs["expectancy"]["percentile"]
        above50 = vs["profit_factor"]["above_p50"]
        above95 = vs["profit_factor"]["above_p95"]
        lines.append(
            f"{bname}: CM fast-sim PF percentile={pf_p} (above_p50={above50}, above_p95={above95}); "
            f"expectancy percentile={exp_p}"
        )
        decomp[bname] = {
            "pf_percentile": pf_p,
            "expectancy_percentile": exp_p,
            "above_p50": above50,
            "above_p95": above95,
        }

    b1_ok = decomp["B1"]["above_p50"]
    b2_ok = decomp["B2"]["above_p50"]
    # B1 = random direction @ real times → beating B1 ⇒ direction edge
    # B2 = real direction @ random times → beating B2 ⇒ timing edge
    if b1_ok and not b2_ok:
        edge = "direction_only"
        edge_note = (
            "Beats B1 (random direction, real timing) but not B2 (real direction, random timing): "
            "edge is in DIRECTION at the chosen times."
        )
    elif b2_ok and not b1_ok:
        edge = "timing_only"
        edge_note = (
            "Beats B2 (real direction, random timing) but not B1 (random direction, real timing): "
            "edge is in TIMING of entries, not side selection."
        )
    elif b1_ok and b2_ok:
        edge = "direction_and_timing"
        edge_note = "Above median on both B1 and B2 — signal likely has both direction and timing content."
    else:
        edge = "none_detectable"
        edge_note = (
            "Inside random distribution on B1 and B2 — no demonstrable signal edge "
            "under this risk template; gate tuning risks polishing noise."
        )

    # Passiveive
    bh = fold_result["passive"]["buy_and_hold_equal_weight"]["metrics"]
    al = fold_result["passive"]["always_long_at_cm_times"]
    ash = fold_result["passive"]["always_short_at_cm_times"]

    return {
        "engine_metrics": engine,
        "fast_sim_metrics": real,
        "baseline_percentiles": decomp,
        "edge_decomposition": edge,
        "edge_note": edge_note,
        "passive_comparison": {
            "cm_fast_pnl": real.get("total_pnl"),
            "buy_hold_pnl": bh.get("total_pnl"),
            "always_long_pnl": al.get("total_pnl"),
            "always_short_pnl": ash.get("total_pnl"),
            "cm_beats_buy_hold": float(real.get("total_pnl") or 0) > float(bh.get("total_pnl") or 0),
            "cm_beats_always_long": float(real.get("total_pnl") or 0) > float(al.get("total_pnl") or 0),
            "cm_beats_always_short": float(real.get("total_pnl") or 0) > float(ash.get("total_pnl") or 0),
        },
        "summary_lines": lines,
        "limitations": {
            "tier_b_oir": (
                "ChecklistMeta live/replay uses Tier-B OIR proxy (candle-derived); "
                "random baselines do NOT use OIR at all. The test is therefore "
                "conservative against CM: any OIR degradation hurts only the real strategy. "
                "Prior Tier-B measurement showed M1≈M2≈M3 (WR 15–16%), suggesting OIR is not "
                "the main WR gap vs live — residual impact on this significance test is likely "
                "small vs direction/timing noise, on the order of a few WR points at most."
            ),
            "fast_sim": (
                "Baselines use a fast path simulator (same SL/TP/BE/max-hold/fees/max-positions) "
                "rather than 200× full BacktestEngine runs. Percentiles are vs fast-sim CM on the "
                "same schedule; engine CM metrics are reported alongside for reference. "
                f"Sanity delta engine vs fast: see fast_sim_sanity."
            ),
            "vwap_excluded": "Phase08 VWAPDeviation disabled — CM-only, as requested for signal test.",
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seeds", type=int, default=200)
    ap.add_argument("--folds", type=str, default="W2,W3")
    args = ap.parse_args()

    cfg = load_config(ROOT / "config" / "settings.yaml")
    snap = ROOT / "data" / "live" / "bot_ruleset_validate.db"
    db = Database(str(snap if snap.exists() else ROOT / "data" / "live" / "bot.db"))
    symbols = list(cfg.get("assets") or ["BTC", "ETH", "SOL", "HYPE"])
    # HYPE listed mid-window — keep if data present
    risk = risk_from_cfg(cfg)
    # Normalise fee: settings has taker_fee_pct: 0.035 meaning percent
    raw_fee = float(cfg.get("risk.taker_fee_pct", 0.035))
    risk.fee_pct = raw_fee / 100.0 if raw_fee > 0.001 else raw_fee
    risk.paper_slip_pct = float(cfg.get("risk.paper_slippage_pct", 0.02)) / 100.0

    fold_keys = [f.strip() for f in args.folds.split(",") if f.strip() in FOLDS]
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    payload: Dict[str, Any] = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "seeds": args.seeds,
        "risk_params": risk.__dict__,
        "folds": {},
        "interpretation": {},
    }

    for fk in fold_keys:
        fr = run_fold(fk, cfg, db, symbols, risk, args.seeds)
        interp = interpret(fr)
        # drop bulky seed arrays from main JSON (save compact)
        seeds = fr.pop("_seed_arrays", {})
        payload["folds"][fk] = fr
        payload["interpretation"][fk] = interp

        seed_path = OUT_DIR / f"baseline_signal_seeds_{fk}_{args.seeds}.json"
        seed_path.write_text(json.dumps(seeds), encoding="utf-8")
        print(f"  Wrote seeds {seed_path}", flush=True)

        print("\n  --- Interpretation ---", flush=True)
        for line in interp["summary_lines"]:
            print(f"  {line}", flush=True)
        print(f"  edge={interp['edge_decomposition']}: {interp['edge_note']}", flush=True)
        pc = interp["passive_comparison"]
        print(
            f"  passive: CM={pc['cm_fast_pnl']} BH={pc['buy_hold_pnl']} "
            f"AL={pc['always_long_pnl']} AS={pc['always_short_pnl']}",
            flush=True,
        )

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = OUT_DIR / f"baseline_signal_test_{ts}.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    # also stable name
    stable = OUT_DIR / "baseline_signal_test_latest.json"
    stable.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # Markdown report
    md = OUT_DIR / "BASELINE_SIGNAL_TEST_REPORT.md"
    md.write_text(_render_md(payload), encoding="utf-8")
    print(f"\nWrote {out}\nWrote {md}", flush=True)
    return 0


def _render_md(payload: Dict[str, Any]) -> str:
    lines = [
        "# ChecklistMeta signal significance vs random baselines",
        "",
        f"Created: {payload.get('created_utc')}",
        f"Seeds per baseline: {payload.get('seeds')}",
        "",
        "## Method",
        "",
        "- **Real**: ChecklistMeta-only full engine + fast-sim replay of the same entry schedule.",
        "- **B1**: same entry times/sizes/SL-TP widths, random long/short.",
        "- **B2**: same sides/sizes, random entry times; SL/TP from ATR at new time.",
        "- **B3**: matched (day×symbol) trade counts; random side + time.",
        "- **Passive**: equal-weight buy&hold; always-long / always-short at CM times.",
        "- Exit template: hard SL/TP (pessimistic), SL-to-BE (vol-aware trigger + buffer), "
        "max-hold, fees, paper slip on BE — aligned with current replay parity fixes.",
        "",
        "## Results by fold",
        "",
    ]
    for fk, fr in (payload.get("folds") or {}).items():
        interp = (payload.get("interpretation") or {}).get(fk) or {}
        lines.append(f"### {fr.get('fold')}")
        lines.append("")
        eng = fr.get("checklist_meta_engine") or {}
        fast = fr.get("checklist_meta_fast_sim") or {}
        lines.append(
            f"- Engine CM: n={eng.get('n_trades')} WR={eng.get('win_rate')} "
            f"PF={eng.get('profit_factor')} exp={eng.get('expectancy')} PnL={eng.get('total_pnl')}"
        )
        lines.append(
            f"- Fast-sim CM (comparable to baselines): n={fast.get('n_trades')} "
            f"WR={fast.get('win_rate')} PF={fast.get('profit_factor')} "
            f"exp={fast.get('expectancy')} PnL={fast.get('total_pnl')}"
        )
        sanity = fr.get("fast_sim_sanity") or {}
        lines.append(
            f"- Fast-sim sanity: engine_pnl={sanity.get('engine_pnl')} "
            f"fast_pnl={sanity.get('fast_pnl')}"
        )
        lines.append("")
        lines.append("| Baseline | PF p50 | CM PF %ile | above p50? | above p95? | exp %ile |")
        lines.append("|----------|-------:|----------:|:----------:|:----------:|---------:|")
        for bname, bkey in (
            ("B1 dir", "B1_random_direction"),
            ("B2 time", "B2_random_timing"),
            ("B3 both", "B3_random_both"),
        ):
            vs = fr["baselines"][bkey]["vs_real_fast"]["profit_factor"]
            exp = fr["baselines"][bkey]["vs_real_fast"]["expectancy"]
            lines.append(
                f"| {bname} | {vs['dist']['p50']} | {vs['percentile']} | "
                f"{vs['above_p50']} | {vs['above_p95']} | {exp['percentile']} |"
            )
        lines.append("")
        pc = interp.get("passive_comparison") or {}
        lines.append(
            f"- Passiveive PnL: CM={pc.get('cm_fast_pnl')} BH={pc.get('buy_hold_pnl')} "
            f"alwaysL={pc.get('always_long_pnl')} alwaysS={pc.get('always_short_pnl')}"
        )
        lines.append(f"- **Edge decomposition**: `{interp.get('edge_decomposition')}`")
        lines.append(f"- {interp.get('edge_note')}")
        lines.append("")

    lines.extend(
        [
            "## Explicit answers",
            "",
        ]
    )
    for fk, interp in (payload.get("interpretation") or {}).items():
        lines.append(f"### {fk}")
        decomp = interp.get("baseline_percentiles") or {}
        for b in ("B1", "B2", "B3"):
            d = decomp.get(b) or {}
            lines.append(
                f"- {b}: above p50 PF? **{d.get('above_p50')}**; "
                f"above p95? **{d.get('above_p95')}**; "
                f"PF percentile={d.get('pf_percentile')}"
            )
        lines.append(f"- Verdict: **{interp.get('edge_decomposition')}** — {interp.get('edge_note')}")
        lines.append("")

    # limitations from first fold
    lim = None
    for interp in (payload.get("interpretation") or {}).values():
        lim = interp.get("limitations")
        break
    if lim:
        lines.append("## Limitations")
        lines.append("")
        lines.append(f"- Tier B OIR: {lim.get('tier_b_oir')}")
        lines.append(f"- Fast sim: {lim.get('fast_sim')}")
        lines.append(f"- Scope: {lim.get('vwap_excluded')}")
        lines.append("")

    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
