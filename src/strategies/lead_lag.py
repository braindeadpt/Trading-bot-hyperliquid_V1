"""Strategy: LeadLag — short-term directional arb when Binance leads Hyperliquid.

Binance price source
--------------------
Live/paper: ``MarketEvent.binance_mid`` and ``MarketEvent.binance_timestamp_ms``,
injected by the trading engine from ``BinanceMidTick`` on DataBus topic
``binance_price:{symbol}``. The tick originates from Binance spot ``@aggTrade``
(via :mod:`src.exchanges.binance_price_bridge`).

Assumed latency: Binance WS ~50–200 ms; HL ``allMids`` similar. The edge window
is HL catching up over roughly 1–5 seconds after a Binance impulse — not funding
dependent.

Backtest: no Binance feed — strategy skips when ``binance_mid`` is absent.
"""

from __future__ import annotations

import collections
import logging
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

from src.strategies.base import ExitSignal, MarketEvent, Position, Signal, Strategy
from src.utils.helpers import safe_divide, safe_float

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PriceSample:
    ts_ms: int
    mid: float


@dataclass
class _LeadLagState:
    """Per-symbol rolling mids for impulse measurement."""

    hl_samples: Deque[_PriceSample] = field(default_factory=collections.deque)
    bn_samples: Deque[_PriceSample] = field(default_factory=collections.deque)
    last_signal_ms: int = 0
    warmup_logged: bool = False


