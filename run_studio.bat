@echo off
REM ==============================================================================
REM FlowKit Studio Launcher (Windows 1-Click)
REM ==============================================================================
title FlowKit Studio - Google Flow Multi-Profile Batch Generator
cd /d "%~dp0"

echo ==========================================================
echo    ✨ FLOWKIT STUDIO - 1-CLICK LAUNCHER (WINDOWS)
echo ==========================================================

REM Check if Python is installed
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Khong tim thay Python! Vui long cai dat Python 3.10+ va tich vao "Add Python to PATH".
    pause
    exit /b 1
)

REM Setup / Activate venv
if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
) else (
    echo [INFO] Dang tao virtual environment (venv)...
    python -m venv venv
    call venv\Scripts\activate.bat
    python -m pip install --upgrade pip
    pip install -r requirements.txt
    pip install playwright python-multipart pytest-mock
)

REM Launch FlowKit Studio
python flowkit_studio.py
if %errorlevel% neq 0 (
    echo [ERROR] FlowKit Studio da dung voi loi %errorlevel%.
    pause
)
