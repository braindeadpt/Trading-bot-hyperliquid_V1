"""
Swarm Backtest Engine - Testa múltiplas configurações da estratégia
Usa dados históricos da base de dados para avaliar performance.

Como usar:
    python src/swarm_backtest.py --days 30 --max-combinations 50
    python src/swarm_backtest.py --report  # Mostrar melhor resultado
"""
import sys
import os
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from itertools import product
import sqlite3
import multiprocessing as mp

sys.path.insert(0, str(Path(__file__).parent / "src"))

from database import BotDatabase

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@dataclass
class BacktestConfig:
    """Configuração para um backtest individual"""
    name: str
    timeframe: str
    leverage: int
    stop_loss_pct: float
    trailing_activation_pct: float
    trailing_stop_pct: float
    volume_threshold: float
    oi_threshold: float
    min_bullish: int
    min_bearish: int
    short_enabled: bool
    max_daily_trades: int
    days: int


@dataclass
class BacktestResult:
    """Resultado de um backtest"""
    config: BacktestConfig
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    profit_factor: float
    total_pnl: float
    max_drawdown: float
    avg_trade_duration: float
    long_trades: int
    short_trades: int
    long_pnl: float
    short_pnl: float
    sharpe_ratio: float
    exec_time_ms: float


def load_historical_data(db_path: str, asset: str, days: int) -> List[Dict]:
    """Carrega dados históricos da base de dados"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cutoff = int((datetime.now() - timedelta(days=days)).timestamp() * 1000)
    
    # Buscar candles
    cursor.execute('''
        SELECT timestamp, open, high, low, close, volume, 
               (SELECT oi FROM open_interest 
                WHERE ABS(timestamp - candles.timestamp) < 60000 
                ORDER BY ABS(timestamp - candles.timestamp) LIMIT 1) as oi,
               (SELECT change_pct FROM open_interest 
                WHERE ABS(timestamp - candles.timestamp) < 60000 
                ORDER BY ABS(timestamp - candles.timestamp) LIMIT 1) as oi_change,
               (SELECT funding_rate FROM funding_rates 
                WHERE ABS(timestamp - candles.timestamp) < 60000 
                ORDER BY ABS(timestamp - candles.timestamp) LIMIT 1) as funding
        FROM candles
        WHERE symbol = ? AND timestamp >= ?
        ORDER BY timestamp
    ''', (asset, cutoff))
    
    candles = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    logger.info(f"Carregados {len(candles)} candles de {days} dias")
    return candles


def simulate_trade(config: BacktestConfig, candles: List[Dict]) -> BacktestResult:
    """
    Simula trades com base na configuração e dados históricos.
    Versão simplificada do motor de trading para backtest rápido.
    """
    start_time = time.time()
    
    capital = 10000.0
    initial_capital = capital
    max_position_usd = 100
    fee_pct = 0.00035
    
    trades = []
    current_position = None
    entry_price = 0
    entry_time = None
    position_size = 0
    max_price = 0
    min_price = 0
    trailing_active = False
    trailing_stop = 0
    daily_trades = 0
    last_day = None
    
    bullish_count = 0
    bearish_count = 0
    volume_history = []
    
    # Preços para SMA
    prices = []
    
    for candle in candles:
        price = candle['close']
        prices.append(price)
        
        # Contadores
        if candle['close'] > candle['open']:
            bullish_count += 1
            bearish_count = 0
        elif candle['close'] < candle['open']:
            bearish_count += 1
            bullish_count = 0
        else:
            bullish_count = 0
            bearish_count = 0
        
        # Volume
        volume_history.append(candle['volume'])
        if len(volume_history) > 20:
            volume_history.pop(0)
        
        avg_volume = sum(volume_history) / max(1, len(volume_history))
        volume_ratio = candle['volume'] / max(avg_volume, 1)
        
        # SMA
        sma_period = 100
        if len(prices) < sma_period:
            continue
        sma = sum(prices[-sma_period:]) / sma_period
        price_above_sma = price > sma
        
        # Funding
        funding = candle.get('funding', 0) or 0
        funding_extreme = abs(funding) > 0.01
        
        # OI
        oi_change = candle.get('oi_change', 0) or 0
        oi_ok_long = oi_change >= config.oi_threshold
        oi_ok_short = oi_change <= -config.oi_threshold
        
        # Verificar saída
        if current_position:
            if current_position == 'long':
                gain_pct = (price - entry_price) / entry_price
                if price > max_price:
                    max_price = price
                
                # Trailing activation
                if gain_pct >= config.trailing_activation_pct and not trailing_active:
                    trailing_active = True
                    trailing_stop = max_price * (1 - config.trailing_stop_pct)
                
                if trailing_active:
                    new_stop = max_price * (1 - config.trailing_stop_pct)
                    if new_stop > trailing_stop:
                        trailing_stop = new_stop
                
                # Stop loss
                if not trailing_active:
                    loss_pct = (entry_price - price) / entry_price
                    if loss_pct >= config.stop_loss_pct:
                        # EXIT
                        pnl_pct = (price - entry_price) / entry_price
                        pnl = position_size * pnl_pct
                        capital += pnl - (position_size * fee_pct * 2)
                        trades.append({
                            'side': 'long',
                            'pnl': pnl,
                            'pnl_pct': pnl_pct,
                            'duration': 0,
                            'exit_reason': 'STOP_LOSS'
                        })
                        current_position = None
                        continue
                
                # Trailing stop
                if trailing_active and price <= trailing_stop:
                    pnl_pct = (price - entry_price) / entry_price
                    pnl = position_size * pnl_pct
                    capital += pnl - (position_size * fee_pct * 2)
                    trades.append({
                        'side': 'long',
                        'pnl': pnl,
                        'pnl_pct': pnl_pct,
                        'duration': 0,
                        'exit_reason': 'TRAILING_STOP'
                    })
                    current_position = None
                    continue
            
            elif current_position == 'short':
                gain_pct = (entry_price - price) / entry_price
                if price < min_price:
                    min_price = price
                
                if gain_pct >= config.trailing_activation_pct and not trailing_active:
                    trailing_active = True
                    trailing_stop = min_price * (1 + config.trailing_stop_pct)
                
                if trailing_active:
                    new_stop = min_price * (1 + config.trailing_stop_pct)
                    if new_stop < trailing_stop:
                        trailing_stop = new_stop
                
                if not trailing_active:
                    loss_pct = (price - entry_price) / entry_price
                    if loss_pct >= config.stop_loss_pct:
                        pnl_pct = (entry_price - price) / entry_price
                        pnl = position_size * pnl_pct
                        capital += pnl - (position_size * fee_pct * 2)
                        trades.append({
                            'side': 'short',
                            'pnl': pnl,
                            'pnl_pct': pnl_pct,
                            'duration': 0,
                            'exit_reason': 'STOP_LOSS'
                        })
                        current_position = None
                        continue
                
                if trailing_active and price >= trailing_stop:
                    pnl_pct = (entry_price - price) / entry_price
                    pnl = position_size * pnl_pct
                    capital += pnl - (position_size * fee_pct * 2)
                    trades.append({
                        'side': 'short',
                        'pnl': pnl,
                        'pnl_pct': pnl_pct,
                        'duration': 0,
                        'exit_reason': 'TRAILING_STOP'
                    })
                    current_position = None
                    continue
        
        # Verificar entrada
        if not current_position:
            # Cooldown simples (evita entradas no candle seguinte a saída)
            # Skip logic simplificada
            
            # LONG
            if price_above_sma and bullish_count >= config.min_bullish:
                if volume_ratio >= config.volume_threshold and not funding_extreme and oi_ok_long:
                    position_size = min(max_position_usd, capital * 0.1)
                    margin = position_size / config.leverage
                    fee = position_size * fee_pct * 2
                    
                    if margin + fee <= capital:
                        current_position = 'long'
                        entry_price = price
                        entry_time = candle['timestamp']
                        position_size = position_size
                        max_price = price
                        min_price = price
                        trailing_active = False
                        trailing_stop = 0
                        daily_trades += 1
            
            # SHORT
            elif config.short_enabled and not price_above_sma and bearish_count >= config.min_bearish:
                if volume_ratio >= config.volume_threshold and not funding_extreme and oi_ok_short:
                    position_size = min(max_position_usd, capital * 0.1)
                    margin = position_size / config.leverage
                    fee = position_size * fee_pct * 2
                    
                    if margin + fee <= capital:
                        current_position = 'short'
                        entry_price = price
                        entry_time = candle['timestamp']
                        position_size = position_size
                        max_price = price
                        min_price = price
                        trailing_active = False
                        trailing_stop = 0
                        daily_trades += 1
    
    # Calcular métricas
    exec_time = (time.time() - start_time) * 1000
    
    if not trades:
        return BacktestResult(
            config=config,
            total_trades=0, winning_trades=0, losing_trades=0,
            win_rate=0, profit_factor=0, total_pnl=0,
            max_drawdown=0, avg_trade_duration=0,
            long_trades=0, short_trades=0,
            long_pnl=0, short_pnl=0, sharpe_ratio=0,
            exec_time_ms=exec_time
        )
    
    wins = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]
    
    total_pnl = sum(t['pnl'] for t in trades)
    win_rate = len(wins) / len(trades) * 100
    
    total_wins = sum(t['pnl'] for t in wins)
    total_losses = abs(sum(t['pnl'] for t in losses))
    profit_factor = total_wins / total_losses if total_losses > 0 else float('inf')
    
    longs = [t for t in trades if t['side'] == 'long']
    shorts = [t for t in trades if t['side'] == 'short']
    
    # Drawdown
    peak = initial_capital
    max_dd = 0
    running = initial_capital
    for t in trades:
        running += t['pnl']
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd
    
    # Sharpe (simplificado)
    returns = [t['pnl_pct'] for t in trades]
    avg_ret = sum(returns) / len(returns)
    variance = sum((r - avg_ret) ** 2 for r in returns) / len(returns)
    std_dev = variance ** 0.5
    sharpe = (avg_ret / std_dev) * (252 ** 0.5) if std_dev > 0 else 0
    
    return BacktestResult(
        config=config,
        total_trades=len(trades),
        winning_trades=len(wins),
        losing_trades=len(losses),
        win_rate=win_rate,
        profit_factor=profit_factor,
        total_pnl=total_pnl,
        max_drawdown=max_dd,
        avg_trade_duration=0,
        long_trades=len(longs),
        short_trades=len(shorts),
        long_pnl=sum(t['pnl'] for t in longs),
        short_pnl=sum(t['pnl'] for t in shorts),
        sharpe_ratio=sharpe,
        exec_time_ms=exec_time
    )


def generate_config_combinations(days: int = 30, max_combinations: int = 50) -> List[BacktestConfig]:
    """Gera combinações de parâmetros para testar"""
    
    # Grid de parâmetros (focado em melhorar os problemas identificados)
    timeframes = ['15m']  # Focar no timeframe vencedor
    leverages = [2, 3, 5]
    stop_losses = [0.02, 0.035, 0.05]
    trailing_activations = [0.015, 0.02, 0.03]
    trailing_stops = [0.015, 0.02, 0.03]
    volume_thresholds = [2.0, 3.0, 4.0]
    oi_thresholds = [0.005, 0.01, 0.015]
    min_candles = [(2, 2)]  # Igual para evitar bias
    
    configs = []
    count = 0
    
    for (tf, lev, sl, ta, ts, vt, ot, (mb, mbr)) in product(
        timeframes, leverages, stop_losses, trailing_activations,
        trailing_stops, volume_thresholds, oi_thresholds, min_candles
    ):
        if count >= max_combinations:
            break
        
        name = f"swarm_{tf}_L{lev}_SL{int(sl*100)}_TA{int(ta*100)}_VT{int(vt)}_OI{int(ot*1000)}"
        
        configs.append(BacktestConfig(
            name=name,
            timeframe=tf,
            leverage=lev,
            stop_loss_pct=sl,
            trailing_activation_pct=ta,
            trailing_stop_pct=ts,
            volume_threshold=vt,
            oi_threshold=ot,
            min_bullish=mb,
            min_bearish=mbr,
            short_enabled=True,
            max_daily_trades=5,
            days=days
        ))
        count += 1
    
    logger.info(f"Geradas {len(configs)} combinações para testar")
    return configs


def run_swarm(asset: str = 'BTC', days: int = 30, max_combinations: int = 50) -> List[BacktestResult]:
    """Executa o swarm de backtests"""
    
    print("\n" + "=" * 70)
    print("  🐝 SWARM BACKTEST ENGINE v1.0")
    print("=" * 70)
    print(f"  Asset: {asset}")
    print(f"  Dias: {days}")
    print(f"  Max Combinations: {max_combinations}")
    print("=" * 70 + "\n")
    
    # Carregar dados
    db_path = str(Path(__file__).parent / "data" / "hyperliquid.db")
    if not os.path.exists(db_path):
        db_path = str(Path(__file__).parent.parent / "data" / "hyperliquid.db")
    
    candles = load_historical_data(db_path, asset, days)
    if not candles:
        print("❌ Sem dados históricos suficientes!")
        return []
    
    # Gerar configs
    configs = generate_config_combinations(days, max_combinations)
    
    # Executar backtests
    results = []
    total = len(configs)
    
    print(f"⚡ A executar {total} backtests...\n")
    
    for i, config in enumerate(configs, 1):
        print(f"[{i}/{total}] {config.name} ... ", end="", flush=True)
        
        result = simulate_trade(config, candles)
        results.append(result)
        
        status = "✅" if result.profit_factor > 1.3 and result.win_rate > 40 else "❌"
        print(f"{status} WR:{result.win_rate:.1f}% PF:{result.profit_factor:.2f} PnL:${result.total_pnl:+.2f} Trades:{result.total_trades}")
    
    return results


def print_results(results: List[BacktestResult], top_n: int = 10):
    """Imprime os melhores resultados"""
    
    if not results:
        print("\n❌ Sem resultados para mostrar")
        return
    
    # Ordenar por profit factor, depois win rate
    sorted_results = sorted(
        results,
        key=lambda r: (r.profit_factor, r.win_rate),
        reverse=True
    )
    
    print("\n" + "=" * 100)
    print("  🏆 TOP RESULTADOS (ordenados por Profit Factor)")
    print("=" * 100)
    print(f"  {'Rank':<5} {'Config':<35} {'Trades':<7} {'WR%':<6} {'PF':<6} {'PnL':<10} {'DD':<8} {'Sharpe':<7}")
    print("-" * 100)
    
    for i, r in enumerate(sorted_results[:top_n], 1):
        pnl_str = f"${r.total_pnl:+.2f}"
        print(f"  {i:<5} {r.config.name:<35} {r.total_trades:<7} {r.win_rate:<6.1f} {r.profit_factor:<6.2f} {pnl_str:<10} ${r.max_drawdown:<7.2f} {r.sharpe_ratio:<7.2f}")
    
    print("=" * 100)
    
    # Best overall
    best = sorted_results[0]
    print(f"\n🥇 MELHOR CONFIGURAÇÃO:")
    print(f"  Nome: {best.config.name}")
    print(f"  Trades: {best.total_trades} | Wins: {best.winning_trades} | Losses: {best.losing_trades}")
    print(f"  Win Rate: {best.win_rate:.1f}%")
    print(f"  Profit Factor: {best.profit_factor:.2f}")
    print(f"  Total PnL: ${best.total_pnl:+.2f}")
    print(f"  Max Drawdown: ${best.max_drawdown:.2f}")
    print(f"  Sharpe: {best.sharpe_ratio:.2f}")
    print(f"\n  Parâmetros:")
    print(f"    Leverage: {best.config.leverage}x")
    print(f"    Stop Loss: {best.config.stop_loss_pct*100:.1f}%")
    print(f"    Trailing Activation: {best.config.trailing_activation_pct*100:.1f}%")
    print(f"    Trailing Stop: {best.config.trailing_stop_pct*100:.1f}%")
    print(f"    Volume Threshold: {best.config.volume_threshold}x")
    print(f"    OI Threshold: {best.config.oi_threshold*100:.1f}%")
    
    # Guardar melhor config
    save_best_config(best)


def save_best_config(result: BacktestResult):
    """Guarda a melhor configuração num ficheiro"""
    output = {
        'name': result.config.name,
        'timestamp': datetime.now().isoformat(),
        'metrics': {
            'win_rate': result.win_rate,
            'profit_factor': result.profit_factor,
            'total_pnl': result.total_pnl,
            'max_drawdown': result.max_drawdown,
            'sharpe_ratio': result.sharpe_ratio,
            'total_trades': result.total_trades
        },
        'config': {
            'timeframe': result.config.timeframe,
            'leverage': result.config.leverage,
            'stop_loss_pct': result.config.stop_loss_pct,
            'trailing_activation_pct': result.config.trailing_activation_pct,
            'trailing_stop_pct': result.config.trailing_stop_pct,
            'volume_threshold': result.config.volume_threshold,
            'oi_threshold': result.config.oi_threshold,
            'min_bullish': result.config.min_bullish,
            'min_bearish': result.config.min_bearish,
            'short_enabled': result.config.short_enabled,
            'max_daily_trades': result.config.max_daily_trades
        }
    }
    
    path = Path(__file__).parent.parent / "config" / "best_swarm_config.json"
    path.parent.mkdir(exist_ok=True)
    
    with open(path, 'w') as f:
        json.dump(output, f, indent=2)
    
    print(f"\n💾 Melhor config guardada em: {path}")


def load_best_config() -> Optional[Dict]:
    """Carrega a melhor configuração guardada"""
    path = Path(__file__).parent.parent / "config" / "best_swarm_config.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return None


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='Swarm Backtest Engine')
    parser.add_argument('--days', type=int, default=30, help='Dias de dados históricos')
    parser.add_argument('--max-combinations', type=int, default=50, help='Máximo de combinações')
    parser.add_argument('--asset', type=str, default='BTC', help='Asset')
    parser.add_argument('--report', action='store_true', help='Mostrar melhor config guardada')
    
    args = parser.parse_args()
    
    if args.report:
        config = load_best_config()
        if config:
            print("\n" + "=" * 60)
            print("  🏆 MELHOR CONFIG (guardada)")
            print("=" * 60)
            print(json.dumps(config, indent=2))
            print("=" * 60 + "\n")
        else:
            print("❌ Nenhuma config guardada. Corre o swarm primeiro!")
        return
    
    results = run_swarm(args.asset, args.days, args.max_combinations)
    print_results(results)


if __name__ == "__main__":
    main()
