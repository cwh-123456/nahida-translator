@echo off
rem First-time setup: install Python packages, then download engine + model (~5GB, resumable)
cd /d "%~dp0.."
echo ============================================================
echo  1/2 Installing Python packages (~30MB)
echo  2/2 Downloading llama.cpp + Qwen2.5-7B model (~5GB)
echo ============================================================
where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found. Please install Python 3.10+ and enable "Add Python to PATH".
  pause
  exit /b 1
)
python -m pip install -r "config\requirements.txt"
if errorlevel 1 (
  echo [ERROR] Failed to install Python packages.
  pause
  exit /b 1
)
python "tools\download.py"
if errorlevel 1 (
  echo [ERROR] Download incomplete. Run setup.bat again to resume.
  pause
  exit /b 1
)
echo.
echo Setup finished. Double-click start.bat in the root folder to launch.
pause
