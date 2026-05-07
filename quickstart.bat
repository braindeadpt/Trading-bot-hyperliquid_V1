@echo off
chcp 65001 >nul
title Hyperliquid Premium Bot — Quick Start

:: Quick launcher — Paper Trading with dashboard
:: For advanced options use start.bat
:: Use service.bat for background operation with auto-restart

cd /d "%~dp0"

:: Ensure logs dir exists
if not exist logs mkdir logs

echo Starting Hyperliquid Premium Bot [Paper Mode]...
echo Dashboard: http://localhost:5000
echo.
echo [TIP] If dashboard doesn't open automatically, go to http://localhost:5000 in your browser.
echo [TIP] For background operation with auto-restart, use service.bat instead.
echo.

:: Open dashboard in default browser (non-blocking)
start "" "http://localhost:5000"

:: Run bot — errors go to fatal_errors.log via main.py
python main.py --mode paper

:: If python crashed, show error and pause
if errorlevel 1 (
    echo.
    echo [ERROR] Bot crashed. Check logs\fatal_errors.log for details.
    type logs\fatal_errors.log 2>nul
    echo.
    pause
)
