@echo off
chcp 65001 >nul
title Hyperliquid Premium Bot — Quick Start

:: Quick launcher — Paper Trading with dashboard
:: For advanced options use start.bat

cd /d "%~dp0"
echo Starting Hyperliquid Premium Bot [Paper Mode]...
echo Dashboard: http://localhost:5000
echo.
python main.py --mode paper
