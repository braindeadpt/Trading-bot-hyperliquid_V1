"""
Strategy Optimizer v2 - Grid Search massivo multi-timeframe
Testa milhares de combinações em 1m, 5m, 15m com confirmações variáveis
"""
import logging
import json
import itertools
from datetime import datetime
from pathlib import Path
from collections import deque
from typing import Dict, List, Tuple, Optional
from database import BotDatabase

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class StrategyOptimizer:
    """
    Optimizer multi-timeframe que faz grid search de parâmetros
    Testa todas as combinações e rankea por profit factor
    """
    
    def __init__(self, db: BotDatabase):
        self.db = db
        
        # Grid ultra-reduzido para ser rápido (testar ~150 combinações por timeframe)
        self.param_grid = {
            'volume_threshold': [2.5, 3.0, 4.0],
            'oi_threshold': [0.01, 0.02],
            'price_sma_period': [60, 100],
            'min_bullish_candles': [1, 2, 3],
            'min_bearish_candles': [1, 2, 3],
            'trailing_activation_pct': [0.015],
            'trailing_stop_pct': [0.01, 0.015],
            'stop_loss_pct': [0.02],
        }
        
        # Timeframes a testar (4 timeframes, 30 dias de dados OI real)
        self.intervals = ['5m', '15m', '30m', '1h', '4h']
        
        # Filtros para eliminar combinações inválidas
        self.min_trades = 5
        self.min_pf = 1.3
        self.max_pf = 10.0
        
        # Resultados por timeframe
        self.results_by_interval = {}
    
    def _load_data(self, symbol: str, interval: str) -> List[Dict]:
        """Carrega dados da DB para um timeframe específico"""
        candles = self.db.get_candles_for_backtest(symbol, interval, days=30)
        
        if not candles:
            logger.warning(f"Sem dados em DB para {symbol} {interval}")
            return []
        
        logger.info(f"Dados carregados: {len(candles)} candles de {symbol} {interval}")
        return candles
    
    def _calculate_sma(self, prices: list, period: int) -> float:
        if len(prices) < period:
            return sum(prices) / max(1, len(prices))
        return sum(prices[-period:]) / period
    
    def _run_backtest(self, data: List[Dict], params: Dict, interval: str) -> Dict:
        """
        Corre um backtest com parâmetros específicos e timeframe
        Retorna métricas incluindo LONGS e SHORTS separados
        """
        capital = 10000.0
        max_position = 100
        fee_pct = 0.00035
        
        current_position = None
        entry_price = 0
        position_size = 0
        max_price = 0
        min_price = 0
        trailing_active = False
        trailing_stop = 0
        
        bullish_count = 0
        bearish_count = 0
        
        longs = []
        shorts = []
        equity_curve = [(data[0]['timestamp'], capital)]
        
        prices = []
        volumes = []
        oi_history = []  # Histórico de OI para calcular mudança
        
        for i, candle in enumerate(data):
            price = candle['close']
            volume = candle['volume']
            oi = candle.get('oi', 0)
            funding = candle.get('funding_rate', 0)
            timestamp = candle['timestamp']
            
            prices.append(price)
            volumes.append(volume)
            oi_history.append(oi)
            
            # Volume médio (lookback 100)
            if len(volumes) >= 100:
                avg_volume = sum(volumes[-100:]) / 100
                volume_ratio = volume / max(avg_volume, 1)
            else:
                volume_ratio = 1.0
            
            # SMA
            sma = self._calculate_sma(prices, params['price_sma_period'])
            price_above_sma = price > sma
            
            # Funding extremo?
            funding_extreme = funding > 0.01 or funding < -0.01
            
            # Candles bullish/bearish
            if price > candle['open']:
                bullish_count += 1
                bearish_count = 0
            elif price < candle['open']:
                bearish_count += 1
                bullish_count = 0
            else:
                bullish_count = 0
                bearish_count = 0
            
            # SAÍDA
            if current_position == 'long':
                gain_pct = (price - entry_price) / entry_price
                
                if price > max_price:
                    max_price = price
                
                if gain_pct >= params['trailing_activation_pct'] and not trailing_active:
                    trailing_active = True
                    trailing_stop = max_price * (1 - params['trailing_stop_pct'])
                
                if trailing_active:
                    new_trailing = max_price * (1 - params['trailing_stop_pct'])
                    if new_trailing > trailing_stop:
                        trailing_stop = new_trailing
                
                exit_reason = None
                if not trailing_active:
                    loss_pct = (entry_price - price) / entry_price
                    if loss_pct >= params['stop_loss_pct']:
                        exit_reason = 'STOP_LOSS'
                
                if trailing_active and price <= trailing_stop:
                    exit_reason = 'TRAILING_STOP'
                
                if exit_reason:
                    pnl_pct = (price - entry_price) / entry_price
                    pnl_usd = position_size * pnl_pct
                    fee = position_size * fee_pct * 2
                    capital += pnl_usd - fee
                    
                    longs.append({
                        'pnl_pct': pnl_pct,
                        'pnl_usd': pnl_usd - fee,
                        'exit_reason': exit_reason,
                    })
                    
                    current_position = None
                    max_price = 0
                    trailing_active = False
            
            elif current_position == 'short':
                gain_pct = (entry_price - price) / entry_price
                
                if price < min_price:
                    min_price = price
                
                if gain_pct >= params['trailing_activation_pct'] and not trailing_active:
                    trailing_active = True
                    trailing_stop = min_price * (1 + params['trailing_stop_pct'])
                
                if trailing_active:
                    new_trailing = min_price * (1 + params['trailing_stop_pct'])
                    if new_trailing < trailing_stop:
                        trailing_stop = new_trailing
                
                exit_reason = None
                if not trailing_active:
                    loss_pct = (price - entry_price) / entry_price
                    if loss_pct >= params['stop_loss_pct']:
                        exit_reason = 'STOP_LOSS'
                
                if trailing_active and price >= trailing_stop:
                    exit_reason = 'TRAILING_STOP'
                
                if exit_reason:
                    pnl_pct = (entry_price - price) / entry_price
                    pnl_usd = position_size * pnl_pct
                    fee = position_size * fee_pct * 2
                    capital += pnl_usd - fee
                    
                    shorts.append({
                        'pnl_pct': pnl_pct,
                        'pnl_usd': pnl_usd - fee,
                        'exit_reason': exit_reason,
                    })
                    
                    current_position = None
                    min_price = 0
                    trailing_active = False
            
            # ENTRADA
            if current_position is None:
                # Calcular OI change (se disponível)
                oi_change = 0
                if len(oi_history) >= 2 and oi > 0 and oi_history[-2] > 0:
                    oi_change = (oi - oi_history[-2]) / oi_history[-2]
                
                # LONG
                if price_above_sma and bullish_count >= params['min_bullish_candles']:
                    if volume_ratio >= params['volume_threshold'] and not funding_extreme:
                        oi_ok = True
                        if oi > 0:
                            # Com OI: requer mudança positiva
                            oi_ok = oi_change > params['oi_threshold']
                        
                        if oi_ok:
                            current_position = 'long'
                            entry_price = price
                            position_size = min(max_position, capital * 0.1)
                            max_price = price
                            min_price = price
                            bullish_count = 0
                
                # SHORT
                elif not price_above_sma and bearish_count >= params['min_bearish_candles']:
                    if volume_ratio >= params['volume_threshold'] and not funding_extreme:
                        oi_ok = True
                        if oi > 0:
                            # Com OI: requer mudança positiva (mais OI = mais pressão, curto em pullbacks)
                            oi_ok = oi_change > params['oi_threshold']
                        
                        if oi_ok:
                            current_position = 'short'
                            entry_price = price
                            position_size = min(max_position, capital * 0.1)
                            max_price = price
                            min_price = price
                            bearish_count = 0
            
            equity_curve.append((timestamp, capital))
        
        # Combinar trades
        trades = longs + shorts
        
        total_trades = len(trades)
        total_longs = len(longs)
        total_shorts = len(shorts)
        
        if total_trades == 0:
            return {
                'params': params,
                'interval': interval,
                'total_trades': 0,
                'win_rate': 0,
                'profit_factor': 0,
                'pnl_total': 0,
                'max_drawdown': 0,
                'valid': False,
            }
        
        wins = [t for t in trades if t['pnl_pct'] > 0]
        losses = [t for t in trades if t['pnl_pct'] <= 0]
        
        long_wins = [t for t in longs if t['pnl_pct'] > 0]
        long_losses = [t for t in longs if t['pnl_pct'] <= 0]
        short_wins = [t for t in shorts if t['pnl_pct'] > 0]
        short_losses = [t for t in shorts if t['pnl_pct'] <= 0]
        
        win_rate = len(wins) / total_trades
        
        total_wins = sum(t['pnl_usd'] for t in wins)
        total_losses = abs(sum(t['pnl_usd'] for t in losses))
        
        profit_factor = total_wins / total_losses if total_losses > 0 else float('inf')
        
        # PF separados
        long_pf = (sum(t['pnl_usd'] for t in long_wins) / abs(sum(t['pnl_usd'] for t in long_losses))) if long_losses else float('inf')
        short_pf = (sum(t['pnl_usd'] for t in short_wins) / abs(sum(t['pnl_usd'] for t in short_losses))) if short_losses else float('inf')
        
        pnl_total = sum(t['pnl_usd'] for t in trades)
        
        # Max drawdown
        peak = capital
        max_dd = 0
        for _, equity in equity_curve:
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak
            if dd > max_dd:
                max_dd = dd
        
        # Verificar se é válido
        valid = (
            total_trades >= self.min_trades and
            profit_factor >= self.min_pf and
            profit_factor <= self.max_pf and
            pnl_total > 0
        )
        
        return {
            'params': params,
            'interval': interval,
            'total_trades': total_trades,
            'total_longs': total_longs,
            'total_shorts': total_shorts,
            'win_rate': win_rate,
            'profit_factor': profit_factor,
            'long_pf': long_pf if total_longs > 0 else 0,
            'short_pf': short_pf if total_shorts > 0 else 0,
            'pnl_total': pnl_total,
            'pnl_pct': (pnl_total / 10000) * 100,
            'max_drawdown': max_dd,
            'avg_win': total_wins / max(1, len(wins)),
            'avg_loss': total_losses / max(1, len(losses)),
            'valid': valid,
            'trades': trades,
        }
    
    def optimize(self, symbol: str = 'BTC') -> Dict:
        """
        Corre grid search completo para todos os timeframes
        Retorna os melhores resultados por timeframe
        """
        logger.info("="*70)
        logger.info("STRATEGY OPTIMIZER v2 - Multi-Timeframe Grid Search")
        logger.info("="*70)
        logger.info(f"Timeframes: {', '.join(self.intervals)}")
        logger.info(f"Assets: {symbol}")
        logger.info("="*70)
        
        all_results = {}
        
        for interval in self.intervals:
            logger.info(f"\n{'='*70}")
            logger.info(f"TIMEFRAME: {interval}")
            logger.info(f"{'='*70}")
            
            # Carregar dados
            data = self._load_data(symbol, interval)
            if not data:
                logger.warning(f"Pulando {interval} - sem dados")
                continue
            
            # Gerar todas as combinações
            keys = list(self.param_grid.keys())
            values = [self.param_grid[k] for k in keys]
            
            total_combinations = 1
            for v in values:
                total_combinations *= len(v)
            
            logger.info(f"Total de combinações a testar: {total_combinations:,}")
            
            # Testar cada combinação
            interval_results = []
            tested = 0
            
            for combo in itertools.product(*values):
                params = dict(zip(keys, combo))
                
                result = self._run_backtest(data, params, interval)
                tested += 1
                
                if result['valid']:
                    interval_results.append(result)
                
                if tested % 200 == 0:
                    logger.info(f"Progresso {interval}: {tested}/{total_combinations} "
                               f"({tested/total_combinations*100:.1f}%) | "
                               f"Válidos: {len(interval_results)} | "
                               f"Best PF: {max([r['profit_factor'] for r in interval_results] or [0]):.2f}")
            
            # Ordenar por profit factor
            interval_results.sort(key=lambda x: x['profit_factor'], reverse=True)
            
            logger.info(f"\n{'='*70}")
            logger.info(f"TOP 5 ESTRATÉGIAS - {interval}")
            logger.info(f"{'='*70}")
            
            for i, r in enumerate(interval_results[:5]):
                logger.info(f"\n#{i+1} — PF: {r['profit_factor']:.2f} | "
                           f"WR: {r['win_rate']*100:.1f}% | "
                           f"PnL: ${r['pnl_total']:.2f} | "
                           f"Trades: {r['total_trades']} (L:{r['total_longs']} S:{r['total_shorts']}) | "
                           f"DD: {r['max_drawdown']*100:.2f}%")
                logger.info(f"    Bullish: {r['params']['min_bullish_candles']} | Bearish: {r['params']['min_bearish_candles']}")
                logger.info(f"    Vol: {r['params']['volume_threshold']:.1f}x | SMA: {r['params']['price_sma_period']} | "
                           f"Trail: {r['params']['trailing_stop_pct']*100:.1f}% | SL: {r['params']['stop_loss_pct']*100:.1f}%")
            
            all_results[interval] = interval_results
            self.results_by_interval[interval] = interval_results[:10]
        
        # Resumo comparativo
        logger.info(f"\n{'='*70}")
        logger.info("RESUMO COMPARATIVO - MELHOR POR TIMEFRAME")
        logger.info(f"{'='*70}")
        
        comparison = []
        for interval in self.intervals:
            if all_results.get(interval) and len(all_results[interval]) > 0:
                best = all_results[interval][0]
                comparison.append({
                    'interval': interval,
                    'pf': best['profit_factor'],
                    'wr': best['win_rate'],
                    'trades': best['total_trades'],
                    'longs': best['total_longs'],
                    'shorts': best['total_shorts'],
                    'long_pf': best['long_pf'],
                    'short_pf': best['short_pf'],
                    'pnl': best['pnl_total'],
                    'dd': best['max_drawdown'],
                    'params': best['params'],
                })
        
        # Ordenar por profit factor
        comparison.sort(key=lambda x: x['pf'], reverse=True)
        
        for c in comparison:
            logger.info(f"\n{c['interval']:>3} | PF: {c['pf']:5.2f} | WR: {c['wr']*100:5.1f}% | "
                       f"Trades: {c['trades']:>2} (L:{c['longs']:>2} S:{c['shorts']:>2}) | "
                       f"PnL: ${c['pnl']:6.2f} | DD: {c['dd']*100:4.2f}%")
            logger.info(f"     Long PF: {c['long_pf']:.2f} | Short PF: {c['short_pf']:.2f}")
            logger.info(f"     Bullish: {c['params']['min_bullish_candles']} | Bearish: {c['params']['min_bearish_candles']} | "
                       f"Vol: {c['params']['volume_threshold']:.1f}x | SMA: {c['params']['price_sma_period']}")
        
        # Guardar resultados
        output_file = Path(f'backtest_results/optimization_multi_timeframe_{symbol}.json')
        output_file.parent.mkdir(exist_ok=True)
        
        with open(output_file, 'w') as f:
            json.dump({
                'timestamp': datetime.now().isoformat(),
                'symbol': symbol,
                'timeframes_tested': self.intervals,
                'comparison': comparison,
                'best_overall': comparison[0] if comparison else None,
                'all_results': {
                    interval: [
                        {
                            'rank': i+1,
                            'profit_factor': r['profit_factor'],
                            'win_rate': r['win_rate'],
                            'pnl_total': r['pnl_total'],
                            'total_trades': r['total_trades'],
                            'total_longs': r['total_longs'],
                            'total_shorts': r['total_shorts'],
                            'long_pf': r['long_pf'],
                            'short_pf': r['short_pf'],
                            'max_drawdown': r['max_drawdown'],
                            'params': r['params'],
                        }
                        for i, r in enumerate(results)
                    ]
                    for interval, results in all_results.items()
                },
            }, f, indent=2)
        
        logger.info(f"\nResultados guardados em: {output_file}")
        
        return {
            'comparison': comparison,
            'best': comparison[0] if comparison else None,
            'all_results': all_results,
        }


