@echo off
REM Одна проверка всех товаров. Этот файл указывается в Планировщике задач Windows.
chcp 65001 >nul
cd /d "%~dp0"

REM Если используете виртуальное окружение — раскомментируйте:
REM call .venv\Scripts\activate.bat

python monitor.py run
