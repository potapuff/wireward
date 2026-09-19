"""Чи вгадує міра схожості лабораторну на РЕАЛЬНИХ запитах (З7).

Питання поставив викладач і відповіді на нього досі немає. Усі числа в
[ADR-019](../../docs/02-decisions.md) виміряні на зразках, складених автором, і
на згенерованому демо-журналі: вони показують, що міра поводиться як задумано,
і не показують, чи вгадує вона лабораторну на живих студентських запитах.
До цієї перевірки покриття на екрані лаби — **оцінка, а не факт**.

Скрипт нічого не вирішує сам. Він рахує те, з чого рішення видно:

**1. Розділення.** Для кожного відбитка з журналу береться найкращий зразок
кожної лаби. Далі порівнюються два розподіли — «своя лаба» проти «чужа». Якщо
вони перекриваються, порога, який працює, не існує в принципі, і жодне
підкручування числа не допоможе.

**2. Де стоїть поріг.** Скільки відбитків він відсікає і скільки пропускає.
Поріг, за яким лежить 2 % журналу, і поріг, за яким 80 %, ламають екран
по-різному, але обидва ламають.

**3. Що саме він відсікає.** Найближчі запити НИЖЧЕ порога, текстом. Це
єдиний спосіб побачити, що міра відкидає справжню роботу, а не сміття, — і
рівно те, чого не видно з жодного середнього.

Читати результат має людина. Автоматичного вердикту тут немає навмисно:
рішення «поріг такий» — викладацьке, і воно ухвалюється, дивлячись на запити.

**04.09.2026:** міра — `sqlshape2` (v2+idf), не голий `sqlshape`. Розвідка й
абляція на реальних звітах студентів — `docs/research/05-observer-formulation.md`:
на 15 лабах хибний прийом на порозі 0.5 впав з 49 % до 34 %, на 9 лабах однієї
схеми (найважчий випадок) — з 81 % до 49 %. Це не заміна цій перевірці: числа
там — з пулу «решта студентів», тут — із реальних `obs.lab_expectations`.
"""
from __future__ import annotations

import sys
from statistics import mean

from .sqlshape2 import WEIGHTS_V2, collect_df, shape, similarity as _similarity


def similarity(a: dict, b: dict, idf=None) -> float:
    return _similarity(a, b, WEIGHTS_V2, idf)

# Скільки різних відбитків брати. Той самий ліміт, що й у звіті по лабі:
# за ним іде довгий хвіст одноразових запитів, який картини не змінює.
FINGERPRINT_LIMIT = 4000

# Нижче цього числа розмова про розподіли безпредметна.
ENOUGH = 50


def _bar(v: float, width: int = 24) -> str:
    n = int(round(v * width))
    return "█" * n + "·" * (width - n)


def _dist(scores: list[float]) -> str:
    if not scores:
        return "немає"
    s = sorted(scores)
    p = lambda q: s[min(len(s) - 1, int(q * len(s)))]  # noqa: E731
    return (f"n={len(s):5d}  сер={mean(s):.2f}  "
            f"мед={p(0.5):.2f}  90%={p(0.9):.2f}  макс={s[-1]:.2f}")


