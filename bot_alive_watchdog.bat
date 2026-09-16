@echo off
chcp 65001 >nul
title Bot Alive Watchdog (15min)

REM Fast liveness check only: bot.lock PID + bot.log freshness.
REM Full watchdog sweep stays on the separate 6h task.
REM Task: schtasks /Create /SC MINUTE /MO 15 /TN "Hyperliquid-Bot-Alive" /TR "C:\Users\Braindead\Documents\trading-bot-hyperliquid\bot_alive_watchdog.bat" /F
REM Manual: schtasks /Run /TN "Hyperliquid-Bot-Alive"

cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

if not exist logs mkdir logs
if not exist data\research mkdir data\research

python -u scripts\research\research_watchdog_supervisor.py --once --only bot_alive >> logs\bot_alive_cron.log 2>&1
exit /b %ERRORLEVEL%
