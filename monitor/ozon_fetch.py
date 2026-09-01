#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ozon_fetch.py — получение цены товара с Ozon.

Три независимые стратегии (пробуются по очереди, пока какая-то не сработает):
  1. api    — внутренний JSON-API Ozon (composer-api.bx). Самый быстрый.
  2. html   — обычная HTML-страница товара + извлечение data-state виджетов.
  3. browser— реальный браузер через Playwright (если установлен). Самый надёжный,
              но самый медленный. Используется как запасной вариант.

Модуль не зависит ни от чего, кроме requests (playwright — опционально).
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

try:
    import requests
except ImportError:  # pragma: no cover
    raise SystemExit(
        "Не установлен пакет requests.\n"
        "Установите зависимости:  pip install -r requirements.txt"
    )

log = logging.getLogger("ozon.fetch")

# --------------------------------------------------------------------------
# Заголовки, максимально похожие на обычный браузер.
# --------------------------------------------------------------------------

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
]

BASE_HEADERS = {
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
}


# --------------------------------------------------------------------------
# Результат одной проверки
# --------------------------------------------------------------------------

@dataclass
class PriceSnapshot:
    """Снимок цен товара на момент проверки."""

    url: str
    product_key: str = ""
    title: Optional[str] = None
    price_card: Optional[float] = None       # цена с Ozon Картой
    price_regular: Optional[float] = None    # обычная цена (то, что платим без карты)
    price_original: Optional[float] = None   # зачёркнутая цена «до скидки»
    available: bool = False
    ok: bool = False
    error: Optional[str] = None
    source: Optional[str] = None             # api / html / browser
    raw_widgets: Dict[str, Any] = field(default_factory=dict, repr=False)

    def tracked(self, mode: str = "card") -> Tuple[Optional[float], Optional[str]]:
        """
        Возвращает (цена, вид цены) для отслеживания.

        Вид цены важен: у части товаров цены с Ozon Картой нет вовсе, и тогда
        берётся обычная. Сравнивать между собой можно только цены одного вида,
        иначе «подешевело на 1 500 ₽» окажется просто сменой типа цены.
        """
        chains = {
            "card": (("card", self.price_card), ("regular", self.price_regular)),
            "regular": (("regular", self.price_regular), ("card", self.price_card)),
            "original": (
                ("original", self.price_original),
                ("regular", self.price_regular),
                ("card", self.price_card),
            ),
        }
        chain = chains.get(mode, chains["regular"])
        for kind, value in chain:
            if value:
                return value, kind
        return None, None

    def tracked_price(self, mode: str = "card") -> Optional[float]:
        """Цена, по которой ведётся отслеживание."""
        return self.tracked(mode)[0]

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("raw_widgets", None)
        return d


# --------------------------------------------------------------------------
# Разбор цен
# --------------------------------------------------------------------------

_MONEY_CLEAN_RE = re.compile(r"[^\d,\.]")

# Разряды в ценах Ozon разделяются неразрывными/тонкими пробелами.
_SPACES = "     "


def parse_money(value: Any) -> Optional[float]:
    """
    Превращает '77 990 ₽', '1 234,50 ₽', 77990, '77990.00' в float.
    Возвращает None, если распознать не удалось.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v if v > 0 else None
    if not isinstance(value, str):
        return None

    s = value.strip()
    if not s:
        return None
    for ch in _SPACES:
        s = s.replace(ch, "")
    s = _MONEY_CLEAN_RE.sub("", s)
    if not s:
        return None

    # '1234,50' -> '1234.50'; '1,234.50' -> '1234.50'
    if "," in s and "." in s:
        s = s.replace(",", "")
    else:
        s = s.replace(",", ".")

    # Хвост вида '.00' оставляем, лишние точки убираем
    if s.count(".") > 1:
        head, _, tail = s.rpartition(".")
        s = head.replace(".", "") + "." + tail

    try:
        v = float(s)
    except ValueError:
        return None
    return v if v > 0 else None


def _json_maybe(value: Any) -> Any:
    """widgetStates хранит значения как JSON-строки — разворачиваем их."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return json.loads(stripped)
            except (ValueError, TypeError):
                return value
    return value


def _deep_find(obj: Any, key: str, _depth: int = 0) -> List[Any]:
    """Собирает все значения по ключу key на любой глубине вложенности."""
    found: List[Any] = []
    if _depth > 12:
        return found
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                found.append(v)
            if isinstance(v, (dict, list)):
                found.extend(_deep_find(v, key, _depth + 1))
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                found.extend(_deep_find(item, key, _depth + 1))
    return found


def _first_price(obj: Any, key: str) -> Optional[float]:
    for candidate in _deep_find(obj, key):
        price = parse_money(candidate)
        if price:
            return price
    return None


