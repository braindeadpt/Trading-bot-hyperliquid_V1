"""Reset paper trading session: clear circuit breaker state and reconcile portfolio peaks.

Two modes:

- Default (dry-run): prints current equity / peak / drawdown / breaker state.
- ``--apply``: persists the reset in ``data/live/bot.db`` —
    * latest ``portfolio_snapshots`` row gets ``peak_capital = capital``
      (and ``_meta.daily_peak_capital = capital``), so drawdown reads 0%;
    * ``runtime_state.engine_runtime_v1.risk`` gets the circuit-breaker and
      daily-drawdown-circuit fields cleared.

Run while the bot is STOPPED (stop.bat), then restart with quickstart.bat or
``main.py --mode paper``. The script refuses to write while a live instance
holds the instance lock or has written a portfolio snapshot in the last
2 minutes (the running bot would overwrite the reset anyway).

Scope note: this resets the *governance* baseline only. Trade history in
``trades``/``signals``/``decision_audit`` is untouched, so Fase-10 / OOS gate
scripts still see the full window. For a fully fresh paper run, delete
``data/live/bot.db`` instead.

Usage:
    python scripts/reset_paper_session.py            # dry-run, show state
    python scripts/reset_paper_session.py --apply    # persist reset
    python scripts/reset_paper_session.py --apply --force   # skip running check
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.utils.config import load_config  # noqa: E402
from src.utils.helpers import utc_timestamp_ms  # noqa: E402

_RUNTIME_STATE_KEY = "engine_runtime_v1"
_FRESH_SNAPSHOT_MS = 120_000  # 2 min — a snapshot newer than this means a live writer


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    import os

    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _bot_running() -> bool:
    """True if the instance lock PID is alive or a snapshot was just written."""
    for lock_path in (ROOT / "data" / "bot.lock", ROOT / "data" / "live" / "bot.lock"):
        try:
            if lock_path.exists():
                pid = int(lock_path.read_text(encoding="utf-8").strip())
                if _pid_alive(pid):
                    return True
        except (OSError, ValueError):
            continue
    return False


def _load_state(db_path: Path) -> dict:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        snap = con.execute(
            "SELECT rowid AS rid, timestamp, capital, peak_capital, daily_pnl, positions_json "
            "FROM portfolio_snapshots ORDER BY rid DESC LIMIT 1"
        ).fetchone()
        rt = con.execute(
            "SELECT value_json, updated_ms FROM runtime_state WHERE state_key = ?",
            (_RUNTIME_STATE_KEY,),
        ).fetchone()
        open_trades = con.execute(
            "SELECT COUNT(*) FROM trades WHERE status = 'open'"
        ).fetchone()[0]
    finally:
        con.close()

    out: dict = {"snapshot": None, "runtime": None, "open_trades": open_trades}
    if snap:
        meta = {}
        try:
            positions = json.loads(snap["positions_json"] or "{}")
            if isinstance(positions, dict):
                meta = positions.get("_meta") or {}
        except (TypeError, ValueError):
            pass
        out["snapshot"] = {
            "rowid": snap["rid"],
            "timestamp": snap["timestamp"],
            "capital": snap["capital"],
            "peak_capital": snap["peak_capital"],
            "daily_pnl": snap["daily_pnl"],
            "meta": meta,
            "positions_json": snap["positions_json"],
        }
    if rt:
        try:
            out["runtime"] = json.loads(rt["value_json"])
            out["runtime_updated_ms"] = rt["updated_ms"]
        except (TypeError, ValueError):
            out["runtime"] = None
    return out


def _print_state(state: dict) -> None:
    snap = state["snapshot"]
    if not snap:
        print("No portfolio snapshot found — nothing to reset.")
    else:
        capital = float(snap["capital"] or 0.0)
        peak = float(snap["peak_capital"] or capital)
        dd = (peak - capital) / peak * 100.0 if peak > 0 else 0.0
        meta = snap["meta"] or {}
        daily_peak = float(meta.get("daily_peak_capital") or capital)
        daily_dd = (daily_peak - capital) / daily_peak * 100.0 if daily_peak > 0 else 0.0
        print(f"  equity:              ${capital:,.2f}")
        print(f"  peak_capital:        ${peak:,.2f}")
        print(f"  drawdown:            {dd:.2f}%")
        print(f"  daily_peak_capital:  ${daily_peak:,.2f} (daily DD {daily_dd:.2f}%)")
        print(f"  daily_pnl:           ${float(snap['daily_pnl'] or 0):,.2f}")
    risk = (state["runtime"] or {}).get("risk") or {}
    print(f"  open trades:         {state['open_trades']}")
    print(f"  circuit_breaker:     {risk.get('circuit_breaker_tripped', False)} "
          f"({risk.get('circuit_breaker_reason') or '—'})")
    print(f"  daily_dd_circuit:    {risk.get('daily_drawdown_circuit_tripped', False)}")


def _apply_reset(db_path: Path, state: dict) -> None:
    con = sqlite3.connect(str(db_path))
    try:
        snap = state["snapshot"]
        if snap:
            capital = float(snap["capital"] or 0.0)
            positions = json.loads(snap["positions_json"] or "{}")
            if not isinstance(positions, dict):
                positions = {}
            meta = positions.get("_meta")
            if isinstance(meta, dict):
                meta["daily_peak_capital"] = capital
                positions["_meta"] = meta
            con.execute(
                "UPDATE portfolio_snapshots SET peak_capital = ?, positions_json = ? "
                "WHERE rowid = ?",
                (capital, json.dumps(positions), snap["rowid"]),
            )

        rt = state["runtime"]
        if isinstance(rt, dict):
            risk = rt.get("risk")
            if isinstance(risk, dict):
                risk["circuit_breaker_tripped"] = False
                risk["circuit_breaker_reason"] = ""
                risk["circuit_breaker_date"] = ""
                risk["daily_drawdown_circuit_tripped"] = False
                risk["daily_drawdown_circuit_date"] = ""
                con.execute(
                    "INSERT INTO runtime_state (state_key, value_json, updated_ms) "
                    "VALUES (?, ?, ?) ON CONFLICT(state_key) DO UPDATE SET "
                    "value_json = excluded.value_json, updated_ms = excluded.updated_ms",
                    (_RUNTIME_STATE_KEY, json.dumps(rt, default=str), utc_timestamp_ms()),
                )
        con.commit()
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Persist the reset to bot.db")
    parser.add_argument("--force", action="store_true", help="Skip the running-bot check")
    args = parser.parse_args()

    cfg = load_config(str(ROOT / "config" / "settings.yaml"))
    db_path = ROOT / str(cfg.get("database.path", "data/live/bot.db"))
    if not db_path.exists():
        print(f"[FATAL] DB not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    state = _load_state(db_path)

    print("=== Paper session state ===")
    _print_state(state)

    if not args.apply:
        print("\nDry-run. Re-run with --apply while the bot is STOPPED to persist.")
        return

    snap = state["snapshot"]
    if not args.force:
        fresh = bool(
            snap and (utc_timestamp_ms() - int(snap["timestamp"])) < _FRESH_SNAPSHOT_MS
        )
        if _bot_running() or fresh:
            print(
                "\n[REFUSED] Bot appears to be RUNNING (lock PID alive or snapshot < 2min old).\n"
                "          A live process would overwrite this reset. Run stop.bat first.\n"
                "          Use --force only if you are sure no instance is writing.",
                file=sys.stderr,
            )
            sys.exit(1)

    if state["open_trades"]:
        print(
            f"\n[WARN] {state['open_trades']} open trade(s) exist — the reset does not "
            "close them; they keep their own stops/targets."
        )

    _apply_reset(db_path, state)
    print("\nReset applied to bot.db:")
    _print_state(_load_state(db_path))
    print("\nRestart the bot (quickstart.bat / main.py --mode paper). "
          "Drawdown now reads 0% — the breaker re-arms against the new peak.")


if __name__ == "__main__":
    main()
