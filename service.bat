@echo off
chcp 65001 >nul
title Hyperliquid Bot — Background Service

:: Runs the bot in background with auto-restart on crash
:: Logs everything to logs/service.log
:: Use stop.bat to terminate

cd /d "%~dp0"

:: Ensure logs dir exists
if not exist logs mkdir logs

:loop
echo [%date% %time%] Starting bot... >> logs\service.log

:: Run bot and capture ALL output (stdout + stderr)
python main.py --mode paper >> logs\service.log 2>&1

set EXITCODE=%ERRORLEVEL%
echo [%date% %time%] Bot exited with code %EXITCODE% >> logs\service.log

:: If exit code is 0, user stopped gracefully — don't restart
if %EXITCODE%==0 (
    echo [%date% %time%] Graceful stop. Not restarting. >> logs\service.log
    goto end
)

:: Otherwise wait 5 seconds and restart
echo [%date% %time%] Restarting in 5 seconds... >> logs\service.log
timeout /t 5 /nobreak >nul
goto loop

:end
echo Bot stopped. Check logs\service.log for details.
pause
