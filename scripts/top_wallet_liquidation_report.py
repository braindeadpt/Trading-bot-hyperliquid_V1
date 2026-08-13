"""Top-wallet forced-liquidation report (idea #2, research only).

The tracker persists ``top_trader_collapse_events`` whenever a tracked top
wallet's position collapses >= collapse_drop_pct between polls. A collapse
is a *candidate* forced liquidation — voluntary exits look identical at
poll granularity. The confirmation step lives here: match each collapse
against the real liquidation feed (liquidation_events in bot.db, same
symbol, within a time window).

Evidence gate (per RESEARCH_BACKLOG discipline): the event-driven
top-wallet-liquidation strategy is only worth building if collapses are
(a) frequent enough and (b) confirmed by the liquidation feed at a
meaningful rate. Target: >= 8 confirmed events per month.

Reads both DBs read-only. Writes nothing.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.research.top_trader_store import TopTraderStore

BOT_DB = ROOT / "data" / "live" / "bot.db"
MATCH_WINDOW_MS = 5 * 60_000  # liquidation within +/-5 min of the collapse


def load_liquidation_map(symbols: List[str], since_ms: int) -> Dict[str, List[float]]:
    conn = sqlite3.connect(f"file:{BOT_DB}?mode=ro", uri=True)
    cur = conn.cursor()
    out: Dict[str, List[float]] = {s: [] for s in symbols}
    for sym in symbols:
        cur.execute(
            "SELECT timestamp_ms FROM liquidation_events "
            "WHERE symbol = ? AND timestamp_ms >= ? AND source IN ('okx','bybit')",
            (sym, since_ms),
        )
        out[sym] = [int(r[0]) for r in cur.fetchall()]
    conn.close()
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()

    since_ms = int(time.time() * 1000) - args.days * 86_400_000
    store = TopTraderStore()
    events = store.collapse_events(since_ms=since_ms)
    symbols = sorted({e["symbol"] for e in events})
    liq_map = load_liquidation_map(symbols, since_ms - MATCH_WINDOW_MS)

    print("=" * 78)
    print("  TOP-WALLET FORCED LIQUIDATION REPORT (idea #2)")
    print(f"  last {args.days}d | collapses: {len(events)}")
    print("=" * 78)

    if not events:
        print("\n  Nenhum colapso registado ainda — o tracker começou a gravar")
        print("  quando este código arrancou. Correr novamente em 1-2 semanas.")
        return

    import bisect

    confirmed = 0
    per_symbol: Dict[str, Dict[str, Any]] = {}
    for e in events:
        sym = e["symbol"]
        ts = e["ts_ms"]
        ts_list = liq_map.get(sym, [])
        i = bisect.bisect_left(ts_list, ts - MATCH_WINDOW_MS)
        hit = any(abs(ts - t) <= MATCH_WINDOW_MS for t in ts_list[i : i + 8])
        e["confirmed"] = bool(hit)
        confirmed += int(hit)
        d = per_symbol.setdefault(sym, {"n": 0, "confirmed": 0,
                                        "notional_sum": 0.0})
        d["n"] += 1
        d["confirmed"] += int(hit)
        d["notional_sum"] += e["from_notional"]

    print(f"\n  Confirmados por feed de liquidações: {confirmed}/{len(events)} "
          f"({100.0 * confirmed / len(events):.0f}%)")
    print(f"\n  {'symbol':6}{'n':>5}{'conf':>6}{'conf%':>7}{'notional$M':>11}")
    for sym in sorted(per_symbol):
        d = per_symbol[sym]
        print(f"  {sym:6}{d['n']:>5}{d['confirmed']:>6}"
              f"{100.0*d['confirmed']/max(1,d['n']):>6.0f}%"
              f"{d['notional_sum']/1e6:>11.1f}")

    per_day = len(events) / max(1.0, args.days / 30.0)
    rate = confirmed / max(1.0, args.days / 30.0)
    print(f"\n  Frequência: {per_day:.1f} colapsos/mês | "
          f"{rate:.1f} confirmados/mês "
          f"(gate de evidência: >= 8 confirmados/mês)")
    if confirmed >= 8:
        print("  ✅ GATE PASS — fluxo suficiente; próximo passo: shadow da estratégia")
    else:
        print("  ⏳ GATE NÃO ATINGIDO — continuar a acumular; não construir ainda")

    print("\n  Últimos 10 eventos:")
    for e in events[-10:]:
        mark = "CONFIRMED" if e["confirmed"] else "unconfirmed"
        print(f"  [{mark:11s}] {datetime.fromtimestamp(e['ts_ms']/1000, timezone.utc):%m-%d %H:%M} "
              f"{e['symbol']:5} {e['side']:5} {e['wallet'][:10]} "
              f"${e['from_notional']/1e3:.0f}K -> ${e['to_notional']/1e3:.0f}K")


if __name__ == "__main__":
    main()
