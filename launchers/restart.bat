@echo off
rem Force-quit any old instance, then launch a fresh one
setlocal
cd /d "%~dp0.."
echo Stopping old instance...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\tools\kill_app.ps1" -Root "%~dp0..\."
ping -n 2 127.0.0.1 >nul
echo Starting...
start "" pythonw "app\main.py"
echo Done.
