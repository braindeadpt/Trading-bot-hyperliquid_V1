@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

:: =============================================================================
:: run_tests.bat — Script de Execução de Testes (Windows)
:: Hyperliquid Trading Bot — Versão Final Unificada
:: =============================================================================
::
:: USO:
::   run_tests.bat              → Roda todos os testes
::   run_tests.bat unit         → Apenas testes unitários
::   run_tests.bat integration  → Apenas testes de integração
::   run_tests.bat system       → Apenas testes de sistema
::   run_tests.bat stress       → Apenas stress tests
::   run_tests.bat regression   → Apenas regressão (bugs conhecidos)
::   run_tests.bat coverage     → Todos os testes com relatório de cobertura
::   run_tests.bat ci           → Modo CI (sem LIVE, com fail-under)
::   run_tests.bat fast         → Apenas testes rápidos (< 5s cada)
::
:: =============================================================================

set "SCRIPT_DIR=%~dp0"
set "PROJECT_DIR=%SCRIPT_DIR%.."
cd /d "%PROJECT_DIR%"

title 🧪 Hyperliquid Bot — Test Runner

:: ─── Cores (ANSI) ───
set "GREEN=[92m"
set "RED=[91m"
set "YELLOW=[93m"
set "BLUE=[94m"
set "CYAN=[96m"
set "RESET=[0m"

:: ─── Verificar Python ───
where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo %RED%❌ ERRO: Python não encontrado no PATH!%RESET%
    echo %YELLOW%   Verifica se instalaste Python e adicionaste ao PATH.%RESET%
    echo %YELLOW%   Ou tenta: py --version%RESET%
    pause
    exit /b 1
)

for /f "tokens=*" %%a in ('python --version 2^>^&1') do set "PYTHON_VERSION=%%a"
echo %CYAN%🐍 %PYTHON_VERSION% detectado%RESET%

:: ─── Verificar/Instalar dependências ───
echo %BLUE%📦 A verificar dependências...%RESET%

python -c "import pytest" >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo %YELLOW%   pytest não encontrado. A instalar...%RESET%
    python -m pip install pytest pytest-cov pytest-race
)

python -c "import yaml" >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo %YELLOW%   pyyaml não encontrado. A instalar...%RESET%
    python -m pip install pyyaml
)

python -c "import requests" >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo %YELLOW%   requests não encontrado. A instalar...%RESET%
    python -m pip install requests
)

python -c "import coverage" >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo %YELLOW%   coverage não encontrado. A instalar...%RESET%
    python -m pip install coverage
)

echo %GREEN%   ✅ Dependências OK%RESET%

:: ─── Criar diretórios de saída ───
if not exist "test_reports" mkdir "test_reports"
if not exist "htmlcov" mkdir "htmlcov"

:: ─── Variáveis de teste ───
set "TIMESTAMP=%date:~6,4%%date:~3,2%%date:~0,2%_%time:~0,2%%time:~3,2%%time:~6,2%"
set "TIMESTAMP=%TIMESTAMP: =0%"
set "REPORT_FILE=test_reports\test_report_%TIMESTAMP%.txt"
set "COVERAGE_FILE=test_reports\coverage_%TIMESTAMP%.xml"

:: ─── Determinar modo ───
set "MODE=%~1"
if "%MODE%"=="" set "MODE=all"

:: ─── Banner ───
echo.
echo %CYAN%╔══════════════════════════════════════════════════════════════╗%RESET%
echo %CYAN%║                 🧪 HYPERLIQUID BOT TESTS                     ║%RESET%
echo %CYAN%╠══════════════════════════════════════════════════════════════╣%RESET%
echo %CYAN%║  Modo: %-54s ║%RESET%
echo %CYAN%║  Data: %-54s ║%RESET%
echo %CYAN%╚══════════════════════════════════════════════════════════════╝%RESET%
echo.

:: ─── Adicionar src/ ao PYTHONPATH ───
set "PYTHONPATH=%PROJECT_DIR%\src;%PROJECT_DIR%\refactored;%PROJECT_DIR%\clean;%PYTHONPATH%"

:: ─── Executar conforme modo ───
if "%MODE%"=="all" goto :run_all
if "%MODE%"=="unit" goto :run_unit
if "%MODE%"=="integration" goto :run_integration
if "%MODE%"=="system" goto :run_system
if "%MODE%"=="stress" goto :run_stress
if "%MODE%"=="regression" goto :run_regression
if "%MODE%"=="coverage" goto :run_coverage
if "%MODE%"=="ci" goto :run_ci
if "%MODE%"=="fast" goto :run_fast
if "%MODE%"=="legacy" goto :run_legacy
if "%MODE%"=="clean" goto :run_clean
if "%MODE%"=="help" goto :show_help

