@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1" %*
if errorlevel 1 (
  echo.
  echo AI Radio could not stop. Please review the message above.
  pause
  exit /b 1
)
exit /b 0
