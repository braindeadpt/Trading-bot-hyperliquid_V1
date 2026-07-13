"""Parser for Hyperliquid official node-data archive records.

Reference (confirmed via official docs, July 2026):

* Historical market/trade archives are published to the requester-pays bucket
  ``s3://hyperliquid-archive`` with key layout
  ``market_data/[date]/[hour]/[datatype]/[coin].lz4``
  (https://hyperliquid.gitbook.io/hyperliquid-docs/historical-data).
  Node-level trade dumps additionally live under
  ``s3://hl-mainnet-node-data/node_trades`` (older/raw format, now stale —
  see below) and ``s3://hl-mainnet-node-data/node_fills`` /
  ``node_fills_by_block`` (API-aligned format, streamed via
  ``--write-fills --batch-by-block``).
* Both archive families are LZ4-compressed, newline-delimited JSON.
* Historical uploads to ``hyperliquid-archive`` happen roughly monthly with
  "no guarantee of timely updates"; requester pays S3 egress
  (``--request-payer requester``).

SCHEMA CHANGE (July 2026): a real read-only listing of
``s3://hl-mainnet-node-data/`` established that ``node_trades`` only covers
2025-03-22 through 2025-06-21 (stale, over a year out of date), while
``node_fills_by_block`` is the currently-maintained prefix (352 date folders
reaching 20260713). A real sample object
(``node_fills_by_block/hourly/20260710/23.lz4``) confirmed its schema is
**not** a flat one-line-per-trade NDJSON stream. Instead it is one line per
**block**:

    {
      "local_time": "2026-07-10T23:00:00.119184915",
      "block_time": "2026-07-10T22:59:59.921821211",
      "block_number": 1068001525,
      "events": [
        ["0xabc...", {"coin": "BTC", "px": "64361.0", "sz": "0.31",
                       "side": "B", "time": 1783724399921, "tid": 804001461971668,
                       "oid": 493281265158, "crossed": false, "hash": "0x...", ...}],
        ["0xdef...", {"coin": "BTC", "px": "64361.0", "sz": "0.31",
                       "side": "A", "time": 1783724399921, "tid": 804001461971668,
                       "oid": 493281294953, "crossed": true, "hash": "0x...", ...}]
      ]
    }

Most blocks have ``"events": []`` (no fills that block) — this is the
common case and must be skipped cleanly, not treated as an error. ``coin``
values include normal perps (``ETH``, ``AAVE``) and exotic spot/pre-launch
formats (``xyz:BIRD``, ``@156``, ``#2120``); callers filter to their target
symbol downstream (the aggregator already does this by ``coin``).

Each matched trade appears **twice** in a block's ``events`` — once per
counterparty, as an ``[address, fill]`` pair. Both legs share the same
``tid`` (trade id) and identical ``px``/``sz``/``time``, but different
``oid``/address, and typically (not guaranteed) one leg has
``"crossed": true`` (the taker) and the other ``"crossed": false`` (the
maker). To avoid double-counting volume, :func:`parse_node_fills_by_block_ndjson`
deduplicates by ``tid``, keeping the ``crossed: true`` leg when exactly one
of the two legs has it, otherwise keeping whichever leg was seen first
(px/sz are identical between legs of the same tid either way, so the choice
only affects which ``oid``/address/hash ends up attached — fields the
aggregator does not use).

The original flat-NDJSON parser (:func:`parse_trade_record` /
:func:`parse_archive_object` / :func:`iter_jsonl_records`) is kept
unmodified below — it remains valid for the legacy ``node_trades`` prefix's
shape (and the API-aligned ``node_fills`` shape, which is also flat) should
either ever be used again; it is simply no longer the default parser used by
the rebuild pipeline. This module treats trade records defensively: any
dict-like record carrying a recognizable coin/price/size/time field is
accepted, and unknown extra fields are ignored.

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


# ---------------------------------------------------------------------------
# node_fills_by_block — the current default source (block-wrapped NDJSON).
# ---------------------------------------------------------------------------


def iter_fills_by_block_records(text: str) -> Iterator[Dict[str, Any]]:
    """Yield one block dict per non-blank line of ``node_fills_by_block`` NDJSON text.

    Unlike :func:`iter_jsonl_records` (which flattens list-shaped lines into
    individual trade dicts), each line here is a single *block* dict carrying
    an ``events`` list — it is returned whole so the caller can walk
    ``events`` itself and apply ``tid``-based dedup.
    """
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise NodeTradesParseError(f"invalid JSON on line {lineno}: {exc}") from exc
        if not isinstance(obj, dict):
            raise NodeTradesParseError(f"unexpected JSON shape on line {lineno}: {type(obj)!r}")
        yield obj


def _select_fill_for_tid(existing: Dict[str, Any], candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Pick which of two same-``tid`` fill legs to keep.

    Prefer the ``crossed: true`` (taker) leg when exactly one of the two has
    it; otherwise keep whichever was seen first (``existing``). ``px``/``sz``
    are identical between legs of the same ``tid`` either way, so this only
    affects which ``oid``/address/hash metadata ends up attached.
    """
    if candidate.get("crossed") is True and existing.get("crossed") is not True:
        return candidate
    return existing


