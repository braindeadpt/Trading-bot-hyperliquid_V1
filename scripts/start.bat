@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

:: ============================================================================
:: HYPERLIQUID BOT v3.0 — Start Script (Windows)
:: ============================================================================
:: Este script arranca o bot em modo UNIFICADO:
::   ✅ Terminal Rich com dashboard ao vivo
::   ✅ Flask Web Dashboard em http://127.0.0.1:5000
::   ✅ Paper Trading (seguro por default)
::   ✅ Dados Hyperliquid correctos (BTC/ETH)
::
:: Modos disponíveis:
::   start.bat              -> Modo UNIFIED (terminal + web)
::   start.bat web          -> Só Flask dashboard
::   start.bat cli          -> Só Terminal Rich
::   start.bat --assets BTC ETH  -> Especificar assets
:: ============================================================================

echo.
echo  ============================================================
echo   🔥 HYPERLIQUID BOT v3.0 — Windows Starter
echo  ============================================================
echo.

:: ─── Determinar diretório do projeto ──────────────────────────────────────
set "SCRIPT_DIR=%~dp0"
set "PROJECT_DIR=%SCRIPT_DIR%.."
cd /d "%PROJECT_DIR%"

echo [Start] Diretorio do projeto: %PROJECT_DIR%

:: ─── Verificar Python ───────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERRO] Python nao encontrado! Verifique se esta instalado e no PATH.
    echo [ERRO] Se usou 'python -m pip', tente 'py -m src.v3.main' em vez disso.
    pause
    exit /b 1
)
for /f "tokens=*" %%a in ('python --version 2^>^&1') do set "PYTHON_VERSION=%%a"
echo [Start] %PYTHON_VERSION%

:: ─── Verificar dependências ────────────────────────────────────────────
echo [Start] A verificar dependencias...
python -c "import flask" >nul 2>&1
if errorlevel 1 (
    echo [AVISO] Flask nao instalado. A instalar...
    python -m pip install flask
)

python -c "import rich" >nul 2>&1
if errorlevel 1 (
    echo [AVISO] Rich nao instalado. A instalar...
    python -m pip install rich
)

python -c "import pyyaml" >nul 2>&1
if errorlevel 1 (
    echo [AVISO] PyYAML nao instalado. A instalar...
    python -m pip install pyyaml
)

python -c "import requests" >nul 2>&1
if errorlevel 1 (
    echo [AVISO] requests nao instalado. A instalar...
    python -m pip install requests
)

:: ─── Ativar venv se existir ─────────────────────────────────────────────
if exist "%PROJECT_DIR%\.venv\Scripts\activate.bat" (
    echo [Start] A ativar ambiente virtual...
    call "%PROJECT_DIR%\.venv\Scripts\activate.bat"
) else if exist "%PROJECT_DIR%\venv\Scripts\activate.bat" (
    echo [Start] A ativar ambiente virtual...
    call "%PROJECT_DIR%\venv\Scripts\activate.bat"
)

:: ─── Configurar UTF-8 ──────────────────────────────────────────────────
set PYTHONIOENCODING=utf-8

:: ─── Parse argumentos ────────────────────────────────────────────────────
set "MODE=unified"
set "ASSETS="

:parse_args
if "%~1"=="" goto :done_parse
if "%~1"=="web" set "MODE=web"
if "%~1"=="cli" set "MODE=cli"
if "%~1"=="unified" set "MODE=unified"
if "%~1"=="--assets" (
    shift
    set "ASSETS=%~1"
    shift
    goto :parse_args
)
if "%~1"=="-a" (
    shift
    set "ASSETS=%~1"
    shift
    goto :parse_args
)
shift
goto :parse_args
:done_parse

:: ─── Mostrar configuração ────────────────────────────────────────────────
echo.
echo  ============================================================
echo   CONFIGURACAO
echo  ============================================================
echo   Modo:       %MODE%
echo   Paper:      SIM (seguro por default)
echo   Assets:     BTC (default, ou do config)
echo   Dashboard:  http://127.0.0.1:5000
echo   Terminal:   Rich Dashboard ao vivo
echo  ============================================================
echo.
echo   [Ctrl+C] para parar o bot (graceful shutdown)
echo.

:: ─── Abrir browser no dashboard (só em modos unified/web) ──────────────
if "%MODE%"=="unified" (
    timeout /t 3 /nobreak >nul
    start "" "http://127.0.0.1:5000"
)
if "%MODE%"=="web" (
    timeout /t 3 /nobreak >nul
    start "" "http://127.0.0.1:5000"
)

:: ─── Arrancar o bot ────────────────────────────────────────────────────
echo [Start] A arrancar bot em modo %MODE%...
echo.

if defined ASSETS (
    python -m src.v3.main --mode %MODE% --assets %ASSETS%
) else (
    python -m src.v3.main --mode %MODE%
)

:: ─── Cleanup ───────────────────────────────────────────────────────────
echo.
echo [Start] Bot encerrado.
echo.
pause
