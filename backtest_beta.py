"""
Backtest: BTC-Alt Beta Arbitrage Strategy

Hypothesis: Altcoins have asymmetric beta to BTC.
When BTC rises, alts rise MORE (beta > 1).
When BTC dumps, alts dump MORE (beta > 1 downward).

Strategy: Detect when the BTC-alt relationship is "broken" (alt lagging BTC
or leading BTC excessively) and trade the mean-reversion of that spread.

Uses Binance API for free historical data.
"""

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import aiohttp
import numpy as np


# ── Data fetcher ──

BINANCE_API = "https://api.binance.com/api/v3/klines"

async def fetch_candles(
    session: aiohttp.ClientSession,
    symbol: str,
    interval: str = "1h",
    days: int = 90,
) -> List[Dict]:
    """Fetch klines from Binance. Returns list of {open, high, low, close, volume, time}."""
    end_time = int(datetime.now().timestamp() * 1000)
    start_time = end_time - int(timedelta(days=days).total_seconds() * 1000)
    
    params = {
        "symbol": symbol,
        "interval": interval,
        "startTime": start_time,
        "endTime": end_time,
        "limit": 1000,
    }
    
    async with session.get(BINANCE_API, params=params) as resp:
        data = await resp.json()
        if isinstance(data, dict) and "code" in data:
            raise RuntimeError(f"Binance error: {data}")
    
    candles = []
    for row in data:
        candles.append({
            "time": row[0],           # Open time ms
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
        })
    return candles


# ── Strategy ──

@dataclass
class BetaParams:
    lookback: int = 20           # Candles for beta calc
    entry_zscore: float = 2.0    # Z-score threshold to enter
    exit_zscore: float = 0.5     # Z-score threshold to exit
    max_hold_candles: int = 5    # Time stop (max candles to hold)
    stop_loss_pct: float = 5.0   # Hard stop
    take_profit_pct: float = 8.0 # Hard TP
    max_positions: int = 3
    position_size_pct: float = 10.0  # % of capital per trade


class BetaArbitrageStrategy:
    """
    For each altcoin:
      1. Compute rolling beta vs BTC (lookback windows)
      2. Compute "expected alt return" = beta * btc_return
      3. Compute "residual" = actual_alt_return - expected_alt_return
      4. Z-score the residual over lookback window
      5. Entry: |zscore| > entry_zscore
         - If residual > 0: alt overperforming → SHORT alt (mean reversion)
         - If residual < 0: alt underperforming → LONG alt (catch-up)
      6. Exit: |zscore| < exit_zscore, or SL/TP hit, or max_hold_candles
    """
    
    def __init__(self, params: BetaParams = None):
        self.p = params or BetaParams()
    
    def compute_beta(
        self,
        btc_returns: np.ndarray,
        alt_returns: np.ndarray,
    ) -> float:
        """Rolling beta = cov(alt, btc) / var(btc)."""
        if len(btc_returns) < 2 or np.var(btc_returns) < 1e-12:
            return 1.0
        beta = np.cov(alt_returns, btc_returns)[0, 1] / np.var(btc_returns)
        return float(beta)
    
    def generate_signals(
        self,
        btc_candles: List[Dict],
        alt_candles: List[Dict],
    ) -> List[Dict]:
        """
        Returns list of signal events:
          {time, symbol, side, zscore, beta, residual, btc_return, alt_return}
        """
        # Align candles by time
        btc_by_time = {c["time"]: c for c in btc_candles}
        alt_by_time = {c["time"]: c for c in alt_candles}
        common_times = sorted(set(btc_by_time.keys()) & set(alt_by_time.keys()))
        
        if len(common_times) < self.p.lookback + 5:
            return []
        
        # Compute returns
        btc_returns = []
        alt_returns = []
        times = []
        for i in range(1, len(common_times)):
            t = common_times[i]
            prev_t = common_times[i - 1]
            btc_ret = (btc_by_time[t]["close"] - btc_by_time[prev_t]["close"]) / btc_by_time[prev_t]["close"]
            alt_ret = (alt_by_time[t]["close"] - alt_by_time[prev_t]["close"]) / alt_by_time[prev_t]["close"]
            btc_returns.append(btc_ret)
            alt_returns.append(alt_ret)
            times.append(t)
        
        btc_returns = np.array(btc_returns)
        alt_returns = np.array(alt_returns)
        
        signals = []
        for i in range(self.p.lookback, len(btc_returns)):
            window_btc = btc_returns[i - self.p.lookback:i]
            window_alt = alt_returns[i - self.p.lookback:i]
            
            beta = self.compute_beta(window_btc, window_alt)
            
            # Current candle
            current_btc_ret = btc_returns[i]
            current_alt_ret = alt_returns[i]
            
            # Expected alt return given BTC move
            expected_alt_ret = beta * current_btc_ret
            
            # Residual = actual - expected
            residual = current_alt_ret - expected_alt_ret
            
            # Z-score of residual over lookback
            historical_residuals = window_alt - beta * window_btc
            mean_resid = np.mean(historical_residuals)
            std_resid = np.std(historical_residuals)
            
            if std_resid < 1e-12:
                zscore = 0.0
            else:
                zscore = (residual - mean_resid) / std_resid
            
            # Signal logic
            side = None
            if zscore > self.p.entry_zscore:
                side = "short"  # Alt overperformed → mean reversion down
            elif zscore < -self.p.entry_zscore:
                side = "long"   # Alt underperformed → catch-up
            
            if side:
                signals.append({
                    "time": times[i],
                    "time_str": datetime.fromtimestamp(times[i] / 1000).strftime("%Y-%m-%d %H:%M"),
                    "symbol": alt_candles[0].get("symbol", "ALT"),
                    "side": side,
                    "zscore": round(zscore, 3),
                    "beta": round(beta, 3),
                    "residual": round(residual * 100, 3),  # %
                    "btc_return": round(current_btc_ret * 100, 3),
                    "alt_return": round(current_alt_ret * 100, 3),
                    "btc_price": btc_by_time[times[i]]["close"],
                    "alt_price": alt_by_time[times[i]]["close"],
                })
        
        return signals


