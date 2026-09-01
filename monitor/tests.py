#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Проверка логики монитора без обращения к Ozon.
Запуск:  python tests.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import monitor
import ozon_fetch
from ozon_fetch import (
    PriceSnapshot,
    clean_url,
    extract_from_widget_states,
    extract_states_from_html,
    is_ozon_product_url,
    parse_money,
    product_key_from_url,
    product_path,
)

PASS = 0
FAIL = 0


def check(name: str, got, expected) -> None:
    global PASS, FAIL
    if got == expected:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}\n       получили: {got!r}\n       ожидали:  {expected!r}")


def section(title: str) -> None:
    print(f"\n{title}")


# --------------------------------------------------------------------------
section("1. Разбор денежных строк")
# --------------------------------------------------------------------------

check("обычная цена", parse_money("77 990 ₽"), 77990.0)
check("неразрывный пробел", parse_money("77 990 ₽"), 77990.0)
check("копейки через запятую", parse_money("1 234,50 ₽"), 1234.5)
check("число", parse_money(74990), 74990.0)
check("строка-число", parse_money("74990.00"), 74990.0)
check("пусто", parse_money(""), None)
check("ноль отбрасывается", parse_money("0 ₽"), None)
check("None", parse_money(None), None)
check("булево не цена", parse_money(True), None)
check("мусор", parse_money("скоро в продаже"), None)


# --------------------------------------------------------------------------
section("2. Работа со ссылками")
# --------------------------------------------------------------------------

URL_DIRTY = "https://www.ozon.ru/product/kofemashina-icm1507-397529235/?asb=abc&utm_source=x"
check("очистка от меток", clean_url(URL_DIRTY),
      "https://www.ozon.ru/product/kofemashina-icm1507-397529235/")
check("путь для API", product_path(URL_DIRTY), "/product/kofemashina-icm1507-397529235/")
check("id товара", product_key_from_url(URL_DIRTY), "397529235")
check("ссылка без схемы", clean_url("ozon.ru/product/abc-123456/"),
      "https://ozon.ru/product/abc-123456/")
check("это товар Ozon", is_ozon_product_url(URL_DIRTY), True)
check("это не товар Ozon", is_ozon_product_url("https://www.wildberries.ru/catalog/1/detail.aspx"), False)


# --------------------------------------------------------------------------
section("3. Разбор виджетов: схема webPrice")
# --------------------------------------------------------------------------

STATES_WEBPRICE = {
    "webProductHeading-146888-default-1": json.dumps(
        {"title": "Кофемашина InHouse Coffee Arte ICM1507, серый"}, ensure_ascii=False
    ),
    "webPrice-3121879-default-1": json.dumps(
        {
            "isAvailable": True,
            "price": "17 990 ₽",
            "originalPrice": "29 990 ₽",
            "cardPrice": "16 490 ₽",
            "showOriginalPrice": True,
        },
        ensure_ascii=False,
    ),
}

snap = extract_from_widget_states(STATES_WEBPRICE, URL_DIRTY)
check("распознано", snap.ok, True)
check("название", snap.title, "Кофемашина InHouse Coffee Arte ICM1507, серый")
check("цена с картой", snap.price_card, 16490.0)
check("обычная цена", snap.price_regular, 17990.0)
check("цена до скидки", snap.price_original, 29990.0)
check("в наличии", snap.available, True)
check("отслеживаемая (card)", snap.tracked_price("card"), 16490.0)
check("отслеживаемая (regular)", snap.tracked_price("regular"), 17990.0)
check("отслеживаемая (original)", snap.tracked_price("original"), 29990.0)


# --------------------------------------------------------------------------
section("4. Разбор виджетов: схема cellTrackingInfo")
# --------------------------------------------------------------------------

STATES_TRACKING = {
    "webSale-3231710-default-1": json.dumps(
        {
            "cellTrackingInfo": {
                "product": {
                    "title": "Наушники Sony WH-1000XM5",
                    "price": 39990,
                    "finalPrice": 27490,
                }
            }
        },
        ensure_ascii=False,
    )
}

