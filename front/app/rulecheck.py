"""Частоти тихих помилок у всьому корпусі (П3.1 — ядро статті P3).

`rules.check()` працює на екрані студента по останніх двох тисячах запитів і
відповідає на питання «про що спитати цього студента». Стаття питає інше:
**наскільки поширені ці помилки взагалі** — скільки студентів, скільки разів, у
яких лабораторних, і чи повертається та сама помилка до того самого студента.

Останнє питання головне, і воно не про частоту. Розрізнення йде ще від PROUST
(Johnson і Soloway, 1984): помилка, що трапилася один раз, найімовірніше описка;
та сама помилка, повторена в різних запитах, — свідчення хибного уявлення. Для
статті це різні знахідки, і рахувати їх треба окремо.

**Ручної розмітки цей скрипт не замінює.** Він рахує спрацювання правил, а не
помилки: точність евристики проти розмітки — окрема робота, і саме для неї в
кінці друкується вибірка запитів. Без неї числа кажуть, що правило спрацювало,
а не що студент помилився.

**З чим звіряти.** Ahadi та ін. (SIGCSE 2016) опублікували розподіл `SQLSTATE`
на 161 000 запитах 2 300 студентів, знятий так само — перевиконанням у
PostgreSQL. Наш розподіл друкується поруч із їхнім: розбіжність тут така сама
знахідка, як і збіг, бо в них онлайн-система з готовими завданнями, а в нас
жива робота в DBeaver. Розбір — docs/student-performance-metrics/05-sql-context.md.

Запуск: `make check-rules` (за весь журнал) або `make check-rules DAYS=30`.
"""
from __future__ import annotations

import sys
from collections import defaultdict

from .rules import RULES, TITLES, check

# Скільки прикладів на правило друкувати для ручної розмітки. Розмітка на
# 200–300 запитів — це те, що робить статтю емпіричною, а не описовою; більше
# за десяток на правило за раз ніхто не розмічає.
SAMPLE_PER_RULE = 8

# Розподіл SQLSTATE у Ahadi та ін. (SIGCSE 2016), 161 000 операторів SELECT від
# 2 300 студентів онлайн-системи AsseSQL, перевиконаних у PostgreSQL. Наведено
# для звірки, а не як еталон: у них іспитові завдання з готовим переліком, у нас
# вільна робота в клієнті. Частки — від усіх зібраних операторів.
AHADI_2016 = {
    "42601": 21.0, "42703": 13.0, "42803": 10.0, "42P01": 3.0,
    "42702": 3.0, "42883": 2.0, "42804": 1.0, "22008": 0.61,
    "22007": 0.53, "22P02": 0.33, "3F000": 0.28, "42704": 0.08,
    "42712": 0.07, "42P10": 0.06, "42P02": 0.03, "42725": 0.01,
}


async def corpus(days: int = 0) -> dict:
    """Один прохід журналу — усе, що потрібно для звіту.

    Читається `sql_text`, тому вибірка важка; це не екран, а разовий прогін
    перед написанням статті, і ходити по журналу двічі сенсу немає.
    """
    from .db import WORK_ONLY, fetchall
    from .labs import fingerprint_map

    since = ("'-infinity'::timestamptz" if days == 0
             else f"now() - interval '{int(days)} days'")
    rows = await fetchall(f"""
        SELECT q.student_id::text, q.sql_fingerprint, q.sql_text,
               q.status, q.sqlstate, q.rows_affected, q.started_at
          FROM obs.queries q
         WHERE {WORK_ONLY} AND q.started_at > {since}
         ORDER BY q.started_at
    """)
    labs = await fingerprint_map()
    lab_titles = {r[0]: r[1] for r in await fetchall(
        "SELECT id, title FROM obs.labs")}
    # Походження журналу. Числа з демо-сидерів і навантажувального тесту
    # виглядають так само, як студентські, і саме на цьому найлегше побудувати
    # статтю про неіснуюче явище. Тому склад клієнтів друкується завжди.
    clients = await fetchall(f"""
        SELECT coalesce(client_name, 'не представився'), count(*)::int
          FROM obs.sessions
         WHERE started_at > {since}
         GROUP BY 1 ORDER BY 2 DESC""")
    data = analyse([tuple(r) for r in rows], labs, lab_titles)
    data["clients"] = [(c, n) for c, n in clients]
    return data


