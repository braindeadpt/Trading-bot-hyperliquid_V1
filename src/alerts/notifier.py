"""Alert notification system for trading bot.
Supports Telegram and Discord webhooks.
"""
from __future__ import annotations

import asyncio
import aiohttp
import logging
from dataclasses import dataclass
from typing import Optional
from datetime import datetime

logger = logging.getLogger("alerts")

@dataclass
class AlertConfig:
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    discord_webhook_url: Optional[str] = None
    enabled: bool = False
    min_level: str = "info"  # debug, info, warning, error
    trade_alerts: bool = True  # entry/exit always push when enabled

class AlertNotifier:
    """Sends alerts via Telegram and/or Discord."""

    def __init__(self, config: AlertConfig):
        self.cfg = config
        self._http: Optional[aiohttp.ClientSession] = None
        self._last_ws_alert = 0.0
        self._last_daily_pnl = 0.0
        self._last_market_data_alert = 0.0

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()
        return self._http

    async def send(self, message: str, level: str = "info", *, force: bool = False) -> None:
        """Send alert to all configured channels."""
        if not self.cfg.enabled:
            return
        if not force:
            if level == "debug" and self.cfg.min_level != "debug":
                return
            if level == "info" and self.cfg.min_level in ("warning", "error"):
                return
            if level == "warning" and self.cfg.min_level == "error":
                return

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        full_msg = f"[{timestamp}] {message}"

        tasks = []
        if self.cfg.telegram_bot_token and self.cfg.telegram_chat_id:
            tasks.append(self._send_telegram(full_msg))
        if self.cfg.discord_webhook_url:
            tasks.append(self._send_discord(full_msg))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def send_alert(self, message: str, level: str = "info") -> None:
        """Alias for send() — used by engine callers."""
        await self.send(message, level)

    async def _send_telegram(self, message: str) -> None:
        """Send message via Telegram Bot API."""
        try:
            session = await self._get_session()
            url = f"https://api.telegram.org/bot{self.cfg.telegram_bot_token}/sendMessage"
            payload = {
                "chat_id": self.cfg.telegram_chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("Telegram alert failed: %s %s", resp.status, body)
                else:
                    logger.debug("Telegram alert sent")
        except Exception:
            logger.exception("Telegram alert error")

    async def _send_discord(self, message: str) -> None:
        """Send message via Discord webhook."""
        try:
            payload = {"content": message}
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.cfg.discord_webhook_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status not in (200, 204):
                        body = await resp.text()
                        logger.warning("Discord alert failed: %s %s", resp.status, body)
                    else:
                        logger.debug("Discord alert sent")
        except Exception:
            logger.exception("Discord alert error")

    # ── Convenience methods ──

    async def trade_entry(
        self,
        symbol: str,
        side: str,
        size: float,
        price: float,
        strategy: str,
        *,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        notional_usd: Optional[float] = None,
    ) -> None:
        if not self.cfg.trade_alerts:
            return
        notional = notional_usd if notional_usd is not None else size * price
        sl_txt = f"${stop_loss:,.2f}" if stop_loss else "—"
        tp_txt = f"${take_profit:,.2f}" if take_profit else "—"
        msg = (
            f"🟢 <b>TRADE ENTRY</b>\n"
            f"Symbol: {symbol}\n"
            f"Side: {side.upper()}\n"
            f"Size: {size:.4f} (~${notional:,.0f})\n"
            f"Price: ${price:,.2f}\n"
            f"SL: {sl_txt} | TP: {tp_txt}\n"
            f"Strategy: {strategy}"
        )
        await self.send(msg, "info", force=True)

    async def trade_exit(
        self,
        symbol: str,
        side: str,
        pnl: float,
        exit_price: float,
        strategy: str,
        *,
        exit_reason: Optional[str] = None,
        pnl_pct: Optional[float] = None,
    ) -> None:
        if not self.cfg.trade_alerts:
            return
        emoji = "🟢" if pnl >= 0 else "🔴"
        pct_txt = f" ({pnl_pct * 100:.2f}%)" if pnl_pct is not None else ""
        reason_txt = f"\nReason: {exit_reason}" if exit_reason else ""
        msg = (
            f"{emoji} <b>TRADE EXIT</b>\n"
            f"Symbol: {symbol}\n"
            f"Side: {side.upper()}\n"
            f"Exit Price: ${exit_price:,.2f}\n"
            f"PnL: ${pnl:,.2f}{pct_txt}\n"
            f"Strategy: {strategy}{reason_txt}"
        )
        await self.send(msg, "info", force=True)

    async def market_data_health_red(
        self,
        overall: str,
        details: str,
        duration_min: float,
    ) -> None:
        """Alert when market data feeds stay unhealthy (rate limited)."""
        now = asyncio.get_event_loop().time()
        if now - self._last_market_data_alert < 900:
            return
        self._last_market_data_alert = now
        msg = (
            f"🚨 <b>MARKET DATA HEALTH {overall.upper()}</b>\n"
            f"Duration: {duration_min:.1f} min\n"
            f"{details}\n"
            f"Strategies may use stale funding/OI — check dashboard."
        )
        await self.send(msg, "error")

    async def ws_disconnect(self, exchange: str, duration_sec: float) -> None:
        """Alert if WS disconnected for more than 5 minutes."""
        if duration_sec < 300:  # Only alert after 5 min
            return
        now = asyncio.get_event_loop().time()
        if now - self._last_ws_alert < 900:  # Rate limit: 1 per 15 min
            return
        self._last_ws_alert = now
        msg = (
            f"⚠️ <b>WEBSOCKET DISCONNECTED</b>\n"
            f"Exchange: {exchange}\n"
            f"Duration: {duration_sec/60:.1f} minutes\n"
            f"Bot may be missing market data!"
        )
        await self.send(msg, "warning")

    async def circuit_breaker(self, reason: str, action: str) -> None:
        msg = (
            f"🛑 <b>CIRCUIT BREAKER TRIGGERED</b>\n"
            f"Reason: {reason}\n"
            f"Action: {action}\n"
            f"Trading halted until manual reset."
        )
        await self.send(msg, "error")

    async def daily_pnl(self, total_pnl: float, trade_count: int, win_rate: float) -> None:
        """Send daily PnL summary (rate limited to once per day)."""
        now = asyncio.get_event_loop().time()
        if now - self._last_daily_pnl < 82800:  # ~23 hours
            return
        self._last_daily_pnl = now
        emoji = "🟢" if total_pnl >= 0 else "🔴"
        msg = (
            f"{emoji} <b>DAILY PnL SUMMARY</b>\n"
            f"Total PnL: ${total_pnl:,.2f}\n"
            f"Trades: {trade_count}\n"
            f"Win Rate: {win_rate:.1f}%\n"
            f"Capital: check dashboard"
        )
        await self.send(msg, "info")

    async def iv_gate_verdict(self, *, report: dict, verdict: dict) -> None:
        """IV gate verdict alert — PROMOTE or REJECT at the evidence gate.

        Fires when the recheck decides (n>=30 closed with an IV decision):
          * PROMOTE — live sample confirms the backtest direction, the router
            may flip from shadow to enforcement (threshold 66.7).
          * REJECT — the sample contradicts the backtest direction; keep
            shadow, never enforce.

        Both carry the **exact diff**: the slice numbers (high_iv / low_iv
        realized PnL + WR + n), the IV threshold, the verdict detail and the
        report path — the operator reviews and acts deliberately. The
        watchdog never touches the router itself — this is the
        human-in-the-loop notification.
        """
        hi = (report.get("slices") or {}).get("high_iv") or {}
        lo = (report.get("slices") or {}).get("low_iv") or {}
        status = str(verdict.get("verdict") or "PROMOTE").upper()
        n_closed = verdict.get("n_closed") or (
            (hi.get("n_closed") or 0) + (lo.get("n_closed") or 0)
        )
        threshold = verdict.get("threshold", 66.7)
        report_path = verdict.get("report_path") or "docs/IV_GATE_SHADOW_RECHECK_RESULT.md"
        detail = verdict.get("detail") or ""

        def _money(v) -> str:
            return "—" if v is None else f"{v:+.2f} USD"

        def _wr(v) -> str:
            return "—" if v is None else f"{v * 100:.0f}%"

        if status == "REJECT":
            emoji, title, intro, level = (
                "🛑", "IV GATE REJECT — manter shadow",
                "A amostra live não confirma a direção do backtest: o gate "
                "mantém-se shadow — nunca enforce.",
                "warning",
            )
        else:
            emoji, title, intro, level = (
                "🚀", "IV GATE PROMOTE — shadow → enforcement",
                "O gate IV passou o gate de evidência e a amostra live confirma "
                "o backtest: o router pode flipar de shadow a enforcement.",
                "warning",
            )

        msg = (
            f"{emoji} <b>{title}</b>\n"
            f"{intro}\n\n"
            f"<b>Evidência (n={n_closed} closed com decisão IV)</b>\n"
            f"· high_iv: net {_money(hi.get('net_pnl_usd'))} · WR {_wr(hi.get('win_rate'))} "
            f"(n={hi.get('n_closed') or 0})\n"
            f"· low_iv : net {_money(lo.get('net_pnl_usd'))} · WR {_wr(lo.get('win_rate'))} "
            f"(n={lo.get('n_closed') or 0})\n"
            f"· threshold IV: {threshold} (DVOL percentile 30d)\n"
        )
        if detail:
            msg += f"· decisão: {detail}\n"
        msg += (
            f"\n<b>Diff exacto:</b> {report_path}\n"
            f"A flip é uma mudança deliberada e revista — fora do âmbito do "
            f"watchdog."
        )
        await self.send(msg, level, force=True)

    async def error(self, message: str) -> None:
        await self.send(f"❌ <b>ERROR</b>\n{message}", "error")

    async def close(self) -> None:
        if self._http and not self._http.closed:
            await self._http.close()