def main():
    """Entry point - otimiza múltiplos timeframes"""
    import sys
    
    # Verificar dados disponíveis
    db = BotDatabase()
    stats = db.get_stats()
    logger.info(f"Dados na DB: {stats}")
    
    # Perguntar ao utilizador se quer descarregar dados em falta
    symbols_available = stats.get('symbols', [])
    intervals_in_db = set()
    
    # Verificar quais timeframes existem
    with db._get_conn() as conn:
        rows = conn.execute("SELECT DISTINCT interval FROM candles").fetchall()
        intervals_in_db = set([r[0] for r in rows])
    
    logger.info(f"Timeframes disponíveis: {intervals_in_db}")
    
    optimizer = StrategyOptimizer(db)
    
    # Otimizar todos os timeframes
    results = optimizer.optimize('BTC')
    
    if results.get('best'):
        best = results['best']
        print("\n" + "="*70)
        print("🏆 MELHOR ESTRATÉGIA GLOBAL")
        print("="*70)
        print(f"Timeframe: {best['interval']}")
        print(f"Profit Factor: {best['pf']:.2f}")
        print(f"Win Rate: {best['wr']*100:.1f}%")
        print(f"Trades: {best['trades']} (L:{best['longs']} S:{best['shorts']})")
        print(f"Long PF: {best['long_pf']:.2f} | Short PF: {best['short_pf']:.2f}")
        print(f"PnL: ${best['pnl']:.2f}")
        print(f"Max DD: {best['dd']*100:.2f}%")
        print("\n📋 PARÂMETROS ÓTIMOS:")
        for k, v in best['params'].items():
            print(f"  {k}: {v}")
        print("="*70)
    else:
        print("\n⚠️ Nenhuma estratégia válida encontrada!")
        print("Possíveis causas:")
        print("  - Sem dados suficientes na DB (precisa de 1m, 5m, 15m)")
        print("  - Dados muito antigos")
        print("  - Thresholds muito apertados para os dados disponíveis")
        print("\nSolução: Descarregar mais dados com data_downloader.py")


if __name__ == "__main__":
    main()
