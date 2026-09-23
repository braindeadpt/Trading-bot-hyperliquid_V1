#!/usr/bin/env python3
"""Pull verified backup artifacts from the VPS into the local D:\\ backup root.

Runs on the OPERATOR PC (Windows + OpenSSH client, over Tailscale or plain
SSH). The VPS only ever holds a *staging* copy: ``hyperliquid-backup.timer``
produces verified runs under ``/srv/hyperliquid/backups/runs/<id>/`` plus the
incremental L2 mirror under ``/srv/hyperliquid/backups/l2_books/`` — the
same layout the local backup writes, so pulled artifacts land identically
and the existing retention rules apply unchanged.

Per remote run:
  1. ``ssh ls`` discovers run dirs (manifest.json present = finished).
  2. Pull manifest first; pull the database files it records; verify each
     pulled file's sha256 against the manifest record. Only then is the
     local run marked ``.pulled_ok``.
  3. Pull every manifest-listed L2 file (``l2_files[].path`` is relative to
     the shared ``l2_books/`` mirror) into the local mirror, sha256-checked.
  4. Remote prune: verified runs beyond ``--keep-remote N`` are deleted on
     the VPS — staging is never the permanent copy.

Auth: key-based only (``ssh -o BatchMode=yes``). Configure the host in
``~/.ssh/config`` (e.g. Host hyperliquid-vps) or pass --host user@ip.

Usage:
    python scripts/ops/pull_vps_backup.py --host hyperliquid-vps
    python scripts/ops/pull_vps_backup.py --host hyperliquid-vps --keep-remote 1 --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List

DEFAULT_LOCAL_ROOT = Path("D:/hyperliquid_backup")


def _sha256(path: Path, _chunk: int = 1 << 22) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(_chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _ssh(host: str, remote_cmd: str) -> str:
    """Run a command on the VPS; return stdout. BatchMode = keys only."""
    out = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host, remote_cmd],
        capture_output=True, text=True, timeout=120,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ssh {host} '{remote_cmd}' failed: {out.stderr.strip()}")
    return out.stdout


def _scp_pull(host: str, remote: str, local: Path) -> None:
    local.parent.mkdir(parents=True, exist_ok=True)
    out = subprocess.run(
        ["scp", "-o", "BatchMode=yes", "-q", f"{host}:{remote}", str(local)],
        capture_output=True, text=True, timeout=7200,
    )
    if out.returncode != 0:
        raise RuntimeError(f"scp {remote} failed: {out.stderr.strip()}")


def list_remote_runs(host: str, remote_root: str) -> List[str]:
    """Remote run dirs that contain a manifest.json (= completed backup)."""
    out = _ssh(host, f"ls -1 {remote_root}/runs 2>/dev/null || true")
    runs = []
    for name in out.split():
        name = name.strip()
        if not name:
            continue
        chk = _ssh(host, f"test -f {remote_root}/runs/{name}/manifest.json && echo yes || true")
        if chk.strip() == "yes":
            runs.append(name)
    return sorted(runs)


def pull_run(
    host: str,
    remote_root: str,
    run: str,
    local_root: Path,
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Pull one run dir (manifest + databases) and its manifest-listed L2 files."""
    remote_dir = f"{remote_root}/runs/{run}"
    local_dir = local_root / "runs" / run
    result: Dict[str, Any] = {
        "run": run, "files": 0, "verified": 0,
        "l2_verified": 0, "mismatched": [], "ok": False,
    }

    if dry_run:
        return {**result, "ok": True}

    # 1) manifest first — it is the verification contract for everything else
    _scp_pull(host, f"{remote_dir}/manifest.json", local_dir / "manifest.json")
    manifest = json.loads((local_dir / "manifest.json").read_text(encoding="utf-8"))

    # 2) databases recorded in the manifest (name = run-dir filename)
    for rec in manifest.get("databases", []):
        name = rec.get("name")
        want = rec.get("sha256")
        if not name:
            continue
        _scp_pull(host, f"{remote_dir}/{name}", local_dir / name)
        result["files"] += 1
        if want:
            got = _sha256(local_dir / name)
            if got != want:
                result["mismatched"].append(f"sha256:{name}")
            else:
                result["verified"] += 1

    # 3) L2 files: manifest paths are relative to the shared l2_books mirror
    for rec in manifest.get("l2_files", []):
        rel = rec.get("path")
        want = rec.get("sha256")
        if not rel:
            continue
        rel_posix = PurePosixPath(rel)
        if rel_posix.is_absolute() or ".." in rel_posix.parts:
            result["mismatched"].append(f"unsafe_path:{rel}")
            continue
        local_l2 = local_root / "l2_books" / Path(*rel_posix.parts)
        if local_l2.exists() and want and _sha256(local_l2) == want:
            result["l2_verified"] += 1
            continue  # already pulled earlier — mirror is incremental
        _scp_pull(host, f"{remote_root}/l2_books/{rel_posix.as_posix()}", local_l2)
        result["files"] += 1
        if want and _sha256(local_l2) != want:
            result["mismatched"].append(f"sha256:l2/{rel}")
        elif want:
            result["l2_verified"] += 1

    result["ok"] = not result["mismatched"]
    result["manifest_ok"] = bool(manifest.get("ok"))
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", required=True,
                    help="ssh host (alias in ~/.ssh/config or user@ip)")
    ap.add_argument("--local-root", type=Path, default=DEFAULT_LOCAL_ROOT,
                    help=f"local backup root (default: {DEFAULT_LOCAL_ROOT})")
    ap.add_argument("--remote-root", default="/srv/hyperliquid/backups",
                    help="remote staging root (default: /srv/hyperliquid/backups)")
    ap.add_argument("--keep-remote", type=int, default=1,
                    help="newest N verified runs kept on the VPS "
                         "(default: 1; staging is never the permanent copy)")
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be pulled, transfer nothing")
    args = ap.parse_args()

    remote_root = args.remote_root.rstrip("/")

    try:
        runs = list_remote_runs(args.host, remote_root)
    except (RuntimeError, FileNotFoundError) as exc:
        print(f"Cannot reach {args.host}: {exc}")
        return 2
    if not runs:
        print(f"No completed runs on {args.host}:{remote_root}/runs")
        return 0

    pulled: List[Dict[str, Any]] = []
    for run in runs:
        local_dir = args.local_root / "runs" / run
        if (local_dir / ".pulled_ok").exists():
            continue  # pulled+verified in an earlier pull
        print(f"pulling {run} ...", flush=True)
        try:
            res = pull_run(args.host, remote_root, run, args.local_root,
                           dry_run=args.dry_run)
        except (RuntimeError, FileNotFoundError, json.JSONDecodeError) as exc:
            print(f"  FAILED: {exc}")
            pulled.append({"run": run, "ok": False, "error": str(exc)})
            continue
        pulled.append(res)
        if args.dry_run:
            print("  dry-run: would pull manifest + databases + l2 records")
            continue
        if not res["ok"]:
            print(f"  FAILED verification: {res['mismatched']}")
            continue
        # local proof this run passed sha256 verification here
        (local_dir / ".pulled_ok").write_text(
            json.dumps({
                "verified_dbs": res["verified"],
                "verified_l2": res["l2_verified"],
                "manifest_ok": res["manifest_ok"],
            }) + "\n",
            encoding="utf-8",
        )
        print(f"  ok — {res['verified']} DBs + {res['l2_verified']} L2 files "
              f"sha256-verified (manifest ok={res['manifest_ok']})")

    # remote prune: delete verified runs beyond the newest --keep-remote
    if not args.dry_run:
        verified = sorted(
            r for r in runs
            if (args.local_root / "runs" / r / ".pulled_ok").exists()
        )
        keep = set(verified[-args.keep_remote:]) if args.keep_remote > 0 else set()
        for run in verified:
            if run in keep:
                continue
            _ssh(args.host, f"rm -rf -- '{remote_root}/runs/{run}'")
            print(f"remote pruned: {run}")

    fails = [r for r in pulled if not r.get("ok")]
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
