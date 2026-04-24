@echo off
chcp 65001 >nul
title Hyperliquid Bot
cls
echo ==================================================
echo   HYPERLIQUID MOMENTUM BOT
echo ==================================================
echo.
echo A iniciar bot...
echo Dashboard vai abrir no browser automaticamente
echo.
echo Para parar: fecha esta janela ou clica no icone verde
echo no canto inferior direito (system tray) e escolhe "Sair"
echo ==================================================
echo.
cd /d "%~dp0"
python app_flask.py
pause
