@echo off
chcp 65001 >nul
REM ============================================================
REM  OJSSC 단발성 전체 메타데이터 수집 배치.
REM  Crossref 공개 API를 사용하므로 도서관 프록시/VPN/로그인이 필요 없음.
REM  PDF 다운로드는 포함하지 않음.
REM ============================================================
setlocal EnableExtensions
for %%I in ("%~dp0..") do set "ROOT=%%~fI"
cd /d "%ROOT%"

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set STAMP=%%i
if not exist logs mkdir logs
set LOG=logs\ojssc_retry_%STAMP%.log

echo [%STAMP%] OJSSC Crossref 전체 수집 시작 > "%LOG%"

call scripts\start_mysql.bat >> "%LOG%" 2>&1

echo. >> "%LOG%"
echo [1/2] OJSSC 전체 구간(2021~현재) 수집 (Crossref API) >> "%LOG%"
".venv\Scripts\python.exe" scripts\fetch_ojssc_crossref.py --full >> "%LOG%" 2>&1

echo. >> "%LOG%"
echo [2/2] Excel -^> MySQL 적재 >> "%LOG%"
".venv\Scripts\python.exe" scripts\import_excel_to_db.py >> "%LOG%" 2>&1

echo. >> "%LOG%"
echo [완료] OJSSC Crossref 전체 수집 종료 >> "%LOG%"

endlocal
