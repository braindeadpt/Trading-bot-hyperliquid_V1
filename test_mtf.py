"""
Teste rápido do Multi-Timeframe (MTF)
Simula TF alto (15m) + TF baixo (5m) com dados existentes
"""
import sys
sys.path.insert(0, 'src')

from database import BotDatabase
from collections import deque
import logging

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

def test_mtf_simulation():
    """Simula MTF com dados da DB"""
    db = BotDatabase()
    
    # Buscar candles 15m
    candles_15m = db.get_candles_for_backtest('BTC', '15m', days=30)
    logger.info(f"📊 Dados: {len(candles_15m)} candles 15m (30 dias)")
    
    if len(candles_15m) < 50:
        logger.error("❌ Dados insuficientes!")
        return
    
    # Simular candles 5m (dividir cada 15m em 3)
    candles_5m = []
    for i, c in enumerate(candles_15m):
        # Criar 3 candles 5m a partir de 1 candle 15m
        # Usar variação aleatória realista
        base_price = c['open']
        close = c['close']
        high = c['high']
        low = c['low']
        volume = c['volume']
        
        # Simular 3 candles 5m com variação
        for j in range(3):
            ratio = (j + 1) / 3
            price_j = base_price + (close - base_price) * ratio
            # Adicionar variação realista
            noise = (high - low) * 0.1 * (1 if j % 2 == 0 else -1)
            
            c5m = {
                'timestamp': c['timestamp'] + j * 5 * 60 * 1000,
                'open': base_price if j == 0 else price_j - noise,
                'high': high if j == 1 else price_j + abs(noise),
                'low': low if j == 1 else price_j - abs(noise),
                'close': price_j,
                'volume': volume / 3 * (1.5 if j == 1 else 0.75),  # Meio do 15m tem mais volume
            }
            candles_5m.append(c5m)
    
    logger.info(f"📊 Simulados: {len(candles_5m)} candles 5m")
    
    # Parâmetros
    sma_period = 100
    volume_threshold = 4.0
    
    # Estado
    prices_15m = []
    volumes_5m = deque(maxlen=20)
    position = None
    entry_price = 0
    trades = []
    
    # HTF (15m) state
    htf_direction = None
    htf_sma = 0
    
    # Contadores
    mtf_signals = 0
    htf_signals = 0
    
    # Percorrer candles 5m
    for i, c5m in enumerate(candles_5m):
        price = c5m['close']
        volume = c5m['volume']
        volumes_5m.append(volume)
        
        # A cada 3 candles 5m = 1 candle 15m → atualizar HTF
        is_new_15m = (i % 3 == 0)
        c15m_idx = i // 3
        
        if is_new_15m and c15m_idx < len(candles_15m):
            c15m = candles_15m[c15m_idx]
            prices_15m.append(c15m['close'])
            
            if len(prices_15m) >= sma_period:
                htf_sma = sum(prices_15m[-sma_period:]) / sma_period
                htf_price = c15m['close']
                
                # Atualizar direção HTF
                old_direction = htf_direction
                if htf_price > htf_sma * 1.005:
                    htf_direction = 'bull'
                elif htf_price < htf_sma * 0.995:
                    htf_direction = 'bear'
                else:
                    htf_direction = 'neutral'
                
                if old_direction != htf_direction:
                    logger.info(f"📈 HTF: {old_direction} → {htf_direction} | SMA: ${htf_sma:,.0f} | Price: ${htf_price:,.0f}")
        
        # MTF: Verificar spike no 5m
        if len(volumes_5m) >= 20 and htf_direction is not None:
            avg_vol = sum(volumes_5m) / len(volumes_5m)
            vol_ratio = volume / max(avg_vol, 1)
            
            # Simular OI e funding (usar dados do 15m)
            oi_change = 0.01 if c15m_idx < len(candles_15m) else 0
            funding = 0.0001
            
            # Sinal MTF: TF baixo volume spike + TF alto direção
            if position is None and vol_ratio >= volume_threshold:
                if htf_direction == 'bull' and price > c5m['open']:
                    # LONG!
                    position = 'long'
                    entry_price = price
                    mtf_signals += 1
                    
                    logger.info(f"⚡ MTF LONG! {c5m['timestamp']} | Price: ${price:,.0f} | Vol: {vol_ratio:.1f}x | HTF: {htf_direction}")
                    
                elif htf_direction == 'bear' and price < c5m['open']:
                    # SHORT!
                    position = 'short'
                    entry_price = price
                    mtf_signals += 1
                    
                    logger.info(f"⚡ MTF SHORT! {c5m['timestamp']} | Price: ${price:,.0f} | Vol: {vol_ratio:.1f}x | HTF: {htf_direction}")
            
            # Saída simples (para teste)
            if position == 'long':
                gain = (price - entry_price) / entry_price
                if gain >= 0.03:  # Take profit 3%
                    trades.append({'side': 'long', 'pnl': gain, 'entry': entry_price, 'exit': price})
                    logger.info(f"✅ EXIT LONG | PnL: +{gain*100:.1f}%")
                    position = None
                elif gain <= -0.02:  # Stop loss 2%
                    trades.append({'side': 'long', 'pnl': gain, 'entry': entry_price, 'exit': price})
                    logger.info(f"❌ EXIT LONG | PnL: {gain*100:.1f}%")
                    position = None
            
            elif position == 'short':
                gain = (entry_price - price) / entry_price
                if gain >= 0.03:
                    trades.append({'side': 'short', 'pnl': gain, 'entry': entry_price, 'exit': price})
                    logger.info(f"✅ EXIT SHORT | PnL: +{gain*100:.1f}%")
                    position = None
                elif gain <= -0.02:
                    trades.append({'side': 'short', 'pnl': gain, 'entry': entry_price, 'exit': price})
                    logger.info(f"❌ EXIT SHORT | PnL: {gain*100:.1f}%")
                    position = None
    
    # Resultados
    logger.info("\n" + "="*60)
    logger.info("RESULTADOS DO TESTE MTF")
    logger.info("="*60)
    logger.info(f"Candles 15m: {len(candles_15m)}")
    logger.info(f"Candles 5m simulados: {len(candles_5m)}")
    logger.info(f"Sinais MTF: {mtf_signals}")
    logger.info(f"Trades: {len(trades)}")
    
    if trades:
        wins = [t for t in trades if t['pnl'] > 0]
        losses = [t for t in trades if t['pnl'] <= 0]
        win_rate = len(wins) / len(trades) * 100
        avg_pnl = sum(t['pnl'] for t in trades) / len(trades) * 100
        total_pnl = sum(t['pnl'] for t in trades) * 100
        
        logger.info(f"Win Rate: {win_rate:.1f}% ({len(wins)}/{len(trades)})")
        logger.info(f"Avg PnL: {avg_pnl:+.2f}%")
        logger.info(f"Total PnL: {total_pnl:+.2f}%")
        
        if wins:
            logger.info(f"Avg Win: +{sum(t['pnl'] for t in wins)/len(wins)*100:.2f}%")
        if losses:
            logger.info(f"Avg Loss: {sum(t['pnl'] for t in losses)/len(losses)*100:.2f}%")
    else:
        logger.info("⚠️ Nenhum trade executado — threshold muito alto?")
    
    logger.info("="*60)

if __name__ == "__main__":
    test_mtf_simulation()
