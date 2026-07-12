"""Strategy 7: CVD OrderFlow — Cumulative Volume Delta divergence.

Computes rolling CVD from per-bar (buy_volume - sell_volume) deltas and
fires reversal signals when price moves are not supported by the order
flow side that should be pushing them.

Signal types
------------
* Bullish divergence:  price falling but CVD rising (sellers exhausted).
  -> LONG, expect mean-reversion bounce.
* Bearish divergence:  price rising but CVD falling (buyers exhausted).
  -> SHORT, expect mean-reversion drop.

Multi-timeframe confirmation:  divergence must align across the 5m,
15m and 1h rolling windows for the signal to fire.  Single-window
divergence is treated as noise.

Filters
-------
* Minimum traded USD notional in the window (avoids thin books).
* ADX in 15-30 band:  too low => dead market;  too high => trend
  continuation breaks the divergence.
* OIR (orderbook imbalance) confirmation:  optional.

Risk
----
* Stop = ATR(1h) * 2.0  (capped to 0.5% - 5%).
* TP   = stop * 2R  (default).
* Max hold 6h.
* Size 0.8% - 2.0% of capital, scaled with divergence strength.

References
----------
* "Cumulative Volume Delta" — standard tape-reading concept, used by
  Bookmap, Sierra Chart, ATAS, Order Flow trading communities.
* See docs/ARCHITECTURE.md for data flow (candle builder already
  tracks buy_volume / sell_volume per 1m bar).
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

from src.strategies.base import (
    ExitSignal,
    MarketEvent,
    Position,
    Signal,
    Strategy,
)
from src.strategies.indicators import Candle, calculate_atr

logger = logging.getLogger(__name__)


@dataclass
class _BarDelta:
    """Per-minute delta derived from a 1m candle's buy/sell volume."""
    timestamp_ms: int
    price_close: float
    delta: float       # buy_volume - sell_volume (signed)
    total_volume: float
    buy_volume: float
    sell_volume: float


@dataclass
class _CVDState:
    """Per-symbol state for CVD strategy."""
    bars_1m: Deque[_BarDelta] = field(default_factory=lambda: deque(maxlen=240))  # 4h
    candles_1h: Deque[Candle] = field(default_factory=lambda: deque(maxlen=50))
    last_signal_ms: int = 0
    _last_skip_log_ms: int = 0
    _last_no_signal_log_ms: int = 0
    _last_warmup_log_ms: int = 0


@dataclass(frozen=True)
class _WindowStats:
    """Output of _compute_window_stats:  aggregate over a rolling window."""
    cvd: float
    price_change_pct: float
    volume: float
    divergence: float  # signed: + = bullish div, - = bearish div, 0 = no div


