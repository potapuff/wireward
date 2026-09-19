#!/usr/bin/env python3
"""Експорт у форматі ProgSnap2 (П2.7) — без бази й без psycopg.

Перевіряється те, ціна помилки в чому найвища: знеособлення. Набір даних
віддається назовні один раз, і якщо в ньому виявиться щось зайве, відкликати
його вже не можна.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "front"))
from app.export import (MAIN_COLUMNS, code_state_id, event_type,  # noqa: E402
                        main_row, metadata_csv, pseudonym)

fails = 0
MASTER = b"secret-for-tests-only"


def check(name, cond):
    global fails
    print(("  ok  " if cond else "  FAIL") + "  " + name)
    if not cond:
        fails += 1


print("псевдонім")
a = pseudonym(MASTER, "11111111-1111-1111-1111-111111111111")
b = pseudonym(MASTER, "11111111-1111-1111-1111-111111111111")
c = pseudonym(MASTER, "22222222-2222-2222-2222-222222222222")
check("той самий студент — той самий код", a == b)
check("різні студенти — різні коди", a != c)
check("вихідного uuid у коді немає", "1111" not in a)
check("інший ключ дає інший код", pseudonym(b"other-key", "11111111-1111-1111-1111-111111111111") != a)
check("сесії мають власний префікс", pseudonym(MASTER, "x", "N").startswith("N"))

print("стан коду")
check("однаковий запит — той самий стан",
      code_state_id("select 1") == code_state_id("select 1"))
check("інший запит — інший стан",
      code_state_id("select 1") != code_state_id("select 2"))

print("тип події")
check("помилка → Compile.Error", event_type("error") == "Compile.Error")
check("успіх → Run.Program", event_type("ok") == "Run.Program")

print("рядок MainTable")
rec = {"id": 42, "started_at": datetime(2026, 9, 4, 10, 30, tzinfo=timezone.utc),
       "status": "error", "sqlstate": "42601",
       "error_message": 'syntax error at or near "SELCT"\nпродовження',
       "duration_ms": 0.4, "rows_affected": None, "sql_kind": "select",
       "client": "psql", "lab_id": 3}
row = main_row(rec, "S0123", 7, "Nabc", "DB-LAB", "select ?")
check("усі колонки на місці", set(row) == set(MAIN_COLUMNS))
check("SQLSTATE у CompileMessageType", row["CompileMessageType"] == "42601")
check("CompileResult=Error", row["CompileResult"] == "Error")
check("ExecutionResult порожній для помилки", row["ExecutionResult"] == "")
check("перенос рядка в повідомленні прибрано — інакше CSV поїде",
      "\n" not in row["CompileMessageData"])
check("порядок як передали", row["Order"] == 7)
check("AssignmentID = лабораторна", row["AssignmentID"] == 3)

ok_row = main_row({**rec, "status": "ok", "sqlstate": None, "error_message": None},
                  "S0123", 8, "Nabc", "DB-LAB", "select ?")
check("успіх: ExecutionResult=Success", ok_row["ExecutionResult"] == "Success")
check("успіх: CompileResult=Success", ok_row["CompileResult"] == "Success")

print("метадані")
md = metadata_csv()
check("версія формату вказана", "Version,6" in md)
check("представлення коду — каталог", "CodeStateRepresentation,Directory" in md)

print("FAIL" if fails else "усе гаразд")
sys.exit(1 if fails else 0)
