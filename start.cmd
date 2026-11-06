@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
if errorlevel 1 (
  echo.
  echo AI Radio could not start. Please review the message above.
  pause
  exit /b 1
)
exit /b 0
