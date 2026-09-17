@echo off
cd /d "%~dp0"

REM ============================================
REM  MidiForge - One-click launcher
REM  (First run: create venv and install deps)
REM ============================================

REM ---- Step 0: Python check ----
echo [Check] Looking for Python...

where python >nul 2>&1
if errorlevel 1 goto :no_python

REM Let Python check its own version (avoids all bat variable-expansion pitfalls)
python -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" 2>nul
if errorlevel 1 goto :py_too_old

for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do echo [Check] Python %%v OK
goto :py_ok

:no_python
echo.
echo [ERROR] Python not found!
echo.
echo Please install Python 3.11 or higher:
echo   https://www.python.org/downloads/windows/
echo.
echo IMPORTANT: check "Add Python to PATH" during installation
echo.
pause
exit /b 1

:py_too_old
echo.
echo [ERROR] Python is too old. Need 3.11+.
echo Download: https://www.python.org/downloads/windows/
echo.
pause
exit /b 1

:py_ok

REM ---- Step 1: venv + deps ----
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo [First run] Virtual env not found, creating...
    python -m venv .venv
    if errorlevel 1 goto :fail

    echo [First run] Installing dependencies (this may take a few minutes)...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto :fail

    echo [OK] Dependencies installed.
    echo.
)

REM ---- Step 2: Launch ----
echo [Start] Launching MidiForge...
echo.
".venv\Scripts\python.exe" app.py
if errorlevel 1 goto :fail

echo.
echo [Done] Program exited.
pause
exit /b 0

:fail
echo.
echo [Error] Launch failed. Check messages above.
pause
exit /b 1
