@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto dependencies
py -3 -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1
if not errorlevel 1 (
  py -3 -m venv .venv
) else (
  python -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1
  if errorlevel 1 goto missing
  python -m venv .venv
)
if errorlevel 1 goto failed

:dependencies
".venv\Scripts\python.exe" -c "import numpy; from websockets.asyncio.client import connect" >nul 2>&1
if not errorlevel 1 goto run
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed

:run
".venv\Scripts\python.exe" -X utf8 server.py %*
exit /b %errorlevel%

:missing
echo Python 3.11 or newer is required. Install it from https://www.python.org/downloads/windows/
exit /b 1

:failed
echo Could not create the Python environment or install dependencies.
exit /b 1
