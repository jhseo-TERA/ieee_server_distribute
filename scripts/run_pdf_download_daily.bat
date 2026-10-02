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
set "LOG=%ROOT%\logs\pdf_download_daily_%STAMP%.log"

echo [%STAMP%] Daily PDF routine started (IEEE -^> Optica -^> Nature, total attempt budget 30) > "%LOG%"
if not exist "%PYTHON%" (
    echo [FAIL] Python executable not found: %PYTHON% >> "%LOG%"
    endlocal & exit /b 2
)

echo [BOOTSTRAP] MySQL 상태 확인을 시작합니다. >> "%LOG%"
"%ComSpec%" /d /c ""%ROOT%\scripts\start_mysql.bat"" >> "%LOG%" 2>&1
if errorlevel 1 goto :failed
echo [BOOTSTRAP] MySQL 준비 완료; 다운로드 루틴을 시작합니다. >> "%LOG%"

if /i "%~1"=="--resume" goto :resume

"%PYTHON%" -u "%ROOT%\scripts\run_pdf_download_routine.py" --limit 30 %* < NUL >> "%LOG%" 2>&1
set "RESULT=%ERRORLEVEL%"
if "%RESULT%"=="75" (
    echo [OUTCOME] SKIPPED_LOCKED: duplicate invocation stopped; the original run remains active. >> "%LOG%"
    set "RESULT=0"
    goto :finished
)
if "%RESULT%"=="76" (
    echo [OUTCOME] DEFERRED: no post-processing was started. >> "%LOG%"
    set "RESULT=0"
    goto :finished
)
if "%RESULT%"=="77" (
    echo [OUTCOME] IDLE: no verified PDF acquired; post-processing skipped. >> "%LOG%"
    set "RESULT=0"
    goto :finished
)
if not "%RESULT%"=="0" goto :finished

"%PYTHON%" -u "%ROOT%\scripts\run_pdf_postprocess.py" < NUL >> "%LOG%" 2>&1
set "RESULT=%ERRORLEVEL%"
if "%RESULT%"=="75" (
    echo [OUTCOME] SKIPPED_LOCKED: post-processing is owned by another invocation. >> "%LOG%"
    set "RESULT=0"
)
goto :finished

:resume
echo [RESUME] Checkpoint-based post-processing resume requested. >> "%LOG%"
if "%~2"=="" (
    "%PYTHON%" -u "%ROOT%\scripts\run_pdf_postprocess.py" --resume < NUL >> "%LOG%" 2>&1
) else (
    "%PYTHON%" -u "%ROOT%\scripts\run_pdf_postprocess.py" --resume --date "%~2" < NUL >> "%LOG%" 2>&1
)
set "RESULT=%ERRORLEVEL%"
if "%RESULT%"=="75" (
    echo [OUTCOME] SKIPPED_LOCKED: post-processing is already active. >> "%LOG%"
    set "RESULT=0"
)

:finished
echo [EXIT CODE] %RESULT% >> "%LOG%"
endlocal & exit /b %RESULT%

:failed
set "RESULT=%ERRORLEVEL%"
echo [EXIT CODE] %RESULT% >> "%LOG%"
endlocal & exit /b %RESULT%
