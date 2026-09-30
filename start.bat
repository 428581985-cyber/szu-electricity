@echo off
setlocal
REM ===============================================================
REM  Dorm electricity checker - Windows launcher
REM  Just double-click this file.
REM    1) find Python (or offer to download + install it)
REM    2) create a desktop shortcut on the very first run
REM    3) install missing packages
REM    4) start the local server and open the browser
REM
REM  NOTE: keep this file pure ASCII. cmd.exe reads .bat files with
REM  the system code page (GBK on zh-CN Windows), so UTF-8 Chinese
REM  comments here would be executed as garbage commands.
REM ===============================================================
cd /d "%~dp0"
if not defined PORT set "PORT=8788"

REM ---------- 0) already running? just open the browser ----------
netstat -ano | findstr /c:":%PORT% " | findstr /i "LISTENING" >nul 2>nul
if not errorlevel 1 (
  echo [*] Port %PORT% is already in use - opening the browser.
  echo     If the page looks outdated, close the old server window first.
  start "" "http://127.0.0.1:%PORT%"
  ping -n 2 127.0.0.1 >nul
  exit /b 0
)

REM ---------- 1) look for an existing Python (never install a second one) ----------
set "PY="
REM 1. env var override  2. existing local env  3. python on PATH  4. py launcher  5. common install dirs
if defined SZU_PY if exist "%SZU_PY%" set "PY=%SZU_PY%"
if not defined PY if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not defined PY ( python -c "import sys" >nul 2>nul && set "PY=python" )
if not defined PY ( py -3 -c "import sys" >nul 2>nul && set "PY=py -3" )
if not defined PY call :scan_common_paths

REM still nothing? ask before touching the machine
if not defined PY (
  echo.
  echo [!] No Python found on this computer.
  echo     This tool needs Python 3.10 or newer. I can download and install it
  echo     for you: official installer, about 25 MB, current user only, no admin.
  echo.
  set "CH="
  set /p CH=    Install Python now? [Y/N]:
  if /i "%CH%"=="Y" call :install_python
)
if not defined PY (
  echo.
  echo [x] Python was not installed, so the server cannot start.
  echo     Install it manually from:
  echo       https://www.python.org/downloads/
  echo     During setup CHECK the box "Add python.exe to PATH", then run me again.
  echo.
  pause
  exit /b 1
)
echo [*] Found Python: %PY%
"%PY%" -V

REM ---------- 2) first run: put a shortcut on the desktop ----------
if not exist "config.json" call :make_shortcut

REM ---------- 3) packages: install only what is missing ----------
"%PY%" -c "import flask, requests, bs4, qrcode" >nul 2>nul || "%PY%" -m pip install -r requirements.txt
"%PY%" -c "import flask, requests, bs4, qrcode" >nul 2>nul || "%PY%" -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/

REM ---------- 4) go ----------
echo.
echo [*] Server: http://127.0.0.1:%PORT%      (keep this window open; close it to stop)
echo.
start "" "http://127.0.0.1:%PORT%"
"%PY%" app.py
pause
exit /b 0

REM ===============================================================
REM  subroutines
REM ===============================================================

:scan_common_paths
if defined PY exit /b 0
for %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%~fD\python.exe" set "PY=%%~fD\python.exe"
if defined PY exit /b 0
for %%D in ("%ProgramFiles%\Python3*") do if exist "%%~fD\python.exe" set "PY=%%~fD\python.exe"
if defined PY exit /b 0
for %%D in ("C:\Python3*") do if exist "%%~fD\python.exe" set "PY=%%~fD\python.exe"
exit /b 0

:install_python
set "VER=3.12.8"
set "SETUP=%TEMP%\python-%VER%-amd64.exe"
set "PRIMARY=https://mirrors.huaweicloud.com/python/%VER%/python-%VER%-amd64.exe"
set "FALLBACK=https://www.python.org/ftp/python/%VER%/python-%VER%-amd64.exe"
echo.
echo [*] Downloading Python %VER% ...
where curl >nul 2>nul
if not errorlevel 1 (
  curl -L --fail --progress-bar -o "%SETUP%" "%PRIMARY%"
  if not exist "%SETUP%" curl -L --fail --progress-bar -o "%SETUP%" "%FALLBACK%"
) else (
  powershell -NoProfile -Command "$ProgressPreference='SilentlyContinue'; $o=Join-Path $env:TEMP 'python-%VER%-amd64.exe'; try { Invoke-WebRequest -Uri '%PRIMARY%' -OutFile $o } catch { try { Invoke-WebRequest -Uri '%FALLBACK%' -OutFile $o } catch { exit 1 } }"
)
if not exist "%SETUP%" (
  echo [x] Download failed. Install Python manually from:
  echo       https://www.python.org/downloads/
  exit /b 0
)
echo [*] Installing (silent, current user only, takes about a minute) ...
"%SETUP%" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_test=0
del "%SETUP%" >nul 2>nul
echo [*] Install finished, locating python.exe ...
call :scan_common_paths
exit /b 0

:make_shortcut
REM The shortcut name is built from char codes on purpose: this file must stay
REM pure ASCII, otherwise cmd.exe (GBK on zh-CN Windows) would misread it.
echo [*] First run - creating a desktop shortcut ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$n=[string]([char]0x5BBF+[char]0x820D+[char]0x7535+[char]0x8D39+[char]0x67E5+[char]0x8BE2); $p=Join-Path ([Environment]::GetFolderPath('Desktop')) ($n+'.lnk'); if (Test-Path $p) { Write-Host '    shortcut already exists - skipped' } else { $ws=New-Object -ComObject WScript.Shell; $s=$ws.CreateShortcut($p); $s.TargetPath='%~f0'; $s.WorkingDirectory='%~dp0'; $s.Description='Dorm electricity checker'; $s.Save(); Write-Host ('    created: '+$p) }"
exit /b 0
