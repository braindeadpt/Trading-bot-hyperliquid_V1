@echo off
chcp 65001 >nul
title HYPERLIQUID BOT — Paper Trading
cls

echo ============================================
echo   HYPERLIQUID MOMENTUM BOT
echo   Paper Trading (Testnet) 
echo ============================================
echo.
echo A iniciar bot...
echo.
echo Modo: PAPER TRADING (sem risco real)
echo Assets: BTC
echo Timeframe: 15m
echo.
echo Dashboard: http://127.0.0.1:5000
echo.
echo [Ctrl+C] para parar
echo ============================================
echo.

cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

python bot_engine.py

pause