"""Check backtest data coverage."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "live" / "bot.db"


def ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def main() -> None:
    db = sqlite3.connect(DB)
    for tf in ["candles_1m", "candles_5m", "candles_15m", "candles_1h"]:
        for sym in ["BTC", "ETH", "SOL", "HYPE"]:
            r = db.execute(
                f"SELECT COUNT(*), MIN(timestamp_ms), MAX(timestamp_ms) FROM {tf} WHERE symbol=?",
                (sym,),
            ).fetchone()
            print(f"{tf:12s} {sym}: {r[0]:6d}  {ts(r[1]) if r[1] else '-'} -> {ts(r[2]) if r[2] else '-'}")

    for t, col in [
        ("funding_history", "timestamp"),
        ("oi_history", "timestamp"),
        ("liquidation_events", "timestamp_ms"),
        ("binance_perp_prices", "timestamp_ms"),
    ]:
        try:
            r = db.execute(f"SELECT COUNT(*), MIN({col}), MAX({col}) FROM {t}").fetchone()
            print(f"{t:22s}: {r[0]:6d}  {ts(r[1]) if r[1] else '-'} -> {ts(r[2]) if r[2] else '-'}")
        except Exception as e:
            print(f"{t}: {e}")

    # buy/sell volume in 1m candles
    r = db.execute(
        "SELECT COUNT(*) FROM candles_1m WHERE buy_volume > 0 OR sell_volume > 0"
    ).fetchone()[0]
    total = db.execute("SELECT COUNT(*) FROM candles_1m").fetchone()[0]
    print(f"candles_1m with buy/sell volume: {r}/{total}")

    db.close()


if __name__ == "__main__":
    main()
