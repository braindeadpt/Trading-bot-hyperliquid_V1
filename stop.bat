@echo off
chcp 65001 >nul
title Stop Hyperliquid Bot

cd /d "%~dp0"

echo Stopping Hyperliquid Bot (this project only)...
echo Folder: %CD%
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop_bot_instances.ps1" -ProjectRoot "%CD%"

if exist "data\live\bot.lock" (
    del /f /q "data\live\bot.lock"
    echo Cleared instance lock: data\live\bot.lock
)

echo.
echo Done.
pause
