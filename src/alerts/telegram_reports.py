"""Telegram report formatters — read-only status queries (Phase B)."""
from __future__ import annotations

import html
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

HELP_TEXT = (
    "<b>Hyperliquid Bot — Commands</b>\n"
    "/status — capital, PnL, drawdown, leverage\n"
    "/positions — open positions with SL/TP\n"
    "/pnl — today's realised PnL\n"
    "/pnl week — last 7 days PnL\n"
    "/trades — last 10 closed trades\n"
    "/strategy — per-strategy breakdown\n"
    "/help — this message"
)


def utc_midnight_ms() -> int:
    now = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp() * 1000)


def rolling_days_ms(days: int) -> int:
    return int((time.time() - days * 86400) * 1000)


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def _fmt_usd(value: float) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}${value:,.2f}"


def _fmt_pct_fraction(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.2f}%"


async def build_status_message(
    engine: Any,
    *,
    db: Any = None,
    since_ms: Optional[int] = None,
    window_label: Optional[str] = None,
) -> str:
    """Build bot status block.

    When *db* + *since_ms* are provided (scheduled digests), Daily PnL /
    trade counts come from closed trades in that window so they match the
    digest PnL / strategy sections. Otherwise they use live portfolio
    session counters (UTC calendar day).
    """
    portfolio = engine._portfolio
    risk = engine._risk
    capital = await portfolio.current_capital
    positions = await portfolio.positions
    # sync_max_drawdown_pct already returns percent points (e.g. 7.15 == 7.15%)
    max_dd = portfolio.sync_max_drawdown_pct()
    cb = risk.is_circuit_breaker_tripped()

    if db is not None and since_ms is not None:
        stats = db.get_closed_trade_stats_since(since_ms)
        daily_pnl = float(stats.get("total_pnl_usd") or 0.0)
        daily_trades = int(stats.get("trades") or 0)
        pnl_label = f"PnL ({window_label or 'window'})"
        trades_label = f"Trades ({window_label or 'window'})"
    else:
        daily_pnl = await portfolio.daily_pnl
        daily_trades = await portfolio.daily_trades
        pnl_label = "Daily PnL"
        trades_label = "Daily trades"

    long_notional = 0.0
    short_notional = 0.0
    for pos in positions.values():
        snap = pos.to_position() if hasattr(pos, "to_position") else pos
        mid = getattr(snap, "current_price", 0) or getattr(snap, "entry_price", 0) or 0
        notional = getattr(snap, "size", 0) * mid
        if getattr(snap, "side", "") == "long":
            long_notional += notional
        else:
            short_notional += notional
    open_notional = long_notional + short_notional
    leverage = (open_notional / capital) if capital > 0 else 0.0
    mode = getattr(engine, "_mode", "paper")

    lines = [
        "<b>Bot Status</b>",
        f"Mode: <code>{_esc(mode.upper())}</code>",
        f"Capital: ${_esc(f'{capital:,.2f}')}",
        f"{pnl_label}: {_esc(_fmt_usd(daily_pnl))}",
        f"Max DD: {_esc(f'{max_dd:.2f}')}%",
        f"Open positions: {len(positions)}",
        f"{trades_label}: {daily_trades}",
        f"Portfolio lev: {_esc(f'{leverage:.2f}')}x",
        f"Long/Short notional: ${_esc(f'{long_notional:,.0f}')} / ${_esc(f'{short_notional:,.0f}')}",
        f"Circuit breaker: {'<b>TRIPPED</b>' if cb else 'ok'}",
    ]
    return "\n".join(lines)


def build_positions_message(engine: Any) -> str:
    from dashboard.web import build_positions_payload

    rows = build_positions_payload(engine)
    if not rows:
        return "<b>Open Positions</b>\n<i>None</i>"

    lines = ["<b>Open Positions</b>"]
    for row in rows:
        sym = _esc(row.get("symbol", "?"))
        side = _esc(str(row.get("side", "?")).upper())
        size = float(row.get("size") or 0)
        entry = float(row.get("entry_price") or 0)
        current = float(row.get("current_price") or 0) or entry
        upnl = float(row.get("unrealized_pnl") or 0)
        pnl_pct = float(row.get("pnl_pct") or 0)
        sl = row.get("stop_loss")
        tp = row.get("take_profit")
        strat = _esc(row.get("strategy", "unknown"))
        sl_txt = f"${sl:,.2f}" if sl else "—"
        tp_txt = f"${tp:,.2f}" if tp else "—"
        emoji = "🟢" if upnl >= 0 else "🔴"
        lines.append(
            f"\n{emoji} <b>{sym}</b> {side} ({strat})"
            f"\n  Size: {size:.4f} @ ${entry:,.2f} → ${current:,.2f}"
            f"\n  uPnL: {_esc(_fmt_usd(upnl))} ({pnl_pct:+.2f}%)"
            f"\n  SL: {sl_txt} | TP: {tp_txt}"
        )
    return "\n".join(lines)


