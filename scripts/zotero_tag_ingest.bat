@echo off
chcp 65001 >nul
setlocal EnableExtensions
REM ============================================================
REM  Zotero 📌 태그 -> 옵시디언 노트 자동 ingest
REM  Zotero 앱이 켜져 있고 Local API가 활성화되어 있어야 동작함.
REM  작업 스케줄러(매일 00:00)에서 이 파일을 실행하도록 등록되어 있음.
REM ============================================================
for %%I in ("%~dp0..") do set "ROOT=%%~fI"
set "PYTHON=%ROOT%\.venv\Scripts\python.exe"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"
set "PYTHON_BASIC_REPL=1"
cd /d "%ROOT%"

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set STAMP=%%i
if not exist logs mkdir logs
set "LOG=%ROOT%\logs\zotero_tag_ingest_%STAMP%.log"

echo [%STAMP%] Zotero 태그 ingest 시작 > "%LOG%"
if not exist "%PYTHON%" (
    echo [실패] Python 실행 파일 없음: %PYTHON% >> "%LOG%"
    endlocal & exit /b 2
)

"%PYTHON%" -u "%ROOT%\scripts\zotero_tag_ingest.py" < NUL >> "%LOG%" 2>&1
set "RESULT=%ERRORLEVEL%"
if "%RESULT%"=="0" (
    echo [완료] Zotero 태그 ingest 종료 >> "%LOG%"
) else (
    echo [실패] Zotero 태그 ingest 종료 코드 %RESULT% >> "%LOG%"
)

endlocal & exit /b %RESULT%
