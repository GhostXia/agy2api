@echo off
chcp 65001 >nul 2>&1
setlocal EnableDelayedExpansion

:: agy2api - One-click startup script
:: Make sure Antigravity CLI (agy) is installed and logged in before using.
:: agy stores its OAuth token in the Windows Credential Manager, which is
:: shared by agy2api (including the stateful-mode sandbox), so no separate
:: login is needed here. We only check the credential exists.

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

:: Check agy is logged in (Windows Credential Manager holds the OAuth token).
:: This is the real auth source for both stateless and stateful mode; the
:: stateful sandbox inherits it automatically. We never run a login flow here
:: to avoid confusing it with agy2api's own API key. The credential block has
:: indented detail lines when present, none when absent — parsed in Python to
:: stay locale-independent (cmdkey's "no credential" marker is localized).
!PYTHON! -c "import subprocess,sys; r=subprocess.run(['cmdkey','/list:gemini:antigravity'],capture_output=True,text=True,encoding='utf-8',errors='replace'); sys.exit(0 if [l for l in r.stdout.splitlines() if l.startswith('    ') and l.strip()] else 1)"
if errorlevel 1 (
    echo [ERROR] agy is not logged in.
    echo.
    echo agy2api uses agy under the hood. Please log in to agy FIRST:
    echo   1. Open a normal terminal ^(NOT this script^)
    echo   2. Run:  agy
    echo   3. Complete the Google sign-in, then exit agy
    echo   4. Run this script again
    echo.
    echo ^(Login is stored in Windows Credential Manager and is shared by
    echo  agy2api, including the stateful-mode sandbox. You do NOT log in here.^)
    goto :fail
)
echo [OK] agy login detected.

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
