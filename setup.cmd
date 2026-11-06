@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
set "radioSetupExit=%ERRORLEVEL%"
echo.
if not "%radioSetupExit%"=="0" echo Please review the setup message above. Existing music and settings were preserved.
if "%~1"=="" pause
exit /b %radioSetupExit%
