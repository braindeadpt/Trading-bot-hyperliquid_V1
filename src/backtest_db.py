"""
Backtest Engine v2 - Usa SQLite em vez de CSVs
Backtest rápido sem precisar de APIs em tempo real
"""
import json
import logging
from pathlib import Path
from datetime import datetime
from collections import deque
from typing import Dict, List, Optional

from database import BotDatabase

logger = logging.getLogger(__name__)


class BacktestEngineDB:
    """
    Motor de backtest v2 - lê dados da base de dados SQLite
    Muito mais rápido e não depende de APIs externas durante o teste
    """
    
    def __init__(self, config: Dict, db: Optional[BotDatabase] = None):
        self.config = config
        self.db = db or BotDatabase()
        
        # Parâmetros da estratégia
        strat = config.get('strategy', {})
        self.volume_threshold = strat.get('volume_spike_threshold', 2.0)
        self.oi_threshold = strat.get('oi_change_threshold', 0.02)
        self.max_funding = strat.get('max_funding_rate', 0.001)
        self.min_funding = strat.get('min_funding_rate', -0.001)
        self.volume_lookback = strat.get('volume_lookback', 20)
        
        # Novos parâmetros de filtro
        self.price_sma_period = strat.get('price_sma_period', 20)
        self.min_bullish_candles = strat.get('min_bullish_candles', 2)
        self.min_bearish_candles = strat.get('min_bearish_candles', 2)
        
        # Filtros separados para short (mais apertados)
        self.short_volume_threshold = strat.get('short_volume_threshold', 3.0)
        self.short_min_bearish_candles = strat.get('short_min_bearish_candles', 3)
        self.short_enabled = strat.get('short_enabled', True)
        
        # Configurações de risco
        risk = config.get('risk', {})
        self.stop_loss_pct = risk.get('stop_loss_pct', 0.02)
        self.short_stop_loss_pct = risk.get('short_stop_loss_pct', 0.025)
        self.max_position_usd = risk.get('max_position_size_usd', 500)
        self.max_leverage = risk.get('max_leverage', 3)
        
        # TRAILING STOP
        self.trailing_stop_pct = risk.get('trailing_stop_pct', 0.015)  # 1.5% abaixo do máximo
        self.trailing_activation_pct = risk.get('trailing_activation_pct', 0.015)  # Só ativa após este lucro
        
        # Estado do backtest
        self.trades = []
        self.equity = []
        self.current_position = None  # 'long' ou 'short'
        self.entry_price = 0
        self.entry_time = None
        self.volume_history = deque(maxlen=self.volume_lookback)
        self.price_history = deque(maxlen=self.price_sma_period)
        self.last_oi = 0
        self.bullish_count = 0  # Contador de candles bullish consecutivos
        self.bearish_count = 0  # Contador de candles bearish consecutivos
        
        # Trailing stop tracking
        self.max_price_since_entry = 0
        self.min_price_since_entry = 0
        self.trailing_stop_price = 0
        self.trailing_active = False  # Só ativa depois de atingir lucro mínimo
        
        # Métricas
        self.initial_capital = 10000.0
        self.current_capital = self.initial_capital
        self.max_drawdown = 0
        self.peak_equity = self.initial_capital
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        
        # Fee estimation (Hyperliquid taker fee ~ 0.035%)
        self.fee_pct = 0.00035
    
    def load_data(self, symbol: str = "BTC", interval: str = "15m", days: int = 30) -> List[Dict]:
        """Carrega dados da base de dados"""
        data = self.db.get_candles_for_backtest(symbol, interval, days)
        
        if not data:
            logger.error(f"Sem dados em DB para {symbol} {interval}!")
            logger.info("Executa primeiro: python src/data_downloader.py")
            return []
        
        logger.info(f"Dados carregados: {len(data)} candles de {symbol} {interval}")
        return data
    
    def run(self, symbol: str = "BTC", interval: str = "15m", days: int = 30,
            save_to_db: bool = True) -> Dict:
        """
        Corre backtest completo
        
        Returns:
            Dict com métricas de performance
        """
        data = self.load_data(symbol, interval, days)
        if not data:
            return {'error': 'Sem dados disponíveis'}
        
        logger.info(f"Iniciando backtest: {symbol} {interval} ({len(data)} candles)")
        
        # Reset estado
        self.__init__(self.config, self.db)
        
        for i, candle in enumerate(data):
            price = candle['close']
            volume = candle['volume']
            oi = candle.get('oi', 0)
            funding = candle.get('funding_rate', 0)
            timestamp = candle['timestamp']
            
            # Guardar volume no histórico
            self.volume_history.append(volume)
            
            # Pular primeiros N candles
            if len(self.volume_history) < self.volume_lookback // 2:
                continue
            
            # Calcular métricas
            volume_avg = sum(self.volume_history) / len(self.volume_history)
            volume_ratio = volume / volume_avg if volume_avg > 0 else 0
            
            # SMA de preço para filtro de tendência
            self.price_history.append(price)
            price_sma = sum(self.price_history) / len(self.price_history) if len(self.price_history) >= self.price_sma_period else 0
            price_above_sma = price > price_sma if price_sma > 0 else False
            
            oi_change = 0
            if self.last_oi > 0 and oi > 0:
                oi_change = (oi - self.last_oi) / self.last_oi
            
            if oi > 0:
                self.last_oi = oi
            
            funding_extreme = funding > self.max_funding or funding < self.min_funding
            
            # Contar candles bullish e bearish consecutivos
            candle_bullish = candle['close'] > candle['open']
            candle_bearish = candle['close'] < candle['open']
            if candle_bullish:
                self.bullish_count += 1
                self.bearish_count = 0
            elif candle_bearish:
                self.bearish_count += 1
                self.bullish_count = 0
            else:
                self.bullish_count = 0
                self.bearish_count = 0
            
            # Logging periódico
            if i % 500 == 0:
                dt = datetime.fromtimestamp(timestamp / 1000)
                trend_status = "✅ ACIMA_SMA" if price_above_sma else "❌ ABAIXO_SMA"
                
                # Mostrar trailing stop se em posição
                trailing_info = ""
                if self.current_position:
                    trailing_info = f" | Trail: ${self.trailing_stop_price:,.0f}"
                
                logger.info(
                    f"[{dt}] ${price:,.0f} | Vol: {volume_ratio:.2f}x | "
                    f"SMA{self.price_sma_period}: ${price_sma:,.0f} | {trend_status} | "
                    f"Bullish: {self.bullish_count}x{trailing_info}"
                )
            
            # SINAIS DE ENTRADA (LONG e SHORT)
            if self.current_position is None:
                # Volume spike forte
                volume_ok = volume_ratio > self.volume_threshold
                funding_ok = not funding_extreme
                
                # LONG: Tendência de alta + candles bullish
                if price_above_sma and self.bullish_count >= self.min_bullish_candles and volume_ok and funding_ok and price > 0:
                    if oi > 0:
                        if oi_change > self.oi_threshold:
                            self._enter_long(symbol, price, timestamp, volume_ratio, oi_change, funding)
                    else:
                        self._enter_long(symbol, price, timestamp, volume_ratio, 0, funding)
                
                # SHORT: Tendência de baixa + candles bearish (filtros MAIS apertados)
                short_volume_ok = volume_ratio > self.short_volume_threshold
                short_bearish_ok = self.bearish_count >= self.short_min_bearish_candles
                
                if self.short_enabled and not price_above_sma and short_bearish_ok and short_volume_ok and funding_ok and price > 0:
                    if oi > 0:
                        if oi_change < -self.oi_threshold:
                            self._enter_short(symbol, price, timestamp, volume_ratio, oi_change, funding)
                    else:
                        self._enter_short(symbol, price, timestamp, volume_ratio, 0, funding)
            
            # SINAL DE SAÍDA LONG
            elif self.current_position == 'long':
                gain_pct = (price - self.entry_price) / self.entry_price
                
                if price > self.max_price_since_entry:
                    self.max_price_since_entry = price
                
                if gain_pct >= self.trailing_activation_pct and not self.trailing_active:
                    self.trailing_active = True
                    self.trailing_stop_price = self.max_price_since_entry * (1 - self.trailing_stop_pct)
                    dt = datetime.fromtimestamp(timestamp / 1000)
                    logger.info(f"[{dt}] 🚀 TRAILING LONG ATIVADO! Lucro: +{gain_pct*100:.1f}% | Stop: ${self.trailing_stop_price:,.2f}")
                
                if self.trailing_active:
                    new_trailing = self.max_price_since_entry * (1 - self.trailing_stop_pct)
                    if new_trailing > self.trailing_stop_price:
                        self.trailing_stop_price = new_trailing
                        if gain_pct > 0.02:
                            dt = datetime.fromtimestamp(timestamp / 1000)
                            logger.info(f"[{dt}] 🎯 LONG MÁX ${price:,.2f} | Trail: ${self.trailing_stop_price:,.2f} | +{gain_pct*100:.1f}%")
                
                if not self.trailing_active:
                    loss_pct = (self.entry_price - price) / self.entry_price
                    if loss_pct >= self.stop_loss_pct:
                        self._exit_position(symbol, price, timestamp, 'STOP_LOSS')
                        continue
                
                if self.trailing_active and price <= self.trailing_stop_price:
                    self._exit_position(symbol, price, timestamp, 'TRAILING_STOP')
                    continue
            
            # SINAL DE SAÍDA SHORT
            elif self.current_position == 'short':
                gain_pct = (self.entry_price - price) / self.entry_price
                
                if price < self.min_price_since_entry:
                    self.min_price_since_entry = price
                
                if gain_pct >= self.trailing_activation_pct and not self.trailing_active:
                    self.trailing_active = True
                    self.trailing_stop_price = self.min_price_since_entry * (1 + self.trailing_stop_pct)
                    dt = datetime.fromtimestamp(timestamp / 1000)
                    logger.info(f"[{dt}] 🚀 TRAILING SHORT ATIVADO! Lucro: +{gain_pct*100:.1f}% | Stop: ${self.trailing_stop_price:,.2f}")
                
                if self.trailing_active:
                    new_trailing = self.min_price_since_entry * (1 + self.trailing_stop_pct)
                    if new_trailing < self.trailing_stop_price:
                        self.trailing_stop_price = new_trailing
                        if gain_pct > 0.02:
                            dt = datetime.fromtimestamp(timestamp / 1000)
                            logger.info(f"[{dt}] 🎯 SHORT MÍN ${price:,.2f} | Trail: ${self.trailing_stop_price:,.2f} | +{gain_pct*100:.1f}%")
                
                if not self.trailing_active:
                    loss_pct = (price - self.entry_price) / self.entry_price
                    if loss_pct >= self.short_stop_loss_pct:
                        self._exit_position(symbol, price, timestamp, 'STOP_LOSS')
                        continue
                
                if self.trailing_active and price >= self.trailing_stop_price:
                    self._exit_position(symbol, price, timestamp, 'TRAILING_STOP')
                    continue
            
            # Guardar equity curve a cada candle
            self.equity.append({
                'timestamp': timestamp,
                'equity': self.current_capital
            })
        
        # Fechar posição aberta
        if self.current_position:
            last_candle = data[-1]
            self._exit_position(symbol, last_candle['close'], last_candle['timestamp'], 'END_OF_DATA')
        
        metrics = self._calculate_metrics()
        
        # Guardar trades na DB
        if save_to_db:
            for trade in self.trades:
                if trade['type'] == 'exit':
                    self.db.save_trade(trade)
        
        return metrics
    
    def _enter_long(self, symbol: str, price: float, timestamp: int, 
                    volume_ratio: float, oi_change: float, funding: float):
        """Simula entrada LONG"""
        self.current_position = 'long'
        self.entry_price = price
        self.entry_time = timestamp
        
        # Inicializar trailing stop tracking
        self.max_price_since_entry = price
        self.min_price_since_entry = price
        self.trailing_stop_price = price * (1 - self.stop_loss_pct)
        self.trailing_active = False  # Começa desativado, ativa após lucro mínimo
        
        # Tamanho da posição (10% do capital, max position size)
        position_size = min(self.max_position_usd, self.current_capital * 0.1)
        
        # Aplicar fee de entrada
        fee = position_size * self.fee_pct
        self.current_capital -= fee
        
        dt = datetime.fromtimestamp(timestamp / 1000)
        logger.info(
            f"[ENTER LONG {symbol}] {dt} @ ${price:,.2f} | Size: ${position_size:.2f} | "
            f"Vol: {volume_ratio:.2f}x | OI: +{oi_change*100:.2f}% | "
            f"Trailing: ${self.trailing_stop_price:,.2f}"
        )
        
        self.trades.append({
            'type': 'entry',
            'symbol': symbol,
            'direction': 'long',
            'price': price,
            'timestamp': timestamp,
            'size_usd': position_size,
            'volume_ratio': volume_ratio,
            'oi_change': oi_change,
            'funding': funding
        })
    
    def _enter_short(self, symbol: str, price: float, timestamp: int,
                     volume_ratio: float, oi_change: float, funding: float):
        """Simula entrada SHORT"""
        self.current_position = 'short'
        self.entry_price = price
        self.entry_time = timestamp
        
        # Inicializar trailing stop tracking para short
        self.max_price_since_entry = price
        self.min_price_since_entry = price
        self.trailing_stop_price = price * (1 + self.short_stop_loss_pct)
        self.trailing_active = False
        
        # Tamanho da posição (10% do capital)
        position_size = min(self.max_position_usd, self.current_capital * 0.1)
        
        # Aplicar fee de entrada
        fee = position_size * self.fee_pct
        self.current_capital -= fee
        
        dt = datetime.fromtimestamp(timestamp / 1000)
        logger.info(
            f"[ENTER SHORT {symbol}] {dt} @ ${price:,.2f} | Size: ${position_size:.2f} | "
            f"Vol: {volume_ratio:.2f}x | OI: {oi_change*100:.2f}% | "
            f"Trailing: ${self.trailing_stop_price:,.2f}"
        )
        
        self.trades.append({
            'type': 'entry',
            'symbol': symbol,
            'direction': 'short',
            'price': price,
            'timestamp': timestamp,
            'size_usd': position_size,
            'volume_ratio': volume_ratio,
            'oi_change': oi_change,
            'funding': funding
        })
    
    def _exit_position(self, symbol: str, price: float, timestamp: int, reason: str):
        """Simula saída da posição"""
        if not self.current_position:
            return
        
        # Encontrar trade de entrada correspondente
        entry_trade = None
        for t in reversed(self.trades):
            if t['type'] == 'entry' and t['symbol'] == symbol:
                entry_trade = t
                break
        
        if not entry_trade:
            return
        
        position_size = entry_trade['size_usd']
        direction = entry_trade.get('direction', 'long')
        
        # Calcular PnL (invertido para shorts)
        if direction == 'short':
            pnl_pct = (self.entry_price - price) / self.entry_price
        else:
            pnl_pct = (price - self.entry_price) / self.entry_price
        pnl_usd = position_size * pnl_pct
        
        # Aplicar fee de saída
        fee = position_size * self.fee_pct
        self.current_capital -= fee
        self.current_capital += pnl_usd
        
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
        
        # Emoji baseado no resultado
        emoji = "✅" if pnl_usd > 0 else "❌"
        
        logger.info(
            f"[{emoji} EXIT {symbol}] {dt} @ ${price:,.2f} | "
            f"PnL: ${pnl_usd:+.2f} ({pnl_pct*100:+.2f}%) | "
            f"Reason: {reason} | Capital: ${self.current_capital:,.2f}"
        )
        
        self.trades.append({
            'type': 'exit',
            'symbol': symbol,
            'direction': direction,
            'entry_price': self.entry_price,
            'exit_price': price,
            'entry_time': self.entry_time,
            'exit_time': timestamp,
            'size_usd': position_size,
            'pnl_usd': pnl_usd,
            'pnl_pct': pnl_pct,
            'exit_reason': reason,
            'max_price_reached': self.max_price_since_entry,
            'min_price_reached': self.min_price_since_entry
        })
        
        # Reset trailing stop
        self.current_position = None
        self.entry_price = 0
        self.max_price_since_entry = 0
        self.trailing_stop_price = 0
    
    def _calculate_metrics(self) -> Dict:
        """Calcula métricas finais com separação Long/Short"""
        if self.total_trades == 0:
            return {
                'total_trades': 0,
                'message': 'Nenhum trade executado'
            }
        
        total_pnl = self.current_capital - self.initial_capital
        total_return = (total_pnl / self.initial_capital) * 100
        
        exits = [t for t in self.trades if t['type'] == 'exit']
        wins = [t['pnl_usd'] for t in exits if t['pnl_usd'] > 0]
        losses = [abs(t['pnl_usd']) for t in exits if t['pnl_usd'] <= 0]
        
        # Separar por direção
        longs = [t for t in exits if t.get('direction', 'long') == 'long']
        shorts = [t for t in exits if t.get('direction', 'long') == 'short']
        
        long_wins = [t['pnl_usd'] for t in longs if t['pnl_usd'] > 0]
        long_losses = [abs(t['pnl_usd']) for t in longs if t['pnl_usd'] <= 0]
        short_wins = [t['pnl_usd'] for t in shorts if t['pnl_usd'] > 0]
        short_losses = [abs(t['pnl_usd']) for t in shorts if t['pnl_usd'] <= 0]
        
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        
        profit_factor = sum(wins) / sum(losses) if losses and sum(losses) > 0 else float('inf')
        
        long_pf = sum(long_wins) / sum(long_losses) if long_losses and sum(long_losses) > 0 else float('inf')
        short_pf = sum(short_wins) / sum(short_losses) if short_losses and sum(short_losses) > 0 else float('inf')
        
        win_rate = (self.winning_trades / self.total_trades) * 100
        
        # Sharpe simplificado (assumindo risk-free = 0)
        returns = []
        for i in range(1, len(self.equity)):
            if self.equity[i-1]['equity'] > 0:
                r = (self.equity[i]['equity'] - self.equity[i-1]['equity']) / self.equity[i-1]['equity']
                returns.append(r)
        
        sharpe = 0
        if returns and len(returns) > 1:
            avg_return = sum(returns) / len(returns)
            variance = sum((r - avg_return) ** 2 for r in returns) / (len(returns) - 1)
            std_dev = variance ** 0.5 if variance > 0 else 0
            sharpe = (avg_return / std_dev) * (252 ** 0.5) if std_dev > 0 else 0
        
        return {
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
            'sharpe_ratio': sharpe,
            'trades': self.trades,
            'equity_curve': self.equity,
            'longs': {
                'count': len(longs),
                'win_rate': len(long_wins) / len(longs) * 100 if longs else 0,
                'profit_factor': long_pf,
                'pnl': sum(t['pnl_usd'] for t in longs)
            },
            'shorts': {
                'count': len(shorts),
                'win_rate': len(short_wins) / len(shorts) * 100 if shorts else 0,
                'profit_factor': short_pf,
                'pnl': sum(t['pnl_usd'] for t in shorts)
            }
        }
    
    def print_report(self, metrics: Dict, symbol: str = "BTC"):
        """Imprime relatório formatado com separação Long/Short"""
        print("\n" + "="*70)
        print(f"          BACKTEST REPORT - {symbol}")
        print("="*70)
        
        if metrics.get('total_trades', 0) == 0:
            print(metrics.get('message', 'Sem resultados'))
            print("="*70)
            return
        
        print(f"Capital Inicial:     ${metrics['initial_capital']:>12,.2f}")
        print(f"Capital Final:       ${metrics['final_capital']:>12,.2f}")
        print(f"PnL Total:           ${metrics['total_pnl']:>12,.2f} ({metrics['total_return_pct']:+.2f}%)")
        print(f"-"*70)
        print(f"Total Trades:        {metrics['total_trades']:>12}")
        print(f"Win Rate:            {metrics['win_rate']:>11.1f}%")
        print(f"Profit Factor:       {metrics['profit_factor']:>12.2f}")
        print(f"Avg Win:             ${metrics['avg_win']:>12,.2f}")
        print(f"Avg Loss:            ${metrics['avg_loss']:>12,.2f}")
        print(f"Max Drawdown:        {metrics['max_drawdown_pct']:>11.2f}%")
        print(f"Sharpe Ratio:        {metrics['sharpe_ratio']:>12.2f}")
        print(f"-"*70)
        
        # Separar por direção
        longs = metrics.get('longs', {})
        shorts = metrics.get('shorts', {})
        
        if longs.get('count', 0) > 0:
            print(f"[LONGS]              {longs['count']:>12} trades")
            print(f"  Win Rate:          {longs['win_rate']:>11.1f}%")
            print(f"  Profit Factor:     {longs['profit_factor']:>12.2f}")
            print(f"  PnL:               ${longs['pnl']:>12,.2f}")
        
        if shorts.get('count', 0) > 0:
            print(f"[SHORTS]             {shorts['count']:>12} trades")
            print(f"  Win Rate:          {shorts['win_rate']:>11.1f}%")
            print(f"  Profit Factor:     {shorts['profit_factor']:>12.2f}")
            print(f"  PnL:               ${shorts['pnl']:>12,.2f}")
        
        print("="*70)
        
        # Veredito
        pf = metrics['profit_factor']
        dd = metrics['max_drawdown_pct']
        wr = metrics['win_rate']
        
        if pf > 1.5 and dd < 20 and wr > 40:
            print("[✅ PASS] Estratégia VÁLIDA - pronta para forward test")
        elif pf > 1.2 and dd < 30:
            print("[⚠️ WARNING] Edge positivo mas precisa de refinamento")
        else:
            print("[❌ FAIL] Estratégia PERDE DINHEIRO - NÃO usar em live")
        
        print("="*70)