async def run(limit: int = FINGERPRINT_LIMIT, show: int = 8) -> int:
    from .db import WORK_ONLY, fetchall

    labs = await fetchall("""
        SELECT l.id, l.title, l.threshold,
               coalesce((SELECT array_agg(g.group_name) FROM obs.lab_groups g
                          WHERE g.lab_id = l.id), ARRAY[]::text[])
          FROM obs.labs l
         WHERE l.archived_at IS NULL
           AND EXISTS (SELECT 1 FROM obs.lab_expectations e WHERE e.lab_id = l.id)
         ORDER BY l.id""")
    if not labs:
        print("жодної лабораторної зі зразками — спершу make seed-labs", file=sys.stderr)
        return 2

    samples: dict[int, list[dict]] = {}
    for lab_id, *_ in labs:
        rows = await fetchall(
            "SELECT sql_sample FROM obs.lab_expectations WHERE lab_id = %s", (lab_id,))
        samples[lab_id] = [shape(r[0]) for r in rows]
    idf = collect_df(samples, WEIGHTS_V2)

    groups = sorted({g for *_, gs in labs for g in gs})
    if not groups:
        print("у лабораторних не вказано жодної групи — звіт іде по групах",
              file=sys.stderr)
        return 2

    rows = await fetchall(f"""
        SELECT q.sql_fingerprint,
               (array_agg(q.sql_text ORDER BY q.started_at DESC))[1],
               count(*)
          FROM obs.queries q
         WHERE {WORK_ONLY}
           AND EXISTS (SELECT 1 FROM obs.student_groups g
                        WHERE g.student_id = q.student_id AND g.until IS NULL
                          AND g.group_name = ANY(%s))
         GROUP BY 1 ORDER BY 3 DESC LIMIT {int(limit)}
    """, (groups,))

    print(f"Групи: {', '.join(groups)}")
    print(f"Різних відбитків у журналі: {len(rows)}")
    if len(rows) < ENOUGH:
        print(f"\n⚠ Це замало. Нижче {ENOUGH} відбитків розподіли нічого не значать —")
        print("  перевірку треба повторити після першого-другого заняття.")
    print()

    # Для кожного відбитка — найкращий бал по кожній лабі.
    best: list[tuple[str, str, int, dict[int, float]]] = []
    for fp, text, n in rows:
        sh = shape(text)
        per = {lab_id: max((similarity(sh, s, idf) for s in samples[lab_id]), default=0.0)
               for lab_id in samples}
        best.append((fp, text, n, per))

    # --- 1. Розділення ------------------------------------------------------
    print("Розділення: своя лаба проти чужої")
    print("Відбиток приписується тій лабі, де його бал найвищий. «Чужа» — бали")
    print("того самого відбитка на решті лаб. Розподіли мають РОЗХОДИТИСЯ.")
    print()
    own: dict[int, list[float]] = {lab_id: [] for lab_id in samples}
    alien: dict[int, list[float]] = {lab_id: [] for lab_id in samples}
    for _fp, _t, _n, per in best:
        winner = max(per, key=lambda k: per[k])
        if per[winner] <= 0:
            continue
        own[winner].append(per[winner])
        for lab_id, sc in per.items():
            if lab_id != winner:
                alien[lab_id].append(sc)

    for lab_id, title, threshold, _gs in labs:
        print(f"  [{lab_id}] {title}   поріг {threshold}")
        print(f"      своя : {_dist(own[lab_id])}")
        print(f"      чужа : {_dist(alien[lab_id])}")
        if own[lab_id] and alien[lab_id]:
            gap = min(own[lab_id]) - max(alien[lab_id])
            if gap > 0:
                print(f"      розподіли НЕ перетинаються, проміжок {gap:.2f} — "
                      f"поріг у цьому проміжку розділяє чисто")
            else:
                overlap = sum(1 for a in alien[lab_id] if a >= threshold)
                print(f"      розподіли ПЕРЕТИНАЮТЬСЯ; чужих вище порога: {overlap}")
        print()

    # --- 2. Де стоїть поріг -------------------------------------------------
    print("Що відсікає поріг")
    for lab_id, title, threshold, _gs in labs:
        scores = [per[lab_id] for _f, _t, _n, per in best]
        above = sum(1 for s in scores if s >= threshold)
        queries_above = sum(n for _fp, _t, n, per in best if per[lab_id] >= threshold)
        total_q = sum(n for _f, _t, n, _p in best)
        share = above / len(scores) if scores else 0
        print(f"  [{lab_id}] {title}")
        print(f"      {_bar(share)}  відбитків вище порога: {above} з {len(scores)} "
              f"({share:.0%}), запитів: {queries_above} з {total_q}")
    print()

    # --- 3. Що саме відсікається -------------------------------------------
    print("Числа вище щось означають ЛИШЕ на справжньому студентському журналі.")
    print("На демо-даних (seed-demo.sh, seed-year.sh) вони показують, що скрипт")
    print("рахує, а не що міра працює — це різні твердження.")
    print()
    print(f"Найближчі НИЖЧЕ порога (по {show} на лабу) — дивитися очима:")
    print("це і є місце, де видно, чи відкидається справжня робота, чи сміття.")
    for lab_id, title, threshold, _gs in labs:
        near = sorted(((per[lab_id], t) for _fp, t, _n, per in best
                       if 0 < per[lab_id] < threshold), reverse=True)[:show]
        print(f"\n  [{lab_id}] {title}")
        for sc, text in near:
            one = " ".join(text.split())
            print(f"      {sc:.2f}  {one[:100]}")
    return 0


async def _main() -> int:
    from .db import close_pools, open_pools

    await open_pools()
    try:
        return await run()
    finally:
        await close_pools()


if __name__ == "__main__":
    import asyncio
    sys.exit(asyncio.run(_main()))
