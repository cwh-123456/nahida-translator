@echo off
rem Force-quit the translator (use when the tray / right-click menu quit does not work)
setlocal
cd /d "%~dp0.."
echo Stopping the translator...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\tools\kill_app.ps1" -Root "%~dp0..\."
echo Done. Start it again with start.bat in the root folder
pause
