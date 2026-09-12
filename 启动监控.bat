@echo off
chcp 65001 >nul
cd /d "%~dp0"
title PU Activity Watcher (auto signup)

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found. Please run:  uv sync
    pause
    exit /b 1
)

echo ==================================================
echo   PU-SignUpBot  --  WATCH MODE
echo.
echo   Rule : auto-join every NEW labor-education activity
echo   Scope: whole school, ignore grade / credit / audit
echo          even when slots are 0, still try
echo.
echo   Keep this window OPEN. Do NOT let the PC sleep.
echo   Press Ctrl+C to stop.
echo ==================================================
echo.

".venv\Scripts\python.exe" watch.py

echo.
echo ==================================================
echo   Watcher stopped.
echo ==================================================
pause
