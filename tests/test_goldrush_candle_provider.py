"""GoldRush / candle provider tests — no live API key required."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.data.candle_providers.base import CandlePage, INTERVAL_MS
from src.data.candle_providers.checkpoint import (
    BackfillCheckpoint,
    load_checkpoint,
    save_checkpoint,
)
from src.data.candle_providers.goldrush_hypercore import (
    GoldrushConfigError,
    GoldrushHypercoreCandleProvider,
)
from src.data.candle_providers.hyperliquid_public import HyperliquidPublicCandleProvider
from src.data.candle_providers.pagination import paginate_candles_chronological
from src.data.candle_providers.parity import compare_candle_overlap
from src.data.candle_providers.validation import (
    CandleValidationError,
    validate_candle_row,
)
from src.data.database import Candle
from src.data.research_database import ResearchDatabase
from src.data.series_metadata import SOURCE_GOLDRUSH, SOURCE_HL_CANDLE_SNAPSHOT, SeriesMetadata
import pytest

pytestmark = pytest.mark.integration_offline


def _row(
    symbol: str,
    interval: str,
    open_ms: int,
    *,
    o: str = "100.0",
    h: str = "101.0",
    l: str = "99.0",
    c: str = "100.5",
    v: str = "1.0",
) -> Dict[str, Any]:
    gap = INTERVAL_MS[interval]
    return {
        "s": symbol,
        "i": interval,
        "t": open_ms,
        "T": open_ms + gap - 1,
        "o": o,
        "h": h,
        "l": l,
        "c": c,
        "v": v,
        "n": 1,
    }


class _MockProvider:
    name = "goldrush_hypercore"  # type: ignore[assignment]

    def __init__(self, pages: List[List[Dict[str, Any]]]) -> None:
        self._pages = pages
        self._idx = 0

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def fetch_page(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
    ) -> CandlePage:
        rows = self._pages[self._idx] if self._idx < len(self._pages) else []
        self._idx += 1
        return CandlePage(
            rows=rows,
            symbol=symbol,
            interval=interval,
            request_start_ms=start_ms,
            request_end_ms=end_ms,
            provider="goldrush_hypercore",
        )


def test_goldrush_requires_env_key() -> None:
    with patch.dict(os.environ, {}, clear=True):
        try:
            GoldrushHypercoreCandleProvider()
            raised = False
        except GoldrushConfigError:
            raised = True
        assert raised


def test_validate_candle_row_rejects_bad_order() -> None:
    row = _row("BTC", "1m", 1_000_000)
    row["T"] = row["t"]
    try:
        validate_candle_row(row, interval="1m")
        raised = False
    except CandleValidationError:
        raised = True
    assert raised


def test_forward_pagination_dedupes_and_pages() -> None:
    gap = INTERVAL_MS["1m"]
    p1 = [_row("BTC", "1m", 1_000_000 + i * gap) for i in range(3)]
    p2 = [_row("BTC", "1m", 1_000_000 + i * gap) for i in range(2, 5)]
    provider = _MockProvider([p1, p2])

    async def _run() -> None:
        with patch("src.data.candle_providers.pagination.MAX_CANDLES_PER_PAGE", 2):
            result = await paginate_candles_chronological(
                provider, "BTC", "1m", 1_000_000, 1_000_000 + 10 * gap,
            )
        assert len(result.rows) == 5
        assert result.pages_fetched >= 2
        assert result.duplicates_skipped >= 1

    asyncio.run(_run())


def test_parity_detects_ohlc_divergence() -> None:
    gap = INTERVAL_MS["1h"]
    ts = 1_700_000_000_000
    meta = {"BTC": {"sz_decimals": 5, "px_decimals": 1}}
    official = [_row("BTC", "1h", ts, c="100.5")]
    goldrush = [_row("BTC", "1h", ts, c="101.5")]
    report = compare_candle_overlap(
        official, goldrush, symbol="BTC", interval="1h", meta_cache=meta,
    )
    assert report.matched_bars == 1
    assert not report.passed
    assert any(m.field == "c" for m in report.mismatches)


def test_parity_passes_identical_rows() -> None:
    gap = INTERVAL_MS["1h"]
    ts = 1_700_000_000_000
    row = _row("ETH", "1h", ts)
    report = compare_candle_overlap([row], [dict(row)], symbol="ETH", interval="1h")
    assert report.passed


def test_non_destructive_save_skips_official() -> None:
    path = Path(tempfile.gettempdir()) / f"gr_db_{uuid.uuid4().hex}.db"
    db = ResearchDatabase(path)
    meta_off = SeriesMetadata.hl_candles()
    meta_gr = SeriesMetadata.goldrush_candles()
    c = Candle(
        symbol="BTC",
        timestamp_ms=1_700_000_000_000,
        open=1.0,
        high=2.0,
        low=0.5,
        close=1.5,
        volume=10.0,
    )
    db.save_research_candles([c], "1h", meta_off)
    c2 = Candle(
        symbol="BTC",
        timestamp_ms=1_700_000_000_000,
        open=9.0,
        high=9.0,
        low=9.0,
        close=9.0,
        volume=99.0,
    )
    inserted, skipped = db.save_research_candles_non_destructive([c2], "1h", meta_gr)
    assert inserted == 0
    assert skipped == 1
    sources = db.get_candle_sources_in_range("BTC", "1h", c.timestamp_ms, c.timestamp_ms)
    assert sources[c.timestamp_ms] == SOURCE_HL_CANDLE_SNAPSHOT
    del db
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def test_goldrush_metadata_source() -> None:
    meta = SeriesMetadata.goldrush_candles()
    assert meta.source == SOURCE_GOLDRUSH
    assert meta.venue == "hyperliquid"
    assert meta.api_version == "hypercore-info-v1"


def test_checkpoint_roundtrip() -> None:
    root = Path(__file__).resolve().parent.parent
    research = root / "data" / "research"
    research.mkdir(parents=True, exist_ok=True)
    path = research / f"_test_gr_ckpt_{uuid.uuid4().hex}.json"
    try:
        cp = BackfillCheckpoint(
            provider="goldrush_hypercore",
            symbol="SOL",
            interval="5m",
            window_start_ms=100,
            window_end_ms=200,
            cursor_ms=150,
            rows_fetched=10,
        )
        save_checkpoint(cp, path)
        loaded = load_checkpoint(path)
        assert "SOL:5m" in loaded
        assert loaded["SOL:5m"].cursor_ms == 150
    finally:
        path.unlink(missing_ok=True)


def test_tca_proxy_tier_b_from_run_manifest() -> None:
    from src.backtest.run_manifest import resolve_fidelity_tier

    tier = resolve_fidelity_tier(
        use_microstructure_proxy=False,
        tca_mode="proxy",
        data_contract_tier="tier_a_hl_ohlc",
    )
    assert tier == "tier_b_tca_proxy"


if __name__ == "__main__":
    test_goldrush_requires_env_key()
    print("  env key guard OK")
    test_validate_candle_row_rejects_bad_order()
    print("  validation OK")
    test_forward_pagination_dedupes_and_pages()
    print("  pagination OK")
    test_parity_detects_ohlc_divergence()
    print("  parity divergence OK")
    test_parity_passes_identical_rows()
    print("  parity pass OK")
    test_non_destructive_save_skips_official()
    print("  non-destructive save OK")
    test_goldrush_metadata_source()
    print("  metadata OK")
    test_checkpoint_roundtrip()
    print("  checkpoint OK")
    test_tca_proxy_tier_b_from_run_manifest()
    print("  tier cap OK")
    print("test_goldrush_candle_provider: all passed")
