@echo off
REM Постоянный мониторинг: окно остаётся открытым и проверяет цены по расписанию.
chcp 65001 >nul
cd /d "%~dp0"

REM Если используете виртуальное окружение — раскомментируйте:
REM call .venv\Scripts\activate.bat

python monitor.py watch
pause
