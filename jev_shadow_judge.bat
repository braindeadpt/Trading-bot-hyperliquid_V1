@echo off
chcp 65001 >nul
title Jev Shadow Judge (5min)

REM TypeSafe/Jev trading-judgment experiment — virtual paper trading.
REM The script asks Jev once/hour per symbol (API cap) and manages virtual
REM SL/TP exits on every run. Closed virtual trades land in bot.db trades
REM (strategy='JevJudge') so the dashboard shows them. Open virtual
REM positions live only in the research DB — the engine never sees them.
REM Needs TYPESAFE_API_KEY in the user environment:
REM   setx TYPESAFE_API_KEY "ts-..."
REM Task: Hyperliquid-Jev-Shadow (registered via rereg_hidden_tasks.ps1 style)

cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

if not exist logs mkdir logs
if not exist data\research mkdir data\research

python -u -X utf8 scripts\research\jev_shadow_judge.py --once >> logs\jev_shadow_cron.log 2>&1
exit /b %ERRORLEVEL%
