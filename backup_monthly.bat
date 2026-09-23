@echo off
chcp 65001 >nul
title Monthly Research Backup (verified)

:: Verified monthly backup of the research evidence DB + live bot DB + L2
:: mirror into D:\hyperliquid_backup. The manifest's ok flag requires a
:: verified hyperliquid.db (integrity_check + count window + sha256) —
:: a scoped or failed run is never written as ok:true.
:: Verification stages the dest copy on the SSD temp dir, so the b-tree
:: walk runs at SSD speed instead of ~10h of random I/O on the HDD.
::
:: Proposed registration (NOT executed by this file):
::   schtasks /Create /TN "Hyperliquid Monthly Backup" /SC MONTHLY /D 1 /ST 04:00 /F ^
::     /TR "wscript.exe \"C:\Users\Braindead\Documents\trading-bot-hyperliquid\scripts\ops\run_hidden.vbs\" \"C:\Users\Braindead\Documents\trading-bot-hyperliquid\backup_monthly.bat\""
:: 04:00 keeps it clear of the 05:00 Overnight Research start and the
:: :20/:50 Jev + shadow-eval slots. Runs while the bot keeps writing —
:: the count-window check accepts live inserts.

cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

if not exist logs mkdir logs

python -u -X utf8 scripts\ops\backup_research_data.py --tag monthly >> logs\backup_monthly_out.log 2>> logs\backup_monthly_err.log
exit /b %ERRORLEVEL%
