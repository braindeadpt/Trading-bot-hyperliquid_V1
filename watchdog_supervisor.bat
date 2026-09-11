@echo off
chcp 65001 >nul
title Research Watchdog Supervisor (6h)

REM Read-only evidence gates: top-trader bias, liquidation flush, IV shadow,
REM feed age creep, feed cadence, liq feed gap, nightly keepalive.
REM Task: schtasks /Create /SC HOURLY /MO 6 /TN "Hyperliquid Research Watchdogs" /TR "C:\Users\Braindead\Documents\trading-bot-hyperliquid\watchdog_supervisor.bat" /F
REM Manual: schtasks /Run /TN "Hyperliquid Research Watchdogs"

cd /d "%~dp0"

REM Same UTF-8 fix as overnight_nightly.bat - cp1252 stdout dies on accents.
set PYTHONIOENCODING=utf-8

if not exist logs mkdir logs
if not exist data\research mkdir data\research

python -u scripts\research\research_watchdog_supervisor.py --once >> logs\watchdog_supervisor_cron.log 2>&1
exit /b %ERRORLEVEL%
