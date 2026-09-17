@echo off
cd /d "%~dp0"

REM ============================================
REM  MidiForge - One-click launcher
REM  (First run: create venv and install deps)
REM ============================================

if not exist ".venv\Scripts\python.exe" (
    echo [First run] Virtual env not found, creating...
    python -m venv .venv
    if errorlevel 1 goto :fail

    echo [First run] Installing dependencies, this may take a few minutes...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements-dev.txt
    if errorlevel 1 goto :fail

    echo [OK] Dependencies installed.
)

echo [Start] Launching MidiForge...
".venv\Scripts\python.exe" app.py
if errorlevel 1 goto :fail

echo.
echo [Done] Program exited.
pause
exit /b 0

:fail
echo.
echo [Error] Launch failed, check the messages above.
pause
exit /b 1
