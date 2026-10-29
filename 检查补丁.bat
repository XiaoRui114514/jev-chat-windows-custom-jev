@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0patch\apply.ps1" -Check %*
pause
