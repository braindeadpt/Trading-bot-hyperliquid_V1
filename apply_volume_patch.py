import re
import sys
import os

# Cores para output
GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
RESET = '\033[0m'

def log(msg, color=RESET):
    print(f"{color}{msg}{RESET}")

def patch_data_aggregator(filepath):
    """Aplica patch no data_aggregator.py"""
    
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Backup
    backup_path = filepath + '.backup'
    with open(backup_path, 'w', encoding='utf-8') as f:
        f.write(content)
    log(f"✅ Backup criado: {backup_path}", GREEN)
    
    # 1. Substituir get_intraday_volume
    old_func = '''    def get_intraday_volume(self, asset: str, interval: str = '15m') -> Optional[Dict]:
        """
        Busca volume INTRADAY real (não 24h acumulado).
        Retorna o último candle com volume real.
        """
        candles = self._fetch_binance_candles(asset, interval, limit=2)
        if candles and len(candles) >= 1:
            latest = candles[-1]
            # Calcular média dos últimos 20 para ratio
            avg_volume = sum(c['volume'] for c in candles) / len(candles)
            volume_ratio = latest['volume'] / max(avg_volume, 1)
            
            return {
                'volume': latest['volume'],
                'volume_ratio': volume_ratio,
                'close': latest['close'],
                'source': 'binance_klines'
            }
        return None'''
    
    new_func = '''    def get_intraday_volume(self, asset: str, interval: str = '5m', ma_period: int = 20) -> Optional[Dict]:
        """
        Busca volume INTRADAY real com média móvel.
        
        Args:
            asset: BTC, ETH, etc.
            interval: Timeframe dos candles (default '5m' para detetar spikes rápidos)
            ma_period: Quantos candles para média móvel (default 20)
        
        Retorna:
            volume, volume_ratio (vs MA), direction (proxy buy/sell)
        """
        # Buscar ma_period + 1 candles (N para média + 1 atual)
        candles = self._fetch_binance_candles(asset, interval, limit=ma_period + 1)
        
        if not candles or len(candles) < 5:
            logger.warning(f"Volume: apenas {len(candles) if candles else 0} candles disponíveis")
            return None
        
        # Último candle (atual/incompleto)
        latest = candles[-1]
        latest_volume = latest['volume']
        latest_close = latest['close']
        latest_open = latest['open']
        
        # Candles anteriores para média (excluindo o último)
        previous_candles = candles[:-1]
        
        if len(previous_candles) < 3:
            logger.warning(f"Volume: apenas {len(previous_candles)} candles anteriores para média")
            return None
        
        # Média móvel dos candles anteriores
        avg_volume = sum(c['volume'] for c in previous_candles) / len(previous_candles)
        
        # Ratio: volume atual vs média
        volume_ratio = latest_volume / max(avg_volume, 1.0)
        
        # Proxy de direção (buy/sell pressure) baseado na cor do candle
        if latest_close < latest_open:
            # Candle vermelho → mais sell pressure
            direction = 'SELL'
            delta = -0.2  # Estimativa conservadora
        elif latest_close > latest_open:
            # Candle verde → mais buy pressure
            direction = 'BUY'
            delta = 0.2
        else:
            direction = 'NEUTRAL'
            delta = 0.0
        
        logger.info(
            f"📊 Volume {asset} | "
            f"Atual: {latest_volume:,.0f} | "
            f"MA{len(previous_candles)}: {avg_volume:,.0f} | "
            f"Ratio: {volume_ratio:.2f}x | "
            f"Dir: {direction}"
        )
        
        return {
            'volume': latest_volume,
            'volume_ratio': volume_ratio,
            'avg_volume': avg_volume,
            'close': latest_close,
            'open': latest_open,
            'direction': direction,
            'delta': delta,
            'samples': len(previous_candles),
            'source': f'binance_klines_{interval}'
        }'''
    
    if old_func in content:
        content = content.replace(old_func, new_func)
        log("✅ get_intraday_volume() atualizado", GREEN)
    else:
        log("⚠️ Função get_intraday_volume() não encontrada no formato esperado", YELLOW)
        log("Tentando regex...", YELLOW)
        
        # Tentativa regex mais flexível
        pattern = r"def get_intraday_volume\(self.*?\n.*?Busca volume INTRADAY.*?\n.*?Retorna.*?\n.*?candles = self\._fetch_binance_candles\(asset, interval, limit=2\).*?\n.*?if candles.*?\n.*?latest = candles\[-1\].*?\n.*?avg_volume = sum.*?\n.*?volume_ratio = latest\['volume'\] / max\(avg_volume, 1\).*?\n.*?return \{.*?\n.*?\}"
        
        if re.search(pattern, content, re.DOTALL):
            content = re.sub(pattern, new_func, content, flags=re.DOTALL)
            log("✅ get_intraday_volume() atualizado via regex", GREEN)
        else:
            log("❌ Não foi possível encontrar get_intraday_volume()", RED)
            return False
    
    # Guardar
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    
    log(f"✅ {filepath} atualizado", GREEN)
    return True

