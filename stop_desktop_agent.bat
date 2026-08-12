@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
".venv\Scripts\python.exe" "desktop_agent_core.py" --shutdown
if errorlevel 1 (
  echo 桌面核心当前没有运行。
) else (
  echo 已通知未名子安全保存并停止桌面核心。
)
pause
endlocal
