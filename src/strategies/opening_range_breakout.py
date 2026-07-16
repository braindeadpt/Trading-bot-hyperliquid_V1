"""Opening Range Breakout (ORB) — research-only, session-anchored to NY open.

Not registered in the live factory path. Construct directly in tests / backtests.
"""

from __future__ import annotations

import collections
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Deque, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from src.strategies.base import ExitSignal, MarketEvent, Position, Signal, Strategy
from src.strategies.indicators import Candle
from src.utils.helpers import safe_divide

logger = logging.getLogger(__name__)

_UTC = timezone.utc


@dataclass(frozen=True)
class SessionSpec:
    """One named session anchor.

    Either ``tz_name`` + ``local_open`` (HH:MM local) or ``utc_open`` (HH:MM UTC).
    """

    name: str
    enabled: bool = True
    tz_name: Optional[str] = None
    local_open: str = "09:30"
    utc_open: Optional[str] = None


def default_sessions(*, enable_asia: bool = False) -> List[SessionSpec]:
    """NY equities open (enabled) + optional Asia 00:00 UTC (disabled by default)."""
    return [
        SessionSpec(name="NY", enabled=True, tz_name="America/New_York", local_open="09:30"),
        SessionSpec(name="Asia", enabled=enable_asia, utc_open="00:00"),
    ]


def _parse_hhmm(value: str) -> Tuple[int, int]:
    parts = value.strip().split(":")
    return int(parts[0]), int(parts[1])


def session_open_utc_ms(spec: SessionSpec, day: date) -> int:
    """Resolve session open for *day* to UTC epoch milliseconds (DST-aware)."""
    if spec.utc_open is not None:
        hh, mm = _parse_hhmm(spec.utc_open)
        dt = datetime(day.year, day.month, day.day, hh, mm, tzinfo=_UTC)
        return int(dt.timestamp() * 1000)

    tz_name = spec.tz_name or "America/New_York"
    tz = ZoneInfo(tz_name)
    hh, mm = _parse_hhmm(spec.local_open)
    local_dt = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
    return int(local_dt.astimezone(_UTC).timestamp() * 1000)


def _bucket_open_ms(timestamp_ms: int, interval_ms: int) -> int:
    return (int(timestamp_ms) // interval_ms) * interval_ms


def _utc_date_for_ms(ts_ms: int) -> date:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=_UTC).date()


def _session_date_for_ms(spec: SessionSpec, ts_ms: int) -> date:
    """Calendar date of the session that *ts_ms* belongs to (local TZ when set)."""
    if spec.utc_open is not None:
        return _utc_date_for_ms(ts_ms)
    tz = ZoneInfo(spec.tz_name or "America/New_York")
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=tz).date()


@dataclass
class _SessionDayState:
    session_name: str
    session_date: date
    session_open_ms: int
    range_high: Optional[float] = None
    range_low: Optional[float] = None
    range_formed: bool = False
    traded: bool = False
    last_signal_ms: int = 0


@dataclass
class _SymbolState:
    candles_5m: Deque[Candle] = field(default_factory=lambda: collections.deque(maxlen=120))
    candles_1m: Deque[Candle] = field(default_factory=lambda: collections.deque(maxlen=400))
    sessions: Dict[Tuple[str, date], _SessionDayState] = field(default_factory=dict)


