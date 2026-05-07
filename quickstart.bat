@echo off
chcp 65001 >nul
title Hyperliquid Premium Bot — Quick Start

:: Quick launcher — Paper Trading with dashboard
:: For advanced options use start.bat

cd /d "%~dp0"

echo Starting Hyperliquid Premium Bot [Paper Mode]...
echo Dashboard: http://localhost:5000
echo.
echo [TIP] If dashboard doesn't open automatically, go to http://localhost:5000 in your browser.
echo.

:: Open dashboard in default browser (non-blocking)
start "" "http://localhost:5000"

:: Run bot with error logging
python main.py --mode paper 2>logs\quickstart_errors.log

:: If python crashed, show error and pause
if errorlevel 1 (
    echo.
    echo [ERROR] Bot crashed. Last error:
    type logs\quickstart_errors.log 2>nul
    echo.
    pause
)

