"""Verify AWS access to Hyperliquid's requester-pays S3 archives.

Read-only sanity check to run AFTER configuring AWS credentials and BEFORE
scripts/hl_node_trades_rebuild.py --execute:

  1. Confirms boto3 is installed and credentials resolve.
  2. Lists a small sample of keys under the node-trades prefix of
     s3://hl-mainnet-node-data (requester-pays) so you can confirm the real
     partitioning layout and adjust the rebuild key template if needed.

Listing objects is a requester-pays operation with negligible cost
(fractions of a cent). Nothing is downloaded.

Usage:
  python scripts/check_hl_s3_access.py
  python scripts/check_hl_s3_access.py --bucket hl-mainnet-node-data --prefix node_trades/ --max-keys 20
"""

from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", default="hl-mainnet-node-data")
    parser.add_argument("--prefix", default="node_trades/")
    parser.add_argument("--max-keys", type=int, default=20)
    args = parser.parse_args()

    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
    except ImportError:
        print("ERRO: boto3 nao esta instalado. Corre:  pip install boto3")
        return 1

    try:
        s3 = boto3.client("s3")
        resp = s3.list_objects_v2(
            Bucket=args.bucket,
            Prefix=args.prefix,
            MaxKeys=args.max_keys,
            RequestPayer="requester",
        )
    except NoCredentialsError:
        print(
            "ERRO: credenciais AWS nao encontradas.\n"
            "Configura %USERPROFILE%\\.aws\\credentials ou as env vars "
            "AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY."
        )
        return 1
    except (ClientError, BotoCoreError) as exc:
        print(f"ERRO no acesso ao S3: {exc}")
        print(
            "Verifica: (1) as chaves estao corretas, (2) a policy IAM permite "
            "s3:ListBucket e s3:GetObject neste bucket, (3) tens rede."
        )
        return 1

    contents = resp.get("Contents", [])
    if not contents:
        print(
            f"Acesso OK, mas 0 objetos sob s3://{args.bucket}/{args.prefix}\n"
            "O prefixo pode ser diferente — tenta --prefix '' para listar a raiz "
            "do bucket e descobrir o layout real."
        )
        return 0

    print(f"Acesso OK — {len(contents)} objeto(s) de exemplo sob s3://{args.bucket}/{args.prefix}:")
    for obj in contents:
        size_mb = obj["Size"] / 1_048_576
        print(f"  {obj['Key']}  ({size_mb:.2f} MB)")
    print(
        "\nCompara estes caminhos com o template do rebuild "
        "(node_trades/{date}/{hour}/{coin}.lz4). Se o layout real for diferente, "
        "passa o template correto ao scripts/hl_node_trades_rebuild.py."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
