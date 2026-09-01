#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ozon Price Monitor — следит за ценами на товары Ozon и присылает
уведомление в Telegram, когда цена опускается до нового исторического минимума.

Быстрый старт:
    pip install -r requirements.txt
    python monitor.py init                       # создать config.json
    python monitor.py chatid                     # узнать свой chat_id
    python monitor.py add "https://www.ozon.ru/product/..."
    python monitor.py check "https://www.ozon.ru/product/..."   # разовая проверка
    python monitor.py watch                      # постоянный мониторинг

Все команды: python monitor.py --help
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import random
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ozon_fetch import (
    OzonSession,
    PriceSnapshot,
    clean_url,
    fetch_price,
    is_ozon_product_url,
    parse_money,
    product_key_from_url,
)

try:
    import requests
except ImportError:  # pragma: no cover
    raise SystemExit("Установите зависимости:  pip install -r requirements.txt")


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
DB_PATH = BASE_DIR / "prices.db"
LOG_PATH = BASE_DIR / "monitor.log"

log = logging.getLogger("ozon")


DEFAULT_CONFIG: Dict[str, Any] = {
    "telegram": {
        "bot_token": "",
        "chat_id": "",
    },
    # Какую цену отслеживать: "card" (с Ozon Картой), "regular" (обычная), "original"
    "track_price": "card",
    # Интервал проверки в режиме watch, в минутах
    "check_interval_minutes": 60,
    # Пауза между товарами, чтобы не выглядеть роботом (секунды, от и до)
    "delay_between_products": [5, 15],
    # Порядок стратегий получения цены
    "strategies": ["api", "html", "browser"],
    # Сколько раз повторять каждую стратегию при сбое
    "retries": 2,
    # Прислать сообщение при самой первой проверке товара
    "notify_on_first_check": True,
    # Сообщить, если товар не удаётся проверить N раз подряд
    "notify_after_failures": 3,
    # Прокси вида http://user:pass@host:port (обычно не нужен)
    "proxy": "",
    # Показывать окно браузера в запасной стратегии (для отладки)
    "browser_headless": True,
    "products": [],
}


# --------------------------------------------------------------------------
# Конфигурация
# --------------------------------------------------------------------------

def load_config(path: Path = CONFIG_PATH) -> Dict[str, Any]:
    if not path.exists():
        raise SystemExit(
            f"Не найден файл настроек {path.name}.\n"
            f"Создайте его командой:  python monitor.py init"
        )
    with path.open(encoding="utf-8") as f:
        try:
            user_cfg = json.load(f)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Ошибка в {path.name}: {exc}")

    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # глубокая копия
    for key, value in user_cfg.items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key].update(value)
        else:
            cfg[key] = value

    # Переменные окружения имеют приоритет — удобно, чтобы не хранить токен в файле
    cfg["telegram"]["bot_token"] = str(
        os.environ.get("OZON_TG_TOKEN") or cfg["telegram"].get("bot_token") or ""
    ).strip()
    cfg["telegram"]["chat_id"] = str(
        os.environ.get("OZON_TG_CHAT_ID") or cfg["telegram"].get("chat_id") or ""
    ).strip()
    return cfg


def read_config_raw() -> Dict[str, Any]:
    """Читает config.json как есть — для команд, которые его изменяют."""
    if not CONFIG_PATH.exists():
        return json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"Ошибка в {CONFIG_PATH.name}: {exc}\n"
            "Поправьте файл (частая причина — лишняя запятая) и повторите."
        )
    if not isinstance(data, dict):
        raise SystemExit(f"{CONFIG_PATH.name} должен содержать объект JSON в фигурных скобках.")
    data.setdefault("products", [])
    return data


def entry_url(entry: Any) -> Optional[str]:
    """Ссылка из записи о товаре: поддерживаем и объект, и просто строку."""
    if isinstance(entry, str):
        return entry.strip() or None
    if isinstance(entry, dict):
        url = entry.get("url")
        return url.strip() if isinstance(url, str) and url.strip() else None
    return None


def save_config(cfg: Dict[str, Any], path: Path = CONFIG_PATH) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")