def run_quick_backtest(config: Dict, symbol: str = "BTC", interval: str = "15m", 
                        days: int = 30):
    """
    Função helper para correr backtest rápido
    """
    db = BotDatabase()
    engine = BacktestEngineDB(config, db)
    
    # Verificar se temos dados
    stats = db.get_stats()
    if stats.get('candles', 0) == 0:
        print("\n" + "!"*60)
        print("AVISO: Base de dados VAZIA!")
        print("Executa primeiro: python src/data_downloader.py")
        print("!"*60)
        return None
    
    # Correr backtest
    metrics = engine.run(symbol, interval, days)
    
    if 'error' in metrics:
        print(f"\nErro: {metrics['error']}")
        return None
    
    # Imprimir relatório
    engine.print_report(metrics, symbol)
    
    # Guardar resultados JSON
    results_dir = Path("backtest_results")
    results_dir.mkdir(exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    results_file = results_dir / f"backtest_{symbol}_{interval}_{timestamp}.json"
    
    # Limpar trades da equity curve para JSON não ficar gigante
    export_metrics = {k: v for k, v in metrics.items() if k != 'trades'}
    export_metrics['trade_count'] = len(metrics.get('trades', []))
    
    with open(results_file, 'w') as f:
        json.dump(export_metrics, f, indent=2, default=str)
    
    print(f"\nResultados guardados: {results_file}")
    
    return metrics


if __name__ == "__main__":
    import sys
    from utils import load_config
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    config = load_config()
    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTC"
    interval = sys.argv[2] if len(sys.argv) > 2 else "15m"
    days = int(sys.argv[3]) if len(sys.argv) > 3 else 30
    
    run_quick_backtest(config, symbol, interval, days)