snap2 = extract_from_widget_states(STATES_TRACKING, "https://www.ozon.ru/product/sony-961244530/")
check("распознано", snap2.ok, True)
check("название", snap2.title, "Наушники Sony WH-1000XM5")
check("платим", snap2.price_regular, 27490.0)
check("до скидки", snap2.price_original, 39990.0)
check("id", snap2.product_key, "961244530")


# --------------------------------------------------------------------------
section("5. Защита от перепутанных цен")
# --------------------------------------------------------------------------

STATES_SWAPPED = {
    "webPrice-1-default-1": json.dumps(
        {"isAvailable": True, "price": "29 990 ₽", "originalPrice": "17 990 ₽"},
        ensure_ascii=False,
    )
}
snap3 = extract_from_widget_states(STATES_SWAPPED, "https://www.ozon.ru/product/x-111111/")
check("цены переставлены местами", (snap3.price_regular, snap3.price_original), (17990.0, 29990.0))

STATES_BAD_CARD = {
    "webPrice-1-default-1": json.dumps(
        {"isAvailable": True, "price": "17 990 ₽", "cardPrice": "19 990 ₽"},
        ensure_ascii=False,
    )
}
snap4 = extract_from_widget_states(STATES_BAD_CARD, "https://www.ozon.ru/product/x-111111/")
check("нелогичная цена по карте отброшена", snap4.price_card, None)


# --------------------------------------------------------------------------
section("6. Нет в наличии")
# --------------------------------------------------------------------------

STATES_OOS = {
    "webOutOfStock-3121879-default-1": json.dumps(
        {"sellerName": "Продавец", "isAvailable": False}, ensure_ascii=False
    )
}
snap5 = extract_from_widget_states(STATES_OOS, "https://www.ozon.ru/product/x-222222/")
check("товара нет", snap5.available, False)
check("цена отсутствует", snap5.tracked_price("card"), None)


# --------------------------------------------------------------------------
section("7. Извлечение состояний из HTML-страницы")
# --------------------------------------------------------------------------

HTML_PAGE = """
<html><body>
  <div id="layoutPage">
    <div id="state-webProductHeading-146888-default-1"
         data-state="{&quot;title&quot;:&quot;Тестовый товар&quot;}"></div>
    <div id="state-webPrice-3121879-default-1" class="a1b2"
         data-state="{&quot;isAvailable&quot;:true,&quot;price&quot;:&quot;5 490 ₽&quot;,&quot;cardPrice&quot;:&quot;4 990 ₽&quot;,&quot;originalPrice&quot;:&quot;8 990 ₽&quot;}"></div>
    <div id="not-a-state" data-state="{&quot;price&quot;:&quot;1 ₽&quot;}"></div>
  </div>
</body></html>
"""

states_html = extract_states_from_html(HTML_PAGE)
check("найдено состояний", len(states_html), 2)
snap6 = extract_from_widget_states(states_html, "https://www.ozon.ru/product/test-333333/")
check("название из HTML", snap6.title, "Тестовый товар")
check("цена с картой из HTML", snap6.price_card, 4990.0)
check("обычная цена из HTML", snap6.price_regular, 5490.0)
check("до скидки из HTML", snap6.price_original, 8990.0)


# --------------------------------------------------------------------------
section("8. Форматирование сумм")
# --------------------------------------------------------------------------

check("целая сумма", monitor.money(74990), "74 990 ₽")
check("с копейками", monitor.money(1234.5), "1 234,50 ₽")
check("нет данных", monitor.money(None), "—")
check("экранирование HTML", monitor.escape_html("Кабель <USB> & Type-C"),
      "Кабель &lt;USB&gt; &amp; Type-C")


# --------------------------------------------------------------------------
section("9. Логика уведомлений (сквозной сценарий)")
# --------------------------------------------------------------------------

class FakeTelegram(monitor.Telegram):
    def __init__(self):
        super().__init__("fake-token", "fake-chat")
        self.sent = []

    def send(self, text, disable_preview=True):
        self.sent.append(text)
        return True


TEST_URL = "https://www.ozon.ru/product/test-tovar-444444/"
scenario_price = {"value": None, "fail": False}


