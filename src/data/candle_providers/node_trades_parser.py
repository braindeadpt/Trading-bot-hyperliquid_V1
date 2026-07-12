"""Parser for Hyperliquid official ``node_trades`` archive records.

Reference (confirmed via official docs, July 2026):

* Historical market/trade archives are published to the requester-pays bucket
  ``s3://hyperliquid-archive`` with key layout
  ``market_data/[date]/[hour]/[datatype]/[coin].lz4``
  (https://hyperliquid.gitbook.io/hyperliquid-docs/historical-data).
  Node-level trade dumps additionally live under
  ``s3://hl-mainnet-node-data/node_trades`` (older/raw format) and
  ``s3://hl-mainnet-node-data/node_fills`` / ``node_fills_by_block``
  (API-aligned format, streamed via ``--write-fills --batch-by-block``).
* Both archive families are LZ4-compressed, newline-delimited JSON.
* Historical uploads to ``hyperliquid-archive`` happen roughly monthly with
  "no guarantee of timely updates"; requester pays S3 egress
  (``--request-payer requester``).
* The node trade / L1 data schema
  (https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/nodes/l1-data-schemas)
  documents one JSON object per trade with fields:

    {
      "coin": "COMP",
      "side": "B",                     # "B" buy / "A" ask(sell)
      "time": "2024-07-26T08:26:25.899",  # ISO-8601, millisecond precision
      "px": "51.367",                  # price, string
      "sz": "0.31",                    # size, string
      "hash": "0xad8e...",
      "trade_dir_override": "Na",
      "side_info": [ {"user": "...", "start_pos": "...", "oid": 123, ...}, ... ]
    }

Because the exact bucket/key template is not pinned down authoritatively for
every archive generation (the docs describe two divergent node_trades
formats), this module treats the record shape defensively: any dict-like
record carrying a recognizable coin/price/size/time field is accepted, and
unknown extra fields (``hash``, ``side_info``, ``trade_dir_override``, ...)
are ignored. This keeps the parser usable against both the official
``node_trades`` layout and the API-aligned ``node_fills`` layout without
code changes if HL adjusts field names.

This module performs no I/O. It only turns already-fetched bytes/text/dicts
into normalized :class:`NodeTradeRecord` instances.
"""

from __future__ import annotations

import datetime as _dt
import io
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, List, Optional, Union

from src.utils.helpers import safe_float

logger = logging.getLogger(__name__)

# Optional LZ4 support — the archives are LZ4-compressed but decoding is only
# required when a fetcher hands back raw compressed bytes. JSONL/dict input
# (as produced by tests and by fetchers that pre-decompress) never needs it.
try:  # pragma: no cover - exercised indirectly by decompression tests
    import lz4.frame as _lz4_frame  # type: ignore
except ImportError:  # pragma: no cover
    _lz4_frame = None


class NodeTradesParseError(ValueError):
    """A trade record/archive blob could not be parsed."""


@dataclass(frozen=True)
class NodeTradeRecord:
    """Normalized single trade from a node_trades / node_fills archive."""

    coin: str
    time_ms: int
    price: float
    size: float
    side: Optional[str] = None  # "B" (buy) / "A" (ask, sell) when present
    raw_hash: Optional[str] = None

    @property
    def symbol(self) -> str:
        return self.coin.upper()


_TIME_KEYS = ("time", "t", "timestamp", "timestamp_ms", "time_ms")
_COIN_KEYS = ("coin", "symbol", "s")
_PRICE_KEYS = ("px", "price", "p")
_SIZE_KEYS = ("sz", "size", "qty", "q")
_SIDE_KEYS = ("side", "dir")
_HASH_KEYS = ("hash", "tid", "trade_id")