# Виджеты, которые описывают сам товар, а не подборки вокруг него.
# Имена виджетов Ozon выглядят как «webPrice-3121879-default-1», поэтому
# сравнивается именно первый сегмент: «webSaleCarousel» — уже не наш виджет.
_OWN_WIDGETS = frozenset({
    "webprice", "websale", "webaddtocart", "webproductheading",
    "webproductmainwidget", "weboutofstock", "webstickyproducts",
})


def _widget_name(lname: str) -> str:
    return lname.split("-", 1)[0]


def _is_own_widget(lname: str) -> bool:
    return _widget_name(lname) in _OWN_WIDGETS


def _own_tracking_product(
    parsed: Dict[str, Any], product_key: str
) -> Optional[Dict[str, Any]]:
    """
    Находит cellTrackingInfo.product, относящийся именно к нужному товару.

    Приоритет:
      1) блок, у которого id/sku совпадает с id товара из ссылки;
      2) блок из виджета самого товара, если id в нём не указан.
    Карточки из каруселей (у них свой чужой id) отбрасываются.
    """
    best: Optional[Tuple[int, Dict[str, Any]]] = None

    for name, state in parsed.items():
        if not isinstance(state, (dict, list)):
            continue
        lname = name.lower()
        is_own_widget = _is_own_widget(lname)

        for info in _deep_find(state, "cellTrackingInfo"):
            product = info.get("product") if isinstance(info, dict) else None
            if not isinstance(product, dict):
                continue
            # У одного товара бывает и id, и sku — сверяемся с обоими
            ids = {
                str(product.get(field)).strip()
                for field in ("id", "sku", "productId")
                if product.get(field) not in (None, "", 0)
            }

            if product_key and product_key in ids:
                rank = 0                      # точное совпадение — то, что нужно
            elif product_key and ids:
                continue                      # карточка чужого товара
            elif is_own_widget:
                rank = 1                      # id нет, но виджет наш
            else:
                continue                      # непонятно чей блок — не рискуем

            if best is None or rank < best[0]:
                best = (rank, product)
            if rank == 0:
                return product

    return best[1] if best else None


def extract_from_widget_states(states: Dict[str, Any], url: str) -> PriceSnapshot:
    """
    Достаёт цены из словаря widgetStates (формат одинаков и для JSON-API,
    и для data-state атрибутов в HTML).
    """
    snap = PriceSnapshot(url=url, product_key=product_key_from_url(url))
    parsed: Dict[str, Any] = {}
    for name, value in states.items():
        parsed[name] = _json_maybe(value)
    snap.raw_widgets = parsed

    out_of_stock_flag = False
    saw_availability_flag = False

    for name, state in parsed.items():
        if not isinstance(state, (dict, list)):
            continue
        lname = name.lower()

        # --- Заголовок товара -------------------------------------------
        if snap.title is None and "productheading" in lname:
            for t in _deep_find(state, "title"):
                if isinstance(t, str) and t.strip():
                    snap.title = t.strip()
                    break

        # --- Основной блок цены: webPrice --------------------------------
        # Схема: {"price": "77 990 ₽", "originalPrice": "99 990 ₽",
        #         "cardPrice": "74 990 ₽", "isAvailable": true}
        if "webprice" in lname:
            card = _first_price(state, "cardPrice") or _first_price(state, "ozonCardPrice")
            current = _first_price(state, "price")
            original = _first_price(state, "originalPrice") or _first_price(state, "basePrice")

            snap.price_card = snap.price_card or card
            snap.price_regular = snap.price_regular or current
            snap.price_original = snap.price_original or original

            for flag in _deep_find(state, "isAvailable"):
                if isinstance(flag, bool):
                    saw_availability_flag = True
                    snap.available = snap.available or flag

        # Трекинговая информация (cellTrackingInfo) разбирается ниже,
        # общим проходом по всем виджетам.

        if "outofstock" in lname:
            out_of_stock_flag = True

    # Запасной разбор через cellTrackingInfo.
    #
    # ВАЖНО: на странице товара есть карусели «с этим товаром покупают»,
    # «похожие товары» и т.п. — у каждой карточки в них свой cellTrackingInfo
    # со своей ценой. Если брать их без разбора, цена соседнего товара
    # попадёт в историю и вызовет ложное уведомление о минимуме.
    # Поэтому берём только блок, который относится к нашему товару.
    if snap.price_regular is None:
        product = _own_tracking_product(parsed, snap.product_key)
        if product is not None:
            base = parse_money(product.get("price"))
            final = parse_money(product.get("finalPrice"))
            if final:
                snap.price_regular = final
            if base and snap.price_original is None:
                snap.price_original = base
            title = product.get("title")
            if snap.title is None and isinstance(title, str) and title.strip():
                snap.title = title.strip()

    # Заголовок как запасной вариант — только из виджетов самого товара
    # (в каруселях «похожие товары» лежат чужие названия).
    if snap.title is None:
        for name, state in parsed.items():
            lname = name.lower()
            if not _is_own_widget(lname) and not _widget_name(lname).startswith("seo"):
                continue
            if not isinstance(state, (dict, list)):
                continue
            for t in _deep_find(state, "title"):
                if isinstance(t, str) and 5 < len(t.strip()) < 300:
                    snap.title = t.strip()
                    break
            if snap.title:
                break

    # --- Проверки здравого смысла ---------------------------------------
    # Зачёркнутая цена не может быть меньше текущей
    if snap.price_original and snap.price_regular and snap.price_original < snap.price_regular:
        snap.price_original, snap.price_regular = snap.price_regular, snap.price_original
    # Цена по карте не может быть выше обычной
    if snap.price_card and snap.price_regular and snap.price_card > snap.price_regular:
        snap.price_card = None

    if snap.price_regular or snap.price_card:
        snap.ok = True
        # Явный флаг наличия от Ozon важнее догадок: считаем товар доступным
        # только если сайт прямо об этом не сказал обратного.
        if not saw_availability_flag and not out_of_stock_flag:
            snap.available = True
    # Виджет «нет в наличии» бывает и на страницах, где товар доступен
    # (например, для отдельного варианта). Прямой признак isAvailable важнее.
    if out_of_stock_flag and not (saw_availability_flag and snap.available):
        snap.available = False
    # Товара нет в продаже — это тоже успешная проверка, просто без цены.
    # Но если признака нет и цену найти не удалось, проверка считается неудачной.
    if not snap.price_regular and not snap.price_card:
        if out_of_stock_flag or (saw_availability_flag and not snap.available):
            snap.available = False
            snap.ok = True
            snap.error = "нет в наличии"

    return snap


