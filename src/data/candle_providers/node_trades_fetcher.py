"""Object-key layout and pluggable fetchers for HL node_trades archives.

Layout facts confirmed from official Hyperliquid docs (July 2026):

* ``historical-data`` page (https://hyperliquid.gitbook.io/hyperliquid-docs/historical-data):
  historical market data is published to the **requester-pays** bucket
  ``s3://hyperliquid-archive`` with key layout
  ``market_data/[date]/[hour]/[datatype]/[coin].lz4`` (date=``YYYYMMDD``,
  hour=unpadded 0-23, e.g. ``market_data/20230916/9/l2Book/SOL.lz4``),
  fetched via ``aws s3 cp ... --request-payer requester``. Uploads happen
  "approximately once a month" with "no guarantee of timely updates."
* Node-level trade dumps (as opposed to market_data snapshots) live in a
  second bucket, ``s3://hl-mainnet-node-data``, under ``node_trades`` (older,
  raw format) or ``node_fills`` / ``node_fills_by_block`` (streamed via
  ``--write-fills --batch-by-block``, matches the public API fill shape).
  The docs do not pin an exact key template for this bucket's date/hour
  partitioning the way they do for ``hyperliquid-archive``.

Because the ``node_trades`` bucket's partitioning isn't authoritatively
documented, the key layout here is a **configurable template** (see
``DEFAULT_KEY_TEMPLATE``) rather than a hardcoded path, mirroring the
``market_data`` convention (date/hour/coin) as the best-known analogue. If
HL publishes a different layout, callers can pass their own template/bucket
without touching this module's logic.

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
DEFAULT_KEY_TEMPLATE = "node_trades/{date}/{hour}/{coin}.lz4"


@dataclass(frozen=True)
class ArchiveObjectKey:
    """One S3 object needed to cover part of a rebuild window."""

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
