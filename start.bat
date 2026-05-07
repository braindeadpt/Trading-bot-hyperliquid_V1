@echo off
chcp 65001 >nul
title Hyperliquid Premium Bot

:: ═══════════════════════════════════════════════════════════
:: HYPERLIQUID PREMIUM BOT — Windows Launcher
:: ═══════════════════════════════════════════════════════════

setlocal enabledelayedexpansion

:: Default config
set "PROJECT_DIR=%~dp0"
cd /d "%PROJECT_DIR%"

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.11+ and add to PATH.
    pause
    exit /b 1
)

:: Show menu
echo.
echo ╔══════════════════════════════════════════════════════════╗
echo ║    HYPERLIQUID PREMIUM BOT v2.0.0                        ║
echo ╚══════════════════════════════════════════════════════════╝
echo.
echo Choose mode:
echo   [1] Paper Trading   (simulation, no real money)
echo   [2] Testnet         (Hyperliquid testnet, paper money)
echo   [3] Mainnet         (REAL MONEY — confirm required)
echo   [4] Backtest        (historical simulation)
echo   [5] Security Audit  (scan code for vulnerabilities)
echo   [6] Update from GitHub  (git pull latest)
echo   [0] Exit
echo.
set /p choice="Enter option [1-6,0]: "

if "%choice%"=="1" goto paper
if "%choice%"=="2" goto testnet
if "%choice%"=="3" goto mainnet
if "%choice%"=="4" goto backtest
if "%choice%"=="5" goto audit
if "%choice%"=="6" goto update
if "%choice%"=="0" goto end

echo Invalid option.
pause
goto end

:paper
echo.
echo [MODE] Paper Trading
echo Dashboard: http://localhost:5000
echo Press Ctrl+C to stop.
echo.
python main.py --mode paper 2>logs\paper_errors.log
if errorlevel 1 (
    echo [ERROR] Bot crashed. Check logs\paper_errors.log
    pause
)
goto end

:testnet
echo.
echo [MODE] Testnet
echo Dashboard: http://localhost:5000
echo Press Ctrl+C to stop.
echo.
python main.py --mode testnet
goto end

:mainnet
echo.
echo ╔══════════════════════════════════════════════════════════╗
echo ║  ⚠️  WARNING: MAINNET = REAL MONEY                      ║
echo ║  You are about to trade with actual funds.              ║
echo ╚══════════════════════════════════════════════════════════╝
echo.
set /p confirm="Type 'MAINNET' to confirm: "
if /i not "%confirm%"=="MAINNET" (
    echo Cancelled.
    pause
    goto end
)
echo.
echo [MODE] Mainnet — REAL MONEY
echo Dashboard: http://localhost:5000
echo.
python main.py --mode live 2>logs\mainnet_errors.log
if errorlevel 1 (
    echo [ERROR] Bot crashed. Check logs\mainnet_errors.log
    pause
)
goto end

:backtest
echo.
echo [MODE] Backtest
echo.
set /p from_date="Start date (YYYY-MM-DD, default: 2024-01-01): "
set /p to_date="End date (YYYY-MM-DD, default: 2024-03-01): "
if "!from_date!"=="" set "from_date=2024-01-01"
if "!to_date!"=="" set "to_date=2024-03-01"
echo Running backtest from !from_date! to !to_date!...
python main.py --backtest --from-date !from_date! --to-date !to_date!
pause
goto end

:audit
echo.
echo [MODE] Security Audit
echo Scanning all source files...
python main.py --audit
pause
goto end

:update
echo.
echo [UPDATE] Pulling latest from GitHub...
git pull origin main
echo.
echo Installing/updating dependencies...
pip install -r requirements.txt
echo.
echo Done. Press any key to return.
pause
goto end

:end
echo.
echo Goodbye.
endlocal
