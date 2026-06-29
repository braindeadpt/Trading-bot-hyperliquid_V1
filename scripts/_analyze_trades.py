"""Ad-hoc trade performance analysis — run: python scripts/_analyze_trades.py"""
from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "live" / "bot.db"


def ts(ms: int | None) -> str:
    if not ms:
        return "--"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def main() -> None:
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row

    total_closed = db.execute(
        "SELECT COUNT(*) FROM trades WHERE exit_time IS NOT NULL"
    ).fetchone()[0]
    open_n = db.execute(
        "SELECT COUNT(*) FROM trades WHERE exit_time IS NULL"
    ).fetchone()[0]

    rows = db.execute(
        """
        SELECT * FROM trades
        WHERE exit_time IS NOT NULL
        ORDER BY exit_time DESC
        LIMIT 80
        """
    ).fetchall()

    print(f"DB: {DB}")
    print(f"Closed: {total_closed} | Open: {open_n}")
    print()

    total_pnl = 0.0
    wins = losses = 0
    by_strat: dict = defaultdict(lambda: {"n": 0, "pnl": 0.0, "w": 0, "l": 0})
    by_sym: dict = defaultdict(lambda: {"n": 0, "pnl": 0.0})
    by_reason = Counter()
    holds: list[float] = []
    recent: list[dict] = []

    for r in rows:
        d = dict(r)
        pnl = float(d.get("pnl_usd") or 0)
        total_pnl += pnl
        if pnl > 0:
            wins += 1
        else:
            losses += 1
        strat = d.get("sub_strategy") or d.get("strategy") or "?"
        by_strat[strat]["n"] += 1
        by_strat[strat]["pnl"] += pnl
        if pnl > 0:
            by_strat[strat]["w"] += 1
        else:
            by_strat[strat]["l"] += 1
        sym = d.get("symbol", "?")
        by_sym[sym]["n"] += 1
        by_sym[sym]["pnl"] += pnl
        reason = d.get("exit_reason") or "unknown"
        by_reason[reason] += 1
        hold_ms = (d.get("exit_time") or 0) - (d.get("entry_time") or 0)
        if hold_ms > 0:
            holds.append(hold_ms / 60_000)
        recent.append(
            {
                "id": d.get("id"),
                "sym": sym,
                "side": d.get("side"),
                "strat": strat,
                "pnl": pnl,
                "pct": d.get("pnl_pct"),
                "entry": ts(d.get("entry_time")),
                "exit": ts(d.get("exit_time")),
                "hold_min": round(hold_ms / 60_000, 1) if hold_ms else None,
                "reason": reason,
            }
        )

    n = len(rows)
    if n:
        wr = 100 * wins / n
        avg_hold = sum(holds) / len(holds) if holds else 0
        print(f"=== LAST {n} CLOSED ===")
        print(f"PnL: ${total_pnl:.2f} | W/L: {wins}/{losses} | WR: {wr:.1f}%")
        print(f"Avg hold: {avg_hold:.1f} min")
    print()

    print("By strategy (last 80):")
    for s, v in sorted(by_strat.items(), key=lambda x: -x[1]["n"]):
        wr = 100 * v["w"] / v["n"] if v["n"] else 0
        print(f"  {s}: {v['n']} trades, PnL ${v['pnl']:.2f}, WR {wr:.0f}%")

    print("\nBy symbol:")
    for s, v in sorted(by_sym.items(), key=lambda x: -x[1]["n"]):
        print(f"  {s}: {v['n']} trades, PnL ${v['pnl']:.2f}")

    print("\nExit reasons:")
    for reason, c in by_reason.most_common(15):
        print(f"  {reason}: {c}")

    print("\nLast 20 trades:")
    for t in recent[:20]:
        print(
            f"  #{t['id']} {t['exit']} {t['sym']} {t['side']} "
            f"{str(t['strat'])[:22]} PnL ${t['pnl']:.2f} "
            f"hold={t['hold_min']}m {t['reason']}"
        )

    # Split pre/post a cutoff — trades after drawdown fix (~trade id 8+ historically)
    all_rows = db.execute(
        "SELECT id, pnl_usd, exit_time FROM trades WHERE exit_time IS NOT NULL ORDER BY id"
    ).fetchall()
    if all_rows:
        first_half = all_rows[: len(all_rows) // 2]
        second_half = all_rows[len(all_rows) // 2 :]
        for label, chunk in [("First half (older)", first_half), ("Second half (newer)", second_half)]:
            pnl = sum(float(r["pnl_usd"] or 0) for r in chunk)
            w = sum(1 for r in chunk if float(r["pnl_usd"] or 0) > 0)
            print(f"\n{label}: n={len(chunk)} PnL=${pnl:.2f} WR={100*w/len(chunk):.0f}%")

    # Portfolio snapshots
    snap = db.execute(
        "SELECT * FROM portfolio_snapshots ORDER BY timestamp DESC LIMIT 5"
    ).fetchall()
    if snap:
        print("\nRecent portfolio snapshots:")
        for s in snap:
            d = dict(s)
            print(
                f"  {ts(d.get('timestamp'))} capital=${d.get('capital',0):.2f} "
                f"daily_pnl=${d.get('daily_pnl',0):.2f}"
            )

    # Decision audit
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    if "decision_audit" in tables:
        print("\nTop rejection types:")
        for r in db.execute(
            """
            SELECT decision_type, COUNT(*) n FROM decision_audit
            WHERE result='rejected' GROUP BY decision_type ORDER BY n DESC LIMIT 10
            """
        ):
            print(f"  {r[0]}: {r[1]}")
        print("\nRecent rejections:")
        try:
            for r in db.execute(
                """
                SELECT created_at, symbol, strategy, decision_type, reason
                FROM decision_audit WHERE result='rejected'
                ORDER BY created_at DESC LIMIT 12
                """
            ):
                print(f"  {ts(r[0])} {r[1]} {r[2]} [{r[3]}] {str(r[4])[:80]}")
        except sqlite3.OperationalError as e:
            print(f"  (decision_audit skip: {e})")

    db.close()

    analyze_funding_extreme()
    analyze_all_time()


def analyze_all_time() -> None:
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    r = db.execute(
        "SELECT COUNT(*), SUM(pnl_usd), SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END) FROM trades WHERE exit_time IS NOT NULL"
    ).fetchone()
    print("\n=== ALL TIME ===")
    print(f"Trades: {r[0]} | PnL: ${r[1] or 0:.2f} | Wins: {r[2]}")
    fees = db.execute(
        "SELECT SUM(COALESCE(entry_fee,0)+COALESCE(exit_fee,0)) FROM trades WHERE exit_time IS NOT NULL"
    ).fetchone()[0]
    print(f"Total fees: ${fees or 0:.2f}")
    print("\nAll-time by strategy (worst first):")
    for row in db.execute(
        """
        SELECT COALESCE(sub_strategy, strategy) AS s, COUNT(*) AS n,
               SUM(pnl_usd) AS pnl,
               SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS w,
               AVG((exit_time - entry_time) / 60000.0) AS hold_min
        FROM trades WHERE exit_time IS NOT NULL
        GROUP BY s ORDER BY pnl ASC
        """
    ):
        wr = 100 * row["w"] / row["n"] if row["n"] else 0
        print(
            f"  {row['s']}: n={row['n']} PnL=${row['pnl']:.2f} "
            f"WR={wr:.0f}% hold={row['hold_min']:.0f}m"
        )
    cut = int(datetime(2026, 6, 26, tzinfo=timezone.utc).timestamp() * 1000)
    r2 = db.execute(
        """
        SELECT COUNT(*), SUM(pnl_usd), SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END)
        FROM trades WHERE exit_time >= ?
        """,
        (cut,),
    ).fetchone()
    wr2 = 100 * r2[2] / r2[0] if r2[0] else 0
    print(f"\nSince 2026-06-26: {r2[0]} trades PnL=${r2[1] or 0:.2f} WR={wr2:.0f}%")
    db.close()


def analyze_funding_extreme() -> None:
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        """
        SELECT symbol, side, pnl_usd, entry_time, exit_time, exit_reason
        FROM trades
        WHERE exit_time IS NOT NULL
          AND (sub_strategy = 'FundingExtreme' OR strategy = 'FundingExtreme')
        ORDER BY exit_time DESC LIMIT 10
        """
    ).fetchall()
    tot = db.execute(
        """
        SELECT COUNT(*), SUM(pnl_usd) FROM trades
        WHERE exit_time IS NOT NULL
          AND (sub_strategy = 'FundingExtreme' OR strategy = 'FundingExtreme')
        """
    ).fetchone()
    print("\n=== FundingExtreme (MeanReversion) ===")
    print(f"Total: {tot[0]} trades, PnL ${tot[1] or 0:.2f}, 0% WR in last 80")
    for r in rows[:5]:
        print(f"  {ts(r['exit_time'])} {r['symbol']} {r['side']} ${r['pnl_usd']:.2f} {r['exit_reason']}")
    db.close()


if __name__ == "__main__":
    main()