# --------------------------------------------------------------------------
# Работа с URL
# --------------------------------------------------------------------------

def clean_url(url: str) -> str:
    """Убирает utm-метки и прочий мусор из ссылки."""
    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url.lstrip("/")
    p = urlparse(url)
    path = p.path
    if not path.endswith("/"):
        path += "/"
    return urlunparse(("https", p.netloc or "www.ozon.ru", path, "", "", ""))


def product_path(url: str) -> str:
    """/product/<slug>-<id>/ — путь, который принимает composer-api."""
    return urlparse(clean_url(url)).path


def product_key_from_url(url: str) -> str:
    """Числовой идентификатор товара из ссылки; если нет — сам слаг."""
    path = product_path(url).rstrip("/")
    slug = path.rsplit("/", 1)[-1]
    m = re.search(r"(\d{6,})$", slug)
    return m.group(1) if m else (slug or path)


def is_ozon_product_url(url: str) -> bool:
    p = urlparse(clean_url(url))
    return "ozon." in (p.netloc or "") and "/product/" in (p.path or "")


# --------------------------------------------------------------------------
# Стратегия 1: внутренний JSON-API
# --------------------------------------------------------------------------

class OzonSession:
    """Сессия requests с прогретыми cookie."""

    def __init__(self, timeout: int = 30, proxy: Optional[str] = None):
        self.timeout = timeout
        self.session = requests.Session()
        self.user_agent = random.choice(USER_AGENTS)
        self.session.headers.update(BASE_HEADERS)
        self.session.headers["User-Agent"] = self.user_agent
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})
        self._warmed = False

    def warm_up(self) -> None:
        """Заходим на главную, чтобы получить cookie — без них API часто отдаёт 403."""
        if self._warmed:
            return
        try:
            self.session.get("https://www.ozon.ru/", timeout=self.timeout)
        except requests.RequestException as exc:
            log.debug("Разогрев сессии не удался: %s", exc)
        self._warmed = True

    def fetch_api(self, url: str) -> PriceSnapshot:
        self.warm_up()
        path = product_path(url)
        api = "https://www.ozon.ru/api/composer-api.bx/page/json/v2?url=" + path
        headers = {
            "Accept": "application/json",
            "Referer": clean_url(url),
            "x-requested-with": "XMLHttpRequest",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        resp = self.session.get(api, headers=headers, timeout=self.timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"API вернул HTTP {resp.status_code}")
        data = resp.json()

        # Ozon может ответить редиректом внутри JSON
        states = data.get("widgetStates") or {}
        if not states and isinstance(data.get("seo"), dict):
            raise RuntimeError("API вернул страницу без цен (возможно, капча)")
        if not states:
            raise RuntimeError("В ответе API нет widgetStates")

        snap = extract_from_widget_states(states, clean_url(url))
        snap.source = "api"
        if not snap.ok:
            raise RuntimeError("В ответе API не нашлось цены")
        return snap

    def fetch_html(self, url: str) -> PriceSnapshot:
        self.warm_up()
        target = clean_url(url)
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://www.ozon.ru/",
        }
        resp = self.session.get(target, headers=headers, timeout=self.timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"Страница вернула HTTP {resp.status_code}")
        states = extract_states_from_html(resp.text)
        if not states:
            raise RuntimeError("На странице не нашлось данных о цене (возможно, капча)")
        snap = extract_from_widget_states(states, target)
        snap.source = "html"
        if not snap.ok:
            raise RuntimeError("В HTML-странице не нашлось цены")
        return snap


