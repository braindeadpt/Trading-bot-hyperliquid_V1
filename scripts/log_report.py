"""
📊 LOG REPORT — Analisador de performance do bot
Gera report detalhado baseado nos logs e base de dados do bot.

Como usar:
    python scripts/log_report.py

Output: Report formatado no terminal + ficheiro report_YYYY-MM-DD.txt
"""
import sqlite3
import re
from pathlib import Path
from datetime import datetime, timedelta
from collections import Counter


def analyze_logs(log_path: str = "logs/bot.log"):
    """Analisa ficheiro de log do bot"""
    log_file = Path(log_path)
    if not log_file.exists():
        return {"error": f"Log não encontrado: {log_path}"}
    
    with open(log_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    stats = {
        "total_lines": len(lines),
        "errors": [],
        "warnings": [],
        "info_signals": [],
        "price_updates": [],
        "api_errors": [],
        "trades_executed": [],
        "shutdown_events": [],
        "first_timestamp": None,
        "last_timestamp": None,
    }
    
    for line in lines:
        # Timestamp
        ts_match = re.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
        if ts_match:
            ts = ts_match.group(1)
            if not stats["first_timestamp"]:
                stats["first_timestamp"] = ts
            stats["last_timestamp"] = ts
        
        # Erros
        if "ERROR" in line or "CRITICAL" in line:
            stats["errors"].append(line.strip())
        
        # Warnings
        if "WARNING" in line:
            stats["warnings"].append(line.strip())
        
        # Preços
        price_match = re.search(r'BTC raw=.([\d.]+)', line)
        if price_match:
            stats["price_updates"].append(float(price_match.group(1)))
        
        # Trades
        if "Ordem executada" in line or "trade" in line.lower():
            stats["trades_executed"].append(line.strip())
        
        # Shutdown
        if "shutdown" in line.lower() or "parar" in line.lower():
            stats["shutdown_events"].append(line.strip())
        
        # API errors
        if "Erro a buscar" in line or "HTML em vez de JSON" in line:
            stats["api_errors"].append(line.strip())
    
    return stats


def analyze_database(db_path: str = "data/trading_bot.db"):
    """Analisa base de dados SQLite do bot"""
    db_file = Path(db_path)
    if not db_file.exists():
        return {"error": f"Base de dados não encontrada: {db_path}"}
    
    conn = sqlite3.connect(str(db_file))
    c = conn.cursor()
    
    stats = {}
    
    # Paper trades
    c.execute("""
        SELECT 
            COUNT(*) as total,
            SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN pnl_usd < 0 THEN 1 ELSE 0 END) as losses,
            SUM(CASE WHEN pnl_usd = 0 THEN 1 ELSE 0 END) as breakeven,
            SUM(pnl_usd) as total_pnl,
            AVG(pnl_pct) as avg_pnl_pct,
            MAX(pnl_pct) as best_trade,
            MIN(pnl_pct) as worst_trade,
            AVG(position_size) as avg_position_size
        FROM paper_trades
    """)
    row = c.fetchone()
    stats["paper_trades"] = {
        "total": row[0] or 0,
        "wins": row[1] or 0,
        "losses": row[2] or 0,
        "breakeven": row[3] or 0,
        "total_pnl_usd": row[4] or 0,
        "avg_pnl_pct": row[5] or 0,
        "best_trade_pct": row[6] or 0,
        "worst_trade_pct": row[7] or 0,
        "avg_position_size": row[8] or 0,
    }
    
    # Candles
    c.execute("""
        SELECT COUNT(*), MIN(close), MAX(close), AVG(close), MIN(timestamp), MAX(timestamp)
        FROM candles
    """)
    row = c.fetchone()
    stats["candles"] = {
        "total": row[0] or 0,
        "min_price": row[1] or 0,
        "max_price": row[2] or 0,
        "avg_price": row[3] or 0,
        "first_timestamp": row[4] or 0,
        "last_timestamp": row[5] or 0,
    }
    
    # Price history últimas 24h
    c.execute("""
        SELECT MIN(price), MAX(price), AVG(price), COUNT(*)
        FROM price_history
        WHERE timestamp > (strftime('%s', 'now') - 86400)
    """)
    row = c.fetchone()
    stats["price_24h"] = {
        "min": row[0] or 0,
        "max": row[1] or 0,
        "avg": row[2] or 0,
        "samples": row[3] or 0,
    }
    
    # Exit reasons
    c.execute("SELECT exit_reason, COUNT(*) FROM paper_trades GROUP BY exit_reason")
    stats["exit_reasons"] = dict(c.fetchall())
    
    # Regime distribution
    c.execute("SELECT market_regime, COUNT(*) FROM paper_trades GROUP BY market_regime")
    stats["regimes"] = dict(c.fetchall())
    
    conn.close()
    return stats


def generate_report(log_stats, db_stats):
    """Gera report formatado"""
    lines = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    lines.append("=" * 60)
    lines.append("  📊 HYPERLIQUID BOT — LOG REPORT")
    lines.append(f"  Gerado: {now}")
    lines.append("=" * 60)
    lines.append("")
    
    # Seção 1: Resumo da sessão
    lines.append("🕐 SESSÃO")
    lines.append("-" * 40)
    if log_stats.get("first_timestamp"):
        lines.append(f"  Início:    {log_stats['first_timestamp']}")
        lines.append(f"  Fim:       {log_stats['last_timestamp']}")
        lines.append(f"  Linhas:    {log_stats['total_lines']:,}")
    else:
        lines.append("  ⚠️ Sem logs disponíveis")
    lines.append("")
    
    # Seção 2: Trades
    lines.append("💰 PAPER TRADES")
    lines.append("-" * 40)
    pt = db_stats.get("paper_trades", {})
    if pt.get("total", 0) > 0:
        win_rate = (pt["wins"] / pt["total"] * 100) if pt["total"] > 0 else 0
        lines.append(f"  Total:     {pt['total']}")
        lines.append(f"  Wins:      {pt['wins']} ({win_rate:.1f}%)")
        lines.append(f"  Losses:    {pt['losses']}")
        lines.append(f"  Breakeven: {pt['breakeven']}")
        lines.append(f"  PnL Total: ${pt['total_pnl_usd']:+.2f}")
        lines.append(f"  PnL Médio: {pt['avg_pnl_pct']*100:+.3f}%")
        lines.append(f"  Melhor:    {pt['best_trade_pct']*100:+.3f}%")
        lines.append(f"  Pior:      {pt['worst_trade_pct']*100:+.3f}%")
        lines.append(f"  Posição:   {pt['avg_position_size']:.2f} contratos")
    else:
        lines.append("  ⚠️ Sem trades registados")
        lines.append("  (Bot analisou sinais mas critérios não foram cumpridos)")
    lines.append("")
    
    # Seção 3: Preços
    lines.append("📈 PREÇOS BTC")
    lines.append("-" * 40)
    p24 = db_stats.get("price_24h", {})
    if p24.get("samples", 0) > 0:
        lines.append(f"  Min 24h:   ${p24['min']:,.2f}")
        lines.append(f"  Max 24h:   ${p24['max']:,.2f}")
        lines.append(f"  Avg 24h:   ${p24['avg']:,.2f}")
        lines.append(f"  Amostras:  {p24['samples']}")
    else:
        candles = db_stats.get("candles", {})
        lines.append(f"  Min hist:  ${candles.get('min_price', 0):,.2f}")
        lines.append(f"  Max hist:  ${candles.get('max_price', 0):,.2f}")
        lines.append(f"  Avg hist:  ${candles.get('avg_price', 0):,.2f}")
    lines.append("")
    
    # Seção 4: Exit reasons
    lines.append("🎯 RAZÕES DE SAÍDA")
    lines.append("-" * 40)
    for reason, count in db_stats.get("exit_reasons", {}).items():
        lines.append(f"  {reason}: {count}")
    lines.append("")
    
    # Seção 5: Regimes
    lines.append("🌊 REGIMES DE MERCADO")
    lines.append("-" * 40)
    for regime, count in db_stats.get("regimes", {}).items():
        lines.append(f"  {regime}: {count}")
    lines.append("")
    
    # Seção 6: Erros
    lines.append("⚠️ ERROS & WARNINGS")
    lines.append("-" * 40)
    lines.append(f"  Erros:     {len(log_stats.get('errors', []))}")
    lines.append(f"  Warnings:  {len(log_stats.get('warnings', []))}")
    lines.append(f"  API fails: {len(log_stats.get('api_errors', []))}")
    if log_stats.get('errors'):
        lines.append("  Últimos erros:")
        for err in log_stats['errors'][-3:]:
            lines.append(f"    → {err[:80]}")
    lines.append("")
    
    # Seção 7: Status
    lines.append("✅ STATUS DO BOT")
    lines.append("-" * 40)
    lines.append(f"  {'🟢 ONLINE' if len(log_stats.get('shutdown_events', [])) == 0 else '🔴 OFFLINE'}")
    lines.append(f"  Paper:     ✅ Ativo")
    lines.append(f"  API:       {'✅ OK' if len(log_stats.get('api_errors', [])) < 5 else '⚠️ Instável'}")
    lines.append("")
    
    lines.append("=" * 60)
    lines.append("  Fim do report")
    lines.append("=" * 60)
    
    return "\n".join(lines)


def main():
    print("🔍 A analisar logs e base de dados...")
    
    log_stats = analyze_logs("logs/bot.log")
    db_stats = analyze_database("data/trading_bot.db")
    
    report = generate_report(log_stats, db_stats)
    
    # Print no terminal
    print(report)
    
    # Guardar em ficheiro
    report_file = f"report_{datetime.now().strftime('%Y-%m-%d')}.txt"
    with open(report_file, 'w', encoding='utf-8') as f:
        f.write(report)
    print(f"\n💾 Report guardado em: {report_file}")


if __name__ == "__main__":
    main()
