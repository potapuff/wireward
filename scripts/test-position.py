#!/usr/bin/env python3
"""Перевірка «де студент зараз» — без кластера.

Меж у лабораторних немає, тому тут немає й правильної відповіді «встиг / не
встиг». Перевіряється рівно те, що функція обіцяє: береться остання за часом
робота, поруч із нею показуються лаби, до яких студент повертався в ті самі
дні, і не показуються ті, які він кинув місяць тому.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "front"))

# `labs` тягне пул psycopg заради запитів, яких тут не буде жодного: перевірка
# суто про тлумачення готових рядків. Підставляємо порожній `app.db`, щоб тест
# ганявся без кластера й без залежностей, як `test-rules` і `test-indicators`.
import types  # noqa: E402
_db = types.ModuleType("app.db")
_db.WORK_ONLY = "true"
_db.execute = _db.fetchall = _db.fetchone = None
sys.modules["app.db"] = _db

from app.labs import PARALLEL_HOURS, where_now  # noqa: E402

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
ok = True


def lab(n, title, last_h, cov=50, missing=2, exp=4):
    return {"lab_id": n, "title": title, "expectations": exp, "coverage": cov,
            "queries": 30, "errors": 5, "missing": [{"id": i} for i in range(missing)],
            "first_at": NOW - timedelta(hours=last_h + 20),
            "last_at": NOW - timedelta(hours=last_h)}


def check(name, cond):
    global ok
    print(("  ok   " if cond else "  ПОМИЛКА ") + name)
    ok = ok and cond


print("порожній журнал")
check("нічого не показуємо, а не «лаба 1»", where_now([]) is None)
check("рядки без жодної спроби не рахуються",
      where_now([dict(lab(1, "Л1", 0), last_at=None, first_at=None)]) is None)

print("остання за часом")
p = where_now([lab(1, "Л1", 400), lab(3, "Л3", 2)], now=NOW)
check("поточна — та, де працював щойно", p["lab_id"] == 3)
check("покинуту місяць тому поруч не тягнемо", p["also"] == [])
check("годин простою рахується від останньої спроби", round(p["idle_hours"]) == 2)
check("закрито = зразки мінус те, чого немає", (p["done"], p["total"]) == (2, 4))

print("дві лаби впереміш")
p = where_now([lab(3, "Л3", 2), lab(4, "Л4", PARALLEL_HOURS - 1)], now=NOW)
check("сусідня за часом лаба показується поруч", [a["lab_id"] for a in p["also"]] == [4])
p = where_now([lab(3, "Л3", 2), lab(4, "Л4", PARALLEL_HOURS + 5)], now=NOW)
check("за межею вікна — вже не «одночасно»", p["also"] == [])

print("довга пауза")
p = where_now([lab(2, "Л2", 24 * 30)], now=NOW)
check("лаба лишається поточною попри місяць паузи", p["lab_id"] == 2)
check("простій видно числом, вердикту немає", round(p["idle_hours"]) == 720)

sys.exit(0 if ok else 1)
