"""Hypothetical trade math (§4) — pure functions, all inputs injected.

Short the token T-5 00:00 UTC -> T+2 00:00 UTC, $10k notional; long BTC
hedge leg sized beta_30d x notional (clamped [0.5, 2]). Entry/exit prices
are the daily-candle open at each 00:00 UTC boundary (the deterministic
"mid at 00:00" — candleSnapshot serves >=2h intervals so the daily open
is the replayable boundary mark), adjusted by the per-leg L2 walk slip
estimate. Fees 4.5 bps per side per leg; funding is the real summed HL
hourly rate over the window, signed by leg.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from src.research.unlock_shadow import spec


def daily_returns(closes: Sequence[float]) -> List[float]:
    return [closes[i] / closes[i - 1] - 1.0
            for i in range(1, len(closes)) if closes[i - 1] > 0]


def beta_30d(tok_rets: Sequence[float], btc_rets: Sequence[float]
             ) -> Optional[float]:
    """OLS beta of token daily returns on BTC daily returns over the
    paired tail (<= BETA_WINDOW_D). None when uncomputable."""
    n = min(len(tok_rets), len(btc_rets), spec.BETA_WINDOW_D)
    if n < 3:
        return None
    tr, br = list(tok_rets)[-n:], list(btc_rets)[-n:]
    mean_b = sum(br) / n
    var_b = sum((r - mean_b) ** 2 for r in br) / n
    if var_b <= 0:
        return None
    mean_t = sum(tr) / n
    cov = sum((a - mean_t) * (b - mean_b) for a, b in zip(tr, br)) / n
    return cov / var_b


def clamp_beta(b: float) -> float:
    return min(spec.BETA_MAX, max(spec.BETA_MIN, b))


def walk_book_slip_bps(side: str, notional_usd: float,
                       levels: Optional[Tuple[list, list]],
                       mid: float) -> Optional[float]:
    """VWAP slip vs mid for a market order of `notional_usd`, walking the
    book. side='sell' consumes bids, 'buy' consumes asks. HL l2Book rows
    are {px, sz, n}. None when the book can't cover the size (the caller
    records slip_unverified rather than inventing a number)."""
    if not levels or mid <= 0:
        return None
    book = levels[0] if side == "sell" else levels[1]
    if not book:
        return None
    need = notional_usd
    cost = 0.0
    qty = 0.0
    for lv in book:
        px = float(lv["px"])
        sz = float(lv["sz"])
        take = min(need, px * sz)
        cost += take
        qty += take / px
        need -= take
        if need <= 0:
            break
    if need > 0 or qty <= 0:
        return None
    vwap = cost / qty
    return abs(vwap - mid) / mid * 1e4


def funding_pnl(side: str, rates: Sequence[float], notional: float
                ) -> float:
    """Signed funding PnL: r>0 = long pays short. Long leg: -sum(r)*N;
    short leg: +sum(r)*N."""
    total = sum(rates) * notional
    return -total if side == "long" else total


@dataclass(frozen=True)
class WindowPrices:
    tok_mid_in: float
    tok_mid_out: float
    btc_mid_in: float
    btc_mid_out: float
    tok_slip_in_bps: float = 0.0
    tok_slip_out_bps: float = 0.0
    btc_slip_in_bps: float = 0.0
    btc_slip_out_bps: float = 0.0


@dataclass(frozen=True)
class SimResult:
    notional_usd: float
    beta: float
    hedge_notional_usd: float
    gross_tok_usd: float
    gross_btc_usd: float
    fees_usd: float
    funding_tok_usd: float
    funding_btc_usd: float
    net_usd: float
    net_bps: float            # vs token notional (the position basis)
    review_flag: bool         # net_bps < REVIEW_NET_BPS (manual review)


def evaluate_window(prices: WindowPrices, beta: float,
                    tok_funding_rates: Sequence[float],
                    btc_funding_rates: Sequence[float],
                    notional: float = spec.NOTIONAL_USD) -> SimResult:
    """Full 7-day-window accounting, both legs, all costs (§4)."""
    beta_c = clamp_beta(beta)
    n_tok = notional
    n_btc = beta_c * notional

    # effective taker prices — every slip is charged AGAINST the trade
    tok_in = prices.tok_mid_in * (1.0 - prices.tok_slip_in_bps / 1e4)   # sell
    tok_out = prices.tok_mid_out * (1.0 + prices.tok_slip_out_bps / 1e4)  # buyback
    btc_in = prices.btc_mid_in * (1.0 + prices.btc_slip_in_bps / 1e4)   # buy
    btc_out = prices.btc_mid_out * (1.0 - prices.btc_slip_out_bps / 1e4)  # sell

    gross_tok = (n_tok / tok_in) * (tok_in - tok_out)
    gross_btc = (n_btc / btc_in) * (btc_out - btc_in)
    fees = (spec.FEE_BPS_PER_SIDE / 1e4) * 2.0 * (n_tok + n_btc)
    fund_tok = funding_pnl("short", tok_funding_rates, n_tok)
    fund_btc = funding_pnl("long", btc_funding_rates, n_btc)

    net = gross_tok + gross_btc + fund_tok + fund_btc - fees
    net_bps = net / n_tok * 1e4
    return SimResult(
        notional_usd=n_tok, beta=beta_c, hedge_notional_usd=n_btc,
        gross_tok_usd=gross_tok, gross_btc_usd=gross_btc,
        fees_usd=fees, funding_tok_usd=fund_tok, funding_btc_usd=fund_btc,
        net_usd=net, net_bps=net_bps,
        review_flag=net_bps < spec.REVIEW_NET_BPS)
