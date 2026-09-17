@echo off
cd /d "%~dp0"

REM ============================================
REM  MidiForge - One-click launcher
REM  (First run: create venv and install deps)
REM ============================================

REM ---- Step 0: Python 自检 ----
echo [Check] Looking for Python...

where python >nul 2>&1
if errorlevel 1 (
    echo.
    echo [ERROR] 没找到 Python！
    echo.
    echo 请先安装 Python 3.11 或更高版本:
    echo   https://www.python.org/downloads/windows/
    echo.
    echo ★ 安装时务必勾选 "Add Python to PATH" ★
    echo.
    pause
    exit /b 1
)

REM 检查版本 >= 3.11
for /f "tokens=*" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo [Check] Found %PYVER%
for /f "tokens=2 delims= " %%m in ('python --version 2^>^&1') do set PYMAJOR=%%m
for /f "tokens=1,2 delims=." %%a in ("%PYMAJOR%") do (
    set /a MAJ=%%a
    set /a MIN=%%b
)
if %MAJ% LSS 3 goto :py_too_old
if %MAJ% EQU 3 if %MIN% LSS 11 goto :py_too_old
goto :py_ok

:py_too_old
echo.
echo [ERROR] Python 版本过低（%PYMAJOR%）。需要 3.11+。
echo 下载: https://www.python.org/downloads/windows/
echo.
pause
exit /b 1

:py_ok

REM ---- Step 1: venv + 依赖 ----
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo [First run] Virtual env not found, creating...
    python -m venv .venv
    if errorlevel 1 goto :fail

    echo [First run] Installing dependencies, this may take a few minutes...
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
echo [Error] Launch failed, check the messages above.
pause
exit /b 1
