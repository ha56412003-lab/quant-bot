@echo off
cd /d "%~dp0"
if not exist logs mkdir logs

netstat -ano | findstr ":8899" | findstr LISTENING >nul 2>&1
if not errorlevel 1 goto ready

start "" wscript.exe "%~dp0run_hidden.vbs"

ping -n 6 127.0.0.1 >nul
netstat -ano | findstr ":8899" | findstr LISTENING >nul 2>&1
if not errorlevel 1 goto ready

ping -n 8 127.0.0.1 >nul
netstat -ano | findstr ":8899" | findstr LISTENING >nul 2>&1
if not errorlevel 1 goto ready

echo.
echo ==========================================
echo  Startup FAILED
echo  Please send this file to Luo Xiaohei:
echo  %~dp0logs\server.log
echo ==========================================
echo.
pause
exit /b 1

:ready
start "" http://127.0.0.1:8899
exit /b 0
