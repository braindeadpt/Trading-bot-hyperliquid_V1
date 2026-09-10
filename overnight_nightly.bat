@echo off
chcp 65001 >nul
title Overnight Research (nightly)

:: Nightly research session — queue-driven, K=4-guarded, summary output.
:: Registered by: schtasks /Create ... /TN "Hyperliquid Overnight Research"
:: Manual run:   double-click, or  schtasks /Run /TN "Hyperliquid Overnight Research"

cd /d "%~dp0"

:: chcp only changes the CONSOLE code page. When stdout is redirected to a
:: file (as below) Python still picks the locale encoding (cp1252) and dies
:: on the first sigma in a cell tag -- this killed the 2026-09-10 02:00 run.
set PYTHONIOENCODING=utf-8

if not exist logs mkdir logs
if not exist data\research\overnight_experiments mkdir data\research\overnight_experiments

python -u scripts\overnight_nightly.py >> logs\overnight_nightly_cron.log 2>&1
exit /b %ERRORLEVEL%
