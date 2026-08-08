"""Experimental strategy: VWAP Trend (Zarattini & Aziz 2023, adapted to crypto).

Paper ("VWAP: The Holy Grail for Day Trading Systems", SSRN 4631351) shows
that on QQQ, **trend-following** VWAP (long above session VWAP, short below)
outperforms fading. Stops fire on a candle close on the opposite side of VWAP.

Crypto adaptations (24/7, ~0.07% round-trip taker fees):
  - Anchor = UTC day (00:00 UTC reset), not equity RTH open.
  - Confirmation on 5m/15m candle close (``vwap_confirm_tf``) to cut noise.
  - **Anti-flip is mandatory**: ``min_flip_interval_minutes`` (default 30) and
    ``vwap_cross_buffer_pct`` (default 0.1%). Flip-every-cross is unviable
    once RT fees (~7 bps) eat the edge of micro-crosses.
  - No "flat at the bell": ``max_hold_hours`` (default 12) + optional
    ``close_on_utc_rollover`` instead of equity session close.

PRODUCTION STATUS: experimental / research only.
  - Must remain ``enabled: false`` if ever wired into config.
  - Must NOT be added to the ensemble weight table.
  - Live config is frozen; instantiate only from research/backtest scripts.
"""

from __future__ import annotations

import collections
import datetime as _dt
import logging
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

from src.strategies.base import ExitSignal, MarketEvent, Position, Signal, Strategy
from src.strategies.indicators import Candle, calculate_anchored_vwap, calculate_atr

logger = logging.getLogger(__name__)


@dataclass
class _VWAPTrendState:
    """Per-symbol state for anchored VWAP trend tracking."""

    candles_1m: Deque[Candle] = field(default_factory=lambda: collections.deque(maxlen=1500))
    candles_5m: Deque[Candle] = field(default_factory=lambda: collections.deque(maxlen=400))
    candles_15m: Deque[Candle] = field(default_factory=lambda: collections.deque(maxlen=200))
    candles_1h: Deque[Candle] = field(default_factory=lambda: collections.deque(maxlen=100))
    last_confirm_ts: int = 0
    last_signal_side: Optional[str] = None
    last_signal_ms: int = 0
    last_exit_ms: int = 0
    last_exit_side: Optional[str] = None


