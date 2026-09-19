#!/usr/bin/env python3
"""SUS (П2.6) — без бази.

Перевіряється саме підрахунок. Формула SUS неочевидна — непарні твердження
рахуються від одиниці, парні від пʼятірки — і помилка в знаку дала б число,
яке виглядає правдоподібно й означає протилежне.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "front"))
from app.surveys import SUS_ITEMS, SUS_SCALE, sus_score  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    print(("  ok  " if cond else "  FAIL") + "  " + name)
    if not cond:
        fails += 1


def answers(pattern):
    return {f"q{i}": pattern(i) for i in range(1, 11)}


print("шкала")
check("рівно десять тверджень", len(SUS_ITEMS) == 10)
check("пʼять поділок", len(SUS_SCALE) == 5)

print("підрахунок")
check("найкращі відповіді = 100",
      sus_score(answers(lambda i: 5 if i % 2 else 1)) == 100.0)
check("найгірші = 0", sus_score(answers(lambda i: 1 if i % 2 else 5)) == 0.0)
check("усе посередині = 50", sus_score(answers(lambda i: 3)) == 50.0)
# Найважливіше: чергування знаків. Якби парні рахувалися як непарні,
# «усе пʼятірки» дало б 100 — тобто найгірший результат виглядав би найкращим.
check("усе «цілком згоден» — НЕ 100: парні твердження негативні",
      sus_score(answers(lambda i: 5)) == 50.0)
check("усе «зовсім не згоден» теж 50", sus_score(answers(lambda i: 1)) == 50.0)

print("межі")
for bad in (0, 6, -1):
    try:
        sus_score(answers(lambda i: bad))
        check(f"значення {bad} відхилено", False)
    except ValueError:
        check(f"значення {bad} відхилено", True)
try:
    sus_score({"q1": 3})
    check("неповна анкета відхилена", False)
except KeyError:
    check("неповна анкета відхилена", True)

print("FAIL" if fails else "усе гаразд")
sys.exit(1 if fails else 0)
