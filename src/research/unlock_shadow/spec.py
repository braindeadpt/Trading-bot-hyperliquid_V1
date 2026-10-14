"""Frozen spec constants — mirrors docs/PREREGISTER_UNLOCK_SHADOW.md.

Anything tunable lives here and ONLY here; changing these constants
post-freeze is a protocol violation (the prereg hash is the contract).
"""
from __future__ import annotations

MS_D = 86_400_000

# ─── §1 calendar ─────────────────────────────────────────────────────────────
SNAPSHOT_UTC_HHMM = (0, 30)          # daily snapshot at 00:30 UTC
DEFILLAMA_INDEX = "https://defillama-datasets.llama.fi/emissionsIndex"
DEFILLAMA_EMISSIONS = "https://defillama-datasets.llama.fi/emissions/{}"
CRYPTORANK_VESTING = "https://api.cryptorank.io/v0/coins/{}/vesting"
TOKENOMIST_VESTING = "https://api.tokenomist.ai/api/v1/unlocks/{}"  # fallback
CRYPTORANK_ENV_KEY = "CRYPTORANK_API_KEY"

# ─── §1/§2 source-A parsing (backtest parity — notes/unlocks/02_events.py)
MAP_EXCLUDED_SYMBOLS = frozenset({"BTC", "ETH", "AI"})  # DefiLlama 'AI' = Gensyn
INCL_CATEGORIES = frozenset({
    "insiders", "privatesale", "publicsale", "airdrop", "ecosystem",
    "liquidity", "community", "uncategorized", "other", "launch"})
RECIPIENT_RES = (
    r"unlocked from (.+?) on \{",
    r"\} of (.+?) tokens",
)
NOTES_ONCHAIN_RE = r"transfer|claim|onchain|on-chain|outflow|event"

# ─── §2 eligibility ──────────────────────────────────────────────────────────
MIN_CLIFF_PCT_UNLOCKED = 0.02        # >= 2% of unlocked supply
SOURCES_REQUIRED = 2                 # must appear in BOTH sources
SEEN_CUTOFF_D = 6                    # present in the snapshot of T-6 or before
DATE_TOL_MS = 1 * MS_D               # cross-source date match +-1 day
AMOUNT_TOL = 0.25                    # cross-source amount match +-25%
# category labels excluded from the cliff set (matched lowercase, substring)
EXCLUDED_CATEGORIES = (
    "noncirculating", "non-circulating", "farming", "staking", "staked",
    "burned", "burn", "onchain", "on-chain", "on_chain",
)

# ─── §3 filter / merge ───────────────────────────────────────────────────────
ADV_MIN_USD = 10_000_000             # ADV 7d on HL >= $10M at T-5
MERGE_WINDOW_MS = 14 * MS_D          # same-token events <14d merge into first

# ─── §4 trade ────────────────────────────────────────────────────────────────
NOTIONAL_USD = 10_000.0              # token short leg
WINDOW_PRE_D = 5                     # open at 00:00 UTC of T-5
WINDOW_POST_D = 2                    # close at 00:00 UTC of T+2
BETA_WINDOW_D = 30                   # beta on 30d daily returns vs BTC
BETA_MIN, BETA_MAX = 0.5, 2.0        # beta clamp
FEE_BPS_PER_SIDE = 4.5               # taker per side per leg (9 bps rt)
HEDGE_COIN = "BTC"

# ─── §5 placebo ──────────────────────────────────────────────────────────────
PLACEBO_N = 3                        # per eligible event
PLACEBO_UNLOCK_PCT = 0.005           # window invalid if unlock >=0.5% +-14d
PLACEBO_GUARD_MS = 14 * MS_D
PLACEBO_HORIZON_MIN_D = 15           # draws only FUTURE windows so the same
PLACEBO_HORIZON_MAX_D = 75           # live L2 pricing path applies (§5)

# ─── §6 read gate ────────────────────────────────────────────────────────────
READ_MIN_CLOSED = 15                 # n>=15 eligible closed, or expiry
EXPIRY_ISO = "2027-10-14"
REVIEW_NET_BPS = -3000.0             # single-trade manual-review trigger

# ─── cadence ─────────────────────────────────────────────────────────────────
TICK_S = 60                          # daemon loop cadence
HEARTBEAT_S = 300                    # meta heartbeat for a future /ops row
HEARTBEAT_STALE_S = 900

DB_PATH = "data/research/unlock_shadow.db"
