@echo off
chcp 65001 >nul
title Overnight Research (nightly)

:: Nightly research session — queue-driven, K=4-guarded, summary output.
:: Registered by: schtasks /Create ... /TN "Hyperliquid Overnight Research"
:: Manual run:   double-click, or  schtasks /Run /TN "Hyperliquid Overnight Research"

cd /d "%~dp0"

if not exist logs mkdir logs
if not exist data\research\overnight_experiments mkdir data\research\overnight_experiments

python -u scripts\overnight_nightly.py >> logs\overnight_nightly_cron.log 2>&1
exit /b %ERRORLEVEL%
