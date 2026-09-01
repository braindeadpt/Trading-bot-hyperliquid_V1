"""Market data feed health snapshots for monitoring and dashboard."""

from __future__ import annotations

import collections
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


@dataclass
class PollRecord:
    """Single health sample."""

    timestamp_ms: int
    symbol: str
    cex_ok: bool
    hl_ok: bool
    status: str  # green | yellow | red


class MarketDataHealthTracker:
    """Rolling window of poll outcomes for failure-rate metrics."""

    def __init__(self, window_sec: float = 3600.0) -> None:
        self._window_sec = float(window_sec)
        self._records: Deque[PollRecord] = collections.deque()

    def record(
        self,
        symbol: str,
        *,
        cex_ok: bool,
        hl_ok: bool,
        status: str,
        timestamp_ms: Optional[int] = None,
    ) -> None:
        ts = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
        self._records.append(
            PollRecord(
                timestamp_ms=ts,
                symbol=symbol,
                cex_ok=cex_ok,
                hl_ok=hl_ok,
                status=status,
            )
        )
        self._prune(ts)

    def _prune(self, now_ms: int) -> None:
        cutoff = now_ms - int(self._window_sec * 1000)
        while self._records and self._records[0].timestamp_ms < cutoff:
            self._records.popleft()

    def symbol_stats(self, symbol: str) -> Tuple[int, int, float]:
        """Return (total_polls, failed_polls, failure_rate) in the window."""
        now_ms = int(time.time() * 1000)
        self._prune(now_ms)
        cutoff = now_ms - int(self._window_sec * 1000)
        total = 0
        failed = 0
        for rec in self._records:
            if rec.symbol != symbol or rec.timestamp_ms < cutoff:
                continue
            total += 1
            if rec.status == "red":
                failed += 1
        rate = (failed / total) if total > 0 else 0.0
        return total, failed, rate

    def overall_stats(self) -> Tuple[int, int, float, str]:
        """Aggregate stats across all symbols in window."""
        now_ms = int(time.time() * 1000)
        self._prune(now_ms)
        cutoff = now_ms - int(self._window_sec * 1000)
        total = 0
        failed = 0
        statuses: List[str] = []
        for rec in self._records:
            if rec.timestamp_ms < cutoff:
                continue
            total += 1
            statuses.append(rec.status)
            if rec.status == "red":
                failed += 1
        rate = (failed / total) if total > 0 else 0.0
        if not statuses:
            return 0, 0, 0.0, "red"
        if any(s == "red" for s in statuses):
            overall = "red"
        elif any(s == "yellow" for s in statuses):
            overall = "yellow"
        else:
            overall = "green"
        return total, failed, rate, overall


@dataclass
class SymbolFeedHealth:
    """Per-symbol market data quality."""

    symbol: str
    cex_ok: bool = False
    cex_stale: bool = False
    cex_age_sec: float = 0.0
    cex_exchanges: List[str] = field(default_factory=list)
    cex_exchange_count: int = 0
    hl_predicted_ok: bool = False
    hl_predicted_stale: bool = False
    hl_predicted_age_sec: float = 0.0
    hl_venues: List[str] = field(default_factory=list)
    status: str = "red"  # green | yellow | red
    polls_1h: int = 0
    failed_polls_1h: int = 0
    failure_rate_1h: float = 0.0
    funding_hl_ws: Optional[float] = None
    funding_hl_predicted_8h: Optional[float] = None
    funding_cex_avg_8h: Optional[float] = None
    oi_cex_usd: Optional[float] = None
    oi_hl: Optional[float] = None

    def to_dict(self) -> Dict[str, object]:
        return {
            "symbol": self.symbol,
            "cex_ok": self.cex_ok,
            "cex_stale": self.cex_stale,
            "cex_age_sec": round(self.cex_age_sec, 1),
            "cex_exchanges": self.cex_exchanges,
            "cex_exchange_count": self.cex_exchange_count,
            "hl_predicted_ok": self.hl_predicted_ok,
            "hl_predicted_stale": self.hl_predicted_stale,
            "hl_predicted_age_sec": round(self.hl_predicted_age_sec, 1),
            "hl_venues": self.hl_venues,
            "status": self.status,
            "polls_1h": self.polls_1h,
            "failed_polls_1h": self.failed_polls_1h,
            "failure_rate_1h": round(self.failure_rate_1h * 100, 1),
            "funding_hl_ws": self.funding_hl_ws,
            "funding_hl_predicted_8h": self.funding_hl_predicted_8h,
            "funding_cex_avg_8h": self.funding_cex_avg_8h,
            "oi_cex_usd": self.oi_cex_usd,
            "oi_hl": self.oi_hl,
        }