def analyse(rows: list[tuple], labs: dict[str, int] | None = None,
            lab_titles: dict[int, str] | None = None) -> dict:
    """Чиста арифметика — щоб її можна було перевірити без кластера.

    Рядок: (student_id, fingerprint, sql_text, status, sqlstate,
            rows_affected, started_at).
    """
    labs = labs or {}
    lab_titles = lab_titles or {}

    hits: dict[str, int] = defaultdict(int)          # правило → спрацювань
    who: dict[str, set] = defaultdict(set)           # правило → студенти
    forms: dict[str, dict] = defaultdict(lambda: defaultdict(set))
    repeat: dict[str, set] = defaultdict(set)        # правило → студенти, що повторили
    per_lab: dict[int, dict] = defaultdict(lambda: defaultdict(int))
    samples: dict[str, list] = defaultdict(list)
    states: dict[str, int] = defaultdict(int)
    students = set()
    errors = 0

    for sid, fp, sql, status, state, rows_affected, at in rows:
        students.add(sid)
        if status == "error":
            errors += 1
            if state:
                states[state] += 1
            # Тихі помилки — про запити, що ВИКОНАЛИСЯ. Помилковий запит уже
            # видно самому студентові, і в цю статистику він не входить.
            continue
        for code in check(sql, rows_affected):
            hits[code] += 1
            who[code].add(sid)
            forms[code][sid].add(fp)
            lab = labs.get(fp)
            if lab:
                per_lab[lab][code] += 1
            if len(samples[code]) < SAMPLE_PER_RULE:
                samples[code].append({"student": sid, "sql": sql, "at": at,
                                      "lab": lab_titles.get(lab or 0)})

    for code, by_student in forms.items():
        # Повтор — та сама помилка в РІЗНИХ за формою запитах одного студента.
        # Той самий запит, надісланий тричі, це одна помилка, а не три.
        repeat[code] = {sid for sid, fps in by_student.items() if len(fps) > 1}

    ok = len(rows) - errors
    return {
        "queries": len(rows), "ok": ok, "errors": errors,
        "students": len(students),
        "rules": [{
            "code": code, "title": TITLES[code][0],
            "hits": hits.get(code, 0),
            "share": (hits.get(code, 0) / ok) if ok else 0.0,
            "students": len(who.get(code, ())),
            "repeaters": len(repeat.get(code, ())),
        } for code, _t, _e, _f in RULES],
        "per_lab": {lab_titles.get(k, f"лаба {k}"): dict(v)
                    for k, v in sorted(per_lab.items())},
        "samples": dict(samples),
        "states": dict(states),
    }


def _bar(x: float, width: int = 24) -> str:
    return "█" * round(x * width)


def report(data: dict) -> None:
    q, ok, students = data["queries"], data["ok"], data["students"]
    print(f"Тихі помилки в корпусі: {q} запитів, {students} студентів, "
          f"{ok} виконалися, {data['errors']} впали.\n")

    clients = data.get("clients")
    if clients:
        print("ЧИМ ЦЕ НАПИСАНО. Демо-сидери й навантажувальний тест дають числа,")
        print("не відрізнити від студентських — і стаття про неіснуюче явище")
        print("починається саме тут. Дивитися сюди ПЕРЕД тим, як вірити решті.")
        print("   " + ", ".join(f"{c}: {n}" for c, n in clients[:8]) + "\n")
    if not ok:
        print("Успішних запитів немає — рахувати нема чого.")
        return

    print("ПРАВИЛА. «Повторили» — студенти, у яких та сама помилка в РІЗНИХ за")
    print("формою запитах: один раз це описка, двічі — привід думати про хибне")
    print("уявлення (розрізнення ще від PROUST, 1984).\n")
    head = f"{'правило':22} {'спрац.':>7} {'% вик.':>7} {'студ.':>6} {'повтор.':>8}"
    print(head)
    print("-" * len(head))
    for r in sorted(data["rules"], key=lambda r: -r["hits"]):
        print(f"{r['title'][:22]:22} {r['hits']:7d} {r['share'] * 100:6.2f}% "
              f"{r['students']:6d} {r['repeaters']:8d}")

    if data["per_lab"]:
        print("\nПО ЛАБОРАТОРНИХ. Прив'язка нечітка (ADR-019), тож це розподіл")
        print("уваги, а не облік: запит поза будь-якою лабою сюди не потрапляє.\n")
        codes = [r["code"] for r in data["rules"] if r["hits"]]
        print(f"{'лабораторна':32} " + " ".join(f"{c[:9]:>9}" for c in codes))
        for lab, counts in data["per_lab"].items():
            print(f"{lab[:32]:32} " + " ".join(
                f"{counts.get(c, 0):>9}" for c in codes))

    if data["states"]:
        total = sum(data["states"].values())
        print(f"\nГУЧНІ ПОМИЛКИ для звірки з Ahadi та ін. (SIGCSE 2016).")
        print("У них — іспитові завдання онлайн-системи, у нас — вільна робота в")
        print("клієнті. Розбіжність тут така сама знахідка, як і збіг.\n")
        print(f"{'SQLSTATE':10} {'у нас':>8} {'частка':>8} {'Ahadi':>8}")
        print("-" * 38)
        for state, n in sorted(data["states"].items(), key=lambda kv: -kv[1])[:16]:
            ours = 100 * n / total
            theirs = AHADI_2016.get(state)
            print(f"{state:10} {n:8d} {ours:7.2f}% "
                  f"{(f'{theirs:6.2f}%' if theirs is not None else '     —'):>8}")

    print("\nВИБІРКА ДЛЯ РУЧНОЇ РОЗМІТКИ. Скрипт рахує СПРАЦЮВАННЯ ПРАВИЛ, а не")
    print("помилки: точність евристики без розмітки невідома. Прочитати очима й")
    print("позначити «справді помилка / хибне спрацювання» — це і робить статтю")
    print("емпіричною.\n")
    for r in sorted(data["rules"], key=lambda r: -r["hits"]):
        ex = data["samples"].get(r["code"])
        if not ex:
            continue
        print(f"— {r['title']} ({r['hits']} спрацювань)")
        for e in ex:
            sql = " ".join((e["sql"] or "").split())[:150]
            lab = f" · {e['lab']}" if e.get("lab") else ""
            print(f"    {e['at']:%d.%m %H:%M}{lab}\n      {sql}")
        print()


async def _main() -> int:
    from .db import close_pools, open_pools

    days = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    await open_pools()
    try:
        data = await corpus(days)
    finally:
        await close_pools()
    report(data)
    return 0 if data["queries"] else 1


if __name__ == "__main__":
    import asyncio
    sys.exit(asyncio.run(_main()))
