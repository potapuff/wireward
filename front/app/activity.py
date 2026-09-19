"""Агрегати активності та дані для ключового екрана.

Чому агрегати, а не підрахунок на льоту: за семестр `obs.queries` виростає до
мільйонів рядків, і теплокарта день×година перетворилася б на повне сканування
при кожному відкритті сторінки. `obs.activity_buckets` — відра по 15 хвилин;
вони похідні й у разі втрати перебудовуються з журналу повністю.

Крок 15 хвилин обраний як компроміс: достатньо дрібно, щоб стрічка сесій і
теплокарта не брехали, і достатньо грубо, щоб семестр однієї групи вміщався в
десятки тисяч рядків замість мільйонів.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta

from .db import WORK_ONLY, execute, fetchall

log = logging.getLogger("observer.activity")

TZ = "Europe/Kyiv"
BUCKET = "15 minutes"

# Скільки послідовних невдач поспіль вважаємо «застряг».
STUCK_THRESHOLD = 5


# --------------------------------------------------------------- перебудова

async def refresh_buckets(hours: int = 3) -> None:
    """Перераховує відра за останні `hours` годин.

    `catalog_queries` тут завжди 0, і це навмисно: службові запити до
    Службовий трафік клієнта у відра НЕ входить (`WORK_ONLY`, ADR-018): він
    зберігається в журналі, але теплокарта має показувати роботу студента, а не
    те, скільки разів DataGrip спитав рівень ізоляції. Загальна кількість
    службових запитів лишається в `obs.sessions.catalog_queries` — для стрічки
    сесій цього достатньо.
    """
    await execute(f"""
        WITH src AS (
            SELECT student_id,
                   date_bin('{BUCKET}', started_at, TIMESTAMPTZ '2000-01-01') AS bucket,
                   status, sql_kind, sql_fingerprint, started_at
              FROM obs.queries
             WHERE started_at >= now() - interval '{hours} hours'
               AND {WORK_ONLY}
        ), agg AS (
            SELECT student_id, bucket,
                   count(*)                                       AS queries,
                   count(*) FILTER (WHERE status = 'error')       AS errors,
                   count(DISTINCT sql_fingerprint)                AS distinct_fingerprints,
                   -- Скільки різних секунд студент щось виконував. Груба, але
                   -- чесна міра «скільки часу справді працював»: пауза між
                   -- запитами в неї не потрапляє.
                   count(DISTINCT date_trunc('second', started_at)) AS active_seconds
              FROM src GROUP BY 1, 2
        ), kinds AS (
            SELECT student_id, bucket, jsonb_object_agg(sql_kind, n) AS kinds
              FROM (SELECT student_id, bucket, sql_kind, count(*) AS n
                      FROM src GROUP BY 1, 2, 3) t
             GROUP BY 1, 2
        )
        INSERT INTO obs.activity_buckets
              (student_id, bucket, queries, errors, catalog_queries,
               distinct_fingerprints, active_seconds, kinds)
        SELECT a.student_id, a.bucket, a.queries, a.errors, 0,
               a.distinct_fingerprints, a.active_seconds, coalesce(k.kinds, '{{}}'::jsonb)
          FROM agg a LEFT JOIN kinds k USING (student_id, bucket)
        ON CONFLICT (student_id, bucket) DO UPDATE
           SET queries               = EXCLUDED.queries,
               errors                = EXCLUDED.errors,
               distinct_fingerprints = EXCLUDED.distinct_fingerprints,
               active_seconds        = EXCLUDED.active_seconds,
               kinds                 = EXCLUDED.kinds
    """)


async def refresh_sticking(hours: int = 6) -> None:
    """Знаходить серії послідовних невдач з однаковим відбитком.

    «Застряг» — це не «зробив багато помилок», а «зробив ту саму помилку
    підряд і не рухався далі». Тому серія рветься будь-яким успіхом або будь-яким
    іншим запитом між спробами: класична задача про острови в потоці.
    """
    await execute(f"""
        DELETE FROM obs.sticking_points
         WHERE last_at >= now() - interval '{hours} hours'
    """)
    await execute(f"""
        WITH q AS (
            SELECT session_id, student_id, seq, sql_fingerprint, status,
                   sqlstate, error_message, sql_text, started_at
              FROM obs.queries
             WHERE started_at >= now() - interval '{hours} hours'
               AND {WORK_ONLY}
        ), marked AS (
            SELECT *,
                   CASE WHEN status = 'error'
                             AND lag(status)          OVER w = 'error'
                             AND lag(sql_fingerprint) OVER w = sql_fingerprint
                        THEN 0 ELSE 1 END AS is_new
              FROM q WINDOW w AS (PARTITION BY session_id ORDER BY seq)
        ), grouped AS (
            SELECT *, sum(is_new) OVER (PARTITION BY session_id ORDER BY seq
                                        ROWS UNBOUNDED PRECEDING) AS grp
              FROM marked
        ), runs AS (
            SELECT student_id, session_id, sql_fingerprint, grp,
                   count(*)                                    AS attempts,
                   min(started_at)                             AS first_at,
                   max(started_at)                             AS last_at,
                   max(sqlstate)                               AS sqlstate,
                   max(error_message)                          AS error_message,
                   (array_agg(sql_text ORDER BY seq DESC))[1]  AS sample_sql
              FROM grouped
             WHERE status = 'error'
             GROUP BY 1, 2, 3, 4
            HAVING count(*) >= {STUCK_THRESHOLD}
        )
        INSERT INTO obs.sticking_points
              (student_id, session_id, fingerprint, attempts, first_at, last_at,
               sqlstate, error_message, resolved, sample_sql)
        SELECT r.student_id, r.session_id, r.sql_fingerprint, r.attempts,
               r.first_at, r.last_at, r.sqlstate, r.error_message,
               -- «Застряг і розібрався» та «застряг і кинув» — різні історії,
               -- і саме це поле їх розрізняє.
               EXISTS (SELECT 1 FROM obs.queries s
                        WHERE s.session_id = r.session_id
                          AND s.sql_fingerprint = r.sql_fingerprint
                          AND s.status = 'ok'
                          AND s.started_at > r.last_at),
               r.sample_sql
          FROM runs r
    """)


async def background_loop(interval: int = 60) -> None:
    """Фонове оновлення агрегатів. Помилка тут не має валити застосунок:
    агрегати похідні, і найгірше, що станеться, — екран трохи відстане."""
    while True:
        try:
            await refresh_buckets(hours=3)
            await refresh_sticking(hours=6)
            # Кеш привʼязки «відбиток → лаба» живиться тим самим циклом, що й
            # відра: окремий планувальник заради ще однієї похідної таблиці
            # означав би два місця, де воно може тихо перестати оновлюватися.
            # Імпорт локальний — labs імпортує db, а не activity, і глобальний
            # імпорт замкнув би коло.
            from . import labs
            await labs.refresh_fingerprint_labs()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("оновлення агрегатів активності не вдалося")
        await asyncio.sleep(interval)


# Перебудова агрегатів триває довго (на реальному масштабі — десятки хвилин
# через CPU-скоринг у rebuild_fingerprint_labs()), і кнопка на екрані нічим не
# захищена від подвійного натискання. Без замка другий клік запустив би
# паралельний DELETE+INSERT по тих самих таблицях — марна робота і подвійне
# навантаження саме тоді, коли перше вже й так триває.
_rebuild_lock = asyncio.Lock()


async def rebuild_all() -> bool:
    """Повна перебудова з журналу. Довго, але агрегати завжди відновлювані.

    Повертає `False`, якщо перебудова вже йде (замок зайнятий) — виклик
    тоді нічого не робить, а не ставиться в чергу за другим проходом.
    """
    if _rebuild_lock.locked():
        return False
    async with _rebuild_lock:
        # DELETE, а не TRUNCATE: TRUNCATE вимагає володіння таблицею, а obs_app
        # навмисно має лише DML. Таблиця мала, різниця в швидкості несуттєва.
        await execute("DELETE FROM obs.activity_buckets")
        await refresh_buckets(hours=24 * 365 * 10)
        await execute("DELETE FROM obs.sticking_points")
        await refresh_sticking(hours=24 * 365 * 10)
        from . import labs
        await labs.rebuild_fingerprint_labs()
    return True


# ------------------------------------------------------------------ вибірки

def _student_clause(student: str | None, params: list,
                    allowed: list[str] | None = None) -> str:
    """`student` звужує до одного студента; якщо його немає — `allowed`
    (RBAC викладача, обчислений заздалегідь у admin.py зі списку груп)
    звужує до студентів дозволених груп. Обидва не задані одночасно ніде
    в застосунку: `student` іде лише коли доступ до нього вже перевірено."""
    if student:
        params.append(student)
        return " AND student_id = %s::uuid"
    if allowed is not None:
        params.append(allowed)
        return " AND student_id = ANY(%s::uuid[])"
    return ""


# Скільки днів ще читається порядково. За цією межею сітка день × година
# перестає бути картинкою: сто рядків не охоплюються оком, і замість «коли
# працювали» видно смугу. Тоді рядком стає тиждень.
HEATMAP_DAY_LIMIT = 35


async def heatmap(days: int, student: str | None, unit: str | None = None,
                  allowed: list[str] | None = None) -> dict:
    """Коли працювали: години в колонках, дні або тижні в рядках.

    Години лишаються колонками в обох режимах навмисно — питання «увечері чи
    вранці», «ніч перед здачею чи рівно» ставиться саме до годин, і зміна
    масштабу не має його забирати. Змінюється лише рядок: день або тиждень.

    `days=0` означає весь журнал; для нього рядок завжди тиждень.
    """
    params: list = []
    clause = _student_clause(student, params, allowed)
    span = "" if days == 0 else f" AND bucket >= now() - interval '{int(days)} days'"
    if unit is None:
        unit = "week" if (days == 0 or days > HEATMAP_DAY_LIMIT) else "day"
    row_expr = (f"date_trunc('week', bucket AT TIME ZONE '{TZ}')::date"
                if unit == "week" else f"(bucket AT TIME ZONE '{TZ}')::date")

    rows = await fetchall(f"""
        SELECT {row_expr}                                                AS d,
               extract(hour FROM bucket AT TIME ZONE '{TZ}')::int        AS h,
               sum(queries)::int, sum(errors)::int, sum(active_seconds)::int
          FROM obs.activity_buckets
         WHERE true{span}{clause}
         GROUP BY 1, 2
    """, tuple(params))

    cells = {(r[0], r[1]): {"queries": r[2], "errors": r[3], "secs": r[4]} for r in rows}
    if unit == "week":
        # Вісь будується з даних, а не з календаря: у порожні тижні (канікули,
        # пауза між лабами) рядка просто немає, і це чесніше, ніж смуга нулів.
        axis = sorted({d for d, _h in cells})
    else:
        today = datetime.now().date()
        axis = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]

    peak = max((c["queries"] for c in cells.values()), default=0)
    return {"days": axis, "cells": cells, "peak": peak, "unit": unit,
            "total": sum(c["queries"] for c in cells.values()),
            "errors": sum(c["errors"] for c in cells.values())}


async def error_curve(days: int, student: str | None,
                      allowed: list[str] | None = None) -> list[dict]:
    """Частка невдалих запитів у часі. Спадна крива в межах заняття —
    це буквально видиме навчання."""
    params: list = []
    clause = _student_clause(student, params, allowed)
    step = "1 hour" if days <= 2 else "1 day"
    rows = await fetchall(f"""
        SELECT date_bin('{step}', bucket, TIMESTAMPTZ '2000-01-01') AS t,
               sum(queries)::int, sum(errors)::int
          FROM obs.activity_buckets
         WHERE bucket >= now() - interval '{days} days'{clause}
         GROUP BY 1 ORDER BY 1
    """, tuple(params))
    return [{"at": r[0], "queries": r[1], "errors": r[2],
             "pct": round(r[2] * 100 / r[1]) if r[1] else 0} for r in rows]


async def sessions_timeline(days: int, student: str) -> list[dict]:
    """Стрічка сесій із засічками помилок.

    Читається як «три години з паузами», а не «417 запитів» — саме тому вона
    показується лише для одного студента: усереднена по групі вона беззмістовна.
    """
    rows = await fetchall(f"""
        SELECT s.id::text, s.started_at, coalesce(s.ended_at, now()) AS ended_at,
               s.queries_total, s.errors_total, s.catalog_queries,
               s.application_name, s.listen_port,
               coalesce((SELECT array_agg(q.started_at ORDER BY q.started_at)
                           FROM obs.queries q
                          WHERE q.session_id = s.id AND q.status = 'error'
                            AND {WORK_ONLY}), '{{}}')
          FROM obs.sessions s
         WHERE s.student_id = %s::uuid
           AND s.started_at >= now() - interval '{days} days'
         ORDER BY s.started_at
    """, (student,))
    keys = ("id", "started_at", "ended_at", "queries", "errors", "catalog",
            "app", "port", "error_times")
    return [dict(zip(keys, r)) for r in rows]


async def sticking(days: int, student: str | None, limit: int = 40,
                   allowed: list[str] | None = None) -> list[dict]:
    params: list = []
    clause = ""
    if student:
        params.append(student)
        clause = " AND sp.student_id = %s::uuid"
    elif allowed is not None:
        params.append(allowed)
        clause = " AND sp.student_id = ANY(%s::uuid[])"
    rows = await fetchall(f"""
        SELECT sp.attempts, sp.first_at, sp.last_at, sp.sqlstate, sp.error_message,
               sp.resolved, sp.sample_sql, sp.student_id::text, st.display_name
          FROM obs.sticking_points sp
          JOIN obs.students st ON st.id = sp.student_id
         WHERE sp.last_at >= now() - interval '{days} days'{clause}
         ORDER BY sp.attempts DESC, sp.last_at DESC
         LIMIT {limit}
    """, tuple(params))
    keys = ("attempts", "first_at", "last_at", "sqlstate", "error_message",
            "resolved", "sample_sql", "student_id", "display_name")
    return [dict(zip(keys, r)) for r in rows]


async def totals(days: int, student: str | None,
                 allowed: list[str] | None = None) -> dict:
    params: list = []
    clause = _student_clause(student, params, allowed)
    row = await fetchall(f"""
        SELECT coalesce(sum(queries), 0)::int,
               coalesce(sum(errors), 0)::int,
               coalesce(sum(active_seconds), 0)::int,
               count(DISTINCT (bucket AT TIME ZONE '{TZ}')::date)::int
          FROM obs.activity_buckets
         WHERE bucket >= now() - interval '{days} days'{clause}
    """, tuple(params))
    q, e, secs, active_days = row[0] if row else (0, 0, 0, 0)
    return {"queries": q, "errors": e, "active_seconds": secs,
            "active_days": active_days,
            "error_pct": round(e * 100 / q) if q else 0}
