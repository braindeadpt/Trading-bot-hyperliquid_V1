"""
Paper Trading Engine v2 - Baixa latência para trailing stops
Separa signal generation (15m) de position monitoring (rápido)
"""
import logging
import time
import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional
from collections import deque

from data_aggregator import DataAggregator
from database import BotDatabase
from strategy import MomentumStrategy
from utils import load_config

logger = logging.getLogger(__name__)


class AutoTuner:
    """
    Ajusta automaticamente thresholds baseado em performance
    Guarda histórico de trades e otimiza parâmetros
    """
    
    def __init__(self, config: Dict, db: BotDatabase):
        self.config = config
        self.db = db
        self.strat_config = config.get('strategy', {})
        
        # Janela de lookback para análise (últimos N trades)
        self.lookback_trades = 50
        
        # Thresholds atuais (começam com os do config)
        self.volume_threshold = self.strat_config.get('volume_spike_threshold', 2.5)
        self.oi_threshold = self.strat_config.get('oi_change_threshold', 0.015)
        self.stop_loss = config.get('risk', {}).get('stop_loss_pct', 0.02)
        
        # Limites de ajuste (não deixar fugir muito)
        self.min_volume = 2.0
        self.max_volume = 5.0
        self.min_oi = 0.005
        self.max_oi = 0.05
        
        # Histórico para análise
        self.trade_history = deque(maxlen=self.lookback_trades)
        
        # Carregar últimos trades da DB
        self._load_history()
    
    def _load_history(self):
        """Carrega últimos trades da base de dados"""
        try:
            # Verificar se tabela existe
            conn = self.db._get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='paper_trades'")
            if not cursor.fetchone():
                conn.close()
                return
            
            cursor.execute('''
                SELECT entry_time, exit_time, pnl_pct, exit_reason, side 
                FROM paper_trades 
                WHERE exit_time IS NOT NULL 
                ORDER BY exit_time DESC 
                LIMIT ?
            ''', (self.lookback_trades,))
            
            rows = cursor.fetchall()
            conn.close()
            
            for row in rows:
                self.trade_history.append({
                    'entry_time': row[0],
                    'exit_time': row[1],
                    'pnl_pct': row[2],
                    'exit_reason': row[3],
                    'side': row[4]
                })
            
            logger.info(f"AutoTuner: {len(self.trade_history)} trades carregados")
            
        except Exception as e:
            logger.warning(f"Erro a carregar histórico: {e}")
    
    def record_trade(self, side: str, pnl_pct: float, exit_reason: str):
        """Regista um trade para análise futura"""
        self.trade_history.append({
            'entry_time': datetime.now().isoformat(),
            'pnl_pct': pnl_pct,
            'exit_reason': exit_reason,
            'side': side
        })
    
    def analyze_and_tune(self) -> Dict:
        """
        Analisa últimos trades e sugere ajustes
        Retorna dict com thresholds otimizados
        """
        if len(self.trade_history) < 10:
            logger.info(f"AutoTuner: Só temos {len(self.trade_history)} trades, precisamos de 10+ para otimizar")
            return {
                'volume_threshold': self.volume_threshold,
                'oi_threshold': self.oi_threshold,
                'stop_loss': self.stop_loss,
                'tuned': False
            }
        
        # Separar wins e losses
        wins = [t for t in self.trade_history if t['pnl_pct'] > 0]
        losses = [t for t in self.trade_history if t['pnl_pct'] <= 0]
        
        win_rate = len(wins) / len(self.trade_history)
        avg_win = sum(t['pnl_pct'] for t in wins) / max(1, len(wins))
        avg_loss = sum(t['pnl_pct'] for t in losses) / max(1, len(losses))
        
        logger.info(f"AutoTuner Análise ({len(self.trade_history)} trades):")
        logger.info(f"  Win Rate: {win_rate*100:.1f}%")
        logger.info(f"  Avg Win: +{avg_win*100:.2f}%")
        logger.info(f"  Avg Loss: {avg_loss*100:.2f}%")
        
        # Estatísticas por razão de saída
        stop_loss_trades = [t for t in losses if t['exit_reason'] == 'STOP_LOSS']
        if len(stop_loss_trades) > len(losses) * 0.7:
            logger.info("  🚨 Muitos stop losses! Pode ser volatilidade excessiva")
        
        # Decisões de ajuste
        adjustments = {}
        
        # 1. Volume threshold
        if win_rate < 0.45:
            # Perdendo muito? Apertar filtros
            new_vol = min(self.volume_threshold * 1.1, self.max_volume)
            adjustments['volume_threshold'] = new_vol
            logger.info(f"  📉 Win rate baixo: Volume threshold {self.volume_threshold:.1f} -> {new_vol:.1f}")
        elif win_rate > 0.65 and avg_win > abs(avg_loss) * 1.5:
            # Muito bem? Relaxar filtros para mais oportunidades
            new_vol = max(self.volume_threshold * 0.9, self.min_volume)
            adjustments['volume_threshold'] = new_vol
            logger.info(f"  📈 Performance forte: Volume threshold {self.volume_threshold:.1f} -> {new_vol:.1f}")
        
        # 2. OI threshold
        if win_rate > 0.55:
            # Podemos ser mais sensíveis ao OI
            new_oi = max(self.oi_threshold * 0.9, self.min_oi)
            adjustments['oi_threshold'] = new_oi
            logger.info(f"  📈 OI threshold {self.oi_threshold:.3f} -> {new_oi:.3f}")
        
        # Aplicar ajustes
        if 'volume_threshold' in adjustments:
            self.volume_threshold = adjustments['volume_threshold']
        if 'oi_threshold' in adjustments:
            self.oi_threshold = adjustments['oi_threshold']
        
        return {
            'volume_threshold': self.volume_threshold,
            'oi_threshold': self.oi_threshold,
            'stop_loss': self.stop_loss,
            'win_rate': win_rate,
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'tuned': len(adjustments) > 0,
            'adjustments': adjustments
        }


