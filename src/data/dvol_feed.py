"""Deribit DVOL / ETH vol-index daily feed → research DB.

Background feed that fetches the Deribit volatility-index closes (DVOL for BTC,
the ETH vol index for ETH) once a day and persists them to the research DB, so
the IV-percentile regime gate can run in production from stored history instead
of a manual script.

The percentile math here is the **canonical** copy — the offline evidence
scripts (`scripts/research/iv_percentile_regime_gate_test.py`,
`scripts/research/iv_high_only_ab_split.py`, `scripts/research/iv_vs_adx_disagreement.py`) import
these functions, so production and backtest never drift.

Data: Deribit public ``get_volatility_index_data`` (daily resolution).
"""

from __future__ import annotations

import asyncio
import bisect
import datetime as _dt
import logging
import math
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import aiohttp

from src.data.research_database import ResearchDatabase
from src.utils.http import make_client_session

logger = logging.getLogger(__name__)

DERIBIT_URL = "https://www.deribit.com/api/v2/public/get_volatility_index_data"
DERIBIT_TICKER_URL = "https://www.deribit.com/api/v2/public/ticker"
DERIBIT_BOOK_URL = "https://www.deribit.com/api/v2/public/get_book_summary_by_currency"
DVOL_WINDOW_DAYS = 30
DVOL_CURRENCIES = ("BTC", "ETH")
ATM_MIN_EXPIRY_DAYS = 7  # skip near-expiry options (pin risk distorts IV)

# Canonical high_iv cut for the IV regime gate. Matches the backtest evidence
# exactly (docs/IV_HIGH_ONLY_AB_SPLIT.md: high_iv = DVOL percentile(30d) > 66.7).
IV_HIGH_PCT = 66.7


async def fetch_dvol(
    currency: str, start_ms: int, end_ms: int
) -> List[Tuple[int, float]]:
    """Daily DVOL/vol-index closes: [(ts_ms, close), ...].

    Async (aiohttp — the codebase-wide HTTP client; also keeps the security
    audit's urllib/requests rule from firing). Each call opens a short-lived
    session, so offline scripts can run it via ``asyncio.run(...)``.
    """
    url = (
        f"{DERIBIT_URL}?currency={currency}&start_timestamp={start_ms}"
        f"&end_timestamp={end_ms}&resolution=86400"
    )
    timeout = aiohttp.ClientTimeout(total=25)
    async with make_client_session(timeout=timeout) as session:
        async with session.get(url, headers={"User-Agent": "research/1.0"}) as resp:
            resp.raise_for_status()
            payload = await resp.json()
    data = payload.get("result", {}).get("data", [])
    return [(int(row[0]), float(row[4])) for row in data]


async def _deribit_get(url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=25)
    async with make_client_session(timeout=timeout) as session:
        async with session.get(
            url, params=params, headers={"User-Agent": "research/1.0"}
        ) as resp:
            resp.raise_for_status()
            return await resp.json()


def _parse_deribit_option_name(name: str) -> Tuple[Optional[int], Optional[float]]:
    """``BTC-26SEP25-100000-C`` -> (expiry_ms, strike). None on parse fail."""
    try:
        _cur, dstr, strike, _cp = name.split("-")
        exp = _dt.datetime.strptime(dstr, "%d%b%y").replace(
            hour=8, tzinfo=_dt.timezone.utc)  # Deribit expiry = 08:00 UTC
        return int(exp.timestamp() * 1000), float(strike)
    except (ValueError, AttributeError):
        return None, None


