@echo off
chcp 65001 >nul
title Wallet Fills Collector (hourly)

REM Per-wallet userFills collector for the markout-ranking research
REM (Public Trader Identity methodology, Zhai 2026). Writes to the
REM research DB only; read-only w.r.t. the live engine.
REM Task: schtasks /Create /SC HOURLY /TN "Hyperliquid Wallet Fills Collector" /TR "C:\Users\Braindead\Documents\trading-bot-hyperliquid\wallet_fills_collector.bat" /F
REM Manual: schtasks /Run /TN "Hyperliquid Wallet Fills Collector"

cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

if not exist logs mkdir logs

python -X utf8 scripts\research\wallet_fills_collector.py --once >> logs\wallet_fills_cron.log 2>&1
exit /b %ERRORLEVEL%
