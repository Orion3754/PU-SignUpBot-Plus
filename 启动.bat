@echo off
chcp 65001 >nul
cd /d "%~dp0"
title PU-SignUpBot

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo [ERROR] .venv not found in this folder.
    echo Please run:  uv sync
    echo.
    pause
    exit /b 1
)

echo ==========================================
echo   PU-SignUpBot  is starting...
echo   Do NOT close this window.
echo ==========================================
echo.

".venv\Scripts\python.exe" main.py

echo.
echo ==========================================
echo   Program finished / stopped.
echo ==========================================
pause
