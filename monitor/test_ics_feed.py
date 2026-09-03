#!/usr/bin/env python3
"""Проверки ленты календаря (сеть не нужна)."""
import sys, re
import ics_feed as F

ok=fail=0
def check(name,cond):
    global ok,fail
    if cond: ok+=1
    else: fail+=1; print("FAIL:",name)

state={"settings":{"leadMin":"15"},"items":[
  {"name":"Квартира","status":"active","tasks":[
    {"id":"t1","text":"Оплатить аренду","done":False,"due":"2026-09-10","time":"08:00","repeat":"monthly"},
    {"id":"t2","text":"Без времени","done":False,"due":"2026-09-12"},
    {"id":"t3","text":"Готово","done":True,"due":"2026-09-01"}]},
  {"name":"Проданное","status":"sold","tasks":[{"id":"t4","text":"Не должно быть","done":False,"due":"2026-09-05"}]}],
 "events":[{"id":"e1","title":"Работа","date":"2021-04-16","time":"10:00","repeat":"weekly","days":[0,1,2,3,4]},
           {"id":"e2","title":"Врач; с запятой, и точкой","date":"2026-09-08","time":"15:30"}],
 "birthdays":[{"id":"b1","name":"Анна","date":"1990-02-15"}]}

t=F.build(state,15)
check("обёртка календаря", t.startswith("BEGIN:VCALENDAR") and t.rstrip().endswith("END:VCALENDAR"))
check("события посчитаны", t.count("BEGIN:VEVENT")==5)
check("выполненная не попала", "Готово" not in t)
check("проданное не попало", "Не должно быть" not in t)
check("повтор задачи", "RRULE:FREQ=MONTHLY" in t)
check("будни у работы", "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR" in t)
check("день рождения ежегодно", "RRULE:FREQ=YEARLY" in t)
check("напоминание за 15 минут", "TRIGGER:-PT15M" in t)
check("у события без времени нет будильника", t.count("BEGIN:VALARM")==3)
check("спецсимволы экранированы", r"Врач\; с запятой\, и точкой" in t)
check("дата без времени как DATE", "DTSTART;VALUE=DATE:20260912" in t)
check("время без часового пояса", "DTSTART:20260910T080000" in t)
check("конец события через час", "DTEND:20260910T090000" in t)
check("строки не длиннее 75 октетов", all(len(l.encode())<=75 for l in t.split("\r\n")))
check("интервал обновления указан", "REFRESH-INTERVAL" in t)

long_state={"settings":{},"items":[{"name":"Дом","status":"active","tasks":[
  {"id":"x","text":"Очень длинная задача "*8,"done":False,"due":"2026-09-09"}]}],"events":[],"birthdays":[]}
lt=F.build(long_state)
check("длинная строка сложена", all(len(l.encode())<=75 for l in lt.split("\r\n")))
check("после склейки текст сохранился", "Очень длинная задача" in lt.replace("\r\n ",""))

check("правило без повтора пустое", F.rrule("")=="")
check("интервал в правиле", "INTERVAL=2" in F.rrule("weekly",[0],2))
check("окончание в правиле", "UNTIL=20261231T235900Z" in F.rrule("weekly",[0],1,"2026-12-31"))

print(f"\nПроверок пройдено: {ok}, провалено: {fail}")
sys.exit(1 if fail else 0)