echo %RED%❌ Modo desconhecido: %MODE%%RESET%
goto :show_help

:: =============================================================================
:: RUN ALL
:: =============================================================================
:run_all
echo %BLUE%🧪 A executar TODOS os testes...%RESET%
echo.
python -m pytest tests/ -v --tb=short ^
    --junitxml="%COVERAGE_FILE%" ^
    2>&1 | tee -a "%REPORT_FILE%"
set "EXIT_CODE=%ERRORLEVEL%"
goto :done

:: =============================================================================
:: RUN UNIT
:: =============================================================================
:run_unit
echo %BLUE%🧪 A executar testes UNITÁRIOS...%RESET%
echo %YELLOW%   (Domain, Application, Infrastructure, src/ legacy)%RESET%
echo.
python -m pytest tests/unit/ -v --tb=short ^
    --durations=10 ^
    2>&1 | tee -a "%REPORT_FILE%"
set "EXIT_CODE=%ERRORLEVEL%"
goto :done

:: =============================================================================
:: RUN INTEGRATION
:: =============================================================================
:run_integration
echo %BLUE%🧪 A executar testes de INTEGRAÇÃO...%RESET%
echo %YELLOW%   (API Hyperliquid [MOCK], SQLite, EventBus)%RESET%
echo.
python -m pytest tests/integration/ -v --tb=short -m "not live" ^
    2>&1 | tee -a "%REPORT_FILE%"
set "EXIT_CODE=%ERRORLEVEL%"
goto :done

:: =============================================================================
:: RUN SYSTEM
:: =============================================================================
:run_system
echo %BLUE%🧪 A executar testes de SISTEMA (E2E)...%RESET%
echo %YELLOW%   (Paper trading end-to-end — pode demorar)%RESET%
echo.
python -m pytest tests/system/ -v --tb=short ^
    2>&1 | tee -a "%REPORT_FILE%"
set "EXIT_CODE=%ERRORLEVEL%"
goto :done

:: =============================================================================
:: RUN STRESS
:: =============================================================================
:run_stress
echo %BLUE%🧪 A executar STRESS TESTS...%RESET%
echo %YELLOW%   (Throughput, concorrência, memory leak)%RESET%
echo.
python -m pytest tests/stress/ -v --tb=short ^
    2>&1 | tee -a "%REPORT_FILE%"
set "EXIT_CODE=%ERRORLEVEL%"
goto :done

:: =============================================================================
:: RUN REGRESSION (Bugs Conhecidos)
:: =============================================================================
:run_regression
echo %BLUE%🧪 A executar testes de REGRESSÃO...%RESET%
echo %YELLOW%   (Bugs conhecidos do legado)%RESET%
echo %YELLOW%   - _is_price_sane attribute missing%RESET%
echo %YELLOW%   - Valores BTC errados%RESET%
echo %YELLOW%   - Dashboard não interativa%RESET%
echo %YELLOW%   - Erros de encoding (charmap)%RESET%
echo.
python -m pytest tests/regression/ -v --tb=short ^
    2>&1 | tee -a "%REPORT_FILE%"
set "EXIT_CODE=%ERRORLEVEL%"
goto :done

:: =============================================================================
:: RUN COVERAGE
:: =============================================================================
:run_coverage
echo %BLUE%📊 A executar testes com COBERTURA DE CÓDIGO...%RESET%
echo %YELLOW%   Meta: 70%% global, 90%% em arquivos críticos%RESET%
echo.
python -m pytest tests/ -v --tb=short ^
    --cov=clean --cov=refactored --cov=src ^
    --cov-report=html:htmlcov ^
    --cov-report=term-missing ^
    --cov-fail-under=70 ^
    2>&1 | tee -a "%REPORT_FILE%"
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo %CYAN%📊 Relatório HTML: %PROJECT_DIR%\htmlcov\index.html%RESET%
goto :done

:: =============================================================================
:: RUN CI (Continuous Integration)
:: =============================================================================
:run_ci
echo %BLUE%🏭 Modo CI — Sem testes LIVE, com fail-under...%RESET%
echo.
python -m pytest tests/unit tests/integration tests/regression -v --tb=short ^
    -m "not live" ^
    --cov=clean --cov=refactored --cov=src ^
    --cov-report=xml:test_reports\coverage.xml ^
    --cov-fail-under=70 ^
    --junitxml="test_reports\junit.xml" ^
    2>&1 | tee -a "%REPORT_FILE%"
set "EXIT_CODE=%ERRORLEVEL%"
goto :done

