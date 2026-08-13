@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\restart.ps1" -OpenBrowser %*
if errorlevel 1 (
  echo.
  echo AutoPrompt Studio restart failed. Review the error above and .runtime\logs.
  pause
)
endlocal
