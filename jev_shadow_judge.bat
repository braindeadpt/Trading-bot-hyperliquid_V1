@echo off
chcp 65001 >nul
title Jev Shadow Judge (5min)

REM TypeSafe/Jev trading-judgment experiment — verdict feeder.
REM Asks Jev once/hour per symbol (API cap), persists to jev_decisions
REM (research DB) and refreshes data/live/jev_latest.json — the verdict
REM file the JevJudge strategy reads to emit signals (paper-only
REM EXPERIMENT promotion, engine manages the trades).
REM Needs TYPESAFE_API_KEY in the user environment:
REM   setx TYPESAFE_API_KEY "ts-..."
REM Task: Hyperliquid-Jev-Shadow (registered via rereg_hidden_tasks.ps1 style)

cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

if not exist logs mkdir logs
if not exist data\research mkdir data\research

python -u -X utf8 scripts\research\jev_shadow_judge.py --once >> logs\jev_shadow_cron.log 2>&1
exit /b %ERRORLEVEL%
