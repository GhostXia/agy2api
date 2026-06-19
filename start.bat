@echo off
chcp 65001 >nul 2>&1
setlocal EnableDelayedExpansion

:: agy2api - One-click startup script
:: Make sure Antigravity CLI (agy) is installed and logged in before using.

set "PROJECT_DIR=%~dp0"
set "VENV_DIR=%PROJECT_DIR%.venv"
set "PYTHON=python"
set "PIPX=%LOCALAPPDATA%\pipx"

:: Check Python
where !PYTHON! >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.9+ and add it to PATH.
    goto :fail
)

:: Create virtual environment if not exists
if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo [SETUP] Creating virtual environment...
    !PYTHON! -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        goto :fail
    )
)

:: Set Python to venv
set "PYTHON=%VENV_DIR%\Scripts\python.exe"
set "PIP=%VENV_DIR%\Scripts\pip.exe"

:: Install dependencies if needed
!PYTHON! -c "import fastapi" >nul 2>&1
if errorlevel 1 (
    echo [SETUP] Installing dependencies...
    !PIP! install -r "%PROJECT_DIR%requirements.txt" -q
    if errorlevel 1 (
        echo [ERROR] Failed to install dependencies.
        goto :fail
    )
)

:: Set default API key if not already configured
if not defined AGY2API_KEY (
    set "AGY2API_KEY=pwd"
    echo [INFO] AGY2API_KEY not set, using default.
)

:: Start server
echo.
echo ========================================
echo   agy2api is starting...
echo   API Key: !AGY2API_KEY!
echo   Press Ctrl+C to stop.
echo ========================================
echo.

"%PYTHON%" "%PROJECT_DIR%server.py"
goto :eof

:fail
echo.
echo [FAILED] Press any key to exit...
pause >nul
exit /b 1