_STATE_DIV_RE = re.compile(
    r'<div\b(?P<attrs>[^>]*\bid="state-[^"]+"[^>]*)>', re.IGNORECASE
)
_ID_RE = re.compile(r'\bid="state-(?P<name>[^"]+)"')
_DATA_STATE_RE = re.compile(r"\bdata-state=(?P<q>[\"'])(?P<value>.*?)(?P=q)", re.DOTALL)


def extract_states_from_html(page_html: str) -> Dict[str, Any]:
    """
    Ozon встраивает состояние виджетов прямо в разметку:
        <div id="state-webPrice-3121879-default-1" data-state="{&quot;price&quot;:...}">
    Собираем их в словарь, совместимый с widgetStates.
    """
    states: Dict[str, Any] = {}
    for m in _STATE_DIV_RE.finditer(page_html):
        attrs = m.group("attrs")
        id_m = _ID_RE.search(attrs)
        ds_m = _DATA_STATE_RE.search(attrs)
        if not id_m or not ds_m:
            continue
        name = id_m.group("name")
        raw = html_lib.unescape(ds_m.group("value"))
        states[name] = raw
    return states


# --------------------------------------------------------------------------
# Стратегия 3: настоящий браузер (Playwright)
# --------------------------------------------------------------------------

def fetch_via_browser(url: str, timeout: int = 60, headless: bool = True) -> PriceSnapshot:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            "Playwright не установлен. Чтобы включить запасной вариант через браузер:\n"
            "    pip install playwright && playwright install chromium"
        )

    target = clean_url(url)
    captured: Dict[str, Any] = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            viewport={"width": 1440, "height": 900},
        )
        # Прячем самый заметный признак автоматизации
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = context.new_page()

        def on_response(response):
            if "composer-api.bx/page/json" in response.url and response.status == 200:
                try:
                    data = response.json()
                except Exception:
                    return
                ws = data.get("widgetStates")
                if ws:
                    captured.update(ws)

        page.on("response", on_response)

        try:
            page.goto(target, wait_until="domcontentloaded", timeout=timeout * 1000)
            try:
                page.wait_for_selector('[id^="state-webPrice"]', timeout=15000)
            except Exception:
                page.wait_for_timeout(4000)
            page_html = page.content()
        finally:
            context.close()
            browser.close()

    states: Dict[str, Any] = dict(captured)
    states.update(extract_states_from_html(page_html))
    if not states:
        raise RuntimeError("Браузер не увидел данных о цене (возможно, капча)")

    snap = extract_from_widget_states(states, target)
    snap.source = "browser"
    if not snap.ok:
        raise RuntimeError("Браузер загрузил страницу, но цена не распознана")
    return snap


# --------------------------------------------------------------------------
# Публичная точка входа
# --------------------------------------------------------------------------

def fetch_price(
    url: str,
    session: Optional[OzonSession] = None,
    strategies: Tuple[str, ...] = ("api", "html", "browser"),
    retries: int = 2,
    timeout: int = 30,
    proxy: Optional[str] = None,
    headless: bool = True,
) -> PriceSnapshot:
    """
    Пытается получить цену всеми доступными способами по очереди.
    Всегда возвращает PriceSnapshot; при неудаче ok=False и заполнен error.
    """
    target = clean_url(url)
    session = session or OzonSession(timeout=timeout, proxy=proxy)
    errors: List[str] = []

    for strategy in strategies:
        for attempt in range(1, retries + 1):
            try:
                if strategy == "api":
                    snap = session.fetch_api(target)
                elif strategy == "html":
                    snap = session.fetch_html(target)
                elif strategy == "browser":
                    snap = fetch_via_browser(target, timeout=max(timeout, 60), headless=headless)
                else:
                    continue
                log.debug("Цена получена через «%s» (попытка %d)", strategy, attempt)
                return snap
            except Exception as exc:  # noqa: BLE001 — нужен любой сбой
                msg = f"{strategy}: {exc}"
                log.debug("Не сработало — %s", msg)
                if attempt == retries:
                    errors.append(msg)
                else:
                    time.sleep(random.uniform(2, 5))

    return PriceSnapshot(
        url=target,
        product_key=product_key_from_url(target),
        ok=False,
        error="; ".join(errors) or "не удалось получить цену",
    )