class PaperTrader:
    """
    Bot de paper trading em tempo real
    Corre a cada X minutos, busca dados, executa sinais com dinheiro virtual
    """
    
    def __init__(self, config: Dict):
        self.config = config
        self.assets = config.get('assets', ['BTC'])
        self.timeframes = config.get('timeframes', {})
        self.primary_tf = self.timeframes.get('primary', '5m')
        
        # Capital virtual
        self.initial_capital = config.get('risk', {}).get('initial_capital', 10000.0)
        self.capital = self.initial_capital
        self.max_position_usd = config.get('risk', {}).get('max_position_size_usd', 100)
        self.max_leverage = config.get('risk', {}).get('max_leverage', 2)
        self.fee_pct = 0.00035  # 0.035% Hyperliquid taker
        
        # Componentes
        self.aggregator = DataAggregator(config)
        self.db = BotDatabase()
        self.strategy = MomentumStrategy(config)
        self.tuner = AutoTuner(config, self.db)
        
        # Estado
        self.current_position = None  # None, 'long', 'short'
        self.entry_price = 0
        self.entry_time = None
        self.position_size = 0
        self.max_price = 0
        self.min_price = 0
        self.trailing_active = False
        self.trailing_stop = 0
        self.trade_count = 0
        self.daily_trades = 0
        self.last_day = None
        
        # Candles em memória (últimos 100)
        self.candles = deque(maxlen=100)
        
        # Config de trailing
        risk = config.get('risk', {})
        self.trailing_activation = risk.get('trailing_activation_pct', 0.015)
        self.trailing_pct = risk.get('trailing_stop_pct', 0.015)
        self.stop_loss_pct = risk.get('stop_loss_pct', 0.02)
        self.short_stop_loss = risk.get('short_stop_loss_pct', 0.025)
        
        # Estratégia adaptativa
        strat = config.get('strategy', {})
        self.market_regime_enabled = strat.get('market_regime_enabled', True)
        self.price_sma_period = strat.get('price_sma_period', 60)
        self.min_bullish = strat.get('min_bullish_candles', 2)
        self.min_bearish = strat.get('min_bearish_candles', 2)
        self.short_enabled = strat.get('short_enabled', True)
        self.short_volume_mult = strat.get('short_volume_multiplier', 1.2)
        self.short_bearish_add = strat.get('short_bearish_add', 1)
        
        # Threading para monitorização rápida
        self._lock = threading.Lock()
        self._monitor_thread = None
        self._monitor_running = False
        self._monitor_interval = 10  # segundos — verifica trailing stop a cada 10s
        self._last_price = 0
        
        # Estatísticas de latência
        self.latency_ms = 0
        self.last_check_time = 0
        
        # ==================================================================
        # MULTI-TIMEFRAME — TF Baixo para deteção rápida de spikes
        # ==================================================================
        self.secondary_tf = self.timeframes.get('secondary', '5m')  # 5m para confirmação rápida
        self._mtf_thread = None
        self._mtf_running = False
        self._mtf_interval = 60  # segundos — busca candle 1m a cada minuto
        self._low_tf_candles = deque(maxlen=100)  # Buffer de candles do TF baixo
        self._last_mtf_signal = None  # Último sinal do TF baixo
        self._mtf_cooldown = 0  # Evitar entradas duplicadas
        
        # Direção do TF alto (15m) — atualizada a cada 15min
        self._htf_direction = None  # 'bull', 'bear', 'neutral'
        self._htf_sma = 0
        self._htf_price = 0
        
        # Criar tabela de paper trades se não existir
        self._init_paper_trades_table()
    
    def _init_paper_trades_table(self):
        """Cria tabela para guardar trades de paper trading"""
        conn = self.db._get_conn()
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_time TEXT NOT NULL,
                entry_price REAL NOT NULL,
                position_size REAL NOT NULL,
                exit_time TEXT,
                exit_price REAL,
                pnl_usd REAL,
                pnl_pct REAL,
                exit_reason TEXT,
                volume_ratio REAL,
                oi_change REAL,
                funding_rate REAL,
                market_regime TEXT
            )
        ''')
        
        conn.commit()
        conn.close()
        logger.info("Tabela paper_trades inicializada")
    
    def _calculate_sma(self, prices: list, period: int) -> float:
        """Calcula média móvel simples"""
        if len(prices) < period:
            return sum(prices) / max(1, len(prices))
        return sum(prices[-period:]) / period
    
    # ==================================================================
    # MONITOR RÁPIDO — Thread de baixa latência para trailing/SL
    # ==================================================================
    
    def _start_monitor_thread(self, asset: str):
        """Inicia thread de monitorização rápida"""
        self._monitor_running = True
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            args=(asset,),
            daemon=True,
            name="FastMonitor"
        )
        self._monitor_thread.start()
        logger.info(f"🚀 Monitor rápido iniciado: {self._monitor_interval}s intervalo")
        
        # Iniciar thread de multi-timeframe (TF baixo para spikes rápidos)
        self._start_mtf_thread(asset)
    
    def _start_mtf_thread(self, asset: str):
        """Inicia thread de multi-timeframe para detetar spikes no TF baixo"""
        self._mtf_running = True
        self._mtf_thread = threading.Thread(
            target=self._mtf_loop,
            args=(asset,),
            daemon=True,
            name="MultiTimeframe"
        )
        self._mtf_thread.start()
        logger.info(f"📊 Multi-Timeframe iniciado: {self.secondary_tf} a cada {self._mtf_interval}s")
    
    def _mtf_loop(self, asset: str):
        """Loop de multi-timeframe — busca candles do TF baixo e deteta spikes"""
        while self._mtf_running:
            try:
                start_time = time.time()
                self._process_low_tf_candle(asset)
                elapsed = time.time() - start_time
                
                sleep_time = max(0, self._mtf_interval - elapsed)
                if sleep_time > 0:
                    time.sleep(sleep_time)
                    
            except Exception as e:
                logger.error(f"Erro no MTF loop: {e}")
                time.sleep(self._mtf_interval)
    
    def _process_low_tf_candle(self, asset: str):
        """Processa candle do TF baixo (1m ou 5m) para detetar spikes em tempo real"""
        try:
            # Buscar candle do TF baixo
            result = self._fetch_low_tf_candle(asset)
            if not result:
                return
            
            candle = result['candle']
            self._low_tf_candles.append(candle)
            
            # Precisamos de pelo menos 20 candles para calcular média
            if len(self._low_tf_candles) < 20:
                return
            
            # Calcular volume ratio no TF baixo
            volumes = [c['volume'] for c in self._low_tf_candles]
            avg_volume = sum(volumes[-20:]) / 20
            current_volume = candle['volume']
            volume_ratio = current_volume / max(avg_volume, 1)
            
            price = candle['close']
            
            # Verificar se há direção do TF alto (15m)
            if self._htf_direction is None:
                return  # Ainda não temos direção do TF alto
            
            # Verificar cooldown (evitar entradas duplicadas)
            current_time = time.time()
            if current_time < self._mtf_cooldown:
                return
            
            # Só entra se não estiver em posição
            if self.current_position is not None:
                return
            
            # DETETAR SPIKE NO TF BAIXO + CONFIRMAÇÃO TF ALTO
            
            # LONG: TF alto bullish + TF baixo volume spike + candle bullish
            if self._htf_direction == 'bull' and volume_ratio >= self.tuner.volume_threshold:
                # Verificar se candle é bullish
                if price > candle['open']:
                    # Verificar funding
                    funding = candle.get('funding', 0)
                    max_funding = self.config.get('strategy', {}).get('max_funding_rate', 0.01)
                    min_funding = self.config.get('strategy', {}).get('min_funding_rate', -0.01)
                    
                    if not (funding > max_funding or funding < min_funding):
                        logger.info(f"⚡ MTF SIGNAL: LONG spike no {self.secondary_tf}! "
                                   f"Vol: {volume_ratio:.1f}x | Price: ${price:,.0f} | "
                                   f"HTF: {self._htf_direction}")
                        
                        # ENTRAR IMEDIATAMENTE!
                        with self._lock:
                            self._enter_position(asset, 'long', price, candle, self._htf_direction)
                        
                        # Cooldown de 5 minutos para evitar re-entrada
                        self._mtf_cooldown = current_time + 300
            
            # SHORT: TF alto bearish + TF baixo volume spike + candle bearish
            elif self._htf_direction == 'bear' and volume_ratio >= self.tuner.volume_threshold:
                if price < candle['open']:
                    funding = candle.get('funding', 0)
                    max_funding = self.config.get('strategy', {}).get('max_funding_rate', 0.01)
                    min_funding = self.config.get('strategy', {}).get('min_funding_rate', -0.01)
                    
                    if not (funding > max_funding or funding < min_funding):
                        logger.info(f"⚡ MTF SIGNAL: SHORT spike no {self.secondary_tf}! "
                                   f"Vol: {volume_ratio:.1f}x | Price: ${price:,.0f} | "
                                   f"HTF: {self._htf_direction}")
                        
                        with self._lock:
                            self._enter_position(asset, 'short', price, candle, self._htf_direction)
                        
                        self._mtf_cooldown = current_time + 300
            
            # Log periódico do TF baixo
            if len(self._low_tf_candles) % 10 == 0:
                logger.info(f"📈 {self.secondary_tf} | Price: ${price:,.0f} | "
                           f"Vol: {volume_ratio:.1f}x | HTF: {self._htf_direction} | "
                           f"Candles: {len(self._low_tf_candles)}")
                    
        except Exception as e:
            logger.warning(f"Erro no processamento MTF: {e}")
    
    def _fetch_low_tf_candle(self, asset: str) -> Optional[Dict]:
        """Busca candle do timeframe baixo (1m ou 5m)"""
        try:
            # Usar data_aggregator para buscar preço atual
            data = self.aggregator.fetch_all_data(asset)
            if not data:
                return None
            
            current_price = data.get('price', 0)
            if current_price <= 0:
                return None
            
            # Simular candle do TF baixo com dados atuais
            # Em produção real, deveria buscar candles históricos do TF baixo
            now = datetime.now()
            
            # Estimar OHLC do último período do TF baixo
            # Na prática, precisaríamos de dados reais de 1m/5m
            candle = {
                'timestamp': int(now.timestamp() * 1000),
                'time': now.strftime('%H:%M:%S'),
                'open': current_price * 0.999,  # Simulado
                'high': current_price * 1.001,
                'low': current_price * 0.998,
                'close': current_price,
                'volume': data.get('volume_total', 0) / (1440 if self.secondary_tf == '1m' else 288),  # Estimativa
                'oi': data.get('oi_total', 0),
                'funding': data.get('funding_avg', 0)
            }
            
            return {'candle': candle, 'data': data}
            
        except Exception as e:
            logger.warning(f"Erro a buscar candle MTF: {e}")
            return None
    
    def _monitor_loop(self, asset: str):
        """Loop de monitorização rápida — verifica preço a cada N segundos"""
        while self._monitor_running:
            try:
                start_time = time.time()
                self._fast_price_check(asset)
                elapsed = time.time() - start_time
                self.latency_ms = elapsed * 1000
                
                # Sleep restante do intervalo
                sleep_time = max(0, self._monitor_interval - elapsed)
                if sleep_time > 0:
                    time.sleep(sleep_time)
                    
            except Exception as e:
                logger.error(f"Erro no monitor rápido: {e}")
                time.sleep(self._monitor_interval)
    
    def _fast_price_check(self, asset: str):
        """Verifica preço rápido e gere trailing stop/SL"""
        # Só importa se estivermos em posição
        if not self.current_position:
            return
        
        try:
            # Buscar preço atual (cache se possível)
            price = self.aggregator.get_cached_price(asset, max_age_seconds=30)
            
            if price <= 0:
                # Fallback: buscar novo preço
                data = self.aggregator.fetch_all_data(asset)
                if data and data.get('exchanges_data'):
                    prices = [d['mark_price'] for d in data['exchanges_data'].values() 
                             if d.get('mark_price', 0) > 0]
                    if prices:
                        price = sum(prices) / len(prices)
            
            if price <= 0:
                return
            
            self._last_price = price
            self.last_check_time = time.time()
            
            with self._lock:
                exit_reason = self._check_exit_signals_fast(price)
                if exit_reason:
                    self._exit_position(asset, price, exit_reason, {'close': price})
                    
        except Exception as e:
            logger.warning(f"Erro no fast check: {e}")
    
    def _check_exit_signals_fast(self, price: float) -> Optional[str]:
        """Versão rápida de verificação de saída (sem candles, só preço)"""
        if self.current_position == 'long':
            gain_pct = (price - self.entry_price) / self.entry_price
            
            # Atualizar máximo
            if price > self.max_price:
                self.max_price = price
            
            # Ativar trailing?
            if gain_pct >= self.trailing_activation and not self.trailing_active:
                self.trailing_active = True
                self.trailing_stop = self.max_price * (1 - self.trailing_pct)
                logger.info(f"🚀 TRAIL ATIVADO! +{gain_pct*100:.1f}% | Stop: ${self.trailing_stop:,.0f} | Price: ${price:,.0f}")
            
            # Atualizar trailing stop
            if self.trailing_active:
                new_stop = self.max_price * (1 - self.trailing_pct)
                if new_stop > self.trailing_stop:
                    self.trailing_stop = new_stop
                    if self.last_check_time % 60 < self._monitor_interval:  # Log a cada ~minuto
                        logger.info(f"📈 Trail UP: ${self.trailing_stop:,.0f} | Max: ${self.max_price:,.0f} | Price: ${price:,.0f}")
            
            # Check stop loss (antes de trailing ativar)
            if not self.trailing_active:
                loss_pct = (self.entry_price - price) / self.entry_price
                if loss_pct >= self.stop_loss_pct:
                    return 'STOP_LOSS'
            
            # Check trailing stop
            if self.trailing_active and price <= self.trailing_stop:
                return 'TRAILING_STOP'
        
        elif self.current_position == 'short':
            gain_pct = (self.entry_price - price) / self.entry_price
            
            if price < self.min_price:
                self.min_price = price
            
            if gain_pct >= self.trailing_activation and not self.trailing_active:
                self.trailing_active = True
                self.trailing_stop = self.min_price * (1 + self.trailing_pct)
                logger.info(f"🚀 TRAIL SHORT ATIVADO! +{gain_pct*100:.1f}% | Stop: ${self.trailing_stop:,.0f}")
            
            if self.trailing_active:
                new_stop = self.min_price * (1 + self.trailing_pct)
                if new_stop < self.trailing_stop:
                    self.trailing_stop = new_stop
            
            if not self.trailing_active:
                loss_pct = (price - self.entry_price) / self.entry_price
                if loss_pct >= self.short_stop_loss:
                    return 'STOP_LOSS'
            
            if self.trailing_active and price >= self.trailing_stop:
                return 'TRAILING_STOP'
        
        return None
    
    def _detect_market_regime(self, prices: list) -> str:
        """
        Deteta regime de mercado:
        - 'bull': preço > SMA200
        - 'bear': preço < SMA200
        - 'ranging': preço próximo da SMA200
        """
        if len(prices) < 200:
            # Sem dados suficientes, usar SMA60
            sma = self._calculate_sma(prices, min(len(prices), 60))
        else:
            sma = self._calculate_sma(prices, 200)
        
        current_price = prices[-1]
        diff_pct = (current_price - sma) / sma
        
        if diff_pct > 0.05:  # 5% acima da SMA
            return 'bull'
        elif diff_pct < -0.05:  # 5% abaixo da SMA
            return 'bear'
        else:
            return 'ranging'
    
    def _get_thresholds_for_regime(self, regime: str, side: str) -> Dict:
        """Retorna thresholds ajustados para o regime atual"""
        base_volume = self.tuner.volume_threshold
        base_oi = self.tuner.oi_threshold
        
        if regime == 'bull':
            if side == 'long':
                return {'volume': base_volume, 'oi': base_oi, 'candles': self.min_bullish}
            else:  # short em bull market = mais difícil
                return {
                    'volume': base_volume * self.short_volume_mult * 1.2,
                    'oi': base_oi * 1.3,
                    'candles': self.min_bearish + self.short_bearish_add + 1
                }
        elif regime == 'bear':
            if side == 'short':
                return {'volume': base_volume, 'oi': base_oi, 'candles': self.min_bearish}
            else:  # long em bear market = mais difícil
                return {
                    'volume': base_volume * self.short_volume_mult * 1.2,
                    'oi': base_oi * 1.3,
                    'candles': self.min_bullish + self.short_bearish_add + 1
                }
        else:  # ranging
            return {'volume': base_volume * 1.1, 'oi': base_oi, 'candles': max(self.min_bullish, self.min_bearish)}
    
    def fetch_and_process_candle(self, asset: str) -> Optional[Dict]:
        """Busca dados de mercado e forma um candle de 5m"""
        try:
            # Buscar dados agregados
            data = self.aggregator.fetch_all_data(asset)
            if not data:
                logger.warning(f"Sem dados para {asset}")
                return None
            
            # Calcular preço médio
            prices = [d['mark_price'] for d in data['exchanges_data'].values() if d.get('mark_price', 0) > 0]
            if not prices:
                return None
            
            current_price = sum(prices) / len(prices)
            current_time = datetime.now()
            
            # Buscar último candle da DB para completar dados
            conn = self.db._get_conn()
            cursor = conn.cursor()
            
            # Tentar obter último preço para calcular OHLC
            cursor.execute('''
                SELECT close, volume FROM candles 
                WHERE symbol=? AND interval=? 
                ORDER BY timestamp DESC LIMIT 1
            ''', (asset, self.primary_tf))
            
            last_row = cursor.fetchone()
            conn.close()
            
            if last_row:
                last_close = last_row[0]
                last_volume = last_row[1]
            else:
                last_close = current_price
                last_volume = 0
            
            # Simular candle com dados atuais
            # Em produção real, usaríamos websocket para dados tick-by-tick
            candle = {
                'timestamp': int(current_time.timestamp() * 1000),
                'open': last_close,
                'high': max(last_close, current_price),
                'low': min(last_close, current_price),
                'close': current_price,
                'volume': data.get('volume_total', 0) / 288,  # Divide por 288 candles de 5m num dia
                'oi': data.get('oi_total', 0),
                'funding': data.get('funding_avg', 0),
                'oi_change': data.get('oi_change_pct', 0)
            }
            
            # Guardar na DB
            self.db.save_candles(asset, self.primary_tf, [candle])
            
            return {
                'candle': candle,
                'data': data,
                'price': current_price,
                'time': current_time
            }
            
        except Exception as e:
            logger.error(f"Erro a buscar candle: {e}")
            return None
    
    def _check_entry_signals(self, candle: Dict, prices: list, regime: str) -> Optional[str]:
        """Verifica se há sinal de entrada"""
        price = candle['close']
        volume = candle['volume']
        oi = candle['oi']
        oi_change = candle.get('oi_change', 0)
        funding = candle.get('funding', 0)
        
        # Calcular SMA
        sma = self._calculate_sma(prices, self.price_sma_period)
        price_above_sma = price > sma
        
        # Funding extremo?
        max_funding = self.config.get('strategy', {}).get('max_funding_rate', 0.01)
        min_funding = self.config.get('strategy', {}).get('min_funding_rate', -0.01)
        funding_extreme = funding > max_funding or funding < min_funding
        
        # Calcular volume ratio (vs média dos últimos candles)
        if len(self.candles) >= 20:
            avg_volume = sum(c['volume'] for c in list(self.candles)[-20:]) / 20
            volume_ratio = volume / max(avg_volume, 1)
        else:
            volume_ratio = 1.0
        
        # Atualizar contadores de candles bullish/bearish
        if price > candle['open']:
            self.bullish_count += 1
            self.bearish_count = 0
        elif price < candle['open']:
            self.bearish_count += 1
            self.bullish_count = 0
        else:
            self.bullish_count = 0
            self.bearish_count = 0
        
        logger.info(f"{candle['time'] if isinstance(candle.get('time'), str) else datetime.now().strftime('%H:%M')} "
                   f"${price:,.0f} | Vol: {volume_ratio:.1f}x | SMA: {sma:,.0f} | "
                   f"{'ABOVE' if price_above_sma else 'BELOW'} | Bullish: {self.bullish_count}x | "
                   f"Regime: {regime}")
        
        # Se já em posição, não entrar
        if self.current_position is not None:
            return None
        
        # LONG
        if price_above_sma and self.bullish_count >= self.min_bullish:
            thresholds = self._get_thresholds_for_regime(regime, 'long')
            
            if volume_ratio >= thresholds['volume'] and not funding_extreme:
                # Verificar OI se disponível
                if oi > 0 and oi_change >= thresholds['oi']:
                    return 'long'
                elif oi == 0:  # Sem OI, usar só volume + candle
                    return 'long'
        
        # SHORT (se ativado)
        if self.short_enabled and not price_above_sma and self.bearish_count >= self.min_bearish:
            thresholds = self._get_thresholds_for_regime(regime, 'short')
            
            if volume_ratio >= thresholds['volume'] and not funding_extreme:
                if oi > 0 and oi_change <= -thresholds['oi']:
                    return 'short'
                elif oi == 0:
                    return 'short'
        
        return None
    
    def _check_exit_signals(self, candle: Dict, prices: list) -> Optional[str]:
        """Verifica se há sinal de saída da posição atual"""
        price = candle['close']
        
        if self.current_position == 'long':
            gain_pct = (price - self.entry_price) / self.entry_price
            
            # Atualizar máximo
            if price > self.max_price:
                self.max_price = price
            
            # Ativar trailing?
            if gain_pct >= self.trailing_activation and not self.trailing_active:
                self.trailing_active = True
                self.trailing_stop = self.max_price * (1 - self.trailing_pct)
                logger.info(f"🚀 TRAILING LONG ATIVADO! Lucro: +{gain_pct*100:.1f}% | Stop: ${self.trailing_stop:,.2f}")
            
            # Atualizar trailing
            if self.trailing_active:
                new_trailing = self.max_price * (1 - self.trailing_pct)
                if new_trailing > self.trailing_stop:
                    self.trailing_stop = new_trailing
                    if gain_pct > 0.02:
                        logger.info(f"🎯 LONG MÁX ${price:,.2f} | Trail: ${self.trailing_stop:,.2f} | +{gain_pct*100:.1f}%")
            
            # Check stop loss
            if not self.trailing_active:
                loss_pct = (self.entry_price - price) / self.entry_price
                if loss_pct >= self.stop_loss_pct:
                    return 'STOP_LOSS'
            
            # Check trailing stop
            if self.trailing_active and price <= self.trailing_stop:
                return 'TRAILING_STOP'
            
            # Check momentum fade (sem OI)
            if len(self.candles) >= 3:
                last_3 = list(self.candles)[-3:]
                avg_vol = sum(c['volume'] for c in last_3) / 3
                if avg_vol < self.candles[-1]['volume'] * 0.5 and price < candle['open']:
                    return 'MOMENTUM_FADE'
        
        elif self.current_position == 'short':
            gain_pct = (self.entry_price - price) / self.entry_price
            
            if price < self.min_price:
                self.min_price = price
            
            if gain_pct >= self.trailing_activation and not self.trailing_active:
                self.trailing_active = True
                self.trailing_stop = self.min_price * (1 + self.trailing_pct)
                logger.info(f"🚀 TRAILING SHORT ATIVADO! Lucro: +{gain_pct*100:.1f}% | Stop: ${self.trailing_stop:,.2f}")
            
            if self.trailing_active:
                new_trailing = self.min_price * (1 + self.trailing_pct)
                if new_trailing < self.trailing_stop:
                    self.trailing_stop = new_trailing
                    if gain_pct > 0.02:
                        logger.info(f"🎯 SHORT MÍN ${price:,.2f} | Trail: ${self.trailing_stop:,.2f} | +{gain_pct*100:.1f}%")
            
            if not self.trailing_active:
                loss_pct = (price - self.entry_price) / self.entry_price
                if loss_pct >= self.short_stop_loss:
                    return 'STOP_LOSS'
            
            if self.trailing_active and price >= self.trailing_stop:
                return 'TRAILING_STOP'
        
        return None
    
    def _enter_position(self, asset: str, side: str, price: float, candle: Dict, regime: str):
        """Abre posição virtual"""
        position_size = min(self.max_position_usd, self.capital * 0.1)
        
        fee = position_size * self.fee_pct * 2  # Entrada + saída estimada
        
        self.current_position = side
        self.entry_price = price
        self.entry_time = datetime.now()
        self.position_size = position_size
        self.max_price = price
        self.min_price = price
        self.trailing_active = False
        self.trailing_stop = 0
        
        if side == 'long':
            self.trailing_stop = price * (1 - self.stop_loss_pct)
        else:
            self.trailing_stop = price * (1 + self.short_stop_loss)
        
        self.trade_count += 1
        self.daily_trades += 1
        
        logger.info(f"[ENTER {side.upper()} {asset}] {self.entry_time.strftime('%H:%M')} @ ${price:,.2f} | "
                   f"Size: ${position_size:,.2f} | Vol: {candle.get('volume', 0):,.0f} | "
                   f"OI: {candle.get('oi_change', 0)*100:.2f}% | Regime: {regime}")
        
        # Guardar na DB
        conn = self.db._get_conn()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO paper_trades (symbol, side, entry_time, entry_price, position_size,
                                     volume_ratio, oi_change, funding_rate, market_regime)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (asset, side, self.entry_time.isoformat(), price, position_size,
              candle.get('volume', 0) / max(1, sum(c['volume'] for c in list(self.candles)[-20:]) / 20) if len(self.candles) >= 20 else 1.0,
              candle.get('oi_change', 0), candle.get('funding', 0), regime))
        conn.commit()
        conn.close()
    
    def _exit_position(self, asset: str, price: float, reason: str, candle: Dict):
        """Fecha posição virtual"""
        if self.current_position == 'long':
            pnl_pct = (price - self.entry_price) / self.entry_price
        else:
            pnl_pct = (self.entry_price - price) / self.entry_price
        
        pnl_usd = self.position_size * pnl_pct
        self.capital += pnl_usd
        
        # Aplicar fees
        fee = self.position_size * self.fee_pct * 2
        self.capital -= fee
        
        exit_time = datetime.now()
        
        emoji = "✅" if pnl_usd > 0 else "❌"
        logger.info(f"[{emoji} EXIT {asset}] {exit_time.strftime('%H:%M')} @ ${price:,.2f} | "
                   f"PnL: ${pnl_usd:+.2f} ({pnl_pct*100:+.2f}%) | Reason: {reason} | "
                   f"Capital: ${self.capital:,.2f}")
        
        # Atualizar trade na DB
        conn = self.db._get_conn()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE paper_trades 
            SET exit_time=?, exit_price=?, pnl_usd=?, pnl_pct=?, exit_reason=?
            WHERE symbol=? AND side=? AND exit_time IS NULL
            ORDER BY entry_time DESC LIMIT 1
        ''', (exit_time.isoformat(), price, pnl_usd, pnl_pct, reason, asset, self.current_position))
        conn.commit()
        conn.close()
        
        # Registar para auto-tune
        self.tuner.record_trade(self.current_position, pnl_pct, reason)
        
        # Reset estado
        self.current_position = None
        self.entry_price = 0
        self.entry_time = None
        self.position_size = 0
    
    def run_cycle(self, asset: str = 'BTC'):
        """Executa um ciclo completo de trading"""
        try:
            # Verificar limite diário
            current_day = datetime.now().date()
            if self.last_day != current_day:
                self.daily_trades = 0
                self.last_day = current_day
            
            max_daily = self.config.get('risk', {}).get('max_daily_trades', 5)
            if self.daily_trades >= max_daily:
                logger.info(f"Limite diário atingido: {self.daily_trades}/{max_daily}")
                return
            
            # Buscar dados
            result = self.fetch_and_process_candle(asset)
            if not result:
                return
            
            candle = result['candle']
            self.candles.append(candle)
            
            # Preparar lista de preços para cálculos
            prices = [c['close'] for c in self.candles]
            if len(prices) < 20:
                logger.info(f"A aguardar dados... {len(prices)}/20 candles")
                return
            
            # Detetar regime e guardar direção do TF alto (15m)
            regime = self._detect_market_regime(prices)
            
            # Atualizar direção do TF alto para a thread MTF
            self._htf_sma = self._calculate_sma(prices, self.price_sma_period)
            self._htf_price = price
            if price > self._htf_sma * 1.005:
                self._htf_direction = 'bull'
            elif price < self._htf_sma * 0.995:
                self._htf_direction = 'bear'
            else:
                self._htf_direction = 'neutral'
            
            # Verificar saída
            if self.current_position:
                exit_reason = self._check_exit_signals(candle, prices)
                if exit_reason:
                    self._exit_position(asset, candle['close'], exit_reason, candle)
                    
                    # Auto-tune após cada 5 trades
                    if self.trade_count % 5 == 0:
                        tune_result = self.tuner.analyze_and_tune()
                        if tune_result['tuned']:
                            logger.info("🤖 Auto-Tune ajustou thresholds!")
                    return
            
            # Verificar entrada
            signal = self._check_entry_signals(candle, prices, regime)
            if signal:
                self._enter_position(asset, signal, candle['close'], candle, regime)
        
        except Exception as e:
            logger.error(f"Erro no ciclo de trading: {e}")
    
    def run_continuous(self, asset: str = 'BTC', interval_seconds: int = 900):
        """Corre em loop contínuo - default 15m (900s) para timeframe 15m"""
        logger.info("="*60)
        logger.info("PAPER TRADING INICIADO - v2 (Baixa Latência)")
        logger.info(f"Capital: ${self.initial_capital:,.2f}")
        logger.info(f"Asset: {asset}")
        logger.info(f"Timeframe: {self.primary_tf}")
        logger.info(f"Signal Interval: {interval_seconds}s ({interval_seconds//60}m)")
        logger.info(f"Monitor Interval: {self._monitor_interval}s (Trailing/SL)")
        logger.info("="*60)
        
        # Iniciar thread de monitorização rápida
        self._start_monitor_thread(asset)
        
        try:
            while True:
                self.run_cycle(asset)
                
                # Log de status com latência
                latency_info = f"Latência: {self.latency_ms:.0f}ms | " if self.latency_ms > 0 else ""
                
                if self.current_position:
                    current_price = self._last_price or 0
                    gain_pct = 0
                    if self.current_position == 'long' and current_price > 0:
                        gain_pct = (current_price - self.entry_price) / self.entry_price * 100
                    elif self.current_position == 'short' and current_price > 0:
                        gain_pct = (self.entry_price - current_price) / self.entry_price * 100
                    
                    trail_info = ""
                    if self.trailing_active:
                        trail_info = f" | Trail: ${self.trailing_stop:,.0f}"
                    
                    logger.info(f"📊 {self.current_position.upper()} | Price: ${current_price:,.0f} | "
                               f"PnL: {gain_pct:+.2f}%{trail_info} | "
                               f"{latency_info}Trades: {self.daily_trades}")
                else:
                    logger.info(f"📊 FLAT | Capital: ${self.capital:,.2f} | "
                               f"{latency_info}Trades hoje: {self.daily_trades}")
                
                logger.info(f"💤 Aguardando {interval_seconds}s...")
                time.sleep(interval_seconds)
                
        except KeyboardInterrupt:
            self._monitor_running = False
            self._mtf_running = False
            if self._monitor_thread:
                self._monitor_thread.join(timeout=2)
            if self._mtf_thread:
                self._mtf_thread.join(timeout=2)
            logger.info("\n" + "="*60)
            logger.info("PAPER TRADING PARADO")
            logger.info(f"Capital Final: ${self.capital:,.2f}")
            logger.info(f"PnL Total: ${self.capital - self.initial_capital:+.2f}")
            logger.info(f"Trades: {self.trade_count}")
            logger.info(f"Latência média monitor: {self.latency_ms:.0f}ms")
            logger.info("="*60)


def main():
    """Entry point para paper trading"""
    import sys
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Carregar config
    config = load_config()
    
    # Criar trader
    trader = PaperTrader(config)
    
    # Verificar argumentos
    if len(sys.argv) > 1 and sys.argv[1] == '--test':
        # Modo teste: corre um ciclo só
        logger.info("MODO TESTE - Um ciclo só")
        trader.run_cycle('BTC')
        
        # Mostrar status
        logger.info(f"Capital: ${trader.capital:,.2f}")
        logger.info(f"Posição: {trader.current_position}")
        if trader.current_position:
            logger.info(f"Entry: ${trader.entry_price:,.2f}")
    else:
        # Modo contínuo
        trader.run_continuous('BTC', interval_seconds=300)


if __name__ == "__main__":
    main()
