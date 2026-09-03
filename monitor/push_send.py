#!/usr/bin/env python3
"""
Отправка напоминаний «Домашнего реестра» пушами на телефон.

Берёт подписки устройств из папки приложения на Яндекс Диске (их кладёт туда
само приложение при включении уведомлений), собирает тот же текст, что и
reminders.py, и шлёт его каждому устройству. Протухшие подписки удаляет.

    YANDEX_TOKEN=… VAPID_PRIVATE_KEY=… python push_send.py
    python push_send.py --dry-run     # показать, кому и что ушло бы
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any, Dict, List

import requests

import webpush as W
from registry_sync import API, Disk, decode_chunks, SYNC_DIR
from reminders import build_text, today_local

PUSH_DIR = "app:/push"
log = logging.getLogger("push")


def read_subs(disk: Disk) -> List[Dict[str, Any]]:
    """Подписки лежат по файлу на устройство: свойства d1..dN — куски JSON."""
    out = []
    for f in disk.listdir(PUSH_DIR):
        props = f.get("custom_properties") or {}
        name = f.get("name")
        if not name or not props:
            continue
        try:
            total = int(props.get("n") or 0)
            raw = "".join(props.get("d" + str(i), "") for i in range(total))
            sub = json.loads(raw)
            if sub.get("endpoint"):
                sub["_file"] = name
                out.append(sub)
        except Exception as exc:  # noqa: BLE001
            log.warning("Подписка %s не разобралась: %s", name, exc)
    return out


def drop_sub(disk: Disk, name: str) -> None:
    try:
        disk.s.delete(API + "/resources", params={"path": PUSH_DIR + "/" + name, "permanently": "true"},
                      timeout=disk.timeout)
        log.info("Удалена недействительная подписка %s", name)
    except Exception:  # noqa: BLE001
        pass


def send_one(sub: Dict[str, Any], payload: bytes, private_key: str) -> int:
    keys = sub.get("keys") or {}
    body = W.encrypt(payload, keys.get("p256dh", ""), keys.get("auth", ""))
    headers = W.vapid_headers(sub["endpoint"], private_key)
    headers.update({
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": "86400",
        "Urgency": "normal",
    })
    r = requests.post(sub["endpoint"], data=body, headers=headers, timeout=30)
    return r.status_code


def run(dry_run: bool = False) -> int:
    token = os.environ.get("YANDEX_TOKEN", "").strip()
    key = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    if not token:
        log.warning("Нет YANDEX_TOKEN. Пропускаю запуск — добавьте секрет YANDEX_TOKEN.")
        return 0
    if not key and not dry_run:
        log.warning("Нет VAPID_PRIVATE_KEY — пуши не отправляю.")
        return 0

    disk = Disk(token)
    state = decode_chunks(disk.listdir(SYNC_DIR))
    if state is None:
        log.warning("Данные реестра не прочитались.")
        return 0
    text = build_text(state, today_local())
    if not text:
        log.info("Дел нет — не беспокоим.")
        return 0

    subs = read_subs(disk)
    if not subs:
        log.info("Нет ни одного устройства с включёнными уведомлениями.")
        return 0

    plain = text.replace("<b>", "").replace("</b>", "")
    lines = plain.split("\n")
    payload = json.dumps({
        "title": lines[0],
        "body": "\n".join(x for x in lines[1:] if x.strip())[:900],
        "url": os.environ.get("APP_URL", ""),
    }, ensure_ascii=False).encode()

    if dry_run:
        print("Устройств:", len(subs))
        print(payload.decode())
        return 0

    sent = 0
    for sub in subs:
        try:
            code = send_one(sub, payload, key)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не отправилось на %s: %s", sub["_file"], exc)
            continue
        if code in (404, 410):
            drop_sub(disk, sub["_file"])
        elif 200 <= code < 300:
            sent += 1
        else:
            log.warning("Служба пушей ответила %s для %s", code, sub["_file"])
    log.info("Отправлено устройствам: %d из %d.", sent, len(subs))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Пуш-напоминания «Домашнего реестра»")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
