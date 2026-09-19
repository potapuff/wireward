#!/usr/bin/env python3
"""Частоти тихих помилок у корпусі (П3.1) — без бази й без psycopg.

Перевіряється не «чи спрацьовує правило» — це робить test-rules — а те, як
скрипт РАХУЄ: що входить у знаменник, що вважається повтором і чи не
змішуються між собою студенти й форми запитів. Помилка тут не видно ніяк:
число просто вийде інше, і в статті буде не те, що виміряно.
"""
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "front"))
_db = types.ModuleType("app.db")
_db.WORK_ONLY = "true"
_db.fetchall = _db.close_pools = _db.open_pools = None
sys.modules["app.db"] = _db

from app.rulecheck import AHADI_2016, analyse  # noqa: E402

fails = 0
T0 = datetime(2026, 9, 1, 10, 0)


def check(name, cond):
    global fails
    print(("  ok  " if cond else "  FAIL") + "  " + name)
    if not cond:
        fails += 1


def r(student, sql, sec=0, status="ok", state=None, rows=None, fp=None):
    return (student, fp or sql, sql, status, state, rows,
            T0 + timedelta(seconds=sec))


BAD = "select * from emp where mgr = null"
BAD2 = "select name from dept where deptno = null"
GOOD = "select * from emp where mgr is null"

print("знаменник")
data = analyse([r("s1", BAD), r("s1", GOOD)])
check("частка рахується від УСПІШНИХ запитів",
      next(x for x in data["rules"] if x["code"] == "eq_null")["share"] == 0.5)
# Помилковий запит студент бачить сам; тихі помилки — про ті, що виконалися.
data = analyse([r("s1", BAD), r("s1", BAD, 1, "error", "42601")])
check("запит з помилкою СУБД у тихі не рахується",
      next(x for x in data["rules"] if x["code"] == "eq_null")["hits"] == 1)
check("він же рахується в гучні", data["states"] == {"42601": 1})
check("успішних один, а не два", data["ok"] == 1)

print("студенти й повтори")
data = analyse([r("s1", BAD), r("s2", BAD)])
row = next(x for x in data["rules"] if x["code"] == "eq_null")
check("двоє студентів з тією самою помилкою", row["students"] == 2)
check("однакова форма в різних людей — не повтор", row["repeaters"] == 0)

data = analyse([r("s1", BAD), r("s1", BAD, 1)])
row = next(x for x in data["rules"] if x["code"] == "eq_null")
check("той самий запит двічі — одна помилка, не повтор", row["repeaters"] == 0)
check("а спрацювань усе одно два", row["hits"] == 2)

data = analyse([r("s1", BAD), r("s1", BAD2, 1)])
check("та сама помилка в РІЗНИХ запитах — повтор",
      next(x for x in data["rules"] if x["code"] == "eq_null")["repeaters"] == 1)

print("розріз по лабах")
data = analyse([r("s1", BAD), r("s2", BAD2)],
               labs={BAD: 1, BAD2: 2}, lab_titles={1: "Лаб 3", 2: "Лаб 4"})
check("кожна помилка пішла у свою лабу",
      data["per_lab"] == {"Лаб 3": {"eq_null": 1}, "Лаб 4": {"eq_null": 1}})
data = analyse([r("s1", BAD)], labs={}, lab_titles={})
check("запит поза лабами в розріз не потрапляє", data["per_lab"] == {})

print("вибірка для розмітки")
data = analyse([r("s1", BAD, i) for i in range(30)])
check("прикладів не більше за ліміт", len(data["samples"]["eq_null"]) <= 8)
check("у прикладі є текст запиту",
      data["samples"]["eq_null"][0]["sql"] == BAD)

print("порожній журнал")
data = analyse([])
check("нічого не падає", data["queries"] == 0)
check("усі правила з нулями", all(x["hits"] == 0 for x in data["rules"]))
check("частка нуль, а не ділення на нуль",
      all(x["share"] == 0.0 for x in data["rules"]))

print("таблиця Ahadi")
check("частки правдоподібні", all(0 < v <= 100 for v in AHADI_2016.values()))
check("сума не перевищує 100%", sum(AHADI_2016.values()) <= 100)
check("42601 найчастіший, як у статті",
      max(AHADI_2016, key=AHADI_2016.get) == "42601")

print("FAIL" if fails else "усе гаразд")
sys.exit(1 if fails else 0)
