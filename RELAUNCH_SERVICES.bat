@echo off
chcp 65001 >nul
setlocal EnableExtensions

REM Relaunch this workspace's services after a Windows reboot.
REM Starts MySQL, Waitress, and the existing named Cloudflare tunnel.

set "ROOT=%~dp0"
set "LOCAL_SERVER=%ROOT%scripts\start_local_server.ps1"
set "CLOUDFLARE_TUNNEL=%ROOT%scripts\start_cloudflare_tunnel.ps1"
cd /d "%ROOT%"

if not exist "%LOCAL_SERVER%" (
    echo [ERROR] Server launcher was not found:
    echo         %LOCAL_SERVER%
    pause
    exit /b 1
)

if not exist "%CLOUDFLARE_TUNNEL%" (
    echo [ERROR] Cloudflare tunnel launcher was not found:
    echo         %CLOUDFLARE_TUNNEL%
    pause
    exit /b 1
)

echo [1/2] Starting MySQL and IEEE Paper Server...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%LOCAL_SERVER%"
if errorlevel 1 (
    echo.
    echo [ERROR] MySQL or Paper Server failed to start.
    echo         Check logs\paper-server.err.log
    pause
    exit /b 1
)

echo.
echo [2/2] Starting the named Cloudflare tunnel...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%CLOUDFLARE_TUNNEL%"
if errorlevel 1 (
    echo.
    echo [ERROR] Cloudflare tunnel failed to start.
    echo         Check %USERPROFILE%\cloudflared\logs
    pause
    exit /b 1
)

echo.
echo [OK] All services are running.
echo      Local server: http://127.0.0.1:5001
echo.
pause

endlocal