async def fetch_deribit_market_snapshot(currency: str) -> Dict[str, Any]:
    """Perp basis + ATM IV snapshot for ``currency`` (free public endpoints).

    - ``index_price`` / ``perp_price`` / ``basis_bps`` (simple perp-index
      spread in bps — not annualized; it is a level, not a carry estimate)
    - ``atm_iv``: mark_iv of the option closest to the money with expiry
      >= ``ATM_MIN_EXPIRY_DAYS`` out.
    """
    cur = currency.upper()
    now_ms = int(time.time() * 1000)
    out: Dict[str, Any] = {
        "currency": cur, "timestamp_ms": now_ms,
        "index_price": None, "perp_price": None, "basis_bps": None,
        "atm_iv": None, "atm_expiry_ms": None, "atm_strike": None,
        "n_options": 0,
    }
    tick = await _deribit_get(DERIBIT_TICKER_URL, {
        "instrument_name": f"{cur}-PERPETUAL",
    })
    res = tick.get("result") or {}
    idx, perp = res.get("index_price"), res.get("mark_price")
    if idx and perp:
        out["index_price"] = float(idx)
        out["perp_price"] = float(perp)
        out["basis_bps"] = (float(perp) - float(idx)) / float(idx) * 10_000.0

    book = await _deribit_get(DERIBIT_BOOK_URL, {
        "currency": cur, "kind": "option",
    })
    instruments = book.get("result") or []
    out["n_options"] = len(instruments)
    min_exp_ms = now_ms + ATM_MIN_EXPIRY_DAYS * 86_400_000
    best: Optional[Tuple[float, Dict[str, Any], int, float]] = None
    for inst in instruments:
        name = str(inst.get("instrument_name", ""))
        exp_ms, strike = _parse_deribit_option_name(name)
        und = inst.get("underlying_price") or res.get("index_price")
        iv = inst.get("mark_iv")
        if exp_ms is None or strike is None or not und or iv is None:
            continue
        if exp_ms < min_exp_ms:
            continue
        moneyness = abs(math.log(float(strike) / float(und)))
        if best is None or moneyness < best[0]:
            best = (moneyness, inst, exp_ms, float(strike))
    if best is not None:
        out["atm_iv"] = float(best[1]["mark_iv"])
        out["atm_expiry_ms"] = best[2]
        out["atm_strike"] = best[3]
    return out


def build_iv_percentile(
    closes: List[Tuple[int, float]], window_days: int = DVOL_WINDOW_DAYS
) -> List[Tuple[int, Optional[float]]]:
    """Asof percentile of each day's close within the trailing window.

    Percentile at day ``t`` uses only closes on ``[t - window, t]`` (all <= t),
    so it is known by the close of day ``t``. A trade entering during day
    ``t+1`` uses the percentile of day ``t`` (see ``iv_pct_at``).
    """
    out: List[Tuple[int, Optional[float]]] = []
    ts_list = [t for t, _ in closes]
    vals = [c for _, c in closes]
    for i, (t, c) in enumerate(closes):
        lo = bisect.bisect_left(ts_list, t - window_days * 86_400_000)
        window = vals[lo : i + 1]
        if len(window) < 20:
            out.append((t, None))
            continue
        pct = 100.0 * sum(1 for v in window if v <= c) / len(window)
        out.append((t, pct))
    return out


def iv_pct_at(series: List[Tuple[int, Optional[float]]], ts: int) -> Optional[float]:
    """IV percentile of the last completed DVOL day before ``ts``."""
    if not series:
        return None
    # last DVOL close strictly before the trade day (end-of-day known value)
    idx = bisect.bisect_left([t for t, _ in series], ts - 86_400_000) - 1
    if idx < 0:
        return None
    return series[idx][1]


def dvol_series_for(
    sym: str,
    btc_iv: List[Tuple[int, Optional[float]]],
    eth_iv: List[Tuple[int, Optional[float]]],
) -> List[Tuple[int, Optional[float]]]:
    """BTC/ETH use their own index; everything else uses BTC as global proxy."""
    if sym == "BTC":
        return btc_iv
    if sym == "ETH":
        return eth_iv
    return btc_iv


def dvol_currency_for(symbol: str) -> str:
    """DVOL index currency for a trade symbol (BTC/ETH native, else BTC proxy).

    Mirrors ``dvol_series_for`` so production and backtest classify every
    symbol against the same index.
    """
    s = str(symbol).strip().upper()
    return s if s in DVOL_CURRENCIES else "BTC"


