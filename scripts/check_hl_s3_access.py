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
  python scripts/check_hl_s3_access.py --list-dates
  python scripts/check_hl_s3_access.py --list-dates --around 20260710
  python scripts/check_hl_s3_access.py --list-prefixes
  python scripts/check_hl_s3_access.py --list-prefixes node_fills/
"""

from __future__ import annotations

import argparse
import sys


def _list_common_prefixes(s3, bucket: str, prefix: str):
    """Paginate list_objects_v2 with Delimiter='/' to enumerate "subfolders".

    Returns the sorted list of subfolder names (e.g. "20250322" or
    "node_trades") found as CommonPrefixes directly under `prefix`. Loops on
    IsTruncated / ContinuationToken so folders with 1000+ entries are not
    silently truncated. Works for any prefix, including the bucket root
    (prefix="").
    """
    names = []
    continuation_token = None
    while True:
        kwargs = {
            "Bucket": bucket,
            "Prefix": prefix,
            "Delimiter": "/",
            "RequestPayer": "requester",
        }
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token
        resp = s3.list_objects_v2(**kwargs)
        for cp in resp.get("CommonPrefixes", []):
            cp_prefix = cp.get("Prefix", "")
            name = cp_prefix[len(prefix):].strip("/")
            if name:
                names.append(name)
        if resp.get("IsTruncated"):
            continuation_token = resp.get("NextContinuationToken")
        else:
            break
    names.sort()
    return names


def _list_dates(s3, bucket: str, base_prefix: str = "node_trades/hourly/"):
    """Enumerate date folders under base_prefix (thin wrapper for --list-dates)."""
    return _list_common_prefixes(s3, bucket, base_prefix)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", default="hl-mainnet-node-data")
    parser.add_argument("--prefix", default="node_trades/")
    parser.add_argument("--max-keys", type=int, default=20)
    parser.add_argument(
        "--list-dates",
        action="store_true",
        help=(
            "Em vez de listar objetos de exemplo, enumera todas as pastas de "
            "data sob node_trades/hourly/ (via Delimiter='/') e mostra a "
            "cobertura real (mais antiga/mais recente) do arquivo."
        ),
    )
    parser.add_argument(
        "--around",
        default=None,
        metavar="YYYYMMDD",
        help=(
            "Usado com --list-dates: mostra a data disponivel mais proxima "
            "antes e depois desta data alvo."
        ),
    )
    parser.add_argument(
        "--list-prefixes",
        nargs="?",
        default=None,
        const="",
        metavar="BASE_PREFIX",
        help=(
            "Lista todas as 'subpastas' (CommonPrefixes) diretamente sob "
            "BASE_PREFIX (via Delimiter='/'), mostrando a contagem e a lista "
            "completa ordenada. Usa --list-prefixes sem argumento para listar "
            "a raiz do bucket (ex.: node_trades, node_fills, "
            "node_fills_by_block) — nao uses --list-prefixes \"\", pois o "
            "PowerShell no Windows pode descartar strings vazias como "
            "argumento. Independente de --list-dates/--around."
        ),
    )
    args = parser.parse_args()

    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
    except ImportError:
        print("ERRO: boto3 nao esta instalado. Corre:  pip install boto3")
        return 1

    if args.list_prefixes is not None:
        try:
            s3 = boto3.client("s3")
            names = _list_common_prefixes(s3, args.bucket, args.list_prefixes)
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

        shown_prefix = args.list_prefixes if args.list_prefixes else "(raiz do bucket)"
        if not names:
            print(
                f"Acesso OK, mas 0 subpastas sob s3://{args.bucket}/{args.list_prefixes}\n"
                "O prefixo pode estar errado ou nao ter subpastas (Delimiter='/')."
            )
            return 0

        print(f"Acesso OK — {len(names)} subpasta(s) sob s3://{args.bucket}/{shown_prefix}:")
        for name in names:
            print(f"  {name}")

        return 0

    if args.list_dates:
        try:
            s3 = boto3.client("s3")
            dates = _list_dates(s3, args.bucket)
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

        if not dates:
            print(
                f"Acesso OK, mas 0 pastas de data sob s3://{args.bucket}/node_trades/hourly/\n"
                "O prefixo pode ser diferente do esperado (node_trades/hourly/{date}/)."
            )
            return 0

        print(f"Acesso OK — {len(dates)} pasta(s) de data sob s3://{args.bucket}/node_trades/hourly/")
        print(f"  mais antiga: {dates[0]}")
        print(f"  mais recente: {dates[-1]}")

        if args.around:
            target = args.around
            before = [d for d in dates if d <= target]
            after = [d for d in dates if d >= target]
            closest_before = before[-1] if before else None
            closest_after = after[0] if after else None
            print(f"\nMais proximas de --around {target}:")
            print(f"  antes/igual: {closest_before if closest_before else 'nenhuma'}")
            print(f"  depois/igual: {closest_after if closest_after else 'nenhuma'}")

        return 0

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