def parse_node_fills_by_block_ndjson(
    payload: Union[bytes, str, List[Dict[str, Any]]],
) -> List[NodeTradeRecord]:
    """Parse one fetched ``node_fills_by_block`` archive object into trades.

    Each NDJSON line is a block dict with an ``events`` list of
    ``[address, fill]`` pairs (often empty — most blocks have no fills).
    Each matched trade appears twice in ``events`` (once per counterparty)
    sharing the same ``tid``; this function deduplicates by ``tid`` (see
    :func:`_select_fill_for_tid`) before normalizing into
    :class:`NodeTradeRecord` via the existing :func:`parse_trade_record`.

    Tolerant of:
      * raw LZ4-compressed bytes (auto-decompressed if ``lz4`` is available)
      * plain UTF-8 NDJSON bytes/text (one block dict per line)
      * an already-parsed list of block dicts (as used by fake fetchers/tests)

    Malformed lines/blocks/events are logged and skipped rather than
    aborting the whole parse, mirroring :func:`parse_archive_object`'s
    tolerance style.
    """
    if isinstance(payload, list):
        blocks: Iterable[Dict[str, Any]] = payload
    else:
        if isinstance(payload, (bytes, bytearray)):
            data = maybe_decompress_lz4(bytes(payload))
            text = data.decode("utf-8")
        else:
            text = payload
        blocks = iter_fills_by_block_records(text)

    fills_by_tid: Dict[Any, Dict[str, Any]] = {}
    tid_order: List[Any] = []

    for block in blocks:
        if not isinstance(block, dict):
            logger.warning("Skipping non-dict node_fills_by_block record: %r", block)
            continue
        events = block.get("events")
        if not events:
            continue
        for event in events:
            if not (isinstance(event, (list, tuple)) and len(event) == 2):
                logger.warning("Skipping malformed events entry: %r", event)
                continue
            _address, fill = event
            if not isinstance(fill, dict):
                logger.warning("Skipping non-dict fill in events entry: %r", fill)
                continue
            tid = fill.get("tid")
            if tid is None:
                logger.warning("Skipping fill with no tid: %r", fill)
                continue

            if tid not in fills_by_tid:
                fills_by_tid[tid] = fill
                tid_order.append(tid)
            else:
                fills_by_tid[tid] = _select_fill_for_tid(fills_by_tid[tid], fill)

    out: List[NodeTradeRecord] = []
    for tid in tid_order:
        try:
            out.append(parse_trade_record(fills_by_tid[tid]))
        except NodeTradesParseError as exc:
            logger.warning("Skipping unparsable node_fills_by_block fill: %s", exc)
    return out
