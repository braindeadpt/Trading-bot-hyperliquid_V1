"""PerpPairRunner — beta-hedged Perp-Perp pairs (UnlockShort & ResMom) on Hyperliquid.

Supports:
  - ``UnlockShort`` (``docs/ops/PREREGISTER_UNLOCK_SHADOW.md``):
      Short token perp ``$N`` + Long ``BTC`` perp ``$(clamp(beta, 0.5, 2.0) * N)``.
  - ``ResMom`` (``notes/resmom-forward-prereg-2026-10-28.md``):
      Beta-neutral long/short perpetual basket entry & exit via ``MultiLegCoordinator``.
"""

from __future__ import annotations

from typing import Dict, Mapping, Optional

from src.exchanges.hyperliquid_live import normalize_size
from src.multileg.coordinator import MultiLegCoordinator
from src.multileg.types import LegSpec, MultiLegEpisode, TopOfBook

BETA_MIN: float = 0.5
BETA_MAX: float = 2.0


def clamp_beta(beta_30d: float) -> float:
    """Clamp 30d token-vs-BTC beta to the preregistered ``[0.5, 2.0]`` band."""
    return min(BETA_MAX, max(BETA_MIN, float(beta_30d)))


class PerpPairRunner:
    """High-level adapter for beta-hedged perp pairs and baskets on Hyperliquid."""

    def __init__(self, coordinator: MultiLegCoordinator) -> None:
        self._coord = coordinator

    async def open_unlock_short_pair(
        self,
        *,
        episode_id: str,
        token_symbol: str,
        token_notional_usd: float,
        beta_30d: float,
        token_book: TopOfBook,
        btc_book: TopOfBook,
        leverage: float = 3.0,
        now_ms: Optional[int] = None,
    ) -> MultiLegEpisode:
        """Submit atomic Short Token + Long BTC beta-hedged pair."""
        if token_symbol.upper() == "BTC":
            raise ValueError("UnlockShort token_symbol cannot be BTC")
        if token_book.mid <= 0 or btc_book.mid <= 0:
            raise ValueError("Valid token and BTC TopOfBook quotes are required")

        eff_beta = clamp_beta(beta_30d)
        btc_notional_usd = float(token_notional_usd) * eff_beta
        total_gross_usd = float(token_notional_usd) + btc_notional_usd
        required_margin = total_gross_usd / max(1.0, float(leverage))

        await self._coord.ensure_margin_ready(
            spot_notional_usd=0.0,
            perp_collateral_usd=required_margin,
        )

        token_qty = normalize_size(token_symbol, float(token_notional_usd) / token_book.mid)
        btc_qty = normalize_size("BTC", btc_notional_usd / btc_book.mid)
        if token_qty <= 0 or btc_qty <= 0:
            raise ValueError(
                f"Beta-hedged pair rounded to zero size: {token_symbol}={token_qty}, BTC={btc_qty}"
            )

        legs = [
            LegSpec(
                leg_id="short_alt",
                symbol=token_symbol,
                market_kind="perp",
                side="sell",
                target_size=token_qty,
                limit_price=token_book.ask,
            ),
            LegSpec(
                leg_id="long_btc",
                symbol="BTC",
                market_kind="perp",
                side="buy",
                target_size=btc_qty,
                limit_price=btc_book.bid,
            ),
        ]
        return await self._coord.submit_entry(
            episode_id=episode_id,
            strategy="UnlockShort",
            legs=legs,
            books={token_symbol: token_book, "BTC": btc_book},
            now_ms=now_ms,
        )

    async def open_perp_basket(
        self,
        *,
        episode_id: str,
        strategy: str,
        signed_notionals_usd: Mapping[str, float],
        books: Dict[str, TopOfBook],
        leverage: float = 2.0,
        now_ms: Optional[int] = None,
    ) -> MultiLegEpisode:
        """Submit an N-leg perpetual basket (e.g. weekly ResMom long/short rebalance)."""
        gross_usd = sum(abs(float(v)) for v in signed_notionals_usd.values() if abs(float(v)) > 0)
        await self._coord.ensure_margin_ready(
            spot_notional_usd=0.0,
            perp_collateral_usd=gross_usd / max(1.0, float(leverage)),
        )
        legs = []
        for sym, signed_usd in signed_notionals_usd.items():
            val = float(signed_usd)
            if abs(val) < 1e-6:
                continue
            book = books.get(sym)
            if book is None or book.mid <= 0:
                raise ValueError(f"Missing TopOfBook for basket symbol {sym}")
            side = "buy" if val > 0 else "sell"
            qty = normalize_size(sym, abs(val) / book.mid)
            if qty <= 0:
                raise ValueError(f"Basket leg {sym} rounded to 0 size")
            legs.append(
                LegSpec(
                    leg_id=f"leg_{sym.lower()}",
                    symbol=sym,
                    market_kind="perp",
                    side=side,  # type: ignore[arg-type]
                    target_size=qty,
                    limit_price=book.bid if side == "buy" else book.ask,
                )
            )
        return await self._coord.submit_entry(
            episode_id=episode_id,
            strategy=strategy,
            legs=legs,
            books=books,
            now_ms=now_ms,
        )