def fake_fetch_price(url, **kwargs):
    if scenario_price["fail"]:
        return PriceSnapshot(url=clean_url(url), product_key=product_key_from_url(url),
                             ok=False, error="api: HTTP 403")
    return PriceSnapshot(
        url=clean_url(url),
        product_key=product_key_from_url(url),
        title="Тестовый товар",
        price_card=scenario_price["value"],
        price_regular=scenario_price["value"] + 500,
        price_original=scenario_price["value"] + 3000,
        available=True,
        ok=True,
        source="api",
    )


monitor.fetch_price = fake_fetch_price

tmpdir = Path(tempfile.mkdtemp())
store = monitor.Storage(tmpdir / "test.db")
tg = FakeTelegram()
cfg = json.loads(json.dumps(monitor.DEFAULT_CONFIG))
entry = {"url": TEST_URL, "target_price": 8000}


def run_step(price=None, fail=False):
    scenario_price["value"] = price
    scenario_price["fail"] = fail
    return monitor.check_product(entry, cfg, store, tg, session=None)


r1 = run_step(10000)
check("первая проверка: не минимум", r1["new_minimum"], False)
check("первая проверка: сообщение отправлено", r1["notified"], True)
check("текст про начало слежки", "Начал следить" in tg.sent[-1], True)

r2 = run_step(11000)
check("цена выросла: тишина", (r2["new_minimum"], r2["notified"]), (False, False))

r3 = run_step(9000)
check("новый минимум распознан", r3["new_minimum"], True)
check("уведомление о минимуме", "Новый минимум" in tg.sent[-1], True)
check("в тексте новая цена", "9 000 ₽" in tg.sent[-1], True)
check("в тексте прежний минимум", "10 000 ₽" in tg.sent[-1], True)
check("посчитана экономия", "−1 000 ₽" in tg.sent[-1], True)

before = len(tg.sent)
r4 = run_step(9000)
check("та же цена — повторно не шлём", (r4["new_minimum"], len(tg.sent)), (False, before))

r5 = run_step(9500)
check("выше минимума — тишина", r5["new_minimum"], False)

r6 = run_step(8500)
check("следующий минимум найден", r6["new_minimum"], True)
check("сравнение с 9 000, а не с 10 000", "9 000 ₽" in tg.sent[-1], True)

before = len(tg.sent)
r7 = run_step(7500)
check("минимум ниже цели: одно сообщение", len(tg.sent) - before, 1)
check("это сообщение о минимуме", "Новый минимум" in tg.sent[-1], True)

before = len(tg.sent)
r8 = run_step(7800)
check("цена ниже цели, но не минимум", (r8["new_minimum"], len(tg.sent) - before), (False, 1))
check("сработала цель", "цель" in tg.sent[-1].lower(), True)

# --- ошибки ---
before = len(tg.sent)
run_step(fail=True)
check("1-я ошибка: молчим", len(tg.sent) - before, 0)
run_step(fail=True)
check("2-я ошибка: молчим", len(tg.sent) - before, 0)
run_step(fail=True)
check("3-я ошибка подряд: предупреждение", len(tg.sent) - before, 1)
check("текст предупреждения", "Не могу проверить" in tg.sent[-1], True)
run_step(fail=True)
check("4-я ошибка: без спама", len(tg.sent) - before, 1)

check("минимум в базе", store.minimum("444444")["price"], 7500.0)
check("удачных проверок", store.observations("444444"), 8)


# --------------------------------------------------------------------------
section("10. Загрузка конфигурации")
# --------------------------------------------------------------------------

cfg_file = tmpdir / "config.json"
cfg_file.write_text(json.dumps({
    "telegram": {"bot_token": "T"},
    "check_interval_minutes": 30,
    "products": [{"url": TEST_URL}],
}, ensure_ascii=False), encoding="utf-8")

loaded = monitor.load_config(cfg_file)
check("значение пользователя", loaded["check_interval_minutes"], 30)
check("значение по умолчанию подставлено", loaded["track_price"], "card")
check("вложенный словарь слит", loaded["telegram"]["chat_id"], "")
check("товары на месте", len(loaded["products"]), 1)


# --------------------------------------------------------------------------
section("11. Цены из каруселей «похожие товары» не попадают в историю")
# --------------------------------------------------------------------------