# --------------------------------------------------------------------------
# Хранилище истории цен
# --------------------------------------------------------------------------

# Колонки с ценами и порядок, в котором tracked() выбирает цену для каждого режима.
_KIND_COLUMNS = {"card": "price_card", "regular": "price_regular", "original": "price_original"}
_KIND_ORDER = {
    "card": ("card", "regular", "original"),
    "regular": ("regular", "card", "original"),
    "original": ("original", "regular", "card"),
}


class Storage:
    def __init__(self, path: Path = DB_PATH, track_price: str = "card"):
        self.track_price = track_price if track_price in _KIND_ORDER else "card"
        # timeout — на случай, когда cron-запуск пересечётся с работающим watch
        self.conn = sqlite3.connect(str(path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        self._migrate()

    def _migrate(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS checks (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                product_key    TEXT NOT NULL,
                url            TEXT NOT NULL,
                title          TEXT,
                price          REAL,
                price_regular  REAL,
                price_card     REAL,
                price_original REAL,
                available      INTEGER NOT NULL DEFAULT 0,
                ok             INTEGER NOT NULL DEFAULT 0,
                error          TEXT,
                source         TEXT,
                checked_at     TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_checks_key
                ON checks (product_key, id);

            CREATE TABLE IF NOT EXISTS notifications (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                product_key TEXT NOT NULL,
                kind        TEXT NOT NULL,
                price       REAL,
                sent_at     TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_notif_key
                ON notifications (product_key, kind, id);
            """
        )
        # Вид отслеживаемой цены (card / regular / original) — добавлен позже,
        # поэтому дописываем колонку в уже существующие базы.
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(checks)")}
        if "price_kind" not in columns:
            self.conn.execute("ALTER TABLE checks ADD COLUMN price_kind TEXT")
        self._backfill_kinds()
        self.conn.commit()

    def _backfill_kinds(self) -> None:
        """
        Старые записи вид цены не хранили — восстанавливаем его, сравнивая
        записанную цену с card/regular/original той же строки. Порядок ветвей
        повторяет порядок выбора в tracked(): если цены совпадают, вид должен
        получиться тот же, что и выбрала бы программа сейчас.
        Запрос идемпотентный — после первого прохода ничего не находит.
        """
        order = _KIND_ORDER[self.track_price]
        whens = " ".join(
            f"WHEN {_KIND_COLUMNS[kind]} IS NOT NULL "
            f"AND abs(price - {_KIND_COLUMNS[kind]}) < 0.005 THEN '{kind}'"
            for kind in order
        )
        self.conn.execute(
            f"UPDATE checks SET price_kind = CASE {whens} END "
            "WHERE price_kind IS NULL AND price IS NOT NULL"
        )

    # -- запись -----------------------------------------------------------

    def record(
        self,
        snap: PriceSnapshot,
        tracked_price: Optional[float],
        price_kind: Optional[str] = None,
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO checks
               (product_key, url, title, price, price_kind, price_regular, price_card,
                price_original, available, ok, error, source, checked_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                snap.product_key,
                snap.url,
                snap.title,
                tracked_price,
                price_kind,
                snap.price_regular,
                snap.price_card,
                snap.price_original,
                1 if snap.available else 0,
                1 if snap.ok else 0,
                snap.error,
                snap.source,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def log_notification(self, product_key: str, kind: str, price: Optional[float]) -> None:
        self.conn.execute(
            "INSERT INTO notifications (product_key, kind, price, sent_at) VALUES (?,?,?,?)",
            (product_key, kind, price, datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    # -- чтение -----------------------------------------------------------

    @staticmethod
    def _kind_filter(kind: Optional[str], params: List[Any]) -> str:
        """Сравнивать между собой можно только цены одного вида."""
        if not kind:
            return ""
        params.append(kind)
        return " AND price_kind = ?"

    def minimum(self, product_key: str, kind: Optional[str] = None) -> Optional[sqlite3.Row]:
        """Строка с минимальной зафиксированной ценой (только удачные проверки)."""
        params: List[Any] = [product_key]
        sql = (
            "SELECT * FROM checks "
            "WHERE product_key = ? AND ok = 1 AND available = 1 AND price IS NOT NULL"
            + self._kind_filter(kind, params)
            + " ORDER BY price ASC, id ASC LIMIT 1"
        )
        return self.conn.execute(sql, params).fetchone()

    def last_success(self, product_key: str, kind: Optional[str] = None) -> Optional[sqlite3.Row]:
        params: List[Any] = [product_key]
        sql = (
            "SELECT * FROM checks "
            "WHERE product_key = ? AND ok = 1 AND price IS NOT NULL"
            + self._kind_filter(kind, params)
            + " ORDER BY id DESC LIMIT 1"
        )
        return self.conn.execute(sql, params).fetchone()

    def observations(self, product_key: str, kind: Optional[str] = None) -> int:
        params: List[Any] = [product_key]
        sql = (
            "SELECT COUNT(*) AS n FROM checks "
            "WHERE product_key = ? AND ok = 1 AND price IS NOT NULL"
            + self._kind_filter(kind, params)
        )
        return int(self.conn.execute(sql, params).fetchone()["n"])

    def consecutive_failures(self, product_key: str) -> int:
        rows = self.conn.execute(
            "SELECT ok FROM checks WHERE product_key = ? ORDER BY id DESC LIMIT 50",
            (product_key,),
        ).fetchall()
        n = 0
        for row in rows:
            if row["ok"]:
                break
            n += 1
        return n

    def last_notification(self, product_key: str, kind: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM notifications
               WHERE product_key = ? AND kind = ? ORDER BY id DESC LIMIT 1""",
            (product_key, kind),
        ).fetchone()

    def history(self, product_key: Optional[str] = None) -> List[sqlite3.Row]:
        if product_key:
            return self.conn.execute(
                "SELECT * FROM checks WHERE product_key = ? ORDER BY id",
                (product_key,),
            ).fetchall()
        return self.conn.execute("SELECT * FROM checks ORDER BY id").fetchall()


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

class Telegram:
    def __init__(self, token: str, chat_id: str, timeout: int = 20):
        self.token = (token or "").strip()
        self.chat_id = str(chat_id or "").strip()
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def _api(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def send(self, text: str, disable_preview: bool = True) -> bool:
        if not self.configured:
            log.warning("Telegram не настроен — уведомление не отправлено.")
            return False
        try:
            resp = requests.post(
                self._api("sendMessage"),
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": disable_preview,
                },
                timeout=self.timeout,
            )
            data = resp.json()
            if not data.get("ok"):
                log.error("Telegram отказал: %s", data.get("description"))
                return False
            return True
        except requests.RequestException as exc:
            log.error("Не удалось отправить сообщение в Telegram: %s", exc)
            return False

    def get_updates(self) -> List[Dict[str, Any]]:
        resp = requests.get(self._api("getUpdates"), timeout=self.timeout)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "неизвестная ошибка"))
        return data.get("result", [])

    def me(self) -> Dict[str, Any]:
        resp = requests.get(self._api("getMe"), timeout=self.timeout)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "неизвестная ошибка"))
        return data["result"]


