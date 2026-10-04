@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 goto run_py
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 goto run_python

echo Python 3.10 or newer was not found.
echo Install Python from https://www.python.org/downloads/windows/
echo Enable "Add Python to PATH", then reopen this file.
set "BOT_EXIT=1"
goto finish

:run_py
py -3 bot.py %*
set "BOT_EXIT=%ERRORLEVEL%"
goto finish

:run_python
python bot.py %*
set "BOT_EXIT=%ERRORLEVEL%"

:finish
echo.
if not "%BOT_EXIT%"=="0" echo Read WINDOWS-fa.md for connection and setup help.
pause
exit /b %BOT_EXIT%
