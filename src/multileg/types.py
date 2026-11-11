"""Domain models for Hyperliquid multi-leg coordination (Spot+Perp & Perp-Perp)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Literal, Optional

MarketKind = Literal["spot", "perp"]
LegSide = Literal["buy", "sell"]
MarginProfile = Literal["unified", "portfolio_margin", "standard_transfer"]
EpisodeStatus = Literal[
    "pending_entry",
    "open",
    "pending_exit",
    "closed",
    "aborted",
    "aborted_unwound",
    "liquidated_guard",
]

# Fixed margin rule constants (mirrors docs/ops/PREREGISTER_CARRY_SHADOW.md §2)
M_BASE: float = 0.45
M0_BASE: float = 0.60
TRIGGER_HEADROOM: float = 0.15
DELEV_FLOOR: float = 0.05
PM_USDC_BUFFER: float = 0.10
PM_ELIGIBLE_BASES = frozenset({"HYPE"})
PM_MIN_ACCOUNT_USD: float = 10_000.0


@dataclass(frozen=True)
class TopOfBook:
    """Top-of-book bid/ask quote for a single Hyperliquid market."""

    bid: float
    ask: float
    timestamp_ms: int = 0

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return 0.5 * (self.bid + self.ask)
        return 0.0


@dataclass(frozen=True)
class CarryMarginParams:
    """Deterministic maintenance & proportional-deleverage parameters per pair."""

    max_leverage: float
    maint_fraction: float
    m_trigger: float
    m0_initial: float
    delev_floor: float = DELEV_FLOOR
    pm_branch: bool = False

    @classmethod
    def for_pair(
        cls,
        base_asset: str,
        max_leverage: float,
        *,
        pm_enabled: bool = False,
        account_value_usd: float = 0.0,
    ) -> "CarryMarginParams":
        lev = max(1.0, float(max_leverage))
        maint = 1.0 / (2.0 * lev)
        m_trig = max(M_BASE, maint + TRIGGER_HEADROOM)
        m0 = m_trig + TRIGGER_HEADROOM
        is_pm = (
            bool(pm_enabled)
            and base_asset.upper() in PM_ELIGIBLE_BASES
            and float(account_value_usd) >= PM_MIN_ACCOUNT_USD
        )
        return cls(
            max_leverage=lev,
            maint_fraction=maint,
            m_trigger=m_trig,
            m0_initial=m0,
            delev_floor=DELEV_FLOOR,
            pm_branch=is_pm,
        )


@dataclass(frozen=True)
class LegSpec:
    """Immutable specification of one leg within a multi-leg episode."""

    leg_id: str
    symbol: str
    market_kind: MarketKind
    side: LegSide
    target_size: float
    limit_price: Optional[float] = None
    reduce_only: bool = False


@dataclass
class LegState:
    """Mutable runtime fill state for one leg."""

    spec: LegSpec
    status: str = "pending"  # pending | resting | partial | filled | cancelled | unwound
    order_id: Optional[str] = None
    cloid: Optional[str] = None
    filled_size: float = 0.0
    avg_fill_price: float = 0.0
    cumulative_fee: float = 0.0
    filled_at_ms: Optional[int] = None

    @property
    def is_filled(self) -> bool:
        return (
            self.spec.target_size > 0
            and self.filled_size >= self.spec.target_size * 0.999
        )

    @property
    def remaining_size(self) -> float:
        return max(0.0, self.spec.target_size - self.filled_size)


@dataclass(frozen=True)
class DeleverageAction:
    """Outcome of one margin check on an open Spot+Perp carry episode."""

    zone: str  # ok | deleveraged | closed_floor | liquidated_guard
    margin_fraction: float
    close_fraction: float = 0.0
    closed_qty: float = 0.0
    remaining_qty: float = 0.0


@dataclass
class MultiLegEpisode:
    """Tracks the full lifecycle of a coordinated multi-leg position."""

    episode_id: str
    strategy: str  # e.g. CarryA1 | UnlockShort | ResMom
    mode: str  # paper | testnet | mainnet
    margin_profile: MarginProfile
    created_ms: int
    legs: Dict[str, LegState] = field(default_factory=dict)
    status: EpisodeStatus = "pending_entry"
    opened_ms: int = 0
    closed_ms: int = 0
    close_reason: str = ""
    # Unlegged risk tracking (basis-free excursion against missing leg's ref mid)
    unlegged_since_ms: int = 0
    unlegged_missing_leg: Optional[str] = None
    unlegged_ref_mid: float = 0.0
    max_unlegged_adverse_bps: float = 0.0
    # Carry accounting fields (used by SpotPerpCarryRunner)
    open_qty: float = 0.0
    entry_spot_px: float = 0.0
    entry_perp_px: float = 0.0
    collateral_usd: float = 0.0
    funding_accrued_usd: float = 0.0
    total_fees_usd: float = 0.0
    deleverages_count: int = 0
    margin_params: Optional[CarryMarginParams] = None
