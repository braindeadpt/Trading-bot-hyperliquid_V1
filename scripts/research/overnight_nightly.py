"""Nightly entrypoint for the overnight research runner.

The runner executes families; it does not decide *what* is runnable. This
wrapper reads the preregistered queue
(``data/research/overnight_experiments/QUEUE.md``), picks the first READY
family, enforces the K=4 window floor at runtime (a session with <4 valid
windows can never KEEP — p floors at 2^-K), runs it in ``--summary`` mode,
and writes a machine-readable status file for the dashboard watchdog.

Usage::

    python scripts/research/overnight_nightly.py                 # queue-driven run
    python scripts/research/overnight_nightly.py --dry-run       # what would run, no backtests
    python scripts/research/overnight_nightly.py --family vwap_thresholds   # force a family
    python scripts/research/overnight_nightly.py --heartbeat     # cron keepalive only

Scheduled task (see README "Research Program"): ``overnight_nightly.bat``
runs this at 02:00 local. Nothing-READY is a *normal* outcome and exits 0 —
the cron log and the dashboard status file carry the reason.

Read-only with respect to the queue: the wrapper never edits QUEUE.md. It
enforces only what the queue already promises (≥4 non-overlapping windows
for READY entries) so a softened queue entry cannot silently produce a
K=3 KEEP-impossible session.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))   # runnable from any cwd / scheduler
QUEUE_PATH = ROOT / "data" / "research" / "overnight_experiments" / "QUEUE.md"
STATUS_PATH = ROOT / "data" / "research" / "overnight_experiments" / "NIGHTLY_STATUS.json"
LOCK_PATH = ROOT / "data" / "research" / "overnight_experiments" / ".nightly.lock"
LOG_DIR = ROOT / "logs"

RUNNER = ROOT / "scripts" / "research" / "overnight_runner.py"
K_FLOOR = 4              # paired sign-flip noise gate cannot pass below this
SUBPROCESS_TIMEOUT_S = 20 * 60       # a hung sweep must not hold the lock forever
# 20 min, not 2h (2026-09-10): a full preregistered session (Q6 session A,
# 6 windows x 3 cells = 18 runs) measures at ~18s. Two hours was not margin,
# it was blindness -- every night the sweep blocked on the live DB's lock and
# was only discovered dead the next morning. At 20 min a block is reported
# while the window is still open, and a legitimate session never comes close.

_DATE = r"\d{4}-\d{2}-\d{2}"

# Queue vocabulary — status tokens parsed from the "## " section header.
READY_RE = re.compile(r"\(READY", re.IGNORECASE)


def _sections(text: str) -> List[Dict[str, Any]]:
    """Split QUEUE.md into per-experiment sections (title + body)."""
    out: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    for line in text.splitlines():
        if line.startswith("## "):
            if cur:
                out.append(cur)
            cur = {"title": line[3:].strip(), "body": []}
        elif cur is not None:
            cur["body"].append(line)
    if cur:
        out.append(cur)
    for s in out:
        s["body"] = "\n".join(s["body"])
    return out


def _field(body: str, name: str) -> str:
    # Queue entries bold the label with the colon inside ("**Harness:** ..."),
    # but "**Name**: ..." is accepted too — both are markdown-correct.
    m = re.search(rf"-\s*\*\*{name}(\*\*:|:\*\*)\s*(.+)", body)
    return m.group(2).strip() if m else ""


def _family_from_harness(harness: str) -> Optional[str]:
    m = re.search(r"--family\s+([a-z0-9_]+)", harness)
    return m.group(1) if m else None


def _window_spec(body: str) -> Optional[Dict[str, Any]]:
    """Parse the **Window set** bullet: start..end, split Nd."""
    m = re.search(
        rf"({_DATE})\.\.({_DATE}).*?split\s+(\d+)\s*d", body, re.DOTALL
    )
    if not m:
        return None
    return {"start": m.group(1), "end": m.group(2), "split_days": int(m.group(3))}


def pick_ready(queue_path: Path = QUEUE_PATH) -> Optional[Dict[str, Any]]:
    """First READY section with a parseable family + window set.

    A harness declaring ``--end dvol`` (or ``--start dvol``) is a
    coverage-gated entry: the span is resolved at run time from the
    persisted DVOL coverage, so no fixed window spec is required — the
    runner enforces the K=4 floor itself and BLOCKs early when the
    coverage is still short (Night 3 reopen path).
    """
    text = queue_path.read_text(encoding="utf-8")
    for sec in _sections(text):
        if not READY_RE.search(sec["title"]):
            continue
        harness = _field(sec["body"], "Harness")
        family = _family_from_harness(harness)
        if family and re.search(r"--(?:start|end|span)\s+dvol\b", harness):
            window = {"span": "dvol", "split_days": 30}
        else:
            window = _window_spec(sec["body"])
        if family and window:
            cells = re.search(r"--cells\s+([\d,]+)", harness)
            symbols = re.search(r"--symbols\s+([A-Za-z0-9,]+)", harness)
            return {
                "title": sec["title"],
                "family": family,
                "window": window,
                "symbols": symbols.group(1).upper() if symbols else "BTC,ETH",
                "cells": cells.group(1) if cells else None,
            }
    return None


def count_windows(window: Dict[str, Any]) -> int:
    """Non-overlapping windows the spec produces — the runtime K=4 guard.

    Uses the same splitter as the sweep itself (scripts.research.regime_router_a_b_test)
    so the guard can never disagree with the run.
    """
    from scripts.research.regime_router_a_b_test import split_windows  # noqa: E402
    return len(split_windows(window["start"], window["end"], window["split_days"]))
    return len(split_windows(window["start"], window["end"], window["split_days"]))


def _write_status(payload: Dict[str, Any]) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )


def _lock_acquired() -> bool:
    """Best-effort PID lockfile — a missed stale lock only risks overlap."""
    try:
        if LOCK_PATH.exists():
            pid = int(LOCK_PATH.read_text(encoding="utf-8").strip() or 0)
            if pid and pid != os.getpid():
                os.kill(pid, 0)   # raises if the pid is gone
                return False
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except OSError:
        return False


def _lock_release() -> None:
    try:
        if LOCK_PATH.exists() and LOCK_PATH.read_text(
            encoding="utf-8"
        ).strip() == str(os.getpid()):
            LOCK_PATH.unlink()
    except OSError:
        pass


def _recent_sessions(limit: int = 5) -> List[Dict[str, Any]]:
    """Digest of past sessions from the artifact dir (newest first)."""
    arts = sorted(STATUS_PATH.parent.glob("*_*.json"), key=lambda p: p.name,
                  reverse=True)
    out: List[Dict[str, Any]] = []
    for p in arts:
        if p.name.startswith("NIGHTLY_STATUS"):
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            out.append({
                "artifact": p.name,
                "family": d.get("family"),
                "verdicts": [r.get("verdict") for r in d.get("results", [])],
                "windows": len(d.get("windows") or []),
            })
        except Exception:  # noqa: BLE001 — a corrupt artifact must not kill the panel
            out.append({"artifact": p.name, "error": "unreadable"})
        if len(out) >= limit:
            break
    return out


def build_nightly_status(ran: Optional[Dict[str, Any]] = None,
                         reason: str = "",
                         queue_path: Path = QUEUE_PATH) -> Dict[str, Any]:
    """The dashboard watchdog payload — no queue edit, no runner call.

    ``queue_path`` must be the same queue the run decision came from, so the
    status file can never disagree with what the wrapper did.
    """
    ready = pick_ready(queue_path)
    payload: Dict[str, Any] = {
        "generated_ms": int(time.time() * 1000),
        "next_ready": None,
        "last_session": ran,
        "recent_sessions": _recent_sessions(),
        "ledger_path": "docs/OVERNIGHT_RESEARCH_LOG.md",
        # Relative display path when inside the repo; absolute otherwise
        # (tests point this at tmp_path).
        "status_path": (str(STATUS_PATH.relative_to(ROOT))
                        if STATUS_PATH.is_relative_to(ROOT)
                        else str(STATUS_PATH)),
    }
    if ready:
        if ready["window"].get("span") == "dvol":
            # Coverage-gated: K is resolved at run time from the persisted
            # DVOL span; the runner BLOCKs (exit 0 + verdict) while the
            # coverage cannot produce the K=4 floor yet.
            ready["windows"] = None
            ready["k_floor_ok"] = None
            ready["coverage_gated"] = True
        else:
            try:
                k = count_windows(ready["window"])
                ready["windows"] = k
                ready["k_floor_ok"] = k >= K_FLOOR
            except Exception as exc:  # noqa: BLE001
                ready["k_floor_ok"] = None
                ready["window_error"] = str(exc)
        payload["next_ready"] = ready
    if reason:
        payload["reason"] = reason
    return payload


def run_session(sel: Dict[str, Any], symbols: str) -> Dict[str, Any]:
    """Run one family via the runner's --summary surface; capture everything."""
    w = sel["window"]
    argv = [sys.executable, str(RUNNER),
            "--family", sel["family"],
            "--split-days", str(w.get("split_days", 30)),
            "--symbols", symbols, "--summary"]
    if w.get("span") == "dvol":
        # Coverage-gated: the runner derives the span from the persisted
        # DVOL coverage and BLOCKs while K < 4 (Night 3 reopen path).
        argv += ["--start", "dvol", "--end", "dvol"]
    else:
        argv += ["--start", w["start"], "--end", w["end"]]
    if sel.get("cells"):
        argv += ["--cells", sel["cells"]]

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    log_path = LOG_DIR / f"overnight_nightly_{ts}.log"

    started = time.time()
    try:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"  # sigma in cell tags killed the
        # 02:00 run when the child picked cp1252 — force it here too, not
        # only in the .bat, so any invocation path is safe
        proc = subprocess.run(
            argv, cwd=str(ROOT), capture_output=True, text=True, env=env,
            encoding="utf-8", errors="replace", timeout=SUBPROCESS_TIMEOUT_S,
        )
        out = proc.stdout or ""
        err = proc.stderr or ""
        rc = proc.returncode
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        err = f"TIMEOUT after {SUBPROCESS_TIMEOUT_S}s"
        rc = -1
        timed_out = True

    log_path.write_text(out + ("\n[stderr]\n" + err if err else ""),
                        encoding="utf-8")
    verdicts = re.findall(r"->\s+(KEEP|INCONCLUSIVE|DISCARD|BLOCKED)", out)
    return {
        "family": sel["family"],
        "started": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(started)),
        "duration_s": round(time.time() - started, 1),
        "returncode": rc,
        "timed_out": timed_out,
        "log": str(log_path.relative_to(ROOT)),
        "verdicts": verdicts,
        "summary_tail": "\n".join(out.strip().splitlines()[-8:]),
    }


