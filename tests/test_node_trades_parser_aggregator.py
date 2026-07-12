"""Unit tests: node_trades parser + trade->1m OHLCV aggregator (synthetic only)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest

from src.data.candle_providers.node_trades_aggregator import (
    aggregate_trades_to_ohlcv,
    bucket_open_ms,
)
from src.data.candle_providers.node_trades_parser import (
    NodeTradesParseError,
    NodeTradeRecord,
    parse_archive_object,
    parse_trade_record,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parse_trade_record_official_schema():
    rec = parse_trade_record({
        "coin": "BTC",
        "side": "B",
        "time": "2026-07-10T12:00:05.123",
        "px": "64361.0",
        "sz": "0.31",
        "hash": "0xabc",
    })
    assert rec.coin == "BTC"
    assert rec.side == "B"
    assert rec.price == 64361.0
    assert rec.size == 0.31
    assert rec.raw_hash == "0xabc"
    import datetime as dt
    expected = int(
        dt.datetime(2026, 7, 10, 12, 0, 5, 123000, tzinfo=dt.timezone.utc).timestamp() * 1000
    )
    assert rec.time_ms == expected


def test_parse_trade_record_epoch_ms_variant():
    rec = parse_trade_record({"symbol": "ETH", "price": 3000.5, "size": 1.2, "t": 1783685005000})
    assert rec.symbol == "ETH"
    assert rec.time_ms == 1783685005000


def test_parse_trade_record_missing_field_raises():
    with pytest.raises(NodeTradesParseError):
        parse_trade_record({"coin": "BTC", "px": "1", "sz": "1"})  # missing time


def test_parse_trade_record_non_dict_raises():
    with pytest.raises(NodeTradesParseError):
        parse_trade_record("not-a-dict")  # type: ignore[arg-type]


def test_parse_archive_object_jsonl_text():
    text = (
        '{"coin": "BTC", "px": "100.0", "sz": "1", "time": 1783685000000}\n'
        '{"coin": "BTC", "px": "101.0", "sz": "2", "time": 1783685000500}\n'
        "\n"  # blank line tolerated
    )
    trades = parse_archive_object(text)
    assert len(trades) == 2
    assert trades[0].price == 100.0
    assert trades[1].size == 2.0


def test_parse_archive_object_list_of_dicts():
    trades = parse_archive_object([
        {"coin": "SOL", "px": "10", "sz": "5", "time": 1783685000000},
    ])
    assert len(trades) == 1
    assert trades[0].coin == "SOL"


def test_parse_archive_object_skips_bad_records_but_keeps_good_ones(caplog):
    text = (
        '{"coin": "BTC", "px": "1", "sz": "1", "time": 1783685000000}\n'
        '{"coin": "BTC", "px": "1"}\n'  # missing sz/time -> skipped, not fatal
    )
    trades = parse_archive_object(text)
    assert len(trades) == 1


def test_parse_archive_object_raises_on_malformed_json_line():
    with pytest.raises(NodeTradesParseError):
        list(__import__(
            "src.data.candle_providers.node_trades_parser", fromlist=["iter_jsonl_records"],
        ).iter_jsonl_records("{not valid json"))


def test_maybe_decompress_lz4_passthrough_for_non_lz4_bytes():
    from src.data.candle_providers.node_trades_parser import maybe_decompress_lz4
    raw = b'{"coin": "BTC"}\n'
    assert maybe_decompress_lz4(raw) == raw


def test_maybe_decompress_lz4_raises_clear_error_without_lz4_installed(monkeypatch):
    from src.data.candle_providers import node_trades_parser as mod
    monkeypatch.setattr(mod, "_lz4_frame", None)
    lz4_magic_blob = b"\x04\x22\x4d\x18" + b"garbage"
    with pytest.raises(NodeTradesParseError, match="lz4"):
        mod.maybe_decompress_lz4(lz4_magic_blob)


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


def _trade(coin, price, size, time_ms, side="B"):
    return NodeTradeRecord(coin=coin, time_ms=time_ms, price=price, size=size, side=side)


def test_bucket_open_ms_floors_to_interval():
    gap = 60_000
    ts = 123_456_789
    assert bucket_open_ms(ts, "1m") == (ts // gap) * gap


def test_aggregate_known_ohlcv_from_synthetic_trades():
    base = 1_800_000_000_000  # arbitrary ms, aligned below
    open_ms = (base // 60_000) * 60_000
    trades = [
        _trade("BTC", 100.0, 1.0, open_ms + 0),
        _trade("BTC", 105.0, 2.0, open_ms + 10_000),
        _trade("BTC", 95.0, 1.5, open_ms + 20_000),
        _trade("BTC", 102.0, 0.5, open_ms + 59_999),  # last ms of bucket
    ]
    rows = aggregate_trades_to_ohlcv(trades, symbol="BTC", interval="1m")
    assert len(rows) == 1
    row = rows[0]
    assert row["t"] == open_ms
    assert row["T"] == open_ms + 59_999
    assert row["o"] == 100.0
    assert row["h"] == 105.0
    assert row["l"] == 95.0
    assert row["c"] == 102.0
    assert float(row["v"]) == pytest.approx(5.0)
    assert row["n"] == 4


def test_aggregate_trade_exactly_on_next_bucket_boundary_splits_buckets():
    open_ms = 1_800_000_000_000
    open_ms = (open_ms // 60_000) * 60_000
    trades = [
        _trade("BTC", 100.0, 1.0, open_ms + 59_999),  # last ms, bucket A
        _trade("BTC", 200.0, 1.0, open_ms + 60_000),  # first ms of next bucket B
    ]
    rows = aggregate_trades_to_ohlcv(trades, symbol="BTC", interval="1m")
    assert len(rows) == 2
    assert rows[0]["t"] == open_ms
    assert rows[0]["c"] == 100.0
    assert rows[1]["t"] == open_ms + 60_000
    assert rows[1]["c"] == 200.0


def test_aggregate_empty_bucket_gap_is_simply_absent_not_fabricated():
    open_ms = 1_800_000_000_000
    open_ms = (open_ms // 60_000) * 60_000
    trades = [
        _trade("BTC", 100.0, 1.0, open_ms),
        _trade("BTC", 110.0, 1.0, open_ms + 3 * 60_000),  # 2 buckets with no trades in between
    ]
    rows = aggregate_trades_to_ohlcv(trades, symbol="BTC", interval="1m")
    assert len(rows) == 2  # no synthetic bars for the empty buckets
    closes = sorted(int(r["T"]) for r in rows)
    assert closes[1] - closes[0] == 3 * 60_000


def test_aggregate_ignores_other_symbols():
    open_ms = 1_800_000_000_000
    open_ms = (open_ms // 60_000) * 60_000
    trades = [
        _trade("BTC", 100.0, 1.0, open_ms),
        _trade("ETH", 3000.0, 1.0, open_ms),
    ]
    rows = aggregate_trades_to_ohlcv(trades, symbol="BTC", interval="1m")
    assert len(rows) == 1
    assert rows[0]["o"] == 100.0


def test_aggregate_applies_hl_price_quantum_rounding():
    # BTC default sz_decimals=5 -> perp cap = 6-5=1 decimal; price > 100k rounds to int.
    open_ms = 1_800_000_000_000
    open_ms = (open_ms // 60_000) * 60_000
    trades = [_trade("BTC", 150000.456, 1.0, open_ms)]
    rows = aggregate_trades_to_ohlcv(trades, symbol="BTC", interval="1m")
    assert rows[0]["o"] == 150000.0  # rounded per format_hl_price >100_000 rule


def test_aggregate_unsupported_interval_raises():
    with pytest.raises(ValueError):
        aggregate_trades_to_ohlcv([], symbol="BTC", interval="2m")
