#!/usr/bin/env python3
"""Офлайн-проверки моста «реестр ↔ монитор» (сеть не нужна)."""
import base64, gzip, json, sys

import registry_sync as rs

ok = fail = 0
def check(name, cond):
    global ok, fail
    if cond:
        ok += 1
    else:
        fail += 1
        print("FAIL:", name)

# --- сборка состояния из кусочков, как их пишет реестр ---
def chunks_for(state, enc="gz", rev="7", size=880):
    raw = json.dumps(state, ensure_ascii=False).encode()
    payload = gzip.compress(raw) if enc == "gz" else raw
    b64 = base64.b64encode(payload).decode()
    parts = [b64[i:i+size] for i in range(0, len(b64), size)] or [""]
    return [{"name": f"c{i:03d}", "custom_properties":
             {"v": rev, "i": str(i), "n": str(len(parts)), "e": enc, "d": d}}
            for i, d in enumerate(parts)]

state = {"rev": 7, "wishes": [
    {"id": "w1", "title": "Робот-пылесос", "target": "25 000", "status": "active",
     "links": [{"id": "l1", "market": "ozon", "url": "https://www.ozon.ru/product/robot-123/"}]},
    {"id": "w2", "title": "Куплено давно", "status": "bought",
     "links": [{"id": "l2", "market": "ozon", "url": "https://www.ozon.ru/product/old-1/"}]},
    {"id": "w3", "title": "Только Wildberries", "status": "active",
     "links": [{"id": "l3", "market": "wb", "url": "https://wildberries.ru/x"}]},
    {"id": "w4", "title": "Без цели", "status": "active",
     "links": [{"id": "l4", "market": "ozon", "url": "https://www.ozon.ru/product/lamp-9/"}]},
]}

check("gzip-кусочки читаются", rs.decode_chunks(chunks_for(state))["rev"] == 7)
check("не сжатые кусочки читаются", rs.decode_chunks(chunks_for(state, enc="raw"))["rev"] == 7)
check("пустой список → None", rs.decode_chunks([]) is None)

torn = chunks_for(state)
if len(torn) > 1:
    torn[1]["custom_properties"]["v"] = "8"
    check("разные ревизии → None (запись идёт)", rs.decode_chunks(torn) is None)
else:
    lost = chunks_for(state)
    lost[0]["custom_properties"]["n"] = "2"
    check("недостающий кусочек → None", rs.decode_chunks(lost) is None)

# --- отбор желаний ---
w = rs.wishes_with_ozon(rs.decode_chunks(chunks_for(state)))
ids = [x[0] for x in w]
check("берутся только активные с Ozon", ids == ["w1", "w4"])
check("цель разобрана из «25 000»", w[0][3] == 25000)
check("без цели — None", w[1][3] is None)

# --- деньги ---
check("money: пробелы", rs.money("24 990") == 24990)
check("money: мусор", rs.money("—") is None)
check("money: число", rs.money(1500) == 1500)

# --- формат сумм ---
check("fmt разделяет тысячи", rs.fmt(24990) == "24 990 ₽")
check("fmt для None", rs.fmt(None) == "—")

# --- логика уведомлений ---
check("первая проверка → «начал следить»",
      "Начал следить" in rs.build_message("t", "u", 100, None, None))
check("ниже минимума → новый минимум",
      "Новый минимум" in rs.build_message("t", "u", 90, 100, None))
check("экономия посчитана",
      "10 ₽" in rs.build_message("t", "u", 90, 100, None))
check("цель достигнута при неизменном минимуме",
      "цели" in (rs.build_message("t", "u", 100, 100, 120) or ""))
check("выше минимума и цели → молчим",
      rs.build_message("t", "u", 110, 100, 50) is None)
check("минимум важнее цели",
      "Новый минимум" in rs.build_message("t", "u", 40, 100, 50))

print(f"\nПроверок пройдено: {ok}, провалено: {fail}")
sys.exit(1 if fail else 0)