def _first_present(record: Dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in record and record[key] is not None:
            return record[key]
    return None


def _parse_time_to_ms(value: Any) -> int:
    """Accept epoch ms (int/float/numeric str) or ISO-8601 timestamp strings."""
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise NodeTradesParseError("empty time field")
        try:
            return int(float(text))
        except ValueError:
            pass
        iso = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            dt = _dt.datetime.fromisoformat(iso)
        except ValueError as exc:
            raise NodeTradesParseError(f"unparseable time value: {value!r}") from exc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return int(dt.timestamp() * 1000)
    raise NodeTradesParseError(f"unsupported time field type: {type(value)!r}")


def parse_trade_record(record: Dict[str, Any]) -> NodeTradeRecord:
    """Normalize one raw trade dict into a :class:`NodeTradeRecord`."""
    if not isinstance(record, dict):
        raise NodeTradesParseError(f"trade record must be a dict, got {type(record)!r}")

    coin = _first_present(record, _COIN_KEYS)
    price = _first_present(record, _PRICE_KEYS)
    size = _first_present(record, _SIZE_KEYS)
    time_val = _first_present(record, _TIME_KEYS)

    if coin is None:
        raise NodeTradesParseError(f"missing coin/symbol field: {record!r}")
    if price is None:
        raise NodeTradesParseError(f"missing price field: {record!r}")
    if size is None:
        raise NodeTradesParseError(f"missing size field: {record!r}")
    if time_val is None:
        raise NodeTradesParseError(f"missing time field: {record!r}")

    return NodeTradeRecord(
        coin=str(coin).upper(),
        time_ms=_parse_time_to_ms(time_val),
        price=safe_float(price),
        size=safe_float(size),
        side=str(record[_first_key(record, _SIDE_KEYS)]) if _first_key(record, _SIDE_KEYS) else None,
        raw_hash=str(record[_first_key(record, _HASH_KEYS)]) if _first_key(record, _HASH_KEYS) else None,
    )


def _first_key(record: Dict[str, Any], keys: Iterable[str]) -> Optional[str]:
    for key in keys:
        if key in record and record[key] is not None:
            return key
    return None


def iter_jsonl_records(text: str) -> Iterator[Dict[str, Any]]:
    """Yield JSON objects from newline-delimited JSON text (blank lines skipped)."""
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise NodeTradesParseError(f"invalid JSON on line {lineno}: {exc}") from exc
        if isinstance(obj, dict):
            yield obj
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, dict):
                    yield item
        else:
            raise NodeTradesParseError(f"unexpected JSON shape on line {lineno}: {type(obj)!r}")


def maybe_decompress_lz4(blob: bytes) -> bytes:
    """Decompress an LZ4-framed blob; returns *blob* unchanged if not LZ4-magic.

    Raises :class:`NodeTradesParseError` if the payload looks LZ4-compressed
    but the optional ``lz4`` package is not installed.
    """
    lz4_magic = b"\x04\x22\x4d\x18"
    if not blob.startswith(lz4_magic):
        return blob
    if _lz4_frame is None:
        raise NodeTradesParseError(
            "Payload is LZ4-compressed but the optional 'lz4' package is not "
            "installed. Install it with `pip install lz4` to decode node_trades "
            "archives, or supply pre-decompressed JSONL to the parser."
        )
    return _lz4_frame.decompress(blob)


def parse_archive_object(
    payload: Union[bytes, str, List[Dict[str, Any]]],
) -> List[NodeTradeRecord]:
    """Parse one fetched archive object (bytes/str/list-of-dicts) into trades.

    Tolerant of:
      * raw LZ4-compressed bytes (auto-decompressed if ``lz4`` is available)
      * plain UTF-8 JSONL bytes/text
      * an already-parsed list of trade dicts (as used by fake fetchers/tests)
    """
    if isinstance(payload, list):
        raw_dicts: Iterable[Dict[str, Any]] = payload
    else:
        if isinstance(payload, (bytes, bytearray)):
            data = maybe_decompress_lz4(bytes(payload))
            text = data.decode("utf-8")
        else:
            text = payload
        raw_dicts = iter_jsonl_records(text)

    out: List[NodeTradeRecord] = []
    for rec in raw_dicts:
        try:
            out.append(parse_trade_record(rec))
        except NodeTradesParseError as exc:
            logger.warning("Skipping unparsable node_trades record: %s", exc)
    return out
