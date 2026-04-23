@echo off
REM ============================================
REM Hyperliquid Bot - Dashboard Launcher
REM Abre o dashboard numa NOVA JANELA do browser
REM ============================================

echo.
echo  ============================================
echo   HYPERLIQUID BOT - DASHBOARD
echo  ============================================
echo.
echo  A iniciar dashboard web...
echo  Abre automaticamente em: http://127.0.0.1:5000
echo.
echo  Pressiona Ctrl+C aqui para parar o servidor
echo  ============================================
echo.

REM Verificar se Python esta disponivel
python --version >nul 2>&1
if errorlevel 1 (
    echo  ERRO: Python nao encontrado!
    echo  Instala Python 3.10+ e adiciona ao PATH
    pause
    exit /b 1
)

REM Verificar Flask
python -c "import flask" >nul 2>&1
if errorlevel 1 (
    echo  A instalar Flask...
    pip install flask
)

REM Iniciar dashboard
python src\dashboard_web.py

pause