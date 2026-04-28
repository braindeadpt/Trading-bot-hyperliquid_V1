import re
import sys
import os
import math

# Cores para output
GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
RESET = '\033[0m'

def log(msg, color=RESET):
    print(f"{color}{msg}{RESET}")

def patch_data_aggregator(filepath):
    """Aplica patch Z-Score no data_aggregator.py"""
    
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Backup
    backup_path = filepath + '.backup_zscore'
    with open(backup_path, 'w', encoding='utf-8') as f:
        f.write(content)
    log(f"✅ Backup criado: {backup_path}", GREEN)
    
    # 1. Substituir get_intraday_volume (qualquer versão) pela versão com Z-Score
    # Procurar por padrões flexíveis da função antiga
    patterns = [
        # Padrão 1: Versão original (2 candles)
        r'def get_intraday_volume\(self, asset: str, interval: str = \'15m\'\).*?return None\n',
        # Padrão 2: Versão patchada anterior (MA20)
        r'def get_intraday_volume\(self, asset: str, interval: str = \'5m\', ma_period: int = 20\).*?\n        \}\n        return None\n',
    ]
    
    new_func = '''    def get_intraday_volume(self, asset: str, interval: str = '5m', lookback: int = 20) -> Optional[Dict]:
        """
        Busca volume INTRADAY real com Z-Score (estatisticamente superior a MA simples).
        
        Args:
            asset: BTC, ETH, etc.
            interval: Timeframe dos candles (default '5m' para detetar spikes rápidos)
            lookback: Quantos candles para cálculo estatístico (default 20)
        
        Retorna:
            volume, z_score, volume_ratio, direction, avg, std
            
        Z-Score = (Volume Atual - Média) / Desvio Padrão
            Z > 2.0  → 95% confiança (spike)
            Z > 2.5  → 98% confiança (spike forte) ✅
            Z > 3.0  → 99.7% confiança (extremamente raro)
        """
        # Buscar lookback + 1 candles (N para estatísticas + 1 atual)
        candles = self._fetch_binance_candles(asset, interval, limit=lookback + 1)
        
        if not candles or len(candles) < 8:
            logger.warning(f"Volume: apenas {len(candles) if candles else 0} candles disponíveis (min 8)")
            return None
        
        # Último candle (atual/incompleto)
        latest = candles[-1]
        latest_volume = latest['volume']
        latest_close = latest['close']
        latest_open = latest['open']
        
        # Candles anteriores para estatísticas (excluindo o último)
        previous_volumes = [c['volume'] for c in candles[:-1]]
        
        if len(previous_volumes) < 5:
            logger.warning(f"Volume: apenas {len(previous_volumes)} candles anteriores (min 5)")
            return None
        
        # Estatísticas
        n = len(previous_volumes)
        avg_volume = sum(previous_volumes) / n
        
        # Desvio padrão amostral (ddof=1 para amostra, não população)
        if n > 1:
            variance = sum((v - avg_volume) ** 2 for v in previous_volumes) / (n - 1)
            std_volume = math.sqrt(variance) if variance > 0 else 0.001  # Evitar divisão por zero
        else:
            std_volume = 0.001
        
        # Z-Score
        z_score = (latest_volume - avg_volume) / std_volume
        
        # Volume ratio (para compatibilidade com código existente)
        volume_ratio = latest_volume / max(avg_volume, 1.0)
        
        # Proxy de direção baseado na cor do candle
        if latest_close < latest_open:
            direction = 'SELL'
            delta = -0.2
        elif latest_close > latest_open:
            direction = 'BUY'
            delta = 0.2
        else:
            direction = 'NEUTRAL'
            delta = 0.0
        
        logger.info(
            f"📊 Volume {asset} | "
            f"Atual: {latest_volume:,.0f} | "
            f"μ={avg_volume:,.0f} σ={std_volume:,.0f} | "
            f"Z-Score: {z_score:.2f} | "
            f"Ratio: {volume_ratio:.2f}x | "
            f"Dir: {direction}"
        )
        
        return {
            'volume': latest_volume,
            'z_score': z_score,
            'volume_ratio': volume_ratio,
            'avg_volume': avg_volume,
            'std_volume': std_volume,
            'close': latest_close,
            'open': latest_open,
            'direction': direction,
            'delta': delta,
            'samples': n,
            'source': f'binance_klines_{interval}'
        }'''
    
    patched = False
    for pattern in patterns:
        if re.search(pattern, content, re.DOTALL):
            content = re.sub(pattern, new_func, content, flags=re.DOTALL, count=1)
            patched = True
            log(f"✅ get_intraday_volume() atualizado com Z-Score", GREEN)
            break
    
    if not patched:
        # Tentar substituição mais agressiva
        start_marker = "def get_intraday_volume(self"
        if start_marker in content:
            # Encontrar o início da função
            start_idx = content.find(start_marker)
            # Encontrar a próxima def ou fim da classe
            end_markers = ["\n    def ", "\n    @", "\n    class ", "\n\"\"\"Valida"]
            end_idx = len(content)
            for marker in end_markers:
                idx = content.find(marker, start_idx + len(start_marker))
                if idx != -1 and idx < end_idx:
                    end_idx = idx
            
            # Substituir
            content = content[:start_idx] + new_func + content[end_idx:]
            log(f"✅ get_intraday_volume() atualizado (método fallback)", GREEN)
        else:
            log(f"❌ get_intraday_volume() não encontrada", RED)
            return False
    
    # Guardar
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    
    log(f"✅ {filepath} atualizado", GREEN)
    return True

