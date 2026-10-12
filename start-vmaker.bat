@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title V-Maker Launcher

 echo.
 echo ========================================
 echo       V-Maker AI Animation Studio
 echo ========================================
 echo.

rem Prefer the project virtual environment when available.
if exist "%CD%\.venv\Scripts\python.exe" (
    set "VMAKER_PYTHON=%CD%\.venv\Scripts\python.exe"
) else (
    where python >nul 2>nul
    if errorlevel 1 goto :python_missing
    set "VMAKER_PYTHON=python"
)

where pnpm >nul 2>nul
if errorlevel 1 goto :pnpm_missing
if not exist "%CD%\node_modules" goto :node_modules_missing

"%VMAKER_PYTHON%" -c "import fastapi,uvicorn,pydantic" >nul 2>nul
if errorlevel 1 goto :python_packages_missing

set "VMAKER_HOST=127.0.0.1"
set "VMAKER_PORT=8765"

 echo [OK] Python: %VMAKER_PYTHON%

rem Optional extras decide whether the export paths can do anything useful. Report them here: a
rem missing extra used to surface only later, as an export that silently fell back to a draft package.
set "EXPORT_MP4=no"
"%VMAKER_PYTHON%" -c "import imageio_ffmpeg" >nul 2>nul
if not errorlevel 1 set "EXPORT_MP4=yes"
if "%EXPORT_MP4%"=="no" (
    where ffmpeg >nul 2>nul
    if not errorlevel 1 set "EXPORT_MP4=yes"
)
if "%EXPORT_MP4%"=="yes" (
     echo [OK] MP4 export: an FFmpeg binary is available.
) else (
     echo [WARN] MP4 export needs FFmpeg. Install the optional extra, put ffmpeg on PATH, or set
     echo        VMAKER_FFMPEG_PATH. Without it, MP4 requests fall back to a draft package.
)

set "EXPORT_JY=no"
"%VMAKER_PYTHON%" -c "import pyJianYingDraft" >nul 2>nul
if not errorlevel 1 set "EXPORT_JY=yes"
if "%EXPORT_JY%"=="yes" (
     echo [OK] Jianying draft export: pyJianYingDraft is installed.
) else (
     echo [WARN] Jianying draft export needs pyJianYingDraft. Without it, draft requests fall back
     echo        to the portable draft package.
)

if "%EXPORT_MP4%%EXPORT_JY%"=="nono" (
     echo [TIP] Install both with: "%VMAKER_PYTHON%" -m pip install -e ".[ffmpeg,jianying]"
)

if /I "%~1"=="check" (
     echo.
     echo [INFO] check only - no services were started.
     exit /b 0
)

rem "backend" starts only the API and leaves the UI to whoever serves it (the Tauri desktop shell,
rem for example). Without this, a desktop window that cannot reach the API has no obvious remedy.
if /I "%~1"=="backend" goto :backend_only

rem Service control goes through the manager so there is exactly one owner of the process.
if /I "%~1"=="stop" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%CD%\scripts\service.ps1" stop
    exit /b %ERRORLEVEL%
)
if /I "%~1"=="status" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%CD%\scripts\service.ps1" status
    exit /b %ERRORLEVEL%
)
if /I "%~1"=="restart" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%CD%\scripts\service.ps1" restart
    exit /b %ERRORLEVEL%
)
if /I "%~1"=="logs" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%CD%\scripts\service.ps1" logs
    exit /b %ERRORLEVEL%
)
rem "doctor" prints everything needed to diagnose a failed start in one screen.
if /I "%~1"=="doctor" goto :doctor

rem Reuse services that are already listening. Starting a second copy only produces a window full of
rem "port already in use" errors, and a browser tab pointed at the older server goes stale.
rem Exit code encodes both probes: 30 = both up, 10 = backend only, 20 = frontend only, 0 = neither.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$back=$false; $front=$false; try{$back=(New-Object Net.Sockets.TcpClient).ConnectAsync('127.0.0.1',8765).Wait(400)}catch{}; try{$front=(New-Object Net.Sockets.TcpClient).ConnectAsync('127.0.0.1',1420).Wait(400)}catch{}; if($back -and $front){exit 30}; if($back){exit 10}; if($front){exit 20}; exit 0"
set "RUNNING=%ERRORLEVEL%"

if "%RUNNING%"=="30" (
    echo [INFO] Backend and frontend are already running - reusing both.
    set "SKIP_BACKEND=1"
    set "SKIP_FRONTEND=1"
)
if "%RUNNING%"=="10" (
    echo [INFO] Backend already running on 127.0.0.1:8765 - reusing it.
    set "SKIP_BACKEND=1"
)
if "%RUNNING%"=="20" (
    echo [INFO] Frontend already running on 127.0.0.1:1420 - reusing it.
    set "SKIP_FRONTEND=1"
)

