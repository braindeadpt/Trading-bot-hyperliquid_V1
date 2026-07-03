@echo off
chcp 65001 >nul
title Hyperliquid Premium Bot v3.1.10 - Quick Start

:: Quick launcher - Paper Trading with dashboard
:: Bot v3.1.10 ships with: per-symbol locks, vol circuit breaker,
:: funding-reset blackout, mainnet defaults override.
:: For advanced options use start.bat
:: For background operation with crash recovery use service.bat
:: For crash recovery with log analysis use run_with_recovery.py

cd /d "%~dp0"

:: Ensure logs dir exists
if not exist logs mkdir logs

echo Starting Hyperliquid Premium Bot [Paper Mode]...
echo Dashboard: http://localhost:5000
echo [INFO] Candle backfill runs automatically on start if DB needs warm-up.
echo.
echo [TIP] If dashboard doesn't open automatically, go to http://localhost:5000 in your browser.
echo [TIP] For background operation with auto-restart, use service.bat instead.
echo [TIP] For crash recovery with log analysis, use: python run_with_recovery.py --mode paper
echo.

:: Open dashboard in default browser after server is ready (non-blocking, 3s delay)
start "" cmd /c "timeout /t 3 /nobreak >nul && start "" http://localhost:5000"

:: Run bot - errors go to fatal_errors.log via main.py
python "%~dp0main.py" --mode paper

:: If python crashed, show error and pause
if errorlevel 1 (
    echo.
    echo [ERROR] Bot crashed. Check logs\fatal_errors.log for details.
    type logs\fatal_errors.log 2>nul
    echo.
    pause
)