@dataclass
class MarketDataHealthSummary:
    """Fleet-wide health rollup."""

    overall: str = "red"
    symbols: Dict[str, SymbolFeedHealth] = field(default_factory=dict)
    polls_1h: int = 0
    failed_polls_1h: int = 0
    failure_rate_1h: float = 0.0
    red_since_sec: float = 0.0

    def to_dict(self) -> Dict[str, object]:
        return {
            "overall": self.overall,
            "polls_1h": self.polls_1h,
            "failed_polls_1h": self.failed_polls_1h,
            "failure_rate_1h": round(self.failure_rate_1h * 100, 1),
            "red_since_sec": round(self.red_since_sec, 1),
            "symbols": {k: v.to_dict() for k, v in self.symbols.items()},
        }


def compute_feed_status(
    *,
    cex_ok: bool,
    cex_stale: bool,
    cex_exchange_count: int,
    min_exchanges: int,
    hl_ok: bool,
    hl_stale: bool,
) -> str:
    """green / yellow / red."""
    if not cex_ok and not hl_ok:
        return "red"
    if cex_stale or hl_stale:
        return "yellow"
    if cex_ok and cex_exchange_count < min_exchanges:
        return "yellow"
    if cex_ok or hl_ok:
        return "green"
    return "red"


@dataclass
class FeedSilenceState:
    """Last-seen + degraded flag for one contracted feed."""

    feed: str
    last_event_ms: Optional[int] = None
    max_silence_sec: float = 3600.0
    degraded: bool = False
    last_alert_mono: float = 0.0
    # Fire-once early-warning: alerted at >=50% of max_silence; reset on beat.
    warned_50_pct: bool = False
    # Wall-clock ms when the early warning fired (None until it does) — the
    # Alerted column shows this so the operator sees WHEN the alert went out.
    warned_50_at_ms: Optional[int] = None
    # Fire-once imminent-warning: alerted at >=90% of max_silence (before
    # degrading); reset on beat.
    warned_90_pct: bool = False
    # Wall-clock ms when the imminent warning fired (None until it does).
    warned_90_at_ms: Optional[int] = None
    # Daily episode counters (UTC calendar day — the same boundary the daily
    # feed_age rollups use). One increment per EPISODE, not per check: the
    # fire-once flags re-arm on beat(), so a feed that alerts twice today
    # counts 2. NOT reset by beat() — only when the UTC day flips.
    early_count_today: int = 0
    imminent_count_today: int = 0
    # Cadence tracking: rolling inter-event gaps (sec) used to detect a feed
    # that is still delivering but far less often than its historical p99.
    gaps: "collections.deque" = field(default_factory=collections.deque)
    # Fire-once cadence alert (age > historical p99 gap); reset on beat.
    warned_cadence: bool = False

    @property
    def warn_level(self) -> str:
        """Escalation level for this feed: degraded > imminent > early > none.

        Single source of truth — the snapshot emits it, the dashboard and the
        offline cadence diagnostic consume it. Nothing re-derives the level
        from the fire-once flags or from age thresholds.
        """
        return warn_level_from_flags(
            degraded=self.degraded,
            imminent=self.warned_90_pct,
            early=self.warned_50_pct,
        )

    def age_sec(self, now_ms: Optional[int] = None) -> Optional[float]:
        if self.last_event_ms is None:
            return None
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        return max(0.0, (now - self.last_event_ms) / 1000.0)

    def cadence_percentile_sec(
        self,
        pct: float = 0.99,
        min_samples: int = 100,
    ) -> Optional[float]:
        """Historical ``pct`` percentile of inter-event gaps, or None with too
        few samples.

        Nearest-rank percentile over the rolling gap deque — the cadence the
        feed normally keeps. A current gap above the p99 means delivery is
        thinning out long before the 6h silence threshold trips. Delegates
        to the module-level ``cadence_percentile`` (single source of truth
        shared with the lead-time validator).
        """
        return cadence_percentile(self.gaps, pct=pct, min_samples=min_samples)

    def cadence_p95_sec(self, min_samples: int = 100) -> Optional[float]:
        """Historical p95 of inter-event gaps (typical quiet ceiling)."""
        return self.cadence_percentile_sec(0.95, min_samples=min_samples)

    def cadence_p99_sec(self, min_samples: int = 100) -> Optional[float]:
        """Historical p99 of inter-event gaps (anomalous-quiet threshold)."""
        return self.cadence_percentile_sec(0.99, min_samples=min_samples)


