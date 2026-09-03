#!/usr/bin/env python3
"""
Мост между «Домашним реестром» и мониторингом цен Ozon.

Читает список желаний из папки приложения на Яндекс Диске (её пишет реестр),
проверяет цены на Ozon и складывает результат обратно на Диск, чтобы реестр
показывал текущую цену и минимум без ручного ввода. При новом минимуме или
достижении цели шлёт сообщение в Telegram.

Запускается по расписанию (GitHub Actions) или руками:

    YANDEX_TOKEN=... python registry_sync.py            # проверить и записать
    YANDEX_TOKEN=... python registry_sync.py --dry-run  # только показать

Переменные окружения:
    YANDEX_TOKEN     — токен с доступом к папке приложения (обязательно)
    OZON_TG_TOKEN    — токен Telegram-бота (необязательно)
    OZON_TG_CHAT_ID  — chat_id для уведомлений (необязательно)
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import logging
import os
import random
import re
import sys
import time
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import requests

from ozon_fetch import fetch_price, is_ozon_product_url, OzonSession

API = "https://cloud-api.yandex.net/v1/disk"
SYNC_DIR = "app:/sync"
PRICES_DIR = "app:/prices"
PROP_LIMIT = 880  # столько же, сколько пишет реестр

log = logging.getLogger("registry-sync")


# ---------------------------------------------------------------- Яндекс Диск

class Disk:
    """Минимальный клиент к папке приложения на Яндекс Диске."""

    def __init__(self, token: str, timeout: int = 30):
        self.token = token
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"OAuth {token}"})

    def _req(self, method: str, path: str, **kw) -> Optional[Any]:
        r = self.s.request(method, API + path, timeout=self.timeout, **kw)
        if r.status_code == 404:
            return None
        if not r.ok:
            raise RuntimeError(f"Яндекс Диск ответил {r.status_code}: {r.text[:200]}")
        if r.status_code == 204 or not r.content:
            return {}
        return r.json()

    def listdir(self, path: str) -> List[Dict[str, Any]]:
        data = self._req(
            "GET",
            "/resources",
            params={
                "path": path,
                "limit": 1000,
                "fields": "_embedded.items.name,_embedded.items.path,_embedded.items.custom_properties",
            },
        )
        if not data:
            return []
        items = (data.get("_embedded") or {}).get("items") or []
        # часть ответов приходит без свойств — дочитываем поштучно
        if items and all(not i.get("custom_properties") for i in items):
            full = []
            for it in items:
                one = self._req(
                    "GET", "/resources",
                    params={"path": it.get("path") or f"{path}/{it['name']}",
                            "fields": "name,custom_properties"},
                )
                if one:
                    full.append(one)
            return full
        return items

    def mkdir(self, path: str) -> None:
        r = self.s.put(API + "/resources", params={"path": path}, timeout=self.timeout)
        if not (r.ok or r.status_code == 409):
            raise RuntimeError(f"Не удалось создать папку {path}: {r.status_code}")

    def touch(self, path: str) -> None:
        """Создаёт пустой файл-носитель свойств."""
        href = self._req("GET", "/resources/upload",
                         params={"path": path, "overwrite": "true"})
        requests.put(href["href"], data=b" ", timeout=self.timeout).raise_for_status()

    def patch(self, path: str, props: Dict[str, Any]) -> None:
        r = self.s.patch(API + "/resources", params={"path": path},
                         json={"custom_properties": props}, timeout=self.timeout)
        if r.status_code == 404:
            self.touch(path)
            r = self.s.patch(API + "/resources", params={"path": path},
                             json={"custom_properties": props}, timeout=self.timeout)
        if not r.ok:
            raise RuntimeError(f"Не удалось записать свойства {path}: {r.status_code}")


# ------------------------------------------------------- разбор данных реестра

def decode_chunks(items: List[Dict[str, Any]]) -> Optional[dict]:
    """Собирает состояние реестра из кусочков app:/sync (см. index.html)."""
    parts: Dict[int, str] = {}
    total = 0
    enc = "raw"
    rev = None
    for f in items:
        p = f.get("custom_properties") or {}
        if p.get("d") is None or p.get("i") is None:
            continue
        if rev is None:
            rev = str(p.get("v"))
            total = int(p.get("n") or 0)
            enc = p.get("e") or "raw"
        elif str(p.get("v")) != rev:
            return None  # запись с устройства ещё идёт — прочитаем в следующий раз
        parts[int(p["i"])] = p["d"]
    if rev is None or not total:
        return None
    b64 = ""
    for i in range(total):
        if i not in parts:
            return None
        b64 += parts[i]
    raw = base64.b64decode(b64 + "=" * (-len(b64) % 4))
    if enc == "gz":
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


def wishes_with_ozon(state: dict) -> List[Tuple[str, str, str, Optional[float]]]:
    """(id желания, название, ссылка на Ozon, целевая цена) для активных желаний."""
    out = []
    for w in state.get("wishes") or []:
        if w.get("status") == "bought":
            continue
        for link in w.get("links") or []:
            url = str(link.get("url") or "")
            if link.get("market") == "ozon" and is_ozon_product_url(url):
                out.append((str(w.get("id")), str(w.get("title") or "товар"),
                            url, money(w.get("target"))))
                break
    return out


def money(value: Any) -> Optional[float]:
    digits = re.sub(r"[^\d]", "", str(value or ""))
    return float(digits) if digits else None


def read_prices(disk: Disk) -> Dict[str, Dict[str, Any]]:
    out = {}
    for f in disk.listdir(PRICES_DIR):
        props = f.get("custom_properties") or {}
        name = f.get("name")
        if name and props:
            out[name] = props
    return out


def fmt(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f}".replace(",", " ") + " ₽"


# ------------------------------------------------------------------- Telegram

def notify(text: str) -> None:
    token = os.environ.get("OZON_TG_TOKEN", "").strip()
    chat = os.environ.get("OZON_TG_CHAT_ID", "").strip()
    if not token or not chat:
        log.info("Telegram не настроен — пропускаю уведомление: %s", text.replace("\n", " "))
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=20,
        )
        if not r.json().get("ok"):
            log.warning("Telegram отказал: %s", r.text[:200])
    except Exception as exc:  # noqa: BLE001
        log.warning("Не вышло отправить в Telegram: %s", exc)


def build_message(title: str, url: str, price: float, prev_min: Optional[float],
                  target: Optional[float]) -> Optional[str]:
    """Сообщение о новом минимуме или достигнутой цели; None — если молчим."""
    if prev_min is not None and price < prev_min:
        saving = prev_min - price
        return (f"🔥 <b>Новый минимум!</b>\n{title}\n"
                f"{fmt(price)} — дешевле прежнего минимума на {fmt(saving)}\n{url}")
    if prev_min is None:
        return f"👀 <b>Начал следить</b>\n{title}\nСейчас {fmt(price)}\n{url}"
    if target is not None and price <= target:
        return (f"🎯 <b>Цена достигла цели</b>\n{title}\n"
                f"{fmt(price)} при цели {fmt(target)}\n{url}")
    return None


# ----------------------------------------------------------------------- main

def run(dry_run: bool = False, track: str = "card") -> int:
    token = os.environ.get("YANDEX_TOKEN", "").strip()
    if not token:
        log.warning("Нет YANDEX_TOKEN — нечем читать данные реестра. Пропускаю запуск — добавьте секрет YANDEX_TOKEN.")
        return 0

    disk = Disk(token)
    state = decode_chunks(disk.listdir(SYNC_DIR))
    if state is None:
        log.warning("Не удалось прочитать данные реестра (пусто или запись в процессе).")
        return 0

    targets = wishes_with_ozon(state)
    if not targets:
        log.info("В желаниях нет товаров Ozon — проверять нечего.")
        return 0

    known = read_prices(disk)
    session = OzonSession()
    checked = failed = 0

    for wid, title, url, target in targets:
        prev = known.get(wid) or {}
        prev_min = money(prev.get("m"))
        snap = fetch_price(url, session=session)
        price, kind = snap.tracked(track)

        if not snap.ok or price is None:
            failed += 1
            log.warning("%s — не вышло получить цену: %s", title, snap.error)
            if not dry_run:
                props = dict(prev)
                props.update({"s": "err", "e": (snap.error or "")[:120], "t": date.today().isoformat()})
                disk.mkdir(PRICES_DIR)
                disk.patch(f"{PRICES_DIR}/{wid}", props)
            continue

        checked += 1
        new_min = price if prev_min is None else min(prev_min, price)
        log.info("%s — %s (мин. %s, вид: %s)", title, fmt(price), fmt(new_min), kind)

        if not dry_run:
            disk.mkdir(PRICES_DIR)
            disk.patch(f"{PRICES_DIR}/{wid}", {
                "p": f"{price:.0f}",
                "m": f"{new_min:.0f}",
                "k": kind or "",
                "t": date.today().isoformat(),
                "s": "ok" if snap.available else "gone",
                "e": "",
            })

        if snap.available:
            msg = build_message(title, url, price, prev_min, target)
            if msg and not dry_run:
                notify(msg)

        time.sleep(random.uniform(3, 7))  # вежливая пауза между товарами

    log.info("Готово: проверено %d, не удалось %d.", checked, failed)
    if failed and not checked:
        log.warning("Ozon не отдал ни одной цены — обычно это блокировка запросов из дата-центра. "
                    "Запустите монитор с домашнего компьютера.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Синхронизация цен Ozon с «Домашним реестром»")
    ap.add_argument("--dry-run", action="store_true", help="только показать, ничего не писать")
    ap.add_argument("--track", default="card", choices=["card", "regular", "original"],
                    help="какую цену отслеживать (по умолчанию с Ozon Картой)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    return run(dry_run=args.dry_run, track=args.track)


if __name__ == "__main__":
    raise SystemExit(main())
