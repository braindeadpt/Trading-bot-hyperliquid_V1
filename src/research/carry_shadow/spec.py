"""Frozen spec constants — mirrors docs/PREREGISTER_CARRY_SHADOW.md.

Anything tunable lives here and ONLY here; changing these constants post-
freeze is a protocol violation (the prereg hash is the contract).
"""
from __future__ import annotations

# ─── signal (identical to A1 prereg) ─────────────────────────────────────────
ENTRY_F_ANN = 0.15            # annualized 24h-mean funding entry threshold
EXIT_NEG_H = 48               # exit after 48h consecutive negative 24h-mean
SPOT_MIN_DAILY_USD = 100_000
PERP_MIN_DAILY_USD = 1_000_000
MIN_HISTORY_D = 30
LIQ_LOOKBACK_D = 30

# ─── margin rule (proportional deleverage, per-pair M/M0 by fixed rule) ──────
M_BASE = 0.45                 # trigger margin fraction
M0_BASE = 0.60                # post-deleverage margin fraction
TRIGGER_HEADROOM = 0.15       # M must sit >= 15pp above maintenance
DELEV_FLOOR = 0.05            # keep<=0.05 -> close episode entirely
SPOT_CAPITAL = 1.0            # spot leg paid 100%
PM_USDC_BUFFER = 0.10         # PM branch: extra USDC buffer
# PM collateral eligibility confirmed only for HYPE (LTV .65). BTC(.50) is
# documented but UBTC spot→BTC PM collateralization is UNCONFIRMED — treat
# as non-PM (normal deleverage) until verified live; add to set when proven.
PM_ELIGIBLE = {"HYPE"}
PM_ACCOUNT_MIN_USD = 10_000   # PM requires account value > $10k


def maint_for(max_leverage: float) -> float:
    """HL maintenance fraction = 1 / (2 * maxLeverage) — documented rule."""
    return 1.0 / (2.0 * max_leverage)


def mm_for(max_leverage: float) -> tuple[float, float]:
    """Per-pair (M, M0) by the fixed rule — never hand-tuned."""
    m = max(M_BASE, maint_for(max_leverage) + TRIGGER_HEADROOM)
    return m, m + TRIGGER_HEADROOM


# ─── execution cost model (measurement fallbacks until live data) ────────────
MAKER_FEE_BPS = 1.5
TAKER_FEE_BPS = 4.5
DELEV_TAKER_RT_BPS = 9.0      # taker both legs on the closed fraction
MAKER_FILL_WINDOW_S = 900     # 15 min fill window per leg
MIN_FILL_RATE = 0.40          # kill gate, §6.2
FILL_RATE_MIN_ATTEMPTS = 20   # interim fill-gate sample size per leg
EDGE_MIN_X = 3.0              # edge >= 3x measured RT cost, §6.1
EDGE_CHECK_MIN_EPS = 5        # interim edge check after >=5 closed eps
HURDLE_RF = 0.04              # rf fixed 2026-10-09
HURDLE_APR = HURDLE_RF + 0.04  # >= 8.0%/yr on committed capital

# ─── cadence / housekeeping ──────────────────────────────────────────────────
MARGIN_CHECK_S = 60           # live margin evaluation cadence
FUNDING_POLL_S = 300          # fundingHistory refresh cadence
GATE_REFRESH_S = 3600         # PIT liquidity gate refresh cadence
HEARTBEAT_S = 60              # meta heartbeat for the /ops feed-silence row
HEARTBEAT_STALE_S = 600       # /ops shows carry_shadow red past this
# §11 gap semantics (measurement, 2026-10-09): a WS gap longer than this
# overlapping a pending entry leg or an eligible entry marks the episode
# gap_unverified / emits gap_entry_unverified
GAP_UNVERIFIED_MS = 5_000     # >5s of tape silence matters for entry state
LOOP_LAG_TICK_S = 1.0         # event-loop lag sampler cadence
LOOP_LAG_WINDOW = 120         # ticks kept (≈ last 2 min); reported max+p99
SHADOW_NOTIONAL_USD = 10_000  # hypothetical leg size for the fill proxy
READ_EPS_TARGET = 10          # >=10 closed clustered episodes
CLUSTER_GAP_H = 24
EXPIRY_ISO = "2027-12-26"
REVIEW_ISO = "2027-04-09"     # 6-month count-only review

DB_PATH = "data/research/carry_shadow.db"