class CVDOrderFlow(Strategy):
    """CVD Divergence detector — fades price moves lacking volume support.

    The strategy only triggers when ALL three rolling windows (5m, 15m, 1h)
    show divergence in the same direction with sufficient magnitude.
    """

    # Window sizes expressed in 1m bars
    DEFAULT_WINDOW_SHORT = 5
    DEFAULT_WINDOW_MEDIUM = 15
    DEFAULT_WINDOW_LONG = 60

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = config or {}

        # ── Windows (1m bars) ────────────────────────────────────────
        self.WINDOW_SHORT = int(cfg.get("window_short_bars", self.DEFAULT_WINDOW_SHORT))
        self.WINDOW_MED = int(cfg.get("window_medium_bars", self.DEFAULT_WINDOW_MEDIUM))
        self.WINDOW_LONG = int(cfg.get("window_long_bars", self.DEFAULT_WINDOW_LONG))

        # ── Divergence thresholds ────────────────────────────────────
        # Signed divergence must exceed this magnitude in the medium window.
        # Range typically 0.0 - 1.0;  higher => stronger signal.
        self.MIN_DIVERGENCE = float(cfg.get("min_divergence_strength", 0.35))
        # Price must have moved at least this much in the medium window.
        self.MIN_PRICE_MOVE = float(cfg.get("min_price_move_pct", 0.002))  # 0.2%
        # Minimum total volume in the medium window (USD notional estimate).
        self.MIN_VOLUME_USD = float(cfg.get("min_volume_usd", 50_000.0))

        # ── Regime filters ───────────────────────────────────────────
        # ADX in band:  too high (strong trend) breaks divergence;  too low = dead.
        self.MAX_ADX = float(cfg.get("max_adx", 30.0))
        self.MIN_ADX = float(cfg.get("min_adx", 12.0))
        # OIR confirmation:  book imbalance must align with the signal.
        self.REQUIRE_OIR_CONFIRM = bool(cfg.get("require_oir_confirm", True))
        self.OIR_THRESHOLD = float(cfg.get("oir_threshold", 0.15))

        # ── Risk management ──────────────────────────────────────────
        self.ATR_PERIOD = int(cfg.get("atr_period", 14))
        self.STOP_ATR_MULT = float(cfg.get("stop_loss_atr_multiplier", 2.0))
        self.TP_R_MULT = float(cfg.get("take_profit_r_multiple", 2.0))
        self.MAX_HOLD_HOURS = float(cfg.get("max_hold_hours", 6.0))
        self.MAX_HOLD_MS = int(self.MAX_HOLD_HOURS * 3_600_000)
        self.STOP_LOSS_FLOOR = float(cfg.get("stop_loss_floor_pct", 0.005))   # 0.5%
        self.STOP_LOSS_CEIL = float(cfg.get("stop_loss_ceiling_pct", 0.05))     # 5.0%

        # ── Sizing & confidence ──────────────────────────────────────
        self.BASE_SIZE_PCT = float(cfg.get("base_size_pct", 0.008))
        self.MAX_SIZE_PCT = float(cfg.get("max_size_pct", 0.02))
        self.MIN_CONFIDENCE = float(cfg.get("min_confidence", 0.60))
        self.CONFIDENCE_STRONG = float(cfg.get("confidence_strong", 0.85))

        # ── Throttling & logging ─────────────────────────────────────
        self.SIGNAL_THROTTLE_MS = int(cfg.get("signal_throttle_ms", 600_000))  # 10 min
        self.SKIP_LOG_INTERVAL_MS = int(cfg.get("skip_log_interval_ms", 300_000))  # 5 min

        self.MANUAL_ENABLED = bool(cfg.get("enabled", True))
        self._state: Dict[str, _CVDState] = {}

    # ── Public API ─────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "CVDOrderFlow"

    def is_active(self) -> bool:
        """Always on when enabled; governor may disable externally."""
        return self.MANUAL_ENABLED

    # ── Helpers ────────────────────────────────────────────────────

    def _get_state(self, symbol: str) -> _CVDState:
        if symbol not in self._state:
            self._state[symbol] = _CVDState()
        return self._state[symbol]

    def _update_candle_1h(self, state: _CVDState, event: MarketEvent) -> None:
        """Append completed 1h candle to history (deduplicated by timestamp)."""
        c = event.candle_1h
        if c is None:
            return
        ts = int(getattr(c, "timestamp_ms", event.timestamp_ms))
        if state.candles_1h and state.candles_1h[-1].timestamp_ms == ts:
            return
        state.candles_1h.append(c)

    def _compute_stop_loss_pct(self, state: _CVDState, price: float) -> float:
        """ATR(1h, 14) stop as fraction of price, clamped to floor/ceiling."""
        atr: Optional[float] = None
        candles = list(state.candles_1h)
        if len(candles) >= self.ATR_PERIOD + 1:
            atr = calculate_atr(candles[-(self.ATR_PERIOD + 1):], period=self.ATR_PERIOD)

        if atr is not None and atr > 0.0 and price > 0.0:
            stop_loss_pct = (atr * self.STOP_ATR_MULT) / price
        else:
            stop_loss_pct = 0.015  # 1.5% fallback until 15+ 1h bars

        return max(self.STOP_LOSS_FLOOR, min(stop_loss_pct, self.STOP_LOSS_CEIL))

    @staticmethod
    def _extract_bar(event: MarketEvent) -> Optional[_BarDelta]:
        """Pull (delta, volume, price) from the event's 1m candle.

        v3.1.14 fix: candle.volume from Hyperliquid is in *token units* (BTC,
        ETH, SOL), not USD. The volume gate (``MIN_VOLUME_USD``) is denominated
        in USD, so we must convert: ``volume_usd = token_volume * close_price``.
        For BTC at $100k, 1 token = $100k. For SOL at $150, 1 token = $150.
        Without this conversion, ``stats_m.volume < 50_000`` was almost always
        True for BTC (token volumes 100-500) and intermittently True for SOL,
        silently blocking valid divergence signals.
        """
        c = event.candle_1m
        if c is None:
            return None
        buy = getattr(c, "buy_volume", None)
        sell = getattr(c, "sell_volume", None)
        if buy is None or sell is None:
            return None
        buy = float(buy)
        sell = float(sell)
        total_tokens = float(getattr(c, "volume", buy + sell) or (buy + sell))
        close_price = float(c.close)
        if close_price <= 0.0:
            return None
        # Convert token units -> USD so MIN_VOLUME_USD gate is in apples-to-apples units.
        total_usd = total_tokens * close_price
        buy_usd = buy * close_price
        sell_usd = sell * close_price
        # v3.1.16 fix: delta must be in the same unit basis as volume (USD) so
        # cvd_magnitude = |cvd| / avg_volume_USD is a dimensionless ratio that
        # can plausibly exceed min_divergence_strength. Previously delta was in
        # token units (e.g. 40 BTC) while volume was in USD (~$4M for the same
        # bar), making the ratio always ~15000x too small to ever fire.
        delta_usd = (buy - sell) * close_price
        ts = int(getattr(c, "timestamp_ms", event.timestamp_ms))
        return _BarDelta(
            timestamp_ms=ts,
            price_close=close_price,
            delta=delta_usd,
            total_volume=total_usd,
            buy_volume=buy_usd,
            sell_volume=sell_usd,
        )

    def _window_stats(self, bars: Deque[_BarDelta], window: int) -> Optional[_WindowStats]:
        """Compute CVD, price change, and signed divergence over *window* bars.

        Divergence is *signed*:
            +X  bullish divergence   (CVD > 0, price < 0)  => fade price down
            -X  bearish divergence   (CVD < 0, price > 0)  => fade price up
            0   no divergence        (same direction or zero)

        Magnitude normalized as min(|CVD|/avgVol, |price_change|*100) so
        the two axes are in roughly comparable units.
        """
        if len(bars) < window:
            return None

        recent = list(bars)[-window:]
        cvd = sum(b.delta for b in recent)
        volume = sum(b.total_volume for b in recent)

        first_close = recent[0].price_close
        last_close = recent[-1].price_close
        if first_close <= 0.0 or volume <= 0.0:
            return None

        price_change_pct = (last_close - first_close) / first_close

        # Divergence: only when signs disagree
        cvd_sign = 1 if cvd > 0 else (-1 if cvd < 0 else 0)
        price_sign = 1 if price_change_pct > 0 else (-1 if price_change_pct < 0 else 0)
        if cvd_sign == 0 or price_sign == 0 or cvd_sign == price_sign:
            return _WindowStats(
                cvd=cvd, price_change_pct=price_change_pct,
                volume=volume, divergence=0.0,
            )

        avg_volume = volume / window
        cvd_magnitude = abs(cvd) / avg_volume           # ~fraction of typical bar
        price_magnitude = abs(price_change_pct) * 100.0  # 1% move => 1.0
        raw_strength = min(cvd_magnitude, price_magnitude)

        # Sign convention:  bullish div => +raw;  bearish => -raw
        return _WindowStats(
            cvd=cvd, price_change_pct=price_change_pct,
            volume=volume, divergence=raw_strength * cvd_sign,
        )

    @staticmethod
    def _aligns(div_a: float, div_b: float, div_c: float, side: str) -> bool:
        """True if all three windows show divergence in the same direction.

        For LONG:  all three must be positive.
        For SHORT: all three must be negative.
        """
        if side == "long":
            return div_a > 0.0 and div_b > 0.0 and div_c > 0.0
        return div_a < 0.0 and div_b < 0.0 and div_c < 0.0

    # ── Entry logic ────────────────────────────────────────────────

    def on_data(self, event: MarketEvent) -> Optional[Signal]:
        if not self.MANUAL_ENABLED:
            return None

        bar = self._extract_bar(event)
        if bar is None:
            return None

        state = self._get_state(event.symbol)
        self._update_candle_1h(state, event)

        # Deduplicate by timestamp (candle_complete fires once per bar).
        if state.bars_1m and state.bars_1m[-1].timestamp_ms == bar.timestamp_ms:
            return None
        state.bars_1m.append(bar)

        # Throttle signals
        if event.timestamp_ms - state.last_signal_ms < self.SIGNAL_THROTTLE_MS:
            return None

        # Warm-up
        if len(state.bars_1m) < self.WINDOW_LONG:
            if event.timestamp_ms - state._last_warmup_log_ms > self.SKIP_LOG_INTERVAL_MS:
                state._last_warmup_log_ms = event.timestamp_ms
                logger.info(
                    "CVDOrderFlow %s WARM-UP: %d/%d 1m bars collected",
                    event.symbol, len(state.bars_1m), self.WINDOW_LONG,
                )
            return None

        # Compute stats for the three windows
        stats_s = self._window_stats(state.bars_1m, self.WINDOW_SHORT)
        stats_m = self._window_stats(state.bars_1m, self.WINDOW_MED)
        stats_l = self._window_stats(state.bars_1m, self.WINDOW_LONG)
        if stats_m is None or stats_s is None or stats_l is None:
            return None

        # Primary signal: medium window
        if abs(stats_m.divergence) < self.MIN_DIVERGENCE:
            self._maybe_log_no_signal(event, state, stats_m)
            return None

        if abs(stats_m.price_change_pct) < self.MIN_PRICE_MOVE:
            self._maybe_log_no_signal(event, state, stats_m, reason="price_move_too_small")
            return None

        # Volume gate
        if stats_m.volume < self.MIN_VOLUME_USD:
            self._maybe_log_no_signal(event, state, stats_m, reason="volume_too_low")
            return None

        # Determine direction from medium window
        target_side = "long" if stats_m.divergence > 0 else "short"

        # Multi-timeframe confirmation:  all three windows must agree
        if not self._aligns(stats_s.divergence, stats_m.divergence, stats_l.divergence, target_side):
            self._maybe_log_no_signal(
                event, state, stats_m,
                reason=f"mtf_misalign_s={stats_s.divergence:+.2f}_l={stats_l.divergence:+.2f}",
            )
            return None

        # ── Filters ────────────────────────────────────────────────

        adx = event.adx_14
        if adx is not None:
            if adx > self.MAX_ADX:
                self._maybe_log_no_signal(
                    event, state, stats_m, reason=f"adx_high_{adx:.1f}",
                )
                return None
            if adx < self.MIN_ADX:
                self._maybe_log_no_signal(
                    event, state, stats_m, reason=f"adx_low_{adx:.1f}",
                )
                return None

        if self.REQUIRE_OIR_CONFIRM:
            oir = event.orderbook_oir
            if oir is not None:
                if target_side == "long" and oir < -self.OIR_THRESHOLD:
                    self._maybe_log_no_signal(
                        event, state, stats_m, reason=f"oir_neg_{oir:+.2f}",
                    )
                    return None
                if target_side == "short" and oir > self.OIR_THRESHOLD:
                    self._maybe_log_no_signal(
                        event, state, stats_m, reason=f"oir_pos_{oir:+.2f}",
                    )
                    return None

        # ── Confidence & size ──────────────────────────────────────

        div_strength = min(abs(stats_m.divergence), 1.0)
        confidence = self.MIN_CONFIDENCE + div_strength * (
            self.CONFIDENCE_STRONG - self.MIN_CONFIDENCE
        )
        confidence = min(confidence, self.CONFIDENCE_STRONG)

        size_pct = self.BASE_SIZE_PCT + div_strength * (
            self.MAX_SIZE_PCT - self.BASE_SIZE_PCT
        )
        size_pct = min(size_pct, self.MAX_SIZE_PCT)

        # ── ATR-based stop loss (14 × 1h bars) ─────────────────────
        stop_loss_pct = self._compute_stop_loss_pct(state, event.price)
        take_profit_pct = stop_loss_pct * self.TP_R_MULT

        state.last_signal_ms = event.timestamp_ms

        logger.info(
            "CVDOrderFlow %s signal for %s — price_chg=%.2f%%, "
            "cvd_m=%+.0f, div_s=%+.2f div_m=%+.2f div_l=%+.2f, "
            "adx=%s, conf=%.2f, size=%.2f%%",
            target_side, event.symbol,
            stats_m.price_change_pct * 100.0,
            stats_m.cvd,
            stats_s.divergence, stats_m.divergence, stats_l.divergence,
            f"{adx:.1f}" if adx is not None else "N/A",
            confidence, size_pct * 100.0,
        )

        return Signal(
            strategy=self.name,
            symbol=event.symbol,
            side=target_side,
            confidence=confidence,
            size_pct=size_pct,
            entry_price=event.price,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            reason=f"cvd_div_{target_side}_str{stats_m.divergence:+.2f}",
            metadata={
                "cvd_short": stats_s.cvd,
                "cvd_medium": stats_m.cvd,
                "cvd_long": stats_l.cvd,
                "div_short": stats_s.divergence,
                "div_medium": stats_m.divergence,
                "div_long": stats_l.divergence,
                "price_change_pct": stats_m.price_change_pct,
                "adx": adx,
                "stop_loss_pct": stop_loss_pct,
                "take_profit_pct": take_profit_pct,
                "window_short_bars": self.WINDOW_SHORT,
                "window_medium_bars": self.WINDOW_MED,
                "window_long_bars": self.WINDOW_LONG,
            },
        )

    # ── Exit logic ─────────────────────────────────────────────────

    def on_position(self, position: Position, event: MarketEvent) -> Optional[ExitSignal]:
        # Time-based exit
        hold_time = event.timestamp_ms - position.entry_time_ms
        if hold_time >= self.MAX_HOLD_MS:
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side="close",
                confidence=0.8,
                reason=f"max_hold_{self.MAX_HOLD_HOURS:g}h",
            )

        # TP / SL based on the position's recorded levels (if present)
        entry = position.entry_price
        current = event.price
        if entry <= 0.0 or current <= 0.0:
            return None

        if position.side == "long":
            pnl_pct = (current - entry) / entry
        else:
            pnl_pct = (entry - current) / entry

        meta = position.metadata or {}
        tp = float(meta.get("take_profit_pct") or 0.0)
        sl = float(meta.get("stop_loss_pct") or 0.0)

        if tp > 0.0 and pnl_pct >= tp:
            return ExitSignal(
                strategy=self.name, symbol=position.symbol, side="close",
                confidence=0.85, reason=f"take_profit_{pnl_pct*100:.2f}%",
            )
        if sl > 0.0 and pnl_pct <= -sl:
            return ExitSignal(
                strategy=self.name, symbol=position.symbol, side="close",
                confidence=0.95, reason=f"stop_loss_{pnl_pct*100:.2f}%",
            )

        # Opposite divergence:  close early
        state = self._get_state(position.symbol)
        self._update_candle_1h(state, event)
        bar = self._extract_bar(event)
        if bar is not None:
            if state.bars_1m and state.bars_1m[-1].timestamp_ms != bar.timestamp_ms:
                state.bars_1m.append(bar)
            if len(state.bars_1m) >= self.WINDOW_MED:
                stats = self._window_stats(state.bars_1m, self.WINDOW_MED)
                if stats is not None and abs(stats.divergence) > 0.4:
                    if position.side == "long" and stats.divergence < 0.0:
                        return ExitSignal(
                            strategy=self.name, symbol=position.symbol, side="close",
                            confidence=0.7, reason="opposite_bearish_divergence",
                        )
                    if position.side == "short" and stats.divergence > 0.0:
                        return ExitSignal(
                            strategy=self.name, symbol=position.symbol, side="close",
                            confidence=0.7, reason="opposite_bullish_divergence",
                        )

        return None

    # ── Logging helpers ────────────────────────────────────────────

    def _maybe_log_no_signal(
        self,
        event: MarketEvent,
        state: _CVDState,
        stats_m: _WindowStats,
        reason: str = "",
    ) -> None:
        if event.timestamp_ms - state._last_no_signal_log_ms < self.SKIP_LOG_INTERVAL_MS:
            return
        state._last_no_signal_log_ms = event.timestamp_ms
        suffix = f" ({reason})" if reason else ""
        logger.info(
            "CVDOrderFlow %s NO SIGNAL — div=%.2f price_chg=%.2f%% vol_usd=%.0f%s",
            event.symbol,
            stats_m.divergence,
            stats_m.price_change_pct * 100.0,
            stats_m.volume,
            suffix,
        )
