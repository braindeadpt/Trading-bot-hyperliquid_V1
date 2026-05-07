@echo off
title Stop Hyperliquid Bot

cd /d "%~dp0"

echo Stopping Hyperliquid Bot...
echo.

:: Find and kill python processes running main.py
for /f "tokens=2" %%a in ('tasklist /FI "IMAGENAME eq python.exe" /NH /FO CSV ^| findstr "main.py"') do (
    echo Killing PID %%a...
    taskkill /PID %%a /F /T
)

:: Also kill any python.exe as fallback
taskkill /F /IM python.exe 2>nul

echo.
echo Bot stopped.
pause
