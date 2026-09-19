#!/usr/bin/env python3
"""Перевірка вибору SQL зі звітів (З7) — без файлів, на рядках."""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("ex", ROOT / "scripts" / "extract-lab-queries.py")
ex = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ex)

bad = 0


def check(name, got, want):
    global bad
    if got != want:
        bad = 1
        print(f"  ✗ {name}\n     маємо: {got}\n     треба: {want}")
    else:
        print(f"  ✓ {name}")


# --- вибір стейтментів ---------------------------------------------------------
check("один запит із крапкою з комою",
      ex._statements("SELECT name FROM emp;"), ["SELECT name FROM emp"])

check("два INSERT в одному абзаці розбиваються",
      ex._statements("INSERT INTO a VALUES (1); INSERT INTO b VALUES (2);"),
      ["INSERT INTO a VALUES (1)", "INSERT INTO b VALUES (2)"])

check("проза після запиту обриває його",
      ex._statements("SELECT ename FROM Emp\nЦей запит показує всіх працівників."),
      ["SELECT ename FROM Emp"])

check("продовження на кількох рядках збирається",
      ex._statements("SELECT a\nFROM t\nWHERE a > 1;"),
      ["SELECT a FROM t WHERE a > 1"])

check("порожній рядок завершує стейтмент",
      ex._statements("SELECT a FROM t\n\nSELECT b FROM u"),
      ["SELECT a FROM t", "SELECT b FROM u"])

check("опечатка у ключовому слові все одно ловиться",
      ex._statements("SELCT * FROM orders;"), ["SELCT * FROM orders"])

check("рядок без SQL-слова не породжує стейтмент",
      ex._statements("Опис лабораторної роботи номер сім про тригери."), [])

# --- відбраковка сміття ------------------------------------------------------
check("справжній запит лишається", ex._keep("SELECT ename, sal FROM Emp WHERE sal > 0"), True)
check("надто короткий уламок відкидається", ex._keep("SELECT 1"), False)

# --- ключ студента ----------------------------------------------------------
check("прізвище з імені файлу", ex._student_key("1413200-Лаб2_Чекмарьов_ІН-31_БДІС.docx"), "Чекмарьов")
check("без прізвища — id LMS", ex._student_key("1429374-Лабораторна_робота_4.docx"), "1429374")

print("\nвибір SQL: усі перевірки пройдено" if not bad else "\nє розбіжності")
sys.exit(bad)
