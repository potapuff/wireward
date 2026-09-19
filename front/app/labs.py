"""Лабораторні: зіставлення журналу зі зразками викладача (З4).

Постановка від викладача, і кожне слово тут визначає рішення:

* лабораторна **не обмежена часом** — тому в моделі немає жодної дати;
* студенти приходять у різні моменти і **повертаються** — тому робота над
  лабою це не відрізок, а множина запитів, розкиданих як завгодно;
* запити **варіативні** — тому збіг нечіткий, за формою (`sqlshape2`), а не за
  текстом чи відбитком. `sqlshape2` — компоненти Aligon (предикати, зʼєднання,
  проєкція, group-by) з резолвінгом псевдонімів і зваженим Жаккаром з IDF по
  активних лабах (docs/research/05); ваги наживо реагують на появу нової лаби.

## Як рахується

Зіставляються не окремі запити, а **відбитки** (`sql_fingerprint`): у сесії
студента 2000 запитів, але різних відбитків — сотні, і форма в межах відбитка
одна. Це і дає прийнятну ціну: зіставити треба сотні форм, а не тисячі рядків.

Далі для кожного відбитка береться найкращий зі зразків лаби. Якщо схожість
вища за поріг лаби — усі запити з цим відбитком вважаються роботою над нею.

## Що показується і чому саме це

**Покриття** — скільки зразків лаби студент так чи інакше відтворив. Це і є
той «великий, хоч і не точний перетин»: не «розвʼязав правильно», а «дійшов до
цієї задачі».

**Заборонені конструкції** — окремо, бо це протилежний за смислом сигнал:
студент задачу зробив, але не тим способом, який відпрацьовувався.

**Схожість показується числом.** Міра добре відділяє одну лабу від іншої, але
розрізняє окремі задачі ВСЕРЕДИНІ лаби лише приблизно — задачі однієї лаби
читають ті самі таблиці. Тому останнє слово лишається за викладачем, і для
цього він має бачити не вердикт, а число та сам запит.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from .db import WORK_ONLY, execute, fetchall, fetchone
from .sqlshape import CONSTRUCT_TITLES, constructs
from .sqlshape2 import WEIGHTS_V2, collect_df
from .sqlshape2 import shape, similarity as _similarity
from .temporal_context import BOT as _BOT, smooth_sequence

# v2+idf замість голого v1 (docs/research/05): компоненти Aligon +
# резолвінг псевдонімів + зважений Жаккар з IDF по лабах. IDF рахується
# наживо з поточного набору активних лаб при кожному звіті — жодного
# заморожування; дає інший бал сьогодні й після того, як зʼявиться нова
# лаба. Для чогось задокументованого заднім числом це не годиться (на
# відміну від замороженого `obs.indicators`); для панелі викладача — саме
# те, що треба.


def similarity(a: dict, b: dict, idf: dict | None = None) -> float:
    return _similarity(a, b, WEIGHTS_V2, idf)


async def _idf_across_active_labs() -> dict[str, dict[str, float]]:
    labs = await fetchall("""
        SELECT l.id FROM obs.labs l
         WHERE l.archived_at IS NULL
           AND EXISTS (SELECT 1 FROM obs.lab_expectations e WHERE e.lab_id = l.id)""")
    pools: dict[int, list[dict]] = {}
    for (lab_id,) in labs:
        rows = await fetchall(
            "SELECT sql_sample FROM obs.lab_expectations WHERE lab_id = %s", (lab_id,))
        pools[lab_id] = [shape(r[0]) for r in rows]
    return collect_df(pools, WEIGHTS_V2)

# Скільки різних відбитків беремо на одну лабу. Ліміт свідомий: за ним іде
# довгий хвіст одноразових запитів, який картину вже не змінює.
FINGERPRINT_LIMIT = 4000


async def _labs_for_groups(lab_ids: list[int], student_id: str | None) -> set[int] | None:
    """Звужує кандидатів привʼязки до груп ОДНОГО студента (З12, docs/07-roadmap.md).

    Без цього пошук найближчого зразка йде по всіх лабораторних системи —
    з двома схожими лабами різних груп/курсів це та сама плутанина, що й
    "сусідні лабораторні однієї схеми" (З7). Тут звуження за групами, там —
    за формою запиту; правляться незалежно.

    Консервативно в обидва боки — краще залишок старої поведінки, ніж хибна
    відмова в матчингу:
    * немає `student_id`, або в студента немає жодної поточної групи —
      звуження не застосовується (`None` — «кандидатів усі»);
    * лабораторна не привʼязана до жодної групи (в схемі це дозволено) —
      вона лишається кандидатом для будь-кого.
    """
    if not student_id or not lab_ids:
        return None
    row = await fetchone(
        "SELECT groups FROM obs.student_current_groups WHERE student_id = %s::uuid",
        (student_id,))
    my_groups = set(row[0]) if row and row[0] else set()
    if not my_groups:
        return None
    rows = await fetchall(
        "SELECT lab_id, array_agg(group_name) FROM obs.lab_groups"
        " WHERE lab_id = ANY(%s) GROUP BY lab_id", (lab_ids,))
    lab_groups = {lab_id: set(gs) for lab_id, gs in rows}
    return {lid for lid in lab_ids
            if not lab_groups.get(lid) or (lab_groups[lid] & my_groups)}


async def lab_list(allowed: list[str] | None = None) -> list[dict]:
    """`allowed`, якщо не None, звужує до лаб груп викладача (RBAC) — та сама
    умова, що і в labs_screen/_labs_for_groups: лаба без груп — спільна для
    всіх, лаба з групами видима, якщо перетинається з `allowed`.
    """
    rows = await fetchall("""
        SELECT l.id, l.title, l.note, l.threshold, l.archived_at,
               coalesce((SELECT array_agg(group_name ORDER BY group_name)
                           FROM obs.lab_groups g WHERE g.lab_id = l.id), ARRAY[]::text[]),
               (SELECT count(*) FROM obs.lab_expectations e WHERE e.lab_id = l.id),
               (SELECT count(*) FROM obs.lab_forbidden f WHERE f.lab_id = l.id)
          FROM obs.labs l ORDER BY l.archived_at NULLS FIRST, l.id
    """)
    keys = ("id", "title", "note", "threshold", "archived_at",
            "groups", "expectations", "forbidden")
    labs = [dict(zip(keys, r)) for r in rows]
    if allowed is not None:
        allowed_set = set(allowed)
        labs = [l for l in labs if not l["groups"] or (set(l["groups"]) & allowed_set)]
    return labs


async def lab_one(lab_id: int) -> dict | None:
    row = await fetchone("""
        SELECT id, title, note, threshold, archived_at FROM obs.labs WHERE id = %s
    """, (lab_id,))
    if row is None:
        return None
    lab = dict(zip(("id", "title", "note", "threshold", "archived_at"), row))
    lab["groups"] = [r[0] for r in await fetchall(
        "SELECT group_name FROM obs.lab_groups WHERE lab_id = %s ORDER BY 1", (lab_id,))]
    lab["expectations"] = [dict(zip(("id", "title", "sql_sample"), r)) for r in await fetchall(
        "SELECT id, title, sql_sample FROM obs.lab_expectations WHERE lab_id = %s ORDER BY id",
        (lab_id,))]
    lab["forbidden"] = [r[0] for r in await fetchall(
        "SELECT construct FROM obs.lab_forbidden WHERE lab_id = %s ORDER BY 1", (lab_id,))]
    return lab


async def create_lab(title: str, note: str, groups: list[str], threshold: float,
                     samples: list[str], forbidden: list[str]) -> int:
    row = await fetchone(
        "INSERT INTO obs.labs (title, note, threshold) VALUES (%s,%s,%s) RETURNING id",
        (title, note or None, threshold))
    lab_id = row[0]
    for g in groups:
        await execute("INSERT INTO obs.lab_groups (lab_id, group_name) VALUES (%s,%s)"
                      " ON CONFLICT DO NOTHING", (lab_id, g))
    for s in samples:
        await execute("INSERT INTO obs.lab_expectations (lab_id, sql_sample) VALUES (%s,%s)",
                      (lab_id, s))
    for c in forbidden:
        if c in CONSTRUCT_TITLES:
            await execute("INSERT INTO obs.lab_forbidden (lab_id, construct) VALUES (%s,%s)"
                          " ON CONFLICT DO NOTHING", (lab_id, c))
    return lab_id


async def report(lab_id: int, scope_groups: list[str] | None = None) -> dict | None:
    """Хто з групи лаби що зробив. Часових меж немає — беремо весь журнал.

    `scope_groups`, якщо переданий (не None), звужує і кандидатів, і подальші
    вибірки до перетину груп лаби з групами викладача (RBAC) — інакше
    викладач однієї з кількох груп лаби бачить студентів і сирий SQL чужої
    групи, теж призначеної цій лабі. `None` означає «без звуження» (адмін).
    """
    lab = await lab_one(lab_id)
    if lab is None:
        return None
    groups = lab["groups"] if scope_groups is None else scope_groups
    if not lab["expectations"] or not groups:
        return {"lab": lab, "students": [], "unmatched": [], "incomplete": True}

    # Кандидати: різні відбитки студентів із груп лаби (звужених до RBAC-
    # scope, якщо він заданий). Один зразок тексту на відбиток — форма в
    # межах відбитка та сама.
    rows = await fetchall(f"""
        SELECT q.sql_fingerprint,
               (array_agg(q.sql_text ORDER BY q.started_at DESC))[1] AS sample,
               count(*) AS n
          FROM obs.queries q
         WHERE {WORK_ONLY}
           AND EXISTS (SELECT 1 FROM obs.student_groups g
                        WHERE g.student_id = q.student_id AND g.until IS NULL
                          AND g.group_name = ANY(%s))
         GROUP BY 1 ORDER BY n DESC LIMIT {FINGERPRINT_LIMIT}
    """, (groups,))

    samples = [(e["id"], e["sql_sample"]) for e in lab["expectations"]]
    shapes = [(eid, shape(s)) for eid, s in samples]
    idf = await _idf_across_active_labs()

    matched: dict[str, dict] = {}     # відбиток -> {expectation_id, score, forbidden}
    near: list[dict] = []             # найкращі з тих, що не дотягнули
    for fp, sample, _n in rows:
        sh = shape(sample)
        best_id, best = None, 0.0
        for eid, esh in shapes:
            sc = similarity(sh, esh, idf)
            if sc > best:
                best_id, best = eid, sc
        if best >= lab["threshold"]:
            matched[fp] = {"expectation_id": best_id, "score": best,
                           "sample": sample,
                           "forbidden": sorted(constructs(sample) & set(lab["forbidden"]))}
        elif best > 0:
            near.append({"score": best, "sample": sample, "expectation_id": best_id})

    near.sort(key=lambda r: -r["score"])

    if not matched:
        return {"lab": lab, "students": [], "unmatched": near[:20], "incomplete": False}

    # Скільки запитів кожного відбитка в кого — уже в базі, а не в памʼяті.
    per = await fetchall(f"""
        SELECT q.student_id::text, s.display_name, q.sql_fingerprint,
               count(*), count(*) FILTER (WHERE q.status='error'),
               min(q.started_at), max(q.started_at)
          FROM obs.queries q JOIN obs.students s ON s.id = q.student_id
         WHERE {WORK_ONLY} AND q.sql_fingerprint = ANY(%s)
           AND EXISTS (SELECT 1 FROM obs.student_groups g
                        WHERE g.student_id = q.student_id AND g.until IS NULL
                          AND g.group_name = ANY(%s))
         GROUP BY 1, 2, 3
    """, (list(matched), groups))

    students: dict[str, dict] = {}
    for sid, name, fp, n, errs, first_at, last_at in per:
        m = matched[fp]
        st = students.setdefault(sid, {
            "id": sid, "name": name, "queries": 0, "errors": 0,
            "covered": set(), "forbidden": {}, "first_at": first_at, "last_at": last_at})
        st["queries"] += n
        st["errors"] += errs
        st["covered"].add(m["expectation_id"])
        st["first_at"] = min(st["first_at"], first_at)
        st["last_at"] = max(st["last_at"], last_at)
        for c in m["forbidden"]:
            st["forbidden"].setdefault(c, {"title": CONSTRUCT_TITLES.get(c, c),
                                           "sample": m["sample"], "n": 0})
            st["forbidden"][c]["n"] += n

    total = len(lab["expectations"])
    out = []
    for st in students.values():
        st["coverage"] = round(100 * len(st["covered"]) / total)
        st["missing"] = [e for e in lab["expectations"] if e["id"] not in st["covered"]]
        st["forbidden"] = list(st["forbidden"].values())
        out.append(st)
    out.sort(key=lambda s: (-s["coverage"], -s["queries"]))
    return {"lab": lab, "students": out, "unmatched": near[:20], "incomplete": False}


async def for_student(student_id: str) -> list[dict]:
    """Зворотний зріз: які лабораторні видно в журналі ЦЬОГО студента.

    Звіт по лабі відповідає на питання «хто з групи що зробив». На картці
    студента потрібне протилежне — «що зробив цей», і рахувати його треба тим
    самим способом, інакше два екрани показуватимуть різні числа про одне й те
    саме.

    Тому тут не окрема логіка, а той самий `report()`, звужений до одного
    студента. Ціна — звіт рахується по всіх лабах студентових груп; на
    десятках лаб це помітно, на чотирьох — ні.
    """
    rows = await fetchall("""
        SELECT l.id FROM obs.labs l
         WHERE l.archived_at IS NULL
           AND EXISTS (SELECT 1 FROM obs.lab_expectations e WHERE e.lab_id = l.id)
           AND EXISTS (SELECT 1 FROM obs.lab_groups g
                        JOIN obs.student_groups sg ON sg.group_name = g.group_name
                       WHERE g.lab_id = l.id AND sg.student_id = %s::uuid
                         AND sg.until IS NULL)
         ORDER BY l.id
    """, (student_id,))
    out = []
    for (lab_id,) in rows:
        data = await report(lab_id)
        if not data or data.get("incomplete"):
            continue
        mine = next((s for s in data["students"] if s["id"] == str(student_id)), None)
        out.append({
            "lab_id": lab_id, "title": data["lab"]["title"],
            "threshold": data["lab"]["threshold"],
            "expectations": len(data["lab"]["expectations"]),
            "coverage": mine["coverage"] if mine else 0,
            "queries": mine["queries"] if mine else 0,
            "errors": mine["errors"] if mine else 0,
            "first_at": mine["first_at"] if mine else None,
            "last_at": mine["last_at"] if mine else None,
            "missing": mine["missing"] if mine else data["lab"]["expectations"],
            "forbidden": mine["forbidden"] if mine else [],
        })
    return out


def report_csv(data: dict) -> str:
    """Звіт по лабі як CSV. Потрібен не для краси: оцінки зводяться в таблиці,
    і переносити числа з екрана руками — це помилки на 160 одиницях."""
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    titles = {e["id"]: (e["title"] or f"зразок {e['id']}")
              for e in data["lab"]["expectations"]}
    w.writerow(["студент", "покриття_%", "зразків_усього", "відтворено",
                "запитів", "помилок", "перша_спроба", "остання_спроба",
                "не_відтворено", "заборонені_конструкції"])
    for st in data["students"]:
        w.writerow([
            st["name"], st["coverage"], len(titles),
            len(titles) - len(st["missing"]),
            st["queries"], st["errors"],
            st["first_at"].isoformat(sep=" ", timespec="seconds") if st["first_at"] else "",
            st["last_at"].isoformat(sep=" ", timespec="seconds") if st["last_at"] else "",
            "; ".join(titles.get(e["id"], "") for e in st["missing"]),
            "; ".join(f["title"] for f in st["forbidden"]),
        ])
    return buf.getvalue()


async def fingerprint_map(student_id: str | None = None,
                          cohort_only: bool = False) -> dict[str, int]:
    """Відбиток → лабораторна, порахований один раз на весь виклик.

    Той самий нечіткий збіг, що й у `report()` (ADR-019), але з протилежного
    боку: там питання «хто з групи зробив цю лабу», тут — «до якої лаби
    належить цей запит». Логіка мусить бути та сама, інакше екран лаби й
    показники по лабі почнуть розходитися в числах про одне й те саме.
    """
    labs = await fetchall("""
        SELECT l.id, l.threshold FROM obs.labs l
         WHERE l.archived_at IS NULL
           AND EXISTS (SELECT 1 FROM obs.lab_expectations e WHERE e.lab_id = l.id)""")
    if not labs:
        return {}
    # Звуження до груп студента (З12) — має сенс лише тоді, коли відбитки
    # заздалегідь відомо чиї, тобто для одного студента. Пакетні виклики
    # (без student_id, `cohort_only`) лишаються по всіх лабах: один відбиток
    # там може належати студентам різних груп одразу.
    allowed = await _labs_for_groups([lab_id for lab_id, _ in labs],
                                     None if cohort_only else student_id)
    if allowed is not None:
        labs = [(lab_id, threshold) for lab_id, threshold in labs if lab_id in allowed]
        if not labs:
            return {}
    samples: dict[int, tuple[float, list]] = {}
    for lab_id, threshold in labs:
        rows = await fetchall(
            "SELECT sql_sample FROM obs.lab_expectations WHERE lab_id = %s", (lab_id,))
        samples[lab_id] = (threshold, [shape(r[0]) for r in rows])
    idf = collect_df({lab_id: shs for lab_id, (_t, shs) in samples.items()}, WEIGHTS_V2)

    where, params = [WORK_ONLY], []
    if student_id:
        where.append("q.student_id = %s::uuid")
        params.append(student_id)
    if cohort_only:
        where.append("q.student_id IN (SELECT student_id FROM obs.research_cohort)")
    fps = await fetchall(f"""
        SELECT q.sql_fingerprint, (array_agg(q.sql_text ORDER BY q.started_at DESC))[1]
          FROM obs.queries q
         WHERE {" AND ".join(where)}
         GROUP BY 1 LIMIT {FINGERPRINT_LIMIT}
    """, tuple(params))

    out: dict[str, int] = {}
    for fp, text in fps:
        sh = shape(text)
        best_id, best = None, 0.0
        for lab_id, (threshold, shapes) in samples.items():
            sc = max((similarity(sh, s, idf) for s in shapes), default=0.0)
            if sc > best and sc >= threshold:
                best_id, best = lab_id, sc
        if best_id is not None:
            out[fp] = best_id
    return out


# ------------------------------------------------- кеш привʼязки для треймапа

# Скільки годин журналу перевіряти на нові відбитки за один прохід фонового
# циклу, коли кеш свіжий. Той самий порядок, що й у
# `activity.refresh_buckets(hours=3)`: цикл крутиться щохвилини, тож вікна в
# кілька годин вистачає із запасом навіть після короткого простою front.
#
# Але базове вікно рахується не від `now()`, а від `max(matched_at)` кешу
# (дивись `refresh_fingerprint_labs`): простій на вихідних чи довша зупинка
# front означали б, що запити за час простою старші за фіксоване вікно від
# `now()` і НІКОЛИ в кеш не потраплять, якщо студент їх не повторив. Вікно від
# останнього оновлення кешу природно розширюється на весь пропущений час.
CACHE_WINDOW_HOURS = 6

# Дефолт, коли кеш порожній (перший запуск, або щойно `rebuild_fingerprint_labs()`
# видалив усе): "від now() мінус 6 годин" тут не годиться так само — беремо
# розумний запас замість того, щоб мовчки не побачити нічого, що сталося раніше.
EMPTY_CACHE_WINDOW_DAYS = 30

# Скільки нових пар (студент, відбиток) зіставляти за один прохід. Обмеження
# не через памʼять, а через час: зіставлення — це розбір SQL і десятки
# порівнянь форм на кожну пару, і фоновий цикл не має стояти в ньому хвилинами
# після seed-у чи першого запуску. Залишок дійде наступним проходом.
CACHE_BATCH = 500


def _score_pairs(pairs: list[tuple[str, str, str]],
                 samples: dict[int, tuple[float, list]],
                 allowed_by_student: dict[str, set[int] | None],
                 idf_by_student: dict[str, dict]) -> list[tuple]:
    """Чистий CPU-скоринг без жодного `await` (розбір SQL + Жаккар по всіх
    зразках усіх лаб на кожен відбиток) — винесений окремо від
    `_match_fingerprints`, щоб його можна було віддати в `asyncio.to_thread`
    і не тримати event loop заблокованим на весь прохід перебудови."""
    out: list[tuple] = []
    for sid, fp, text in pairs:
        allowed = allowed_by_student.get(sid)
        idf = idf_by_student.get(sid, {})
        sh = shape(text)
        best_id, best = None, 0.0
        for lab_id, (threshold, shapes) in samples.items():
            if allowed is not None and lab_id not in allowed:
                continue
            sc = max((similarity(sh, s, idf) for s in shapes), default=0.0)
            if sc > best and sc >= threshold:
                best_id, best = lab_id, sc
        # Промах теж записується (lab_id = NULL): інакше довгий хвіст
        # одноразових запитів зіставлявся б наново щохвилини, вічно.
        out.append((sid, fp, best_id, best if best_id is not None else None))
    return out


async def _match_fingerprints(pairs: list[tuple[str, str, str]]) -> list[tuple]:
    """Зіставляє пари (student_id, fingerprint, sample_sql) з лабораторними.

    Повертає рядки для `obs.fingerprint_labs`. Логіка та сама, що в
    `fingerprint_map()`: найкращий зразок серед лаб, звужених до груп
    студента (З12), поріг свій у кожної лаби, і, головне, IDF рахується
    ПІСЛЯ цього звуження — по пулу лаб конкретного студента, а не по всіх
    активних лабах системи. Порядок важливий: `similarity()` з IDF, порахованим
    по ширшому пулу, дає інше число для того самого відбитка, ніж по вузькому —
    і розходження між кешем і екраном лаби було б найгіршим варіантом: два
    екрани про ту саму роботу з різними числами.

    Підготовка (запити до бази — звуження за групами, IDF) лишається тут,
    async; сам скоринг (чистий Python, без `await`) віддається в
    `asyncio.to_thread`, щоб не блокувати event loop на весь прохід
    (`rebuild_fingerprint_labs()` на реальному масштабі — десятки хвилин).
    """
    labs = await fetchall("""
        SELECT l.id, l.threshold FROM obs.labs l
         WHERE l.archived_at IS NULL
           AND EXISTS (SELECT 1 FROM obs.lab_expectations e WHERE e.lab_id = l.id)""")
    if not labs:
        return []
    samples: dict[int, tuple[float, list]] = {}
    for lab_id, threshold in labs:
        rows = await fetchall(
            "SELECT sql_sample FROM obs.lab_expectations WHERE lab_id = %s", (lab_id,))
        samples[lab_id] = (threshold, [shape(r[0]) for r in rows])
    all_ids = [lab_id for lab_id, _ in labs]

    # Звуження за групами і IDF по звуженому пулу рахуються РАЗ НА СТУДЕНТА,
    # а не раз на відбиток: у пакеті на 500 пар студентів одиниці, а інакше
    # обидва рахунки повторювалися б по разу на кожен відбиток.
    allowed_by_student: dict[str, set[int] | None] = {}
    idf_by_student: dict[str, dict] = {}
    for sid in {p[0] for p in pairs}:
        allowed = await _labs_for_groups(all_ids, sid)
        allowed_by_student[sid] = allowed
        pool = ({lab_id: shs for lab_id, (_t, shs) in samples.items()}
                if allowed is None else
                {lab_id: shs for lab_id, (_t, shs) in samples.items()
                 if lab_id in allowed})
        idf_by_student[sid] = collect_df(pool, WEIGHTS_V2)

    return await asyncio.to_thread(
        _score_pairs, pairs, samples, allowed_by_student, idf_by_student)


async def refresh_fingerprint_labs(hours: int | None = None,
                                   limit: int = CACHE_BATCH) -> int:
    """Досипає в `obs.fingerprint_labs` пари, яких там ще немає.

    Інкрементально й лише вперед: перезіставляти вже пораховане щохвилини
    немає сенсу — форма запиту в межах відбитка не змінюється. Що змінюється —
    це набір лабораторних; на цей випадок є `rebuild_fingerprint_labs()`, і
    саме його треба кликати після правки лаби чи її зразків.

    `hours=None` (типовий виклик із фонового циклу) — вікно рахується не від
    `now()`, а від `max(matched_at)` уже наявного кешу: `now() - CACHE_WINDOW_HOURS`
    для звичайного проходу дав би дірку назавжди, якби front простояв довше за
    це вікно (вихідні, збій) — запити за час простою старші за нього і без
    повторного виконання студентом у кеш ніколи б не потрапили. Відлік від
    останнього оновлення кешу природно розширюється на весь пропущений час.
    Порожній кеш (перший запуск чи щойно після `rebuild_fingerprint_labs()`) —
    `EMPTY_CACHE_WINDOW_DAYS` замість `now()`.

    `hours=<число>` (явний виклик, зокрема з `rebuild_fingerprint_labs()`) —
    старий фіксований відлік від `now()`, без звертання до кешу: під час
    повної перебудови кеш щойно очищено, і "від останнього matched_at" там
    означало б "нічого не бачити".

    Повертає кількість доданих пар.
    """
    # Відлік лишається в SQL (не рахується в Python над значенням із бази):
    # `matched_at`/`started_at` — timestamptz, і порівнювати їх із наївним
    # `datetime.now()` Python-боку означало б тихо переплутати часові пояси.
    if hours is None:
        since_expr = f"""coalesce(
            (SELECT max(matched_at) FROM obs.fingerprint_labs) - interval '{CACHE_WINDOW_HOURS} hours',
            now() - interval '{EMPTY_CACHE_WINDOW_DAYS} days')"""
    else:
        since_expr = f"now() - interval '{hours} hours'"
    rows = await fetchall(f"""
        SELECT q.student_id::text, q.sql_fingerprint,
               (array_agg(q.sql_text ORDER BY q.started_at DESC))[1]
          FROM obs.queries q
         WHERE q.started_at >= {since_expr}
           AND {WORK_ONLY}
           AND NOT EXISTS (SELECT 1 FROM obs.fingerprint_labs fl
                            WHERE fl.student_id = q.student_id
                              AND fl.sql_fingerprint = q.sql_fingerprint)
         GROUP BY 1, 2
         LIMIT {limit}
    """)
    if not rows:
        return 0
    matched = await _match_fingerprints([(r[0], r[1], r[2]) for r in rows])
    for sid, fp, lab_id, score in matched:
        await execute("""
            INSERT INTO obs.fingerprint_labs
                  (student_id, sql_fingerprint, lab_id, score, matched_at)
            VALUES (%s::uuid, %s, %s, %s, now())
            ON CONFLICT (student_id, sql_fingerprint) DO UPDATE
               SET lab_id = EXCLUDED.lab_id,
                   score = EXCLUDED.score,
                   matched_at = EXCLUDED.matched_at
        """, (sid, fp, lab_id, score))
    return len(matched)


async def rebuild_fingerprint_labs() -> int:
    """Повна перебудова кешу з журналу.

    Потрібна після будь-якої зміни, що впливає на зіставлення: нова лаба,
    правка зразків, зміна порога, зміна групи студента. Кеш похідний, тому
    найпростіший коректний шлях — викинути і зібрати наново.
    """
    # DELETE, а не TRUNCATE: obs_app навмисно має лише DML, не володіння
    # таблицею. Те саме міркування, що в activity.rebuild_all().
    await execute("DELETE FROM obs.fingerprint_labs")
    total = 0
    while True:
        n = await refresh_fingerprint_labs(hours=24 * 365 * 10, limit=CACHE_BATCH)
        total += n
        if n < CACHE_BATCH:
            break
    return total


async def treemap(days: int = 7, group: str | None = None) -> list[dict]:
    """Розподіл активності по лабораторних за останні `days` днів (З13/З14).

    `group=None` — уся система (адмін-панель, З14); назва групи — одна група
    (екран групи, З13). Рядок на (студент, лабораторна): розмір плитки —
    `queries`/`active_seconds`, колір — `error_rate`. День у розтині
    лишається агрегованим тут, бо треймап показує тиждень цілим; сирі дні
    доступні через `obs.lab_activity_daily()` напряму, якщо знадобиться
    динаміка всередині вікна.
    """
    rows = await fetchall("""
        SELECT a.student_id::text, s.display_name, a.lab_id, l.title,
               sum(a.queries)::bigint, sum(a.errors)::bigint,
               sum(a.active_seconds)::bigint, max(a.last_at),
               count(DISTINCT a.day)::int
          FROM obs.lab_activity_daily(now() - make_interval(days => %s), %s) a
          JOIN obs.students s ON s.id = a.student_id
          JOIN obs.labs l     ON l.id = a.lab_id
         GROUP BY 1, 2, 3, 4
         ORDER BY 5 DESC
    """, (days, group))
    out = []
    for sid, name, lab_id, title, queries, errors, secs, last_at, active_days in rows:
        out.append({
            "student_id": sid, "student": name,
            "lab_id": lab_id, "lab": title,
            "queries": int(queries), "errors": int(errors),
            "active_seconds": int(secs), "active_days": active_days,
            "last_at": last_at,
            # Частка помилок — саме частка, а не кількість: плитка на 200
            # запитів із 20 помилками і плитка на 20 із 20 — різні історії,
            # і колір мусить розрізняти їх, а не розмір.
            "error_rate": (errors / queries) if queries else 0.0,
        })
    return out


async def treemap_by_lab(days: int = 7, group: str | None = None) -> list[dict]:
    """Агрегує `treemap()` по лабораторній (З13/З14).

    `treemap()` дає рядок на (студент, лабораторна) — забагато плиток, щоб
    прочитати «на якій лабі найбільше часу» одним поглядом, навіть на одну
    групу. Тому плитка тут — лабораторна, розмір і колір рахуються сумою по
    ній; найактивніший студент не губиться, а лишається в підписі плитки
    (`top_student`), а не стає окремою плиткою.
    """
    rows = await treemap(days, group)
    by_lab: dict[int, dict] = {}
    for r in rows:
        item = by_lab.setdefault(r["lab_id"], {
            "lab_id": r["lab_id"], "title": r["lab"],
            "queries": 0, "errors": 0, "active_seconds": 0,
            "students": 0, "top_student": None, "top_queries": -1,
        })
        item["queries"] += r["queries"]
        item["errors"] += r["errors"]
        item["active_seconds"] += r["active_seconds"]
        item["students"] += 1
        if r["queries"] > item["top_queries"]:
            item["top_queries"] = r["queries"]
            item["top_student"] = r["student"]
    out = list(by_lab.values())
    for it in out:
        it["error_rate"] = (it["errors"] / it["queries"]) if it["queries"] else 0.0
    out.sort(key=lambda x: -x["queries"])
    return out


# Скільки годин після останньої спроби вважати «тим самим заходом». Це не
# поріг оцінювання, а лише спосіб сказати «працює над двома одразу»: студент,
# який увечері робив третю лабу, а вранці четверту, робить їх упереміш, і
# показувати лише одну з них означає збрехати про половину роботи.
PARALLEL_HOURS = 36


def where_now(rows: list[dict], now=None) -> dict | None:
    """Де студент зараз — над чим працював востаннє і що там ще не закрито.

    Меж у лабораторних немає (ADR-019), тому питання «чи здав вчасно» не має
    сенсу взагалі. Але саме через відсутність меж з'являється питання, на яке
    журнал відповідає, а відомість — ні: **на чому студент стоїть цієї
    хвилини**. Викладачеві на консультації потрібне саме це, а не покриття всіх
    чотирьох лаб таблицею.

    Вердикту тут немає навмисно. «Давно не заходив» — це і хвороба, і канікули,
    і робота в іншому клієнті; число днів викладач прочитає сам.

    Приймає готові рядки `for_student()`, щоб екран лаби, картка студента й
    кабінет рахували це однаково, а не троє по-своєму.
    """
    seen = [r for r in rows if r.get("last_at")]
    if not seen:
        return None
    seen.sort(key=lambda r: r["last_at"], reverse=True)
    head = seen[0]

    # Паралельні — ті, до яких студент повертався поруч у часі з останньою.
    edge = head["last_at"] - timedelta(hours=PARALLEL_HOURS)
    also = [r for r in seen[1:] if r["last_at"] >= edge]

    now = now or datetime.now(head["last_at"].tzinfo)
    total = head["expectations"] or 0
    done = total - len(head["missing"])
    return {
        "lab_id": head["lab_id"], "title": head["title"],
        "coverage": head["coverage"], "done": done, "total": total,
        "missing": head["missing"], "queries": head["queries"],
        "errors": head["errors"],
        "last_at": head["last_at"], "first_at": head["first_at"],
        "idle_hours": max(0.0, (now - head["last_at"]).total_seconds() / 3600),
        "also": [{"lab_id": r["lab_id"], "title": r["title"],
                  "coverage": r["coverage"]} for r in also],
    }


async def positions() -> dict[str, dict]:
    """Те саме, але на всю групу одразу — для списку студентів.

    `for_student()` на кожного студента означав би `report()` × студентів ×
    лаб; тут `report()` рахується по разу на лабу, а далі розкладається по
    студентах. Результат мусить збігатися з карткою студента рядок у рядок,
    тому інтерпретація одна й та сама — `where_now()`.
    """
    labs = await fetchall("""
        SELECT l.id FROM obs.labs l
         WHERE l.archived_at IS NULL
           AND EXISTS (SELECT 1 FROM obs.lab_expectations e WHERE e.lab_id = l.id)
         ORDER BY l.id""")
    per_student: dict[str, list[dict]] = {}
    for (lab_id,) in labs:
        data = await report(lab_id)
        if not data or data.get("incomplete"):
            continue
        total = len(data["lab"]["expectations"])
        for st in data["students"]:
            per_student.setdefault(st["id"], []).append({
                "lab_id": lab_id, "title": data["lab"]["title"],
                "expectations": total, "coverage": st["coverage"],
                "queries": st["queries"], "errors": st["errors"],
                "first_at": st["first_at"], "last_at": st["last_at"],
                "missing": st["missing"],
            })
    return {sid: p for sid, rows in per_student.items()
            if (p := where_now(rows)) is not None}


# ------------------------------------------------------------- часовий контекст

# Персистентність стану в самопетлі HMM (docs/article-p4-research/07-10):
# студент здебільшого лишається на тій самій лабі від запиту до запиту, а не
# перескакує щоразу. Значення — з синтетичного бенчмарку (10, "Рекомендація
# для Етапу 2": F1 0.49→0.72 без калібрування на реальних даних живого
# журналу, бо їх для цього ще недостатньо, 09 «Етап 2»). Іменований параметр
# навмисно, не число в тілі виклику — щоб дослідницьке походження лишалося
# видимим на місці використання.
TEMPORAL_P_STAY = 0.85


async def temporal_context(rows: list[dict], student_id: str | None = None,
                           p_stay: float = TEMPORAL_P_STAY) -> dict[int, str | None]:
    """Згладжена контекстом мітка лаби для впорядкованої стрічки запитів
    ОДНОГО студента (docs/article-p4-research/07-10-temporal-context-*.md).

    ДОДАТКОВИЙ шар, не заміна `report()`/`fingerprint_map()`: ті рахують
    приналежність до лаби по ВІДБИТКУ (найкращий зразок, поріг лаби) і
    лишаються джерелом правди для покриття. Тут — та сама міра
    (`sqlshape2.similarity`), але по КОЖНОМУ запиту окремо і згладжена
    `smooth_sequence()` (regime-switching Viterbi, самопетля `p_stay`) —
    бо per-query рішення саме по собі шумне (07 п. 3), а студент працює
    епізодами, повертаючись до попередніх лаб, а не хаотично (07 п. 2/5).

    `rows` — рядки з ключами `id`, `sql_text`, `started_at`, той самий
    формат, що повертає `admin.recent_queries()` (не обов'язково
    впорядковані — сортуються тут). `student_id`, якщо переданий, звужує
    лабораторні-кандидати до груп цього студента (З12) — той самий фільтр,
    що й у `fingerprint_map()`. Повертає `{id: назва_лаби | None}` —
    `None` означає ⊥ («жодна лаба», нижче середнього порогу активних лаб).
    Порожній вхід або відсутність активних лаб зі зразками → порожній
    словник (виклику з екрана нема чим доповнити).
    """
    if not rows:
        return {}
    lab_rows = await fetchall("""
        SELECT l.id, l.title, l.threshold FROM obs.labs l
         WHERE l.archived_at IS NULL
           AND EXISTS (SELECT 1 FROM obs.lab_expectations e WHERE e.lab_id = l.id)
         ORDER BY l.id""")
    if not lab_rows:
        return {}
    allowed = await _labs_for_groups([r[0] for r in lab_rows], student_id)
    if allowed is not None:
        lab_rows = [r for r in lab_rows if r[0] in allowed]
        if not lab_rows:
            return {}
    lab_ids = [r[0] for r in lab_rows]
    titles = {r[0]: r[1] for r in lab_rows}
    thresholds = [r[2] for r in lab_rows]

    shapes: dict[int, list[dict]] = {}
    for lab_id in lab_ids:
        srows = await fetchall(
            "SELECT sql_sample FROM obs.lab_expectations WHERE lab_id = %s", (lab_id,))
        shapes[lab_id] = [shape(s) for (s,) in srows]
    idf = collect_df(shapes, WEIGHTS_V2)

    ordered = sorted(rows, key=lambda r: r["started_at"])
    vectors = [
        {lab_id: max((similarity(shape(r["sql_text"]), s, idf) for s in shapes[lab_id]),
                     default=0.0)
         for lab_id in lab_ids}
        for r in ordered
    ]
    # Один орієнтир на всі лаби для емісії ⊥ у смузі HMM: пороги лаб різні
    # (`obs.labs.threshold`), а `smooth_sequence()` очікує одне число — це
    # спрощення для допоміжного шару, не перегляд порогів самого `report()`.
    t_lo = sum(thresholds) / len(thresholds)
    path = smooth_sequence(vectors, lab_ids, p_stay=p_stay, t_lo=t_lo)
    return {r["id"]: (titles.get(lab) if lab is not _BOT else None)
            for r, lab in zip(ordered, path)}
