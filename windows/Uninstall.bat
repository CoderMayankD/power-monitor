@echo off
:: Removes Power Monitor (logs are kept). Windows will ask for administrator permission.
net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
copy /y "%~dp0Uninstall.ps1" "%TEMP%\PowerMonitor-Uninstall.ps1" >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%TEMP%\PowerMonitor-Uninstall.ps1"
pause