class VWAPTrend(Strategy):
    """Anchored-VWAP trend follower (crypto adaptation of Zarattini & Aziz).

    Entry (on confirm-TF close):
      close > VWAP · (1 + buffer) → long
      close < VWAP · (1 - buffer) → short

    Exit:
      confirm-TF close on the opposite side of VWAP (with buffer), OR
      max_hold_hours, OR optional UTC-day rollover flat.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = config or {}
        self.ENABLED = bool(cfg.get("enabled", False))

        confirm_tf = str(cfg.get("vwap_confirm_tf", "15m")).lower().strip()
        if confirm_tf not in ("5m", "15m"):
            confirm_tf = "15m"
        self.CONFIRM_TF = confirm_tf

        self.MIN_FLIP_INTERVAL_MS = int(
            float(cfg.get("min_flip_interval_minutes", 30)) * 60_000
        )
        self.CROSS_BUFFER_PCT = float(cfg.get("vwap_cross_buffer_pct", 0.001))
        self.MAX_HOLD_HOURS = float(cfg.get("max_hold_hours", 12))
        self.MAX_HOLD_MS = int(self.MAX_HOLD_HOURS * 3_600_000)
        self.CLOSE_ON_UTC_ROLLOVER = bool(cfg.get("close_on_utc_rollover", True))

        # Optional hour-of-day entry filter (research variants)
        self.USE_SESSION_FILTER = bool(cfg.get("use_session_filter", False))
        self.SESSION_HOURS = self._parse_session_hours(cfg.get("session_hours_utc"))
        self.SESSION_START_UTC_H = int(cfg.get("session_start_utc_h", 0))
        self.SESSION_END_UTC_H = int(cfg.get("session_end_utc_h", 24))

        self.BASE_SIZE_PCT = float(cfg.get("base_size_pct", 0.01))
        self.MAX_SIZE_PCT = float(cfg.get("max_size_pct", 0.03))
        self.STOP_ATR_MULT = float(cfg.get("stop_loss_atr_multiplier", 2.0))
        self.MIN_CONFIDENCE = float(cfg.get("min_confidence", 0.55))
        self.SIGNAL_THROTTLE_MS = int(cfg.get("signal_throttle_ms", 60_000))
        # Warm-up: need enough bars in the current UTC day for a stable VWAP
        self.MIN_SESSION_BARS = int(cfg.get("min_session_bars", 3))

        self._state: Dict[str, _VWAPTrendState] = {}

    @staticmethod
    def _parse_session_hours(raw: Any) -> Optional[set]:
        """Parse explicit UTC hour allow-list, e.g. [13,14,15,19,20]."""
        if raw is None:
            return None
        if isinstance(raw, (list, tuple, set)):
            return {int(h) for h in raw}
        return None

    @property
    def name(self) -> str:
        return "VWAPTrend"

    def is_active(self) -> bool:
        """Research default is disabled; backtests force-enable via config."""
        return self.ENABLED

    # ------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------

    def on_data(self, event: MarketEvent) -> Optional[Signal]:
        if not self.ENABLED:
            return None

        state = self._get_state(event.symbol)
        self._ingest_candles(state, event)

        confirm = self._confirm_candle(state)
        if confirm is None:
            return None
        if confirm.timestamp_ms == state.last_confirm_ts:
            return None  # already evaluated this confirm bar
        state.last_confirm_ts = confirm.timestamp_ms

        if event.timestamp_ms - state.last_signal_ms < self.SIGNAL_THROTTLE_MS:
            return None

        if not self._in_session(event.timestamp_ms):
            return None

        session_bars = self._session_bars(state, confirm.timestamp_ms)
        if len(session_bars) < self.MIN_SESSION_BARS:
            return None

        vwap = calculate_anchored_vwap(session_bars, anchor="utc_day")
        if vwap is None or vwap <= 0:
            return None

        side = self._side_from_close(confirm.close, vwap)
        if side is None:
            return None

        # Anti-flip: block opposite entry too soon after a signal/exit
        if not self._anti_flip_ok(state, side, event.timestamp_ms):
            return None

        atr_src = list(state.candles_1h) if len(state.candles_1h) >= 15 else session_bars
        atr = calculate_atr(atr_src[-30:], 14) if len(atr_src) >= 15 else None
        if atr is not None and event.price > 0:
            stop_loss_pct = (atr * self.STOP_ATR_MULT) / event.price
        else:
            stop_loss_pct = 0.02

        # Soft confidence from distance-to-VWAP (capped)
        dist_pct = abs(confirm.close - vwap) / vwap
        confidence = min(0.90, self.MIN_CONFIDENCE + dist_pct * 20.0)
        size_pct = min(self.BASE_SIZE_PCT * (1.0 + dist_pct * 10.0), self.MAX_SIZE_PCT)

        state.last_signal_side = side
        state.last_signal_ms = event.timestamp_ms

        logger.info(
            "VWAPTrend %s %s — close=%.4f vwap=%.4f dist=%.3f%% conf=%.2f",
            side, event.symbol, confirm.close, vwap, dist_pct * 100, confidence,
        )

        return Signal(
            strategy=self.name,
            symbol=event.symbol,
            side=side,
            confidence=confidence,
            size_pct=size_pct,
            entry_price=event.price,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=None,  # paper exits via VWAP cross / time
            reason=f"vwap_trend_{side}_buf{self.CROSS_BUFFER_PCT}",
            metadata={
                "vwap": vwap,
                "confirm_tf": self.CONFIRM_TF,
                "confirm_close": confirm.close,
                "dist_pct": dist_pct,
                "buffer_pct": self.CROSS_BUFFER_PCT,
                "atr": atr,
                "stop_loss_pct": stop_loss_pct,
            },
        )

    # ------------------------------------------------------------------
    # Exit
    # ------------------------------------------------------------------

    def on_position(self, position: Position, event: MarketEvent) -> Optional[ExitSignal]:
        if not self.ENABLED:
            return None

        state = self._get_state(position.symbol)
        self._ingest_candles(state, event)

        hold_ms = event.timestamp_ms - position.entry_time_ms
        if hold_ms >= self.MAX_HOLD_MS:
            return self._exit(position, f"max_hold_{self.MAX_HOLD_HOURS}h", 0.8)

        if self.CLOSE_ON_UTC_ROLLOVER and self._crossed_utc_midnight(
            position.entry_time_ms, event.timestamp_ms
        ):
            return self._exit(position, "utc_day_rollover", 0.75)

        confirm = self._confirm_candle(state)
        if confirm is None:
            return None

        session_bars = self._session_bars(state, confirm.timestamp_ms)
        if len(session_bars) < 1:
            return None
        vwap = calculate_anchored_vwap(session_bars, anchor="utc_day")
        if vwap is None or vwap <= 0:
            return None

        # Stop: confirm-TF close on the opposite side of VWAP (with buffer)
        if position.side == "long":
            opposite = confirm.close < vwap * (1.0 - self.CROSS_BUFFER_PCT)
        else:
            opposite = confirm.close > vwap * (1.0 + self.CROSS_BUFFER_PCT)

        if opposite:
            state.last_exit_ms = event.timestamp_ms
            state.last_exit_side = position.side
            return self._exit(
                position,
                f"vwap_opposite_close_{self.CONFIRM_TF}",
                0.85,
                extra={"vwap": vwap, "confirm_close": confirm.close},
            )

        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_state(self, symbol: str) -> _VWAPTrendState:
        if symbol not in self._state:
            self._state[symbol] = _VWAPTrendState()
        return self._state[symbol]

    def _ingest_candles(self, state: _VWAPTrendState, event: MarketEvent) -> None:
        if event.candle_1m and (
            not state.candles_1m
            or state.candles_1m[-1].timestamp_ms != event.candle_1m.timestamp_ms
        ):
            state.candles_1m.append(event.candle_1m)
        if event.candle_5m and (
            not state.candles_5m
            or state.candles_5m[-1].timestamp_ms != event.candle_5m.timestamp_ms
        ):
            state.candles_5m.append(event.candle_5m)
        if event.candle_15m and (
            not state.candles_15m
            or state.candles_15m[-1].timestamp_ms != event.candle_15m.timestamp_ms
        ):
            state.candles_15m.append(event.candle_15m)
        if event.candle_1h and (
            not state.candles_1h
            or state.candles_1h[-1].timestamp_ms != event.candle_1h.timestamp_ms
        ):
            state.candles_1h.append(event.candle_1h)

    def _confirm_candle(self, state: _VWAPTrendState) -> Optional[Candle]:
        if self.CONFIRM_TF == "5m":
            return state.candles_5m[-1] if state.candles_5m else None
        return state.candles_15m[-1] if state.candles_15m else None

    def _session_bars(self, state: _VWAPTrendState, asof_ms: int) -> List[Candle]:
        """Bars of the confirm TF belonging to the UTC day of *asof_ms*."""
        day0 = (asof_ms // 86_400_000) * 86_400_000
        day1 = day0 + 86_400_000
        src = state.candles_5m if self.CONFIRM_TF == "5m" else state.candles_15m
        return [c for c in src if day0 <= c.timestamp_ms < day1]

    def _side_from_close(self, close: float, vwap: float) -> Optional[str]:
        upper = vwap * (1.0 + self.CROSS_BUFFER_PCT)
        lower = vwap * (1.0 - self.CROSS_BUFFER_PCT)
        if close > upper:
            return "long"
        if close < lower:
            return "short"
        return None  # inside dead band — no flip

    def _anti_flip_ok(
        self, state: _VWAPTrendState, side: str, now_ms: int
    ) -> bool:
        """Block rapid opposite-side entries after a recent signal/exit."""
        last_side = state.last_signal_side
        if last_side is None:
            return True
        if side == last_side:
            # Same-side re-entry: still respect throttle (handled upstream)
            # but allow after flip-interval from last exit of opposite.
            if (
                state.last_exit_side
                and state.last_exit_side != side
                and now_ms - state.last_exit_ms < self.MIN_FLIP_INTERVAL_MS
            ):
                return False
            return True
        # Opposite side = flip
        ref_ms = max(state.last_signal_ms, state.last_exit_ms)
        if now_ms - ref_ms < self.MIN_FLIP_INTERVAL_MS:
            return False
        return True

    def _in_session(self, timestamp_ms: int) -> bool:
        if not self.USE_SESSION_FILTER:
            return True
        hour = _dt.datetime.fromtimestamp(
            timestamp_ms / 1000.0, tz=_dt.timezone.utc
        ).hour
        if self.SESSION_HOURS is not None:
            return hour in self.SESSION_HOURS
        start = self.SESSION_START_UTC_H
        end = self.SESSION_END_UTC_H
        if start == end:
            return True
        if start < end:
            return start <= hour < end
        # Wrap past midnight (e.g. Asia 22→06)
        return hour >= start or hour < end

    @staticmethod
    def _crossed_utc_midnight(entry_ms: int, now_ms: int) -> bool:
        return (entry_ms // 86_400_000) != (now_ms // 86_400_000)

    def _exit(
        self,
        position: Position,
        reason: str,
        confidence: float,
        extra: Optional[Dict[str, Any]] = None,
    ) -> ExitSignal:
        return ExitSignal(
            strategy=self.name,
            symbol=position.symbol,
            side=position.side,
            confidence=confidence,
            reason=reason,
            metadata=extra or {},
        )

    def on_candle(self, candle: Candle, symbol: str) -> None:
        """Optional hook for completed candles (mirrors VWAPDeviation)."""
        state = self._get_state(symbol)
        # Heuristic by bar duration is unavailable; stash on 15m deque as fallback.
        if not state.candles_15m or state.candles_15m[-1].timestamp_ms != candle.timestamp_ms:
            state.candles_15m.append(candle)
