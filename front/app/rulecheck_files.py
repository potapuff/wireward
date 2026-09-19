"""Чи ловлять правила тихих помилок хоч щось — на РЕАЛЬНИХ студентських звітах.

`rulecheck` рахує спрацювання каталогу на живому журналі проксі. Тут журналу
немає: на вхід — запити, вибрані `scripts/extract-lab-queries.py` зі звітів,
зданих у LMS. Це інший матеріал і інше питання. Журнал показує роботу, звіт
показує її результат, і саме через це звіт — найсуворіша перевірка каталогу:
помилка, що дожила до зданої роботи, не була помічена ніким.

**Класифікація хибних спрацювань живе в `rules.py`, не тут.** Перша версія
цього модуля дублювала регулярні вирази для «яке спрацювання хибне» поруч із
самими правилами — і два набори одразу почали розходитися: правило вже не
ловило конструкцію, а копія класифікатора в цьому файлі про це не знала й
далі рахувала спрацювання «хибним», якого вже не було. Тому тут не
переоцінюється жодне рішення `check()`: якщо правило спрацювало — це кандидат
у помилку за визначенням правила. Модуль додає лише те, чого немає в самому
правилі:

* **Обрив витягу.** Текст зі звіту, вирваний з дужками, що не закрилися, —
  ознака зіпсованого фрагмента, не студентської помилки; на живому журналі
  проксі цього не буває, тут — типово.
* **Розклад підрахунку за колонкою.** `count(колонка)` — єдине правило
  каталогу, де виняток («ідіома зовнішнього з'єднання за ключем») сам є
  евристикою, а не строгим фактом; тут показано, скільки випадків узагалі
  вживають конструкцію, скільки виключено ідіомою і скільки лишається
  нерозв'язним без схеми — тими самими функціями, що й у самому правилі.
* **Група ризику.** Для пастки `NOT IN` окремо рахується, скільки запитів
  узагалі на неї наражаються, незалежно від того, спрацювало правило чи ні.

Запуск: `make check-rules-files` (або `python3 -m app.rulecheck_files [шлях]`).
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from hashlib import sha1
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if not __package__:
    sys.path.insert(0, str(_ROOT / "front"))
    from app.rules import (
        RULES, TITLES, _clean, _COUNT_COL, _IS_NULL, _KEY_LIKE, _NOT_IN_OPEN,
        _is_subquery, _outer_join_aliases, _spans, check,
    )
else:
    from .rules import (
        RULES, TITLES, _clean, _COUNT_COL, _IS_NULL, _KEY_LIKE, _NOT_IN_OPEN,
        _is_subquery, _outer_join_aliases, _spans, check,
    )

# `not_in_empty` вимагає rows_affected із живого журналу — звіт його не
# містить, і check(sql, None) не може повернути цей код НІКОЛИ на цьому
# матеріалі. Виключено з таблиці, а не показано як «0 спрацювань»: нуль тут
# означав би «такої помилки в звітах нема», а насправді модуль її не бачить
# за побудовою.
_MEASURABLE = tuple(r for r in RULES if r[0] != "not_in_empty")

DEFAULT_JSONL = _ROOT / "labs" / "labs2" / "_extracted" / "queries.jsonl"
SAMPLE_PER_RULE = 8

_WS = re.compile(r"\s+")
_NUM = re.compile(r"\b\d+\b")
_STR_LIT = re.compile(r"'(?:[^']|'')*'")


def fingerprint(sql: str) -> str:
    """Відбиток форми запиту: без літералів, чисел і форматування."""
    s = _NUM.sub("?", _STR_LIT.sub("?", sql or ""))
    return sha1(_WS.sub(" ", s).strip().lower().encode()).hexdigest()[:16]


def unbalanced(sql: str) -> bool:
    """Незбалансовані дужки — ознака обірваного витягу зі звіту, не помилки
    студента: production-правило працює на повних запитах із живого журналу,
    де такого не буває."""
    s = _clean(sql)
    return s.count("(") != s.count(")")


def load(path: Path) -> list[dict]:
    with path.open() as f:
        return [json.loads(line) for line in f]


def analyse(rows: list[dict]) -> dict:
    """Чиста арифметика над списком записів звітів."""
    hits: dict[str, int] = defaultdict(int)
    torn_hits: dict[str, int] = defaultdict(int)
    who: dict[str, set] = defaultdict(set)
    forms: dict[str, dict] = defaultdict(lambda: defaultdict(set))
    per_lab: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    samples: dict[str, list] = defaultdict(list)

    kinds: dict[str, int] = defaultdict(int)
    fps: set[str] = set()
    subs: set[str] = set()
    labs: set[str] = set()
    torn = 0

    for r in rows:
        sql, lab = r.get("sql") or "", r.get("lab") or "?"
        sid = r.get("source") or r.get("student") or "?"
        labs.add(lab)
        subs.add(sid)
        fps.add(fingerprint(sql))
        word = sql.strip().split()
        kinds[word[0].lower() if word else "?"] += 1
        is_torn = unbalanced(sql)
        torn += is_torn
        for code in check(sql, None):
            hits[code] += 1
            who[code].add(sid)
            forms[code][sid].add(fingerprint(sql))
            per_lab[lab][code] += 1
            if is_torn:
                torn_hits[code] += 1
            if len(samples[code]) < SAMPLE_PER_RULE:
                samples[code].append(sql)

    # Розклад count_column: скільки запитів узагалі вживають конструкцію,
    # скільки виключено ідіомою підрахунку за ключем зовнішнього з'єднання,
    # скільки лишається нерозв'язним без схеми. Ті самі функції, що й у
    # самому правилі (`_outer_join_aliases`, `_KEY_LIKE`) — не копія.
    cc_exposure = cc_idiom = 0
    for r in rows:
        s = _clean(r.get("sql") or "")
        ms = list(_COUNT_COL.finditer(s))
        if not ms:
            continue
        cc_exposure += 1
        aliases = _outer_join_aliases(s)
        if all(m.group(1) and m.group(1).lower() in aliases
               and _KEY_LIKE.match(m.group(2)) for m in ms):
            cc_idiom += 1

    # Група ризику пастки NOT IN: запит із NOT IN (SELECT …) без жодного
    # захисту IS NOT NULL усередині підзапиту — те, повз що детектор
    # проходить свідомо (рівень «текст + схема», не помилка правила).
    at_risk = guarded = 0
    for r in rows:
        s = _clean(r.get("sql") or "")
        for inner in _spans(s, _NOT_IN_OPEN):
            if not _is_subquery(inner):
                continue
            if _IS_NULL.search(inner):
                guarded += 1
            else:
                at_risk += 1
            break

    n = len(rows)
    return {
        "queries": n, "labs": len(labs), "submissions": len(subs),
        "fingerprints": len(fps), "torn": torn,
        "kinds": dict(sorted(kinds.items(), key=lambda kv: -kv[1])[:10]),
        "rules": [{
            "code": code,
            "title": TITLES[code][0],
            "hits": hits.get(code, 0),
            "torn_hits": torn_hits.get(code, 0),
            "students": len(who.get(code, ())),
            "repeaters": sum(1 for f in forms[code].values() if len(f) > 1),
        } for code, _t, _e, _f in _MEASURABLE],
        "per_lab": {k: dict(v) for k, v in sorted(per_lab.items())},
        "samples": dict(samples),
        "at_risk": at_risk, "guarded": guarded,
        "cc_exposure": cc_exposure, "cc_idiom": cc_idiom,
    }


def report(d: dict) -> None:
    print(f"Тихі помилки у зданих звітах: {d['queries']} запитів, "
          f"{d['submissions']} робіт, {d['labs']} лабораторних, "
          f"{d['fingerprints']} унікальних форм.\n")

    print("ЩО ЦЕ ЗА МАТЕРІАЛ. Це не журнал роботи, а те, що студент вписав у")
    print("зданий звіт. Помилка, знайдена тут, пережила і власну перевірку")
    print("студента, і здачу. Помилки, виправленої під час роботи, тут немає")
    print("за побудовою — і саме тому числа нижче не є частотою помилок.")
    print(f"   обірваних витягів (незбалансовані дужки): {d['torn']} "
          f"({100 * d['torn'] / d['queries']:.1f}%)")
    print("   склад: " + ", ".join(f"{k} {v}" for k, v in d["kinds"].items()) + "\n")

    print("ПРАВИЛА. «Спрац.» — рахується так само, як у бойовому коді:")
    print("спрацювання, яке пояснюється контекстом (SET, захист IS NOT NULL,")
    print("гілка операції над множинами, ідіома ключа при JOIN), правило вже")
    print("не показує. «З обриву» — скільки спрацювань припадає на")
    print("незбалансований фрагмент, артефакт витягу зі звіту, не запит.")
    print("Правило «NOT IN (SELECT …) повернув 0 рядків» тут не показане:")
    print("воно потребує rows_affected із живого журналу, якого звіт не")
    print("містить, — не 0 спрацювань, а неможливість це виміряти взагалі.\n")
    head = f"{'правило':30} {'спрац.':>7} {'з обриву':>9} {'робіт':>6} {'повтор.':>8}"
    print(head)
    print("-" * len(head))
    for r in sorted(d["rules"], key=lambda r: -r["hits"]):
        print(f"{r['title'][:30]:30} {r['hits']:7d} {r['torn_hits']:9d} "
              f"{r['students']:6d} {r['repeaters']:8d}")

    print("\nРОЗКЛАД count(колонка). Єдине правило каталогу, де виняток —")
    print("евристика (ключ зовнішнього з'єднання), а не строгий факт.")
    print(f"   запитів із конструкцією взагалі:            {d['cc_exposure']}")
    print(f"   із них виключено ідіомою підрахунку за ключем: {d['cc_idiom']}")
    print(f"   лишається нерозв'язним без схеми:             "
          f"{d['cc_exposure'] - d['cc_idiom']}\n")

    print("ЧОГО ТЕКСТ НЕ ДІСТАЄ (пастка NOT IN).")
    print(f"   запитів NOT IN (SELECT …) із захистом IS NOT NULL: {d['guarded']}")
    print(f"   запитів NOT IN (SELECT …) без жодного захисту:     {d['at_risk']}")
    print("   Другий рядок — група ризику: чи справді там пастка, залежить від")
    print("   того, чи допускає NULL колонка підзапиту. Текст цього не знає,")
    print("   тому правило мовчить — і саме цей розрив міряє межу рівня.\n")

    print("ВИБІРКА ДЛЯ ОЧЕЙ. Усі спрацювання правил, що лишилися рідкісними —")
    print("це і є матеріал для читання вручну, а не для ручної розмітки за")
    print("вибіркою: спрацювань мало, є сенс прочитати кожне.\n")
    for r in sorted(d["rules"], key=lambda x: -x["hits"]):
        ex = d["samples"].get(r["code"])
        if not ex:
            continue
        print(f"— {r['title']} ({r['hits']} спрацювань, показано {len(ex)})")
        for s in ex:
            print("     " + " ".join(s.split())[:150])
        print()


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else DEFAULT_JSONL
    if not path.exists():
        print(f"Немає {path}. Спершу: python3 scripts/extract-lab-queries.py "
              f"labs/labs2", file=sys.stderr)
        return 1
    rows = load(path)
    if not rows:
        print("Порожній корпус.", file=sys.stderr)
        return 1
    report(analyse(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