def patch_paper_trading(filepath):
    """Aplica patch no paper_trading.py para usar Z-Score"""
    
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Backup
    backup_path = filepath + '.backup_zscore'
    with open(backup_path, 'w', encoding='utf-8') as f:
        f.write(content)
    log(f"✅ Backup criado: {backup_path}", GREEN)
    
    # 1. Atualizar volume_threshold default para Z-Score 2.5
    if "'volume_spike_threshold', 2.5" in content:
        content = content.replace("'volume_spike_threshold', 2.5", "'zscore_threshold', 2.5")
        log("✅ Config key atualizada: volume_spike_threshold -> zscore_threshold", GREEN)
    
    # 2. Atualizar short_volume_threshold
    if "'short_volume_threshold', 4.0" in content:
        content = content.replace("'short_volume_threshold', 4.0", "'short_zscore_threshold', 2.5")
        log("✅ Short threshold atualizado", GREEN)
    
    # Guardar
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    
    log(f"✅ {filepath} atualizado", GREEN)
    return True

def patch_settings(filepath):
    """Atualiza settings.yaml com Z-Score"""
    
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Backup
    backup_path = filepath + '.backup_zscore'
    with open(backup_path, 'w', encoding='utf-8') as f:
        f.write(content)
    
    # Substituir volume_spike_threshold
    if 'volume_spike_threshold: 2.0' in content:
        content = content.replace(
            'volume_spike_threshold: 2.0',
            'volume_spike_threshold: 2.5  # Z-Score threshold (98% confiança)'
        )
        log("✅ volume_spike_threshold atualizado para 2.5 (Z-Score)", GREEN)
    
    # Substituir short_volume_threshold
    if 'short_volume_threshold: 2.0' in content:
        content = content.replace(
            'short_volume_threshold: 2.0',
            'short_volume_threshold: 2.5  # Z-Score threshold para shorts'
        )
        log("✅ short_volume_threshold atualizado para 2.5", GREEN)
    
    # Adicionar secção volume se não existir (ou atualizar)
    volume_section = """
  # Volume configuration (Z-Score based)
  volume:
    interval: "5m"              # Candles de 5m para deteção rápida
    lookback: 20                # 20 candles para estatísticas = 100 min
    zscore_threshold: 2.5       # Z > 2.5 = 98% confiança (spike real)
    min_zscore: 0.0             # Z < 0 = abaixo da média (sem interesse)
    # Nota: Z-Score = (Volume - Média) / Desvio Padrão
    #   Z > 2.5 → 98% confiança (apenas 2% falsos positivos)
    #   Z > 3.0 → 99.7% confiança (extremamente raro)
"""
    
    if 'zscore_threshold' not in content:
        # Inserir após a secção strategy
        if 'strategy:' in content:
            lines = content.split('\n')
            for i, line in enumerate(lines):
                if line.strip().startswith('strategy:'):
                    # Inserir depois da primeira linha de strategy
                    lines.insert(i + 1, volume_section)
                    break
            content = '\n'.join(lines)
            log("✅ Secção de volume (Z-Score) adicionada", GREEN)
    
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    
    log(f"✅ {filepath} atualizado", GREEN)
    return True

def main():
    log("=" * 60, GREEN)
    log("PATCH AUTOMÁTICO: Z-Score Volume Fix", GREEN)
    log("=" * 60, GREEN)
    
    if len(sys.argv) > 1:
        base_dir = sys.argv[1]
    else:
        base_dir = os.getcwd()
    
    log(f"\nDiretório base: {base_dir}")
    
    da_path = os.path.join(base_dir, 'src', 'data_aggregator.py')
    pt_path = os.path.join(base_dir, 'src', 'paper_trading.py')
    settings_path = os.path.join(base_dir, 'config', 'settings.yaml')
    
    results = {}
    
    log("\n[1/3] A analisar data_aggregator.py...")
    if os.path.exists(da_path):
        results['data_aggregator'] = patch_data_aggregator(da_path)
    else:
        log(f"❌ {da_path} não encontrado", RED)
        results['data_aggregator'] = False
    
    log("\n[2/3] A analisar paper_trading.py...")
    if os.path.exists(pt_path):
        results['paper_trading'] = patch_paper_trading(pt_path)
    else:
        log(f"❌ {pt_path} não encontrado", RED)
        results['paper_trading'] = False
    
    log("\n[3/3] A analisar settings.yaml...")
    if os.path.exists(settings_path):
        results['settings'] = patch_settings(settings_path)
    else:
        log(f"❌ {settings_path} não encontrado", RED)
        results['settings'] = False
    
    log("\n" + "=" * 60, GREEN)
    log("RESUMO DO PATCH Z-SCORE", GREEN)
    log("=" * 60, GREEN)
    
    for name, ok in results.items():
        status = "✅ SUCESSO" if ok else "❌ FALHOU"
        log(f"  {name}: {status}")
    
    if all(results.values()):
        log("\n🎉 Todos os patches Z-Score aplicados!", GREEN)
        log("\n📊 Fórmula:", GREEN)
        log("  Z = (Volume Atual - Média) / Desvio Padrão", GREEN)
        log("  Z > 2.5 → 98% confiança (spike real)", GREEN)
        log("\nPróximos passos:", GREEN)
        log("  1. Verificar sintaxe: python -m py_compile src/data_aggregator.py")
        log("  2. Reiniciar o bot")
        log("  3. Observar logs para Z-Score realista")
    else:
        log("\n⚠️ Alguns patches falharam. Verifica os backups (.backup_zscore)", YELLOW)
    
    return 0 if all(results.values()) else 1

if __name__ == "__main__":
    sys.exit(main())