# ── Backtest runner ──

def simulate_trade(
    signal: Dict,
    future_prices: List[float],
    side: str,
    stop_loss_pct: float,
    take_profit_pct: float,
    exit_zscore_func,  # Function to compute current zscore
    max_hold: int,
) -> Dict:
    """
    Simulate a trade from entry to exit.
    Returns trade result dict.
    """
    entry_price = signal["alt_price"]
    side_mult = 1.0 if side == "long" else -1.0
    
    # Check each future candle
    for i, price in enumerate(future_prices[:max_hold]):
        pnl_pct = (price - entry_price) / entry_price * 100 * side_mult
        
        # Stop loss
        if pnl_pct <= -stop_loss_pct:
            return {
                "exit_reason": "stop_loss",
                "hold_candles": i + 1,
                "pnl_pct": round(pnl_pct, 3),
                "exit_price": price,
            }
        
        # Take profit
        if pnl_pct >= take_profit_pct:
            return {
                "exit_reason": "take_profit",
                "hold_candles": i + 1,
                "pnl_pct": round(pnl_pct, 3),
                "exit_price": price,
            }
    
    # Time stop — exit at last available price
    final_price = future_prices[-1] if future_prices else entry_price
    pnl_pct = (final_price - entry_price) / entry_price * 100 * side_mult
    return {
        "exit_reason": "time_stop",
        "hold_candles": len(future_prices),
        "pnl_pct": round(pnl_pct, 3),
        "exit_price": final_price,
    }