# --------------------------------------------------------------------------
# Форматирование
# --------------------------------------------------------------------------

def money(value: Optional[float]) -> str:
    if value is None:
        return "—"
    if abs(value - round(value)) < 0.005:
        s = f"{int(round(value)):,}".replace(",", " ")
    else:
        s = f"{value:,.2f}".replace(",", " ").replace(".", ",")
    return f"{s} ₽"


def escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def short_date(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%d.%m.%Y")
    except (ValueError, TypeError):
        return iso or "?"


def product_title(snap: PriceSnapshot, cfg_entry: Dict[str, Any]) -> str:
    return (
        cfg_entry.get("name")
        or snap.title
        or f"Товар {snap.product_key}"
    )


# --------------------------------------------------------------------------
# Основная логика проверки
# --------------------------------------------------------------------------

def check_product(
    entry: Dict[str, Any],
    cfg: Dict[str, Any],
    store: Storage,
    tg: Telegram,
    session: OzonSession,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Проверяет один товар, при необходимости шлёт уведомление."""
    url = clean_url(entry["url"])
    key = product_key_from_url(url)
    mode = cfg.get("track_price", "card")

    snap = fetch_price(
        url,
        session=session,
        strategies=tuple(cfg.get("strategies", ["api", "html", "browser"])),
        retries=int(cfg.get("retries", 2)),
        proxy=cfg.get("proxy") or None,
        headless=bool(cfg.get("browser_headless", True)),
    )
    price, price_kind = snap.tracked(mode) if snap.ok else (None, None)
    title = product_title(snap, entry)

    result: Dict[str, Any] = {
        "key": key,
        "title": title,
        "url": url,
        "ok": snap.ok,
        "price": price,
        "source": snap.source,
        "error": snap.error,
        "new_minimum": False,
        "notified": False,
    }

    # --- Неудачная проверка ---------------------------------------------
    if not snap.ok or price is None:
        log.warning("[%s] не удалось получить цену: %s", title, snap.error)
        if dry_run:
            return result
        store.record(snap, None)
        fails = store.consecutive_failures(key)
        threshold = int(cfg.get("notify_after_failures", 3) or 0)
        if threshold and fails == threshold:
            last = store.last_notification(key, "error")
            recent = (
                last
                and datetime.fromisoformat(last["sent_at"]) > datetime.now() - timedelta(hours=12)
            )
            if not recent:
                tg.send(
                    "⚠️ <b>Не могу проверить цену</b>\n\n"
                    f"<b>{escape_html(title)}</b>\n"
                    f"Неудачных попыток подряд: {fails}\n"
                    f"Причина: <code>{escape_html((snap.error or '')[:300])}</code>\n\n"
                    f'<a href="{url}">Открыть на Ozon</a>'
                )
                store.log_notification(key, "error", None)
        return result

    # --- Успешная проверка ----------------------------------------------
    prev_min_row = store.minimum(key, price_kind)
    prev_min = prev_min_row["price"] if prev_min_row else None
    last_row = store.last_success(key, price_kind)
    prev_price = last_row["price"] if last_row else None

    # «Первая проверка» считается по всей истории товара, а не по виду цены,
    # иначе смена вида цены выглядела бы как новый товар.
    is_first = store.observations(key) == 0
    is_new_min = (
        snap.available
        and not is_first
        and prev_min is not None
        and price < prev_min - 0.001
    )
    result["new_minimum"] = is_new_min

    log.info(
        "[%s] %s%s%s (источник: %s)",
        title,
        money(price),
        "" if snap.available else " — НЕТ В НАЛИЧИИ",
        f", прежний минимум {money(prev_min)}" if prev_min else "",
        snap.source,
    )

    if dry_run:
        return result

    store.record(snap, price, price_kind)

    # --- Уведомления -----------------------------------------------------
    if is_first and cfg.get("notify_on_first_check", True):
        lines = [
            "\U0001f440 <b>Начал следить за товаром</b>",
            "",
            f"<b>{escape_html(title)}</b>",
            f"Текущая цена: <b>{money(price)}</b>",
        ]
        details = price_details(snap, mode)
        if details:
            lines.append(details)
        lines += ["", f'<a href="{url}">Открыть на Ozon</a>']
        if tg.send("\n".join(lines)):
            store.log_notification(key, "first", price)
            result["notified"] = True

    elif is_new_min:
        drop = prev_min - price
        pct = drop / prev_min * 100 if prev_min else 0
        lines = [
            "\U0001f525 <b>Новый минимум!</b>",
            "",
            f"<b>{escape_html(title)}</b>",
            f"Цена: <b>{money(price)}</b>",
            f"Прежний минимум: {money(prev_min)} "
            f"({short_date(prev_min_row['checked_at'])})",
            f"Экономия: <b>−{money(drop)}</b> (−{pct:.1f}%)",
        ]
        details = price_details(snap, mode)
        if details:
            lines.append(details)
        lines += ["", f'<a href="{url}">Открыть на Ozon</a>']
        if tg.send("\n".join(lines)):
            store.log_notification(key, "min", price)
            result["notified"] = True

    # Дополнительно: желаемая цена, если задана в конфиге
    target = parse_money(entry.get("target_price"))
    if target and snap.available and price <= target and not is_new_min:
        last = store.last_notification(key, "target")
        already = last and last["price"] is not None and price >= last["price"]
        if not already:
            lines = [
                "\U0001f3af <b>Цена достигла вашей цели</b>",
                "",
                f"<b>{escape_html(title)}</b>",
                f"Цена: <b>{money(price)}</b> (цель: {money(target)})",
            ]
            details = price_details(snap, mode)
            if details:
                lines.append(details)
            lines += ["", f'<a href="{url}">Открыть на Ozon</a>']
            if tg.send("\n".join(lines)):
                store.log_notification(key, "target", price)
                result["notified"] = True

    if prev_price is not None and abs(price - prev_price) > 0.001:
        log.info("   изменение с прошлой проверки: %s", money(price - prev_price))

    return result


def price_details(snap: PriceSnapshot, mode: str) -> str:
    """Строка с прочими ценами товара (карта / обычная / без скидки)."""
    parts = []
    if mode == "card" and snap.price_card and snap.price_regular:
        parts.append(f"без Ozon Карты {money(snap.price_regular)}")
    elif mode != "card" and snap.price_card:
        parts.append(f"с Ozon Картой {money(snap.price_card)}")
    if snap.price_original:
        parts.append(f"до скидки {money(snap.price_original)}")
    if not snap.available:
        parts.append("⚠️ нет в наличии")
    return ("<i>" + " · ".join(parts) + "</i>") if parts else ""


def parse_delay(value: Any) -> Tuple[float, float]:
    """Пауза между товарами: принимает [5, 15], одно число или мусор."""
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value), float(value)
        if isinstance(value, (list, tuple)) and value:
            nums = [float(v) for v in value[:2]]
            return (nums[0], nums[1]) if len(nums) > 1 else (nums[0], nums[0])
    except (TypeError, ValueError):
        pass
    return 5.0, 15.0


def run_once(cfg: Dict[str, Any], store: Storage, tg: Telegram) -> List[Dict[str, Any]]:
    products = cfg.get("products", [])
    if not products:
        log.warning("В config.json нет ни одного товара. Добавьте: python monitor.py add <ссылка>")
        return []

    session = OzonSession(proxy=cfg.get("proxy") or None)
    delay_from, delay_to = parse_delay(cfg.get("delay_between_products"))
    results = []

    log.info("--- Проверка %d товар(ов) ---", len(products))
    for i, entry in enumerate(products):
        if isinstance(entry, str):           # разрешаем и просто строку-ссылку
            entry = {"url": entry}
        if not isinstance(entry, dict) or not entry.get("url"):
            log.warning("Пропускаю запись в config.json без ссылки: %r", entry)
            continue
        if entry.get("enabled") is False:
            continue
        try:
            results.append(check_product(entry, cfg, store, tg, session))
        except Exception as exc:  # noqa: BLE001
            log.exception("Непредвиденная ошибка при проверке %s: %s", entry.get("url"), exc)
        if i < len(products) - 1:
            time.sleep(random.uniform(float(delay_from), float(delay_to)))

    mins = sum(1 for r in results if r.get("new_minimum"))
    fails = sum(1 for r in results if not r.get("ok"))
    log.info(
        "--- Готово: проверено %d, новых минимумов %d, ошибок %d ---",
        len(results), mins, fails,
    )
    return results


def watch(cfg: Dict[str, Any], store: Storage, tg: Telegram) -> None:
    interval = max(5, int(cfg.get("check_interval_minutes", 60)))
    log.info("Мониторинг запущен. Проверка каждые %d мин. Остановить — Ctrl+C.", interval)
    try:
        while True:
            run_once(cfg, store, tg)
            # Небольшой разброс, чтобы запросы не шли ровно по расписанию
            jitter = random.uniform(-0.08, 0.08) * interval * 60
            sleep_for = interval * 60 + jitter
            wake = datetime.now() + timedelta(seconds=sleep_for)
            log.info("Следующая проверка в %s", wake.strftime("%H:%M:%S"))
            time.sleep(sleep_for)
    except KeyboardInterrupt:
        log.info("Мониторинг остановлен.")


# --------------------------------------------------------------------------
# Команды CLI
# --------------------------------------------------------------------------

def cmd_init(args) -> int:
    if CONFIG_PATH.exists() and not args.force:
        print(f"{CONFIG_PATH.name} уже существует. Перезаписать: python monitor.py init --force")
        return 1
    save_config(DEFAULT_CONFIG)
    print(f"Создан {CONFIG_PATH.name}.")
    print("Дальше:")
    print("  1) создайте бота у @BotFather и вставьте токен в config.json")
    print("  2) напишите своему боту любое сообщение")
    print("  3) python monitor.py chatid   — узнать chat_id")
    print('  4) python monitor.py add "ссылка на товар"')
    return 0


def cmd_add(args) -> int:
    cfg_raw = read_config_raw()

    existing = {
        product_key_from_url(u)
        for u in (entry_url(p) for p in cfg_raw["products"])
        if u
    }

    added = 0
    for url in args.url:
        if not is_ozon_product_url(url):
            print(f"Пропускаю (не похоже на ссылку товара Ozon): {url}")
            continue
        url_clean = clean_url(url)
        key = product_key_from_url(url_clean)
        if key in existing:
            print(f"Уже отслеживается: {url_clean}")
            continue
        existing.add(key)
        entry: Dict[str, Any] = {"url": url_clean}
        if args.name:
            entry["name"] = args.name
        if args.target is not None:
            entry["target_price"] = args.target
        cfg_raw["products"].append(entry)
        added += 1
        print(f"Добавлен: {url_clean}")

    if added:
        save_config(cfg_raw)
        print(f"Всего товаров в списке: {len(cfg_raw['products'])}")
    return 0


def cmd_remove(args) -> int:
    if not CONFIG_PATH.exists():
        print("Файл config.json не найден — удалять нечего.")
        return 1
    cfg_raw = read_config_raw()
    before = len(cfg_raw["products"])
    target_keys = {product_key_from_url(u) if "/" in u else u for u in args.url}
    cfg_raw["products"] = [
        p for p in cfg_raw["products"]
        if not (entry_url(p) and product_key_from_url(entry_url(p)) in target_keys)
    ]
    save_config(cfg_raw)
    print(f"Удалено товаров: {before - len(cfg_raw['products'])}")
    return 0


def cmd_list(args) -> int:
    cfg = load_config()
    store = Storage(track_price=cfg.get("track_price", "card"))
    products = cfg.get("products", [])
    if not products:
        print("Список пуст. Добавьте товар: python monitor.py add <ссылка>")
        return 0

    print(f"Отслеживается товаров: {len(products)}\n")
    for i, entry in enumerate(products, 1):
        url = entry_url(entry)
        if not url:
            print(f"{i}. запись без ссылки — проверьте config.json\n")
            continue
        opts = entry if isinstance(entry, dict) else {}
        key = product_key_from_url(url)
        last = store.last_success(key)
        # Минимум показываем по тому же виду цены, по которому идут уведомления
        low = store.minimum(key, last["price_kind"] if last else None)
        name = opts.get("name") or (last["title"] if last and last["title"] else key)
        print(f"{i}. {name}")
        print(f"   {url}")
        if last:
            print(f"   сейчас:  {money(last['price'])}   ({short_date(last['checked_at'])})")
        else:
            print("   сейчас:  ещё не проверялся")
        if low:
            print(f"   минимум: {money(low['price'])}   ({short_date(low['checked_at'])})")
        target = parse_money(opts.get("target_price"))
        if target:
            print(f"   цель:    {money(target)}")
        if opts.get("enabled") is False:
            print("   (проверка отключена)")
        print(f"   проверок: {store.observations(key)}")
        print()
    return 0


def cmd_check(args) -> int:
    """Разовая диагностическая проверка — ничего не пишет в базу."""
    cfg = load_config() if CONFIG_PATH.exists() else json.loads(json.dumps(DEFAULT_CONFIG))
    urls = args.url or [u for u in (entry_url(p) for p in cfg.get("products", [])) if u]
    if not urls:
        print("Укажите ссылку: python monitor.py check <ссылка>")
        return 1

    session = OzonSession(proxy=cfg.get("proxy") or None)
    strategies = tuple(args.strategy) if args.strategy else tuple(cfg.get("strategies", ["api", "html", "browser"]))

    for url in urls:
        print(f"\n=== {clean_url(url)}")
        snap = fetch_price(
            url,
            session=session,
            strategies=strategies,
            retries=int(cfg.get("retries", 2)),
            proxy=cfg.get("proxy") or None,
            headless=bool(cfg.get("browser_headless", True)),
        )
        if not snap.ok:
            print(f"  НЕ УДАЛОСЬ: {snap.error}")
            print("  Подсказка: попробуйте запасной вариант через браузер —")
            print(f'      python monitor.py check "{url}" --strategy browser')
            continue
        print(f"  Товар:            {snap.title or '—'}")
        print(f"  ID:               {snap.product_key}")
        print(f"  С Ozon Картой:    {money(snap.price_card)}")
        print(f"  Обычная цена:     {money(snap.price_regular)}")
        print(f"  Цена до скидки:   {money(snap.price_original)}")
        print(f"  В наличии:        {'да' if snap.available else 'нет'}")
        print(f"  Источник данных:  {snap.source}")
        print(f"  Отслеживаем:      {money(snap.tracked_price(cfg.get('track_price', 'card')))}")
    return 0


def cmd_run(args) -> int:
    cfg = load_config()
    store = Storage(track_price=cfg.get("track_price", "card"))
    tg = Telegram(cfg["telegram"]["bot_token"], cfg["telegram"]["chat_id"])
    if not tg.configured:
        log.warning("Telegram не настроен — цены будут только записываться в базу.")
    run_once(cfg, store, tg)
    return 0


def cmd_watch(args) -> int:
    cfg = load_config()
    store = Storage(track_price=cfg.get("track_price", "card"))
    tg = Telegram(cfg["telegram"]["bot_token"], cfg["telegram"]["chat_id"])
    if not tg.configured:
        log.warning("Telegram не настроен — цены будут только записываться в базу.")
    watch(cfg, store, tg)
    return 0


def cmd_test_telegram(args) -> int:
    cfg = load_config()
    tg = Telegram(cfg["telegram"]["bot_token"], cfg["telegram"]["chat_id"])
    if not tg.token:
        print("В config.json не указан bot_token.")
        return 1
    try:
        me = tg.me()
        print(f"Бот на связи: @{me.get('username')}")
    except Exception as exc:  # noqa: BLE001
        print(f"Токен не подошёл: {exc}")
        return 1
    if not tg.chat_id:
        print("Не указан chat_id. Узнать: python monitor.py chatid")
        return 1
    ok = tg.send(
        "✅ <b>Ozon Price Monitor на связи</b>\n"
        "Уведомления о новых минимумах будут приходить сюда."
    )
    print("Тестовое сообщение отправлено." if ok else "Отправить не удалось — см. лог выше.")
    return 0 if ok else 1


def cmd_chatid(args) -> int:
    cfg = load_config() if CONFIG_PATH.exists() else json.loads(json.dumps(DEFAULT_CONFIG))
    token = args.token or cfg["telegram"]["bot_token"]
    if not token:
        print("Укажите токен: python monitor.py chatid --token <токен>")
        return 1
    tg = Telegram(token, "")
    try:
        updates = tg.get_updates()
    except Exception as exc:  # noqa: BLE001
        print(f"Ошибка: {exc}")
        return 1
    if not updates:
        print("Сообщений нет. Откройте своего бота в Telegram, нажмите «Старт»,")
        print("напишите ему любое слово и повторите эту команду.")
        return 1

    seen = {}
    for upd in updates:
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            seen[chat["id"]] = chat
    print("Найденные чаты:\n")
    for cid, chat in seen.items():
        who = chat.get("username") or chat.get("title") or chat.get("first_name") or ""
        print(f"  chat_id: {cid}   ({chat.get('type')}, {who})")
    print("\nВставьте нужный chat_id в config.json -> telegram -> chat_id")
    return 0


def cmd_history(args) -> int:
    mode = load_config().get("track_price", "card") if CONFIG_PATH.exists() else "card"
    store = Storage(track_price=mode)
    key = product_key_from_url(args.product) if args.product and "/" in args.product else args.product
    rows = store.history(key)
    if not rows:
        print("История пуста.")
        return 0
    if args.csv:
        out = Path(args.csv)
        with out.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow([
                "дата", "товар", "id", "цена", "с картой", "обычная",
                "до скидки", "в наличии", "источник", "ошибка",
            ])
            for r in rows:
                writer.writerow([
                    r["checked_at"], r["title"] or "", r["product_key"],
                    r["price"] or "", r["price_card"] or "", r["price_regular"] or "",
                    r["price_original"] or "", "да" if r["available"] else "нет",
                    r["source"] or "", r["error"] or "",
                ])
        print(f"Готово: {out} ({len(rows)} строк)")
        return 0

    shown = rows if args.limit <= 0 else rows[-args.limit:]
    for r in shown:
        status = money(r["price"]) if r["ok"] else f"ошибка: {(r['error'] or '')[:60]}"
        try:
            when = datetime.fromisoformat(r["checked_at"]).strftime("%d.%m.%Y %H:%M")
        except (ValueError, TypeError):
            when = r["checked_at"]
        print(f"{when}  {(r['title'] or r['product_key'])[:45]:<45}  {status}")
    print(f"\nВсего записей: {len(rows)}")
    return 0


# --------------------------------------------------------------------------

def setup_logging(verbose: bool = False) -> None:
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()

    # На русской Windows перенаправленный вывод по умолчанию в cp1251,
    # и знак ₽ роняет print. Переключаем поток на UTF-8.
    for stream_obj in (sys.stdout, sys.stderr):
        try:
            stream_obj.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    stream.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(stream)

    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)
    root.addHandler(file_handler)

    logging.getLogger("urllib3").setLevel(logging.WARNING)


def money_arg(text: str) -> float:
    """Принимает цену в любом виде: 24990, «24 990», «24990,50»."""
    value = parse_money(text)
    if value is None:
        raise argparse.ArgumentTypeError(f"не похоже на цену: {text!r}")
    return value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="monitor.py",
        description="Мониторинг цен на Ozon с уведомлением в Telegram о новом минимуме.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-v", "--verbose", action="store_true", help="подробный лог")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="создать config.json")
    s.add_argument("--force", action="store_true", help="перезаписать существующий")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("add", help="добавить товар в список")
    s.add_argument("url", nargs="+", help="ссылка(и) на товар Ozon")
    s.add_argument("--name", help="своё название товара")
    s.add_argument("--target", type=money_arg,
                   help="желаемая цена, доп. уведомление (например: 24990 или 24 990,50)")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("remove", help="убрать товар из списка")
    s.add_argument("url", nargs="+", help="ссылка или id товара")
    s.set_defaults(func=cmd_remove)

    s = sub.add_parser("list", help="показать список товаров и текущие цены")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("check", help="разовая проверка цены (в базу не пишет)")
    s.add_argument("url", nargs="*", help="ссылка на товар (по умолчанию — все из конфига)")
    s.add_argument("--strategy", nargs="+", choices=["api", "html", "browser"],
                   help="какие способы получения цены использовать")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("run", help="одна проверка всех товаров (для cron)")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("watch", help="постоянный мониторинг по расписанию")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("test-telegram", help="проверить настройки Telegram")
    s.set_defaults(func=cmd_test_telegram)

    s = sub.add_parser("chatid", help="узнать свой chat_id")
    s.add_argument("--token", help="токен бота, если его ещё нет в конфиге")
    s.set_defaults(func=cmd_chatid)

    s = sub.add_parser("history", help="показать или выгрузить историю цен")
    s.add_argument("--product", help="ссылка или id конкретного товара")
    s.add_argument("--csv", help="выгрузить в CSV-файл")
    s.add_argument("--limit", type=int, default=30, help="сколько последних строк показать")
    s.set_defaults(func=cmd_history)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
