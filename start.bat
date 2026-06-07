@echo off
title Heaven - Uma Breeding and Analysis
cd /d "%~dp0"

echo.
echo  =============================================
echo   Project Heaven
echo   http://127.0.0.1:1620
echo  =============================================
echo.
echo  Close this window to stop (Ctrl+C).
echo.

:: Open browser after 1s grace period
start "" /b cmd /c "timeout /t 2 /nobreak >nul & start http://127.0.0.1:1620"

:: Single process, no --reload: the in-app "Update & restart" button re-execs
:: this same entry point, and uvicorn's reloader does not coexist with that
:: (it left orphaned servers fighting over the port). Restart picks up new code.
python server.py