async def run_backtest(
    symbols: List[str],
    days: int = 60,
    interval: str = "1h",
    capital: float = 10000.0,
) -> Dict:
    """
    Run beta-arbitrage backtest for BTC vs each alt.
    Returns summary stats.
    """
    async with aiohttp.ClientSession() as session:
        print(f"[Backtest] Fetching {interval} candles for last {days} days...")
        
        # Fetch BTC
        btc_candles = await fetch_candles(session, "BTCUSDT", interval, days)
        print(f"  BTC: {len(btc_candles)} candles")
        
        # Fetch alts
        alt_data = {}
        for sym in symbols:
            try:
                candles = await fetch_candles(session, sym, interval, days)
                alt_data[sym] = candles
                print(f"  {sym}: {len(candles)} candles")
            except Exception as e:
                print(f"  {sym}: FAILED — {e}")
        
        # Build price lookup for forward simulation
        btc_by_time = {c["time"]: c for c in btc_candles}
        
        # Run strategy per alt
        strategy = BetaArbitrageStrategy()
        all_trades = []
        
        for sym, candles in alt_data.items():
            for c in candles:
                c["symbol"] = sym
            
            signals = strategy.generate_signals(btc_candles, candles)
            print(f"  {sym}: {len(signals)} signals generated")
            
            # Simulate each signal
            alt_by_time = {c["time"]: c for c in candles}
            common_times = sorted(set(btc_by_time.keys()) & set(alt_by_time.keys()))
            
            for sig in signals:
                sig_time = sig["time"]
                # Find index of signal time in common_times
                try:
                    idx = common_times.index(sig_time)
                except ValueError:
                    continue
                
                # Get next N prices for forward simulation
                future_times = common_times[idx + 1:idx + 1 + strategy.p.max_hold_candles]
                future_prices = [alt_by_time[t]["close"] for t in future_times]
                
                if not future_prices:
                    continue
                
                result = simulate_trade(
                    sig, future_prices, sig["side"],
                    strategy.p.stop_loss_pct,
                    strategy.p.take_profit_pct,
                    None,  # zscore func not used in simplified sim
                    strategy.p.max_hold_candles,
                )
                
                trade = {
                    **sig,
                    **result,
                }
                all_trades.append(trade)
        
        # Summary stats
        total_trades = len(all_trades)
        wins = sum(1 for t in all_trades if t["pnl_pct"] > 0)
        losses = sum(1 for t in all_trades if t["pnl_pct"] <= 0)
        win_rate = wins / total_trades * 100 if total_trades > 0 else 0
        total_pnl = sum(t["pnl_pct"] for t in all_trades)
        avg_pnl = total_pnl / total_trades if total_trades > 0 else 0
        
        # Cumulative equity curve
        equity = [capital]
        for t in all_trades:
            pnl_amount = equity[-1] * (t["pnl_pct"] / 100) * (strategy.p.position_size_pct / 100)
            equity.append(equity[-1] + pnl_amount)
        
        max_drawdown = 0.0
        peak = capital
        for eq in equity:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak * 100
            if dd > max_drawdown:
                max_drawdown = dd
        
        return {
            "symbols_tested": list(alt_data.keys()),
            "total_signals": total_trades,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 2),
            "total_pnl_pct": round(total_pnl, 3),
            "avg_pnl_per_trade": round(avg_pnl, 3),
            "final_capital": round(equity[-1], 2) if equity else capital,
            "max_drawdown_pct": round(max_drawdown, 2),
            "trades": all_trades,
        }


# ── Main ──

async def main():
    print("=" * 60)
    print("BTC-Alt Beta Arbitrage Backtest")
    print("=" * 60)
    print()
    
    symbols = [
        "ETHUSDT",
        "SOLUSDT",
        "XRPUSDT",
        "DOGEUSDT",
        "ADAUSDT",
        "AVAXUSDT",
        "LINKUSDT",
    ]
    
    results = await run_backtest(symbols, days=60, interval="1h")
    
    print()
    print("=" * 60)
    print("RESULTS SUMMARY")
    print("=" * 60)
    print(f"Symbols tested:     {', '.join(results['symbols_tested'])}")
    print(f"Total trades:       {results['total_signals']}")
    print(f"Win rate:           {results['win_rate']}%")
    print(f"Wins / Losses:      {results['wins']} / {results['losses']}")
    print(f"Total PnL:          {results['total_pnl_pct']}%")
    print(f"Avg per trade:      {results['avg_pnl_per_trade']}%")
    print(f"Final capital:      ${results['final_capital']:.2f}")
    print(f"Max drawdown:       {results['max_drawdown_pct']}%")
    print()
    
    # Show top 10 winning trades
    if results['trades']:
        print("Top 10 WINNING trades by PnL:")
        sorted_trades = sorted(results['trades'], key=lambda x: x['pnl_pct'], reverse=True)
        for t in sorted_trades[:10]:
            print(f"  {t['time_str']} | {t['symbol']} | {t['side'].upper()} | "
                  f"z={t['zscore']:.2f} | beta={t['beta']:.2f} | "
                  f"PnL={t['pnl_pct']:.2f}% | {t['exit_reason']}")
        
        print()
        print("Top 10 LOSING trades by PnL:")
        sorted_trades = sorted(results['trades'], key=lambda x: x['pnl_pct'])
        for t in sorted_trades[:10]:
            print(f"  {t['time_str']} | {t['symbol']} | {t['side'].upper()} | "
                  f"z={t['zscore']:.2f} | beta={t['beta']:.2f} | "
                  f"PnL={t['pnl_pct']:.2f}% | {t['exit_reason']}")
    
    # Save results
    with open("backtest_results.json", "w") as f:
        json.dump(results, f, indent=2, default=str)
    print()
    print("Results saved to backtest_results.json")


if __name__ == "__main__":
    asyncio.run(main())
