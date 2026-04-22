"""
Backtest Engine - Simula a estratégia em dados históricos
"""
import csv
import json
import logging
from pathlib import Path
from datetime import datetime
from collections import deque
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class BacktestEngine:
    """
    Motor de backtest para a estratégia de momentum
    Simula trades em dados históricos e calcula métricas de performance
    """
    
    def __init__(self, config: Dict):
        self.volume_threshold = config['strategy']['volume_spike_threshold']
        self.oi_threshold = config['strategy']['oi_change_threshold']
        self.max_funding = config['strategy']['max_funding_rate']
        self.min_funding = config['strategy']['min_funding_rate']
        self.volume_lookback = config['strategy']['volume_lookback']
        
        # Configurações de risco
        self.stop_loss_pct = config['risk']['stop_loss_pct']
        self.max_position_usd = config['risk']['max_position_size_usd']
        self.max_leverage = config['risk']['max_leverage']
        
        # Estado do backtest
        self.trades = []
        self.equity = []  # Equity curve
        self.current_position = None  # None ou 'long'
        self.entry_price = 0
        self.entry_time = None
        self.volume_history = deque(maxlen=self.volume_lookback)
        self.last_oi = 0
        
        # Métricas
        self.initial_capital = 10000.0  # Capital inicial simulado
        self.current_capital = self.initial_capital
        self.max_drawdown = 0
        self.peak_equity = self.initial_capital
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        
    def load_data(self, data_dir: str, symbol: str, interval: str = "5m") -> List[Dict]:
        """
        Carrega e alinha dados históricos (candles + OI + funding)
        
        Returns:
            Lista de dicts com: timestamp, open, high, low, close, volume, oi, funding_rate
        """
        data_dir = Path(data_dir)
        
        # Carregar candles
        candles_file = data_dir / f"{symbol.lower()}_{interval}.csv"
        candles = []
        with open(candles_file, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                candles.append({
                    'timestamp': int(row['timestamp']),
                    'open': float(row['open']),
                    'high': float(row['high']),
                    'low': float(row['low']),
                    'close': float(row['close']),
                    'volume': float(row['volume'])
                })
        
        logger.info(f"Candles carregados: {len(candles)}")
        
        # Carregar OI history
        oi_file = data_dir / f"{symbol.lower()}_oi_{interval}.csv"
        oi_data = {}
        if oi_file.exists():
            with open(oi_file, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    ts = int(row['timestamp'])
                    oi_data[ts] = float(row['sumOpenInterestValue'])
            logger.info(f"OI carregado: {len(oi_data)} registos")
        else:
            logger.warning(f"Ficheiro OI não encontrado: {oi_file}")
        
        # Carregar funding rate
        funding_file = data_dir / f"{symbol.lower()}_funding.csv"
        funding_data = {}
        if funding_file.exists():
            with open(funding_file, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    ts = int(row['fundingTime'])
                    funding_data[ts] = float(row['fundingRate'])
            logger.info(f"Funding carregado: {len(funding_data)} registos")
        else:
            logger.warning(f"Ficheiro funding não encontrado: {funding_file}")
        
        # Alinhar dados: para cada candle, encontrar OI e funding mais próximos
        aligned_data = []
        for candle in candles:
            ts = candle['timestamp']
            
            # Encontrar OI mais próximo (dentro de 5 minutos)
            oi = 0
            for oi_ts, oi_val in oi_data.items():
                if abs(oi_ts - ts) < 300000:  # 5 minutos em ms
                    oi = oi_val
                    break
            
            # Encontrar funding mais próximo (dentro de 8h)
            funding = 0
            for fund_ts, fund_val in funding_data.items():
                if abs(fund_ts - ts) < 28800000:  # 8h em ms
                    funding = fund_val
                    break
            
            aligned_data.append({
                **candle,
                'oi': oi,
                'funding_rate': funding
            })
        
        return aligned_data
    
    def run(self, data: List[Dict]) -> Dict:
        """
        Corre o backtest sobre os dados históricos
        
        Returns:
            Dict com métricas de performance
        """
        logger.info(f"A iniciar backtest com {len(data)} candles...")
        
        for i, candle in enumerate(data):
            price = candle['close']
            volume = candle['volume']
            oi = candle['oi']
            funding = candle['funding_rate']
            timestamp = candle['timestamp']
            
            # Guardar volume no histórico
            self.volume_history.append(volume)
            
            # Pular primeiros N candles até termos dados suficientes
            if len(self.volume_history) < self.volume_lookback // 2:
                continue
            
            # Calcular média de volume
            volume_avg = sum(self.volume_history) / len(self.volume_history)
            volume_ratio = volume / volume_avg if volume_avg > 0 else 0
            
            # Calcular variação de OI
            oi_change = 0
            if self.last_oi > 0 and oi > 0:
                oi_change = (oi - self.last_oi) / self.last_oi
            
            # Atualizar último OI
            if oi > 0:
                self.last_oi = oi
            
            # Verificar se funding está extremo
            funding_extreme = funding > self.max_funding or funding < self.min_funding
            
            # LOGGING periódico
            if i % 100 == 0:
                dt = datetime.fromtimestamp(timestamp / 1000)
                logger.info(
                    f"[{dt}] Preço: ${price:,.2f} | Vol: {volume_ratio:.2f}x | "
                    f"OI: ${oi:,.0f} | OI Δ: {oi_change*100:.3f}% | Funding: {funding*100:.4f}%"
                )
            
            # SINAL DE ENTRADA LONG
            if self.current_position is None:
                if (volume_ratio > self.volume_threshold and 
                    oi_change > self.oi_threshold and
                    not funding_extreme and
                    oi > 0):
                    
                    self._enter_long(price, timestamp, volume_ratio, oi_change, funding)
            
            # SINAL DE SAÍDA
            elif self.current_position == 'long':
                # Check stop loss
                loss_pct = (self.entry_price - price) / self.entry_price
                if loss_pct >= self.stop_loss_pct:
                    self._exit_position(price, timestamp, 'STOP_LOSS')
                    continue
                
                # Check exaustão de momentum (OI a descer)
                if oi_change < -0.005 and oi > 0:
                    self._exit_position(price, timestamp, 'OI_EXHAUSTION')
                    continue
                
                # Check take profit simples (2x risk)
                gain_pct = (price - self.entry_price) / self.entry_price
                if gain_pct >= 0.04:  # 4% take profit
                    self._exit_position(price, timestamp, 'TAKE_PROFIT')
                    continue
        
        # Fechar posição aberta no final
        if self.current_position:
            last_candle = data[-1]
            self._exit_position(last_candle['close'], last_candle['timestamp'], 'END_OF_DATA')
        
        return self._calculate_metrics()
    
    def _enter_long(self, price: float, timestamp: int, volume_ratio: float, 
                    oi_change: float, funding: float):
        """Simula entrada em posição LONG"""
        self.current_position = 'long'
        self.entry_price = price
        self.entry_time = timestamp
        
        # Tamanho da posição
        position_size = min(self.max_position_usd, self.current_capital * 0.1)
        
        logger.info(
            f"[ENTER LONG] @ ${price:,.2f} | Size: ${position_size:.2f} | "
            f"Vol: {volume_ratio:.2f}x | OI: +{oi_change*100:.3f}% | Funding: {funding*100:.4f}%"
        )
        
        self.trades.append({
            'type': 'entry',
            'direction': 'long',
            'price': price,
            'timestamp': timestamp,
            'size': position_size
        })
    
    def _exit_position(self, price: float, timestamp: int, reason: str):
        """Simula saída da posição"""
        if not self.current_position:
            return
        
        # Calcular PnL
        if self.current_position == 'long':
            pnl_pct = (price - self.entry_price) / self.entry_price
        else:
            pnl_pct = (self.entry_price - price) / self.entry_price
        
        position_size = self.trades[-1]['size'] if self.trades else self.max_position_usd
        pnl_usd = position_size * pnl_pct
        
        # Atualizar capital
        self.current_capital += pnl_usd
        
        # Atualizar equity
        self.equity.append({
            'timestamp': timestamp,
            'equity': self.current_capital
        })
        
        # Atualizar drawdown
        if self.current_capital > self.peak_equity:
            self.peak_equity = self.current_capital
        drawdown = (self.peak_equity - self.current_capital) / self.peak_equity
        if drawdown > self.max_drawdown:
            self.max_drawdown = drawdown
        
        # Contabilizar trade
        self.total_trades += 1
        if pnl_usd > 0:
            self.winning_trades += 1
        else:
            self.losing_trades += 1
        
        dt = datetime.fromtimestamp(timestamp / 1000)
        logger.info(
            f"[EXIT] @ ${price:,.2f} | PnL: ${pnl_usd:+.2f} ({pnl_pct*100:+.2f}%) | "
            f"Reason: {reason} | Capital: ${self.current_capital:,.2f}"
        )
        
        self.trades.append({
            'type': 'exit',
            'price': price,
            'timestamp': timestamp,
            'pnl_usd': pnl_usd,
            'pnl_pct': pnl_pct,
            'reason': reason
        })
        
        self.current_position = None
        self.entry_price = 0
    
    def _calculate_metrics(self) -> Dict:
        """Calcula métricas finais de performance"""
        if self.total_trades == 0:
            return {
                'total_trades': 0,
                'message': 'Nenhum trade executado - dados insuficientes ou estratégia não disparou'
            }
        
        total_pnl = self.current_capital - self.initial_capital
        total_return = (total_pnl / self.initial_capital) * 100
        
        wins = [t['pnl_usd'] for t in self.trades if t.get('type') == 'exit' and t['pnl_usd'] > 0]
        losses = [t['pnl_usd'] for t in self.trades if t.get('type') == 'exit' and t['pnl_usd'] <= 0]
        
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        
        profit_factor = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float('inf')
        
        win_rate = (self.winning_trades / self.total_trades) * 100
        
        metrics = {
            'initial_capital': self.initial_capital,
            'final_capital': self.current_capital,
            'total_pnl': total_pnl,
            'total_return_pct': total_return,
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'losing_trades': self.losing_trades,
            'win_rate': win_rate,
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'profit_factor': profit_factor,
            'max_drawdown_pct': self.max_drawdown * 100,
            'trades': self.trades
        }
        
        return metrics
    
    def print_report(self, metrics: Dict):
        """Imprime relatório formatado"""
        print("\n" + "="*60)
        print("          BACKTEST REPORT")
        print("="*60)
        
        if metrics.get('total_trades', 0) == 0:
            print(metrics.get('message', 'Sem resultados'))
            print("="*60)
            return
        
        print(f"Capital Inicial:     ${metrics['initial_capital']:>12,.2f}")
        print(f"Capital Final:       ${metrics['final_capital']:>12,.2f}")
        print(f"PnL Total:           ${metrics['total_pnl']:>12,.2f} ({metrics['total_return_pct']:+.2f}%)")
        print(f"-"*60)
        print(f"Total Trades:        {metrics['total_trades']:>12}")
        print(f"Win Rate:            {metrics['win_rate']:>11.1f}%")
        print(f"Profit Factor:       {metrics['profit_factor']:>12.2f}")
        print(f"Avg Win:             ${metrics['avg_win']:>12,.2f}")
        print(f"Avg Loss:            ${metrics['avg_loss']:>12,.2f}")
        print(f"Max Drawdown:        {metrics['max_drawdown_pct']:>11.2f}%")
        print("="*60)
        
        # Veredito
        if metrics['profit_factor'] > 1.5 and metrics['max_drawdown_pct'] < 20:
            print("[PASS] Estratégia parece VÁLIDA para testes adicionais")
        elif metrics['profit_factor'] > 1.0:
            print("[WARNING] Estratégia tem edge positivo mas precisa de refinamento")
        else:
            print("[FAIL] Estratégia PERDE DINHEIRO - NÃO usar em live trading")
        
        print("="*60)


def run_backtest(config: Dict, symbol: str = "BTCUSDT", interval: str = "5m", 
                 data_dir: str = "data", days: int = 30):
    """
    Função helper para correr backtest completo
    """
    engine = BacktestEngine(config)
    
    # Verificar se dados existem
    data_path = Path(data_dir)
    candles_file = data_path / f"{symbol.lower()}_{interval}.csv"
    
    if not candles_file.exists():
        logger.error(f"Dados não encontrados: {candles_file}")
        print(f"\nDados não encontrados! Corre primeiro:")
        print(f"  python src/data_downloader.py {symbol} {days}")
        return None
    
    # Carregar e correr
    data = engine.load_data(data_dir, symbol, interval)
    metrics = engine.run(data)
    engine.print_report(metrics)
    
    # Guardar resultados
    results_dir = Path("backtest_results")
    results_dir.mkdir(exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    results_file = results_dir / f"backtest_{symbol.lower()}_{interval}_{timestamp}.json"
    
    with open(results_file, 'w') as f:
        json.dump(metrics, f, indent=2, default=str)
    
    print(f"\nResultados guardados: {results_file}")
    
    return metrics


if __name__ == "__main__":
    import sys
    from utils import load_config
    
    logging.basicConfig(level=logging.INFO)
    
    config = load_config()
    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    
    run_backtest(config, symbol, days=days)
