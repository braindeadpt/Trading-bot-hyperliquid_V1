"""Frozen spec constants — mirrors notes/resmom-forward-prereg-2026-10-28.md.

Anything tunable lives here and ONLY here; changing these constants
post-freeze is a protocol violation — a forward amendment is required.
"""
from __future__ import annotations

import datetime as _dt

MS_D = 86_400_000
MS_H = 3_600_000
MS_WEEK = 7 * MS_D

# ─── §regra: universe ────────────────────────────────────────────────────────
UNIVERSE_TOP_N = 25            # top perps by 30d mean notional volume
VOL_WINDOW_D = 30              # notional volume mean window (days)
MIN_HISTORY_D = 90             # >=90d HL history required
# stables excluded from the universe (BTC stays: ranks in, legs out)
STABLES = frozenset({
    "USDT", "USDC", "USDH", "USDE", "DAI", "FDUSD", "USDQ", "TUSD",
    "USDD", "PYUSD", "EURC", "XUSD", "BUSD", "USDP",
})
BTC = "BTC"

# ─── §regra: signal (Arm A, frozen) ──────────────────────────────────────────
BETA_WINDOW_D = 60             # OLS beta window (daily log returns vs BTC)
RESID_WINDOW_D = 15            # cumulative residual window
VOL30_WINDOW_D = 30            # realized-vol window (sample std, ddof=1)
QUINTILE_FRAC = 0.2            # floor(0.2*n) names per leg

# ─── §regra: portfolio ───────────────────────────────────────────────────────
GROSS_TOTAL = 1.0              # total gross (both legs)
GROSS_LEG = 0.5                # fallback leg gross when beta-neutral unsolvable
BETA_EPS = 1e-12               # unit-beta floor for the neutral solve
NOTIONAL_USD = 10_000.0        # paper gross notional
COST_BPS_SIDE = 6.5            # 6.5 bps per side on turnover
STOP_DESCRIBED = -0.25         # 25% stop is descriptive only — never acts

# ─── §leitura ────────────────────────────────────────────────────────────────
N_NULLS = 200                  # random-signal portfolios per week
READ_REBALANCES = 26           # single read at 26 closed rebalances
READ_FIRST = "2026-11-09"      # first scheduled rebalance (Mon) — Amendment 1
READ_DATE_APPROX = "2027-05-03"
READ_FIRST_MS = int(
    _dt.datetime.fromisoformat(READ_FIRST)
    .replace(tzinfo=_dt.timezone.utc).timestamp() * 1000)
KILL_DD = -0.35                # early kill: net drawdown <= -35%
PASS_SHARPE = 0.5              # annualized, weekly returns
SHARPE_P75_NULLS = True        # Sharpe must beat p75 of null Sharpes

# ─── cadence ─────────────────────────────────────────────────────────────────
REBALANCE_WEEKDAY = 0          # Monday (weekday() == 0)
REBALANCE_MIN_UTC = 5          # timer fires Mon 00:05 UTC
FILL_LAG_S = 300               # first 1m candle with t >= W+300s
MARK_MIN_UTC = 10              # daily NAV mark at 00:10 UTC (candle finality)
FUNDING_MIN_UTC = 10           # hourly funding accrual at :10 UTC
TICK_S = 60
HEARTBEAT_S = 300
HEARTBEAT_STALE_S = 7200       # healthcheck grace: 2h

# ─── sources ─────────────────────────────────────────────────────────────────
HL_INFO = "https://api.hyperliquid.xyz/info"
BINANCE_KLINES = "https://fapi.binance.com/fapi/v1/klines"

DB_PATH = "data/research/resmom_shadow.db"
