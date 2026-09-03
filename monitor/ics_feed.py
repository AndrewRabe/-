#!/usr/bin/env python3
"""
Лента календаря для подписки (Google Календарь, Яндекс, Apple).

Собирает из данных реестра файл .ics — задачи со сроком, мероприятия с
повторами и дни рождения — кладёт его на Яндекс Диск и публикует, выдавая
постоянную ссылку. Календарь, подписанный на эту ссылку, сам подтягивает
изменения (Google обновляет внешние ленты раз в несколько часов).

    YANDEX_TOKEN=… python ics_feed.py            # обновить ленту и показать ссылку
    YANDEX_TOKEN=… python ics_feed.py --dry-run  # только показать содержимое
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List

import requests

from registry_sync import API, Disk, decode_chunks, SYNC_DIR

FEED_PATH = "app:/calendar.ics"
DOW = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
log = logging.getLogger("ics-feed")


def esc(v: Any) -> str:
    s = str(v if v is not None else "")
    return s.replace("\\", "\\\\").replace(";", r"\;").replace(",", r"\,").replace("\n", r"\n")


def fold(line: str) -> str:
    """Длинные строки складываются по 73 октета, как требует формат."""
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode()) > 73:
            out.append(cur)
            cur = " " + ch
        else:
            cur += ch
    out.append(cur)
    return "\r\n".join(out)


def rrule(rep: str, days: List[int] = None, interval: int = 1, until: str = "") -> str:
    if not rep:
        return ""
    freq = {"daily": "DAILY", "weekly": "WEEKLY", "monthly": "MONTHLY", "yearly": "YEARLY"}.get(rep)
    if not freq:
        return ""
    rule = "RRULE:FREQ=" + freq
    if rep == "weekly" and days:
        rule += ";BYDAY=" + ",".join(DOW[d] for d in sorted(set(days)) if 0 <= d < 7)
    if interval and interval > 1:
        rule += ";INTERVAL=%d" % interval
    if until:
        rule += ";UNTIL=" + until.replace("-", "") + "T235900Z"
    return rule


def build(state: dict, lead_min: int = 0) -> str:
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    L = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Домашний реестр//RU", "CALSCALE:GREGORIAN",
         "METHOD:PUBLISH", "X-WR-CALNAME:Домашний реестр",
         "X-WR-TIMEZONE:" + os.environ.get("TZ_NAME", "Europe/Moscow"),
         "REFRESH-INTERVAL;VALUE=DURATION:PT2H", "X-PUBLISHED-TTL:PT2H"]

    def event(uid: str, summary: str, day: str, time: str, rule: str, desc: str, alarm: bool) -> None:
        L.extend(["BEGIN:VEVENT", "UID:%s@home-registry" % uid, "DTSTAMP:" + now])
        if time:
            hh, mm = time.split(":")[:2]
            start = day.replace("-", "") + "T" + hh + mm + "00"
            end_dt = datetime.fromisoformat(day + "T" + hh + ":" + mm) + timedelta(hours=1)
            L.append("DTSTART:" + start)
            L.append("DTEND:" + end_dt.strftime("%Y%m%dT%H%M%S"))
        else:
            L.append("DTSTART;VALUE=DATE:" + day.replace("-", ""))
        L.append(fold("SUMMARY:" + esc(summary)))
        if desc:
            L.append(fold("DESCRIPTION:" + esc(desc)))
        if rule:
            L.append(rule)
        if alarm and time:
            L.extend(["BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Напоминание",
                      "TRIGGER:-PT%dM" % lead_min, "END:VALARM"])
        L.append("END:VEVENT")

    for it in state.get("items") or []:
        if it.get("status") in ("archive", "sold"):
            continue
        for t in it.get("tasks") or []:
            if t.get("done") or not t.get("due"):
                continue
            event("t-" + str(t.get("id")), "☑ " + str(t.get("text") or ""), str(t["due"]),
                  str(t.get("time") or ""), rrule(str(t.get("repeat") or "")),
                  str(it.get("name") or ""), True)

    for e in state.get("events") or []:
        if not e.get("date"):
            continue
        event("e-" + str(e.get("id")), str(e.get("title") or ""), str(e["date"]), str(e.get("time") or ""),
              rrule(str(e.get("repeat") or ""), e.get("days"), int(e.get("interval") or 1),
                    str(e.get("until") or "")), "", True)

    for b in state.get("birthdays") or []:
        if not b.get("date"):
            continue
        event("b-" + str(b.get("id")), "🎂 " + str(b.get("name") or ""), str(b["date"]), "",
              "RRULE:FREQ=YEARLY", "", False)

    L.append("END:VCALENDAR")
    return "\r\n".join(L) + "\r\n"


def upload(disk: Disk, text: str) -> str:
    href = disk._req("GET", "/resources/upload", params={"path": FEED_PATH, "overwrite": "true"})
    requests.put(href["href"], data=text.encode("utf-8"), timeout=60).raise_for_status()
    # публикуем и забираем постоянную ссылку
    disk.s.put(API + "/resources/publish", params={"path": FEED_PATH}, timeout=disk.timeout)
    meta = disk._req("GET", "/resources", params={"path": FEED_PATH, "fields": "public_url,public_key"})
    return (meta or {}).get("public_url", "")


def run(dry_run: bool = False) -> int:
    token = os.environ.get("YANDEX_TOKEN", "").strip()
    if not token:
        log.warning("Нет YANDEX_TOKEN — пропускаю.")
        return 0
    disk = Disk(token)
    state = decode_chunks(disk.listdir(SYNC_DIR))
    if state is None:
        log.warning("Данные реестра не прочитались.")
        return 0
    lead = 0
    try:
        lead = int(str((state.get("settings") or {}).get("leadMin") or 0))
    except ValueError:
        lead = 0
    text = build(state, lead)
    count = text.count("BEGIN:VEVENT")
    if dry_run:
        print(text)
        log.info("Событий в ленте: %d", count)
        return 0
    url = upload(disk, text)
    log.info("Лента обновлена: %d событий.", count)
    if url:
        # прямая ссылка на скачивание — её понимает «Добавить по URL» в календарях
        log.info("Ссылка для подписки: %s", url)
        print(url)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Лента календаря для подписки")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
