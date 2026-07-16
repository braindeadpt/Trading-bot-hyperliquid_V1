"""Tests for Hyperliquid-native liquidation map infrastructure."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.candle_providers.node_trades_parser import iter_fill_legs_with_address
from src.data.research_database import ResearchDatabase
from src.research.liquidation_map import (
    HlOpenPosition,
    build_zones,
    harvest_addresses,
    load_latest_snapshot,
    parse_clearinghouse_positions,
    persist_snapshot,
)

pytestmark = pytest.mark.unit


def _fill(coin, px, sz, side, time_ms, tid, oid, crossed, address="0xaddr", start_position="0"):
    return [
        address,
        {
            "coin": coin,
            "px": px,
            "sz": sz,
            "side": side,
            "time": time_ms,
            "tid": tid,
            "oid": oid,
            "crossed": crossed,
            "hash": "0xhash",
            "startPosition": start_position,
        },
    ]


def _block(block_number, events):
    return {
        "local_time": "2026-07-10T23:00:00.119184915",
        "block_time": "2026-07-10T22:59:59.921821211",
        "block_number": block_number,
        "events": events,
    }


def test_iter_fill_legs_preserves_both_counterparty_addresses() -> None:
    open_ms = 1_800_000_000_000
    events = [
        _fill("BTC", "64361.0", "0.31", "A", open_ms, tid=1, oid=100, crossed=False, address="0xAAAA"),
        _fill("BTC", "64361.0", "0.31", "B", open_ms, tid=1, oid=101, crossed=True, address="0xBBBB"),
    ]
    legs = list(iter_fill_legs_with_address([_block(1, events)]))
    assert len(legs) == 2
    addrs = {leg.address for leg in legs}
    assert addrs == {"0xaaaa", "0xbbbb"}
    assert all(leg.coin == "BTC" for leg in legs)
    assert all(leg.size == pytest.approx(0.31) for leg in legs)


def test_harvest_addresses_top_n_and_min_notional() -> None:
    t = 1_800_000_000_000
    # whale: 2 BTC * 50k = 100k notional
    # mid: 1 BTC * 50k = 50k (exactly at threshold)
    # dust: 0.1 BTC * 50k = 5k (filtered)
    blocks = [
        _block(
            1,
            [
                _fill("BTC", "50000", "2.0", "B", t, 1, 1, True, address="0xwhale"),
                _fill("BTC", "50000", "2.0", "A", t, 1, 2, False, address="0xcounter1"),
                _fill("BTC", "50000", "1.0", "B", t + 1, 2, 3, True, address="0xmid"),
                _fill("BTC", "50000", "1.0", "A", t + 1, 2, 4, False, address="0xcounter2"),
                _fill("BTC", "50000", "0.1", "B", t + 2, 3, 5, True, address="0xdust"),
                _fill("BTC", "50000", "0.1", "A", t + 2, 3, 6, False, address="0xcounter3"),
            ],
        ),
    ]
    top = harvest_addresses(blocks, top_n=3, min_notional_usd=50_000.0)
    assert "0xdust" not in top
    assert top[0] == "0xwhale"
    assert "0xmid" in top
    assert len(top) <= 3


def test_parse_clearinghouse_skips_missing_liquidation_px() -> None:
    raw = {
        "assetPositions": [
            {
                "position": {
                    "coin": "BTC",
                    "szi": "0.5",
                    "entryPx": "60000",
                    "leverage": {"type": "cross", "value": 5},
                    "liquidationPx": "54000",
                    "marginUsed": "6000",
                },
            },
            {
                "position": {
                    "coin": "ETH",
                    "szi": "-2.0",
                    "entryPx": "3000",
                    "leverage": {"type": "cross", "value": 3},
                    "liquidationPx": None,
                    "marginUsed": "2000",
                },
            },
            {
                "position": {
                    "coin": "SOL",
                    "szi": "10",
                    "entryPx": "150",
                    "leverage": {"type": "isolated", "value": 2},
                    "liquidationPx": "0",
                    "marginUsed": "750",
                },
            },
        ],
    }
    positions = parse_clearinghouse_positions(raw, "0xAbc", fetched_at_ms=123)
    assert len(positions) == 1
    p = positions[0]
    assert p.coin == "BTC"
    assert p.side == "long"
    assert p.liquidation_px == pytest.approx(54000.0)
    assert p.notional_usd == pytest.approx(30000.0)
    assert p.fetched_at_ms == 123


def test_build_zones_hand_computed_bands() -> None:
    """Mark=100, bucket_pct=1.0 → bands of width 1.0.

    Long liq at 98.5 → bucket k=floor((0.985-1)/0.01)=floor(-1.5)=-2
      low=100*(1-0.02)=98, high=100*(1-0.01)=99
    Short liq at 102.2 → k=floor(0.022/0.01)=2
      low=102, high=103
    """
    positions = [
        HlOpenPosition(
            address="0xa", coin="BTC", szi=1.0, side="long", entry_px=100.0,
            leverage=5.0, liquidation_px=98.5, margin_used=20.0,
            notional_usd=150_000.0, fetched_at_ms=1,
        ),
        HlOpenPosition(
            address="0xb", coin="BTC", szi=1.0, side="long", entry_px=100.0,
            leverage=5.0, liquidation_px=98.7, margin_used=20.0,
            notional_usd=50_000.0, fetched_at_ms=1,
        ),
        HlOpenPosition(
            address="0xc", coin="BTC", szi=-1.0, side="short", entry_px=100.0,
            leverage=5.0, liquidation_px=102.2, margin_used=20.0,
            notional_usd=200_000.0, fetched_at_ms=1,
        ),
    ]
    zones = build_zones(
        positions,
        bucket_pct=1.0,
        min_zone_notional_usd=100_000.0,
        mark_prices={"BTC": 100.0},
    )
    assert len(zones) == 2
    long_zone = next(z for z in zones if z.side == "long")
    short_zone = next(z for z in zones if z.side == "short")
    assert long_zone.price_low == pytest.approx(98.0)
    assert long_zone.price_high == pytest.approx(99.0)
    assert long_zone.total_notional_usd == pytest.approx(200_000.0)
    assert long_zone.position_count == 2
    assert short_zone.price_low == pytest.approx(102.0)
    assert short_zone.price_high == pytest.approx(103.0)
    assert short_zone.total_notional_usd == pytest.approx(200_000.0)


def test_persist_and_reload_snapshot(tmp_path: Path) -> None:
    db_path = tmp_path / "research_liq.db"
    db = ResearchDatabase(db_path)
    try:
        zones = build_zones(
            [
                HlOpenPosition(
                    address="0xa", coin="ETH", szi=10.0, side="long", entry_px=3000.0,
                    leverage=4.0, liquidation_px=2700.0, margin_used=1000.0,
                    notional_usd=250_000.0, fetched_at_ms=99,
                ),
            ],
            bucket_pct=0.25,
            min_zone_notional_usd=100_000.0,
            mark_prices={"ETH": 3000.0},
        )
        assert zones
        sid = persist_snapshot(db, zones, {"n": 1}, snapshot_id="snap-test", fetched_at_ms=999)
        assert sid == "snap-test"
        loaded = load_latest_snapshot(db, coin="ETH")
        assert len(loaded) == 1
        assert loaded[0]["snapshot_id"] == "snap-test"
        assert loaded[0]["coin"] == "ETH"
        assert float(loaded[0]["total_notional_usd"]) == pytest.approx(250_000.0)
    finally:
        db.close()


def test_cli_dry_run_with_fixture(tmp_path: Path) -> None:
    """Dry-run harvest from synthetic NDJSON — no network."""
    import subprocess

    t = 1_800_000_000_000
    block = _block(
        1,
        [
            _fill("BTC", "60000", "2.0", "B", t, 1, 1, True, address="0xwhale1"),
            _fill("BTC", "60000", "2.0", "A", t, 1, 2, False, address="0xwhale2"),
        ],
    )
    fixture = tmp_path / "fills.ndjson"
    fixture.write_text(json.dumps(block) + "\n", encoding="utf-8")

    script = ROOT / "scripts" / "build_liquidation_map.py"
    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            "--from-fills",
            str(fixture),
            "--top-n",
            "10",
            "--min-notional-usd",
            "50000",
            "--coins",
            "BTC",
            "--dry-run",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "dry-run" in proc.stdout
    assert "api_calls_would_be_made" in proc.stdout


@pytest.mark.network
def test_network_clearinghouse_smoke() -> None:
    """Opt-in: hit real info API with a single address if available.

    Skips when offline / no known whale address configured.
    """
    import asyncio
    import os

    addr = os.environ.get("HL_LIQ_MAP_SMOKE_ADDRESS", "").strip()
    if not addr:
        pytest.skip("Set HL_LIQ_MAP_SMOKE_ADDRESS to enable network smoke")

    from src.exchanges.hyperliquid_rest import HyperliquidRESTClient
    from src.research.liquidation_map import fetch_positions

    async def _run() -> None:
        async with HyperliquidRESTClient() as client:
            result = await fetch_positions(
                [addr], client=client, delay_ms=0, max_addresses=1,
            )
        assert result.addresses_queried == 1
        # Soft assert: either positions or a clean empty book (no exception)
        assert isinstance(result.positions, list)

    asyncio.run(_run())
