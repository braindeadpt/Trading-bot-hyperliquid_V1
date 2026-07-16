"""One-shot helper: download recent node_fills_by_block hourly object(s)
for the liquidation-map CLI. Real, requester-pays S3 download(s).

Usage:
  python scripts/download_recent_fills.py
  python scripts/download_recent_fills.py --date 20260715 --hour 14
  python scripts/download_recent_fills.py --hours 8          (last 8 hours, spanning midnight if needed)
  python scripts/download_recent_fills.py --date 20260715 --start-hour 0 --end-hour 23   (whole day)
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _download_one(client, bucket: str, date_str: str, hour: int, out_path: Path) -> tuple[bool, str]:
    from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

    key = f"node_fills_by_block/hourly/{date_str}/{hour}.lz4"
    print(f"A descarregar s3://{bucket}/{key} ...")
    try:
        client.download_file(bucket, key, str(out_path), ExtraArgs={"RequestPayer": "requester"})
    except NoCredentialsError:
        print(
            "ERRO: credenciais AWS nao encontradas.\n"
            "Configura %USERPROFILE%\\.aws\\credentials ou as env vars "
            "AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY."
        )
        return False, str(out_path)
    except (ClientError, BotoCoreError) as exc:
        print(f"  SKIP {date_str}/{hour}: {exc}")
        return False, str(out_path)

    size_mb = out_path.stat().st_size / 1_048_576
    print(f"  OK — {out_path} ({size_mb:.2f} MB)")
    return True, str(out_path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", default="hl-mainnet-node-data")
    parser.add_argument("--date", default=None, help="YYYYMMDD (default: yesterday, UTC)")
    parser.add_argument("--hour", type=int, default=None, help="single hour 0-23")
    parser.add_argument("--start-hour", type=int, default=None, help="range start hour 0-23")
    parser.add_argument("--end-hour", type=int, default=None, help="range end hour 0-23 (inclusive)")
    parser.add_argument("--hours", type=int, default=None, help="download the last N hours ending now (UTC), may span 2 dates")
    parser.add_argument("--out-dir", default="data/research/fills")
    args = parser.parse_args()

    try:
        import boto3
    except ImportError:
        print("ERRO: boto3 nao esta instalado. Corre:  pip install boto3")
        return 1

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    client = boto3.client("s3")

    jobs: list[tuple[str, int]] = []

    if args.hours is not None:
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        for i in range(args.hours, 0, -1):
            dt = now - timedelta(hours=i)
            jobs.append((dt.strftime("%Y%m%d"), dt.hour))
    elif args.start_hour is not None and args.end_hour is not None:
        date_str = args.date or (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y%m%d")
        for h in range(args.start_hour, args.end_hour + 1):
            jobs.append((date_str, h))
    else:
        date_str = args.date or (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y%m%d")
        hour = args.hour if args.hour is not None else 14
        jobs.append((date_str, hour))

    ok_paths: list[str] = []
    for date_str, hour in jobs:
        out_path = out_dir / f"{date_str}_{hour:02d}.lz4"
        if out_path.exists():
            print(f"  ja existe: {out_path} (skip)")
            ok_paths.append(str(out_path))
            continue
        ok, path = _download_one(client, args.bucket, date_str, hour, out_path)
        if ok:
            ok_paths.append(path)

    print(f"\n{len(ok_paths)}/{len(jobs)} ficheiro(s) disponivel(is):")
    for p in ok_paths:
        print(f"  {p}")
    if ok_paths:
        print("\nPassa todos ao build_liquidation_map.py de uma vez:")
        print(f"  python scripts/build_liquidation_map.py --from-fills {' '.join(ok_paths)} --execute")
    return 0 if ok_paths else 1


if __name__ == "__main__":
    raise SystemExit(main())
