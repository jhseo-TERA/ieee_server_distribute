@echo off
chcp 65001 >nul
setlocal EnableExtensions
for %%I in ("%~dp0..") do set "ROOT=%%~fI"
set "PYTHON=%ROOT%\.venv\Scripts\python.exe"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"
set "PYTHON_BASIC_REPL=1"
cd /d "%ROOT%"

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set STAMP=%%i
if not exist logs mkdir logs
set "LOG=%ROOT%\logs\zotero_favorites_sync_%STAMP%.log"

echo [%STAMP%] MySQL file-backed favorites to Zotero started > "%LOG%"
if not exist "%PYTHON%" (
    echo [FAIL] Python executable not found: %PYTHON% >> "%LOG%"
    endlocal & exit /b 2
)

"%ComSpec%" /d /c ""%ROOT%\scripts\start_mysql.bat"" >> "%LOG%" 2>&1
if errorlevel 1 goto :failed

"%PYTHON%" -u "%ROOT%\scripts\sync_zotero_favorites.py" --apply %* < NUL >> "%LOG%" 2>&1
set "RESULT=%ERRORLEVEL%"
echo [EXIT CODE] %RESULT% >> "%LOG%"
endlocal & exit /b %RESULT%

:failed
set "RESULT=%ERRORLEVEL%"
echo [EXIT CODE] %RESULT% >> "%LOG%"
endlocal & exit /b %RESULT%
