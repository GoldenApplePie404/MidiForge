@echo off
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 goto :no_python

if not exist ".venv\Scripts\python.exe" goto :first_run
goto :launch

:first_run
echo [First run] Setting up virtual environment...
python -m venv .venv
if errorlevel 1 goto :fail
echo [First run] Installing dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :fail
echo [OK] Ready.

:launch
echo [Start] Launching MidiForge...
echo.
".venv\Scripts\python.exe" app.py
if errorlevel 1 goto :fail
echo.
echo [Done] Program exited.
pause
exit /b 0

:no_python
echo.
echo [ERROR] Python not found!
echo Install: https://www.python.org/downloads/windows/
echo IMPORTANT: check "Add Python to PATH"
echo.
pause
exit /b 1

:fail
echo.
echo [Error] Launch failed.
pause
exit /b 1