if not defined SKIP_BACKEND (
    rem Delegated to the manager: it owns the PID file and the log, so the process can be queried
    rem and stopped later instead of living in a window nobody can find.
    powershell -NoProfile -ExecutionPolicy Bypass -File "%CD%\scripts\service.ps1" start
    if errorlevel 1 goto :startup_failed
)

if not defined SKIP_FRONTEND (
    echo [INFO] Starting frontend in a separate window...
    start "V-Maker Frontend" /D "%CD%" cmd /k "pnpm dev:web -- --host 127.0.0.1 --port 1420"
)

 rem First start (or first start after a reboot) can be slow: Vite has to optimise dependencies.
 rem 30 seconds was too tight and produced a failure message on a machine that was merely slow.
 echo [INFO] Waiting for services (up to 90 seconds; first start is slower)...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$deadline=(Get-Date).AddSeconds(90); $front=$false; $back=$false; $tick=0; while((Get-Date)-lt $deadline){ try{$back=((Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8765/api/health' -TimeoutSec 2).StatusCode -eq 200)}catch{}; try{$front=((Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:1420' -TimeoutSec 2).StatusCode -eq 200)}catch{}; if($front -and $back){break}; $tick=$tick+1; if($tick -ge 10){ $tick=0; Write-Host ('  ... waiting: backend=' + $back + ' frontend=' + $front) }; Start-Sleep -Milliseconds 1000 }; if($front -and $back){exit 0}else{exit 1}"

if errorlevel 1 goto :startup_failed

 echo [OK] V-Maker is ready.
 echo [INFO] Opening http://127.0.0.1:1420
 echo [TIP] If the page looks out of date, press Ctrl + Shift + R to force a reload.
start "" "http://127.0.0.1:1420"
 echo.
 echo You may close this launcher window.
 echo The backend runs hidden under scripts\service.ps1 - status: start-vmaker.bat status
 echo Keep the "V-Maker Frontend" window open while using V-Maker.
rem 不要用 timeout.exe：输入被重定向时它会以 "Input redirection is not supported" 直接失败退出。
powershell -NoProfile -Command "Start-Sleep -Seconds 4"
exit /b 0

:python_missing
echo [ERROR] Python was not found.
echo Install Python 3.12+ or create .venv first.
goto :failed

:backend_only
rem Delegated to the manager so the process is queryable and stoppable afterwards.
powershell -NoProfile -ExecutionPolicy Bypass -File "%CD%\scripts\service.ps1" start
if errorlevel 1 goto :backend_failed
 echo [INFO] Start the UI however you serve it (the V-Maker desktop shell, or: pnpm dev:web).
 echo [INFO] Status: start-vmaker.bat status   Stop: start-vmaker.bat stop   Logs: start-vmaker.bat logs
 exit /b 0

:backend_failed
echo [ERROR] The backend did not become healthy.
echo Run "start-vmaker.bat logs" to see the backend output.
goto :failed

:pnpm_missing
echo [ERROR] pnpm was not found.
echo Install Node.js and pnpm first.
goto :failed

:node_modules_missing
echo [ERROR] Frontend dependencies are missing.
echo Run: pnpm install
goto :failed

:python_packages_missing
echo [ERROR] Backend dependencies are missing.
echo Run: "%VMAKER_PYTHON%" -m pip install -e ".[dev]"
goto :failed

:startup_failed
echo [ERROR] Services did not become ready within 90 seconds.
echo Run "start-vmaker.bat doctor" and send that output: it lists Python/pnpm/Node, the ports,
echo and the last lines of both logs, which is what identifies the actual cause.
echo Backend log: backend\data\logs\backend.err.log   (or: start-vmaker.bat logs)
echo Check the "V-Maker Frontend" window for frontend errors.
goto :failed

:doctor
 echo.
 echo ========== V-Maker doctor ==========
 echo [1/5] interpreters and package managers
 where python 2>nul
 where pnpm 2>nul
 where node 2>nul
 echo.
 echo [2/5] backend dependency import check
 "%VMAKER_PYTHON%" -c "import fastapi,uvicorn,pydantic;print('  backend deps ok')" 2>&1
 echo.
 echo [3/5] ports
 powershell -NoProfile -ExecutionPolicy Bypass -Command ^
   "foreach($p in 8765,1420){ $l=Get-NetTCPConnection -State Listen -LocalPort $p -ErrorAction SilentlyContinue; if($l){ Write-Host ('  ' + $p + ' listening, pid=' + $l.OwningProcess) } else { Write-Host ('  ' + $p + ' not listening') } }"
 echo.
 echo [4/5] service manager status
 powershell -NoProfile -ExecutionPolicy Bypass -File "%CD%\scripts\service.ps1" status
 echo.
 echo [5/5] last lines of the backend logs
 powershell -NoProfile -ExecutionPolicy Bypass -File "%CD%\scripts\service.ps1" logs -Tail 15
 echo ========== end ==========
goto :failed

:failed
echo.
pause
exit /b 1
