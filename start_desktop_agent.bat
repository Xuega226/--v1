@echo off
setlocal
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_desktop_agent.ps1"
set "START_RESULT=%ERRORLEVEL%"
if not "%START_RESULT%"=="0" (
  echo Failed to start Unnameko Desktop Agent. Error code: %START_RESULT%
  pause
)
exit /b %START_RESULT%
