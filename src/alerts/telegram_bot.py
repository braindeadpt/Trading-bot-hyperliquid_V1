"""Interactive Telegram bot — read-only commands + scheduled digests (Phase A+B)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional, Set

import aiohttp

from alerts.telegram_reports import (
    HELP_TEXT,
    build_digest_message,
    build_pnl_message,
    build_positions_message,
    build_status_message,
    build_strategy_message,
    build_trades_message,
)

logger = logging.getLogger("alerts.telegram_bot")


class TelegramAPIError(RuntimeError):
    """Telegram Bot API error with HTTP-style error_code."""

    def __init__(self, method: str, data: dict) -> None:
        self.method = method
        self.error_code = int(data.get("error_code") or 0)
        self.description = str(data.get("description") or "")
        super().__init__(f"Telegram {method} failed: {data}")


class TelegramCommandBot:
    """Polls Telegram getUpdates for slash commands; sends scheduled digests."""

    def __init__(
        self,
        *,
        token: str,
        allowed_chat_ids: List[str],
        db: Any,
        engine_getter: Callable[[], Any],
        notifier: Any,
        poll_interval_sec: float = 2.0,
        digest_hours_utc: Optional[List[int]] = None,
        weekly_digest_day: int = 0,
        weekly_digest_hour_utc: int = 8,
    ) -> None:
        self._token = token
        self._allowed: Set[str] = {str(c).strip() for c in allowed_chat_ids if str(c).strip()}
        self._db = db
        self._get_engine = engine_getter
        self._notifier = notifier
        self._poll_interval = max(1.0, float(poll_interval_sec))
        self._digest_hours = digest_hours_utc if digest_hours_utc is not None else [8, 20]
        self._weekly_day = int(weekly_digest_day)
        self._weekly_hour = int(weekly_digest_hour_utc)
        self._offset = 0
        self._running = False
        self._session: Optional[aiohttp.ClientSession] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._digest_task: Optional[asyncio.Task] = None
        self._sent_digest_keys: Set[str] = set()
        self._conflict_warned = False

    @property
    def running(self) -> bool:
        return self._running

    async def start(self) -> None:
        if not self._token or not self._allowed:
            logger.warning("TelegramCommandBot not started: missing token or chat_id")
            return
        self._running = True
        self._session = aiohttp.ClientSession()
        self._poll_task = asyncio.create_task(self._poll_loop(), name="telegram_poll")
        self._digest_task = asyncio.create_task(self._digest_loop(), name="telegram_digest")
        logger.info(
            "TelegramCommandBot started (chats=%s digest_hours=%s)",
            len(self._allowed),
            self._digest_hours,
        )

    async def stop(self) -> None:
        self._running = False
        for task in (self._poll_task, self._digest_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        logger.info("TelegramCommandBot stopped")

    async def _api_get(self, method: str, **params: Any) -> dict:
        assert self._session is not None
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        async with self._session.get(
            url,
            params=params,
            timeout=aiohttp.ClientTimeout(total=35),
        ) as resp:
            data = await resp.json()
            if resp.status != 200 or not data.get("ok"):
                raise TelegramAPIError(method, data)
            return data

    async def _api_post(self, method: str, payload: dict) -> dict:
        assert self._session is not None
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        async with self._session.post(
            url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            data = await resp.json()
            if resp.status != 200 or not data.get("ok"):
                raise TelegramAPIError(method, data)
            return data

    async def _send_message(self, chat_id: str, text: str) -> None:
        if len(text) > 4096:
            text = text[:4090] + "\n…"
        await self._api_post(
            "sendMessage",
            {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
        )

    async def _poll_loop(self) -> None:
        await asyncio.sleep(3)
        while self._running:
            try:
                data = await self._api_get(
                    "getUpdates",
                    offset=self._offset,
                    timeout=25,
                    allowed_updates='["message"]',
                )
                for update in data.get("result", []):
                    self._offset = int(update["update_id"]) + 1
                    await self._handle_update(update)
            except asyncio.CancelledError:
                raise
            except TelegramAPIError as exc:
                if exc.error_code == 409:
                    if not self._conflict_warned:
                        logger.warning(
                            "Telegram getUpdates conflict (409) — another bot instance "
                            "is polling the same token; stop duplicate main.py processes"
                        )
                        self._conflict_warned = True
                    await asyncio.sleep(max(self._poll_interval, 10.0))
                else:
                    logger.exception("Telegram poll error")
                    await asyncio.sleep(self._poll_interval)
            except Exception:
                logger.exception("Telegram poll error")
                await asyncio.sleep(self._poll_interval)
            else:
                await asyncio.sleep(0.5)

    async def _handle_update(self, update: dict) -> None:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        if chat_id not in self._allowed:
            logger.debug("Ignoring Telegram message from unauthorized chat %s", chat_id)
            return

        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            return

        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        args = [a.lower() for a in parts[1:]]

        try:
            reply = await self._dispatch(cmd, args)
        except Exception:
            logger.exception("Telegram command %s failed", cmd)
            reply = "⚠️ Command failed — check bot logs."

        if reply:
            await self._send_message(chat_id, reply)

    async def _dispatch(self, cmd: str, args: List[str]) -> str:
        engine = self._get_engine()
        if engine is None and cmd not in ("/help", "/start"):
            return "Bot engine not ready yet."

        if cmd in ("/start", "/help"):
            return HELP_TEXT
        if cmd == "/status":
            return await build_status_message(engine)
        if cmd == "/positions":
            return build_positions_message(engine)
        if cmd == "/pnl":
            period = "week" if args and args[0] in ("week", "7d", "7") else "day"
            return build_pnl_message(self._db, period=period)
        if cmd == "/trades":
            limit = 10
            if args and args[0].isdigit():
                limit = min(25, max(1, int(args[0])))
            return build_trades_message(self._db, limit=limit)
        if cmd == "/strategy":
            return build_strategy_message(self._db)
        return f"Unknown command: {cmd}\n\n{HELP_TEXT}"

    async def _digest_loop(self) -> None:
        await asyncio.sleep(15)
        while self._running:
            try:
                now = datetime.now(timezone.utc)
                date_key = now.strftime("%Y-%m-%d")

                for hour in self._digest_hours:
                    key = f"daily-{date_key}-{hour}"
                    if now.hour == hour and now.minute < 2 and key not in self._sent_digest_keys:
                        await self._send_digest("daily")
                        self._sent_digest_keys.add(key)

                if now.weekday() == self._weekly_day and now.hour == self._weekly_hour:
                    wkey = f"weekly-{date_key}"
                    if now.minute < 2 and wkey not in self._sent_digest_keys:
                        await self._send_digest("weekly")
                        self._sent_digest_keys.add(wkey)

                if now.hour == 0 and now.minute < 2:
                    self._sent_digest_keys = {
                        k for k in self._sent_digest_keys if k.startswith(f"daily-{date_key}")
                        or k.startswith(f"weekly-{date_key}")
                    }
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Telegram digest loop error")
            await asyncio.sleep(60)

    async def _send_digest(self, period: str) -> None:
        engine = self._get_engine()
        if engine is None:
            return
        msg = await build_digest_message(engine, self._db, period=period)
        for chat_id in self._allowed:
            try:
                await self._send_message(chat_id, msg)
            except Exception:
                logger.exception("Failed to send %s digest to %s", period, chat_id)
        logger.info("Telegram %s digest sent to %d chat(s)", period, len(self._allowed))
