@echo off
chcp 65001 >nul
title HYPERLIQUID BOT — Arranque Automático
color 0A

echo.
echo  ===========================================
echo   🚀 HYPERLIQUID MOMENTUM BOT
echo   Arranque Automatico — Paper Trading
echo  ===========================================
echo.

:: Verificar se Python esta instalado
python --version >nul 2>&1
if errorlevel 1 (
    echo  ❌ ERRO: Python nao encontrado!
    echo  Instala Python 3.10+ em https://python.org
    echo.
    pause
    exit /b 1
)

echo  ✅ Python detectado

:: Verificar dependencias
python -c "import flask" >nul 2>&1
if errorlevel 1 (
    echo  📦 A instalar dependencias...
    python -m pip install flask pystray pillow requests
)

echo  ✅ Dependencias OK
echo.
echo  🌐 Dashboard: http://127.0.0.1:5000
echo  📁 Pasta: %~dp0
echo  🛑 Para parar: CTRL+C ou fecha a janela
echo.
echo  ===========================================
echo   Bot a iniciar...
echo  ===========================================
echo.

:: Mudar para a pasta do bot
cd /d "%~dp0"

:: Limpar logs antigos (opcional, mantem os ultimos 7 dias)
:: forfiles /p logs /m *.log /d -7 /c "cmd /c del @path" 2>nul

:: Iniciar o bot (app_flask.py inicia bot + dashboard + browser automaticamente)
python app_flask.py

:: Se o bot parar, mostrar mensagem
echo.
echo  ===========================================
echo   ⚠️  BOT PARADO
echo  ===========================================
echo.
pause