OUR_URL = "https://www.ozon.ru/product/kofemashina-397529235/"

STATES_WITH_CAROUSEL = {
    "webPrice-3121879-default-1": json.dumps(
        {"isAvailable": True, "price": "16 490 ₽", "cardPrice": "15 990 ₽"},
        ensure_ascii=False,
    ),
    # Карусель «с этим товаром покупают» — чужой товар за 990 ₽
    "skuShelfGoods-3311234-default-1": json.dumps(
        {
            "items": [
                {
                    "cellTrackingInfo": {
                        "product": {"id": 555555555, "title": "Капсулы для кофе",
                                    "price": 1290, "finalPrice": 990}
                    }
                }
            ]
        },
        ensure_ascii=False,
    ),
}

snap7 = extract_from_widget_states(STATES_WITH_CAROUSEL, OUR_URL)
check("цена нашего товара", snap7.tracked_price("card"), 15990.0)
check("чужая цена не подменила обычную", snap7.price_regular, 16490.0)
check("чужое название не подставилось", snap7.title, None)

# Тот же случай, но у нашего товара нет блока webPrice — только карусель
STATES_ONLY_CAROUSEL = {
    "skuShelfGoods-3311234-default-1": STATES_WITH_CAROUSEL["skuShelfGoods-3311234-default-1"]
}
snap8 = extract_from_widget_states(STATES_ONLY_CAROUSEL, OUR_URL)
check("одна карусель — цена не распознана", snap8.ok, False)

# А если id в cellTrackingInfo совпадает с нашим товаром — берём смело
STATES_MATCHING_ID = {
    "tileCarousel-1-default-1": json.dumps(
        {
            "cellTrackingInfo": {
                "product": {"id": 397529235, "title": "Кофемашина",
                            "price": 29990, "finalPrice": 17990}
            }
        },
        ensure_ascii=False,
    )
}
snap9 = extract_from_widget_states(STATES_MATCHING_ID, OUR_URL)
check("свой товар по совпадению id", snap9.price_regular, 17990.0)
check("и цена до скидки", snap9.price_original, 29990.0)


# --------------------------------------------------------------------------
section("12. Явный признак «нет в наличии» не игнорируется")
# --------------------------------------------------------------------------

STATES_UNAVAILABLE = {
    "webPrice-1-default-1": json.dumps(
        {"isAvailable": False, "price": "17 990 ₽", "cardPrice": "16 990 ₽"},
        ensure_ascii=False,
    )
}
snap10 = extract_from_widget_states(STATES_UNAVAILABLE, "https://www.ozon.ru/product/x-777777/")
check("цена прочитана", snap10.tracked_price("card"), 16990.0)
check("но товара нет в наличии", snap10.available, False)
check("в тексте появится пометка", "нет в наличии" in monitor.price_details(snap10, "card"), True)


# --------------------------------------------------------------------------
section("13. Разные виды цен не сравниваются между собой")
# --------------------------------------------------------------------------

check("вид цены: с картой", snap7.tracked("card"), (15990.0, "card"))
check("вид цены: обычная", snap7.tracked("regular"), (16490.0, "regular"))

STATES_NO_CARD = {
    "webPrice-1-default-1": json.dumps(
        {"isAvailable": True, "price": "16 490 ₽"}, ensure_ascii=False
    )
}
snap11 = extract_from_widget_states(STATES_NO_CARD, OUR_URL)
check("карты нет — берём обычную и помечаем вид", snap11.tracked("card"), (16490.0, "regular"))

store2 = monitor.Storage(tmpdir / "kinds.db")
tg2 = FakeTelegram()
cfg2 = json.loads(json.dumps(monitor.DEFAULT_CONFIG))
entry2 = {"url": OUR_URL}

kind_sequence = [snap11, snap7, snap11]  # обычная 16490 -> с картой 15990 -> обычная 16490
monitor.fetch_price = lambda url, **kw: kind_sequence.pop(0)

