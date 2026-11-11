"""Event detection + hypothetical gain (pure logic, injectable clock).

Per prereg §Medição:
  * event   = Binance mid moves >= MOVE_BPS within <= MOVE_WINDOW_MS
  * delay   = time until the paired xyz mid covers >= half the move
  * gain    = xyz move from t0+500ms (taker ask/bid) to t0+5s, -11 bps
One open event per (bin, xyz) pair; a Binance move fans out to all its
declared xyz pairs as independent events (each xyz has its own book).
"""
from __future__ import annotations

import collections
import math
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional, Tuple

from src.latency_probe import spec

OnDone = Callable[[dict], None]


@dataclass
class _Open:
    t0: int
    direction: int
    move_bps: float
    xyz_ref_mid: Optional[float]
    session_id: str
    delay_ms: Optional[int] = None
    entry_px: Optional[float] = None
    entry_ms: Optional[int] = None


class EventDetector:
    """Feed ticks in; completed event rows come out via on_done."""

    def __init__(self, on_done: OnDone,
                 pairs: Dict[str, List[str]] = spec.SYMBOL_PAIRS,
                 move_bps: float = spec.MOVE_BPS,
                 window_ms: int = spec.MOVE_WINDOW_MS):
        self._pairs = pairs
        self._thr = move_bps
        self._win = window_ms
        self._on_done = on_done
        # bin symbol -> deque[(ts_ms, mid)] covering ~2x the window
        self._bin_mids: Dict[str, Deque[Tuple[int, float]]] = {
            s: collections.deque() for s in pairs}
        self._xyz_books: Dict[str, Tuple[int, float, float]] = {}
        self._open: Dict[Tuple[str, str], _Open] = {}
        self._last_open: Dict[str, int] = {}

    # ── feeds ─────────────────────────────────────────────────────────
    def on_bin_book(self, sym: str, ts_ms: int, bid: float,
                    ask: float, session_id: str) -> None:
        if sym not in self._bin_mids or bid <= 0 or ask <= 0:
            return
        mid = (bid + ask) / 2.0
        dq = self._bin_mids[sym]
        dq.append((ts_ms, mid))
        while dq and dq[0][0] < ts_ms - 2 * self._win:
            dq.popleft()
        ref = self._ref_mid(dq, ts_ms - self._win)
        if ref is None or ref <= 0:
            return
        move = (mid / ref - 1.0) * 1e4
        if abs(move) < self._thr:
            return
        if ts_ms - self._last_open.get(sym, -10**9) < spec.EVENT_COOLDOWN_MS:
            return
        self._last_open[sym] = ts_ms
        for xyz in self._pairs[sym]:
            book = self._xyz_books.get(xyz)
            ref_mid = (book[1] + book[2]) / 2.0 if book else None
            self._open[(sym, xyz)] = _Open(
                t0=ts_ms, direction=1 if move > 0 else -1,
                move_bps=abs(move), xyz_ref_mid=ref_mid,
                session_id=session_id)

    def on_xyz_book(self, sym: str, ts_ms: int, bid: float,
                    ask: float) -> None:
        if bid <= 0 or ask <= 0:
            return
        self._xyz_books[sym] = (ts_ms, bid, ask)
        mid = (bid + ask) / 2.0
        for (bsym, xsym), ev in list(self._open.items()):
            if xsym != sym:
                continue
            d = ev.direction
            # half-move delay (once)
            if ev.delay_ms is None and ev.xyz_ref_mid:
                cov = (mid / ev.xyz_ref_mid - 1.0) * d * 1e4
                if cov >= ev.move_bps * spec.HALF_RATIO:
                    ev.delay_ms = ts_ms - ev.t0
            # taker entry at first tick >= t0+500ms
            if ev.entry_px is None and ts_ms >= ev.t0 + spec.ENTRY_LAG_MS:
                if ts_ms <= ev.t0 + spec.ENTRY_LAG_MS + spec.ENTRY_DEADLINE_MS:
                    ev.entry_px = ask if d > 0 else bid
                    ev.entry_ms = ts_ms
            # exit at first tick >= t0+5s
            if ts_ms >= ev.t0 + spec.HOLD_MS:
                self._finish((bsym, xsym), ev, mid)

    def sweep(self, now_ms: int) -> None:
        """Expire events whose xyz never ticked past t0+5s."""
        for key, ev in list(self._open.items()):
            if now_ms >= ev.t0 + spec.HOLD_MS + spec.EVENT_COOLDOWN_MS:
                self._finish(key, ev, None)

    # ── internals ─────────────────────────────────────────────────────
    def _finish(self, key: Tuple[str, str], ev: _Open,
                exit_mid: Optional[float]) -> None:
        self._open.pop(key, None)
        bsym, xsym = key
        row = {"session_id": ev.session_id, "t0_ms": ev.t0,
               "bin_symbol": bsym, "xyz_symbol": xsym,
               "direction": ev.direction, "bin_move_bps": ev.move_bps,
               "delay_ms": ev.delay_ms,
               "entry_px": ev.entry_px, "exit_px": exit_mid,
               "gain_bps": None, "status": "no_fill",
               "detail": {"xyz_ref_mid": ev.xyz_ref_mid,
                          "entry_ms": ev.entry_ms}}
        if ev.entry_px and exit_mid:
            row["gain_bps"] = (ev.direction
                               * (exit_mid / ev.entry_px - 1.0) * 1e4
                               - spec.TAKER_COST_BPS)
            row["status"] = ("closed" if ev.delay_ms is not None
                             else "no_move")
        else:
            row["status"] = "no_fill"
        self._on_done(row)

    @staticmethod
    def _ref_mid(dq: Deque[Tuple[int, float]],
                 target_ms: int) -> Optional[float]:
        ref = None
        for ts, m in dq:
            if ts <= target_ms:
                ref = m
            else:
                break
        return ref
