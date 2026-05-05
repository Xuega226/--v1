@echo off
cd /d "%~dp0"
call .venv\Scripts\activate
python ui.py
pause
