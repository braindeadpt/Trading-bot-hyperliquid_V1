"""latency-probe daemon — collector only, active inside NY windows.

Zero orders, zero paper trades: two public WS readers (Binance perp
bookTicker + HL l2Book on the declared xyz pairs), a local-timestamped
event detector, and an isolated SQLite ledger at spec.DB_PATH.
Outside the prereg windows it sleeps — nothing is collected.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

import websockets

from src.latency_probe import spec, windows
from src.latency_probe.detect import EventDetector
from src.latency_probe.store import ProbeStore

logger = logging.getLogger("latency_probe")

MS_MIN = 60_000
PRE_CONNECT_MS = 10_000        # wake slightly before the window opens


def _now_ms() -> int:
    return int(time.time() * 1000)


class LatencyProbeDaemon:
    def __init__(self, db_path: str = spec.DB_PATH,
                 ws_bin: str = spec.BINANCE_STREAM,
                 ws_hl: str = spec.HL_WS):
        self.store = ProbeStore(db_path)
        self._ws_bin = ws_bin
        self._ws_hl = ws_hl
        self._session_id: Optional[str] = None
        self._window_end_ms = 0
        self.detector = EventDetector(self._on_event)
        self._running = True
        self.store.meta_set("hb_ts_ms", str(_now_ms()))

    # ── event sink ────────────────────────────────────────────────────
    def _on_event(self, row: dict) -> None:
        try:
            self.store.event_insert(row)
        except Exception:  # noqa: BLE001
            logger.exception("latency-probe: event insert failed")

    # ── ws readers ────────────────────────────────────────────────────
    async def _binance_reader(self) -> None:
        streams = "/".join(f"{s.lower()}@bookTicker"
                           for s in spec.BIN_SYMBOLS)
        url = f"{self._ws_bin}?streams={streams}"
        async for ws in websockets.connect(url, ping_interval=20):
            try:
                async for raw in ws:
                    now = _now_ms()
                    if now >= self._window_end_ms:
                        return
                    try:
                        d = json.loads(raw).get("data") or {}
                        bid, ask = float(d["b"]), float(d["a"])
                        self.detector.on_bin_book(
                            str(d["s"]).upper(), now, bid, ask,
                            self._session_id or "")
                    except (KeyError, TypeError, ValueError):
                        continue
            except websockets.ConnectionClosed:
                continue

    async def _hl_reader(self) -> None:
        subs = [{"method": "subscribe",
                 "subscription": {"type": "l2Book", "coin": s}}
                for s in spec.XYZ_SYMBOLS]
        async for ws in websockets.connect(self._ws_hl, ping_interval=20):
            try:
                for s in subs:
                    await ws.send(json.dumps(s))
                async for raw in ws:
                    now = _now_ms()
                    if now >= self._window_end_ms:
                        return
                    try:
                        msg = json.loads(raw)
                        if msg.get("channel") != "l2Book":
                            continue
                        d = msg["data"]
                        lv = d.get("levels") or []
                        if len(lv) < 2 or not lv[0] or not lv[1]:
                            continue
                        bid = float(lv[0][0]["px"])
                        ask = float(lv[1][0]["px"])
                        self.detector.on_xyz_book(
                            str(d["coin"]), now, bid, ask)
                    except (KeyError, TypeError, ValueError, IndexError):
                        continue
            except websockets.ConnectionClosed:
                continue

    async def _sweeper(self) -> None:
        while self._running and _now_ms() < self._window_end_ms:
            self.detector.sweep(_now_ms())
            await asyncio.sleep(spec.HEARTBEAT_S)

    async def _heartbeat(self) -> None:
        """Always-on liveness — outside windows the probe must still
        report alive so the 2h-grace healthcheck stays green."""
        while self._running:
            self.store.meta_set("hb_ts_ms", str(_now_ms()))
            await asyncio.sleep(spec.HEARTBEAT_S)

    # ── window loop ───────────────────────────────────────────────────
    async def run(self) -> None:
        hb = asyncio.create_task(self._heartbeat())
        try:
            await self._window_loop()
        finally:
            hb.cancel()
            try:
                await hb
            except asyncio.CancelledError:
                pass

    async def _window_loop(self) -> None:
        while self._running:
            now = _now_ms()
            w = windows.window_at(now)
            if w is None:
                nxt = windows.next_window_start(now)
                sleep_ms = ((nxt[0] - now - PRE_CONNECT_MS)
                            if nxt else MS_MIN)
                await asyncio.sleep(max(5.0, min(sleep_ms, MS_MIN) / 1000))
                continue
            name, sid, end_ms = w
            ny_date = sid.split(":", 1)[0]
            self._session_id = sid
            self._window_end_ms = end_ms
            self.store.session_open(sid, name, ny_date, now)
            logger.info("latency-probe: window open %s until %d",
                        sid, end_ms)
            try:
                await asyncio.wait_for(
                    asyncio.gather(self._binance_reader(),
                                   self._hl_reader(),
                                   self._sweeper(),
                                   return_exceptions=True),
                    timeout=max(5.0, (end_ms - now) / 1000 + 30))
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass
            self.detector.sweep(_now_ms())
            self.store.session_close(sid, _now_ms())
            self.store.meta_set("hb_ts_ms", str(_now_ms()))
            logger.info("latency-probe: window closed %s", sid)

    def shutdown(self) -> None:
        self._running = False
        self.store.close()


async def _main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    d = LatencyProbeDaemon()
    try:
        await d.run()
    finally:
        d.shutdown()


if __name__ == "__main__":
    asyncio.run(_main())
