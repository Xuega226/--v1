@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ========================================
echo   未名子 - 微信机器人
echo ========================================
echo.
echo 启动中...
.venv\Scripts\python.exe wechat_bot.py %*
pause
