#!/usr/bin/env python3
"""Проверки напоминаний ко времени (сеть не нужна)."""
import sys
from datetime import datetime, timedelta
import notify_due as N

ok=fail=0
def check(name,cond):
    global ok,fail
    if cond: ok+=1
    else: fail+=1; print("FAIL:",name)

day="2026-09-01"           # вторник
def at(h,m): return datetime(2026,9,1,h,m)

state={"settings":{"leadMin":"0"},"items":[
  {"name":"Квартира","status":"active","instructions":[],"tasks":[
     {"id":"t1","text":"Оплатить аренду","done":False,"due":day,"time":"08:00"},
     {"id":"t2","text":"Позвонить мастеру","done":False,"due":day,"time":"14:30"},
     {"id":"t3","text":"Без времени","done":False,"due":day},
     {"id":"t4","text":"Выполненная","done":True,"due":day,"time":"08:00"}]},
  {"name":"Проданное","status":"sold","instructions":[],"tasks":[
     {"id":"t5","text":"Не слать","done":False,"due":day,"time":"08:00"}]}],
 "events":[
   {"id":"e1","title":"Работа","date":"2021-05-19","time":"10:00","repeat":"weekly","days":[0,1,2,3,4]},
   {"id":"e2","title":"Врач","date":day,"time":"15:30"},
   {"id":"e3","title":"Без времени","date":day}],
 "birthdays":[]}

# ── попадание в окно ──
r=N.due_now(state,at(8,0))
check("задача в 8:00 попадает в окно 8:00", any("аренду" in x["title"] for x in r))
check("выполненная не попадает", not any("Выполненная" in x["title"] for x in r))
check("проданное не попадает", not any("Не слать" in x["title"] for x in r))
check("без времени не попадает", not any("Без времени" in x["title"] for x in r))
check("в 8:00 нет чужих", len(r)==1)

check("задача в 8:10 ловится окном 8:00", any("аренду" in x["title"] for x in N.due_now(state,at(8,0),15)))
check("в 7:40 ещё рано", not N.due_now(state,at(7,40)))
check("в 8:15 уже поздно (окно ушло)", not any("аренду" in x["title"] for x in N.due_now(state,at(8,15))))
check("мероприятие в 15:30", any("Врач" in x["title"] for x in N.due_now(state,at(15,30))))
check("повторяющаяся работа во вторник в 10:00", any("Работа" in x["title"] for x in N.due_now(state,at(10,0))))
check("задача 14:30", any("мастеру" in x["title"] for x in N.due_now(state,at(14,30))))

# ── «напоминать заранее» ──
lead=dict(state); lead["settings"]={"leadMin":"30"}
check("за 30 минут: в 7:30 приходит про 8:00", any("аренду" in x["title"] for x in N.due_now(lead,at(7,30))))
check("за 30 минут: в 8:00 уже не дублируется", not any("аренду" in x["title"] for x in N.due_now(lead,at(8,0))))

# ── ключи для защиты от повторов ──
r1=N.due_now(state,at(8,0)); r2=N.due_now(state,at(8,0))
check("ключ стабилен между запусками", r1[0]["key"]==r2[0]["key"])
check("ключ содержит дату и время", day in r1[0]["key"] and "08:00" in r1[0]["key"])
ev=[x for x in N.due_now(state,at(15,30)) if "Врач" in x["title"]][0]
check("ключ мероприятия отличается от задачи", ev["key"].startswith("e:"))

# ── тексты ──
check("в тексте задачи есть объект", "Квартира" in r1[0]["body"])
check("в тексте задачи есть время", "08:00" in r1[0]["body"])
check("у мероприятия помечено «сегодня»", "сегодня" in ev["body"])

# ── ночное окно и переход на завтра при «заранее» ──
night=dict(state); night["settings"]={"leadMin":"60"}
night["events"]=[{"id":"e9","title":"Ранний рейс","date":"2026-09-02","time":"00:30"}]
check("за час ловим завтрашние 00:30 в 23:30", any("рейс" in x["title"] for x in N.due_now(night,at(23,30))))
check("и помечаем «завтра»", any("завтра" in x["body"] for x in N.due_now(night,at(23,30))))

print(f"\nПроверок пройдено: {ok}, провалено: {fail}")
sys.exit(1 if fail else 0)
