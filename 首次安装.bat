@echo off
chcp 65001 >nul
cd /d "%~dp0"
title PU-SignUpBot - first install

set "UV=uv"
where uv >nul 2>nul
if not errorlevel 1 goto run

if exist "%USERPROFILE%\.local\bin\uv.exe" (
    set "UV=%USERPROFILE%\.local\bin\uv.exe"
    goto run
)

echo.
echo [ERROR] "uv" not found.
echo.
echo   Install uv first:
echo     1. Press Win key, type  powershell  , press Enter
echo     2. Paste this line and press Enter:
echo        powershell -c "irm https://astral.sh/uv/install.ps1 ^| iex"
echo     3. Close PowerShell, then double-click this file again
echo.
pause
exit /b 1

:run
echo Downloading Python + dependencies, this may take a few minutes.
echo Do NOT close this window.
echo.
"%UV%" sync
if errorlevel 1 goto fail

echo.
echo ==========================================
echo   Install finished.
echo   Next: open the .txt guide file in this
echo   folder and follow  step 2  (it tells you
echo   which file to double-click next).
echo ==========================================
pause
exit /b 0

:fail
echo.
echo [ERROR] Install failed. Check your network (do NOT use a VPN), then retry.
echo.
pause
exit /b 1