class LeadLag(Strategy):
    """Exploit Binance → Hyperliquid price lag on short impulses.

    Entry: Binance impulse exceeds threshold while HL mid has not caught up
    (gap beyond fees + spread). Exit: gap convergence, time-stop, impulse
    reversal, or engine hard stop/take-profit.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = config or {}
        self.IMPULSE_WINDOW_MS = int(cfg.get("impulse_window_ms", 10_000))
        self.IMPULSE_MIN_PCT = float(cfg.get("impulse_min_pct", 0.0005))
        self.GAP_MIN_PCT = float(cfg.get("gap_min_pct", 0.0003))
        self.MIN_EDGE_BUFFER_PCT = float(cfg.get("min_edge_buffer_pct", 0.0002))
        self.TAKER_FEE_PCT = float(cfg.get("taker_fee_pct", 0.00035))
        self.MAX_HL_SPREAD_PCT = float(cfg.get("max_hl_spread_pct", 0.0008))
        self.REQUIRE_OIR_CONFIRM = bool(cfg.get("require_oir_confirm", True))
        self.OIR_THRESHOLD = float(cfg.get("oir_threshold", 0.10))
        self.CONVERGENCE_PCT = float(cfg.get("convergence_pct", 0.0001))
        self.STOP_LOSS_PCT = float(cfg.get("stop_loss_pct", 0.003))
        self.MAX_HOLD_MS = int(cfg.get("max_hold_seconds", 90)) * 1000
        self.BASE_SIZE_PCT = float(cfg.get("base_size_pct", 0.005))
        self.MAX_SIZE_PCT = float(cfg.get("max_size_pct", 0.008))
        self.MIN_CONFIDENCE = float(cfg.get("min_confidence", 0.45))
        self.MAX_CONFIDENCE = float(cfg.get("max_confidence", 0.85))
        self.SIGNAL_THROTTLE_MS = int(cfg.get("signal_throttle_ms", 30_000))
        self.BUFFER_MS = int(cfg.get("buffer_ms", 30_000))
        self.MAX_BINANCE_STALE_MS = int(cfg.get("max_binance_stale_ms", 2_000))
        self.IMPULSE_REVERSAL_PCT = float(cfg.get("impulse_reversal_pct", 0.0003))
        self.WARMUP_SAMPLES = int(cfg.get("warmup_samples", 5))

        self._state: Dict[str, _LeadLagState] = {}

    @property
    def name(self) -> str:
        return "LeadLag"

    def _get_state(self, symbol: str) -> _LeadLagState:
        if symbol not in self._state:
            self._state[symbol] = _LeadLagState()
        return self._state[symbol]

    def _prune(self, samples: Deque[_PriceSample], now_ms: int) -> None:
        cutoff = now_ms - self.BUFFER_MS
        while samples and samples[0].ts_ms < cutoff:
            samples.popleft()

    def _append_sample(
        self,
        samples: Deque[_PriceSample],
        ts_ms: int,
        mid: float,
    ) -> None:
        if mid <= 0:
            return
        if samples and samples[-1].ts_ms == ts_ms:
            samples[-1] = _PriceSample(ts_ms=ts_ms, mid=mid)
        else:
            samples.append(_PriceSample(ts_ms=ts_ms, mid=mid))

    def _impulse_pct(self, samples: Deque[_PriceSample], now_ms: int) -> Optional[float]:
        if len(samples) < 2:
            return None
        current = samples[-1].mid
        target_ts = now_ms - self.IMPULSE_WINDOW_MS
        anchor: Optional[_PriceSample] = None
        for sample in samples:
            if sample.ts_ms <= target_ts:
                anchor = sample
            else:
                break
        if anchor is None:
            anchor = samples[0]
        if anchor.mid <= 0:
            return None
        return safe_divide(current - anchor.mid, anchor.mid, 0.0)

    @staticmethod
    def _gap_pct(hl_mid: float, bn_mid: float) -> float:
        return safe_divide(hl_mid - bn_mid, bn_mid, 0.0)

    def _min_net_gap_pct(self, hl_spread_pct: Optional[float]) -> float:
        spread = safe_float(hl_spread_pct, 0.0)
        if spread < 0:
            spread = 0.0
        rt_fee = 2.0 * self.TAKER_FEE_PCT
        return self.GAP_MIN_PCT + spread + rt_fee + self.MIN_EDGE_BUFFER_PCT

    def _size_pct(self, confidence: float) -> float:
        span = max(self.MAX_SIZE_PCT - self.BASE_SIZE_PCT, 0.0)
        return min(self.BASE_SIZE_PCT + span * confidence, self.MAX_SIZE_PCT)

    def _confidence(self, impulse_pct: float, gap_pct: float, min_gap: float) -> float:
        impulse_score = min(abs(impulse_pct) / max(self.IMPULSE_MIN_PCT, 1e-9), 1.0)
        gap_excess = max(abs(gap_pct) - min_gap, 0.0)
        gap_score = min(gap_excess / max(self.GAP_MIN_PCT, 1e-9), 1.0)
        raw = (impulse_score + gap_score) / 2.0
        span = self.MAX_CONFIDENCE - self.MIN_CONFIDENCE
        return min(self.MIN_CONFIDENCE + span * raw, self.MAX_CONFIDENCE)

    def _oir_confirms(self, side: str, oir: Optional[float]) -> bool:
        if not self.REQUIRE_OIR_CONFIRM:
            return True
        if oir is None:
            return False
        if side == "long":
            return oir >= self.OIR_THRESHOLD
        return oir <= -self.OIR_THRESHOLD

    def _update_buffers(
        self,
        event: MarketEvent,
        bn_mid: float,
        bn_ts: int,
    ) -> _LeadLagState:
        state = self._get_state(event.symbol)
        now_ms = event.timestamp_ms
        self._append_sample(state.hl_samples, now_ms, event.price)
        self._append_sample(state.bn_samples, bn_ts, bn_mid)
        self._prune(state.hl_samples, now_ms)
        self._prune(state.bn_samples, now_ms)
        return state

    def on_data(self, event: MarketEvent) -> Optional[Signal]:
        bn_mid = event.binance_mid
        bn_ts = event.binance_timestamp_ms
        if bn_mid is None or bn_ts is None or bn_mid <= 0:
            return None

        now_ms = event.timestamp_ms
        if now_ms - bn_ts > self.MAX_BINANCE_STALE_MS:
            logger.debug(
                "LeadLag SKIP %s — stale Binance tick (%dms old)",
                event.symbol,
                now_ms - bn_ts,
            )
            return None

        state = self._update_buffers(event, bn_mid, bn_ts)

        if (
            len(state.hl_samples) < self.WARMUP_SAMPLES
            or len(state.bn_samples) < self.WARMUP_SAMPLES
        ):
            if not state.warmup_logged:
                logger.info(
                    "LeadLag %s WARM-UP: hl=%d/%d bn=%d/%d samples",
                    event.symbol,
                    len(state.hl_samples),
                    self.WARMUP_SAMPLES,
                    len(state.bn_samples),
                    self.WARMUP_SAMPLES,
                )
                state.warmup_logged = True
            return None

        if now_ms - state.last_signal_ms < self.SIGNAL_THROTTLE_MS:
            return None

        spread = event.orderbook_spread_pct
        if spread is None or spread > self.MAX_HL_SPREAD_PCT:
            logger.debug(
                "LeadLag SKIP %s — HL spread too wide (%.4f%% > %.4f%%)",
                event.symbol,
                (spread or -1.0) * 100.0,
                self.MAX_HL_SPREAD_PCT * 100.0,
            )
            return None

        impulse = self._impulse_pct(state.bn_samples, now_ms)
        if impulse is None:
            return None

        gap = self._gap_pct(event.price, bn_mid)
        min_gap = self._min_net_gap_pct(spread)

        side: Optional[str] = None
        if impulse >= self.IMPULSE_MIN_PCT and gap <= -min_gap:
            side = "long"
        elif impulse <= -self.IMPULSE_MIN_PCT and gap >= min_gap:
            side = "short"

        if side is None:
            return None

        if not self._oir_confirms(side, event.orderbook_oir):
            logger.debug(
                "LeadLag SKIP %s %s — OIR not aligned (oir=%s)",
                event.symbol,
                side,
                event.orderbook_oir,
            )
            return None

        confidence = self._confidence(impulse, gap, min_gap)
        if confidence < self.MIN_CONFIDENCE:
            return None

        size_pct = self._size_pct(confidence)
        take_profit_pct = max(abs(gap) - self.CONVERGENCE_PCT, self.CONVERGENCE_PCT)

        state.last_signal_ms = now_ms
        logger.info(
            "LeadLag %s SIGNAL %s — impulse=%.4f%% gap=%.4f%% "
            "min_gap=%.4f%% conf=%.2f size=%.3f%%",
            event.symbol,
            side,
            impulse * 100.0,
            gap * 100.0,
            min_gap * 100.0,
            confidence,
            size_pct * 100.0,
        )

        return Signal(
            strategy=self.name,
            symbol=event.symbol,
            side=side,
            confidence=confidence,
            size_pct=size_pct,
            entry_price=event.price,
            stop_loss_pct=self.STOP_LOSS_PCT,
            take_profit_pct=take_profit_pct,
            reason=f"lead_lag_{side}_imp{impulse * 100:.3f}_gap{gap * 100:.3f}",
            metadata={
                "entry_gap_pct": gap,
                "entry_impulse_pct": impulse,
                "entry_bn_mid": bn_mid,
                "entry_hl_mid": event.price,
                "min_net_gap_pct": min_gap,
                "convergence_pct": self.CONVERGENCE_PCT,
                "take_profit_r": safe_divide(take_profit_pct, self.STOP_LOSS_PCT, 0.0),
            },
        )

    def on_position(self, position: Position, event: MarketEvent) -> Optional[ExitSignal]:
        meta = position.metadata or {}
        if meta.get("original_strategy") not in (None, self.name) and meta.get("strategy") != self.name:
            if meta.get("sub_strategy") != self.name:
                return None

        bn_mid = event.binance_mid
        if bn_mid is None or bn_mid <= 0:
            return None

        now_ms = event.timestamp_ms
        hold_ms = now_ms - position.entry_time_ms
        gap = self._gap_pct(event.price, bn_mid)
        entry_gap = safe_float(meta.get("entry_gap_pct"), gap)

        if abs(gap) <= self.CONVERGENCE_PCT:
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side="close",
                confidence=0.9,
                reason=f"gap_converged_{gap * 100:.3f}pct",
                metadata={"gap_pct": gap, "hold_ms": hold_ms},
            )

        if hold_ms >= self.MAX_HOLD_MS:
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side="close",
                confidence=0.75,
                reason=f"time_stop_{hold_ms // 1000}s",
                metadata={"gap_pct": gap, "hold_ms": hold_ms},
            )

        state = self._get_state(position.symbol)
        self._update_buffers(event, bn_mid, event.binance_timestamp_ms or now_ms)
        impulse = self._impulse_pct(state.bn_samples, now_ms)
        if impulse is not None:
            if position.side == "long" and impulse <= -self.IMPULSE_REVERSAL_PCT:
                return ExitSignal(
                    strategy=self.name,
                    symbol=position.symbol,
                    side="close",
                    confidence=0.8,
                    reason="binance_impulse_reversal",
                    metadata={"impulse_pct": impulse, "gap_pct": gap},
                )
            if position.side == "short" and impulse >= self.IMPULSE_REVERSAL_PCT:
                return ExitSignal(
                    strategy=self.name,
                    symbol=position.symbol,
                    side="close",
                    confidence=0.8,
                    reason="binance_impulse_reversal",
                    metadata={"impulse_pct": impulse, "gap_pct": gap},
                )

        # Early exit if gap moved past convergence toward overshoot vs entry
        if position.side == "long" and gap >= 0 and entry_gap < 0:
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side="close",
                confidence=0.85,
                reason="gap_overshoot_long",
                metadata={"gap_pct": gap},
            )
        if position.side == "short" and gap <= 0 and entry_gap > 0:
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side="close",
                confidence=0.85,
                reason="gap_overshoot_short",
                metadata={"gap_pct": gap},
            )

        return None
