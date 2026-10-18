@echo off
rem Launch the English-to-Chinese translator (no console window)
cd /d "%~dp0"
where pythonw >nul 2>nul
if errorlevel 1 (
  echo [ERROR] pythonw not found. Please install Python 3.10+ and enable "Add Python to PATH".
  pause
  exit /b 1
)
start "" pythonw "app\main.py"
