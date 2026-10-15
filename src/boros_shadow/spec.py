"""Frozen spec constants — mirrors docs/PREREGISTER_BOROS_SPREAD.md.

Anything tunable lives here and ONLY here; changing these constants
post-freeze is a protocol violation — a forward amendment is required.
"""
from __future__ import annotations

MS_D = 86_400_000
MS_YEAR = 365 * MS_D
MS_H = 3_600_000

# ─── §1 sources ──────────────────────────────────────────────────────────────
BOROS_MARKETS = "https://api.boros.finance/open-api/v1/markets"
BOROS_PAGE_LIMIT = 100
SNAPSHOT_UTC_MINUTE = 5          # hourly snapshot window opens at :05 UTC
SNAPSHOT_WINDOW_END_MIN = 10     # on-schedule window: 5 <= minute < 10 UTC

HL_INFO = "https://api.hyperliquid.xyz/info"
BINANCE_DEPTH = "https://fapi.binance.com/fapi/v1/depth"
GATE_ORDERBOOK = "https://api.gateio.ws/api/v4/futures/usdt/order_book"
OKX_BOOKS = "https://www.okx.com/api/v5/market/books"
BYBIT_ORDERBOOK = "https://api.bybit.com/v5/market/orderbook"
KUCOIN_DEPTH20 = "https://api-futures.kucoin.com/api/v1/level2/depth20"
LIGHTER_ORDERS = (
    "https://mainnet.zklighter.elliot.ai/api/v1/orderBookOrders")

BOOK_DEPTH_LIMIT = 20

# ─── §3 universe ─────────────────────────────────────────────────────────────
ASSETS = ("BTC", "ETH", "HYPE")
B_VENUES = ("binance", "gate", "okx", "bybit", "lighter", "kucoin")
HL_PREFIX = "Hyperliquid "
LIGHTER_MARKET_ID = {"BTC": 1, "ETH": 0, "HYPE": 24}

# ─── §3 trade math ───────────────────────────────────────────────────────────
NOTIONAL_USD = 10_000.0                # N per leg
MIN_MATURITY_D = 14
MAX_MATURITY_D = 90
TAKER_BPS = {                          # perp-leg taker VIP0, bps
    "hyperliquid": 4.5, "binance": 5.0, "gate": 5.0, "okx": 5.0,
    "bybit": 5.5, "lighter": 0.0, "kucoin": 6.0,
}
BOROS_SLIP_RATE = 0.0001               # 1 bp/yr slippage per Boros leg
PERP_CAPITAL_FRAC = 0.2                # 0.2*N for the perp legs (pooled)
BOROS_CAPITAL_FRAC = 0.1               # 0.1*N per Boros leg
NET_APY_MIN = 0.08                     # enter when net on capital >= 8%/yr
NEAR_MISS_APY = 0.05                   # >=5% but not entered -> near-entry
MAX_NEW_PER_ASSET_PER_DAY = 1

# ─── Phase 2 read gate (kept sealed — Phase 2 not deployed) ──────────────────
READ_MIN_CLUSTERS = 10
EXPIRY_ISO = "2027-06-30"

# ─── cadence ─────────────────────────────────────────────────────────────────
TICK_S = 60
HEARTBEAT_S = 300
HEARTBEAT_STALE_S = 900

DB_PATH = "data/research/boros_shadow.db"
