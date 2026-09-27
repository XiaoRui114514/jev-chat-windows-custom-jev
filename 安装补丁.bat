@echo off
setlocal
cd /d "%~dp0"
set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS%" set "PS=powershell"
chcp 65001 >nul
"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0patch\apply.ps1" %*
echo.
pause
