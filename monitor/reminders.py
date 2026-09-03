#!/usr/bin/env python3
"""
Напоминания из «Домашнего реестра» в Telegram.

Читает данные реестра из папки приложения на Яндекс Диске и присылает
сообщение: что просрочено, что сегодня, что завтра — задачи, мероприятия,
напоминания по инструкциям, дни рождения и регламент машины.

Запускается по расписанию (GitHub Actions) или руками:

    YANDEX_TOKEN=... OZON_TG_TOKEN=... OZON_TG_CHAT_ID=... python reminders.py
    python reminders.py --dry-run     # показать текст, ничего не отправляя

Переменные окружения:
    YANDEX_TOKEN     — токен доступа к папке приложения (обязательно)
    OZON_TG_TOKEN    — токен Telegram-бота
    OZON_TG_CHAT_ID  — chat_id получателя
    TZ_OFFSET        — часовой пояс в часах от UTC (по умолчанию 3, Москва)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from registry_sync import Disk, decode_chunks, money, notify, SYNC_DIR

log = logging.getLogger("reminders")

WEEKDAY = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def today_local() -> date:
    off = float(os.environ.get("TZ_OFFSET", "3"))
    return (datetime.now(timezone.utc) + timedelta(hours=off)).date()


def dow(d: date) -> int:
    return d.weekday()


def add_months(iso: str, months: int) -> str:
    y, m, dd = int(iso[:4]), int(iso[5:7]), int(iso[8:10])
    m += months
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    for day in range(dd, 0, -1):
        try:
            return date(y, m, day).isoformat()
        except ValueError:
            continue
    return iso


def event_on(ev: Dict[str, Any], day: date) -> bool:
    """Повторяющиеся мероприятия — та же логика, что в приложении."""
    start = str(ev.get("date") or "")
    if not start:
        return False
    ds = day.isoformat()
    if ds < start:
        return False
    until = str(ev.get("until") or "")
    if until and ds > until:
        return False
    rep = str(ev.get("repeat") or "")
    if not rep:
        return ds == start
    if rep == "daily":
        return True
    if rep == "weekly":
        days = ev.get("days") or [dow(date.fromisoformat(start))]
        if dow(day) not in days:
            return False
        weeks = (day - date.fromisoformat(start)).days // 7
        interval = int(ev.get("interval") or 1)
        return interval < 2 or weeks % interval == 0
    if rep == "monthly":
        return ds[8:] == start[8:]
    if rep == "yearly":
        return ds[5:] == start[5:]
    return False


def collect(state: dict, day: date) -> Dict[str, List[str]]:
    ds = day.isoformat()
    out: Dict[str, List[str]] = {"overdue": [], "today": [], "events": [], "bd": [], "car": []}
    for it in state.get("items") or []:
        if it.get("status") in ("archive", "sold"):
            continue
        name = it.get("name") or "объект"
        for t in it.get("tasks") or []:
            if t.get("done") or not t.get("due"):
                continue
            due = str(t["due"])
            mark = " 🔁" if t.get("repeat") else ""
            if due < ds:
                out["overdue"].append(f"{t.get('text','')} — {name} (с {due[8:10]}.{due[5:7]}){mark}")
            elif due == ds:
                out["today"].append(f"{t.get('text','')} — {name}{mark}")
        for ins in it.get("instructions") or []:
            due = str(ins.get("due") or "")
            if not due:
                continue
            if due < ds:
                out["overdue"].append(f"{ins.get('title','инструкция')} — {name} (с {due[8:10]}.{due[5:7]})")
            elif due == ds:
                out["today"].append(f"{ins.get('title','инструкция')} — {name}")
        car = it.get("car") or {}
        odo = money(car.get("odo"))
        for sv in car.get("plan") or []:
            left_km = None
            if sv.get("km") and money(sv.get("lastKm")) and odo:
                left_km = int(sv["km"]) - (odo - money(sv["lastKm"]))
            left_days = None
            if sv.get("months") and sv.get("lastAt"):
                due_at = add_months(str(sv["lastAt"]), int(sv["months"]))
                left_days = (date.fromisoformat(due_at) - day).days
            if (left_km is not None and left_km <= 0) or (left_days is not None and left_days <= 0):
                out["car"].append(f"{sv.get('name','обслуживание')} — {name}: пора")
            elif (left_km is not None and 0 < left_km <= 500) or (left_days is not None and 0 < left_days <= 14):
                bits = []
                if left_km is not None and left_km > 0:
                    bits.append(f"{left_km} км")
                if left_days is not None and left_days >= 0:
                    bits.append(f"{left_days} дн.")
                out["car"].append(f"{sv.get('name','обслуживание')} — {name}: через {' / '.join(bits)}")
    for ev in state.get("events") or []:
        if event_on(ev, day):
            t = str(ev.get("time") or "")
            out["events"].append((t + " " if t else "") + str(ev.get("title") or ""))
    for bd in state.get("birthdays") or []:
        if str(bd.get("date") or "")[5:] == ds[5:]:
            out["bd"].append(str(bd.get("name") or ""))
    out["events"].sort()
    return out


def build_text(state: dict, day: date) -> Optional[str]:
    cur = collect(state, day)
    nxt = collect(state, day + timedelta(days=1))
    total = sum(len(v) for v in cur.values())
    if not total and not nxt["events"] and not nxt["today"] and not nxt["bd"]:
        return None

    head = f"<b>Домашний реестр — {day.day:02d}.{day.month:02d}, {WEEKDAY[dow(day)]}</b>"
    parts = [head]

    def block(title: str, rows: List[str], limit: int = 12) -> None:
        if not rows:
            return
        shown = rows[:limit]
        extra = len(rows) - len(shown)
        body = "\n".join("• " + r for r in shown)
        if extra:
            body += f"\n• …и ещё {extra}"
        parts.append(f"\n{title}\n{body}")

    block("⚠️ <b>Просрочено</b>", cur["overdue"])
    block("☑️ <b>Сегодня</b>", cur["today"])
    block("📅 <b>Мероприятия</b>", cur["events"])
    block("🎂 <b>Дни рождения</b>", cur["bd"])
    block("🚗 <b>Машина</b>", cur["car"])

    tomorrow = nxt["today"] + nxt["events"] + [f"🎂 {b}" for b in nxt["bd"]]
    block("🔜 <b>Завтра</b>", tomorrow, limit=6)
    return "\n".join(parts)


def run(dry_run: bool = False) -> int:
    token = os.environ.get("YANDEX_TOKEN", "").strip()
    if not token:
        log.warning("Нет YANDEX_TOKEN — нечем читать данные реестра. Пропускаю запуск — добавьте секрет YANDEX_TOKEN.")
        return 0
    state = decode_chunks(Disk(token).listdir(SYNC_DIR))
    if state is None:
        log.warning("Данные реестра не прочитались (пусто или идёт запись).")
        return 0
    text = build_text(state, today_local())
    if not text:
        log.info("На сегодня и завтра дел нет — молчим.")
        return 0
    if dry_run:
        print(text)
        return 0
    notify(text)
    log.info("Напоминание отправлено.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Напоминания из «Домашнего реестра» в Telegram")
    ap.add_argument("--dry-run", action="store_true", help="показать текст, ничего не отправляя")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
