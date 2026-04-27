#!/usr/bin/env python3
"""
Bot Monitor — Verifica estado do bot Hyperliquid a cada 1 hora
Busca dados via ngrok e gera relatório
"""
import urllib.request
import json
import time
from datetime import datetime, timezone

NGROK_URL = "https://remedial-deception-contact.ngrok-free.dev"
HEADERS = {"ngrok-skip-browser-warning": "1"}

def fetch_api(endpoint):
    """Faz GET para API do bot via ngrok"""
    try:
        req = urllib.request.Request(
            f"{NGROK_URL}{endpoint}",
            headers=HEADERS
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}

def check_bot():
    """Verifica estado completo do bot"""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    
    # Buscar dados
    status = fetch_api("/api/status")
    mtf_state = fetch_api("/api/mtf-state")
    mtf_debug = fetch_api("/api/mtf-debug")
    
    # Formatar relatório
    report = f"""
🔍 BOT CHECK — {now}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
    
    # Status principal
    if "error" in status:
        report += f"⚠️  ERRO a buscar status: {status['error']}\n"
    else:
        bot_running = status.get("bot_running", False)
        price = status.get("last_price", 0)
        position = status.get("current_position")
        capital = status.get("capital", 0)
        
        report += f"Bot: {'✅ RUNNING' if bot_running else '❌ STOPPED'}\n"
        report += f"Preço BTC: ${price:,.2f}\n" if price else "Preço: indisponível\n"
        report += f"Capital: ${capital:,.2f}\n" if capital else ""
        
        if position:
            report += f"📍 POSIÇÃO ABERTA: {position.get('direction', position.get('side', 'N/A'))}\n"
            report += f"   Entry: ${position.get('entryPrice', position.get('entry_price', 0)):,.2f}\n"
            report += f"   Size: ${position.get('size', position.get('position_size', 0)):,.2f}\n"
        else:
            report += "📍 Sem posição aberta\n"
    
    # MTF State
    report += "\n📊 MTF State:\n"
    if "error" in mtf_state:
        report += f"   ⚠️  Erro: {mtf_state['error']}\n"
    else:
        mtf = mtf_state.get("mtf", {})
        report += f"   HTF Direction: {mtf.get('htf_direction', 'N/A')}\n"
        report += f"   5m Candles: {mtf.get('real_5m_candles_count', 0)}\n"
        report += f"   Cooldown: {'🔴 ATIVO' if mtf.get('mtf_cooldown_active') else '🟢 Inativo'}\n"
        report += f"   Volume Threshold: {mtf.get('volume_threshold', 'N/A')}x\n"
    
    # MTF Debug (últimos logs)
    report += "\n📋 Últimos Logs MTF:\n"
    if "error" in mtf_debug:
        report += f"   ⚠️  Erro: {mtf_debug['error']}\n"
    else:
        logs = mtf_debug.get("logs", [])
        if logs:
            for line in logs[-5:]:  # últimas 5 linhas
                report += f"   {line}\n"
        else:
            report += "   (sem logs recentes)\n"
    
    report += "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    
    return report

def main():
    """Loop principal — corre a cada 1 hora"""
    print(f"🤖 Bot Monitor iniciado — checks a cada 1h")
    print(f"   URL: {NGROK_URL}")
    print(f"   Início: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 50)
    
    while True:
        report = check_bot()
        print(report)
        
        # Guardar último report em ficheiro para fácil acesso
        with open("/root/.openclaw/workspace/trading-bot-hyperliquid/memory/bot_hourly_check.md", "w") as f:
            f.write(report)
        
        next_check = datetime.now(timezone.utc).timestamp() + 3600
        next_dt = datetime.fromtimestamp(next_check, tz=timezone.utc)
        print(f"\n😴 Próximo check às {next_dt.strftime('%H:%M')} UTC")
        time.sleep(3600)  # 1 hora

if __name__ == "__main__":
    main()