r_a = monitor.check_product(entry2, cfg2, store2, tg2, session=None)
check("первая запись — обычная цена", (r_a["price"], r_a["new_minimum"]), (16490.0, False))
r_b = monitor.check_product(entry2, cfg2, store2, tg2, session=None)
check("появилась цена с картой — это не «минимум»", r_b["new_minimum"], False)
check("но она записана", r_b["price"], 15990.0)
r_c = monitor.check_product(entry2, cfg2, store2, tg2, session=None)
check("возврат к обычной цене — тоже не «минимум»", r_c["new_minimum"], False)


# --------------------------------------------------------------------------
section("14. Устойчивость к кривому config.json")
# --------------------------------------------------------------------------

check("пауза списком", monitor.parse_delay([3, 9]), (3.0, 9.0))
check("пауза одним числом", monitor.parse_delay(10), (10.0, 10.0))
check("пауза из одного элемента", monitor.parse_delay([7]), (7.0, 7.0))
check("пауза-мусор", monitor.parse_delay("быстро"), (5.0, 15.0))
check("пауза отсутствует", monitor.parse_delay(None), (5.0, 15.0))

null_cfg = tmpdir / "null.json"
null_cfg.write_text(
    json.dumps({"telegram": {"bot_token": None, "chat_id": None}, "products": []}),
    encoding="utf-8",
)
loaded_null = monitor.load_config(null_cfg)
check("null в токене не роняет загрузку", loaded_null["telegram"]["bot_token"], "")
check("null в chat_id не превращается в 'None'", loaded_null["telegram"]["chat_id"], "")
check("Telegram считается ненастроенным",
      monitor.Telegram(loaded_null["telegram"]["bot_token"],
                       loaded_null["telegram"]["chat_id"]).configured, False)

# Кривые записи товаров не должны ронять весь обход
store3 = monitor.Storage(tmpdir / "broken.db")
cfg3 = json.loads(json.dumps(monitor.DEFAULT_CONFIG))
cfg3["delay_between_products"] = 0
cfg3["products"] = [
    {"name": "запись без ссылки"},
    "https://www.ozon.ru/product/stroka-888888/",
    {"url": "https://www.ozon.ru/product/normalnyy-999999/"},
    None,
]
monitor.fetch_price = lambda url, **kw: PriceSnapshot(
    url=clean_url(url), product_key=product_key_from_url(url), title="Товар",
    price_card=1000.0, price_regular=1200.0, available=True, ok=True, source="api",
)
res = monitor.run_once(cfg3, store3, FakeTelegram())
check("проверены только корректные записи", len(res), 2)
check("все успешно", all(r["ok"] for r in res), True)


# --------------------------------------------------------------------------
section("15. Тонкие случаи опознания «своего» товара")
# --------------------------------------------------------------------------

SONY_URL = "https://www.ozon.ru/product/naushniki-sony-961244530/"

# id внутренний, а совпадает sku — товар всё равно наш
STATES_SKU = {
    "webSale-1-default-1": json.dumps(
        {"cellTrackingInfo": {"product": {"id": 1552881000, "sku": 961244530,
                                          "title": "Наушники Sony", "price": 39990,
                                          "finalPrice": 27490}}},
        ensure_ascii=False,
    )
}
snap12 = extract_from_widget_states(STATES_SKU, SONY_URL)
check("совпадение по sku принято", snap12.price_regular, 27490.0)

# Карусель, чьё имя лишь содержит «webSale», не должна считаться своим виджетом
STATES_FAKE_OWN = {
    "webSaleCarousel-9-default-1": json.dumps(
        {"cellTrackingInfo": {"product": {"title": "Чехол", "price": 890, "finalPrice": 690}}},
        ensure_ascii=False,
    )
}
snap13 = extract_from_widget_states(STATES_FAKE_OWN, SONY_URL)
check("карусель с похожим именем отвергнута", snap13.ok, False)
check("и чужое название не взято", snap13.title, None)

# Виджет «нет в наличии» не перебивает явный признак доступности
STATES_MIXED_STOCK = {
    "webPrice-1-default-1": json.dumps(
        {"isAvailable": True, "price": "5 490 ₽"}, ensure_ascii=False
    ),
    "webOutOfStock-2-default-1": json.dumps({"sellerName": "другой продавец"},
                                            ensure_ascii=False),
}
snap14 = extract_from_widget_states(STATES_MIXED_STOCK, SONY_URL)
check("явное «в наличии» сильнее чужого блока", snap14.available, True)


