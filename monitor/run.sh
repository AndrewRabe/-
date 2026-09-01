#!/usr/bin/env bash
# Одна проверка всех товаров. Удобно ставить в cron.
# Пример строки crontab (каждый час в :07):
#   7 * * * * /путь/к/ozon-price-monitor/run.sh >> /путь/к/ozon-price-monitor/cron.log 2>&1

set -euo pipefail
cd "$(dirname "$0")"

# Если используете виртуальное окружение — раскомментируйте:
# source .venv/bin/activate

exec python3 monitor.py run
