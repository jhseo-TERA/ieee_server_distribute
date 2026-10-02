@echo off
chcp 65001 >nul
setlocal EnableExtensions
for %%I in ("%~dp0..") do set "ROOT=%%~fI"
cd /d "%ROOT%"

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set STAMP=%%i
if not exist logs mkdir logs
set "LOG=%ROOT%\logs\mysql_backup_%STAMP%.log"

echo [%STAMP%] MySQL backup started > "%LOG%"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\backup_mysql.ps1" %* >> "%LOG%" 2>&1
set "RESULT=%ERRORLEVEL%"
echo [EXIT CODE] %RESULT% >> "%LOG%"
endlocal & exit /b %RESULT%
