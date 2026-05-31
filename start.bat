@echo off
title Heaven - Uma Breeding and Analysis
cd /d "%~dp0"

echo.
echo  =============================================
echo   Project Heaven
echo   http://127.0.0.1:1620
echo  =============================================
echo.
echo  Server auto-reloads on code changes.
echo  Close this window to stop (Ctrl+C).
echo.

:: Open browser after 1s grace period
start "" /b cmd /c "timeout /t 2 /nobreak >nul & start http://127.0.0.1:1620"

:: Start with reload so it picks up changes
python -m uvicorn server:app --host 127.0.0.1 --port 1620 --reload --log-level warning
