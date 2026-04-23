@echo off
echo ============================================
echo  PAPER TRADING - Hyperliquid Bot
echo  Asset: BTC | Timeframe: 15m
echo  Capital: $10,000 (virtual)
echo ============================================
echo.
cd /d "%~dp0"
python src/paper_trading.py
pause