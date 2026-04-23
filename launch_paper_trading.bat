@echo off
echo ============================================
echo  PAPER TRADING v2 - Hyperliquid Bot
echo  Baixa Latencia (10s monitor + 15m sinais)
echo  Asset: BTC | Capital: $10,000 (virtual)
echo ============================================
echo.
cd /d "%~dp0"
python src/paper_trading.py
pause