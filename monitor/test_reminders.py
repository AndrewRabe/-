#!/usr/bin/env python3
"""Офлайн-проверки напоминаний (сеть не нужна)."""
import sys
from datetime import date, timedelta
import reminders as R

ok=fail=0
def check(name,cond):
    global ok,fail
    if cond: ok+=1
    else: fail+=1; print("FAIL:",name)

today=date(2026,9,1)               # вторник
y=(today-timedelta(days=3)).isoformat()
t=today.isoformat()
tm=(today+timedelta(days=1)).isoformat()

state={"items":[
  {"name":"Квартира","status":"active","tasks":[
     {"text":"Оплатить аренду","done":False,"due":t,"repeat":"monthly"},
     {"text":"Вызвать электрика","done":False,"due":y},
     {"text":"Старое дело","done":True,"due":y},
     {"text":"Завтрашнее","done":False,"due":tm}],
   "instructions":[{"title":"Показания счётчиков","due":t}]},
  {"name":"Проданное","status":"sold","tasks":[{"text":"Не должно попасть","done":False,"due":t}],"instructions":[]},
  {"name":"Автомобиль","status":"active","tasks":[],"instructions":[],
   "car":{"odo":"196000","plan":[
     {"name":"Масло","km":10000,"months":12,"lastKm":"186500","lastAt":"2025-09-01"},
     {"name":"Антифриз","km":60000,"months":48,"lastKm":"190000","lastAt":"2026-01-01"}]}}],
 "events":[
   {"title":"Работа","date":"2021-05-19","time":"10:00","repeat":"weekly","days":[0,1,2,3,4]},
   {"title":"Врач","date":t,"time":"15:30","repeat":""},
   {"title":"Давнее","date":"2020-01-01","repeat":""},
   {"title":"Только завтра","date":tm,"time":"09:00","repeat":""}],
 "birthdays":[{"name":"Анна","date":"1990-09-01"},{"name":"Пётр","date":"1988-12-31"}]}

cur=R.collect(state,today)
check("просроченная задача попала", any("электрика" in x for x in cur["overdue"]))
check("выполненная не попала", not any("Старое дело" in x for x in cur["overdue"]+cur["today"]))
check("сегодняшняя задача", any("аренду" in x for x in cur["today"]))
check("пометка периодической", any("🔁" in x for x in cur["today"]))
check("инструкция сегодня", any("счётчиков" in x for x in cur["today"]))
check("проданное исключено", not any("Не должно" in x for x in cur["today"]))
check("еженедельное мероприятие во вторник", any("Работа" in x for x in cur["events"]))
check("разовое сегодня", any("Врач" in x for x in cur["events"]))
check("давнее не попало", not any("Давнее" in x for x in cur["events"]))
check("завтрашнее не в сегодня", not any("Только завтра" in x for x in cur["events"]))
check("день рождения сегодня", cur["bd"]==["Анна"])
check("перепробег по маслу", any("Масло" in x and "пора" in x for x in cur["car"]))
check("антифриз ещё не горит", not any("Антифриз" in x for x in cur["car"]))

nxt=R.collect(state,today+timedelta(days=1))
check("завтрашняя задача видна", any("Завтрашнее" in x for x in nxt["today"]))
check("завтрашнее мероприятие", any("Только завтра" in x for x in nxt["events"]))

txt=R.build_text(state,today)
check("текст собран", txt and "Домашний реестр" in txt)
check("есть блок просрочено", "Просрочено" in txt)
check("есть блок завтра", "Завтра" in txt)
check("даты в человеческом виде", "01.09" in txt)

empty=R.build_text({"items":[],"events":[],"birthdays":[]},today)
check("пустой день — молчим", empty is None)

# еженедельное по средам не должно попадать во вторник
wed={"items":[],"events":[{"title":"Спортзал","date":"2026-08-26","repeat":"weekly","days":[2]}],"birthdays":[]}
check("среда не срабатывает во вторник", not R.collect(wed,today)["events"])
check("среда срабатывает в среду", R.collect(wed,today+timedelta(days=1))["events"]==["Спортзал"])

# ежемесячное 31-го числа
mon={"items":[],"events":[{"title":"Отчёт","date":"2026-01-31","repeat":"monthly"}],"birthdays":[]}
check("31-е в месяце с 31 днём", R.collect(mon,date(2026,10,31))["events"]==["Отчёт"])
check("31-е не срабатывает 30-го", not R.collect(mon,date(2026,10,30))["events"])

print(f"\nПроверок пройдено: {ok}, провалено: {fail}")
sys.exit(1 if fail else 0)
