@echo off
chcp 65001 >nul
REM ============================================================
REM  IEEE Paper Repository 실행 (MySQL 확인 + Flask 서버 시작)
REM  더블클릭하면 http://127.0.0.1:5001 에서 웹사이트가 열립니다.
REM ============================================================
setlocal EnableExtensions
for %%I in ("%~dp0..") do set "ROOT=%%~fI"
cd /d "%ROOT%"

call "%~dp0start_mysql.bat"
".venv\Scripts\python.exe" scripts\serdes_data_pipeline.py --migrate-only
if errorlevel 1 (
    echo [ERROR] SerDes schema migration failed.
    pause
    exit /b 1
)

echo.
echo [Paper Server] http://127.0.0.1:5001  (종료: Ctrl+C)
echo.
".venv\Scripts\waitress-serve.exe" --host=127.0.0.1 --port=5001 web.app:app
pause
endlocal
