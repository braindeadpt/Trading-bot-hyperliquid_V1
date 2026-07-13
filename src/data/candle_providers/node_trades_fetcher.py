"""Object-key layout and pluggable fetchers for HL node_trades archives.

Layout facts confirmed from a real read-only ``list_objects_v2`` against
``s3://hl-mainnet-node-data/`` (via ``scripts/check_hl_s3_access.py``, July
2026):

* The bucket root actually has 6 top-level prefixes: ``explorer_blocks``,
  ``misc_events_by_block``, ``node_fills``, ``node_fills_by_block``,
  ``node_trades``, ``replica_cmds``.
* ``node_trades`` is **STALE** — its date range only covers 2025-03-22
  through 2025-06-21, over a year out of date. It is no longer a usable
  source for recent windows and is no longer the default here.
* ``node_fills_by_block`` is the **currently-maintained** prefix — a live
  listing showed 352 date folders reaching all the way to today
  (20260713). It uses the *same path shape* as the old ``node_trades``
  default: ``node_fills_by_block/hourly/{date}/{hour}.lz4``, with the hour
  segment unpadded (``10.lz4``, ``0.lz4``, ``1.lz4`` — never
  ``00.lz4``/``01.lz4``) and the date segment ``YYYYMMDD`` with no
  separators. Real observed keys look like
  ``node_fills_by_block/hourly/20260710/23.lz4``.
* There is **no per-coin key**. Each hourly object contains the fills for
  **every coin** traded in that hour/block, mixed together. Filtering to
  one symbol happens *after* download/parse, by each fill record's own
  ``coin`` field — not via the S3 path.
* Unlike the legacy ``node_trades`` shape (flat NDJSON, one line per
  trade), ``node_fills_by_block`` objects are NDJSON with **one line per
  block** — each line is a dict with a ``block_number``/``block_time`` and
  an ``events`` list of ``[address, fill]`` pairs (often empty). See
  ``node_trades_parser.parse_node_fills_by_block_ndjson`` for the parser
  that understands this shape, including the ``tid``-based dedup needed
  because each matched trade appears twice in ``events`` (once per
  counterparty).

This supersedes the earlier assumption (mirrored from the separate,
per-coin ``hyperliquid-archive`` / ``market_data`` bucket's
``market_data/[date]/[hour]/[datatype]/[coin].lz4`` layout) that these
node-data prefixes have a ``{coin}`` path segment — they do not.

The key layout here remains a **configurable template** (see
``DEFAULT_KEY_TEMPLATE``) rather than a hardcoded path, so callers can pass
their own template/bucket without touching this module's logic if HL ever
changes the layout again (including reverting to ``node_trades`` for a
future window, by passing ``key_template="node_trades/hourly/{date}/{hour}.lz4"``
explicitly). Because objects are no longer per-coin, callers that need
multiple symbols for the same date/hour should fetch the object **once**
and let each symbol's aggregation step filter the shared trade list by
``coin`` (see ``node_trades_rebuild.rebuild_from_support_package``, which
shares a parsed-trade cache keyed by object URI across symbols/windows so a
single downloaded file serves every symbol that needs it).

No network calls happen at import time or in ``FakeNodeTradesFetcher``. The
real S3 fetcher requires ``boto3`` (optional dependency) and AWS credentials
plus willingness to pay requester-pays transfer costs — it performs no work
until ``fetch_object`` is called explicitly.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_BUCKET = "hl-mainnet-node-data"
# node_trades/hourly/{date}/{hour}.lz4 is STALE (2025-03-22..2025-06-21 only).
# node_fills_by_block/hourly/{date}/{hour}.lz4 is the confirmed current
# source (live listing reaches 20260713) and shares the same path shape.
DEFAULT_KEY_TEMPLATE = "node_fills_by_block/hourly/{date}/{hour}.lz4"


@dataclass(frozen=True)
class ArchiveObjectKey:
    """One S3 object needed to cover part of a rebuild window.

    ``coin`` records which symbol's rebuild window requested this object —
    it is metadata about the *caller's intent*, not part of the S3 key
    itself. Real ``node_trades`` archive objects are multi-coin (one file
    per hour holds every coin's trades), so the same ``(bucket, key)`` will
    naturally be produced for multiple symbols that share a date/hour; the
    ``uri`` property (used for dedup) intentionally ignores ``coin``.
    """

    bucket: str
    key: str
    date: str
    hour: int
    coin: str

    @property
    def uri(self) -> str:
        return f"s3://{self.bucket}/{self.key}"


def _hour_buckets_for_window(start_ms: int, end_ms: int) -> List[tuple]:
    """Return sorted, deduped (date_str, hour) pairs spanned by [start_ms, end_ms]."""
    import datetime as dt

    if end_ms < start_ms:
        start_ms, end_ms = end_ms, start_ms
    start_dt = dt.datetime.fromtimestamp(start_ms / 1000, tz=dt.timezone.utc)
    end_dt = dt.datetime.fromtimestamp(end_ms / 1000, tz=dt.timezone.utc)

    cur = start_dt.replace(minute=0, second=0, microsecond=0)
    end_hour = end_dt.replace(minute=0, second=0, microsecond=0)
    out: List[tuple] = []
    seen = set()
    while cur <= end_hour:
        key = (cur.strftime("%Y%m%d"), cur.hour)
        if key not in seen:
            seen.add(key)
            out.append(key)
        cur += dt.timedelta(hours=1)
    return out


def archive_keys_for_window(
    symbol: str,
    start_ms: int,
    end_ms: int,
    *,
    bucket: str = DEFAULT_BUCKET,
    key_template: str = DEFAULT_KEY_TEMPLATE,
) -> List[ArchiveObjectKey]:
    """Enumerate the archive objects needed to cover [start_ms, end_ms] for symbol."""
    coin = symbol.upper()
    out: List[ArchiveObjectKey] = []
    for date_str, hour in _hour_buckets_for_window(start_ms, end_ms):
        key = key_template.format(date=date_str, hour=hour, coin=coin)
        out.append(ArchiveObjectKey(bucket=bucket, key=key, date=date_str, hour=hour, coin=coin))
    return out


class NodeTradesFetcher(ABC):
    """Fetch a single archive object's raw bytes/text given its key."""

    @abstractmethod
    def fetch_object(self, obj: ArchiveObjectKey) -> bytes:
        """Return raw (possibly LZ4-compressed) bytes for *obj*."""


@dataclass
class FakeNodeTradesFetcher(NodeTradesFetcher):
    """In-memory fetcher for tests — never touches the network.

    ``objects`` maps ``obj.uri`` -> payload. Payload may be raw bytes, a
    JSONL str, or a pre-decoded list of trade dicts (all accepted by
    :func:`node_trades_parser.parse_archive_object`).
    """

    objects: Dict[str, object] = field(default_factory=dict)
    fetch_log: List[str] = field(default_factory=list)

    def put(self, obj: ArchiveObjectKey, payload: object) -> None:
        self.objects[obj.uri] = payload

    def fetch_object(self, obj: ArchiveObjectKey):  # type: ignore[override]
        self.fetch_log.append(obj.uri)
        if obj.uri not in self.objects:
            raise KeyError(f"FakeNodeTradesFetcher has no object for {obj.uri}")
        return self.objects[obj.uri]


class S3NodeTradesFetcher(NodeTradesFetcher):
    """Real requester-pays S3 fetcher. Requires ``pip install boto3`` + AWS creds.

    Never imported eagerly by the rebuild orchestrator — only constructed
    when a caller explicitly asks for real downloads (``--execute`` in the
    CLI), so the module tree keeps importing cleanly without boto3 present.
    """

    def __init__(self, *, region_name: Optional[str] = None) -> None:
        try:
            import boto3  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "S3NodeTradesFetcher requires the optional 'boto3' dependency. "
                "Install it with `pip install boto3` and configure AWS "
                "credentials (e.g. via `aws configure` or environment "
                "variables) before using --execute. Real downloads also "
                "incur requester-pays S3 transfer costs billed to your AWS "
                "account."
            ) from exc
        self._client = boto3.client("s3", region_name=region_name)

    def fetch_object(self, obj: ArchiveObjectKey) -> bytes:
        logger.info("Fetching %s (requester-pays)", obj.uri)
        resp = self._client.get_object(
            Bucket=obj.bucket,
            Key=obj.key,
            RequestPayer="requester",
        )
        return resp["Body"].read()
