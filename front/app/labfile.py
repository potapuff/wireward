"""Лабораторні з текстового файлу (П0.5).

Заводити чотири лаби через форму в адмінці — це десятки полів і жодного сліду
в git. Зразки викладача — такий самий матеріал курсу, як умови задач: їх правлять,
порівнюють версії й переносять між семестрами. Тому вони живуть файлом.

Формат навмисно мінімальний — це Markdown, який читається очима:

    # Вибірка з однієї таблиці
    групи: 2026, 2025
    поріг: 0.5
    заборонено: join, subquery
    нотатка: перше заняття, без зʼєднань

    ## Товари дорожчі за 100
    select name, price from products where price > 100;

    ## Найдорожчий товар
    select name from products order by price desc limit 1;

`#` — лабораторна, `##` — очікуваний запит, усе між заголовками — SQL. Ключі
пишуться до першого `##` і всі необовʼязкові, крім груп: лаба без групи не
потрапить у жоден звіт, бо звіт іде по групах.

**Ідемпотентність за назвою.** Повторний запуск не плодить дублікати: лаба з
такою самою назвою оновлюється — зразки й заборонені конструкції замінюються
цілком. Так виправлення зразка — це правка файлу і повторний запуск, а не
пошук потрібного рядка у формі.
"""
from __future__ import annotations

import re
import sys

from .sqlshape import CONSTRUCT_TITLES

# psycopg свідомо НЕ імпортується на рівні модуля: розбір файлу має
# перевірятися без кластера і без залежностей — так само, як test-shape.

_KEYS = {"групи": "groups", "поріг": "threshold",
         "заборонено": "forbidden", "нотатка": "note"}


def parse(text: str) -> list[dict]:
    labs: list[dict] = []
    lab: dict | None = None
    exp: dict | None = None

    def close_exp() -> None:
        nonlocal exp
        if exp is not None:
            exp["sql"] = exp["sql"].strip()
            if exp["sql"]:
                lab["expectations"].append(exp)
            exp = None

    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            if lab is None:
                raise ValueError("зразок ('## …') раніше за лабораторну ('# …')")
            close_exp()
            exp = {"title": line[3:].strip(), "sql": ""}
        elif line.startswith("# "):
            close_exp()
            lab = {"title": line[2:].strip(), "note": "", "groups": [],
                   "threshold": 0.5, "forbidden": [], "expectations": []}
            labs.append(lab)
        elif exp is not None:
            exp["sql"] += raw + "\n"
        elif lab is not None and ":" in line and line.split(":", 1)[0].strip() in _KEYS:
            key, value = line.split(":", 1)
            field = _KEYS[key.strip()]
            value = value.strip()
            if field == "groups":
                lab["groups"] = [g.strip() for g in value.split(",") if g.strip()]
            elif field == "threshold":
                lab["threshold"] = min(max(float(value.replace(",", ".")), 0.05), 1.0)
            elif field == "forbidden":
                lab["forbidden"] = [c.strip() for c in value.split(",") if c.strip()]
            else:
                lab["note"] = value
    close_exp()
    return labs


def problems(labs: list[dict]) -> list[str]:
    """Те, що зробить лабу мовчки марною. Саме мовчки — у цьому й річ."""
    out = []
    for lab in labs:
        if not lab["title"]:
            out.append("лабораторна без назви")
        if not lab["expectations"]:
            out.append(f"«{lab['title']}»: жодного зразка — звіт буде порожній")
        if not lab["groups"]:
            out.append(f"«{lab['title']}»: не вказано групи — лаба не потрапить у звіт")
        for c in lab["forbidden"]:
            if c not in CONSTRUCT_TITLES:
                out.append(f"«{lab['title']}»: невідома конструкція {c!r}; "
                           f"є: {', '.join(sorted(CONSTRUCT_TITLES))}")
    seen: set[str] = set()
    for lab in labs:
        if lab["title"] in seen:
            out.append(f"«{lab['title']}»: назва повторюється у файлі")
        seen.add(lab["title"])
    return out


async def apply(labs: list[dict]) -> list[str]:
    from .db import execute, fetchone

    done = []
    for lab in labs:
        row = await fetchone("SELECT id FROM obs.labs WHERE title = %s", (lab["title"],))
        if row:
            lab_id = row[0]
            await execute("UPDATE obs.labs SET note = %s, threshold = %s WHERE id = %s",
                          (lab["note"] or None, lab["threshold"], lab_id))
            # Зразки замінюються цілком, а не доповнюються: інакше виправлений
            # зразок жив би поруч зі старим, і покриття рахувалося б по обох.
            await execute("DELETE FROM obs.lab_expectations WHERE lab_id = %s", (lab_id,))
            await execute("DELETE FROM obs.lab_forbidden WHERE lab_id = %s", (lab_id,))
            await execute("DELETE FROM obs.lab_groups WHERE lab_id = %s", (lab_id,))
            verb = "оновлено"
        else:
            row = await fetchone(
                "INSERT INTO obs.labs (title, note, threshold) VALUES (%s,%s,%s) RETURNING id",
                (lab["title"], lab["note"] or None, lab["threshold"]))
            lab_id = row[0]
            verb = "створено"

        for g in lab["groups"]:
            await execute("INSERT INTO obs.lab_groups (lab_id, group_name) VALUES (%s,%s)"
                          " ON CONFLICT DO NOTHING", (lab_id, g))
        for e in lab["expectations"]:
            await execute(
                "INSERT INTO obs.lab_expectations (lab_id, title, sql_sample) VALUES (%s,%s,%s)",
                (lab_id, e["title"] or None, e["sql"]))
        for c in lab["forbidden"]:
            if c in CONSTRUCT_TITLES:
                await execute("INSERT INTO obs.lab_forbidden (lab_id, construct) VALUES (%s,%s)"
                              " ON CONFLICT DO NOTHING", (lab_id, c))
        done.append(f"  {verb}: «{lab['title']}» — зразків {len(lab['expectations'])}, "
                    f"групи {', '.join(lab['groups'])}, поріг {lab['threshold']}")
    return done


async def _main() -> int:
    from .db import close_pools, open_pools

    write = "--apply" in sys.argv
    text = sys.stdin.read()
    try:
        labs = parse(text)
    except ValueError as e:
        print(f"файл не розібрано: {e}", file=sys.stderr)
        return 2

    print(f"Знайдено лабораторних: {len(labs)}")
    for lab in labs:
        print(f"  «{lab['title']}» — зразків {len(lab['expectations'])}, "
              f"групи {', '.join(lab['groups']) or '—'}, поріг {lab['threshold']}"
              + (f", заборонено: {', '.join(lab['forbidden'])}" if lab["forbidden"] else ""))

    bad = problems(labs)
    for p in bad:
        print(f"  ⚠ {p}")
    if bad:
        print("Виправте перелічене — інакше лаба заведеться, але звіт буде порожній.",
              file=sys.stderr)
        return 1
    if not write:
        print("\nНічого не записано. Щоб застосувати, додайте --apply.")
        return 0

    await open_pools()
    try:
        for line in await apply(labs):
            print(line)
    finally:
        await close_pools()
    print("Готово.")
    return 0


if __name__ == "__main__":
    import asyncio
    sys.exit(asyncio.run(_main()))
