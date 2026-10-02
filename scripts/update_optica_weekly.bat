@echo off
chcp 65001 >nul
setlocal EnableExtensions
REM Windows Task Scheduler entry point; metadata only, no PDF downloads.
for %%I in ("%~dp0..") do set "ROOT=%%~fI"
set "PYTHON=%ROOT%\.venv\Scripts\python.exe"
set "PYTHONUNBUFFERED=1"
set "PYTHONUTF8=1"
set "PYTHON_BASIC_REPL=1"
set "METADATA_LAUNCHER=weekly-optica-batch"
cd /d "%ROOT%"
if not exist logs mkdir logs
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set STAMP=%%i
set "LOG=%ROOT%\logs\optica_update_%STAMP%.log"
if not exist "%PYTHON%" exit /b 2
if /I "%~1"=="--self-test" goto :self_test
REM Bootstrap MySQL in a child cmd so its exit cannot terminate this batch.
"%ComSpec%" /d /c ""%ROOT%\scripts\start_mysql.bat"" >> "%LOG%" 2>&1
if errorlevel 1 exit /b 1
"%PYTHON%" -u "%ROOT%\scripts\run_metadata_update.py" --pipeline optica %* < NUL >> "%LOG%" 2>&1
exit /b %ERRORLEVEL%

:self_test
call :check_file "%ROOT%\scripts\import_excel_to_db.py"
if errorlevel 1 exit /b 2
"%PYTHON%" -u "%ROOT%\scripts\run_metadata_update.py" --pipeline optica --self-test < NUL >> "%LOG%" 2>&1
exit /b %ERRORLEVEL%

:check_file
if not exist "%~1" exit /b 2
exit /b 0