def main() -> int:
    # Cell tags carry sigma ("z=3.5σ"). With stdout redirected to a file
    # Windows picks cp1252 and printing one raises UnicodeEncodeError, killing
    # the session (2026-09-10). Force UTF-8 when the stream supports it;
    # pytest's capture object does not, hence the guard.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--queue", default=str(QUEUE_PATH))
    ap.add_argument("--status", default=str(STATUS_PATH))
    ap.add_argument("--family", default=None,
                    help="force a family (bypasses queue selection; K=4 still enforced)")
    ap.add_argument("--symbols", default="BTC,ETH")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would run without running it")
    ap.add_argument("--heartbeat", action="store_true",
                    help="refresh the status file only (cron keepalive)")
    args = ap.parse_args()

    queue_path = Path(args.queue)
    if not queue_path.exists():
        print(f"queue not found: {queue_path}", file=sys.stderr)
        return 1

    if args.heartbeat:
        _write_status(build_nightly_status(reason="heartbeat",
                                           queue_path=queue_path))
        print("status refreshed (heartbeat)")
        return 0

    if args.family:
        # Forced family: reuse the queue's window spec when it declares one,
        # else the runner's own defaults via a synthetic selection.
        ready = pick_ready(queue_path)
        if ready and ready["family"] == args.family:
            sel = ready
        else:
            sel = {"title": f"forced {args.family}",
                   "family": args.family,
                   "window": {"start": "2026-05-18", "end": "2026-09-08",
                              "split_days": 30},
                   "symbols": None, "cells": None}
    else:
        sel = pick_ready(queue_path)
        if sel is None:
            payload = build_nightly_status(reason="nothing READY in queue",
                                           queue_path=queue_path)
            _write_status(payload)
            print("overnight: nothing READY in queue — status written, exiting 0")
            return 0

    if sel["window"].get("span") == "dvol":
        # Coverage-gated: the K=4 floor is enforced at run time by the
        # runner (the span resolves from the persisted DVOL coverage).
        pass
    else:
        k = count_windows(sel["window"])
        if k < K_FLOOR:
            payload = build_nightly_status(
                reason=f"{sel['family']}: only {k} windows < K={K_FLOOR} floor — refused",
                queue_path=queue_path)
            _write_status(payload)
            print(f"overnight: REFUSED {sel['family']} — {k} windows < K={K_FLOOR}")
            return 0

    symbols = args.symbols if not sel.get("symbols") else sel["symbols"]
    if args.dry_run:
        span_txt = (
            f"{sel['window']['start']}..{sel['window']['end']} "
            f"split={sel['window']['split_days']}d K={k}"
            if sel["window"].get("span") != "dvol"
            else "dvol coverage-gated (K resolved at run time)"
        )
        payload = build_nightly_status(
            reason=f"dry-run: {sel['family']} {span_txt} symbols={symbols}",
            queue_path=queue_path)
        _write_status(payload)
        print(f"overnight dry-run: {sel['family']} {span_txt} symbols={symbols}")
        return 0

    if not _lock_acquired():
        _write_status(build_nightly_status(reason="another nightly run holds the lock",
                                           queue_path=queue_path))
        print("overnight: another run holds the lock — skipping")
        return 0
    try:
        result = run_session(sel, symbols)
        reason = "session complete" if result["returncode"] == 0 else \
            f"runner exit {result['returncode']}"
        _write_status(build_nightly_status(ran=result, reason=reason,
                                           queue_path=queue_path))
        print(result["summary_tail"] or f"runner exit {result['returncode']}")
        # Weekly ledger digest — idempotent, at most one entry per ISO
        # week (state file inside the script). Best-effort: a digest
        # failure never fails the nightly session.
        try:
            dg = subprocess.run(
                [sys.executable,
                 str(ROOT / "scripts" / "research" / "weekly_ledger_summary.py"),
                 "--if-due"],
                cwd=str(ROOT), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=60,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            if dg.returncode == 0 and (dg.stdout or "").strip():
                print(f"weekly digest: {dg.stdout.strip()}")
        except Exception as exc:  # noqa: BLE001
            print(f"weekly digest skipped: {exc}")
        return 0 if result["returncode"] == 0 else 1
    finally:
        _lock_release()


if __name__ == "__main__":
    raise SystemExit(main())
