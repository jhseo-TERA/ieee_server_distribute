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
set "LOG=%ROOT%\logs\citation_weekly_%STAMP%.log"

echo [%STAMP%] Weekly SQL citation refresh started >> "%LOG%"
if not exist "%PYTHON%" (
    echo [FAIL] Python executable not found: %PYTHON% >> "%LOG%"
    endlocal & exit /b 2
)

if /i "%~1"=="--self-test" goto :self_test

call "%ROOT%\scripts\start_mysql.bat" >> "%LOG%" 2>&1
if errorlevel 1 goto :failed

echo [STEP 1/2] Refresh favorite-paper citations from IEEE and Crossref >> "%LOG%"
"%PYTHON%" -u "%ROOT%\scripts\update_favorite_citations.py" --refresh-days 6 --workers 2 --delay 0.5 --batch-size 20 < NUL >> "%LOG%" 2>&1
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" (
    echo [FAIL] Citation refresh returned %RESULT%; Zotero sync was not started. >> "%LOG%"
    goto :finished
)

echo [STEP 2/2] Apply refreshed SQL citations to Zotero >> "%LOG%"
call "%ROOT%\scripts\sync_zotero_favorites.bat"
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" (
    echo [FAIL] Zotero citation sync returned %RESULT%. >> "%LOG%"
    goto :finished
)

echo [OK] Weekly citation refresh and Zotero sync completed. >> "%LOG%"
goto :finished

:self_test
"%PYTHON%" -m py_compile "%ROOT%\scripts\update_favorite_citations.py" "%ROOT%\scripts\sync_zotero_favorites.py" >> "%LOG%" 2>&1
set "RESULT=%ERRORLEVEL%"
if "%RESULT%"=="0" echo [SELF-TEST OK] Weekly citation entry points compiled. >> "%LOG%"
goto :finished

:failed
set "RESULT=%ERRORLEVEL%"

:finished
echo [EXIT CODE] %RESULT% >> "%LOG%"
endlocal & exit /b %RESULT%