class OpeningRangeBreakout(Strategy):
    """NY-session Opening Range Breakout (research-only)."""

    def __init__(
        self,
        *,
        range_minutes: int = 15,
        entry_window_minutes: int = 120,
        volume_mult: float = 1.5,
        volume_lookback: int = 20,
        take_profit_r: float = 2.0,
        min_stop_pct: float = 0.0015,
        max_range_pct: float = 0.015,
        max_hold_hours: float = 4.0,
        session_flat_minutes: int = 360,
        min_confidence: float = 0.60,
        size_pct: float = 0.02,
        signal_throttle_ms: int = 1_800_000,
        sessions: Optional[Sequence[SessionSpec]] = None,
        enable_asia_session: bool = False,
    ) -> None:
        self.RANGE_MINUTES = int(range_minutes)
        self.ENTRY_WINDOW_MINUTES = int(entry_window_minutes)
        self.VOLUME_MULT = float(volume_mult)
        self.VOLUME_LOOKBACK = int(volume_lookback)
        self.TAKE_PROFIT_R = float(take_profit_r)
        self.MIN_STOP_PCT = float(min_stop_pct)
        self.MAX_RANGE_PCT = float(max_range_pct)
        self.MAX_HOLD_HOURS = float(max_hold_hours)
        self.MAX_HOLD_MS = int(self.MAX_HOLD_HOURS * 3_600_000)
        self.SESSION_FLAT_MINUTES = int(session_flat_minutes)
        self.SESSION_FLAT_MS = int(self.SESSION_FLAT_MINUTES * 60_000)
        self.MIN_CONFIDENCE = float(min_confidence)
        self.SIZE_PCT = float(size_pct)
        self.SIGNAL_THROTTLE_MS = int(signal_throttle_ms)

        if sessions is not None:
            self._sessions = [s for s in sessions if s.enabled]
        else:
            self._sessions = [s for s in default_sessions(enable_asia=enable_asia_session) if s.enabled]

        self._state: Dict[str, _SymbolState] = {}
        self._INTERVAL_5M_MS = 300_000
        self._INTERVAL_1M_MS = 60_000
        self._range_bars_5m = max(1, self.RANGE_MINUTES // 5)

    @property
    def name(self) -> str:
        return "OpeningRangeBreakout"

    def _get_state(self, symbol: str) -> _SymbolState:
        if symbol not in self._state:
            self._state[symbol] = _SymbolState()
        return self._state[symbol]

    def _ingest(self, event: MarketEvent) -> None:
        state = self._get_state(event.symbol)
        if event.candle_5m is not None:
            if (
                not state.candles_5m
                or state.candles_5m[-1].timestamp_ms != event.candle_5m.timestamp_ms
            ):
                state.candles_5m.append(event.candle_5m)
        if event.candle_1m is not None:
            if (
                not state.candles_1m
                or state.candles_1m[-1].timestamp_ms != event.candle_1m.timestamp_ms
            ):
                state.candles_1m.append(event.candle_1m)

    def _active_session_day(
        self,
        ts_ms: int,
    ) -> Optional[Tuple[SessionSpec, date, int]]:
        """Return (spec, session_date, open_ms) if *ts_ms* is inside any session window."""
        for spec in self._sessions:
            day = _session_date_for_ms(spec, ts_ms)
            open_ms = session_open_utc_ms(spec, day)
            # Allow looking from open through session_flat for exits; entries gated later.
            if open_ms <= ts_ms < open_ms + max(self.SESSION_FLAT_MS, self.ENTRY_WINDOW_MINUTES * 60_000):
                return spec, day, open_ms
            # Also accept the short pre-flat window used while forming the range
            # when ts is slightly before flat but after open (already covered).
        return None

    def _work_candles(self, state: _SymbolState) -> Tuple[List[Candle], int]:
        """Prefer 5m history; fall back to aggregated 1m if 5m unavailable."""
        if state.candles_5m:
            return list(state.candles_5m), self._INTERVAL_5M_MS
        if not state.candles_1m:
            return [], self._INTERVAL_5M_MS
        # Aggregate 1m → synthetic 5m buckets for range + breakout detection
        buckets: Dict[int, List[Candle]] = {}
        for c in state.candles_1m:
            bo = _bucket_open_ms(c.timestamp_ms, self._INTERVAL_5M_MS)
            buckets.setdefault(bo, []).append(c)
        synth: List[Candle] = []
        for bo in sorted(buckets):
            chunk = buckets[bo]
            if len(chunk) < 5:
                continue
            synth.append(
                Candle(
                    open=chunk[0].open,
                    high=max(x.high for x in chunk),
                    low=min(x.low for x in chunk),
                    close=chunk[-1].close,
                    volume=sum(x.volume for x in chunk),
                    timestamp_ms=bo + self._INTERVAL_5M_MS - 1,
                ),
            )
        return synth, self._INTERVAL_5M_MS

    def _range_candles(
        self,
        candles: List[Candle],
        session_open_ms: int,
        interval_ms: int,
    ) -> List[Candle]:
        range_end = session_open_ms + self.RANGE_MINUTES * 60_000
        out: List[Candle] = []
        for c in candles:
            open_ms = _bucket_open_ms(c.timestamp_ms, interval_ms)
            if session_open_ms <= open_ms < range_end:
                out.append(c)
        return out

    def _ensure_session_state(
        self,
        state: _SymbolState,
        spec: SessionSpec,
        day: date,
        open_ms: int,
        candles: List[Candle],
        interval_ms: int,
        now_ms: int,
    ) -> _SessionDayState:
        key = (spec.name, day)
        day_state = state.sessions.get(key)
        if day_state is None:
            day_state = _SessionDayState(
                session_name=spec.name,
                session_date=day,
                session_open_ms=open_ms,
            )
            state.sessions[key] = day_state

        if day_state.range_formed:
            return day_state

        range_end = open_ms + self.RANGE_MINUTES * 60_000
        if now_ms < range_end:
            return day_state

        rc = self._range_candles(candles, open_ms, interval_ms)
        # Need enough bars covering the opening range
        if len(rc) < self._range_bars_5m:
            return day_state

        day_state.range_high = max(c.high for c in rc)
        day_state.range_low = min(c.low for c in rc)
        day_state.range_formed = True
        return day_state

    def on_data(self, event: MarketEvent) -> Optional[Signal]:
        self._ingest(event)
        state = self._get_state(event.symbol)

        active = self._active_session_day(event.timestamp_ms)
        if active is None:
            return None
        spec, day, open_ms = active

        candles, interval_ms = self._work_candles(state)
        if not candles:
            return None

        day_state = self._ensure_session_state(
            state, spec, day, open_ms, candles, interval_ms, event.timestamp_ms,
        )
        if not day_state.range_formed or day_state.range_high is None or day_state.range_low is None:
            return None

        if day_state.traded:
            return None

        if event.timestamp_ms - day_state.last_signal_ms < self.SIGNAL_THROTTLE_MS:
            return None

        entry_deadline = open_ms + self.ENTRY_WINDOW_MINUTES * 60_000
        if event.timestamp_ms > entry_deadline:
            return None

        # Breakout must be evaluated on a completed bar after the range window
        range_end = open_ms + self.RANGE_MINUTES * 60_000
        bar = candles[-1]
        bar_open = _bucket_open_ms(bar.timestamp_ms, interval_ms)
        if bar_open < range_end:
            return None

        # Only act when the latest bar is the event's candle (avoid stale re-fires)
        if event.candle_5m is not None:
            if bar.timestamp_ms != event.candle_5m.timestamp_ms:
                return None
        elif event.candle_1m is not None:
            # With 1m fallback, require the synthetic bar close to align with event time
            if abs(bar.timestamp_ms - event.timestamp_ms) > interval_ms:
                return None

        rh = float(day_state.range_high)
        rl = float(day_state.range_low)
        mid = (rh + rl) / 2.0
        if mid <= 0:
            return None
        range_pct = (rh - rl) / mid
        if range_pct > self.MAX_RANGE_PCT:
            logger.info(
                "OpeningRangeBreakout SKIP %s — range too wide (%.3f%% > %.3f%%)",
                event.symbol,
                range_pct * 100.0,
                self.MAX_RANGE_PCT * 100.0,
            )
            return None

        side: Optional[str] = None
        if bar.close > rh:
            side = "long"
        elif bar.close < rl:
            side = "short"
        else:
            return None

        # Volume confirmation vs preceding lookback bars (exclude breakout bar)
        hist = [c for c in candles if c.timestamp_ms < bar.timestamp_ms]
        if len(hist) < self.VOLUME_LOOKBACK:
            return None
        lookback = hist[-self.VOLUME_LOOKBACK :]
        mean_vol = sum(c.volume for c in lookback) / float(self.VOLUME_LOOKBACK)
        vol_ratio = safe_divide(bar.volume, mean_vol, 0.0)
        if vol_ratio < self.VOLUME_MULT:
            logger.info(
                "OpeningRangeBreakout SKIP %s — vol_ratio=%.2f (need >= %.2f)",
                event.symbol,
                vol_ratio,
                self.VOLUME_MULT,
            )
            return None

        entry = float(bar.close)
        if entry <= 0:
            return None

        if side == "long":
            stop_dist = entry - rl
        else:
            stop_dist = rh - entry
        stop_loss_pct = max(self.MIN_STOP_PCT, safe_divide(stop_dist, entry, self.MIN_STOP_PCT))
        take_profit_pct = stop_loss_pct * self.TAKE_PROFIT_R

        # Confidence: base + small bonus from volume surge, capped 0.85
        surge_bonus = min(0.25, max(0.0, (vol_ratio - self.VOLUME_MULT) * 0.05))
        confidence = min(0.85, self.MIN_CONFIDENCE + surge_bonus)

        day_state.traded = True
        day_state.last_signal_ms = event.timestamp_ms

        return Signal(
            strategy=self.name,
            symbol=event.symbol,
            side=side,
            confidence=confidence,
            size_pct=self.SIZE_PCT,
            entry_price=entry,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            reason=f"orb_{spec.name.lower()}_{side}",
            metadata={
                "session": spec.name,
                "session_date": day.isoformat(),
                "session_open_ms": open_ms,
                "range_high": rh,
                "range_low": rl,
                "range_pct": range_pct,
                "volume_ratio": vol_ratio,
            },
        )

    def on_position(self, position: Position, event: MarketEvent) -> Optional[ExitSignal]:
        hold_ms = event.timestamp_ms - position.entry_time_ms
        if hold_ms >= self.MAX_HOLD_MS:
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side=position.side,
                confidence=0.8,
                reason="orb_max_hold",
            )

        meta = position.metadata or {}
        session_open_ms = meta.get("session_open_ms")
        if session_open_ms is None:
            # Infer from entry time + default NY session of that local day
            for spec in self._sessions:
                day = _session_date_for_ms(spec, position.entry_time_ms)
                open_ms = session_open_utc_ms(spec, day)
                if open_ms <= position.entry_time_ms < open_ms + self.SESSION_FLAT_MS:
                    session_open_ms = open_ms
                    break

        if session_open_ms is not None:
            if event.timestamp_ms >= int(session_open_ms) + self.SESSION_FLAT_MS:
                return ExitSignal(
                    strategy=self.name,
                    symbol=position.symbol,
                    side=position.side,
                    confidence=0.85,
                    reason="orb_session_flat",
                )

        return None
