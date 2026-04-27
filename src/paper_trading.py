"""
Paper Trading Engine v2 - Baixa latência para trailing stops
Separa signal generation (15m) de position monitoring (rápido)
"""
import logging
import time
import json
import threading
import signal
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional
from collections import deque

from data_aggregator import DataAggregator
from database import BotDatabase
from strategy import MomentumStrategy
from utils import load_config
from volume_profile import VolumeProfile

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
        
        # HyperTracker layer de confirmacao (opcional)
        self._last_hypertracker_confirmation = None
        self._last_hypertracker_time = 0
        
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
        
        # Leverage adaptativa
        self.adaptive_leverage = config.get('risk', {}).get('adaptive_leverage', False)
        self.adaptive_config = config.get('risk', {}).get('adaptive_conditions', {})
        self.current_leverage = self.max_leverage
        
        self.fee_pct = 0.00035  # 0.035% Hyperliquid taker
        
        # Componentes
        self.aggregator = DataAggregator(config)
        self.db = BotDatabase()
        self.strategy = MomentumStrategy(config)
        self.tuner = AutoTuner(config, self.db)
        
        # Volume Profile (Fase A do roadmap)
        vp_lookback = config.get('strategy', {}).get('vp_lookback', 96)  # 96 candles de 15m = 24h
        self.volume_profile = VolumeProfile(lookback=vp_lookback)
        self.vp_enabled = config.get('strategy', {}).get('vp_enabled', True)
        self.last_vp = None  # Cache do último VP calculado
        
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
        
        # Contadores de candles bullish/bearish consecutivos
        self.bullish_count = 0
        self.bearish_count = 0
        
        # Candles em memória (últimos 100)
        self.candles = deque(maxlen=100)
        
        # Config de trailing
        risk = config.get('risk', {})
        self.trailing_activation = risk.get('trailing_activation_pct', 0.015)
        self.trailing_pct = risk.get('trailing_stop_pct', 0.015)
        self.stop_loss_pct = risk.get('stop_loss_pct', 0.02)
        self.short_stop_loss = risk.get('short_stop_loss_pct', 0.025)
        
        # ⚡ TAKE PROFIT
        strat = config.get('strategy', {})
        self.take_profit_pct = strat.get('take_profit_pct', 0.10)  # 10% default
        
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
        
        # ⚡ COOLDOWN — Evita entradas repetidas
        self._last_entry_time = 0  # timestamp da última entrada
        self._cooldown_seconds = config.get('strategy', {}).get('entry_cooldown_seconds', 300)
        
        # ⚡ FILTRO DE REGIME — Não tradar em condições extremas
        self._regime_filter_enabled = config.get('strategy', {}).get('regime_filter_enabled', True)
        self._max_volatility_pct = config.get('strategy', {}).get('max_volatility_pct', 5.0)
        self._max_price_vs_sma_pct = config.get('strategy', {}).get('max_price_vs_sma_pct', 5.0)
        
        # Estatísticas de latência
        self.latency_ms = 0
        self.last_check_time = 0
        
        # ⚡ CRASH RECOVERY — Restaurar posição aberta da DB se existir
        self._recover_open_position()
        
        # ⚡ GRACEFUL SHUTDOWN — handler de sinais
        self._shutdown_requested = False
        self._shutdown_complete = False
        self._setup_signal_handlers()
        
        # ==================================================================
        # MULTI-TIMEFRAME — TF Baixo para deteção rápida de spikes
        # ==================================================================
        self.secondary_tf = self.timeframes.get('secondary', '5m')  # 5m para gatilho de entrada
        self._mtf_thread = None
        self._mtf_running = False
        self._mtf_interval = 300  # 5 minutos = sincronizado com candles 5m
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
                leverage REAL DEFAULT 2,
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
        
        # Migracao: adicionar coluna leverage se nao existir
        try:
            cursor.execute("ALTER TABLE paper_trades ADD COLUMN leverage REAL DEFAULT 2")
        except Exception:
            pass  # Coluna ja existe
        
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
        self._mtf_thread.start()  # ⚡ FIX: Thread agora inicia!
    def _setup_signal_handlers(self):
        """⚡ GRACEFUL SHUTDOWN — handlers para SIGINT e SIGTERM"""
        def _handle_shutdown(signum, frame):
            logger.warning(f"\n{'='*50}")
            logger.warning(f"🛑 SINAL RECEBIDO ({signum}) — A iniciar graceful shutdown...")
            logger.warning(f"{'='*50}")
            self._shutdown_requested = True
            
            # Se temos posição aberta, fechar
            if self.current_position:
                logger.warning(f"🛑 A fechar posição {self.current_position} antes de parar...")
                with self._lock:
                    # Buscar preço actual
                    try:
                        data = self.aggregator.fetch_all_data(self.assets[0])
                        if data:
                            hl = data.get('exchanges_data', {}).get('hyperliquid', {})
                            price = hl.get('mark_price', self.entry_price)
                            self._exit_position(self.assets[0], price, 'GRACEFUL_SHUTDOWN', {'close': price})
                    except Exception as e:
                        logger.error(f"Erro ao fechar posição no shutdown: {e}")
            
            # Parar threads
            self._monitor_running = False
            self._mtf_running = False
            self._shutdown_complete = True
            
            logger.warning("✅ Shutdown completo. A sair...")
            sys.exit(0)
        
        # ⚡ Só configurar signals na thread principal
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, _handle_shutdown)   # Ctrl+C
            signal.signal(signal.SIGTERM, _handle_shutdown)  # kill / systemctl stop
            logger.info("⚡ Graceful shutdown configurado (Ctrl+C fecha posições)")
        else:
            logger.debug("Signal handlers ignorados — não estamos na thread principal")
    
    def is_shutdown_requested(self) -> bool:
        """Verifica se foi pedido shutdown"""
        return self._shutdown_requested
    
    def _recover_open_position(self):
        """
        ⚡ CRASH RECOVERY — Restaura posição aberta da DB se o bot tiver crashado
        Verifica trades com exit_time IS NULL e restaura o estado.
        """
        try:
            with self._lock:
                open_trade = self.db.get_open_trade()
                if open_trade:
                    symbol = open_trade['symbol']
                    direction = open_trade['direction']
                    entry_price = open_trade['entry_price']
                    entry_time = open_trade['entry_time']
                    size_usd = open_trade['size_usd']
                    
                    self.current_position = direction
                    self.entry_price = entry_price
                    self.entry_time = entry_time
                    self.position_size = size_usd
                    self.max_price = entry_price  # Reset — vamos recalcular com preço actual
                    self.min_price = entry_price
                    self.trailing_active = False
                    self.trailing_stop = 0
                    
                    logger.warning(f"🚨 CRASH RECOVERY: Posição {direction.upper()} {symbol} restaurada!")
                    logger.warning(f"   Entry: ${entry_price:,.2f} | Size: ${size_usd:,.2f}")
                    logger.warning(f"   Trade ID: {open_trade['id']} | Entry time: {datetime.fromtimestamp(entry_time)}")
        except Exception as e:
            logger.warning(f"⚠️ Erro no crash recovery (ignorado): {e}")
    
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
        """Processa candle do TF baixo (5m) para detetar spikes em tempo real
        
        ESTRATÉGIA MTF:
        - 15m (HTF): Define a direção/tendência (bull/bear/neutral)
        - 5m (LTF): Gatilho de entrada quando deteta volume spike na direção do HTF
        - Só entra se AMBOS concordam — 5m confirma, 15m filtra
        """
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
            
            # Calcular métricas no TF baixo
            volumes = [c['volume'] for c in self._low_tf_candles]
            avg_volume = sum(volumes[-20:]) / 20
            current_volume = candle['volume']
            volume_ratio = current_volume / max(avg_volume, 1)
            
            # Calcular variação de preço no candle 5m atual
            price_change_pct = (candle['close'] - candle['open']) / candle['open'] if candle['open'] > 0 else 0
            
            price = candle['close']
            
            # Verificar se há direção do TF alto (15m)
            if self._htf_direction is None:
                logger.debug(f"MTF: Aguardando direção HTF...")
                return  # Ainda não temos direção do TF alto
            
            # Verificar cooldown (evitar entradas duplicadas)
            current_time = time.time()
            if current_time < self._mtf_cooldown:
                return
            
            # Log de status MTF a cada 5 candles
            if len(self._low_tf_candles) % 5 == 0:
                logger.info(f"📊 MTF Status | {self.secondary_tf} | Price: ${price:,.0f} | "
                           f"Vol: {volume_ratio:.1f}x | Price Δ: {price_change_pct*100:+.2f}% | "
                           f"HTF: {self._htf_direction}")
            
            # ⚡ FIX RACE: Verificar posição DENTRO do lock para evitar double-entry
            with self._lock:
                if self.current_position is not None:
                    return
                
                # =====================================================================
                # REGRAS DE ENTRADA MTF
                # =====================================================================
                
                # Funding check (evita pagar funding extremo)
                funding = candle.get('funding', 0)
                max_funding = self.config.get('strategy', {}).get('max_funding_rate', 0.01)
                min_funding = self.config.get('strategy', {}).get('min_funding_rate', -0.01)
                funding_extreme = funding > max_funding or funding < min_funding
                
                if funding_extreme:
                    logger.debug(f"MTF: Funding extremo ({funding*100:.3f}%), skipping")
                    return
                
                # --- LONG: TF alto bullish + TF baixo volume spike + candle bullish ---
                if self._htf_direction == 'bull':
                    # Requisitos para entrada LONG:
                    # 1. Volume no 5m > threshold (spike confirmado)
                    # 2. Candle 5m bullish (close > open)
                    # 3. Preço já subiu pelo menos 0.2% no 5m (confirma momentum)
                    
                    volume_spike = volume_ratio >= self.tuner.volume_threshold
                    candle_bullish = price > candle['open']
                    momentum_confirmed = price_change_pct >= 0.002  # 0.2% mínimo
                    
                    if volume_spike and candle_bullish and momentum_confirmed:
                        logger.info(f"⚡ MTF SIGNAL LONG! {self.secondary_tf} confirmado | "
                                   f"Vol: {volume_ratio:.1f}x | Price Δ: +{price_change_pct*100:.2f}% | "
                                   f"HTF: {self._htf_direction} | Entry: ${price:,.0f}")
                        
                        # ENTRAR!
                        self._enter_position(asset, 'long', price, candle, self._htf_direction)
                        
                        # Cooldown de 5 minutos para evitar re-entrada
                        self._mtf_cooldown = current_time + 300
                    else:
                        # Log porquê não entrou
                        if not volume_spike:
                            logger.debug(f"MTF LONG skip: volume {volume_ratio:.1f}x < {self.tuner.volume_threshold}x")
                        elif not candle_bullish:
                            logger.debug(f"MTF LONG skip: candle bearish ({price:,.0f} < {candle['open']:,.0f})")
                        elif not momentum_confirmed:
                            logger.debug(f"MTF LONG skip: momentum fraco ({price_change_pct*100:.2f}% < 0.2%)")
                
                # --- SHORT: TF alto bearish + TF baixo volume spike + candle bearish ---
                elif self._htf_direction == 'bear':
                    # Requisitos para entrada SHORT:
                    # 1. Volume no 5m > threshold
                    # 2. Candle 5m bearish (close < open)
                    # 3. Preço já desceu pelo menos 0.2% no 5m
                    
                    volume_spike = volume_ratio >= self.tuner.volume_threshold
                    candle_bearish = price < candle['open']
                    momentum_confirmed = price_change_pct <= -0.002  # -0.2% mínimo
                    
                    if volume_spike and candle_bearish and momentum_confirmed:
                        logger.info(f"⚡ MTF SIGNAL SHORT! {self.secondary_tf} confirmado | "
                                   f"Vol: {volume_ratio:.1f}x | Price Δ: {price_change_pct*100:.2f}% | "
                                   f"HTF: {self._htf_direction} | Entry: ${price:,.0f}")
                        
                        self._enter_position(asset, 'short', price, candle, self._htf_direction)
                        
                        self._mtf_cooldown = current_time + 300
                    else:
                        if not volume_spike:
                            logger.debug(f"MTF SHORT skip: volume {volume_ratio:.1f}x < {self.tuner.volume_threshold}x")
                        elif not candle_bearish:
                            logger.debug(f"MTF SHORT skip: candle bullish ({price:,.0f} > {candle['open']:,.0f})")
                        elif not momentum_confirmed:
                            logger.debug(f"MTF SHORT skip: momentum fraco ({price_change_pct*100:.2f}% > -0.2%)")
                
                else:
                    # HTF neutral — não entra em nenhuma direção
                    logger.debug(f"MTF: HTF neutral, aguardando direção clara")
                    
        except Exception as e:
            logger.warning(f"Erro no processamento MTF: {e}")
    
    def _fetch_low_tf_candle(self, asset: str) -> Optional[Dict]:
        """Busca candle REAL do timeframe baixo (5m) da Binance
        
        ⚡ FIX: Antes simulávamos o candle (open=price*0.999, volume=estimativa).
        Agora buscamos candles reais de 5m da Binance com OHLCV real.
        """
        try:
            # Buscar candles reais de 5m da Binance (20 candles = 100 minutos de histórico)
            candles_5m = self.aggregator._fetch_binance_candles(asset, '5m', limit=20)
            
            if not candles_5m or len(candles_5m) < 2:
                logger.debug(f"MTF: Sem candles 5m reais disponíveis")
                return None
            
            # Pegar no último candle fechado (penúltimo se o último estiver em formação)
            # O último candle pode estar incompleto, usar o penúltimo para ter dados fechados
            latest_candle = candles_5m[-2] if len(candles_5m) >= 2 else candles_5m[-1]
            
            # Buscar dados adicionais (OI, funding) do aggregator
            data = self.aggregator.fetch_all_data(asset)
            
            # Construir candle no formato esperado pelo MTF
            candle = {
                'timestamp': latest_candle['timestamp'],
                'time': datetime.fromtimestamp(latest_candle['timestamp'] / 1000).strftime('%H:%M:%S'),
                'open': latest_candle['open'],
                'high': latest_candle['high'],
                'low': latest_candle['low'],
                'close': latest_candle['close'],
                'volume': latest_candle['volume'],  # Volume REAL deste candle 5m
                'oi': data.get('oi_total', 0) if data else 0,
                'funding': data.get('funding_avg', 0) if data else 0
            }
            
            # Guardar candles para cálculo de média de volume
            if not hasattr(self, '_real_5m_candles'):
                self._real_5m_candles = []
            self._real_5m_candles.append(candle)
            if len(self._real_5m_candles) > 50:
                self._real_5m_candles = self._real_5m_candles[-50:]
            
            # ⚡ FIX: Inicializar _low_tf_candles com histórico real se estiver vazio
            # Assim temos 20 candles imediatamente para calcular média de volume
            if len(self._low_tf_candles) < 20 and len(candles_5m) >= 20:
                for c in candles_5m[-20:]:
                    self._low_tf_candles.append({
                        'timestamp': c['timestamp'],
                        'open': c['open'],
                        'high': c['high'],
                        'low': c['low'],
                        'close': c['close'],
                        'volume': c['volume']
                    })
            
            return {'candle': candle, 'data': data}
            
        except Exception as e:
            logger.warning(f"Erro a buscar candle MTF real: {e}")
            # Fallback: tentar o método antigo (simulado) como último recurso
            return self._fetch_low_tf_candle_fallback(asset)
    
    def _fetch_low_tf_candle_fallback(self, asset: str) -> Optional[Dict]:
        """Fallback: candle simulado (método antigo)"""
        try:
            data = self.aggregator.fetch_all_data(asset)
            if not data:
                return None
            
            exchanges = data.get('exchanges_data', {})
            hl_data = exchanges.get('hyperliquid', {})
            current_price = hl_data.get('mark_price', 0)
            
            if current_price <= 0:
                prices = [ex.get('mark_price', 0) for ex in exchanges.values() if ex.get('mark_price', 0) > 0]
                if prices:
                    current_price = sum(prices) / len(prices)
            
            if current_price <= 0:
                return None
            
            now = datetime.now()
            candle = {
                'timestamp': int(now.timestamp() * 1000),
                'time': now.strftime('%H:%M:%S'),
                'open': current_price * 0.999,
                'high': current_price * 1.001,
                'low': current_price * 0.998,
                'close': current_price,
                'volume': data.get('volume_total', 0) / 288,
                'oi': data.get('oi_total', 0),
                'funding': data.get('funding_avg', 0)
            }
            
            return {'candle': candle, 'data': data}
            
        except Exception as e:
            logger.warning(f"Erro no fallback MTF: {e}")
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
            
            # ⚡ FIX TOCTOU: Toda a lógica de posição DENTRO do lock
            with self._lock:
                if not self.current_position:
                    return
                    
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
    
    def run_cycle(self, asset: str) -> Optional[Dict]:
        """
        Ciclo completo de trading — chamado pelo BotEngine.
        Wrapper para fetch_and_process_candle com logging.
        """
        try:
            result = self.fetch_and_process_candle(asset)
            if result:
                logger.info(f"[CYCLE] {asset} | Price: {result.get('close', 0):,.2f} | "
                          f"Volume: {result.get('volume', 0):,.0f} | "
                          f"Signal: {result.get('signal', 'NONE')}")
            return result
        except Exception as e:
            logger.error(f"[CYCLE] Erro no ciclo para {asset}: {e}")
            return None
    
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
            
            # ⚡ FIX: Volume INTRADAY real (não 24h/288!)
            # Buscar volume real do candle de 15m via Binance klines
            intraday = self.aggregator.get_intraday_volume(asset, self.primary_tf)
            if intraday:
                candle_volume = intraday['volume']
                volume_ratio_hint = intraday.get('volume_ratio', 1.0)
                logger.info(f"  [VOLUME] Intraday: {candle_volume:,.0f} | Ratio: {volume_ratio_hint:.1f}x")
            else:
                # Fallback: volume 24h (menos preciso)
                candle_volume = data.get('volume_total', 0) / 288
                logger.warning(f"  [VOLUME] Fallback 24h/288: {candle_volume:,.0f}")
            
            # Simular candle com dados atuais
            candle = {
                'timestamp': int(current_time.timestamp() * 1000),
                'open': last_close,
                'high': max(last_close, current_price),
                'low': min(last_close, current_price),
                'close': current_price,
                'volume': candle_volume,  # ⚡ FIX: Volume real intraday!
                'oi': data.get('oi_total', 0),
                'funding': data.get('funding_avg', 0),
                'oi_change': data.get('oi_change_pct', 0)
            }
            
            # ⚡ HYPERTRACKER CONFIRMATION LAYER
            # Buscar a cada 1h (~48 requests/dia, limite gratuito 100/dia)
            now_ts = time.time()
            if now_ts - self._last_hypertracker_time >= 3600:
                try:
                    ht_conf = self.aggregator.get_hypertracker_confirmation()
                    self._last_hypertracker_confirmation = ht_conf
                    self._last_hypertracker_time = now_ts
                    if ht_conf:
                        logger.info(f"[HT] Confirmacao: {ht_conf.get('recommendation', 'neutral')} | "
                                  f"Boost: {ht_conf.get('total_boost', 0):+d} | "
                                  f"Calls: {ht_conf.get('calls_today', 0)}/100")
                except Exception as e:
                    logger.warning(f"[HT] Erro a buscar confirmacao: {e}")
            
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
    
    def _check_cooldown(self) -> bool:
        """
        Verifica se cooldown entre entradas está respeitado.
        Retorna True se pode entrar, False se está em cooldown.
        """
        current_time = time.time()
        elapsed = current_time - self._last_entry_time
        if elapsed < self._cooldown_seconds:
            logger.info(f"  [COOLDOWN] {self._cooldown_seconds - elapsed:.0f}s restantes antes de nova entrada")
            return False
        return True
    
    def _check_regime_filter(self, regime: str, prices: list, candle: Dict) -> tuple[bool, str]:
        """
        Filtro de regime operacional — bloqueia entradas em condições extremas.
        Retorna (pode_entrar, razão).
        """
        if not self._regime_filter_enabled:
            return True, ""
        
        current_price = prices[-1] if prices else 0
        
        # 1. Verificar volatilidade (evitar whipsaw)
        volatility = 0
        if len(prices) >= 14:
            diffs = [abs(prices[i] - prices[i-1]) for i in range(1, len(prices))]
            avg_diff = sum(diffs[-14:]) / 14
            volatility = (avg_diff / current_price) * 100 if current_price > 0 else 0
        
        if volatility > self._max_volatility_pct:
            return False, f"REGIME_FILTER: volatilidade_alta({volatility:.1f}% > {self._max_volatility_pct}%)"
        
        # 2. Verificar distância da SMA200 (evitar entradas em topos/fundos)
        if len(prices) >= 200:
            sma_200 = self._calculate_sma(prices, 200)
        elif len(prices) >= 60:
            sma_200 = self._calculate_sma(prices, 60)
        else:
            return True, ""  # Dados insuficientes
        
        if sma_200 > 0:
            price_vs_sma_pct = abs((current_price - sma_200) / sma_200) * 100
            if price_vs_sma_pct > self._max_price_vs_sma_pct:
                return False, f"REGIME_FILTER: preco_extended({price_vs_sma_pct:.1f}% vs SMA200 > {self._max_price_vs_sma_pct}%)"
        
        return True, ""
    
    def _check_entry_signals(self, candle: Dict, prices: list, regime: str,
                              volume_ratio: float, price_above_sma: bool,
                              funding_extreme: bool, oi_ok_long: bool,
                              oi_ok_short: bool,
                              hypertracker_conf: Optional[Dict] = None) -> Optional[str]:
        """Verifica se há sinal de entrada e GUARDA na DB (mesmo os que não entram)"""
        price = candle['close']
        
        # Se já em posição, não entrar
        if self.current_position is not None:
            return None
        
        # ⚡ COOLDOWN — Evita entradas repetidas
        if not self._check_cooldown():
            return None
        
        # ⚡ FILTRO DE REGIME — Bloqueia entradas em condições extremas
        regime_ok, regime_reason = self._check_regime_filter(regime, prices, candle)
        if not regime_ok:
            self._log_signal('BTC', 'BLOCKED', 0.0, candle, regime, regime_reason, executed=False)
            return None
        
        # Calcular Volume Profile (para filtro de qualidade)
        vp_info = ""
        vp_allowed = True
        vp_reason = ""
        if self.vp_enabled and len(self.candles) >= 20:
            candles_list = list(self.candles)
            self.last_vp = self.volume_profile.calculate(candles_list)
            if self.last_vp:
                vp_info = f" | {self.volume_profile.format_profile(self.last_vp)}"
        
        # ============================================================
        # HYPERTRACKER CONFIRMATION LAYER
        # ============================================================
        ht_boost = 0
        ht_rec = 'neutral'
        if hypertracker_conf:
            ht_boost = hypertracker_conf.get('total_boost', 0)
            ht_rec = hypertracker_conf.get('recommendation', 'neutral')
            logger.info(f"[HT] Boost: {ht_boost:+d} | Rec: {ht_rec}")
        
        # ============================================================
        # LONG Signal Analysis
        # ============================================================
        if price_above_sma and self.bullish_count >= self.min_bullish:
            thresholds = self._get_thresholds_for_regime(regime, 'long')
            
            # Aplicar HyperTracker boost aos thresholds
            # Se HT confirma LONG, baixar threshold (mais fácil entrar)
            # Se HT contradiz, subir threshold (mais difícil entrar)
            adjusted_volume_threshold = thresholds['volume']
            if ht_rec in ('confirm', 'strong_confirm'):
                adjusted_volume_threshold *= 0.85  # 15% mais fácil
            elif ht_rec in ('contradict', 'strong_contradict'):
                adjusted_volume_threshold *= 1.3  # 30% mais difícil
            
            if volume_ratio >= adjusted_volume_threshold and not funding_extreme and oi_ok_long:
                # Filtro Volume Profile
                if self.vp_enabled and self.last_vp:
                    vp_allowed, vp_reason = self.volume_profile.filter_entry(price, 'long', self.last_vp)
                    if not vp_allowed:
                        logger.info(f"  [VP BLOCK] LONG bloqueado: {vp_reason}{vp_info}")
                        self._log_signal('BTC', 'LONG', 0.0, candle, regime,
                                        f"VP_BLOCK: {vp_reason}", executed=False)
                        return None
                    logger.info(f"  [VP OK] LONG aprovado: {vp_reason}")
                
                # Se HyperTracker contradiz fortemente, skip mesmo com VP ok
                if ht_rec == 'strong_contradict' and ht_boost < -20:
                    logger.warning(f"[HT BLOCK] LONG bloqueado: HyperTracker contradiz fortemente ({ht_rec}, {ht_boost:+d})")
                    self._log_signal('BTC', 'LONG', 0.0, candle, regime,
                                    f"HT_BLOCK: {ht_rec}", executed=False)
                    return None
                
                # LOG: Sinal aceite!
                ht_info = f" | HT:{ht_rec}" if hypertracker_conf else ""
                self._log_signal('BTC', 'LONG', 1.0, candle, regime,
                                f"ENTRY: vol={volume_ratio:.1f}x | {vp_reason}{ht_info}", executed=True)
                return 'long'
            else:
                # LOG: Sinal rejeitado (volume, funding, OI)
                reason_parts = []
                if volume_ratio < adjusted_volume_threshold:
                    reason_parts.append(f"vol_low({volume_ratio:.1f}x<{adjusted_volume_threshold:.1f})")
                if funding_extreme:
                    reason_parts.append("funding_extreme")
                if not oi_ok_long:
                    reason_parts.append("oi_insufficient")
                if ht_rec in ('contradict', 'strong_contradict'):
                    reason_parts.append(f"ht_{ht_rec}")
                
                self._log_signal('BTC', 'LONG', 0.0, candle, regime,
                                f"FILTER: {' | '.join(reason_parts)}", executed=False)
        
        # ============================================================
        # SHORT Signal Analysis
        # ============================================================
        if self.short_enabled and not price_above_sma and self.bearish_count >= self.min_bearish:
            thresholds = self._get_thresholds_for_regime(regime, 'short')
            
            # Aplicar HyperTracker boost aos thresholds
            adjusted_volume_threshold = thresholds['volume']
            if ht_rec in ('confirm', 'strong_confirm'):
                adjusted_volume_threshold *= 0.85
            elif ht_rec in ('contradict', 'strong_contradict'):
                adjusted_volume_threshold *= 1.3
            
            if volume_ratio >= adjusted_volume_threshold and not funding_extreme and oi_ok_short:
                if self.vp_enabled and self.last_vp:
                    vp_allowed, vp_reason = self.volume_profile.filter_entry(price, 'short', self.last_vp)
                    if not vp_allowed:
                        logger.info(f"  [VP BLOCK] SHORT bloqueado: {vp_reason}{vp_info}")
                        self._log_signal('BTC', 'SHORT', 0.0, candle, regime,
                                        f"VP_BLOCK: {vp_reason}", executed=False)
                        return None
                    logger.info(f"  [VP OK] SHORT aprovado: {vp_reason}")
                
                # Se HyperTracker contradiz fortemente, skip
                if ht_rec == 'strong_contradict' and ht_boost > 20:
                    logger.warning(f"[HT BLOCK] SHORT bloqueado: HyperTracker contradiz fortemente ({ht_rec}, {ht_boost:+d})")
                    self._log_signal('BTC', 'SHORT', 0.0, candle, regime,
                                    f"HT_BLOCK: {ht_rec}", executed=False)
                    return None
                
                ht_info = f" | HT:{ht_rec}" if hypertracker_conf else ""
                self._log_signal('BTC', 'SHORT', 1.0, candle, regime,
                                f"ENTRY: vol={volume_ratio:.1f}x | {vp_reason}{ht_info}", executed=True)
                return 'short'
            else:
                reason_parts = []
                if volume_ratio < adjusted_volume_threshold:
                    reason_parts.append(f"vol_low({volume_ratio:.1f}x<{adjusted_volume_threshold:.1f})")
                if funding_extreme:
                    reason_parts.append("funding_extreme")
                if not oi_ok_short:
                    reason_parts.append("oi_insufficient")
                if ht_rec in ('contradict', 'strong_contradict'):
                    reason_parts.append(f"ht_{ht_rec}")
                
                self._log_signal('BTC', 'SHORT', 0.0, candle, regime,
                                f"FILTER: {' | '.join(reason_parts)}", executed=False)
        
        # Nenhum sinal
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
            
            # ⚡ CHECK TAKE PROFIT
            if gain_pct >= self.take_profit_pct:
                return 'TAKE_PROFIT'
            
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
            
            # ⚡ CHECK TAKE PROFIT SHORT
            if gain_pct >= self.take_profit_pct:
                return 'TAKE_PROFIT'
            
            if self.trailing_active and price >= self.trailing_stop:
                return 'TRAILING_STOP'
        
        return None
    
    def _calculate_adaptive_leverage(self, side: str, candle: Dict, regime: str) -> int:
        """Calcula leverage adaptativa baseada em condicoes de mercado"""
        if not self.adaptive_leverage:
            return self.max_leverage
        
        base = self.max_leverage  # Default (3x)
        low_cfg = self.adaptive_config.get('low_leverage', {})
        high_cfg = self.adaptive_config.get('high_leverage', {})
        
        # Obter metricas do candle
        funding = candle.get('funding_rate', 0)
        atr14 = candle.get('atr14', 0)  # Se disponivel
        
        # Verificar streak de perdas
        recent_trades = self.db.get_recent_trades(limit=5) if hasattr(self, 'db') and self.db else []
        loss_streak = 0
        for t in reversed(recent_trades):
            pnl = t.get('pnl_pct', 0)
            if pnl < 0:
                loss_streak += 1
            else:
                break
        
        # === CONDICOES PARA LEVERAGE BAIXA (2x) ===
        low_factors = 0
        
        # Funding positivo alto (caro manter posicao)
        funding_thresh = low_cfg.get('funding_threshold', 0.0005)
        if funding > funding_thresh:
            low_factors += 1
        
        # Volatilidade alta
        if atr14 > low_cfg.get('volatility_threshold', 0.06):
            low_factors += 1
        
        # Streak de perdas
        if loss_streak >= low_cfg.get('streak_threshold', 3):
            low_factors += 1
        
        # Regime contra a posicao
        if regime == 'bear' and side == 'long':
            low_factors += 1
        elif regime == 'bull' and side == 'short':
            low_factors += 1
        
        if low_factors >= 2:
            return 2  # Reduzir para 2x
        
        # === CONDICOES PARA LEVERAGE ALTA (5x) ===
        high_factors = 0
        
        # Funding negativo (pagam-te para segurar)
        funding_high = high_cfg.get('funding_threshold', -0.0002)
        if funding < funding_high:
            high_factors += 1
        
        # Volatilidade baixa
        if atr14 > 0 and atr14 < high_cfg.get('volatility_threshold', 0.03):
            high_factors += 1
        
        # Regime a favor da posicao (boost)
        if high_cfg.get('regime_boost', False):
            if regime == 'bull' and side == 'long':
                high_factors += 1
            elif regime == 'bear' and side == 'short':
                high_factors += 1
        
        if high_factors >= 2:
            return 5  # Aumentar para 5x
        
        return base  # 3x default
    
    def _enter_position(self, asset: str, side: str, price: float, candle: Dict, regime: str):
        """Abre posicao virtual"""
        # Leverage adaptativa
        self.current_leverage = self._calculate_adaptive_leverage(side, candle, regime)
        
        position_size = min(self.max_position_usd, self.capital * 0.1)
        margin = position_size / self.current_leverage
        fee = position_size * self.fee_pct * 2  # Entrada + saida estimada
        
        # Verificar se ha capital suficiente para margin + fee
        if margin + fee > self.capital:
            logger.warning(f"Capital insuficiente para {side.upper()} {asset}: "
                          f"margin=${margin:.2f} + fee=${fee:.2f} > capital=${self.capital:.2f}")
            return
        
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
        
        # ⚡ COOLDOWN — Registar timestamp da entrada
        self._last_entry_time = time.time()
        
        logger.info(f"[ENTER {side.upper()} {asset}] {self.entry_time.strftime('%H:%M')} @ ${price:,.2f} | "
                   f"Size: ${position_size:,.2f} | Leverage: {self.current_leverage}x | "
                   f"Margin: ${margin:,.2f} | Vol: {candle.get('volume', 0):,.0f} | "
                   f"OI: {candle.get('oi_change', 0)*100:.2f}% | Regime: {regime}")
        
        # Log signal de entrada executada
        self._log_signal(asset, side.upper(), 1.0, candle, regime,
                        f"EXECUTED: size=${position_size:.0f} | lev={self.current_leverage}x", executed=True)
        
        # Guardar na DB
        conn = self.db._get_conn()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO paper_trades (symbol, side, entry_time, entry_price, position_size,
                                     leverage, volume_ratio, oi_change, funding_rate, market_regime)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (asset, side, self.entry_time.isoformat(), price, position_size,
              self.current_leverage,
              candle.get('volume', 0) / max(1, sum(c['volume'] for c in list(self.candles)[-20:]) / 20) if len(self.candles) >= 20 else 1.0,
              candle.get('oi_change', 0), candle.get('funding', 0), regime))
        conn.commit()
        conn.close()
    
    def _log_signal(self, asset: str, signal_type: str, confidence: float,
                    candle: Dict, regime: str, reason: str,
                    executed: bool = False):
        """
        Guarda SINAL na base de dados — inclui os que NÃO entraram!
        Isto é OURO para análise posterior: porquê rejeitámos entradas?
        """
        try:
            now = int(datetime.now().timestamp() * 1000)
            signal = {
                'timestamp': now,
                'asset': asset,
                'signal_type': signal_type,
                'confidence': confidence,
                'strategy': 'momentum_v2',
                'entry_price': candle.get('close'),
                'stop_loss': candle.get('close') * (1 - self.stop_loss_pct) if signal_type == 'LONG' else candle.get('close') * (1 + self.short_stop_loss) if signal_type == 'SHORT' else None,
                'take_profit': None,
                'executed': executed,
                'execution_time': now if executed else None,
                'reason': reason,
                'market_regime': regime,
                'volume_ratio': candle.get('volume_ratio'),
                'oi_change': candle.get('oi_change'),
                'funding_rate': candle.get('funding'),
                'price_above_sma': candle.get('price_above_sma'),
                'bullish_count': self.bullish_count,
                'bearish_count': self.bearish_count
            }
            self.db.save_signal(signal)
        except Exception as e:
            logger.warning(f"Erro a guardar sinal: {e}")

    def _log_market_regime(self, asset: str, regime: str, prices: list):
        """Guarda regime de mercado na DB"""
        try:
            if len(prices) >= 200:
                sma_200 = self._calculate_sma(prices, 200)
            elif len(prices) >= 60:
                sma_200 = self._calculate_sma(prices, 60)
            else:
                sma_200 = sum(prices) / len(prices) if prices else 0

            current_price = prices[-1] if prices else 0
            price_vs_sma = ((current_price - sma_200) / sma_200) if sma_200 > 0 else 0

            # Volatilidade simples (ATR-like)
            volatility = 0
            if len(prices) >= 14:
                diffs = [abs(prices[i] - prices[i-1]) for i in range(1, len(prices))]
                avg_diff = sum(diffs[-14:]) / 14
                volatility = (avg_diff / current_price) * 100 if current_price > 0 else 0

            # Volume profile baseado nos últimos candles
            volume_profile = 'NORMAL'
            if len(self.candles) >= 20:
                recent_volumes = [c['volume'] for c in list(self.candles)[-20:]]
                avg_vol = sum(recent_volumes) / len(recent_volumes)
                current_vol = self.candles[-1]['volume'] if self.candles else 0
                if current_vol > avg_vol * 1.5:
                    volume_profile = 'HIGH'
                elif current_vol < avg_vol * 0.5:
                    volume_profile = 'LOW'

            regime_data = {
                'timestamp': int(datetime.now().timestamp() * 1000),
                'asset': asset,
                'regime': regime,
                'volatility_24h': volatility,
                'trend_strength': abs(price_vs_sma) * 100,
                'volume_profile': volume_profile,
                'sma_200': sma_200,
                'price_vs_sma_pct': price_vs_sma * 100
            }
            self.db.save_market_regime(regime_data)
        except Exception as e:
            logger.warning(f"Erro a guardar regime: {e}")

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
        
        # Log signal de saída
        self._log_signal(asset, 'EXIT', 1.0, candle, 'active_trade', reason, executed=True)
        
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
    
    def _calculate_daily_pnl(self) -> float:
        """Calcula PnL acumulado do dia atual (paper trades)"""
        try:
            today = datetime.now().date().isoformat()
            conn = self.db._get_conn()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT COALESCE(SUM(pnl_usd), 0) FROM paper_trades
                WHERE DATE(entry_time) = ? AND exit_time IS NOT NULL
            ''', (today,))
            result = cursor.fetchone()
            conn.close()
            return result[0] if result else 0.0
        except Exception:
            return 0.0
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
            
            # ⚡ CIRCUIT BREAKER — Para se perder demasiado num dia
            daily_pnl = self._calculate_daily_pnl()
            daily_loss_limit = self.config.get('risk', {}).get('daily_loss_limit_pct', 0.05)
            daily_loss_hard = self.config.get('risk', {}).get('daily_loss_hard_stop_pct', 0.10)
            
            if daily_pnl <= -self.initial_capital * daily_loss_hard:
                logger.error(f"🚨 CIRCUIT BREAKER HARD STOP! Perda: ${daily_pnl:,.2f} ({daily_pnl/self.initial_capital*100:.1f}%) — Bot PARADO")
                self.shutdown()
                return
            
            if daily_pnl <= -self.initial_capital * daily_loss_limit:
                logger.warning(f"⚠️  CIRCUIT BREAKER SOFT STOP! Perda: ${daily_pnl:,.2f} ({daily_pnl/self.initial_capital*100:.1f}%) — Trading suspenso")
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
            
            # LOG: Regime de mercado
            self._log_market_regime(asset, regime, prices)
            
            # Atualizar direção do TF alto para a thread MTF
            self._htf_sma = self._calculate_sma(prices, self.price_sma_period)
            self._htf_price = candle['close']
            if candle['close'] > self._htf_sma * 1.005:
                self._htf_direction = 'bull'
            elif candle['close'] < self._htf_sma * 0.995:
                self._htf_direction = 'bear'
            else:
                self._htf_direction = 'neutral'
            
            # ============================================================
            # Calcular métricas UMA VEZ para passar ao signal checker
            # ============================================================
            sma = self._calculate_sma(prices, self.price_sma_period)
            price_above_sma = candle['close'] > sma
            
            # Funding
            funding = candle.get('funding', 0)
            max_funding = self.config.get('strategy', {}).get('max_funding_rate', 0.01)
            min_funding = self.config.get('strategy', {}).get('min_funding_rate', -0.01)
            funding_extreme = funding > max_funding or funding < min_funding
            
            # Volume ratio
            if len(self.candles) >= 20:
                avg_volume = sum(c['volume'] for c in list(self.candles)[-20:]) / 20
                volume_ratio = candle['volume'] / max(avg_volume, 1)
            else:
                volume_ratio = 1.0
            
            # Atualizar contadores de candles bullish/bearish
            if candle['close'] > candle['open']:
                self.bullish_count += 1
                self.bearish_count = 0
            elif candle['close'] < candle['open']:
                self.bearish_count += 1
                self.bullish_count = 0
            else:
                self.bullish_count = 0
                self.bearish_count = 0
            
            # OI checks — STRICT (não entra se OI=0 ou API falhou)
            oi = candle.get('oi', 0)
            oi_change = candle.get('oi_change', 0)
            thresholds_long = self._get_thresholds_for_regime(regime, 'long')
            thresholds_short = self._get_thresholds_for_regime(regime, 'short')
            oi_ok_long = oi > 0 and oi_change >= thresholds_long['oi']
            oi_ok_short = oi > 0 and oi_change <= -thresholds_short['oi']
            
            # Log de status
            logger.info(f"{datetime.now().strftime('%H:%M')} "
                       f"${candle['close']:,.0f} | Vol: {volume_ratio:.1f}x | SMA: {sma:,.0f} | "
                       f"{'ABOVE' if price_above_sma else 'BELOW'} | Bullish: {self.bullish_count}x | "
                       f"Regime: {regime}")
            
            # Adicionar métricas ao candle para logging
            candle['volume_ratio'] = volume_ratio
            candle['price_above_sma'] = price_above_sma
            
            # Verificar saída
            if self.current_position:
                with self._lock:
                    exit_reason = self._check_exit_signals(candle, prices)
                    if exit_reason:
                        self._exit_position(asset, candle['close'], exit_reason, candle)
                        
                        # Auto-tune após cada 5 trades
                        if self.trade_count % 5 == 0:
                            tune_result = self.tuner.analyze_and_tune()
                            if tune_result['tuned']:
                                logger.info("Auto-Tune ajustou thresholds!")
                        return
            
            # Verificar entrada (agora com métricas pré-calculadas + HyperTracker)
            signal = self._check_entry_signals(
                candle, prices, regime,
                volume_ratio, price_above_sma, funding_extreme,
                oi_ok_long, oi_ok_short,
                self._last_hypertracker_confirmation
            )
            if signal:
                with self._lock:
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