def cadence_percentile(
    gaps: Sequence[float],
    pct: float = 0.99,
    min_samples: int = 100,
) -> Optional[float]:
    """Nearest-rank ``pct`` percentile of inter-event gaps, or None with too
    few samples.

    The cadence the feed normally keeps. Single source of truth: the
    monitor's ``FeedSilenceState.cadence_percentile_sec`` and the cadence
    lead-time validator (``scripts/validate_feed_cadence_leadtime.py``)
    both call this, so the walk-forward simulation measures exactly the
    production rule and can never drift from it.
    """
    if len(gaps) < min_samples:
        return None
    ordered = sorted(gaps)
    idx = min(len(ordered) - 1, int(pct * len(ordered)))
    return float(ordered[idx])


def warn_level_from_flags(
    *,
    degraded: bool,
    imminent: bool,
    early: bool,
) -> str:
    """Escalation level for one feed: ``degraded`` > ``imminent`` > ``early`` > ``none``.

    Single source of truth for the ``warn_level`` string the dashboard
    consumes. ``imminent``/``early`` are the fire-once flags (``warned_90_pct``
    / ``warned_50_pct``); the precedence mirrors the escalation order of the
    silence alerts — a feed is always exactly one level, and ``degraded``
    wins regardless of which warnings fired first. Shared by
    ``FeedSilenceState.warn_level`` (live snapshot) and the offline cadence
    diagnostic, so the live monitor and the offline reconstruction can never
    disagree about the ordering.
    """
    if degraded:
        return "degraded"
    if imminent:
        return "imminent"
    if early:
        return "early"
    return "none"


