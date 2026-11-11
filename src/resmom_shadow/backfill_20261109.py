"""One-shot, idempotent Amendment-2 backfill for the 2026-11-09 ResMom rebalance.

Mirrors ``notes/resmom-forward-prereg-2026-10-28.md`` Amendment 2:
  - Target weights recorded at ``2026-11-09 00:05:41 UTC`` on arms ``A`` and ``B``
    are preserved verbatim (never recomputed).
  - Entry fills use the exact deterministic prereg price: ``open`` of the first
    ``1m`` candle with ``t >= 2026-11-09 00:05:00 UTC`` (``1794182700000``).
  - ``turnover_usd`` and ``cost_usd`` are recalculated strictly over real fills.
  - Embeds those same ``00:05 UTC`` entry fills into ``resmom_nulls`` for week
    ``1794182400000`` (which had ``"px": {}`` due to the swallowed 429s).

Safety contract:
  - **Dry-run by default** (no ``--apply``): opens SQLite in ``?mode=ro``, prints
    the full JSON diff of every row that would be inserted/updated, and writes
    zero bytes.
  - **Idempotent**: if ``resmom_positions`` already contains rows for ``arm='A'``
    on ``week_key_ms=1794182400000``, exits cleanly with ``already_applied=True``.

Usage:
  python -m src.resmom_shadow.backfill_20261109 --db /data/research/resmom_shadow.db
  python -m src.resmom_shadow.backfill_20261109 --db /data/research/resmom_shadow.db --apply
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src.resmom_shadow import portfolio as pf
from src.resmom_shadow import prices
from src.resmom_shadow import spec

TARGET_WEEK_MS: int = 1794182400000  # Monday 2026-11-09 00:00:00 UTC
ENTRY_FILL_MS: int = TARGET_WEEK_MS + spec.FILL_LAG_S * 1000  # 2026-11-09 00:05:00 UTC


def build_backfill_plan(
    db_path: str,
    *,
    fill_fn: Callable[[str, int], Optional[float]] = prices.first_1m_open,
    pace_s: float = 0.08,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> Dict[str, Any]:
    """Read-only diff builder: computes exact inserts/updates without touching DB."""
    con = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        reb_a = con.execute(
            "SELECT * FROM resmom_rebalances WHERE week_key_ms=? AND arm='A'",
            (TARGET_WEEK_MS,),
        ).fetchone()
        reb_b = con.execute(
            "SELECT * FROM resmom_rebalances WHERE week_key_ms=? AND arm='B'",
            (TARGET_WEEK_MS,),
        ).fetchone()
        if reb_a is None or reb_b is None:
            raise RuntimeError(
                f"Missing rebalance row(s) for week_key_ms={TARGET_WEEK_MS} (A={bool(reb_a)}, B={bool(reb_b)})"
            )

        existing_a = con.execute(
            "SELECT COUNT(*) c FROM resmom_positions WHERE rebalance_id=? AND arm='A'",
            (int(reb_a["id"]),),
        ).fetchone()["c"]
        existing_b = con.execute(
            "SELECT COUNT(*) c FROM resmom_positions WHERE rebalance_id=? AND arm='B'",
            (int(reb_b["id"]),),
        ).fetchone()["c"]
        if int(existing_a) > 0 or int(existing_b) > 0:
            return {
                "already_applied": True,
                "week_key_ms": TARGET_WEEK_MS,
                "existing_positions": {"A": int(existing_a), "B": int(existing_b)},
            }

        weights_a: Dict[str, float] = {
            k: float(v) for k, v in json.loads(reb_a["weights_json"]).items()
        }
        weights_b: Dict[str, float] = {
            k: float(v) for k, v in json.loads(reb_b["weights_json"]).items()
        }
        null_rows = [
            dict(r)
            for r in con.execute(
                "SELECT id, null_id, weights_json FROM resmom_nulls WHERE week_key_ms=? ORDER BY null_id ASC",
                (TARGET_WEEK_MS,),
            ).fetchall()
        ]
        null_syms = set()
        for nr in null_rows:
            w_obj = json.loads(nr["weights_json"]).get("w") or {}
            null_syms.update(w_obj.keys())

        needed_syms = sorted(set(weights_a) | set(weights_b) | null_syms)
        fills: Dict[str, float] = {}
        missing_syms: List[str] = []
        for idx, sym in enumerate(needed_syms):
            if idx > 0 and pace_s > 0:
                sleep_fn(pace_s)
            px = fill_fn(sym, ENTRY_FILL_MS)
            if px is not None and px > 0:
                fills[sym] = float(px)
            else:
                missing_syms.append(sym)

        arms_plan: Dict[str, Any] = {}
        for arm, reb_row, weights in (("A", reb_a, weights_a), ("B", reb_b, weights_b)):
            positions_to_insert = []
            executed_w: Dict[str, float] = {}
            skipped: List[str] = []
            for sym, w in sorted(weights.items()):
                px = fills.get(sym)
                if px is None:
                    skipped.append(sym)
                    continue
                side = "long" if w > 0 else "short"
                qty = abs(w) * spec.NOTIONAL_USD / px
                positions_to_insert.append({
                    "rebalance_id": int(reb_row["id"]),
                    "arm": arm,
                    "symbol": sym,
                    "side": side,
                    "weight": abs(w),
                    "qty": qty,
                    "entry_px": px,
                    "entry_ms": ENTRY_FILL_MS,
                })
                executed_w[sym] = w

            tover = pf.turnover_usd({}, executed_w)
            cost = tover * spec.COST_BPS_SIDE / 1e4
            gross = sum(abs(v) for v in executed_w.values()) * spec.NOTIONAL_USD
            nav_entry = spec.NOTIONAL_USD - cost

            arms_plan[arm] = {
                "rebalance_id": int(reb_row["id"]),
                "before": {
                    "turnover_usd": float(reb_row["turnover_usd"]),
                    "cost_usd": float(reb_row["cost_usd"]),
                    "n_positions": 0,
                },
                "after": {
                    "turnover_usd": round(tover, 6),
                    "cost_usd": round(cost, 6),
                    "gross_usd": round(gross, 6),
                    "entry_nav_usd": round(nav_entry, 6),
                    "n_positions": len(positions_to_insert),
                    "skipped": skipped,
                },
                "positions": positions_to_insert,
            }

        null_updates = []
        for nr in null_rows:
            payload = json.loads(nr["weights_json"])
            w_map = payload.get("w") or {}
            px_map = {s: fills[s] for s in w_map if s in fills}
            executed_null_w = {s: float(w_map[s]) for s in px_map}
            null_tover = pf.turnover_usd({}, executed_null_w)
            null_cost = null_tover * spec.COST_BPS_SIDE / 1e4
            null_nav = spec.NOTIONAL_USD - null_cost
            null_updates.append({
                "row_id": int(nr["id"]),
                "null_id": int(nr["null_id"]),
                "weights_json": json.dumps({"w": w_map, "px": px_map}, sort_keys=True),
                "cost_usd": null_cost,
                "nav_usd": null_nav,
            })

        return {
            "already_applied": False,
            "week_key_ms": TARGET_WEEK_MS,
            "entry_fill_ms": ENTRY_FILL_MS,
            "symbols_queried": len(needed_syms),
            "symbols_filled": len(fills),
            "missing_symbols": missing_syms,
            "fills": fills,
            "arms": arms_plan,
            "nulls_to_update": len(null_updates),
            "_null_updates": null_updates,
        }
    finally:
        con.close()


def apply_backfill_plan(db_path: str, plan: Dict[str, Any]) -> Dict[str, Any]:
    """Atomically write the computed Amendment-2 backfill plan into *db_path*."""
    if plan.get("already_applied"):
        return {"applied": False, "already_applied": True}

    for arm in ("A", "B"):
        if plan["arms"][arm]["after"]["n_positions"] == 0:
            raise RuntimeError(f"Refusing to apply empty backfill on arm={arm}")

    now_ms = int(time.time() * 1000)
    con = sqlite3.connect(str(Path(db_path)), timeout=30.0)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(resmom_rebalances)")}
        if "status" not in cols:
            con.execute(
                "ALTER TABLE resmom_rebalances ADD COLUMN status TEXT NOT NULL DEFAULT 'ok'"
            )
        with con:
            for arm in ("A", "B"):
                ap = plan["arms"][arm]
                rid = int(ap["rebalance_id"])
                after = ap["after"]
                con.execute(
                    "UPDATE resmom_rebalances SET turnover_usd=?, cost_usd=?, status='ok' WHERE id=?",
                    (after["turnover_usd"], after["cost_usd"], rid),
                )
                for p in ap["positions"]:
                    con.execute(
                        "INSERT INTO resmom_positions(rebalance_id, arm, symbol, side,"
                        " weight, qty, entry_px, entry_ms, status)"
                        " VALUES(?,?,?,?,?,?,?,?, 'open')",
                        (
                            p["rebalance_id"], p["arm"], p["symbol"], p["side"],
                            p["weight"], p["qty"], p["entry_px"], p["entry_ms"],
                        ),
                    )
                # Update entry NAV row for this arm at ENTRY_FILL_MS
                con.execute(
                    "DELETE FROM resmom_nav WHERE arm=? AND ts_ms=?",
                    (arm, ENTRY_FILL_MS),
                )
                legs_zero = {p["symbol"]: 0.0 for p in ap["positions"]}
                con.execute(
                    "INSERT INTO resmom_nav(ts_ms, arm, nav_usd, gross_usd,"
                    " n_positions, dd_pct, legs_json) VALUES(?,?,?,?,?,?,?)",
                    (
                        ENTRY_FILL_MS,
                        arm,
                        after["entry_nav_usd"],
                        after["gross_usd"],
                        after["n_positions"],
                        0.0,
                        json.dumps(legs_zero, sort_keys=True),
                    ),
                )
                con.execute(
                    "INSERT INTO resmom_meta(k, v, ts_ms) VALUES(?,?,?)"
                    " ON CONFLICT(k) DO UPDATE SET v=excluded.v, ts_ms=excluded.ts_ms",
                    (f"costs_{arm}", repr(after["cost_usd"]), now_ms),
                )
                con.execute(
                    "INSERT INTO resmom_meta(k, v, ts_ms) VALUES(?,?,?)"
                    " ON CONFLICT(k) DO UPDATE SET v=excluded.v, ts_ms=excluded.ts_ms",
                    (f"funding_from_{arm}", str(ENTRY_FILL_MS), now_ms),
                )
                con.execute(
                    "INSERT INTO resmom_meta(k, v, ts_ms) VALUES(?,?,?)"
                    " ON CONFLICT(k) DO UPDATE SET v=excluded.v, ts_ms=excluded.ts_ms",
                    (f"rebalance_failed_{arm}", "", now_ms),
                )

            for nu in plan.get("_null_updates", []):
                con.execute(
                    "UPDATE resmom_nulls SET weights_json=?, cost_usd=?, nav_usd=? WHERE id=?",
                    (nu["weights_json"], nu["cost_usd"], nu["nav_usd"], nu["row_id"]),
                )

            con.execute(
                "INSERT INTO resmom_log(ts_ms, kind, ref_id, data_json) VALUES(?,?,?,?)",
                (
                    now_ms,
                    "amendment_2_backfill_20261109",
                    plan["arms"]["A"]["rebalance_id"],
                    json.dumps(
                        {
                            "week_key_ms": TARGET_WEEK_MS,
                            "entry_fill_ms": ENTRY_FILL_MS,
                            "A": plan["arms"]["A"]["after"],
                            "B": plan["arms"]["B"]["after"],
                            "nulls_updated": plan["nulls_to_update"],
                        },
                        sort_keys=True,
                    ),
                ),
            )
        return {
            "applied": True,
            "already_applied": False,
            "A_opened": plan["arms"]["A"]["after"]["n_positions"],
            "B_opened": plan["arms"]["B"]["after"]["n_positions"],
            "nulls_updated": plan["nulls_to_update"],
        }
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=spec.DB_PATH, help="Path to resmom_shadow.db")
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Write the backfill atomically (default without --apply is read-only diff)",
    )
    args = ap.parse_args()

    plan = build_backfill_plan(args.db)
    preview = {k: v for k, v in plan.items() if not k.startswith("_")}
    print(json.dumps({"mode": "apply" if args.apply else "dry_run_diff", "plan": preview}, indent=2))

    if not args.apply or plan.get("already_applied"):
        return 0

    res = apply_backfill_plan(args.db, plan)
    print(json.dumps({"result": res}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
