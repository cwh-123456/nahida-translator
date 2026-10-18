@echo off
rem Debug launch: keeps the console window so logs and errors are visible
cd /d "%~dp0.."
echo ============================================================
echo  Translator debug mode (console kept open)
echo ============================================================
python "app\main.py" --debug
pause
