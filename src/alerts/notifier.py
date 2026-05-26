"""Alert notification system for trading bot.
Supports Telegram and Discord webhooks.
"""
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

class AlertNotifier:
    """Sends alerts via Telegram and/or Discord."""

    def __init__(self, config: AlertConfig):
        self.cfg = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_ws_alert = 0.0
        self._last_daily_pnl = 0.0

    async def _session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def send(self, message: str, level: str = "info") -> None:
        """Send alert to all configured channels."""
        if not self.cfg.enabled:
            return
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
            url = f"https://api.telegram.org/bot{self.cfg.telegram_bot_token}/sendMessage"
            payload = {
                "chat_id": self.cfg.telegram_chat_id,
                "text": message,
                "parse_mode": "HTML",
            }
            async with aiohttp.ClientSession() as session:
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

    async def trade_entry(self, symbol: str, side: str, size: float, price: float, strategy: str) -> None:
        msg = (
            f"🟢 <b>TRADE ENTRY</b>\n"
            f"Symbol: {symbol}\n"
            f"Side: {side.upper()}\n"
            f"Size: {size:.4f}\n"
            f"Price: ${price:,.2f}\n"
            f"Strategy: {strategy}"
        )
        await self.send(msg, "info")

    async def trade_exit(self, symbol: str, side: str, pnl: float, exit_price: float, strategy: str) -> None:
        emoji = "🟢" if pnl >= 0 else "🔴"
        msg = (
            f"{emoji} <b>TRADE EXIT</b>\n"
            f"Symbol: {symbol}\n"
            f"Side: {side.upper()}\n"
            f"Exit Price: ${exit_price:,.2f}\n"
            f"PnL: ${pnl:,.2f}\n"
            f"Strategy: {strategy}"
        )
        await self.send(msg, "info")

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

    async def error(self, message: str) -> None:
        await self.send(f"❌ <b>ERROR</b>\n{message}", "error")

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
