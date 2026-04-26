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
        
        # ⚡ CONFIGURAÇÃO DE CUSTOS (realismo)
        self.taker_fee = config.get('backtest_costs', {}).get('taker_fee', 0.00045)  # 0.045%
        self.maker_fee = config.get('backtest_costs', {}).get('maker_fee', 0.00015)  # 0.015%
        self.slippage = config.get('backtest_costs', {}).get('slippage_btc', 0.0002)  # 0.02%
        self.funding_hourly = config.get('backtest_costs', {}).get('funding_hourly_avg', 0.00001)  # 0.001%/h
        
        # ⚡ TAKE PROFIT E TRAILING STOP
        self.take_profit_pct = config.get('strategy', {}).get('take_profit_pct', 0.10)  # 10% default
        self.trailing_activation_pct = config['risk']['trailing_activation_pct']
        self.trailing_stop_pct = config['risk']['trailing_stop_pct']
        
        # Estado do backtest
        self.trades = []
        self.equity = []  # Equity curve
        self.current_position = None
        self.entry_price = 0
        self.entry_time = None
        self.highest_price = 0  # Para trailing stop
        self.trailing_active = False
        self.volume_history = deque(maxlen=self.volume_lookback)
        self.last_oi = 0
        
        # Métricas
        self.initial_capital = 10000.0
        self.current_capital = self.initial_capital
        self.max_drawdown = 0
        self.peak_equity = self.initial_capital
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        
        # Custos totais acumulados
        self.total_fees_paid = 0
        self.total_slippage_paid = 0
        self.total_funding_paid = 0
        
        # PnL tracking
        self.pnl_gross_total = 0
        self.pnl_net_total = 0
        
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
                # Atualizar highest price para trailing stop
                if price > self.highest_price:
                    self.highest_price = price
                
                # Check stop loss
                loss_pct = (self.entry_price - price) / self.entry_price
                if loss_pct >= self.stop_loss_pct:
                    self._exit_position(price, timestamp, 'STOP_LOSS')
                    continue
                
                # Check take profit (10%)
                gain_pct = (price - self.entry_price) / self.entry_price
                if gain_pct >= self.take_profit_pct:
                    self._exit_position(price, timestamp, 'TAKE_PROFIT')
                    continue
                
                # ⚡ TRAILING STOP
                # Ativa quando lucro > trailing_activation_pct (2%)
                if gain_pct >= self.trailing_activation_pct:
                    self.trailing_active = True
                    # Trailing stop = highest_price - trailing_stop_pct (2%)
                    trailing_level = self.highest_price * (1 - self.trailing_stop_pct)
                    if price <= trailing_level:
                        self._exit_position(price, timestamp, 'TRAILING_STOP')
                        continue
                
                # Check exaustão de momentum (OI a descer) — menos agressivo
                if oi_change < -0.01 and oi > 0:  # Mudado de -0.005 para -0.01
                    self._exit_position(price, timestamp, 'OI_EXHAUSTION')
                    continue
        
        # Fechar posição aberta no final
        if self.current_position:
            last_candle = data[-1]
            self._exit_position(last_candle['close'], last_candle['timestamp'], 'END_OF_DATA')
        
        return self._calculate_metrics()
    
    def _enter_long(self, price: float, timestamp: int, volume_ratio: float, 
                    oi_change: float, funding: float):
        """Simula entrada em posição LONG com custos"""
        self.current_position = 'long'
        self.entry_price = price
        self.entry_time = timestamp
        self.highest_price = price  # Reset highest price
        self.trailing_active = False
        
        # Tamanho da posição
        position_size = min(self.max_position_usd, self.current_capital * 0.1)
        self.position_size = position_size
        
        # ⚡ CUSTOS DE ENTRADA (taker + slippage)
        entry_fee = position_size * self.taker_fee
        entry_slippage = position_size * self.slippage
        total_entry_cost = entry_fee + entry_slippage
        
        self.current_capital -= total_entry_cost
        self.total_fees_paid += entry_fee
        self.total_slippage_paid += entry_slippage
        
        logger.info(
            f"[ENTER LONG] @ ${price:,.2f} | Size: ${position_size:.2f} | "
            f"Vol: {volume_ratio:.2f}x | OI: +{oi_change*100:.3f}% | Funding: {funding*100:.4f}% | "
            f"Custo entrada: ${total_entry_cost:.2f}"
        )
        
        self.trades.append({
            'type': 'entry',
            'direction': 'long',
            'price': price,
            'timestamp': timestamp,
            'size': position_size,
            'entry_cost': total_entry_cost
        })
    
    def _exit_position(self, price: float, timestamp: int, reason: str):
        """Simula saída da posição com custos reais"""
        if not self.current_position:
            return
        
        # Calcular PnL bruto
        if self.current_position == 'long':
            pnl_pct = (price - self.entry_price) / self.entry_price
        else:
            pnl_pct = (self.entry_price - price) / self.entry_price
        
        position_size = getattr(self, 'position_size', self.max_position_usd)
        pnl_gross = position_size * pnl_pct
        
        # ⚡ CUSTOS DE SAÍDA (maker + slippage)
        exit_fee = position_size * self.maker_fee  # Assume maker na saída
        exit_slippage = position_size * self.slippage
        
        # ⚡ CUSTOS DE FUNDING durante o hold
        hold_hours = (timestamp - self.entry_time) / (1000 * 3600)  # ms para horas
        funding_cost = position_size * self.funding_hourly * hold_hours
        
        total_exit_cost = exit_fee + exit_slippage + funding_cost
        
        # PnL LÍQUIDO
        pnl_net = pnl_gross - total_exit_cost
        
        # Atualizar acumuladores
        self.total_fees_paid += exit_fee
        self.total_slippage_paid += exit_slippage
        self.total_funding_paid += funding_cost
        self.pnl_gross_total += pnl_gross
        self.pnl_net_total += pnl_net
        
        # Atualizar capital
        self.current_capital += pnl_net
        
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
        if pnl_net > 0:
            self.winning_trades += 1
        else:
            self.losing_trades += 1
        
        dt = datetime.fromtimestamp(timestamp / 1000)
        logger.info(
            f"[EXIT] @ ${price:,.2f} | PnL Bruto: ${pnl_gross:+.2f} | "
            f"Custos: ${total_exit_cost:.2f} | PnL Líquido: ${pnl_net:+.2f} ({pnl_pct*100:+.2f}%) | "
            f"Reason: {reason} | Capital: ${self.current_capital:,.2f}"
        )
        
        self.trades.append({
            'type': 'exit',
            'price': price,
            'timestamp': timestamp,
            'pnl_gross': pnl_gross,
            'pnl_net': pnl_net,
            'pnl_pct': pnl_pct,
            'exit_cost': total_exit_cost,
            'reason': reason
        })
        
        self.current_position = None
        self.entry_price = 0
        self.position_size = 0
    
    def _calculate_metrics(self) -> Dict:
        """Calcula métricas finais de performance com PnL líquido"""
        if self.total_trades == 0:
            return {
                'total_trades': 0,
                'message': 'Nenhum trade executado - dados insuficientes ou estratégia não disparou'
            }
        
        total_pnl_gross = self.pnl_gross_total
        total_pnl_net = self.pnl_net_total
        total_costs = self.total_fees_paid + self.total_slippage_paid + self.total_funding_paid
        
        total_return_gross = (total_pnl_gross / self.initial_capital) * 100
        total_return_net = (total_pnl_net / self.initial_capital) * 100
        
        wins = [t['pnl_net'] for t in self.trades if t.get('type') == 'exit' and t['pnl_net'] > 0]
        losses = [t['pnl_net'] for t in self.trades if t.get('type') == 'exit' and t['pnl_net'] <= 0]
        
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        
        profit_factor = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float('inf')
        
        win_rate = (self.winning_trades / self.total_trades) * 100
        
        # Breakeven win rate
        avg_win_abs = abs(avg_win) if avg_win != 0 else 0.01
        avg_loss_abs = abs(avg_loss) if avg_loss != 0 else 0.01
        breakeven_wr = avg_loss_abs / (avg_win_abs + avg_loss_abs) * 100
        
        # Razões de saída
        from collections import Counter
        exit_reasons = Counter(t['reason'] for t in self.trades if t.get('type') == 'exit')
        
        metrics = {
            'initial_capital': self.initial_capital,
            'final_capital': self.current_capital,
            'total_pnl_gross': total_pnl_gross,
            'total_pnl_net': total_pnl_net,
            'total_costs': total_costs,
            'total_fees': self.total_fees_paid,
            'total_slippage': self.total_slippage_paid,
            'total_funding_costs': self.total_funding_paid,
            'total_return_gross_pct': total_return_gross,
            'total_return_net_pct': total_return_net,
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'losing_trades': self.losing_trades,
            'win_rate': win_rate,
            'breakeven_wr': breakeven_wr,
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'profit_factor': profit_factor,
            'max_drawdown_pct': self.max_drawdown * 100,
            'exit_reasons': dict(exit_reasons),
            'trades': self.trades
        }
        
        return metrics
    
    def print_report(self, metrics: Dict):
        """Imprime relatório formatado com PnL líquido e custos"""
        print("\n" + "="*70)
        print("          BACKTEST REPORT — PnL LÍQUIDO")
        print("="*70)
        
        if metrics.get('total_trades', 0) == 0:
            print(metrics.get('message', 'Sem resultados'))
            print("="*70)
            return
        
        print(f"Capital Inicial:       ${metrics['initial_capital']:>12,.2f}")
        print(f"Capital Final:         ${metrics['final_capital']:>12,.2f}")
        print(f"-"*70)
        print(f"PnL BRUTO:             ${metrics['total_pnl_gross']:>12,.2f} ({metrics['total_return_gross_pct']:+.2f}%)")
        print(f"  Total Fees:          ${metrics['total_fees']:>12,.2f}")
        print(f"  Total Slippage:      ${metrics['total_slippage']:>12,.2f}")
        print(f"  Total Funding:       ${metrics['total_funding_costs']:>12,.2f}")
        print(f"CUSTOS TOTAIS:         ${metrics['total_costs']:>12,.2f}")
        print(f"PnL LÍQUIDO:           ${metrics['total_pnl_net']:>12,.2f} ({metrics['total_return_net_pct']:+.2f}%)")
        print(f"-"*70)
        print(f"Total Trades:          {metrics['total_trades']:>12}")
        print(f"Win Rate:              {metrics['win_rate']:>11.1f}%")
        print(f"Breakeven WR:          {metrics['breakeven_wr']:>11.1f}%")
        print(f"Profit Factor:         {metrics['profit_factor']:>12.2f}")
        print(f"Avg Win:               ${metrics['avg_win']:>12,.2f}")
        print(f"Avg Loss:              ${metrics['avg_loss']:>12,.2f}")
        print(f"Max Drawdown:          {metrics['max_drawdown_pct']:>11.2f}%")
        print(f"-"*70)
        print("Razões de Saída:")
        for reason, count in metrics.get('exit_reasons', {}).items():
            print(f"  {reason:20s}: {count:>3}")
        print("="*70)


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