def patch_paper_trading(filepath):
    """Aplica patch no paper_trading.py"""
    
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Backup
    backup_path = filepath + '.backup'
    with open(backup_path, 'w', encoding='utf-8') as f:
        f.write(content)
    log(f"✅ Backup criado: {backup_path}", GREEN)
    
    # 1. Atualizar volume_threshold default de 4.4 para 2.0
    if "'volume_spike_threshold', 4.4" in content:
        content = content.replace("'volume_spike_threshold', 4.4", "'volume_spike_threshold', 2.0")
        log("✅ volume_threshold atualizado: 4.4 -> 2.0", GREEN)
    elif "'volume_spike_threshold', 2.5" in content:
        content = content.replace("'volume_spike_threshold', 2.5", "'volume_spike_threshold', 2.0")
        log("✅ volume_threshold atualizado: 2.5 -> 2.0", GREEN)
    else:
        log("⚠️ volume_threshold não encontrado no formato esperado", YELLOW)
    
    # Guardar
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    
    log(f"✅ {filepath} atualizado", GREEN)
    return True

def patch_settings(filepath):
    """Atualiza settings.yaml com novos parâmetros de volume"""
    
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Verificar se já tem a nova config
    if "interval: \"5m\"" in content and "ma_period: 20" in content:
        log("✅ settings.yaml já tem configuração nova", GREEN)
        return True
    
    # Backup
    backup_path = filepath + '.backup'
    with open(backup_path, 'w', encoding='utf-8') as f:
        f.write(content)
    
    # Adicionar seção volume se não existir
    volume_config = """
  # Volume fix: usar candles de 5m com média móvel real
  volume:
    interval: "5m"           # Buscar candles de 5m (mais sensível a spikes)
    ma_period: 20            # Média móvel de 20 candles = 100 minutos
    spike_threshold: 2.0     # 2x acima da média = spike (era 4.4, impossível)
    min_ratio: 1.0           # Volume deve ser pelo menos 1x a média
"""
    
    # Procurar onde inserir (depois da secção strategy)
    if "strategy:" in content and "volume:" not in content:
        # Inserir após a linha com "strategy:"
        lines = content.split('\n')
        for i, line in enumerate(lines):
            if line.strip().startswith("strategy:"):
                # Inserir após esta linha
                lines.insert(i + 1, volume_config)
                break
        
        content = '\n'.join(lines)
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        
        log(f"✅ {filepath} atualizado com configuração de volume", GREEN)
        return True
    else:
        log("⚠️ Não foi possível atualizar settings.yaml automaticamente", YELLOW)
        return False

def main():
    log("=" * 60, GREEN)
    log("PATCH AUTOMÁTICO: Volume Fix", GREEN)
    log("=" * 60, GREEN)
    
    # Verificar argumentos
    if len(sys.argv) > 1:
        base_dir = sys.argv[1]
    else:
        base_dir = os.getcwd()
    
    log(f"\nDiretório base: {base_dir}")
    
    # Ficheiros
    da_path = os.path.join(base_dir, 'src', 'data_aggregator.py')
    pt_path = os.path.join(base_dir, 'src', 'paper_trading.py')
    settings_path = os.path.join(base_dir, 'config', 'settings.yaml')
    
    results = {}
    
    # 1. Patch data_aggregator.py
    log("\n[1/3] A analisar data_aggregator.py...")
    if os.path.exists(da_path):
        results['data_aggregator'] = patch_data_aggregator(da_path)
    else:
        log(f"❌ {da_path} não encontrado", RED)
        results['data_aggregator'] = False
    
    # 2. Patch paper_trading.py
    log("\n[2/3] A analisar paper_trading.py...")
    if os.path.exists(pt_path):
        results['paper_trading'] = patch_paper_trading(pt_path)
    else:
        log(f"❌ {pt_path} não encontrado", RED)
        results['paper_trading'] = False
    
    # 3. Patch settings.yaml
    log("\n[3/3] A analisar settings.yaml...")
    if os.path.exists(settings_path):
        results['settings'] = patch_settings(settings_path)
    else:
        log(f"❌ {settings_path} não encontrado", RED)
        results['settings'] = False
    
    # Resumo
    log("\n" + "=" * 60, GREEN)
    log("RESUMO DO PATCH", GREEN)
    log("=" * 60, GREEN)
    
    for name, ok in results.items():
        status = "✅ SUCESSO" if ok else "❌ FALHOU"
        log(f"  {name}: {status}")
    
    if all(results.values()):
        log("\n🎉 Todos os patches aplicados com sucesso!", GREEN)
        log("\nPróximos passos:", GREEN)
        log("  1. Verificar sintaxe: python -m py_compile src/data_aggregator.py")
        log("  2. Reiniciar o bot")
        log("  3. Observar logs para ver volume_ratio realista (2x-5x em spikes)")
    else:
        log("\n⚠️ Alguns patches falharam. Verifica os backups (.backup)", YELLOW)
    
    return 0 if all(results.values()) else 1

if __name__ == "__main__":
    sys.exit(main())
