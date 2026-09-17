"""JevJudge — TypeSafe System One judgment-model experiment (PAPER ONLY).

Owner-requested experiment (2026-09-17): let a general-purpose judgment
model (Jev, api.typesafe.ai) emit directional calls that the engine
executes in paper mode, so the experiment's trades show up as REAL open
positions on the dashboard — managed by the normal engine/risk path like
any other strategy.

Architecture (keeps blocking HTTP out of the event loop):
  * scripts/research/jev_shadow_judge.py (scheduled task, every 5 min)
    asks Jev once/hour/symbol and writes the latest verdict to
    ``data/live/jev_latest.json`` — plus full rows in research DB
    ``jev_decisions`` for offline evaluation (jev_eval.py).
  * This strategy only READS that file (mtime-cached, no I/O per tick)
    and edge-triggers one Signal per new decision.

Safety:
  * Hard paper-only guard: emits nothing unless config mode == 'paper'
    (injected as ``_mode`` by the factory). phase08 paper_only and the
    manifest EXPERIMENT verdict are additional layers, not substitutes.
  * Experiments have zero evidence — the manifest gate carries verdict
    EXPERIMENT (not PASS) and the audit docs mark it explicitly.
  * Engine manages SL/TP from the signal's stop/take-profit pct;
    on_position adds a max-hold exit and a flip exit (Jev reversed).

Signal semantics follow VWAPDeviation: stop_loss_pct / take_profit_pct /
size_pct are FRACTIONS (0.01 = 1%).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from src.strategies.base import (
    ExitSignal,
    MarketEvent,
    Position,
    Signal,
    Strategy,
)

logger = logging.getLogger(__name__)

_DEFAULT_LATEST_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "live" / "jev_latest.json"
)


class JevJudge(Strategy):
    """Execute Jev's latest directional verdict as a paper experiment."""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = config or {}
        # Paper-only guard — the factory injects the process run mode as
        # _mode. Fail CLOSED: no explicit mode means disabled, never an
        # assumed-paper default — this guard exists to block real-money
        # order placement, so ambiguity must refuse.
        raw_mode = cfg.get("_mode")
        self._mode = str(raw_mode).lower() if raw_mode is not None else ""
        self._enabled = self._mode == "paper"
        if not self._enabled:
            logger.warning(
                "JevJudge disabled — _mode=%r (paper-only experiment requires "
                "an explicit 'paper' run mode injected by the factory)",
                raw_mode,
            )

        self.LATEST_PATH = Path(
            cfg.get("latest_path") or _DEFAULT_LATEST_PATH
        )
        self.MIN_CONFIDENCE = float(cfg.get("min_confidence", 0.60))
        self.DECISION_TTL_MS = int(cfg.get("decision_ttl_ms", 90 * 60_000))
        self.BASE_SIZE_PCT = float(cfg.get("base_size_pct", 0.01))
        # Exit geometry: SL = max(sl_pct_min, atr_mult * ATR%), TP = 2R
        self.SL_PCT_MIN = float(cfg.get("sl_pct_min", 0.01))
        self.SL_ATR_MULT = float(cfg.get("sl_atr_mult", 2.0))
        self.TP_R_MULT = float(cfg.get("tp_r_mult", 2.0))
        self.MAX_HOLD_MS = int(cfg.get("max_hold_hours", 4) * 3_600_000)
        self.FLIP_EXIT = bool(cfg.get("flip_exit", True))
        self.WARMUP_LOG_MS = int(cfg.get("warmup_log_ms", 300_000))

        # mtime-cached verdict file: {symbol: {ts_ms, action, confidence, ...}}
        self._file_mtime: float = -1.0
        self._verdicts: Dict[str, Dict[str, Any]] = {}
        self._last_signaled: Dict[str, int] = {}  # symbol -> decision ts_ms
        self._last_warn_ms = 0

    @property
    def name(self) -> str:
        return "JevJudge"

    # ------------------------------------------------------------------
    # Verdict file (written by the scheduled judge — never blocks the loop)
    # ------------------------------------------------------------------

    def _verdict(self, symbol: str, now_ms: int) -> Optional[Dict[str, Any]]:
        try:
            mtime = os.stat(self.LATEST_PATH).st_mtime
        except OSError:
            self._maybe_warn(now_ms, f"verdict file missing: {self.LATEST_PATH}")
            return None
        if mtime != self._file_mtime:
            try:
                raw = json.loads(self.LATEST_PATH.read_text(encoding="utf-8"))
                self._verdicts = raw if isinstance(raw, dict) else {}
                self._file_mtime = mtime
            except (OSError, ValueError) as exc:
                self._maybe_warn(now_ms, f"verdict file unreadable: {exc}")
                return None
        v = self._verdicts.get(symbol)
        if not isinstance(v, dict):
            return None
        ts = v.get("ts_ms")
        if not ts or now_ms - int(ts) > self.DECISION_TTL_MS:
            return None
        return v

    def _maybe_warn(self, now_ms: int, msg: str) -> None:
        if now_ms - self._last_warn_ms > self.WARMUP_LOG_MS:
            self._last_warn_ms = now_ms
            logger.info("JevJudge: %s", msg)

    # ------------------------------------------------------------------
    # Entry / exit
    # ------------------------------------------------------------------

    def on_data(self, event: MarketEvent) -> Optional[Signal]:
        if not self._enabled:
            return None
        v = self._verdict(event.symbol, event.timestamp_ms)
        if v is None:
            return None
        action = str(v.get("action") or "")
        if action not in ("long", "short"):
            return None
        conf = float(v.get("confidence") or 0.0)
        if conf < self.MIN_CONFIDENCE:
            return None
        ts = int(v.get("ts_ms") or 0)
        if ts <= self._last_signaled.get(event.symbol, 0):
            return None  # edge-triggered: one signal per new decision

        atr_pct = float(v.get("atr_pct_15m") or 0.0)
        sl_pct = max(self.SL_PCT_MIN, self.SL_ATR_MULT * atr_pct / 100.0)
        regime = v.get("regime")
        self._last_signaled[event.symbol] = ts
        logger.info(
            "JevJudge %s SIGNAL %s conf=%.2f regime=%s sl=%.2f%% (experiment)",
            event.symbol, action, conf, regime, sl_pct * 100,
        )
        return Signal(
            strategy=self.name,
            symbol=event.symbol,
            side=action,
            confidence=conf,
            size_pct=self.BASE_SIZE_PCT,
            entry_price=event.price,
            stop_loss_pct=sl_pct,
            take_profit_pct=sl_pct * self.TP_R_MULT,
            reason=f"jev_experiment:{action}@{conf:.2f}",
            metadata={
                "experiment": "jev-shadow",
                "jev_decision_ts_ms": ts,
                "jev_regime": regime,
                "jev_confidence": conf,
            },
        )

    def on_position(
        self, position: Position, event: MarketEvent
    ) -> Optional[ExitSignal]:
        if not self._enabled or position.symbol != event.symbol:
            return None
        held_ms = event.timestamp_ms - int(position.entry_time_ms or 0)
        if held_ms >= self.MAX_HOLD_MS:
            return ExitSignal(
                strategy=self.name,
                symbol=position.symbol,
                side=position.side,
                confidence=0.8,
                reason="jev_max_hold",
            )
        if self.FLIP_EXIT:
            v = self._verdict(event.symbol, event.timestamp_ms)
            if v is not None:
                action = str(v.get("action") or "")
                opposite = {"long": "short", "short": "long"}.get(position.side)
                if action == opposite and float(v.get("confidence") or 0.0) >= self.MIN_CONFIDENCE:
                    return ExitSignal(
                        strategy=self.name,
                        symbol=position.symbol,
                        side=position.side,
                        confidence=0.7,
                        reason=f"jev_flip:{action}@{v.get('confidence')}",
                    )
        return None
