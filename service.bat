@echo off
chcp 65001 >nul
title Hyperliquid Bot - Background Service with Crash Recovery

:: Runs the bot with automatic crash recovery (Task 5.2)
:: Uses run_with_recovery.py which:
::   - Detects crashes and captures reason
::   - Restarts in paper mode (safety fallback)
::   - Limits to 3 restarts with 30s cooldown
::   - Logs crashes to logs/crashes.log
:: Use stop.bat to terminate

cd /d "%~dp0"

:: Ensure logs dir exists
if not exist logs mkdir logs

echo Starting Hyperliquid Bot with Crash Recovery...
echo Mode: paper (fallback after any crash)
echo Dashboard: http://localhost:5000
echo.
echo Logs: logs/bot.log (bot output)
echo Crash log: logs/crashes.log (crash history)
echo.
echo Press Ctrl+C to stop permanently.
echo.

:: Open dashboard in default browser after server is ready (non-blocking, 3s delay)
start "" cmd /c "timeout /t 3 /nobreak >nul && start "" http://localhost:5000"

:: Run with crash recovery wrapper
python run_with_recovery.py --mode paper --max-restarts 3 --cooldown 30

echo.
echo Bot stopped. Check logs/crashes.log for crash history.
pause
