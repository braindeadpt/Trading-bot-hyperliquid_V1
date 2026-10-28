"""Frozen constants — notes/latency-measure-prereg-2026-10-28.md.

Any change here is a spec change: edit the prereg first.
"""
from __future__ import annotations

DB_PATH = "data/research/latency_probe.db"

# ── windows (America/New_York, weekdays) ───────────────────────────────
NY_TZ = "America/New_York"
US_CLOSE_START = (19, 45)          # 19:45 NY
US_CLOSE_END = (20, 15)            # 20:15 NY
CONTROL_START = (15, 0)            # 15:00 NY
CONTROL_MIN = 30                   # 30 min control
WEEKDAYS = frozenset({0, 1, 2, 3, 4})   # Mon..Fri (America/New_York)

# ── feeds ────────────────────────────────────────────────────────────
BINANCE_STREAM = "wss://fstream.binance.com/stream"
HL_WS = "wss://api.hyperliquid.xyz/ws"

# Binance USDT-M perp -> trade.xyz names that should reflect it.
# Declared in the prereg as "símbolos com par nos dois venues": the only
# economically meaningful mapping is crypto-linked xyz perps.
SYMBOL_PAIRS = {
    "BTCUSDT": ["xyz:MSTR", "xyz:COIN", "xyz:IREN", "xyz:HOOD"],
    "ETHUSDT": ["xyz:BMNR"],
    "PAXGUSDT": ["xyz:GOLD"],
}
BIN_SYMBOLS = sorted(SYMBOL_PAIRS)
XYZ_SYMBOLS = sorted(s for v in SYMBOL_PAIRS.values() for s in v)

# ── event rule ───────────────────────────────────────────────────────
MOVE_BPS = 15.0            # Binance mid move threshold
MOVE_WINDOW_MS = 1000      # ... within <= 1 s
HALF_RATIO = 0.5           # trade.xyz mid must cover >= half the move
ENTRY_LAG_MS = 500         # hypothetical taker entry at t0+500ms
HOLD_MS = 5000             # exit mark at t0+5s
EVENT_COOLDOWN_MS = 5000   # one open event per Binance symbol
ENTRY_DEADLINE_MS = 1500   # no xyz tick by t0+1.5s -> no_fill
TAKER_COST_BPS = 11.0      # HL taker round trip

# ── readout ──────────────────────────────────────────────────────────
MIN_SESSIONS = 10
MIN_EVENTS = 100
MAX_WEEKS = 4
CLUSTER_ALPHA = 0.05       # 95% CI across sessions
DOMINANT_SHARE = 0.5       # "not explained by 1-2 events"

HEARTBEAT_S = 30
