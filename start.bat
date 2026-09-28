@echo off
REM ===============================================================
REM  Dorm electricity checker - Windows launcher
REM  Just double-click this file.
REM    1) find a usable Python
REM    2) install missing packages
REM    3) start the local server and open the browser
REM
REM  NOTE: keep this file pure ASCII. cmd.exe reads .bat files with
REM  the system code page (GBK on zh-CN Windows), so UTF-8 Chinese
REM  comments here would be executed as garbage commands.
REM ===============================================================
cd /d "%~dp0"
if not defined PORT set "PORT=8788"

REM already running? then just open the browser instead of crashing on the port
netstat -ano | findstr /c:":%PORT% " | findstr /i "LISTENING" >nul 2>nul
if not errorlevel 1 (
  echo [*] Port %PORT% is already in use - opening the browser.
  echo     If the page looks outdated, close the old server window first.
  start "" "http://127.0.0.1:%PORT%"
  ping -n 2 127.0.0.1 >nul
  exit /b 0
)

set "PY="
REM try in order: env var SZU_PY -> existing local env -> python on PATH -> py launcher
if defined SZU_PY if exist "%SZU_PY%" set "PY=%SZU_PY%"
if not defined PY if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not defined PY ( python -c "import sys" >nul 2>nul && set "PY=python" )
if not defined PY ( py -3 -c "import sys" >nul 2>nul && set "PY=py -3" )
if not defined PY (
  echo.
  echo [x] Python not found.
  echo     Please install Python 3.10 or newer, and CHECK the box
  echo     "Add python.exe to PATH" during setup:
  echo     https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)
echo [*] Python: %PY%

REM install only when something is missing; retry via a China mirror if pypi.org is blocked
%PY% -c "import flask, requests, bs4" >nul 2>nul || %PY% -m pip install -r requirements.txt
%PY% -c "import flask, requests, bs4" >nul 2>nul || %PY% -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/

start "" "http://127.0.0.1:%PORT%"
%PY% app.py
pause
