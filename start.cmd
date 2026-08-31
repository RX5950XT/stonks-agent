@echo off
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" -Mode research -DatabasePort 55434
if errorlevel 1 pause
