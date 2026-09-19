#!/usr/bin/env python3
"""Розбір файлу лабораторних (П0.5) — без бази й без psycopg.

Перевіряється не «розбирає чи ні», а що файл, у якому викладач помилився,
НЕ проходить мовчки. Лаба без груп або без зразків заводиться без жодної
помилки і дає порожній звіт — саме це і треба ловити тут, а не в грудні.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "front"))
from app.labfile import parse, problems  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    print(("  ok  " if cond else "  FAIL") + "  " + name)
    if not cond:
        fails += 1


GOOD = """
# Вибірка з однієї таблиці
групи: 2026, 2025
поріг: 0,6
заборонено: join, subquery
нотатка: перше заняття

## Товари дорожчі за 100
select name, price from products where price > 100;

## Найдорожчий
select name from products
 order by price desc limit 1;

# Зʼєднання
групи: 2026

## Клієнти та замовлення
select c.name, o.id from customers c join orders o on o.customer_id = c.id;
"""

print("правильний файл")
labs = parse(GOOD)
check("дві лабораторні", len(labs) == 2)
check("групи через кому", labs[0]["groups"] == ["2026", "2025"])
check("кома в порозі як десяткова", labs[0]["threshold"] == 0.6)
check("заборонені конструкції", labs[0]["forbidden"] == ["join", "subquery"])
check("нотатка", labs[0]["note"] == "перше заняття")
check("зразків у першій", len(labs[0]["expectations"]) == 2)
check("назва зразка", labs[0]["expectations"][0]["title"] == "Товари дорожчі за 100")
check("багаторядковий SQL склеєно", "order by price desc" in labs[0]["expectations"][1]["sql"])
check("ключі другої лаби не позичені в першої", labs[1]["forbidden"] == [])
check("зауважень немає", problems(labs) == [])

print("тихі помилки, які інакше зʼясуються в грудні")
check("лаба без груп", any("групи" in p for p in problems(parse("# Л\n## з\nselect 1;"))))
check("лаба без зразків", any("зразка" in p for p in problems(parse("# Л\nгрупи: 2026\n"))))
check("невідома конструкція",
      any("невідома" in p for p in problems(parse("# Л\nгрупи: 2026\nзаборонено: джоін\n## з\nselect 1;"))))
check("повтор назви",
      any("повторюється" in p for p in problems(parse(
          "# Л\nгрупи: 2026\n## з\nselect 1;\n# Л\nгрупи: 2026\n## з\nselect 2;"))))

print("порожній зразок не рахується")
labs = parse("# Л\nгрупи: 2026\n## порожній\n\n## справжній\nselect 1;")
check("лишився один зразок", len(labs[0]["expectations"]) == 1)

print("зразок раніше за лабораторну — це помилка файлу, а не тиха втрата")
try:
    parse("## з\nselect 1;")
    check("викинуто ValueError", False)
except ValueError:
    check("викинуто ValueError", True)

print("приклад у репозиторії розбирається і не має зауважень")
example = Path(__file__).resolve().parent.parent / "labs" / "example.md"
labs = parse(example.read_text(encoding="utf-8"))
check("дві лабораторні в прикладі", len(labs) == 2)
check("без зауважень", problems(labs) == [])

print("FAIL" if fails else "усе гаразд")
sys.exit(1 if fails else 0)
