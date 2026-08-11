"""Top-trader position tracker for Hyperliquid (research / shadow features).

Polls public ``clearinghouseState`` for a configured wallet list and aggregates
per-symbol long/short notional. No copy-trading / no execution — feature feed
only. Wallets come from config or ``data/research/top_traders.json``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from src.utils.helpers import safe_float, validate_safe_path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class TopTraderSymbolSnapshot:
    """Aggregated positioning of tracked wallets in one coin."""

    symbol: str
    n_wallets: int
    n_long: int
    n_short: int
    long_notional_usd: float
    short_notional_usd: float
    net_bias: float  # (long - short) / (long + short), -1..+1
    long_frac: float  # long / (long + short), 0..1
    updated_ms: int


@dataclass
class TopTraderTracker:
    """Async poller + in-memory snapshots (singleton used by strategy)."""

    wallets: List[str] = field(default_factory=list)
    symbols: List[str] = field(default_factory=list)
    top_n: int = 10
    min_notional_usd: float = 10_000.0
    poll_interval_sec: float = 60.0
    request_delay_sec: float = 0.15
    enabled: bool = True

    def __post_init__(self) -> None:
        self._snapshots: Dict[str, TopTraderSymbolSnapshot] = {}
        self._lock = asyncio.Lock()
        self._client: Any = None
        self._running = False
        self._last_poll_ms: int = 0
        self._last_error: Optional[str] = None
        self._on_poll: Optional[Any] = None  # sync callback(snaps) after poll
        self._persist_samples: bool = True
        self._wallets_path: str = "data/research/top_traders.json"
        self._auto_from_leaderboard: bool = False
        self._leaderboard_window: str = "allTime"
        self._leaderboard_refresh_ms: int = 24 * 3_600_000
        self._min_account_value: float = 100_000.0
        self._min_volume: float = 5_000_000.0
        self._require_month_positive: bool = True
        self._last_leaderboard_refresh_ms: int = 0
        self._leaderboard_source: Optional[str] = None

    def set_on_poll(self, callback: Optional[Any]) -> None:
        """Optional sync callback invoked with snapshot dict after each poll."""
        self._on_poll = callback

    def configure_leaderboard(
        self,
        *,
        enabled: bool,
        wallets_path: str,
        window: str = "allTime",
        refresh_hours: float = 24.0,
        min_account_value: float = 100_000.0,
        min_volume: float = 5_000_000.0,
        require_month_positive: bool = True,
    ) -> None:
        self._auto_from_leaderboard = bool(enabled)
        self._wallets_path = str(wallets_path)
        self._leaderboard_window = str(window or "allTime")
        self._leaderboard_refresh_ms = int(max(1.0, float(refresh_hours)) * 3_600_000)
        self._min_account_value = float(min_account_value)
        self._min_volume = float(min_volume)
        self._require_month_positive = bool(require_month_positive)

    async def refresh_from_leaderboard(self, *, force: bool = False) -> int:
        """Pull durable top-N from HL stats leaderboard; returns wallet count."""
        if not self._auto_from_leaderboard and not force:
            return len(self.wallets)
        now = int(time.time() * 1000)
        if (
            not force
            and self.wallets
            and self._last_leaderboard_refresh_ms > 0
            and (now - self._last_leaderboard_refresh_ms) < self._leaderboard_refresh_ms
        ):
            return len(self.wallets)
        try:
            from src.exchanges.hl_leaderboard import (
                fetch_durable_top_wallets,
                wallets_payload,
            )

            selected = await fetch_durable_top_wallets(
                top_n=self.top_n,
                window=self._leaderboard_window,
                min_account_value=self._min_account_value,
                min_volume=self._min_volume,
                require_month_positive=self._require_month_positive,
            )
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"leaderboard_refresh_failed:{exc}"
            logger.warning("TopTrader leaderboard refresh failed: %s", exc)
            return len(self.wallets)
        if not selected:
            self._last_error = "leaderboard_empty"
            logger.warning("TopTrader leaderboard returned 0 wallets after filters")
            return len(self.wallets)
        self.set_wallets([w.address for w in selected])
        self._last_leaderboard_refresh_ms = now
        self._leaderboard_source = "stats-data.hyperliquid.xyz"
        payload = wallets_payload(selected)
        try:
            self._write_wallets_file(payload)
        except Exception as exc:  # noqa: BLE001
            logger.debug("TopTrader wallets file write skipped: %s", exc)
        logger.info(
            "TopTrader leaderboard refreshed — top_n=%d window=%s",
            len(self.wallets),
            self._leaderboard_window,
        )
        return len(self.wallets)

    def _write_wallets_file(self, payload: Dict[str, Any]) -> None:
        raw = Path(self._wallets_path)
        if not raw.is_absolute():
            candidate = ROOT / raw
            rel = Path(str(self._wallets_path).replace("\\", "/"))
        else:
            candidate = raw
            rel = candidate.resolve().relative_to(ROOT)
        safe = validate_safe_path(rel.as_posix())
        if safe is None:
            raise ValueError(f"unsafe wallets path: {self._wallets_path}")
        path = Path(safe) if Path(safe).is_absolute() else ROOT / safe
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def load_wallets_from_path(self, path: str | Path) -> int:
        """Load wallet addresses from JSON; returns count loaded."""
        raw = Path(path)
        try:
            if raw.is_absolute():
                rel = raw.resolve().relative_to(ROOT)
            else:
                rel = Path(str(path).replace("\\", "/"))
        except ValueError:
            logger.warning("TopTraderTracker wallets path outside project: %s", path)
            return 0
        safe = validate_safe_path(rel.as_posix())
        if safe is None:
            logger.warning("TopTraderTracker wallets path rejected: %s", path)
            return 0
        p = Path(safe) if Path(safe).is_absolute() else ROOT / safe
        if not p.exists():
            logger.warning("TopTraderTracker wallets file missing: %s", p)
            return 0
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("TopTraderTracker wallets file unreadable: %s", exc)
            return 0
        wallets: List[str] = []
        if isinstance(data, dict):
            rows = data.get("wallets") or data.get("addresses") or []
        elif isinstance(data, list):
            rows = data
        else:
            rows = []
        for row in rows:
            if isinstance(row, str):
                addr = row.strip()
            elif isinstance(row, dict):
                addr = str(row.get("address") or row.get("user") or "").strip()
            else:
                continue
            if (
                addr.startswith("0x")
                and len(addr) >= 42
                and "replace" not in addr.lower()
            ):
                wallets.append(addr.lower())
        # de-dupe preserve order
        seen = set()
        uniq: List[str] = []
        for w in wallets:
            if w not in seen:
                seen.add(w)
                uniq.append(w)
        self.wallets = uniq[: max(1, int(self.top_n))] if self.top_n > 0 else uniq
        logger.info(
            "TopTraderTracker loaded %d wallets (top_n=%d) from %s",
            len(self.wallets),
            self.top_n,
            p,
        )
        return len(self.wallets)

    def set_wallets(self, wallets: Sequence[str]) -> None:
        seen = set()
        uniq: List[str] = []
        for w in wallets:
            addr = str(w).strip().lower()
            if addr.startswith("0x") and addr not in seen:
                seen.add(addr)
                uniq.append(addr)
        self.wallets = uniq[: max(1, int(self.top_n))] if self.top_n > 0 else uniq

    def get_snapshot(self, symbol: str) -> Optional[TopTraderSymbolSnapshot]:
        return self._snapshots.get(symbol.upper())

    def all_snapshots(self) -> Dict[str, TopTraderSymbolSnapshot]:
        return dict(self._snapshots)

    async def bind_client(self, client: Any) -> None:
        self._client = client

    async def poll_once(self) -> Dict[str, TopTraderSymbolSnapshot]:
        """Fetch clearinghouseState for each wallet and rebuild aggregates."""
        if not self.enabled:
            return {}
        if not self.wallets:
            self._last_error = "no_wallets"
            return {}
        if self._client is None:
            self._last_error = "no_client"
            return {}

        coin_filter = {s.upper() for s in self.symbols} if self.symbols else None
        # per coin: long_notional, short_notional, n_long, n_short, wallet set
        long_n: Dict[str, float] = {}
        short_n: Dict[str, float] = {}
        n_long: Dict[str, int] = {}
        n_short: Dict[str, int] = {}
        now_ms = int(time.time() * 1000)
        errors = 0

        for addr in self.wallets:
            try:
                raw = await self._client.clearinghouse_state(addr)
            except Exception as exc:  # noqa: BLE001
                errors += 1
                logger.debug("TopTraderTracker clearinghouse %s: %s", addr, exc)
                await asyncio.sleep(self.request_delay_sec)
                continue
            for pos in _iter_positions(raw, coins=coin_filter):
                coin = pos["coin"]
                notional = pos["notional"]
                if notional < self.min_notional_usd:
                    continue
                if pos["side"] == "long":
                    long_n[coin] = long_n.get(coin, 0.0) + notional
                    n_long[coin] = n_long.get(coin, 0) + 1
                else:
                    short_n[coin] = short_n.get(coin, 0.0) + notional
                    n_short[coin] = n_short.get(coin, 0) + 1
            await asyncio.sleep(self.request_delay_sec)

        snaps: Dict[str, TopTraderSymbolSnapshot] = {}
        coins = set(long_n) | set(short_n)
        for coin in coins:
            ln = float(long_n.get(coin, 0.0))
            sn = float(short_n.get(coin, 0.0))
            tot = ln + sn
            if tot <= 0:
                continue
            snaps[coin] = TopTraderSymbolSnapshot(
                symbol=coin,
                n_wallets=int(n_long.get(coin, 0) + n_short.get(coin, 0)),
                n_long=int(n_long.get(coin, 0)),
                n_short=int(n_short.get(coin, 0)),
                long_notional_usd=ln,
                short_notional_usd=sn,
                net_bias=(ln - sn) / tot,
                long_frac=ln / tot,
                updated_ms=now_ms,
            )

        async with self._lock:
            self._snapshots = snaps
            self._last_poll_ms = now_ms
            self._last_error = f"errors={errors}" if errors else None
        if snaps and self._persist_samples:
            try:
                from src.research.top_trader_store import TopTraderStore

                TopTraderStore().persist_bias_samples(snaps)
            except Exception as exc:  # noqa: BLE001
                logger.debug("TopTrader bias persist skipped: %s", exc)
        if self._on_poll is not None:
            try:
                self._on_poll(snaps)
            except Exception as exc:  # noqa: BLE001
                logger.debug("TopTrader on_poll callback failed: %s", exc)
        logger.info(
            "TopTraderTracker poll: wallets=%d symbols=%d errors=%d",
            len(self.wallets),
            len(snaps),
            errors,
        )
        return snaps

    async def run_loop(self) -> None:
        """Background poll until cancelled."""
        self._running = True
        if self._auto_from_leaderboard:
            try:
                await self.refresh_from_leaderboard(force=not bool(self.wallets))
            except Exception as exc:  # noqa: BLE001
                logger.warning("TopTrader initial leaderboard refresh: %s", exc)
        while self._running:
            try:
                if self._auto_from_leaderboard:
                    await self.refresh_from_leaderboard(force=False)
                await self.poll_once()
            except asyncio.CancelledError:
                self._running = False
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("TopTraderTracker loop failed: %s", exc)
            await asyncio.sleep(max(5.0, float(self.poll_interval_sec)))

    def stop(self) -> None:
        self._running = False


_TRACKER: Optional[TopTraderTracker] = None


def get_tracker() -> Optional[TopTraderTracker]:
    return _TRACKER


def set_tracker(tracker: Optional[TopTraderTracker]) -> None:
    global _TRACKER
    _TRACKER = tracker


def get_top_trader_snapshot(symbol: str) -> Optional[TopTraderSymbolSnapshot]:
    tr = _TRACKER
    if tr is None:
        return None
    return tr.get_snapshot(symbol)


def _iter_positions(
    raw: Any,
    *,
    coins: Optional[set[str]] = None,
) -> List[Dict[str, Any]]:
    """Parse clearinghouseState into simple position dicts (liqPx optional)."""
    if not isinstance(raw, dict):
        return []
    out: List[Dict[str, Any]] = []
    for item in raw.get("assetPositions", []) or []:
        if not isinstance(item, dict):
            continue
        pos = item.get("position")
        if not isinstance(pos, dict):
            continue
        coin = str(pos.get("coin", "")).upper()
        if not coin:
            continue
        if coins is not None and coin not in coins:
            continue
        szi = safe_float(pos.get("szi"))
        if abs(szi) < 1e-12:
            continue
        entry_px = safe_float(pos.get("entryPx"))
        position_value = abs(safe_float(pos.get("positionValue")))
        if position_value > 0:
            notional = position_value
        elif entry_px > 0:
            notional = abs(szi) * entry_px
        else:
            continue
        out.append(
            {
                "coin": coin,
                "side": "long" if szi > 0 else "short",
                "notional": notional,
                "szi": szi,
            }
        )
    return out


def build_tracker_from_config(config: Any) -> Optional[TopTraderTracker]:
    """Construct tracker from YAML ``market_data.top_trader_tracker`` + strategy cfg."""
    md = {}
    strat = {}
    if hasattr(config, "get"):
        md = config.get("market_data.top_trader_tracker", {}) or {}
        strat = config.get("strategy.top_trader_flow", {}) or {}
    if not bool(md.get("enabled", strat.get("enabled", False))):
        return None

    top_n = int(md.get("top_n", strat.get("top_n", 10)))
    wallets_path = str(
        md.get("wallets_path")
        or strat.get("wallets_path")
        or "data/research/top_traders.json"
    )
    tracker = TopTraderTracker(
        top_n=top_n,
        min_notional_usd=float(md.get("min_notional_usd", 10_000.0)),
        poll_interval_sec=float(md.get("poll_interval_sec", 60.0)),
        request_delay_sec=float(md.get("request_delay_sec", 0.15)),
        enabled=True,
    )
    symbols = []
    if hasattr(config, "get"):
        symbols = list(config.get("symbols", []) or config.get("assets", []) or [])
    tracker.symbols = [str(s).upper() for s in symbols]
    tracker.configure_leaderboard(
        enabled=bool(md.get("auto_from_leaderboard", True)),
        wallets_path=wallets_path,
        window=str(md.get("leaderboard_window", "allTime")),
        refresh_hours=float(md.get("leaderboard_refresh_hours", 24.0)),
        min_account_value=float(md.get("min_account_value", 100_000.0)),
        min_volume=float(md.get("min_volume", 5_000_000.0)),
        require_month_positive=bool(md.get("require_month_positive", True)),
    )

    wallets = list(md.get("wallets") or strat.get("wallets") or [])
    if wallets:
        tracker.set_wallets([str(w) for w in wallets])
    else:
        tracker.load_wallets_from_path(wallets_path)
    set_tracker(tracker)
    return tracker