# --------------------------------------------------------------------------
section("16. Обновление старой базы и повторное «начал следить»")
# --------------------------------------------------------------------------

# Старая база: колонки price_kind ещё нет
legacy_db = tmpdir / "legacy.db"
legacy = sqlite3.connect(str(legacy_db))
legacy.executescript(
    """
    CREATE TABLE checks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, product_key TEXT NOT NULL,
        url TEXT NOT NULL, title TEXT, price REAL, price_regular REAL,
        price_card REAL, price_original REAL, available INTEGER NOT NULL DEFAULT 0,
        ok INTEGER NOT NULL DEFAULT 0, error TEXT, source TEXT, checked_at TEXT NOT NULL);
    CREATE TABLE notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT, product_key TEXT NOT NULL,
        kind TEXT NOT NULL, price REAL, sent_at TEXT NOT NULL);
    """
)
legacy.executemany(
    """INSERT INTO checks (product_key, url, title, price, price_regular, price_card,
                           price_original, available, ok, source, checked_at)
       VALUES (?,?,?,?,?,?,?,1,1,'api',?)""",
    [
        ("961244530", SONY_URL, "Наушники Sony", 17990.0, 17990.0, None, 25990.0, "2026-08-01T10:00:00"),
        ("961244530", SONY_URL, "Наушники Sony", 16490.0, 17990.0, 16490.0, 25990.0, "2026-08-05T10:00:00"),
    ],
)
legacy.commit()
legacy.close()

store4 = monitor.Storage(legacy_db)
kinds = [r["price_kind"] for r in store4.history("961244530")]
check("вид цены восстановлен из старых записей", kinds, ["regular", "card"])
check("минимум по обычной цене", store4.minimum("961244530", "regular")["price"], 17990.0)
check("минимум по цене с картой", store4.minimum("961244530", "card")["price"], 16490.0)

# Появилась цена с картой 16 490 при прежней обычной 17 990 — это не минимум
monitor.fetch_price = lambda url, **kw: PriceSnapshot(
    url=clean_url(url), product_key="961244530", title="Наушники Sony",
    price_card=16490.0, price_regular=17990.0, price_original=25990.0,
    available=True, ok=True, source="api",
)
tg4 = FakeTelegram()
r_legacy = monitor.check_product({"url": SONY_URL}, cfg, store4, tg4, session=None)
check("ложного минимума на старой базе нет", r_legacy["new_minimum"], False)
check("и сообщений тоже нет", len(tg4.sent), 0)

# Смена вида цены не должна повторять «Начал следить за товаром»
store5 = monitor.Storage(tmpdir / "firsts.db")
tg5 = FakeTelegram()
switch = [snap11, snap7, snap11]  # обычная -> с картой -> обычная
monitor.fetch_price = lambda url, **kw: switch.pop(0)
for _ in range(3):
    monitor.check_product({"url": OUR_URL}, cfg, store5, tg5, session=None)
first_messages = [m for m in tg5.sent if "Начал следить" in m]
check("приветственное сообщение ровно одно", len(first_messages), 1)


# --------------------------------------------------------------------------
section("17. Восстановление вида цены учитывает настройку track_price")
# --------------------------------------------------------------------------