def build_pnl_message(db: Any, *, period: str = "day") -> str:
    if period == "week":
        since_ms = rolling_days_ms(7)
        title = "PnL — Last 7 Days"
    elif period == "rolling_24h":
        since_ms = rolling_days_ms(1)
        title = "PnL — Last 24h"
    else:
        since_ms = utc_midnight_ms()
        title = "PnL — Today (UTC)"

    stats = db.get_closed_trade_stats_since(since_ms)
    total = float(stats.get("total_pnl_usd") or 0)
    trades = int(stats.get("trades") or 0)
    wins = int(stats.get("wins") or 0)
    win_rate = (wins / trades * 100) if trades else 0.0
    emoji = "🟢" if total >= 0 else "🔴"

    lines = [
        f"{emoji} <b>{title}</b>",
        f"Realised PnL: {_esc(_fmt_usd(total))}",
        f"Trades: {trades} ({wins}W / {trades - wins}L)",
        f"Win rate: {win_rate:.1f}%",
    ]

    if period == "week":
        series = db.get_daily_pnl_series(days=7)
        if series:
            lines.append("\n<b>Daily breakdown:</b>")
            for row in series[-7:]:
                day_pnl = float(row.get("pnl_usd") or 0)
                day_trades = int(row.get("trades") or 0)
                sign = "+" if day_pnl >= 0 else ""
                lines.append(
                    f"  {_esc(row.get('day', '?'))}: {sign}${day_pnl:,.2f} ({day_trades} trades)"
                )
    return "\n".join(lines)


def build_trades_message(db: Any, limit: int = 10) -> str:
    rows = db.get_closed_trades_since(rolling_days_ms(30), limit=limit)
    if not rows:
        return "<b>Recent Trades</b>\n<i>No closed trades in the last 30 days</i>"

    lines = [f"<b>Recent Trades</b> (last {len(rows)})"]
    for row in rows:
        sym = _esc(row.get("symbol", "?"))
        side = _esc(str(row.get("side", "?")).upper())
        pnl = float(row.get("pnl_usd") or 0)
        pnl_pct = row.get("pnl_pct")
        strat = _esc(row.get("strategy", "?"))
        reason = _esc(row.get("exit_reason") or "—")
        emoji = "🟢" if pnl >= 0 else "🔴"
        exit_ms = row.get("exit_time")
        ts = ""
        if exit_ms:
            ts = datetime.fromtimestamp(int(exit_ms) / 1000, tz=timezone.utc).strftime(
                "%m-%d %H:%M UTC"
            )
        lines.append(
            f"\n{emoji} <b>{sym}</b> {side} — {_esc(_fmt_usd(pnl))} "
            f"({_fmt_pct_fraction(pnl_pct)})"
            f"\n  {strat} | {reason}"
            + (f"\n  {_esc(ts)}" if ts else "")
        )
    return "\n".join(lines)


def build_strategy_message(db: Any, *, since_ms: Optional[int] = None) -> str:
    rows = db.get_strategy_pnl(since_ms=since_ms)
    if not rows:
        return "<b>Strategy PnL</b>\n<i>No closed trades yet</i>"

    period_label = "last 24h" if since_ms is not None else "all time"
    lines = [f"<b>Strategy PnL</b> ({period_label})"]
    for row in rows[:12]:
        name = _esc(row.get("strategy", "?"))
        total = float(row.get("total_pnl_usd") or 0)
        trades = int(row.get("trades") or 0)
        wr = float(row.get("win_rate") or 0) * 100
        emoji = "🟢" if total >= 0 else "🔴"
        lines.append(
            f"\n{emoji} <b>{name}</b>: {_esc(_fmt_usd(total))}"
            f" | {trades} trades | WR {wr:.0f}%"
        )
    return "\n".join(lines)


async def build_digest_message(engine: Any, db: Any, *, period: str) -> str:
    """Build scheduled digest (daily or weekly).

    Daily digests use a **rolling 24h** window for status PnL, the PnL
    block, and strategy breakdown — previously "Today (UTC)" disagreed
    with "Strategy PnL (last 24h)" when trades closed yesterday UTC but
    still inside the rolling window (common for 08:00 UTC digests).
    """
    if period == "weekly":
        since_ms = rolling_days_ms(7)
        window_label = "last 7 days"
        pnl = build_pnl_message(db, period="week")
        header = "<b>📊 Weekly Digest</b>\n"
    else:
        since_ms = rolling_days_ms(1)
        window_label = "last 24h"
        pnl = build_pnl_message(db, period="rolling_24h")
        header = "<b>📊 Daily Digest</b>\n"

    status = await build_status_message(
        engine, db=db, since_ms=since_ms, window_label=window_label,
    )
    strategy = build_strategy_message(db, since_ms=since_ms)
    parts = [header, status, "", pnl, "", strategy]
    text = "\n".join(parts)
    if len(text) > 4000:
        return text[:3990] + "\n…"
    return text