class FeedSilenceMonitor:
    """Alert when a contracted feed produces no events for N hours.

    This is the structural fix for the 2026-06-29 Binance fstream outage that
    went unnoticed for six weeks and contaminated screening + ChecklistMeta.
    """

    def __init__(
        self,
        *,
        alert_cooldown_sec: float = 3600.0,
        feeds: Optional[Dict[str, float]] = None,
        warn_fraction: float = 0.5,
        imminent_fraction: float = 0.9,
        cadence_min_samples: int = 100,
        cadence_gap_history: int = 4000,
        on_alert: Optional[Callable[[str, str, int, str], None]] = None,
    ) -> None:
        # feed_name -> max silence seconds
        defaults = {
            "liquidation_binance": 6 * 3600.0,
            "liquidation_okx": 6 * 3600.0,
            "liquidation_bybit": 6 * 3600.0,
            "liquidation_coinalyze_check": 12 * 3600.0,
            "binance_perp": 1 * 3600.0,
            "funding_cex": 1 * 3600.0,
            "funding_hl": 1 * 3600.0,
            "taker_split": 1 * 3600.0,
        }
        cfg = dict(defaults)
        if feeds:
            cfg.update({k: float(v) for k, v in feeds.items()})
        self._states: Dict[str, FeedSilenceState] = {
            name: FeedSilenceState(feed=name, max_silence_sec=max_sec)
            for name, max_sec in cfg.items()
        }
        self._alert_cooldown_sec = float(alert_cooldown_sec)
        self._warn_fraction = float(warn_fraction)
        self._imminent_fraction = float(imminent_fraction)
        self._cadence_min_samples = max(1, int(cadence_min_samples))
        # Keep the rolling history at least as deep as min_samples — with a
        # shorter deque the percentile could never be computed.
        self._cadence_gap_history = max(
            int(cadence_gap_history), self._cadence_min_samples
        )
        # Optional sink for every emitted alert: (feed, alert_type, fired_ms,
        # message). The engine uses it to persist the real silence history to
        # the research DB for audit vs the daily max-age rollup.
        self._on_alert = on_alert
        self._enabled_feeds: set[str] = set(cfg.keys())
        # time.monotonic() is since an arbitrary epoch (often system boot on
        # Windows), NOT process start. Never-seen silence must use age since
        # monitor construction or every restart fires "never produced" instantly.
        self._started_mono: float = time.monotonic()
        # UTC date (YYYY-MM-DD) the per-feed daily episode counters were last
        # rolled to. None until the first check/snapshot anchors them.
        self._counts_day: Optional[str] = None

    def _roll_counts_to_day(self, now_ms: int) -> None:
        """Reset per-feed daily episode counters when the UTC day flips.

        "Today" is a UTC calendar day (the same boundary the daily
        ``feed_age_history`` rollups use), not a 24h rolling window. Called
        before increments (``check_early_warnings``) and before reads
        (``snapshot``) so the dashboard never shows yesterday's counts after
        midnight — and increments always land in the right day.
        """
        day = time.strftime("%Y-%m-%d", time.gmtime(int(now_ms) // 1000))
        if day == self._counts_day:
            return
        self._counts_day = day
        for st in self._states.values():
            st.early_count_today = 0
            st.imminent_count_today = 0

    def _emit_alert(
        self,
        alerts: List[str],
        name: str,
        alert_type: str,
        now_ms: int,
        msg: str,
    ) -> None:
        """Append an alert and forward it to the optional on_alert sink.

        The sink (research-DB recorder) is best-effort: a failing recorder
        must never break the monitor's own alert flow.
        """
        alerts.append(msg)
        if self._on_alert is not None:
            try:
                self._on_alert(name, alert_type, int(now_ms), msg)
            except Exception as exc:  # noqa: BLE001
                logger.warning("feed silence on_alert failed: %s", exc)

    def enable_feed(self, feed: str, max_silence_sec: Optional[float] = None) -> None:
        self._enabled_feeds.add(feed)
        if feed not in self._states:
            self._states[feed] = FeedSilenceState(
                feed=feed,
                max_silence_sec=float(max_silence_sec or 3600.0),
            )
        elif max_silence_sec is not None:
            self._states[feed].max_silence_sec = float(max_silence_sec)

    def disable_feed(self, feed: str) -> None:
        self._enabled_feeds.discard(feed)

    def beat(self, feed: str, timestamp_ms: Optional[int] = None) -> None:
        if feed not in self._states:
            self._states[feed] = FeedSilenceState(feed=feed)
        st = self._states[feed]
        ts = (
            int(timestamp_ms) if timestamp_ms is not None else int(time.time() * 1000)
        )
        # Record the inter-event gap (cadence) when we have a prior beat.
        if st.last_event_ms is not None:
            gap_sec = (ts - st.last_event_ms) / 1000.0
            if gap_sec >= 0:
                st.gaps.append(gap_sec)
                while len(st.gaps) > self._cadence_gap_history:
                    st.gaps.popleft()
        st.last_event_ms = ts
        st.degraded = False
        st.warned_50_pct = False
        st.warned_90_pct = False
        st.warned_cadence = False
        st.warned_50_at_ms = None
        st.warned_90_at_ms = None

    def check(self, now_ms: Optional[int] = None) -> List[str]:
        """Return alert messages for newly-degraded (or re-alertable) feeds."""
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        mono = time.monotonic()
        uptime_sec = mono - self._started_mono
        alerts: List[str] = []
        for name in sorted(self._enabled_feeds):
            st = self._states.get(name)
            if st is None:
                continue
            if st.last_event_ms is None:
                # Never seen — degrade only after max_silence from *monitor start*
                # (not raw monotonic, which can be days since boot on Windows).
                if uptime_sec > st.max_silence_sec:
                    newly_degraded = not st.degraded
                    st.degraded = True
                    # First degrade always pages; repeats honour alert_cooldown_sec.
                    if newly_degraded or mono - st.last_alert_mono >= self._alert_cooldown_sec:
                        st.last_alert_mono = mono
                        self._emit_alert(
                            alerts, name, "degraded", now,
                            f"FEED SILENT: `{name}` never produced an event "
                            f"(threshold {st.max_silence_sec/3600:.1f}h) — "
                            f"marking degraded",
                        )
                continue
            age = (now - st.last_event_ms) / 1000.0
            if age >= st.max_silence_sec:
                newly_degraded = not st.degraded
                st.degraded = True
                # First degrade always pages — even if imminent fired seconds ago.
                if newly_degraded or mono - st.last_alert_mono >= self._alert_cooldown_sec:
                    st.last_alert_mono = mono
                    self._emit_alert(
                        alerts, name, "degraded", now,
                        f"FEED SILENT: `{name}` quiet for {age/3600:.1f}h "
                        f"(threshold {st.max_silence_sec/3600:.1f}h) — "
                        f"data may be stale or path blocked",
                    )
            else:
                st.degraded = False
        return alerts

    def check_early_warnings(
        self,
        now_ms: Optional[int] = None,
        warn_fraction: Optional[float] = None,
        imminent_fraction: Optional[float] = None,
    ) -> List[str]:
        """Return fire-once early/imminent warning messages before degrading.

        Two escalation levels, each firing once per silence episode and reset
        on ``beat()``: ``early`` when age crosses ``warn_fraction`` (default
        ``self._warn_fraction`` — 50%, configurable via
        ``FEED_SILENCE_WARN_FRACTION``) of ``max_silence_sec``, and
        ``imminent`` when it crosses ``imminent_fraction`` (default
        ``self._imminent_fraction`` — 90%, configurable via
        ``FEED_SILENCE_IMMINENT_FRACTION``) — the last checkpoint before the
        feed trips ``degraded`` at 100%. Already-degraded feeds are skipped
        (``check()`` owns those alerts).
        """
        if warn_fraction is None:
            warn_fraction = self._warn_fraction
        if imminent_fraction is None:
            imminent_fraction = self._imminent_fraction
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        self._roll_counts_to_day(now)
        mono = time.monotonic()
        uptime_sec = mono - self._started_mono
        warnings: List[str] = []
        for name in sorted(self._enabled_feeds):
            st = self._states.get(name)
            if st is None or st.degraded:
                continue
            if st.last_event_ms is None:
                # Never seen — escalate on uptime before the never-produced
                # degrade threshold. Fire early then imminent, independently.
                if not st.warned_50_pct and uptime_sec >= st.max_silence_sec * warn_fraction:
                    st.warned_50_pct = True
                    st.warned_50_at_ms = int(now)
                    st.early_count_today += 1
                    self._emit_alert(
                        warnings, name, "early", now,
                        f"FEED QUIET (early): `{name}` never produced an event "
                        f"for {uptime_sec/3600:.1f}h "
                        f"(≥{warn_fraction * 100:.0f}% of {st.max_silence_sec/3600:.1f}h threshold)",
                    )
                if not st.warned_90_pct and uptime_sec >= st.max_silence_sec * imminent_fraction:
                    st.warned_90_pct = True
                    st.warned_90_at_ms = int(now)
                    st.imminent_count_today += 1
                    self._emit_alert(
                        warnings, name, "imminent", now,
                        f"FEED QUIET (imminent): `{name}` still no events after "
                        f"{uptime_sec/3600:.1f}h "
                        f"(≥{imminent_fraction * 100:.0f}% of {st.max_silence_sec/3600:.1f}h "
                        f"threshold) — degrade iminente",
                    )
                continue
            age = (now - st.last_event_ms) / 1000.0
            if not st.warned_50_pct and age >= st.max_silence_sec * warn_fraction:
                st.warned_50_pct = True
                st.warned_50_at_ms = int(now)
                st.early_count_today += 1
                self._emit_alert(
                    warnings, name, "early", now,
                    f"FEED QUIET (early): `{name}` quiet for {age/3600:.1f}h "
                    f"(≥{warn_fraction * 100:.0f}% of {st.max_silence_sec/3600:.1f}h threshold) — "
                    f"check delivery path before it degrades",
                )
            if not st.warned_90_pct and age >= st.max_silence_sec * imminent_fraction:
                st.warned_90_pct = True
                st.warned_90_at_ms = int(now)
                st.imminent_count_today += 1
                self._emit_alert(
                    warnings, name, "imminent", now,
                    f"FEED QUIET (imminent): `{name}` quiet for {age/3600:.1f}h "
                    f"(≥{imminent_fraction * 100:.0f}% of {st.max_silence_sec/3600:.1f}h "
                    f"threshold) — verificar já, degrade iminente",
                )
        return warnings

    def check_cadence(
        self,
        now_ms: Optional[int] = None,
    ) -> List[str]:
        """Alert when a feed's current gap exceeds its historical p99.

        Catches *subtle* degradation long before the 6h silence threshold: a
        feed that is still delivering but far less often than its usual
        cadence. Fire-once per episode (reset on ``beat()``); requires at
        least ``cadence_min_samples`` recorded gaps so a cold-start feed with
        no history never fires.
        """
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        alerts: List[str] = []
        for name in sorted(self._enabled_feeds):
            st = self._states.get(name)
            if st is None or st.degraded or st.last_event_ms is None:
                continue
            if st.warned_cadence:
                continue
            p99 = st.cadence_p99_sec(min_samples=self._cadence_min_samples)
            if p99 is None:
                continue  # not enough history yet — stay quiet
            age = (now - st.last_event_ms) / 1000.0
            if age > p99:
                st.warned_cadence = True
                self._emit_alert(
                    alerts, name, "cadence", now,
                    f"FEED CADENCE: `{name}` quiet for {age/60:.0f}m "
                    f"(>p99 historical gap {p99/60:.0f}m, n={len(st.gaps)}) — "
                    f"delivery thinning out, check before it degrades",
                )
        return alerts

    def snapshot(self, now_ms: Optional[int] = None) -> Dict[str, Dict[str, object]]:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        self._roll_counts_to_day(now)
        out: Dict[str, Dict[str, object]] = {}
        for name in sorted(self._enabled_feeds):
            st = self._states[name]
            age = st.age_sec(now)
            out[name] = {
                "last_event_ms": st.last_event_ms,
                "age_sec": None if age is None else round(age, 1),
                "max_silence_sec": st.max_silence_sec,
                "degraded": st.degraded,
                "warned_50_pct": st.warned_50_pct,
                "warned_50_at_ms": st.warned_50_at_ms,
                "warned_90_pct": st.warned_90_pct,
                "warned_90_at_ms": st.warned_90_at_ms,
                "early_count_today": st.early_count_today,
                "imminent_count_today": st.imminent_count_today,
                "warned_cadence": st.warned_cadence,
                "cadence_p50_sec": st.cadence_percentile_sec(
                    0.50, min_samples=self._cadence_min_samples
                ),
                "cadence_p95_sec": st.cadence_p95_sec(
                    min_samples=self._cadence_min_samples
                ),
                "cadence_p99_sec": st.cadence_p99_sec(
                    min_samples=self._cadence_min_samples
                ),
                "cadence_samples": len(st.gaps),
                "cadence_min_samples": self._cadence_min_samples,
                # Where the CURRENT gap sits in the recorded distribution
                # (0-100, rank-based; None with no age or no gaps yet).
                "cadence_pct_current": self._current_gap_percentile(st, age),
                "warn_fraction": round(self._warn_fraction, 4),
                "imminent_fraction": round(self._imminent_fraction, 4),
                "warn_level": st.warn_level,
            }
        return out

    @staticmethod
    def _current_gap_percentile(
        st: FeedSilenceState,
        age_sec: Optional[float],
    ) -> Optional[float]:
        """Rank-based percentile of the current gap within the recorded gaps.

        ``100 * (# recorded gaps <= current age) / len(gaps)`` — where the
        current silence sits in the feed's own distribution, no learning
        gate (it is a descriptive stat, not a detector threshold). None
        when the feed was never seen or has no recorded gaps.
        """
        if age_sec is None or not st.gaps:
            return None
        below = sum(1 for g in st.gaps if g <= age_sec)
        return round(100.0 * below / len(st.gaps), 1)

    @property
    def any_degraded(self) -> bool:
        return any(
            self._states[n].degraded
            for n in self._enabled_feeds
            if n in self._states
        )