def make_legacy_db(path: Path, rows) -> Path:
    """Старая база без колонки price_kind."""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, product_key TEXT NOT NULL,
            url TEXT NOT NULL, title TEXT, price REAL, price_regular REAL,
            price_card REAL, price_original REAL, available INTEGER NOT NULL DEFAULT 0,
            ok INTEGER NOT NULL DEFAULT 0, error TEXT, source TEXT, checked_at TEXT NOT NULL);
        CREATE TABLE notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT, product_key TEXT NOT NULL,
            kind TEXT NOT NULL, price REAL, sent_at TEXT NOT NULL);
        """
    )
    conn.executemany(
        """INSERT INTO checks (product_key, url, title, price, price_regular,
                               price_card, price_original, available, ok, source, checked_at)
           VALUES (?,?,?,?,?,?,?,1,1,'api',?)""",
        rows,
    )
    conn.commit()
    conn.close()
    return path


# Товар без скидки по Ozon Карте: card == regular. При track_price=regular
# такие записи должны опознаваться как «обычная цена», а не как «с картой».
TIE_ROWS = [
    ("444444", TEST_URL, "Товар", 15000.0, 15000.0, 15000.0, 21000.0, "2026-08-01T10:00:00"),
    ("444444", TEST_URL, "Товар", 17000.0, 17000.0, None, 21000.0, "2026-08-05T10:00:00"),
]

store_reg = monitor.Storage(make_legacy_db(tmpdir / "tie_reg.db", TIE_ROWS), track_price="regular")
check("track_price=regular: вид цены — обычная",
      [r["price_kind"] for r in store_reg.history("444444")], ["regular", "regular"])
check("минимум виден весь", store_reg.minimum("444444", "regular")["price"], 15000.0)

monitor.fetch_price = lambda url, **kw: PriceSnapshot(
    url=clean_url(url), product_key="444444", title="Товар",
    price_card=None, price_regular=16000.0, price_original=21000.0,
    available=True, ok=True, source="api",
)
tg6 = FakeTelegram()
cfg_reg = json.loads(json.dumps(monitor.DEFAULT_CONFIG))
cfg_reg["track_price"] = "regular"
r_tie = monitor.check_product({"url": TEST_URL}, cfg_reg, store_reg, tg6, session=None)
check("16 000 при минимуме 15 000 — не минимум", r_tie["new_minimum"], False)
check("сообщений нет", len(tg6.sent), 0)

# Тот же набор при track_price=card опознаётся как цена с картой
store_card = monitor.Storage(make_legacy_db(tmpdir / "tie_card.db", TIE_ROWS), track_price="card")
check("track_price=card: первая запись — с картой",
      [r["price_kind"] for r in store_card.history("444444")], ["card", "regular"])


# --------------------------------------------------------------------------
section("18. Дозаполнение работает и на уже обновлённой базе")
# --------------------------------------------------------------------------

half_db = tmpdir / "half.db"
monitor.Storage(make_legacy_db(half_db, TIE_ROWS))     # колонка появилась
conn = sqlite3.connect(str(half_db))                    # ...но значения затёрли
conn.execute("UPDATE checks SET price_kind = NULL")
conn.commit()
conn.close()

store_half = monitor.Storage(half_db, track_price="card")
check("вид цены дозаполнен при следующем открытии",
      [r["price_kind"] for r in store_half.history("444444")], ["card", "regular"])
check("старый минимум снова виден", store_half.minimum("444444", "card")["price"], 15000.0)


# --------------------------------------------------------------------------
section("19. Нет в наличии — уведомление о цели не приходит")
# --------------------------------------------------------------------------

store6 = monitor.Storage(tmpdir / "oos.db")
tg7 = FakeTelegram()
cfg_oos = json.loads(json.dumps(monitor.DEFAULT_CONFIG))
cfg_oos["notify_on_first_check"] = False

monitor.fetch_price = lambda url, **kw: PriceSnapshot(
    url=clean_url(url), product_key=product_key_from_url(url), title="Товар",
    price_card=16490.0, price_regular=17990.0, available=False, ok=True, source="api",
)
r_oos = monitor.check_product({"url": TEST_URL, "target_price": 20000}, cfg_oos,
                              store6, tg7, session=None)
check("цена записана", r_oos["price"], 16490.0)
check("но уведомления о цели нет", len(tg7.sent), 0)

monitor.fetch_price = lambda url, **kw: PriceSnapshot(
    url=clean_url(url), product_key=product_key_from_url(url), title="Товар",
    price_card=16490.0, price_regular=17990.0, available=True, ok=True, source="api",
)
monitor.check_product({"url": TEST_URL, "target_price": 20000}, cfg_oos,
                      store6, tg7, session=None)
check("появился в наличии — цель сработала", len(tg7.sent), 1)
check("это сообщение о цели", "цели" in tg7.sent[-1], True)


# --------------------------------------------------------------------------
print(f"\n{'=' * 50}")
print(f"Пройдено: {PASS}   Провалено: {FAIL}")
print("=" * 50)
sys.exit(1 if FAIL else 0)