def classify_iv(
    percentile: Optional[float], threshold: float = IV_HIGH_PCT
) -> str:
    """Shadow-gate decision: ``high_iv`` / ``low_iv`` / ``unknown``.

    ``high_iv`` = trailing-30d DVOL percentile strictly above the threshold
    (the backtest gate in docs/IV_HIGH_ONLY_AB_SPLIT.md). ``None`` (no DVOL
    history yet) is ``unknown`` and must never be treated as a block.
    """
    if percentile is None:
        return "unknown"
    return "high_iv" if float(percentile) > float(threshold) else "low_iv"


class DvolFeed:
    """Async background task: fetch daily DVOL closes → research DB."""

    def __init__(
        self,
        db: ResearchDatabase,
        currencies: Sequence[str],
        *,
        interval_sec: float = 86_400.0,
        lookback_days: int = 60,
        window_days: int = DVOL_WINDOW_DAYS,
    ) -> None:
        self._db = db
        self._currencies = [str(c).strip().upper() for c in currencies] or list(DVOL_CURRENCIES)
        self._interval = max(3_600.0, float(interval_sec))
        self._lookback_ms = int(max(30, int(lookback_days))) * 86_400_000
        self._window_days = int(window_days)
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last_error: Optional[str] = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="dvol_feed")
        logger.info(
            "DvolFeed started — %s every %.1fh → %s",
            ",".join(self._currencies),
            self._interval / 3600.0,
            self._db.db_path,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._fetch_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                logger.warning("DvolFeed cycle failed: %s", exc)
            await asyncio.sleep(self._interval)

    async def _fetch_once(self) -> int:
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - self._lookback_ms
        total = 0
        for currency in self._currencies:
            rows = await fetch_dvol(currency, start_ms, end_ms)
            if rows:
                n = self._db.save_dvol_daily(
                    [(currency, ts, close) for ts, close in rows]
                )
                total += n
                logger.info(
                    "DvolFeed %s: %d closes persisted (%.0fd lookback)",
                    currency,
                    n,
                    self._lookback_ms / 86_400_000,
                )
            # Perp basis + ATM IV snapshot — same cadence, same table set.
            try:
                snap = await fetch_deribit_market_snapshot(currency)
                self._db.save_deribit_snapshot(snap)
            except Exception as exc:  # noqa: BLE001
                logger.warning("DvolFeed %s market snapshot failed: %s",
                               currency, exc)
        self._last_error = None if total else "no_data"
        return total

    def status(self) -> dict:
        return {
            "currencies": list(self._currencies),
            "interval_sec": self._interval,
            "lookback_days": self._lookback_ms // 86_400_000,
            "window_days": self._window_days,
            "running": self._running,
            "last_error": self._last_error,
        }


def start_dvol_feed_from_config(cfg: Any) -> Optional[DvolFeed]:
    """Build the DVOL feed from ``research.dvol_feed``; None when disabled."""
    research = cfg.get("research", {}) or {}
    section = research.get("dvol_feed", {}) or {}
    if not bool(section.get("enabled", False)):
        return None
    db = ResearchDatabase(ResearchDatabase.resolve_path(cfg))
    currencies = section.get("currencies") or list(DVOL_CURRENCIES)
    interval_sec = float(section.get("interval_hours", 24.0)) * 3600.0
    return DvolFeed(
        db,
        currencies,
        interval_sec=interval_sec,
        lookback_days=int(section.get("lookback_days", 60)),
        window_days=int(section.get("window_days", DVOL_WINDOW_DAYS)),
    )


def current_dvol_percentile(
    currency: str,
    ts_ms: Optional[int] = None,
    db: Optional[ResearchDatabase] = None,
    window_days: int = DVOL_WINDOW_DAYS,
) -> Optional[float]:
    """Trailing IV percentile at ``ts_ms`` (default now) for the regime gate.

    Uses the last completed day's percentile (no lookahead) — the same value
    the backtest evidence attaches to a trade entering on day ``ts_ms``.
    """
    ts = int(ts_ms if ts_ms is not None else time.time() * 1000)
    rdb = db if db is not None else ResearchDatabase.open()
    lookback = int(max(2 * window_days + 5, 45)) * 86_400_000
    closes = rdb.load_dvol_daily(currency.upper(), ts - lookback, ts)
    if not closes:
        return None
    series = build_iv_percentile(closes, window_days)
    return iv_pct_at(series, ts)
