#!/usr/bin/env python3
"""
Напоминания к назначенному времени.

Запускается каждые 15 минут: смотрит, у каких задач и мероприятий время
наступает в ближайшем окне (с учётом «напоминать заранее» из настроек),
и шлёт уведомление — пушем на телефон и в Telegram. Каждое напоминание
уходит один раз: отправленные отмечаются в файле на Диске.

    YANDEX_TOKEN=… VAPID_PRIVATE_KEY=… OZON_TG_TOKEN=… OZON_TG_CHAT_ID=… python notify_due.py
    python notify_due.py --dry-run --at 08:00     # что ушло бы в 8 утра
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

import webpush as W
from registry_sync import API, Disk, decode_chunks, SYNC_DIR
from reminders import event_on, notify, today_local
import push_send

LOG_DIR = "app:/pushlog"
WINDOW_MIN = 15
log = logging.getLogger("notify-due")


def now_local(at: str = None) -> datetime:
    off = float(os.environ.get("TZ_OFFSET", "3"))
    now = datetime.now(timezone.utc) + timedelta(hours=off)
    if at:
        hh, mm = at.split(":")
        now = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    return now.replace(tzinfo=None)


def parse_hhmm(value: str) -> Tuple[int, int]:
    hh, mm = str(value).split(":")[:2]
    return int(hh), int(mm)


def due_now(state: dict, now: datetime, window: int = WINDOW_MIN) -> List[Dict[str, str]]:
    """Что напомнить прямо сейчас: [{key, title, body}]."""
    lead = 0
    try:
        lead = int(str((state.get("settings") or {}).get("leadMin") or 0))
    except ValueError:
        lead = 0
    # окно смотрим вперёд с учётом «заранее»
    start = now + timedelta(minutes=lead)
    end = start + timedelta(minutes=window)
    out: List[Dict[str, str]] = []

    def hit(day: str, hhmm: str) -> bool:
        if not day or not hhmm:
            return False
        try:
            hh, mm = parse_hhmm(hhmm)
        except Exception:  # noqa: BLE001
            return False
        try:
            when = datetime.fromisoformat(day).replace(hour=hh, minute=mm)
        except Exception:  # noqa: BLE001
            return False
        return start <= when < end

    for it in state.get("items") or []:
        if it.get("status") in ("archive", "sold"):
            continue
        name = it.get("name") or "объект"
        for t in it.get("tasks") or []:
            if t.get("done"):
                continue
            if hit(str(t.get("due") or ""), str(t.get("time") or "")):
                out.append({
                    "key": "t:" + str(t.get("id")) + ":" + str(t.get("due")) + ":" + str(t.get("time")),
                    "title": "☑️ " + str(t.get("text") or "Задача"),
                    "body": (name + " · " + str(t.get("time"))).strip(),
                })

    for ev in state.get("events") or []:
        time_str = str(ev.get("time") or "")
        if not time_str:
            continue
        for shift in (0, 1):                     # событие может попадать на завтра из-за «заранее»
            day = (now + timedelta(days=shift)).date().isoformat()
            if event_on(ev, (now + timedelta(days=shift)).date()) and hit(day, time_str):
                out.append({
                    "key": "e:" + str(ev.get("id")) + ":" + day + ":" + time_str,
                    "title": "📅 " + str(ev.get("title") or "Мероприятие"),
                    "body": time_str + (" · сегодня" if shift == 0 else " · завтра"),
                })
    return out


def read_sent(disk: Disk, day: str) -> set:
    for f in disk.listdir(LOG_DIR):
        if f.get("name") != day:
            continue
        props = f.get("custom_properties") or {}
        try:
            total = int(props.get("n") or 0)
            raw = "".join(props.get("d" + str(i), "") for i in range(total))
            return set(json.loads(raw))
        except Exception:  # noqa: BLE001
            return set()
    return set()


def write_sent(disk: Disk, day: str, keys: set) -> None:
    """Храним последние ключи (чтобы свойства не разрастались) и чистим старые дни."""
    data = json.dumps(sorted(keys)[-120:], ensure_ascii=False)
    parts = [data[i:i + 880] for i in range(0, len(data), 880)] or [""]
    props = {"n": str(len(parts))}
    for i, d in enumerate(parts):
        props["d" + str(i)] = d
    disk.mkdir(LOG_DIR)
    disk.patch(LOG_DIR + "/" + day, props)
    for f in disk.listdir(LOG_DIR):
        name = f.get("name") or ""
        if name and name < day and name != day:
            try:
                disk.s.delete(API + "/resources", params={"path": LOG_DIR + "/" + name, "permanently": "true"},
                              timeout=disk.timeout)
            except Exception:  # noqa: BLE001
                pass


def deliver(disk: Disk, items: List[Dict[str, str]]) -> None:
    key = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    subs = push_send.read_subs(disk) if key else []
    for item in items:
        payload = json.dumps({"title": item["title"], "body": item["body"],
                              "url": os.environ.get("APP_URL", "")}, ensure_ascii=False).encode()
        for sub in subs:
            try:
                code = push_send.send_one(sub, payload, key)
                if code in (404, 410):
                    push_send.drop_sub(disk, sub["_file"])
            except Exception as exc:  # noqa: BLE001
                log.warning("Пуш не ушёл: %s", exc)
        notify("<b>{0}</b>\n{1}".format(item["title"], item["body"]))


def run(dry_run: bool = False, at: str = None) -> int:
    token = os.environ.get("YANDEX_TOKEN", "").strip()
    if not token:
        log.error("Нет YANDEX_TOKEN.")
        return 2
    disk = Disk(token)
    state = decode_chunks(disk.listdir(SYNC_DIR))
    if state is None:
        log.warning("Данные реестра не прочитались.")
        return 0

    now = now_local(at)
    items = due_now(state, now)
    if not items:
        log.info("На ближайшие %d минут напоминаний нет.", WINDOW_MIN)
        return 0

    day = now.date().isoformat()
    sent = set() if dry_run else read_sent(disk, day)
    fresh = [i for i in items if i["key"] not in sent]
    if not fresh:
        log.info("Всё из этого окна уже отправлено.")
        return 0

    if dry_run:
        for i in fresh:
            print("→", i["title"], "|", i["body"])
        return 0

    deliver(disk, fresh)
    sent.update(i["key"] for i in fresh)
    write_sent(disk, day, sent)
    log.info("Отправлено напоминаний: %d.", len(fresh))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Напоминания к назначенному времени")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--at", help="проверить как будто сейчас это время, например 08:00")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    return run(dry_run=args.dry_run, at=args.at)


if __name__ == "__main__":
    raise SystemExit(main())
