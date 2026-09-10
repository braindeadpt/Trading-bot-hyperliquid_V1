"""Pre-start feed-delivery check.

Before starting the bot, verify that every feed contracted by THIS
deployment has recent delivery evidence — fail early instead of letting a
blocked feed sit silent until the 6h/1h/12h watchdog threshold trips (the
2026-06-29 fstream outage lesson).

Sources of evidence (persisted artifacts, so the check works with the bot
stopped):

  * liquidation_okx / liquidation_bybit / liquidation_binance
      -> liquidation_events (max timestamp_ms per source)
  * funding_hl / funding_cex
      -> funding_history (max timestamp)
  * taker_split
      -> candles_1m (max timestamp_ms where buy/sell volume > 0)
  * binance_perp
      -> binance_perp_prices (max timestamp_ms)
  * l2_book_recording
      -> newest file mtime under data/research/l2_books/
  * liquidation_coinalyze_check
      -> verify-only venue: no persisted evidence; reported but never gated.

``l2_book_recording`` is SELF-PRODUCED — the bot writes it, so its evidence
only exists while the bot runs. It is reported for visibility but NEVER
gates the boot (after any downtime it is stale by definition); the runtime
FeedSilenceMonitor keeps degrading it while the bot runs. See
``src.core.engine.SELF_PRODUCED_FEEDS``.

**Stale vs dead (generalization, 2026-09-09).** The same "evidence only
exists while the bot runs" logic applies to EVERY feed after a long
downtime: the evidence lives in the local DB and is only written while the
bot is up, so after any long stop ALL feeds look stale and the boot gate
deadlocks (this is why ``--skip-preflight`` crept into the launchers —
which silently disabled the fstream-outage protection for every boot).
The gate therefore distinguishes the two causes:

  * ``age_sec <= downtime_sec + DOWNTIME_TOLERANCE_SEC`` → the feed's
    staleness is fully explained by the bot being OFF ("stale-since-
    downtime"): reported in output + JSON, counted as a warning, never a
    failure — boot proceeds and the runtime monitor takes over;
  * otherwise the feed was already dead WHILE the bot ran — the original
    failure mode — and still fails (exit 1), preserving the 2026-06-29
    fstream lesson.

downtime_sec is derived per-run from the evidence itself:
``last_alive_ms = max(evidence)`` (the last instant the bot demonstrably
wrote anything). With NO evidence at all the behavior is unchanged — no
downtime is invented.

``--out PATH`` persists the same JSON report to a file (the boot wiring
uses it so the dashboard feed panel can show, after a downtime, which
feeds the boot classified as merely stale vs genuinely dead).

Per-symbol candle freshness (1m/15m) is also validated for every trading
symbol — a data backlog (the collector fell behind) shows up here before a
backtest silently reads a window that ends days ago. Two modes:

  * freshness (default): the latest 1m/15m candle per symbol must be within
    --candle-1m-max-age-sec / --candle-15m-max-age-sec of now. Candles are
    written by the bot's own connector, so they are exactly as
    downtime-sensitive as the external feeds above: the SAME stale-vs-dead
    rule applies (2026-09-10 fix) — a symbol whose candle age fits inside
    ``downtime_sec + DOWNTIME_TOLERANCE_SEC + interval_sec`` is
    ``stale-since-downtime`` (warning, never blocks boot); older than that
    is a genuine collector backlog while the bot was running and still
    fails. The trailing ``+ interval_sec`` term (the series' own bar
    period — 60s for 1m, 900s for 15m, derived from the label) matters
    because a slower series' last COMPLETE bar always lags the fast-cadence
    downtime evidence by up to one full period even with zero real
    backlog — comparing age to downtime without it is a unit mismatch that
    false-fails 15m right after a restart. GUARD: this grace only applies
    when ``downtime_sec > DOWNTIME_TOLERANCE_SEC`` (a genuine stop, not a
    quick restart) — with the bot running, downtime_sec ≈ 0 and the grace
    would otherwise exceed the 1m threshold itself, excusing a real 1m
    backlog while the bot is alive; below the tolerance the plain
    threshold check applies with no grace at all;
  * coverage (--min-latest-ms, used before backtests): the latest candle
    must REACH the requested end-of-window timestamp, whatever its age —
    catches a backlog at the end of the window without blocking historical
    windows. Coverage is about historical completeness, not freshness, so
    the downtime grace does NOT apply here — behavior is unchanged.

Exit codes:
  0  all contracted feeds have fresh evidence AND candles are fresh/covered
  1  at least one feed aged past its silence threshold (or missing entirely)
     or a symbol's candles failed (missing / past max age / below min) with
     no downtime explaining it
  2  at least one check aged past --warn-fraction of its threshold (default:
     FEED_SILENCE_WARN_FRACTION env — the same early-warning level the
     bot's FeedSilenceMonitor uses — else 0.5; --warn-fraction overrides),
     or classified stale-since-downtime
  3  the check itself crashed (EXIT_INTERNAL_ERROR) — an unhandled exception,
     NOT a verdict about feed/candle health; the caller must not treat this
     as "feed failed" (see main.py's boot wiring)
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.core.engine import (  # noqa: E402
    SELF_PRODUCED_FEEDS,
    feed_silence_contracts,
    feed_silence_warn_fraction,
)
from src.data.market_data_health import DOWNTIME_TOLERANCE_SEC  # noqa: E402
from src.utils.config import get_trading_symbols, load_config  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "live" / "bot.db"

# Distinct exit code for "the checker itself crashed" — must never be
# confused with rc=1 ("a feed/candle is genuinely failing"). Python's
# default unhandled-exception exit code is also 1, which is exactly the
# ambiguity this constant exists to remove (2026-09-10: an OOM'd preflight
# process was logged as "feed FAILED" with no diagnostic). Module constant,
# not config — see the DOWNTIME_TOLERANCE_SEC note below for why.
EXIT_INTERNAL_ERROR = 3

# Where the boot wiring persists the last preflight verdict so the
# dashboard feed panel can show, after a downtime, which feeds were merely
# stale (bot off) vs genuinely dead while it ran. Not config — a fixed
# research artifact path, same convention as NIGHTLY_STATUS.json.
PREFLIGHT_REPORT_PATH = ROOT / "data" / "research" / "preflight_last.json"
L2_BOOKS_DIR = ROOT / "data" / "research" / "l2_books"

# Grace added to the computed downtime when classifying an over-threshold
# feed as "stale-since-downtime": collectors flush on different cadences and
# writes are asynchronous, so the youngest evidence can lag the actual
# shutdown instant by minutes. A feed whose age fits inside downtime+grace
# went stale because the bot was OFF; anything older was already dead while
# the bot ran (the fstream failure mode) and still blocks boot. Module
# constant on purpose — NOT config (a new key would drift the Fase-10
# config_hash manifest). Shared with the runtime monitor
# (src.data.market_data_health) so the post-boot alert-suppression window
# is exactly the boot's verdict window — imported above (the import
# already binds DOWNTIME_TOLERANCE_SEC in this module for callers/tests).


def _db_latest(db: sqlite3.Connection, table: str, col: str,
               where: str = "") -> int:
    q = f"SELECT MAX({col}) FROM {table}"
    if where:
        q += f" WHERE {where}"
    row = db.execute(q).fetchone()
    return int(row[0]) if row and row[0] else 0


def _candle_latest(db: sqlite3.Connection, table: str, symbol: str) -> int:
    """Latest persisted candle timestamp (ms) for one symbol; 0 if none."""
    row = db.execute(
        f"SELECT MAX(timestamp_ms) FROM {table} WHERE symbol = ?", (symbol,)
    ).fetchone()
    return int(row[0]) if row and row[0] else 0


# Interval, in seconds, implied by a candle-series label. Used to widen the
# downtime grace by one bar period (2026-09-10 fix): a candle series only
# closes bars every ``interval_sec``, so its last COMPLETE bar is always up
# to one full period further behind "now" than the fast-cadence evidence
# used to compute ``downtime_sec`` — comparing the two directly without this
# term is a unit mismatch that false-fails slower series (e.g. 15m) right
# after a restart. Derived from the label so callers never pass it by hand.
_INTERVAL_SEC_BY_LABEL = {"1m": 60, "15m": 900}


def _interval_sec(label: str) -> float:
    return float(_INTERVAL_SEC_BY_LABEL.get(label, 0))


def _candle_status(
    latest_ms: int,
    now_ms: int,
    *,
    max_age_sec: float,
    warn_fraction: float,
    min_latest_ms: Optional[int] = None,
    last_alive_ms: int = 0,
    downtime_sec: float = 0.0,
    interval_sec: float = 0.0,
) -> tuple:
    """Status (ok/warn/fail/stale-since-downtime) + age for one symbol/interval.

    Coverage mode (``min_latest_ms`` set — the backtest window end): the
    data must REACH the requested timestamp; freshness-vs-now is irrelevant
    for a historical window, and the downtime grace below does NOT apply —
    this is about historical completeness, not liveness.

    Freshness mode: fail at ``max_age_sec``, warn past ``warn_fraction`` of
    it — a data backlog means the collector fell behind. Candles are
    written by the bot's own connector, so after any downtime they are
    stale by definition — same reasoning as the external-feed generalization
    in this module's docstring. An over-threshold candle whose age is fully
    explained by the bot being off is ``stale-since-downtime`` (warning,
    never blocks boot); older than that is a genuine collector backlog
    while the bot ran and still fails.

    The grace is ``downtime_sec + DOWNTIME_TOLERANCE_SEC + interval_sec``
    (2026-09-10): the trailing ``+ interval_sec`` accounts for the series'
    own bar period (a 15m bar only closes every 15 minutes, so the last
    complete bar lags the fast-cadence downtime evidence by up to one full
    period even with zero real backlog) — comparing ``age_sec`` to
    ``downtime_sec`` without it is a unit mismatch that false-fails slower
    series right after a restart.

    GUARD (do not remove — closes the hole the grace above opens): the
    grace applies ONLY when ``downtime_sec > DOWNTIME_TOLERANCE_SEC``, i.e.
    there was a genuine stop, not just a quick restart. With the bot
    running, ``downtime_sec`` is ~0 and the 1m grace would otherwise be
    ``0 + DOWNTIME_TOLERANCE_SEC + 60``, which EXCEEDS the 1m series' own
    300s threshold — a real collector backlog while the bot is alive would
    be silently excused. Below the tolerance, classification falls back to
    the plain threshold check with no grace at all.
    """
    if latest_ms == 0:
        return "fail", None
    age_sec = max(0.0, (now_ms - latest_ms) / 1000.0)
    if min_latest_ms is not None:
        return ("ok" if latest_ms >= min_latest_ms else "fail"), age_sec
    if age_sec >= max_age_sec:
        if (
            last_alive_ms
            and downtime_sec > DOWNTIME_TOLERANCE_SEC
            and age_sec <= downtime_sec + DOWNTIME_TOLERANCE_SEC + interval_sec
        ):
            return "stale-since-downtime", age_sec
        return "fail", age_sec
    if age_sec >= max_age_sec * warn_fraction:
        return "warn", age_sec
    return "ok", age_sec


def _l2_books_mtime(l2_dir: Path) -> int:
    newest = 0
    if l2_dir.exists():
        for p in l2_dir.rglob("*"):
            if p.is_file():
                newest = max(newest, int(p.stat().st_mtime * 1000))
    return newest


def collect_evidence(db: sqlite3.Connection, *, l2_dir: Path = L2_BOOKS_DIR) -> dict:
    """Latest delivery timestamp per feed key (ms). Absent keys = no evidence."""
    ev: dict = {}
    ev["liquidation_okx"] = _db_latest(db, "liquidation_events", "timestamp_ms",
                                       "source='okx'")
    ev["liquidation_bybit"] = _db_latest(db, "liquidation_events", "timestamp_ms",
                                         "source='bybit'")
    ev["liquidation_binance"] = _db_latest(db, "liquidation_events", "timestamp_ms",
                                           "source='binance'")
    ev["funding_hl"] = _db_latest(db, "funding_history", "timestamp")
    ev["funding_cex"] = _db_latest(db, "funding_history", "timestamp")
    ev["taker_split"] = _db_latest(
        db, "candles_1m", "timestamp_ms",
        "(buy_volume > 0 OR sell_volume > 0)",
    )
    ev["binance_perp"] = _db_latest(db, "binance_perp_prices", "timestamp_ms")
    ev["l2_book_recording"] = _l2_books_mtime(l2_dir)
    # coinalyze_check: verify-only, no persisted evidence -> key absent
    return ev


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB),
                        help="bot.db path (default: data/live/bot.db)")
    parser.add_argument("--config", default=str(ROOT / "config" / "settings.yaml"),
                        help="settings.yaml path")
    parser.add_argument(
        "--l2-dir",
        default=str(L2_BOOKS_DIR),
        help="L2 book recording directory (default: data/research/l2_books)",
    )
    parser.add_argument("--warn-fraction", type=float, default=None,
                        help="warn when age exceeds this fraction of threshold "
                             "(default: FEED_SILENCE_WARN_FRACTION env, else 0.5)")
    parser.add_argument("--json", action="store_true", help="emit JSON report")
    parser.add_argument("--out", default=None, help="also write the JSON report to this file "
                                                  "(used by the boot wiring / dashboard panel)")
    parser.add_argument("--gate-coinalyze", action="store_true",
                        help="fail if coinalyze has no evidence (default: skipped, verify-only)")
    parser.add_argument("--candles-only", action="store_true",
                        help="check only per-symbol 1m/15m candle freshness (skip "
                             "feed contracts) — used before running a backtest")
    parser.add_argument("--candle-1m-max-age-sec", type=float, default=300.0,
                        help="1m candle older than this (sec) is a backlog fail")
    parser.add_argument("--candle-15m-max-age-sec", type=float, default=1800.0,
                        help="15m candle older than this (sec) is a backlog fail")
    parser.add_argument("--min-latest-ms", type=int, default=None,
                        help="backtest end-of-window: latest candle must reach this "
                             "timestamp (coverage mode, freshness ignored)")
    args = parser.parse_args()

    # Same warn threshold the bot's FeedSilenceMonitor uses — the env wins
    # over the hardcoded 0.5, and an explicit --warn-fraction overrides both.
    warn_frac = (
        args.warn_fraction
        if args.warn_fraction is not None
        else feed_silence_warn_fraction()
    )
    cfg = load_config(args.config)
    contracts = feed_silence_contracts(cfg)  # same decision the engine makes
    symbols = get_trading_symbols(cfg)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: bot DB not found: {db_path}", file=sys.stderr)
        return 1
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    now_ms = int(time.time() * 1000)
    l2_dir = Path(args.l2_dir)
    evidence = collect_evidence(db, l2_dir=l2_dir)

    # Downtime inference: the newest persisted artifact is the last instant
    # the bot demonstrably wrote anything, so everything older may simply
    # reflect the bot being off. Zero evidence (first run / empty DB) means
    # NO downtime is invented — the classic gate applies unchanged.
    positive = [v for v in evidence.values() if v and v > 0]
    last_alive_ms = max(positive) if positive else 0
    downtime_sec = (
        max(0.0, (now_ms - last_alive_ms) / 1000.0) if last_alive_ms else 0.0
    )

    report: dict = {
        "now_ms": now_ms,
        # Boot context the dashboard needs to explain the per-feed verdicts:
        "downtime_sec": round(downtime_sec, 1),
        "last_alive_ms": last_alive_ms or None,
        "feeds": {},
        "candles": {},
    }
    failures = 0
    warnings = 0

    if not args.candles_only:
        for feed, max_sec in sorted(contracts.items()):
            latest = evidence.get(feed, 0)
            age_sec = None
            status = "ok"
            if feed in SELF_PRODUCED_FEEDS:
                # Evidence exists only while the bot runs; after any downtime
                # it is stale by definition, so boot gating on it would
                # deadlock every restart longer than the threshold. Reported
                # for visibility (age still shown when fresh), NEVER gated —
                # the RUNTIME FeedSilenceMonitor still degrades it while the
                # bot runs (2026-08-14 audit).
                if latest:
                    age_sec = max(0.0, (now_ms - latest) / 1000.0)
                status = "self-produced"
            elif latest == 0:
                if feed == "liquidation_coinalyze_check" and not args.gate_coinalyze:
                    # Verify-only venue: never persisted, never blocks. Reported
                    # so operators still see it in the panel.
                    status = "skipped"
                else:
                    status = "fail"
                    failures += 1
            else:
                age_sec = max(0.0, (now_ms - latest) / 1000.0)
                if age_sec >= max_sec:
                    if last_alive_ms and age_sec <= downtime_sec + DOWNTIME_TOLERANCE_SEC:
                        # Staleness fully explained by the bot being off:
                        # same reasoning as SELF_PRODUCED_FEEDS, generalized.
                        # Warn (visible in output/JSON) but never gate boot —
                        # the runtime FeedSilenceMonitor owns it once live.
                        # Anything OLDER than downtime+grace was already dead
                        # while the bot ran -> still a hard fail below.
                        status = "stale-since-downtime"
                        warnings += 1
                    else:
                        status = "fail"
                        failures += 1
                elif age_sec >= max_sec * warn_frac:
                    status = "warn"
                    warnings += 1
                else:
                    status = "ok"
            report["feeds"][feed] = {
                "max_silence_sec": max_sec,
                "age_sec": None if age_sec is None else round(age_sec, 1),
                "latest_ms": latest or None,
                "pct_of_threshold": (
                    None if age_sec is None else round(age_sec / max_sec * 100, 1)
                ),
                "status": status,
                "self_produced": feed in SELF_PRODUCED_FEEDS,
                "stale_since_downtime": status == "stale-since-downtime",
            }

    # Per-symbol 1m/15m candle freshness / coverage.
    intervals = (
        ("candles_1m", "1m", args.candle_1m_max_age_sec),
        ("candles_15m", "15m", args.candle_15m_max_age_sec),
    )
    for symbol in sorted(symbols):
        report["candles"][symbol] = {}
        for table, label, max_age in intervals:
            latest = _candle_latest(db, table, symbol)
            status, age_sec = _candle_status(
                latest, now_ms,
                max_age_sec=max_age,
                warn_fraction=warn_frac,
                min_latest_ms=args.min_latest_ms,
                last_alive_ms=last_alive_ms,
                downtime_sec=downtime_sec,
                interval_sec=_interval_sec(label),
            )
            if status == "fail":
                failures += 1
            elif status in ("warn", "stale-since-downtime"):
                warnings += 1
            report["candles"][symbol][label] = {
                "latest_ms": latest or None,
                "age_sec": None if age_sec is None else round(age_sec, 1),
                "max_age_sec": None if args.min_latest_ms is not None else max_age,
                "min_latest_ms": args.min_latest_ms,
                "status": status,
                "stale_since_downtime": status == "stale-since-downtime",
            }
    db.close()

    if args.out:
        out = Path(args.out)
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        except OSError as exc:
            print(f"WARN: could not write preflight report to {out}: {exc}",
                  file=sys.stderr)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for feed, st in report["feeds"].items():
            age = st["age_sec"]
            age_txt = (f"{age:.0f}s" if age is not None and age < 60 else
                       f"{age / 60:.1f}m" if age is not None and age < 3600 else
                       f"{age / 3600:.1f}h" if age is not None else "no evidence")
            th = st["max_silence_sec"]
            th_txt = (f"{th:.0f}s" if th < 60 else
                      f"{th / 60:.0f}m" if th < 3600 else f"{th / 3600:.1f}h")
            print(f"{feed:32s} age={age_txt:>10s} "
                  f"threshold={th_txt:>6s} "
                  f"pct={st['pct_of_threshold']}%  {st['status'].upper()}")
        print("\n--- per-symbol candle freshness ---")
        for symbol, cells in sorted(report["candles"].items()):
            for label, st in cells.items():
                age = st["age_sec"]
                age_txt = (f"{age:.0f}s" if age is not None and age < 60 else
                           f"{age / 60:.1f}m" if age is not None and age < 3600 else
                           f"{age / 3600:.1f}h" if age is not None else "no candles")
                crit = (f"min={st['min_latest_ms']}" if st["min_latest_ms"] is not None
                        else f"max={st['max_age_sec']:.0f}s")
                print(f"candles_{label:>2s} {symbol:5s} age={age_txt:>10s} "
                      f"{crit:>22s}  {st['status'].upper()}")

    if failures:
        print(f"\n[FAIL] {failures} check(s) failed — {len([f for f in report['feeds'] if report['feeds'][f]['status'] == 'fail'])} "
              f"feed(s), {sum(1 for s in report['candles'] for c in report['candles'][s].values() if c['status'] == 'fail')} "
              f"candle(s) — check before starting (silence/backlog would only degrade later).",
              file=sys.stderr if not args.json else sys.stdout)
        return 1
    stale_feeds = [f for f, st in report["feeds"].items()
                   if st["status"] == "stale-since-downtime"]
    stale_candles = [
        f"{symbol}/{label}"
        for symbol, cells in report["candles"].items()
        for label, st in cells.items()
        if st["status"] == "stale-since-downtime"
    ]
    if warnings:
        print(f"\n[WARN] {warnings} check(s) past {warn_frac * 100:.0f}% "
              "of threshold — delivery/backlog forming?", file=sys.stderr)
        if stale_feeds or stale_candles:
            parts = []
            if stale_feeds:
                parts.append(f"{len(stale_feeds)} feed(s) "
                             f"({', '.join(sorted(stale_feeds))})")
            if stale_candles:
                parts.append(f"{len(stale_candles)} candle series "
                             f"({', '.join(sorted(stale_candles))})")
            print(f"       stale-since-downtime: {' and '.join(parts)} — ages "
                  f"consistent with the bot being off ~{downtime_sec / 3600:.1f}h; "
                  "boot proceeds, the runtime FeedSilenceMonitor / next "
                  "collector cycle owns them.", file=sys.stderr)
        return 2
    if not args.json:
        print("\n[PASS] all contracted feeds fresh and candles up to date.")
    return 0


def main() -> int:
    """Thin crash barrier around ``_main`` — see EXIT_INTERNAL_ERROR: a
    checker crash (traceback on stderr, exit 3) must never be reported by a
    caller as "feed failed" (rc 1), which is also Python's default exit code
    for an unhandled exception. Do not add real check logic here."""
    try:
        return _main()
    except Exception:
        import traceback
        traceback.print_exc(file=sys.stderr)
        print(
            f"\n[INTERNAL ERROR] preflight_feed_check.py crashed — exit "
            f"{EXIT_INTERNAL_ERROR}. This is NOT a feed/candle verdict; see "
            "the traceback above.",
            file=sys.stderr,
        )
        return EXIT_INTERNAL_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
