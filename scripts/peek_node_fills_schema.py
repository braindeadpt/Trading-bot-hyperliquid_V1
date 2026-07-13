"""One-shot, read-only diagnostic: peek at the real schema inside a
``node_fills_by_block`` archive object.

CONTEXT: The candle-rebuild pipeline was built assuming Hyperliquid's
official S3 archive lived at ``node_trades/hourly/{date}/{hour}.lz4``
(all-coins-per-hour trade tape). That prefix turned out to be stale (only
covers 2025-03-22 to 2025-06-21). The bucket root has 6 top-level prefixes
(``explorer_blocks``, ``misc_events_by_block``, ``node_fills``,
``node_fills_by_block``, ``node_trades``, ``replica_cmds``), and
``node_fills_by_block/hourly/`` is confirmed to reach up to the present day.

This script downloads exactly ONE ``.lz4`` object from that prefix,
decompresses it, and pretty-prints the first few decoded JSON lines so a
human can see the real field names/nesting/types before
``node_trades_parser.py`` / ``node_trades_fetcher.py`` /
``node_trades_rebuild.py`` / ``node_trades_aggregator.py`` are updated to
understand the ``node_fills_by_block`` record shape.

This is a REAL, requester-pays download (single object, a few MB to a few
tens of MB, negligible cost — must be explicitly run by a human who has
already accepted that cost). It performs no other side effects: nothing is
written to disk, nothing is wired into the rebuild pipeline, and it is not
imported by any other module.

Usage:
  python scripts/peek_node_fills_schema.py
  python scripts/peek_node_fills_schema.py --bucket hl-mainnet-node-data \
      --key node_fills_by_block/hourly/20260710/12.lz4 --max-lines 10

Cost caveat: every run downloads the full object from a requester-pays
bucket (billed to whatever AWS credentials are configured). The default key
(``node_fills_by_block/hourly/20260710/23.lz4``, ~15.96 MB) was chosen as
the smallest of the target window's hours specifically to minimize this
one-off inspection cost. Do not loop this script over many keys.
"""

from __future__ import annotations

import argparse
import json
import sys

# Reuse the exact same LZ4-framed decompression helper already used by the
# rebuild pipeline (src/data/candle_providers/node_trades_parser.py) instead
# of reimplementing LZ4 handling here.
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from src.data.candle_providers.node_trades_parser import (  # noqa: E402
    NodeTradesParseError,
    maybe_decompress_lz4,
)

DEFAULT_BUCKET = "hl-mainnet-node-data"
DEFAULT_KEY = "node_fills_by_block/hourly/20260710/23.lz4"


def _fetch_object(bucket: str, key: str) -> bytes:
    try:
        import boto3  # type: ignore
        from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
    except ImportError:
        print("ERRO: boto3 nao esta instalado. Corre:  pip install boto3")
        raise SystemExit(1)

    try:
        client = boto3.client("s3")
        print(f"Fetching s3://{bucket}/{key} (requester-pays, real download)...")
        resp = client.get_object(Bucket=bucket, Key=key, RequestPayer="requester")
        return resp["Body"].read()
    except NoCredentialsError:
        print(
            "ERRO: credenciais AWS nao encontradas.\n"
            "Configura %USERPROFILE%\\.aws\\credentials ou as env vars "
            "AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY."
        )
        raise SystemExit(1)
    except (ClientError, BotoCoreError) as exc:
        print(f"ERRO no acesso ao S3: {exc}")
        raise SystemExit(1)


def _decompress(blob: bytes) -> bytes:
    """Try the shared LZ4-frame helper first; fall back to raw bytes.

    ``maybe_decompress_lz4`` already handles the common case (LZ4-framed
    blob -> decompressed bytes, or pass-through if not LZ4-magic). If that
    raises because the optional ``lz4`` package is missing, surface a clear
    error rather than crash with a traceback.
    """
    try:
        return maybe_decompress_lz4(blob)
    except NodeTradesParseError as exc:
        print(f"ERRO ao descomprimir: {exc}")
        raise SystemExit(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--key", default=DEFAULT_KEY)
    parser.add_argument("--max-lines", type=int, default=5)
    args = parser.parse_args()

    raw = _fetch_object(args.bucket, args.key)
    print(f"Downloaded {len(raw):,} compressed bytes.")

    data = _decompress(raw)
    print(f"Decompressed size: {len(data):,} bytes.")

    text = data.decode("utf-8", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    print(f"Total non-blank lines: {len(lines):,}")

    n = max(0, args.max_lines)
    print(f"\n--- First {min(n, len(lines))} line(s), pretty-printed ---\n")
    for i, line in enumerate(lines[:n], start=1):
        print(f"[line {i}]")
        try:
            obj = json.loads(line)
            print(json.dumps(obj, indent=2))
        except json.JSONDecodeError as exc:
            truncated = line[:500]
            print(f"WARNING: not valid JSON ({exc}); raw (truncated to 500 chars):")
            print(truncated)
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
