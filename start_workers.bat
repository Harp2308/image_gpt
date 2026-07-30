@echo off
setlocal EnableDelayedExpansion

cd /d "%~dp0"

echo ========================================
echo  Colep AI - Workers Setup
echo ========================================

:: ----------------------------------------
:: 1. Check .venv exists
:: ----------------------------------------
echo.
echo [1/4] Checking virtual environment...
if not exist ".venv\Scripts\python.exe" (
    echo ERROR: .venv not found. Run 'uv sync' first.
    pause
    exit /b 1
)
echo .venv OK.

:: ----------------------------------------
:: 2. Check Memurai (Redis) is running
:: ----------------------------------------
echo.
echo [2/4] Checking Memurai (Redis)...
sc query Memurai >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Memurai service not found. Install Memurai first.
    pause
    exit /b 1
)

sc query Memurai | find "RUNNING" >nul 2>&1
if %errorlevel% neq 0 (
    echo Memurai not running — starting...
    net start Memurai >nul 2>&1
    timeout /t 3 /nobreak >nul
)

"C:\Program Files\Memurai\memurai-cli.exe" ping >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Redis not responding after start. Check Memurai service.
    pause
    exit /b 1
)
echo Redis is up.

:: ----------------------------------------
:: 3. Check .env exists
:: ----------------------------------------
echo.
echo [3/4] Checking .env...
if not exist ".env" (
    echo ERROR: .env file not found. Copy .env.example and fill in values.
    pause
    exit /b 1
)
echo .env OK.

:: ----------------------------------------
:: 4. Launch FastAPI + Watchdog
:: ----------------------------------------
echo.
echo [4/4] Launching services...


:: Watchdog — manages all 4 Celery workers (orchestration/ingestion/indexing/cleanup)
:: Each worker gets its own console window via CREATE_NEW_CONSOLE in watchdog.py
start "Colep AI - Watchdog" cmd /k ".venv\Scripts\python.exe watchdog.py"

echo.
echo ===========================================================
echo  Colep AI started successfully.
echo ===========================================================
echo.
echo  Services launched:
echo    - Watchdog      managing 4 Celery workers:
echo        * worker.orchestration  (concurrency=2)
echo        * worker.ingestion      (concurrency=4)
echo        * worker.indexing       (concurrency=8)
echo        * worker.cleanup        (concurrency=8)
echo.
echo.
pause
endlocal
