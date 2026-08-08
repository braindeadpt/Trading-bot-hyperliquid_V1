"""Comparative backtest: VWAP fade (rolling) vs VWAP trend (anchored).

Compares on available crypto history (default 2026-05-18 -> today):
  (a) VWAPDeviation — mean-reversion / fade, rolling 24h VWAP
  (b) VWAPTrend     — trend-follow, UTC-day anchored VWAP
  (c) VWAPTrend + hour-of-day filters (UTC blocks, US/Asia analogues)

Uses a **lightweight 15m (or 5m) bar replay** that drives the real strategy
classes. Full 1m BacktestEngine is available via ``--engine full`` but is
~10-20 min/symbol on this window — too slow for a 7-variant grid.

Reports per variant x symbol (+ aggregate): n_trades, win_rate,
avg_win/avg_loss, expectancy, profit_factor, Sharpe, maxDD, and
**gross vs net PnL with total fees**.

Does NOT modify production config. VWAPTrend is force-enabled only here.

Usage:
    python scripts/backtest_vwap_trend_vs_fade.py
    python scripts/backtest_vwap_trend_vs_fade.py --symbols BTC,ETH --end 2026-08-07
    python scripts/backtest_vwap_trend_vs_fade.py --engine full   # slow fidelity
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.backtest.engine import BacktestConfig, BacktestEngine
from src.data.database import Database
from src.strategies.base import MarketEvent, Position
from src.strategies.indicators import Candle
from src.strategies.vwap_deviation import VWAPDeviation
from src.strategies.vwap_trend import VWAPTrend
from src.utils.config import load_config

logging.basicConfig(level=logging.ERROR)
for _n in (
    "src.core.volatility_circuit",
    "src.backtest.engine",
    "src.strategies",
    "src.core.risk_manager",
    "src.core.funding_blackout",
):
    logging.getLogger(_n).setLevel(logging.ERROR)

DEFAULT_START = "2026-05-18"
SYMBOLS_DEFAULT = ["BTC", "ETH", "SOL", "HYPE"]


@dataclass
class Variant:
    key: str
    label: str
    kind: str  # "fade" | "trend"
    overrides: Dict[str, Any] = field(default_factory=dict)
    confirm_tf: str = "15m"  # bar tf used by light replay


VARIANTS: List[Variant] = [
    Variant("fade_baseline", "(a) VWAPDeviation fade / rolling 24h", "fade", {}),
    Variant(
        "trend_baseline",
        "(b) VWAPTrend / anchored UTC-day",
        "trend",
        {
            "enabled": True,
            "vwap_confirm_tf": "15m",
            "min_flip_interval_minutes": 30,
            "vwap_cross_buffer_pct": 0.001,
            "max_hold_hours": 12,
            "close_on_utc_rollover": True,
            "use_session_filter": False,
        },
    ),
    Variant(
        "trend_5m",
        "(b2) VWAPTrend confirm=5m",
        "trend",
        {
            "enabled": True,
            "vwap_confirm_tf": "5m",
            "min_flip_interval_minutes": 30,
            "vwap_cross_buffer_pct": 0.001,
            "max_hold_hours": 12,
            "close_on_utc_rollover": True,
        },
        confirm_tf="5m",
    ),
    Variant(
        "trend_us_paper_hours",
        "(c) VWAPTrend US paper hours (13-16,19 UTC)",
        "trend",
        {
            "enabled": True,
            "vwap_confirm_tf": "15m",
            "min_flip_interval_minutes": 30,
            "vwap_cross_buffer_pct": 0.001,
            "max_hold_hours": 12,
            "close_on_utc_rollover": True,
            "use_session_filter": True,
            "session_hours_utc": [13, 14, 15, 19],
        },
    ),
    Variant(
        "trend_us_rth",
        "(c) VWAPTrend US RTH (13-20 UTC)",
        "trend",
        {
            "enabled": True,
            "vwap_confirm_tf": "15m",
            "min_flip_interval_minutes": 30,
            "vwap_cross_buffer_pct": 0.001,
            "max_hold_hours": 12,
            "close_on_utc_rollover": True,
            "use_session_filter": True,
            "session_start_utc_h": 13,
            "session_end_utc_h": 20,
        },
    ),
    Variant(
        "trend_asia",
        "(c) VWAPTrend Asia (00-08 UTC)",
        "trend",
        {
            "enabled": True,
            "vwap_confirm_tf": "15m",
            "min_flip_interval_minutes": 30,
            "vwap_cross_buffer_pct": 0.001,
            "max_hold_hours": 12,
            "close_on_utc_rollover": True,
            "use_session_filter": True,
            "session_start_utc_h": 0,
            "session_end_utc_h": 8,
        },
    ),
    Variant(
        "trend_eu",
        "(c) VWAPTrend EU (07-16 UTC)",
        "trend",
        {
            "enabled": True,
            "vwap_confirm_tf": "15m",
            "min_flip_interval_minutes": 30,
            "vwap_cross_buffer_pct": 0.001,
            "max_hold_hours": 12,
            "close_on_utc_rollover": True,
            "use_session_filter": True,
            "session_start_utc_h": 7,
            "session_end_utc_h": 16,
        },
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ms_from_date(s: str, end: bool = False) -> int:
    dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp() * 1000)


def today_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def db_candle_to_ind(c: Any) -> Candle:
    return Candle(
        open=float(c.open),
        high=float(c.high),
        low=float(c.low),
        close=float(c.close),
        volume=float(c.volume),
        timestamp_ms=int(c.timestamp_ms),
        open_interest=getattr(c, "open_interest", None),
    )


def sharpe_from_equity(equity: List[Tuple[int, float]]) -> float:
    if len(equity) < 3:
        return 0.0
    rets: List[float] = []
    for i in range(1, len(equity)):
        prev = equity[i - 1][1]
        cur = equity[i][1]
        if prev > 0:
            rets.append((cur - prev) / prev)
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    std = math.sqrt(var) if var > 0 else 0.0
    if std == 0.0:
        return 0.0
    # 15m bars -> 96 bars/day * 365
    return (mean / std) * math.sqrt(96 * 365)


def max_dd_pct(equity: List[Tuple[int, float]]) -> float:
    peak = equity[0][1] if equity else 0.0
    max_dd = 0.0
    for _, cap in equity:
        if cap > peak:
            peak = cap
        if peak > 0:
            dd = (peak - cap) / peak
            if dd > max_dd:
                max_dd = dd
    return max_dd * 100.0


def summarize_trades(
    trades: List[Dict[str, Any]],
    equity: List[Tuple[int, float]],
) -> Dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {
            "n_trades": 0,
            "win_rate": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "expectancy": 0.0,
            "profit_factor": 0.0,
            "sharpe": 0.0,
            "max_dd_pct": 0.0,
            "net_pnl": 0.0,
            "gross_pnl": 0.0,
            "fees_total": 0.0,
            "fee_drag_pct_of_gross": 0.0,
        }

    pnls = [float(t.get("pnl_usd", 0.0)) for t in trades]
    fees = [float(t.get("fees_paid", 0.0)) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    net = sum(pnls)
    fees_total = sum(fees)
    gross = net + fees_total

    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    wr = len(wins) / n
    expectancy = wr * avg_win + (1.0 - wr) * avg_loss
    gp = sum(wins)
    gl = abs(sum(losses))
    pf = gp / gl if gl > 0 else (999.0 if gp > 0 else 0.0)
    fee_drag = (fees_total / abs(gross) * 100.0) if abs(gross) > 1e-9 else 0.0

    return {
        "n_trades": n,
        "win_rate": round(wr * 100.0, 1),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "expectancy": round(expectancy, 2),
        "profit_factor": round(pf, 3) if pf != float("inf") else 999.0,
        "sharpe": round(sharpe_from_equity(equity), 3),
        "max_dd_pct": round(max_dd_pct(equity), 2),
        "net_pnl": round(net, 2),
        "gross_pnl": round(gross, 2),
        "fees_total": round(fees_total, 2),
        "fee_drag_pct_of_gross": round(fee_drag, 1),
    }


def build_strategy(variant: Variant, fade_section: Dict[str, Any]) -> Any:
    if variant.kind == "fade":
        section = dict(fade_section)
        section.update(variant.overrides)
        section["enabled"] = True
        # Light replay has no OIR — disable so fade is not silently muted
        section["require_oir_confirm"] = False
        return VWAPDeviation(section)
    section = dict(variant.overrides)
    section["enabled"] = True
    return VWAPTrend(section)


# ---------------------------------------------------------------------------
# Lightweight multi-symbol bar replay
# ---------------------------------------------------------------------------

@dataclass
class _OpenPos:
    symbol: str
    side: str
    entry_price: float
    size: float
    entry_time_ms: int
    stop_loss_price: Optional[float]
    strategy_name: str


def light_replay(
    db: Database,
    strategy: Any,
    symbols: List[str],
    start_ms: int,
    end_ms: int,
    *,
    bar_tf: str,
    initial_capital: float,
    commission_pct: float,
    slippage_bps: float,
) -> Dict[str, Any]:
    """Drive strategy on confirm-TF bars with simple fill / fee model.

    - Entry/exit at bar close +/- slippage
    - Fee = commission_pct/100 * notional per side
    - Hard SL checked on bar high/low when stop_loss_pct provided
    - One position per symbol
    """
    fee_rate = commission_pct / 100.0
    slip = slippage_bps / 10_000.0

    bars_by_sym: Dict[str, List[Candle]] = {}
    hours_by_sym: Dict[str, List[Candle]] = {}
    for sym in symbols:
        raw = db.get_candles(sym, bar_tf, limit=500_000, start_ms=start_ms, end_ms=end_ms)
        bars_by_sym[sym] = [db_candle_to_ind(c) for c in raw]
        raw_h = db.get_candles(sym, "1h", limit=500_000, start_ms=start_ms, end_ms=end_ms)
        hours_by_sym[sym] = [db_candle_to_ind(c) for c in raw_h]

    timeline: List[Tuple[int, str, Candle]] = []
    for sym, bars in bars_by_sym.items():
        for b in bars:
            timeline.append((b.timestamp_ms, sym, b))
    timeline.sort(key=lambda x: (x[0], x[1]))

    if not timeline:
        return {"trades": [], "equity_curve": [(start_ms, initial_capital)], "metrics": {}}

    capital = initial_capital
    equity: List[Tuple[int, float]] = [(timeline[0][0], capital)]
    trades: List[Dict[str, Any]] = []
    open_pos: Dict[str, _OpenPos] = {}
    hour_idx: Dict[str, int] = {s: 0 for s in symbols}
    last_1h: Dict[str, Optional[Candle]] = {s: None for s in symbols}

    def advance_1h(sym: str, ts: int) -> Optional[Candle]:
        hours = hours_by_sym[sym]
        i = hour_idx[sym]
        while i < len(hours) and hours[i].timestamp_ms <= ts:
            last_1h[sym] = hours[i]
            i += 1
        hour_idx[sym] = i
        return last_1h[sym]

    def mark_equity(ts: int) -> None:
        equity.append((ts, capital))

    def open_from_signal(sig: Any, bar: Candle, ts: int) -> None:
        nonlocal capital
        if sig.symbol in open_pos:
            return
        px = bar.close
        entry = px * (1.0 + slip) if sig.side == "long" else px * (1.0 - slip)
        size_pct = float(getattr(sig, "size_pct", 0.01) or 0.01)
        notional = capital * size_pct
        if entry <= 0 or notional <= 0:
            return
        size = notional / entry
        entry_fee = notional * fee_rate
        capital -= entry_fee
        sl_pct = float(getattr(sig, "stop_loss_pct", 0.0) or 0.0)
        if sig.side == "long":
            sl = entry * (1.0 - sl_pct) if sl_pct > 0 else None
        else:
            sl = entry * (1.0 + sl_pct) if sl_pct > 0 else None
        open_pos[sig.symbol] = _OpenPos(
            symbol=sig.symbol,
            side=sig.side,
            entry_price=entry,
            size=size,
            entry_time_ms=ts,
            stop_loss_price=sl,
            strategy_name=str(sig.strategy),
        )

    def settle(pos: _OpenPos, exit_px: float, ts: int, reason: str) -> None:
        nonlocal capital
        if pos.side == "long":
            fill = exit_px * (1.0 - slip)
            gross = (fill - pos.entry_price) * pos.size
        else:
            fill = exit_px * (1.0 + slip)
            gross = (pos.entry_price - fill) * pos.size
        exit_notional = fill * pos.size
        entry_notional = pos.entry_price * pos.size
        exit_fee = exit_notional * fee_rate
        entry_fee = entry_notional * fee_rate
        net = gross - exit_fee
        capital += net
        trades.append({
            "symbol": pos.symbol,
            "side": pos.side,
            "strategy": pos.strategy_name,
            "entry_price": pos.entry_price,
            "exit_price": fill,
            "entry_time": pos.entry_time_ms,
            "exit_time": ts,
            "size": pos.size,
            "pnl_usd": round(net, 4),
            "fees_paid": round(entry_fee + exit_fee, 4),
            "exit_reason": reason,
        })
        del open_pos[pos.symbol]

    for ts, sym, bar in timeline:
        c1h = advance_1h(sym, ts)
        event_kwargs: Dict[str, Any] = {
            "symbol": sym,
            "price": bar.close,
            "timestamp_ms": ts,
            "candle_1h": c1h,
        }
        if bar_tf == "5m":
            event_kwargs["candle_5m"] = bar
        else:
            event_kwargs["candle_15m"] = bar
        event = MarketEvent(**event_kwargs)

        # --- manage open position ---
        if sym in open_pos:
            pos = open_pos[sym]
            # Hard SL on bar extremes
            if pos.stop_loss_price is not None:
                hit = False
                if pos.side == "long" and bar.low <= pos.stop_loss_price:
                    hit = True
                    exit_px = pos.stop_loss_price
                elif pos.side == "short" and bar.high >= pos.stop_loss_price:
                    hit = True
                    exit_px = pos.stop_loss_price
                else:
                    exit_px = bar.close
                if hit:
                    settle(pos, exit_px, ts, "stop_loss")
                    equity.append((ts, capital))
                    continue

            base_pos = Position(
                symbol=pos.symbol,
                side=pos.side,
                entry_price=pos.entry_price,
                size=pos.size,
                entry_time_ms=pos.entry_time_ms,
                stop_loss_price=pos.stop_loss_price,
            )
            exit_sig = strategy.on_position(base_pos, event)
            if exit_sig is not None:
                settle(pos, bar.close, ts, exit_sig.reason)
                equity.append((ts, capital))
                continue

        # --- entries (flat only) ---
        if sym not in open_pos:
            sig = strategy.on_data(event)
            if sig is not None and sig.side in ("long", "short"):
                open_from_signal(sig, bar, ts)

        if len(equity) == 0 or equity[-1][0] != ts:
            equity.append((ts, capital))

    # Force flat at end
    if timeline and open_pos:
        last_ts = timeline[-1][0]
        last_close = {sym: bars_by_sym[sym][-1].close for sym in symbols if bars_by_sym[sym]}
        for sym, pos in list(open_pos.items()):
            settle(pos, last_close.get(sym, pos.entry_price), last_ts, "end_of_data")

    return {
        "trades": trades,
        "equity_curve": equity,
        "metrics": {},
    }


def slice_by_symbol(
    result: Dict[str, Any],
    symbols: List[str],
    initial_capital: float,
) -> Dict[str, Dict[str, Any]]:
    """Build per-symbol and ALL summaries from one multi-symbol replay."""
    trades = result.get("trades", [])
    equity = result.get("equity_curve", [])
    out: Dict[str, Dict[str, Any]] = {
        "ALL": summarize_trades(trades, equity),
    }
    for sym in symbols:
        st = [t for t in trades if t.get("symbol") == sym]
        # Approximate per-symbol equity from that symbol's trade PnL path
        cap = initial_capital
        eq: List[Tuple[int, float]] = []
        for t in sorted(st, key=lambda x: int(x.get("exit_time", 0))):
            cap += float(t.get("pnl_usd", 0.0))
            eq.append((int(t.get("exit_time", 0)), cap))
        if not eq:
            eq = [(0, initial_capital)]
        out[sym] = summarize_trades(st, eq)
    return out


# ---------------------------------------------------------------------------
# Full BacktestEngine path (slow)
# ---------------------------------------------------------------------------

def run_full_engine(
    cfg: Any,
    db: Database,
    variant: Variant,
    fade_section: Dict[str, Any],
    symbols: List[str],
    start_ms: int,
    end_ms: int,
    initial_capital: float,
) -> Dict[str, Any]:
    strategy = build_strategy(variant, fade_section)
    risk_cfg = dict(cfg.get("risk", {}) or {})
    bt_cfg = BacktestConfig(
        initial_capital=initial_capital,
        commission_pct=float(cfg.get("backtest.commission_pct", 0.035)),
        slippage_bps=float(cfg.get("backtest.slippage_bps", 2.0)),
        max_positions=int(cfg.get("risk.max_positions", 5)),
        tca_enabled=False,
        paper_slippage_pct=float(cfg.get("risk.paper_slippage_pct", 0.02)),
        use_regime_weights=False,
        use_cooldown=True,
        use_microstructure_proxy=False,
        use_risk_manager=True,
        use_volatility_circuit=False,
        use_funding_blackout=False,
        use_external_feeds_replay=False,
        max_daily_trades=0,
    )
    engine = BacktestEngine(
        database=db,
        strategy=strategy,
        config=bt_cfg,
        symbols=symbols,
        risk_config=risk_cfg,
    )
    result = engine.run(start_ms=start_ms, end_ms=end_ms)
    return {
        "trades": result.get("trades", []) or [],
        "equity_curve": result.get("equity_curve", []) or [],
        "metrics": result.get("metrics", {}) or {},
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def fmt_row(r: Dict[str, Any]) -> str:
    if r.get("error"):
        return f"{r['variant']:<24} {r['symbols']:<16} ERROR: {r['error']}"
    return (
        f"{r['variant']:<24} {r['symbols']:<16} "
        f"n={r['n_trades']:>4}  WR={r['win_rate']:>5.1f}%  "
        f"avgW={r['avg_win']:>7.2f} avgL={r['avg_loss']:>8.2f}  "
        f"E[x]={r['expectancy']:>7.2f}  PF={r['profit_factor']:>5.2f}  "
        f"Sh={r['sharpe']:>6.2f}  DD={r['max_dd_pct']:>6.2f}%  "
        f"gross={r['gross_pnl']:>8.2f}  fees={r['fees_total']:>7.2f}  "
        f"net={r['net_pnl']:>8.2f}"
    )


def verdict(rows: List[Dict[str, Any]]) -> str:
    by_key = {r["variant"]: r for r in rows if r.get("symbols") == "ALL" and not r.get("error")}
    fade = by_key.get("fade_baseline")
    trend = by_key.get("trend_baseline")
    if not fade or not trend:
        return "Insufficient aggregate results for a verdict."

    lines = [
        "",
        "=" * 88,
        "VERDICT",
        "=" * 88,
        f"Fade  (a): n={fade['n_trades']} WR={fade['win_rate']}%  "
        f"net=${fade['net_pnl']}  fees=${fade['fees_total']}  "
        f"gross=${fade['gross_pnl']}  PF={fade['profit_factor']}  E[x]=${fade['expectancy']}",
        f"Trend (b): n={trend['n_trades']} WR={trend['win_rate']}%  "
        f"net=${trend['net_pnl']}  fees=${trend['fees_total']}  "
        f"gross=${trend['gross_pnl']}  PF={trend['profit_factor']}  E[x]=${trend['expectancy']}",
    ]

    trend_edge = trend["net_pnl"] > 0 and trend["expectancy"] > 0 and trend["profit_factor"] > 1.0
    fade_edge = fade["net_pnl"] > 0 and fade["expectancy"] > 0 and fade["profit_factor"] > 1.0
    beats = trend["net_pnl"] > fade["net_pnl"] and trend["expectancy"] > fade["expectancy"]

    if trend_edge and beats:
        lines.append(
            "-> Trend (anchored) BEATS fade on net PnL/expectancy AFTER fees in this window."
        )
    elif trend_edge and not beats:
        lines.append(
            "-> Trend has a positive edge after fees, but does NOT clearly beat fade on "
            "net PnL/expectancy in this window."
        )
    elif not trend_edge and fade_edge:
        lines.append(
            "-> Fade keeps the better AFTER-FEE profile; trend does not show a usable "
            "crypto edge in this window (fees and/or adverse selection)."
        )
    else:
        lines.append(
            "-> Neither variant shows a clean after-fee edge on the aggregate window "
            "(check per-symbol — crypto regimes differ)."
        )

    if trend["n_trades"] > 0:
        lines.append(
            f"-> Trend fee drag: ${trend['fees_total']} "
            f"({trend['fee_drag_pct_of_gross']}% of |gross|) across {trend['n_trades']} trades "
            f"— high flip rate is expensive at ~7 bps RT."
        )

    season_keys = ["trend_us_paper_hours", "trend_us_rth", "trend_asia", "trend_eu"]
    season = [by_key[k] for k in season_keys if k in by_key and by_key[k]["n_trades"] > 0]
    if season and trend["n_trades"] > 0:
        best = max(season, key=lambda r: r["net_pnl"])
        lines.append(
            f"-> Best hour filter: {best['variant']} net=${best['net_pnl']} "
            f"(vs trend baseline net=${trend['net_pnl']})."
        )
        if best["net_pnl"] > trend["net_pnl"] and best["expectancy"] > 0:
            lines.append(
                "-> Weak evidence of hour-of-day seasonality helping AFTER fees — "
                "treat as hypothesis, not production gate."
            )
        elif best["net_pnl"] > trend["net_pnl"]:
            lines.append(
                "-> Hour filters reduce loss/fee bleed vs unconstrained trend but do NOT "
                "create positive after-fee expectancy — not analogous to equity RTH edge."
            )
        else:
            lines.append(
                "-> No strong evidence that US/Asia-style hour filters improve "
                "anchored VWAP trend after fees vs unconstrained trend."
            )
    else:
        lines.append("-> Seasonality variants produced too few trades for a firm call.")

    lines.append(
        "Caveat: light 15m/5m replay (not full 1m engine); sample ~May-Aug 2026; "
        "HYPE starts later; live fade left-tail may differ from replay."
    )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", default=DEFAULT_START)
    p.add_argument("--end", default=today_utc_str())
    p.add_argument("--symbols", default=",".join(SYMBOLS_DEFAULT))
    p.add_argument("--capital", type=float, default=None)
    p.add_argument("--variants", default="all")
    p.add_argument(
        "--engine",
        choices=("light", "full"),
        default="light",
        help="light=15m/5m research replay (default); full=1m BacktestEngine (slow)",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    start_ms = ms_from_date(args.start)
    end_ms = ms_from_date(args.end, end=True)

    cfg = load_config()
    db = Database(cfg.get("database.path", "data/live/bot.db"))
    fade_section = dict(cfg.get("strategy.vwap_deviation", {}) or {})
    initial_capital = float(
        args.capital
        if args.capital is not None
        else cfg.get("backtest.initial_capital", cfg.get("risk.initial_capital", 10_000.0))
    )
    commission_pct = float(cfg.get("backtest.commission_pct", 0.04))
    slippage_bps = float(cfg.get("backtest.slippage_bps", 2.0))

    if args.variants == "all":
        variants = list(VARIANTS)
    else:
        wanted = {v.strip() for v in args.variants.split(",") if v.strip()}
        variants = [v for v in VARIANTS if v.key in wanted]
        if not variants:
            print(f"No matching variants for {wanted}")
            return 2

    print(
        f"VWAP trend vs fade | {args.start} -> {args.end} | "
        f"symbols={symbols} | capital={initial_capital:.0f} | engine={args.engine}",
        flush=True,
    )
    print(
        f"commission_pct={commission_pct}  (half-turn; RT ~= 2x)  "
        f"slippage_bps={slippage_bps}",
        flush=True,
    )
    print("-" * 88, flush=True)

    rows: List[Dict[str, Any]] = []
    t0 = time.time()

    for variant in variants:
        print(f"Running {variant.key} @ ALL({','.join(symbols)}) ...", flush=True)
        try:
            strategy = build_strategy(variant, fade_section)
            if args.engine == "full":
                result = run_full_engine(
                    cfg, db, variant, fade_section, symbols,
                    start_ms, end_ms, initial_capital,
                )
            else:
                bar_tf = variant.confirm_tf if variant.kind == "trend" else "15m"
                # Fade uses 1h VWAP but can be stepped on 15m closes
                result = light_replay(
                    db, strategy, symbols, start_ms, end_ms,
                    bar_tf=bar_tf,
                    initial_capital=initial_capital,
                    commission_pct=commission_pct,
                    slippage_bps=slippage_bps,
                )
            sliced = slice_by_symbol(result, symbols, initial_capital)
        except Exception as exc:
            err = {
                "variant": variant.key,
                "label": variant.label,
                "symbols": "ALL",
                "error": str(exc)[:240],
                "n_trades": 0,
                "net_pnl": 0.0,
                "gross_pnl": 0.0,
                "fees_total": 0.0,
            }
            rows.append(err)
            print(fmt_row(err), flush=True)
            continue

        for scope in ["ALL"] + symbols:
            summary = dict(sliced[scope])
            summary.update({
                "variant": variant.key,
                "label": variant.label,
                "symbols": scope,
                "error": "",
            })
            rows.append(summary)
            print(fmt_row(summary), flush=True)

    out_dir = ROOT / "data" / "backtests"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    csv_path = out_dir / f"vwap_trend_vs_fade_{ts}.csv"
    json_path = out_dir / f"vwap_trend_vs_fade_{ts}.json"

    fieldnames = [
        "variant", "label", "symbols", "n_trades", "win_rate", "avg_win", "avg_loss",
        "expectancy", "profit_factor", "sharpe", "max_dd_pct",
        "gross_pnl", "fees_total", "net_pnl", "fee_drag_pct_of_gross", "error",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(r)

    vtext = verdict(rows)
    with json_path.open("w", encoding="utf-8") as fh:
        json.dump(
            {
                "start": args.start,
                "end": args.end,
                "symbols": symbols,
                "capital": initial_capital,
                "engine": args.engine,
                "rows": rows,
                "verdict": vtext,
            },
            fh,
            indent=2,
        )

    print("-" * 88, flush=True)
    print(f"Elapsed {time.time() - t0:.1f}s", flush=True)
    print(f"CSV:  {csv_path}", flush=True)
    print(f"JSON: {json_path}", flush=True)
    print(vtext, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