:: =============================================================================
:: RUN FAST (Apenas testes rápidos)
:: =============================================================================
:run_fast
echo %BLUE%⚡ Modo RÁPIDO — Apenas testes < 5s...%RESET%
echo.
python -m pytest tests/unit tests/regression -v --tb=short ^
    --durations=10 ^
    -x ^
    2>&1 | tee -a "%REPORT_FILE%"
set "EXIT_CODE=%ERRORLEVEL%"
goto :done

:: =============================================================================
:: RUN LEGACY (src/ apenas)
:: =============================================================================
:run_legacy
echo %BLUE%📦 A executar testes do código LEGADO (src/)...%RESET%
echo.
python -m pytest tests/unit/src/ -v --tb=short ^
    2>&1 | tee -a "%REPORT_FILE%"
set "EXIT_CODE=%ERRORLEVEL%"
goto :done

:: =============================================================================
:: RUN CLEAN (clean/ apenas)
:: =============================================================================
:run_clean
echo %BLUE%🏛️ A executar testes da CLEAN ARCHITECTURE...%RESET%
echo.
python -m pytest tests/unit/clean/ -v --tb=short ^
    2>&1 | tee -a "%REPORT_FILE%"
set "EXIT_CODE=%ERRORLEVEL%"
goto :done

:: =============================================================================
:: SHOW HELP
:: =============================================================================
:show_help
echo.
echo %CYAN%╔══════════════════════════════════════════════════════════════╗%RESET%
echo %CYAN%║                      🧪 AJUDA — Test Runner                  ║%RESET%
echo %CYAN%╠══════════════════════════════════════════════════════════════╣%RESET%
echo %CYAN%║  Uso: run_tests.bat [modo]                                   ║%RESET%
echo %CYAN%║                                                              ║%RESET%
echo %CYAN%║  Modos disponíveis:                                          ║%RESET%
echo %CYAN%║    all          → Todos os testes (padrão)                   ║%RESET%
echo %CYAN%║    unit         → Testes unitários por camada                ║%RESET%
echo %CYAN%║    integration  → Testes de integração (API, DB, EventBus)   ║%RESET%
echo %CYAN%║    system       → Testes de sistema (E2E paper trading)      ║%RESET%
echo %CYAN%║    stress       → Stress tests e performance                 ║%RESET%
echo %CYAN%║    regression   → Regressão — bugs conhecidos do legado      ║%RESET%
echo %CYAN%║    coverage     → Todos os testes com relatório de cobertura ║%RESET%
echo %CYAN%║    ci           → Modo CI (sem LIVE, com fail-under)         ║%RESET%
echo %CYAN%║    fast         → Apenas testes rápidos (^<5s cada)          ║%RESET%
echo %CYAN%║    legacy       → Apenas código legado (src/)                ║%RESET%
echo %CYAN%║    clean        → Apenas Clean Architecture (clean/)         ║%RESET%
echo %CYAN%║    help         → Mostra esta ajuda                          ║%RESET%
echo %CYAN%║                                                              ║%RESET%
echo %CYAN%║  Exemplos:                                                   ║%RESET%
echo %CYAN%║    run_tests.bat                                             ║%RESET%
echo %CYAN%║    run_tests.bat coverage                                    ║%RESET%
echo %CYAN%║    run_tests.bat fast                                        ║%RESET%
echo %CYAN%║    run_tests.bat regression                                  ║%RESET%
echo %CYAN%╚══════════════════════════════════════════════════════════════╝%RESET%
echo.
exit /b 0

:: =============================================================================
:: DONE — Resultado Final
:: =============================================================================
:done
echo.
echo %CYAN%═══════════════════════════════════════════════════════════════%RESET%

if %EXIT_CODE% equ 0 (
    echo %GREEN%✅ TODOS OS TESTES PASSARAM!%RESET%
    echo %GREEN%   O bot está pronto para paper trading.%RESET%
) else (
    echo %RED%❌ ALGUNS TESTES FALHARAM (exit code: %EXIT_CODE%)%RESET%
    echo %YELLOW%   Verifica o relatório: %REPORT_FILE%%RESET%
    echo %YELLOW%   Corrige os erros antes de continuar.%RESET%
)

echo %CYAN%═══════════════════════════════════════════════════════════════%RESET%
echo %BLUE%📄 Relatório: %REPORT_FILE%%RESET%

if "%MODE%"=="coverage" (
    echo %BLUE%📊 Cobertura HTML: %PROJECT_DIR%\htmlcov\index.html%RESET%
    start "" "%PROJECT_DIR%\htmlcov\index.html"
)

echo.
echo %CYAN%Pressiona qualquer tecla para sair...%RESET%
pause >nul
exit /b %EXIT_CODE%
