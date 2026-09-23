@echo off
chcp 65001 >nul
title Shadow Outcome Evaluation (every 3h)

:: Persists the heavy 14-day shadow-outcome scoreboards so the dashboard
:: reads a cheap SQLite table instead of evaluating ~158k decisions inside
:: the trading process (the evaluation was the dominant RSS driver).
:: A PID lockfile inside the CLI prevents overlap — a second run while one
:: is still active exits 0 with no work. Runs every 3h: the eval takes
:: ~30min on the research HDD, so hourly would leave no slack.
::
:: Proposed registration (NOT executed by this file):
::   schtasks /Create /TN "Hyperliquid Shadow Outcome Eval" /SC HOURLY /MO 3 /ST 00:20 /F ^
::     /TR "wscript.exe \"C:\Users\Braindead\Documents\trading-bot-hyperliquid\scripts\ops\run_hidden.vbs\" \"C:\Users\Braindead\Documents\trading-bot-hyperliquid\shadow_outcome_eval.bat\""
:: :20 offset keeps it clear of the 5-min Jev verdict feed (:00/:05/...)
:: and the 05:00 Overnight Research start.

cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

if not exist logs mkdir logs
if not exist data\research mkdir data\research

python -u -X utf8 scripts\research\evaluate_shadow_outcomes.py --persist --since-days 14 >> logs\shadow_eval_cron.log 2>&1
exit /b %ERRORLEVEL%
