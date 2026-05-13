@echo off
REM PDF Text Sanitiser — start server and open browser
REM Requires: Python 3.11+, pip install -r requirements.txt, Ghostscript on PATH

setlocal
cd /d "%~dp0"

REM Check Python
where python >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Python not found on PATH.
    pause
    exit /b 1
)

REM Check gswin64c
where gswin64c >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo WARNING: gswin64c not found on PATH.
    echo          Ghostscript is required for the text-outlining pass.
    echo          Download from https://www.ghostscript.com/releases/
    echo.
)

REM Install deps if needed (idempotent)
python -m pip install -q -r requirements.txt

REM Create sessions dir
if not exist sessions mkdir sessions

REM Open browser after a short delay (background)
start "" cmd /c "timeout /t 2 >nul && start http://localhost:8000"

REM Start server
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
