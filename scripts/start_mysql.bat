@echo off
chcp 65001 >nul
setlocal EnableExtensions
REM ============================================================
REM  MySQL (portable, no-service) 시작 스크립트
REM  - 관리자 권한 불필요. 재부팅 후 이 파일을 실행하면 DB가 켜집니다.
REM ============================================================
if not defined MYSQL_BASE set "MYSQL_BASE=%USERPROFILE%\mysql\mysql-8.4.9-winx64"
if not defined MYSQL_INI set "MYSQL_INI=%USERPROFILE%\mysql\my.ini"

if not exist "%MYSQL_BASE%\bin\mysqld.exe" (
    echo [MySQL] 실행 파일 없음: %MYSQL_BASE%\bin\mysqld.exe
    goto MYSQL_FAILED
)
if not exist "%MYSQL_INI%" (
    echo [MySQL] 설정 파일 없음: %MYSQL_INI%
    goto MYSQL_FAILED
)

powershell -NoProfile -NonInteractive -Command "$c = New-Object Net.Sockets.TcpClient; try { $c.Connect('127.0.0.1', 3306); if ($c.Connected) { exit 0 } else { exit 1 } } catch { exit 1 } finally { $c.Dispose() }" >nul 2>&1
if %ERRORLEVEL%==0 goto MYSQL_RUNNING

echo [MySQL] 시작합니다...
powershell -NoProfile -NonInteractive -Command "$mysqlExe = Join-Path $env:MYSQL_BASE 'bin\mysqld.exe'; $mysqlProcess = Start-Process -FilePath $mysqlExe -ArgumentList ('--defaults-file=' + $env:MYSQL_INI) -WindowStyle Hidden -PassThru; Start-Sleep -Milliseconds 250; if ($mysqlProcess.HasExited) { exit $mysqlProcess.ExitCode }"
if errorlevel 1 goto MYSQL_FAILED
powershell -NoProfile -NonInteractive -Command "Start-Sleep -Seconds 3"
powershell -NoProfile -NonInteractive -Command "$c = New-Object Net.Sockets.TcpClient; try { $c.Connect('127.0.0.1', 3306); if ($c.Connected) { exit 0 } else { exit 1 } } catch { exit 1 } finally { $c.Dispose() }" >nul 2>&1
if errorlevel 1 goto MYSQL_FAILED

echo [MySQL] 127.0.0.1:3306 에서 실행 중.
endlocal & exit /b 0

:MYSQL_RUNNING
echo [MySQL] 이미 실행 중입니다. ^(127.0.0.1:3306^)
endlocal & exit /b 0

:MYSQL_FAILED
echo [MySQL] 시작 실패: 127.0.0.1:3306 연결 불가.
endlocal & exit /b 1
