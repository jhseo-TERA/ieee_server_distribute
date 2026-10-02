@echo off
chcp 65001 >nul
cd /d "%~dp0.."

echo Configure a new password for the administrator account configured in .env.
echo The password will not be displayed. Only its hash is stored in .env.
echo.
".venv\Scripts\python.exe" "scripts\setup_admin_auth.py"
echo.
pause
