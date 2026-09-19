"""Адмінка.

Чотири екрани, як і задумувалося: студенти, останні запити, запити по студенту,
події баз. Екран «Активність» — фаза 5.

Одне правило проходить крізь усе: журнал показує **активність**, а не
**авторство**. Один вставлений блок SQL, що виконався з першого разу, у даних
виглядає як робота дуже сильного студента. Тому інтерфейс ніде не виводить
оцінок і не ранжує студентів «за якістю» — лише показує, як ішла робота.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from . import activity, indicators, maintenance, quota, ratelimit, surveys
from .db import WORK_ONLY, execute, fetchall, fetchone
from .provisioning import log_event
from .security import csrf_ok, read_session
from .labs import (create_lab, for_student as labs_for_student, lab_list,
                   lab_one, positions as lab_positions, report, report_csv,
                   temporal_context, treemap_by_lab, where_now)
from .rules import TITLES, check
from .sqlshape import CONSTRUCT_TITLES

log = logging.getLogger("observer.admin")

router = APIRouter(prefix="/admin")


def require_csrf(request: Request, submitted: str | None) -> None:
    """CSRF на мутаціях адмінки — так само, як на студентських.

    `SameSite=Lax` на сесійній куці й так не дає браузеру надіслати її з чужим
    POST, тож це другий шар. Але саме тут він і потрібен: адмін-мутації дорожчі
    за студентські (створення лабораторної, виставлення оцінки), а покладатися
    на один механізм у місці, де ціна помилки найвища, — погана угода.
    """
    if not csrf_ok(read_session(request), submitted):
        raise HTTPException(status_code=400, detail="CSRF")

PERIODS = {"1": "1 день", "7": "7 днів", "30": "30 днів", "0": "увесь час"}


def _interval(days: str) -> str:
    """Межа періоду як SQL-вираз. '0' означає «увесь час»."""
    return "'-infinity'::timestamptz" if days == "0" else f"now() - interval '{int(days)} days'"


# --------------------------------------------------------------- 1. Студенти

async def students_rows(days: str, group: str | None = None,
                        allowed: list[str] | None = None) -> list[dict]:
    """Лічильники беруться з obs.activity_buckets, а не з журналу.

    Це не оптимізація заради оптимізації. Пряма агрегація по obs.queries
    сканувала журнал ОДИН РАЗ НА СТУДЕНТА: при 2.9 млн записів і 48 студентах
    сторінка «за весь час» відкривалася 3.7 секунди. Відра містять рівно ті
    самі суми, важать 8 МБ замість 1.8 ГБ і читаються за індексом.

    Ціна — відставання до хвилини (відра оновлює фоновий job). Для списку
    студентів це непомітно; там, де потрібна точність до запиту, і далі
    читається сам журнал.

    `group`, якщо задано, звужує список до ПОТОЧНИХ членів групи (drill-down
    з екрана «Групи») — той самий предикат, що і фільтр «Останніх запитів».

    `allowed`, якщо не None, звужує список до груп викладача (RBAC): список
    може бути порожнім (викладач без призначених груп), тоді результат
    порожній, а не «всі студенти».
    """
    since = _interval(days)
    conds, params = [], []
    if group:
        conds.append("EXISTS (SELECT 1 FROM obs.student_groups sg"
                     " WHERE sg.student_id = s.id AND sg.until IS NULL"
                     "   AND sg.group_name = %s)")
        params.append(group)
    if allowed is not None:
        conds.append("EXISTS (SELECT 1 FROM obs.student_groups sg"
                     " WHERE sg.student_id = s.id AND sg.until IS NULL"
                     "   AND sg.group_name = ANY(%s::text[]))")
        params.append(allowed)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    # Розмір слота теж кешований (ts_slots.used_bytes): pg_tablespace_size —
    # системний виклик на кожен рядок, і на 150 слотах це відчутно.
    rows = await fetchall(f"""
        SELECT s.id, s.display_name, s.email,
               coalesce(g.groups, ARRAY[]::text[]) AS groups, s.db_name,
               s.is_admin, s.disabled_at, s.last_seen_at, s.consented_at,
               ts.tablespace_name, ts.quota_bytes, ts.used_bytes,
               coalesce(b.total, 0), coalesce(b.errors, 0),
               b.last_bucket, coalesce(se.sessions, 0)
          FROM obs.students s
          LEFT JOIN obs.ts_slots ts ON ts.slot = s.slot
          LEFT JOIN obs.student_current_groups g ON g.student_id = s.id
          LEFT JOIN LATERAL (
               SELECT sum(queries)::bigint AS total,
                      sum(errors)::bigint  AS errors,
                      max(bucket)          AS last_bucket
                 FROM obs.activity_buckets
                WHERE student_id = s.id AND bucket > {since}
          ) b ON true
          LEFT JOIN LATERAL (
               SELECT count(*) AS sessions
                 FROM obs.sessions
                WHERE student_id = s.id AND started_at > {since}
          ) se ON true
         {where}
         ORDER BY b.last_bucket DESC NULLS LAST, s.display_name
    """, params)
    keys = ("id", "display_name", "email", "groups", "db_name", "is_admin",
            "disabled_at", "last_seen_at", "consented_at", "tablespace_name",
            "quota_bytes", "used_bytes", "total", "errors", "last_query_at", "sessions")
    return [dict(zip(keys, r)) for r in rows]


# --------------------------------------------------------------- 1б. Групи

async def groups_rows(days: str, allowed: list[str] | None = None) -> list[dict]:
    """Групи як одиниця огляду (З11): кількість студентів, скільки з них
    вимкнено, і сумарна активність з тих самих відер, що й «Студенти» —
    жодного додаткового проходу по obs.queries.

    `allowed`, якщо не None, звужує перелік до груп викладача (RBAC).
    """
    since = _interval(days)
    clause, params = "", ()
    if allowed is not None:
        clause = " AND sg.group_name = ANY(%s::text[])"
        params = (allowed,)
    rows = await fetchall(f"""
        SELECT sg.group_name,
               count(DISTINCT s.id)::int AS students,
               count(DISTINCT s.id) FILTER (WHERE s.disabled_at IS NOT NULL)::int AS disabled,
               coalesce(sum(b.total), 0)::bigint AS total,
               coalesce(sum(b.errors), 0)::bigint AS errors,
               max(b.last_bucket) AS last_bucket
          FROM obs.student_groups sg
          JOIN obs.students s ON s.id = sg.student_id
          LEFT JOIN LATERAL (
               SELECT sum(queries)::bigint AS total,
                      sum(errors)::bigint  AS errors,
                      max(bucket)          AS last_bucket
                 FROM obs.activity_buckets
                WHERE student_id = s.id AND bucket > {since}
          ) b ON true
         WHERE sg.until IS NULL{clause}
         GROUP BY sg.group_name
         ORDER BY sg.group_name
    """, params)
    keys = ("name", "students", "disabled", "total", "errors", "last_bucket")
    return [dict(zip(keys, r)) for r in rows]


async def sparklines(days: int = 14) -> dict[str, list[int]]:
    """Активність по днях для спарклайна в рядку таблиці. З відер, не з журналу."""
    rows = await fetchall(f"""
        SELECT student_id::text,
               (bucket AT TIME ZONE 'Europe/Kyiv')::date AS d,
               sum(queries)::int
          FROM obs.activity_buckets
         WHERE bucket > now() - interval '{days} days'
         GROUP BY 1, 2
    """)
    today = datetime.now().date()
    axis = [(today - timedelta(days=i)) for i in range(days - 1, -1, -1)]
    idx = {d: i for i, d in enumerate(axis)}
    out: dict[str, list[int]] = {}
    for sid, d, n in rows:
        out.setdefault(sid, [0] * days)
        if d in idx:
            out[sid][idx[d]] = n
    return out


def _sized(items: list[dict], key: str) -> list[dict]:
    """Домальовує `size_pct` — частку від найбільшого значення набору.

    Треймап у шаблонах — суто CSS (flexbox, без бібліотек): `size_pct` іде в
    `flex-grow` плитки, тож площа виходить пропорційною метриці. Мінімум 4 —
    щоб студент чи лаба з малою активністю не перетворилися на непомітну
    смужку поряд із найактивнішими.
    """
    mx = max((it.get(key) or 0) for it in items) if items else 0
    for it in items:
        it["size_pct"] = round((it.get(key) or 0) / mx * 100, 1) if mx else 4
        if it["size_pct"] < 4:
            it["size_pct"] = 4
    return items


# --------------------------------------------------------- 2. Останні запити

QUERY_FIELDS = """
    q.id, q.started_at, q.duration_ms, q.status, q.sqlstate, q.error_message,
    q.sql_text, q.sql_kind, q.protocol, q.rows_affected, q.params,
    q.student_id::text, s.display_name,
    (q.is_catalog OR q.is_service) AS is_service
"""
QUERY_KEYS = ("id", "started_at", "duration_ms", "status", "sqlstate", "error_message",
              "sql_text", "sql_kind", "protocol", "rows_affected", "params",
              "student_id", "display_name", "is_service")


def _filters(errors_only: bool, kind: str, group: str, student: str | None,
             service: bool = False, allowed: list[str] | None = None) -> tuple[str, list]:
    where, params = [], []
    if not service:
        # Службові запити ховаються, а не викидаються: галочка «показати
        # службові» повертає їх на екран. Помилки видно завжди.
        where.append("(q.status = 'error' OR NOT (q.is_catalog OR q.is_service))")
    if errors_only:
        where.append("q.status = 'error'")
    if kind:
        params.append(kind)
        where.append("q.sql_kind = %s")
    if group:
        params.append(group)
        # Членство має період, тож фільтр — по ПОТОЧНИХ групах.
        where.append("EXISTS (SELECT 1 FROM obs.student_groups g"
                     " WHERE g.student_id = s.id AND g.until IS NULL"
                     "   AND g.group_name = %s)")
    if student:
        params.append(student)
        where.append("q.student_id = %s::uuid")
    if allowed is not None:
        params.append(allowed)
        where.append("EXISTS (SELECT 1 FROM obs.student_groups g"
                     " WHERE g.student_id = s.id AND g.until IS NULL"
                     "   AND g.group_name = ANY(%s::text[]))")
    return (" AND " + " AND ".join(where) if where else ""), params


async def recent_queries(days: str, errors_only: bool, kind: str, group: str,
                         student: str | None, after_id: int | None = None,
                         limit: int = 120, service: bool = False,
                         allowed: list[str] | None = None) -> list[dict]:
    clause, params = _filters(errors_only, kind, group, student, service, allowed)
    cursor = ""
    if after_id is not None:
        params.append(after_id)
        cursor = " AND q.id > %s"
        order = "ASC"
    else:
        order = "DESC"
    rows = await fetchall(f"""
        SELECT {QUERY_FIELDS}
          FROM obs.queries q JOIN obs.students s ON s.id = q.student_id
         WHERE q.started_at > {_interval(days)}{clause}{cursor}
         ORDER BY q.id {order}
         LIMIT {limit}
    """, tuple(params))
    # Порядок навмисно різний. Для сторінки — від нового до старого, як її й
    # читають. Для потоку — від старого до нового: клієнт додає рядки зверху,
    # тож зворотний порядок вивернув би стрічку навиворіт.
    return [dict(zip(QUERY_KEYS, r)) for r in rows]


# ------------------------------------------------------------------ маршрути

def register(app, render, current_student):
    """Реєструє маршрути. render і current_student приходять із main.py,
    щоб не тягнути звідти імпорт по колу."""

    async def require_admin(request: Request) -> dict:
        student = await current_student(request)
        # Права перевіряються на кожному запиті, а не лише при рендері меню.
        # 404, а не 403: стороннім не варто підтверджувати, що адмінка існує.
        if not student or not student["is_admin"]:
            raise HTTPException(status_code=404)
        # Для нав-меню admin/base.html (request завжди в контексті рендеру
        # Jinja2Templates, окремо тягнути актора у кожен render() не треба).
        request.state.is_admin = True
        request.state.is_teacher = bool(student.get("is_teacher"))
        return student

    async def require_staff(request: Request) -> tuple[dict, list[str] | None]:
        """Адмін або викладач. Повертає (student, allowed_groups).

        `is_admin` перевіряється ПЕРШИМ і незалежно від `is_teacher`: обидва
        прапорці можуть бути істинними разом, і адміністратор не має власних
        рядків у `obs.teacher_groups` — якби перевірку почали з `is_teacher`,
        такий адмін побачив би порожній екран замість усіх студентів.
        `allowed_groups=None` означає «без фільтра» (усе бачить); порожній
        список — законний стан викладача без призначених груп, екран просто
        нічого не покаже, а не 404.
        """
        student = await current_student(request)
        if not student:
            raise HTTPException(status_code=404)
        request.state.is_admin = bool(student["is_admin"])
        request.state.is_teacher = bool(student.get("is_teacher"))
        if student["is_admin"]:
            return student, None
        if student.get("is_teacher"):
            rows = await fetchall(
                "SELECT group_name FROM obs.teacher_groups WHERE teacher_id = %s::uuid",
                (student["id"],))
            return student, [r[0] for r in rows]
        raise HTTPException(status_code=404)

    async def gaps() -> list[dict]:
        """Проміжки, коли журнал свідомо неповний. Показуються на КОЖНОМУ
        екрані: графік із дірою, поданий як суцільний, гірший за відсутність
        графіка."""
        rows = await fetchall("""
            SELECT started_at, ended_at, reason, details
              FROM obs.journal_gaps
             WHERE ended_at IS NULL OR ended_at > now() - interval '7 days'
             ORDER BY started_at DESC LIMIT 5
        """)
        return [dict(zip(("started_at", "ended_at", "reason", "details"), r)) for r in rows]

    @router.get("", response_class=HTMLResponse)
    @router.get("/", response_class=HTMLResponse)
    async def index_screen(request: Request):
        """Корінь адмінки веде на групи (З11): це те місце, звідки викладач
        природно починає — не поіменний список, а перелік груп."""
        await require_staff(request)
        return RedirectResponse("/admin/groups")

    @router.get("/groups", response_class=HTMLResponse)
    async def groups_screen(request: Request, days: str = "7"):
        """Головний вхід в адмінку (З11): групи, а не поіменний список,
        як одиниця, з якою викладач насправді працює."""
        _, allowed = await require_staff(request)
        if days not in PERIODS:
            days = "7"
        return render(request, "admin/groups.html",
                      rows=await groups_rows(days, allowed), days=days, periods=PERIODS,
                      gaps=await gaps(), nav="groups")

    @router.get("/overview", response_class=HTMLResponse)
    async def overview_screen(request: Request):
        """Загальносистемний огляд (З14): те саме, що на екрані групи, лише по
        всіх студентах одразу — без входу в деталі кожного. Той самий агрегат
        (`treemap_by_lab`), той самий тип плитки, лише `group=None`."""
        await require_admin(request)
        return render(request, "admin/overview.html",
                      activity_tiles=_sized(await treemap_by_lab(7, None), "queries"),
                      disk_tiles=_sized(await quota.disk_usage(), "used_bytes"),
                      gaps=await gaps(), nav="overview")

    @router.get("/students", response_class=HTMLResponse)
    async def students_screen(request: Request, days: str = "7"):
        _, allowed = await require_staff(request)
        if days not in PERIODS:
            days = "7"
        rows = await students_rows(days, allowed=allowed)
        client_clause, client_params = "", ()
        if allowed is not None:
            client_clause = (" AND EXISTS (SELECT 1 FROM obs.student_groups g"
                             " WHERE g.student_id = sessions.student_id AND g.until IS NULL"
                             "   AND g.group_name = ANY(%s::text[]))")
            client_params = (allowed,)
        clients = [dict(zip(("name", "version", "sessions", "guessed"), r))
                   for r in await fetchall(f"""
            SELECT coalesce(client_name, 'не представився'),
                   string_agg(DISTINCT client_version, ', '),
                   count(*)::int, bool_or(client_guessed)
              FROM obs.sessions
             WHERE started_at > {_interval(days)}{client_clause}
             GROUP BY 1 ORDER BY 3 DESC""", client_params)]
        return render(request, "admin/students.html",
                      rows=rows, spark=await sparklines(), days=days, clients=clients,
                      # Один прохід report() на лабу, а не на студента:
                      # інакше список коштував би стільки ж, скільки екран лаби
                      # помножений на групу.
                      here=await lab_positions(),
                      sus=await surveys.summary(),
                      periods=PERIODS, gaps=await gaps(), nav="students")

    @router.get("/group/{name:path}", response_class=HTMLResponse)
    async def group_screen(request: Request, name: str, days: str = "7"):
        """Drill-down з переліку груп: ті самі студенти, що на екрані
        «Студенти», але звужені до цієї групи — і посилання на «Останні
        запити»/«Запити студента» вже несуть фільтр group=."""
        _, allowed = await require_staff(request)
        if not name:
            # `{name:path}` матчить і порожній рядок (URL /admin/group/) —
            # `students_rows(group='')` тоді хибний у `if group:` і фільтр не
            # застосовується, показуючи ВСІХ студентів як одну "групу".
            raise HTTPException(status_code=404)
        if allowed is not None and name not in allowed:
            raise HTTPException(status_code=404)
        if days not in PERIODS:
            days = "7"
        rows = await students_rows(days, group=name)
        if not rows:
            # Група могла існувати колись (until не NULL) або взагалі не
            # існувати — обидва випадки викладачеві варто побачити як 404,
            # а не як порожню таблицю без пояснення.
            known = await fetchone(
                "SELECT 1 FROM obs.student_groups WHERE group_name = %s LIMIT 1", (name,))
            if not known:
                raise HTTPException(status_code=404)
        return render(request, "admin/group.html",
                      group=name, rows=rows, spark=await sparklines(), days=days,
                      periods=PERIODS, here=await lab_positions(),
                      # Treemap за тиждень (З13б) — той самий тип, що на дашборді
                      # (nav="overview"), лише звужений до цієї групи.
                      tiles=_sized(await treemap_by_lab(7, name), "queries"),
                      gaps=await gaps(), nav="groups")

    @router.get("/queries", response_class=HTMLResponse)
    async def queries_screen(request: Request, days: str = "1", errors: int = 0,
                             kind: str = "", group: str = "", service: int = 0):
        _, allowed = await require_staff(request)
        if days not in PERIODS:
            days = "1"
        if allowed is not None and group and group not in allowed:
            raise HTTPException(status_code=404)
        rows = await recent_queries(days, bool(errors), kind, group, None,
                                    service=bool(service), allowed=allowed)
        groups_clause, groups_params = "", ()
        if allowed is not None:
            groups_clause = " AND group_name = ANY(%s::text[])"
            groups_params = (allowed,)
        groups = [r[0] for r in await fetchall(
            "SELECT DISTINCT group_name FROM obs.student_groups"
            f" WHERE until IS NULL{groups_clause} ORDER BY 1", groups_params)]
        if rows:
            last_id = rows[0]["id"]
        else:
            # Порожня сторінка (типово для короткого/нетипового періоду чи
            # групи, що сьогодні не працювала) не означає «нічого не
            # писалося взагалі»: `after=0` тут змусив би перший-таки тик
            # потоку віддати ВЕСЬ scope як щойно з'явлений (queries.html
            # позначає такі рядки class="fresh"). Курсор — max(id) у межах
            # ТОГО САМОГО scope, що й сам потік (той самий фільтр, вікно
            # potoку — 7 днів, а не поточний `days` сторінки).
            clause, cparams = _filters(bool(errors), kind, group, None,
                                       bool(service), allowed)
            max_row = await fetchone(f"""
                SELECT max(q.id) FROM obs.queries q JOIN obs.students s ON s.id = q.student_id
                 WHERE q.started_at > {_interval('7')}{clause}""", cparams)
            last_id = (max_row[0] if max_row and max_row[0] else 0)
        return render(request, "admin/queries.html",
                      rows=rows, days=days, periods=PERIODS, errors=errors,
                      kind=kind, group=group, groups=groups, service=service,
                      gaps=await gaps(), nav="queries",
                      last_id=last_id)

    async def _refresh_allowed(student_id) -> list[str] | None:
        """Перечитує права викладача так само, як require_staff().

        Потрібно для довгих SSE-зʼєднань (queries_stream): `allowed`,
        обчислений один раз при відкритті потоку, інакше лишається чинним
        годинами — і якщо адмін забирає групу чи саму роль викладача під час
        відкритого потоку, стрічка й далі віддає чужі запити."""
        row = await fetchone(
            "SELECT is_admin, is_teacher FROM obs.students WHERE id = %s::uuid",
            (student_id,))
        if not row:
            return []
        is_admin, is_teacher = row
        if is_admin:
            return None
        if not is_teacher:
            return []
        rows = await fetchall(
            "SELECT group_name FROM obs.teacher_groups WHERE teacher_id = %s::uuid",
            (student_id,))
        return [r[0] for r in rows]

    @router.get("/queries/stream")
    async def queries_stream(request: Request, after: int = 0, errors: int = 0,
                             kind: str = "", group: str = "", service: int = 0):
        """Живий потік нових запитів. Опитування раз на дві секунди —
        для 40 студентів цього більш ніж достатньо, а LISTEN/NOTIFY тут
        додав би залежність заради непомітного виграшу."""
        admin, allowed = await require_staff(request)
        if allowed is not None and group and group not in allowed:
            raise HTTPException(status_code=404)

        async def gen():
            last = after
            allowed_live = allowed
            tick = 0
            while True:
                if await request.is_disconnected():
                    return
                # Раз на ~30 тіків (60 с при кроці 2 с), а не на кожен —
                # дешевий SELECT, але не варто його й на кожну секунду вікна
                # довіри: перечитуємо права живого зʼєднання, а не довіряємо
                # тим, що були на момент відкриття потоку.
                if tick % 30 == 0:
                    allowed_live = await _refresh_allowed(admin["id"])
                    if allowed_live is not None and group and group not in allowed_live:
                        return
                tick += 1
                rows = await recent_queries("7", bool(errors), kind, group, None,
                                            after_id=last, limit=50,
                                            service=bool(service), allowed=allowed_live)
                for r in rows:
                    last = max(last, r["id"])
                    yield "data: " + json.dumps({
                        "id": r["id"],
                        "at": r["started_at"].strftime("%H:%M:%S"),
                        "student": r["display_name"] or "—",
                        "student_id": r["student_id"],
                        "status": r["status"],
                        "sqlstate": r["sqlstate"] or "",
                        "error": r["error_message"] or "",
                        "kind": r["sql_kind"],
                        "protocol": r["protocol"],
                        "ms": float(r["duration_ms"] or 0),
                        "rows": r["rows_affected"],
                        "sql": r["sql_text"][:400],
                        "is_service": r["is_service"],
                    }, ensure_ascii=False) + "\n\n"
                await asyncio.sleep(2)

        return StreamingResponse(gen(), media_type="text/event-stream; charset=utf-8",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    @router.get("/student/{student_id}", response_class=HTMLResponse)
    async def student_screen(request: Request, student_id: UUID,
                             days: str = "30", errors: int = 0, service: int = 0,
                             quota_error: str = "", quota_saved: str = "",
                             vacuum_error: str = "", vacuum_ms: str = ""):
        _, allowed = await require_staff(request)
        if days not in PERIODS:
            days = "30"
        sid = str(student_id)

        row = await fetchone("""
            SELECT s.id, s.display_name, s.email,
                   coalesce(g.groups, ARRAY[]::text[]) AS groups,
                   s.db_name, s.role_name, s.created_at, s.last_seen_at, s.consented_at, s.disabled_at,
                   ts.tablespace_name, ts.quota_bytes,
                   CASE WHEN ts.tablespace_name IS NOT NULL
                        THEN pg_tablespace_size(ts.tablespace_name) END
              FROM obs.students s
              LEFT JOIN obs.ts_slots ts ON ts.slot = s.slot
              LEFT JOIN obs.student_current_groups g ON g.student_id = s.id
             WHERE s.id = %s::uuid""", (sid,))
        if not row:
            raise HTTPException(status_code=404)
        student = dict(zip(("id", "display_name", "email", "groups", "db_name",
                            "role_name", "created_at", "last_seen_at", "consented_at",
                            "disabled_at", "tablespace_name", "quota_bytes", "used_bytes"), row))
        if allowed is not None and not (set(student["groups"]) & set(allowed)):
            # Викладач бачить лише студентів своїх груп — і членство, і
            # призначення викладача мають лише ПОТОЧний стан (без until/since).
            raise HTTPException(status_code=404)
        since = _interval(days)
        stats = await fetchone(f"""
            SELECT count(*), count(*) FILTER (WHERE status='error'),
                   count(DISTINCT session_id), coalesce(sum(duration_ms),0),
                   min(started_at), max(started_at)
              FROM obs.queries
             WHERE student_id = %s::uuid AND started_at > {since}
               AND {WORK_ONLY}""", (sid,))

        # Групування за відбитком — ядро розбору: «N спроб одного й того самого
        # запиту, і всі N упали» читається інакше, ніж N окремих помилок.
        stuck = await fetchall(f"""
            SELECT sql_fingerprint, count(*) AS attempts,
                   count(*) FILTER (WHERE status='error') AS failed,
                   max(sqlstate), max(error_message),
                   min(started_at), max(started_at),
                   (array_agg(sql_text ORDER BY started_at DESC))[1]
              FROM obs.queries
             WHERE student_id = %s::uuid AND started_at > {since}
               AND {WORK_ONLY}
             GROUP BY sql_fingerprint
            HAVING count(*) FILTER (WHERE status='error') >= 3
             ORDER BY failed DESC, attempts DESC LIMIT 20""", (sid,))
        stuck_keys = ("fingerprint", "attempts", "failed", "sqlstate", "error_message",
                      "first_at", "last_at", "sample")
        stuck = [dict(zip(stuck_keys, r)) for r in stuck]

        # Помилки, яких СУБД не помітила (З5). Рахуються тут, а не в проксі:
        # будь-яка робота в його гарячому шляху — ризик для журналу.
        # Ліміт свідомий: правила ілюструють, а не звітують, і 2000 останніх
        # запитів дають ту саму картину, не сканувавши весь семестр.
        suspects = await fetchall(f"""
            SELECT sql_text, rows_affected, started_at
              FROM obs.queries
             WHERE student_id = %s::uuid AND started_at > {since}
               AND {WORK_ONLY}
             ORDER BY started_at DESC LIMIT 2000""", (sid,))
        found: dict[str, dict] = {}
        for sql_text, rows_affected, at in suspects:
            for code in check(sql_text, rows_affected):
                f = found.setdefault(code, {
                    "code": code, "title": TITLES[code][0],
                    "explanation": TITLES[code][1],
                    "count": 0, "sample": sql_text, "last_at": at})
                f["count"] += 1
        suspicions = sorted(found.values(), key=lambda f: -f["count"])

        sessions = await fetchall(f"""
            SELECT started_at, ended_at, listen_port, application_name,
                   queries_total, errors_total, catalog_queries, end_reason,
                   client_name, client_version, client_purpose, client_guessed
              FROM obs.sessions
             WHERE student_id = %s::uuid AND started_at > {since}
             ORDER BY started_at DESC LIMIT 25""", (sid,))
        sessions = [dict(zip(("started_at", "ended_at", "listen_port", "application_name",
                              "queries_total", "errors_total", "catalog_queries",
                              "end_reason", "client_name", "client_version",
                              "client_purpose", "client_guessed"), r)) for r in sessions]

        events = await fetchall("""
            SELECT event, actor, details, at FROM obs.db_events
             WHERE student_id = %s::uuid ORDER BY at DESC LIMIT 25""", (sid,))
        events = [dict(zip(("event", "actor", "details", "at"), r)) for r in events]

        # Один виклик, а не два: labs_for_student() ганяє report() по кожній
        # лабі груп студента, і повторити це заради словника було б удвічі
        # дорожче рівно ні за що.
        student_labs = await labs_for_student(sid)
        lab_ind = {}
        for l in student_labs:
            lab_ind[str(l["lab_id"])] = await indicators.cached(sid, 0, l["lab_id"])

        rows = await recent_queries(days, bool(errors), "", "", sid, service=bool(service))
        # Допоміжний шар поверх стрічки запитів (docs/article-p4-research/07-10):
        # та сама лаба, але за контекстом сусідніх запитів, а не окремо по
        # кожному відбитку. Рахується лише на вже завантажені `rows` — той
        # самий обсяг, що екран і так показує, без додаткового сканування
        # журналу. Не впливає на `labs`/`here` вище — ті лишаються джерелом
        # правди для покриття лаби.
        temporal = await temporal_context(rows, sid)

        return render(request, "admin/student.html",
                      student=student, days=days, periods=PERIODS, errors=errors,
                      quota_error=quota_error, quota_saved=quota_saved,
                      vacuum_error=vacuum_error, vacuum_ms=vacuum_ms,
                      labs=student_labs, lab_ind=lab_ind,
                      # Меж у лабораторних немає, тож питання не «чи встиг», а
                      # «на чому стоїть»: саме це потрібне на консультації.
                      here=where_now(student_labs),
                      # Показники читаються з кешу: рахувати їх на кожне
                      # відкриття означало б витягати весь журнал студента.
                      ind=await indicators.cached(sid, 0),
                      ind_titles=indicators.INDICATORS,
                      service=service,
                      stats=dict(zip(("total", "errors", "sessions", "total_ms",
                                      "first_at", "last_at"), stats)),
                      rows=rows, temporal=temporal,
                      stuck=stuck, sessions=sessions, events=events,
                      suspicions=suspicions,
                      spark=(await sparklines()).get(sid, []),
                      gaps=await gaps(), nav="students")

    @router.post("/student/{student_id}/quota")
    async def student_quota_set(request: Request, student_id: UUID,
                                csrf_token: str = Form(""), quota_mb: str = Form("")):
        """Ручна квота (З15а): нове значення в МБ замінює `obs.ts_slots.quota_bytes`,
        подія лягає в `obs.db_events` усередині `quota.set_quota()` самого.

        Помилка вводу повертається через query-параметр, а не 400: адмін щойно
        стояв на цій сторінці і має побачити пояснення на тому самому екрані,
        а не порожню сторінку помилки.
        """
        admin = await require_admin(request)
        require_csrf(request, csrf_token)
        sid = str(student_id)
        try:
            mb = int((quota_mb or "").strip())
        except ValueError:
            return RedirectResponse(
                f"/admin/student/{sid}?quota_error={quote('квота має бути цілим числом')}",
                status_code=303)
        try:
            await quota.set_quota(sid, mb, admin["id"])
        except quota.QuotaError as e:
            return RedirectResponse(
                f"/admin/student/{sid}?quota_error={quote(str(e))}", status_code=303)
        return RedirectResponse(f"/admin/student/{sid}?quota_saved=1", status_code=303)

    @router.post("/student/{student_id}/vacuum")
    async def student_vacuum_run(request: Request, student_id: UUID,
                                 csrf_token: str = Form("")):
        """Примусовий VACUUM ANALYZE бази студента (З15б). Дзеркало
        `student_quota_set`: та сама роль `require_admin`/`require_csrf`,
        результат — назад на сторінку студента через query-параметри, а не
        500, бо студент чекає пояснення на тому самому екрані."""
        admin = await require_admin(request)
        require_csrf(request, csrf_token)
        sid = str(student_id)
        # П0.8: роль obs_maintenance має CONNECTION LIMIT 2 — без цього третє
        # одночасне натискання впало б сирою помилкою "too many connections"
        # замість зрозумілого тексту (аудит безпеки З15б, F5).
        wait = ratelimit.vacuum.hit(admin["id"])
        if wait:
            return RedirectResponse(
                f"/admin/student/{sid}?vacuum_error="
                f"{quote(f'забагато запусків, спробуйте за {int(wait) + 1} с')}",
                status_code=303)
        try:
            result = await maintenance.run_vacuum(sid, admin["id"])
        except maintenance.MaintenanceError as e:
            # Адміну — узагальнений текст: сира помилка psycopg (аж до
            # внутрішньої адреси контейнера postgres) не має йти в URL/історію
            # браузера (аудит безпеки З15б, F6). Повний текст лишається лише в
            # серверному логі та в obs.db_events.details — це внутрішній
            # журнал, не UI.
            log.warning("VACUUM ANALYZE через адмінку не вдався для студента "
                        "%s (адмін %s): %s", sid, admin["id"], e)
            return RedirectResponse(
                f"/admin/student/{sid}?vacuum_error="
                f"{quote('VACUUM не виконано, зверніться до розробника')}",
                status_code=303)
        return RedirectResponse(
            f"/admin/student/{sid}?vacuum_ms={result.duration_ms}", status_code=303)

    @router.get("/activity", response_class=HTMLResponse)
    async def activity_screen(request: Request, days: str = "30",
                              student: str | None = None, rebuild: str = ""):
        """Ключовий екран продукту. Чотири представлення одних і тих самих
        даних, кожне відповідає на своє питання:
          теплокарта  — коли працював;
          стрічка     — як довго і з якими паузами;
          крива       — чи ставало легше;
          застрягання — де саме зупинився.
        """
        _, allowed = await require_staff(request)
        d = int(days) if days in PERIODS and days != "0" else 90
        who = None
        # Ідентифікатори студентів дозволених груп — рахуються один раз тут
        # (не в activity.py, щоб той не знав про obs.teacher_groups) і йдуть
        # далі у heatmap/error_curve/totals/sticking замість системного
        # обсягу, коли конкретного студента не обрано.
        allowed_ids: list[str] | None = None
        if allowed is not None:
            allowed_ids = [r[0] for r in await fetchall(
                "SELECT DISTINCT sg.student_id::text FROM obs.student_groups sg"
                " WHERE sg.until IS NULL AND sg.group_name = ANY(%s::text[])",
                (allowed,))]
        if student:
            if allowed_ids is not None and student not in allowed_ids:
                raise HTTPException(status_code=404)
            row = await fetchone(
                "SELECT display_name, email FROM obs.students WHERE id = %s::uuid", (student,))
            if not row:
                raise HTTPException(status_code=404)
            who = row[0] or row[1]
        students_clause, students_params = "", ()
        if allowed_ids is not None:
            students_clause = " WHERE id = ANY(%s::uuid[])"
            students_params = (allowed_ids,)
        return render(request, "admin/activity.html",
                      days=days, periods=PERIODS, gaps=await gaps(),
                      student=student, who=who, nav="activity", rebuild=rebuild,
                      totals=await activity.totals(d, student, allowed_ids),
                      # Межі в 60 днів більше немає: за нею heatmap сам
                      # переходить на тижневі рядки, а мовчазне обрізання
                      # періоду показувало «весь час», а насправді два місяці.
                      heat=await activity.heatmap(0 if days == "0" else d, student,
                                                  allowed=allowed_ids),
                      curve=await activity.error_curve(d, student, allowed_ids),
                      timeline=await activity.sessions_timeline(d, student) if student else [],
                      stuck=await activity.sticking(d, student, allowed=allowed_ids),
                      students=[dict(zip(("id", "name"), r)) for r in await fetchall(
                          "SELECT id::text, coalesce(display_name, email, '—') "
                          f"FROM obs.students{students_clause} ORDER BY 2", students_params)])

    @router.post("/activity/rebuild")
    async def activity_rebuild(request: Request, csrf_token: str = Form("")):
        """Повна перебудова агрегатів. Вони похідні, тож це безпечно —
        і потрібно після зміни правил підрахунку."""
        await require_admin(request)
        require_csrf(request, csrf_token)
        started = await activity.rebuild_all()
        return RedirectResponse(
            "/admin/activity" if started else "/admin/activity?rebuild=busy",
            status_code=303)

    # --------------------------------------------------- 6. Лабораторні (З4)

    @router.get("/labs", response_class=HTMLResponse)
    async def labs_screen(request: Request):
        _, allowed = await require_staff(request)
        groups_clause, groups_params = "", ()
        if allowed is not None:
            groups_clause = " AND group_name = ANY(%s::text[])"
            groups_params = (allowed,)
        groups = [r[0] for r in await fetchall(
            "SELECT DISTINCT group_name FROM obs.student_groups"
            f" WHERE until IS NULL{groups_clause} ORDER BY 1", groups_params)]
        labs = await lab_list()
        if allowed is not None:
            # Лаба без груп — спільна для всіх (той самий предикат, що і
            # labs._labs_for_groups): і її, і лаби своїх груп викладач бачить.
            allowed_set = set(allowed)
            labs = [l for l in labs if not l["groups"] or (set(l["groups"]) & allowed_set)]
        return render(request, "admin/labs.html", labs=labs,
                      groups=groups, constructs=CONSTRUCT_TITLES,
                      gaps=await gaps(), nav="labs")

    @router.post("/labs")
    async def labs_create(request: Request):
        await require_admin(request)
        form = await request.form()
        require_csrf(request, form.get("csrf_token"))
        title = (form.get("title") or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="потрібна назва")
        # Зразки розділяються порожнім рядком, а не «;»: у зразку викладача
        # крапка з комою всередині тексту цілком можлива, а порожній рядок —
        # природний спосіб відділити один запит від іншого в текстовому полі.
        samples = [b.strip() for b in (form.get("samples") or "").split("\n\n")]
        samples = [b for b in samples if b]
        threshold = float(form.get("threshold") or 0.5)
        lab_id = await create_lab(
            title, form.get("note") or "",
            form.getlist("groups"), min(max(threshold, 0.05), 1.0),
            samples, form.getlist("forbidden"))
        # Нова лаба — нові кандидати для вже зіставлених відбитків, яких
        # раніше не було з чим порівняти (промахи лежать у кеші з lab_id NULL,
        # див. _match_fingerprints). Перераховуємо лише промахи, а не все
        # (rebuild_fingerprint_labs() тут була б надлишковою і довгою).
        await execute("DELETE FROM obs.fingerprint_labs WHERE lab_id IS NULL")
        return RedirectResponse(f"/admin/lab/{lab_id}", status_code=303)

    @router.get("/lab/{lab_id}", response_class=HTMLResponse)
    async def lab_screen(request: Request, lab_id: int):
        _, allowed = await require_staff(request)
        # Лаба з групами кількох викладачів звужується до перетину ще ДО
        # report(): інакше report() тягне кандидатів по ВСІХ групах лаби, і
        # викладач однієї з груп бачить (і — у lab_export — експортує) чужу.
        lab = await lab_one(lab_id)
        if lab is None:
            raise HTTPException(status_code=404)
        scope = None
        if allowed is not None and lab["groups"]:
            # Лаба без груп — спільна для всіх (той самий предикат, що і
            # labs_screen/labs._labs_for_groups): report() без груп і так дає
            # "incomplete" однаково всім, звужувати нема чого.
            scope = sorted(set(lab["groups"]) & set(allowed))
            if not scope:
                # Лаба призначена лише чужим групам — 404, а не порожній
                # звіт: порожній звіт виглядав би як «нема кому», хоча
                # насправді просто доступу немає.
                raise HTTPException(status_code=404)
        data = await report(lab_id, scope)
        if data is None:
            raise HTTPException(status_code=404)
        return render(request, "admin/lab.html", **data,
                      ind=await indicators.for_lab(lab_id),
                      constructs=CONSTRUCT_TITLES, gaps=await gaps(), nav="labs")

    # ------------------------------------------- 7. Постійне посилання (П1.4)
    # На захисті треба відкрити конкретний рядок журналу і показати студентові.
    # Без цього доводиться шукати його очима серед тисяч.
    @router.get("/query/{query_id}", response_class=HTMLResponse)
    async def query_screen(request: Request, query_id: int):
        _, allowed = await require_staff(request)
        row = await fetchone("""
            SELECT q.id, q.student_id::text, coalesce(s.display_name, s.email, '—'),
                   q.started_at, q.duration_ms, q.protocol, q.sql_kind,
                   q.sql_text, q.sql_fingerprint, q.status, q.sqlstate,
                   q.error_message, q.error_position, q.rows_affected,
                   q.session_id::text, q.is_catalog, q.is_service
              FROM obs.queries q JOIN obs.students s ON s.id = q.student_id
             WHERE q.id = %s""", (query_id,))
        if not row:
            raise HTTPException(status_code=404)
        keys = ("id", "student_id", "student_name", "started_at", "duration_ms",
                "protocol", "sql_kind", "sql_text", "sql_fingerprint", "status",
                "sqlstate", "error_message", "error_position", "rows_affected",
                "session_id", "is_catalog", "is_service")
        q = dict(zip(keys, row))
        if allowed is not None:
            in_scope = await fetchone(
                "SELECT 1 FROM obs.student_groups WHERE student_id = %s::uuid"
                "   AND until IS NULL AND group_name = ANY(%s::text[])",
                (q["student_id"], allowed))
            if not in_scope:
                raise HTTPException(status_code=404)
        # Сусіди за відбитком: один рядок журналу майже завжди читається в
        # компанії — «скільки разів це саме падало до і після».
        same = await fetchall("""
            SELECT id, started_at, status, sqlstate
              FROM obs.queries
             WHERE student_id = %s::uuid AND sql_fingerprint = %s
             ORDER BY started_at LIMIT 50""", (q["student_id"], q["sql_fingerprint"]))
        return render(request, "admin/query.html", q=q,
                      same=[dict(zip(("id", "started_at", "status", "sqlstate"), r))
                            for r in same],
                      rules=[{"code": c, "title": TITLES[c][0],
                              "explanation": TITLES[c][1]}
                             for c in check(q["sql_text"], q["rows_affected"])],
                      gaps=await gaps(), nav="queries")

    @router.get("/lab/{lab_id}/export.csv")
    async def lab_export(request: Request, lab_id: int):
        _, allowed = await require_staff(request)
        # Те саме звуження, що і в lab_screen (той самий клас витоку через
        # CSV, якщо не звузити ДО report()).
        lab = await lab_one(lab_id)
        if lab is None:
            raise HTTPException(status_code=404)
        scope = None
        if allowed is not None and lab["groups"]:
            scope = sorted(set(lab["groups"]) & set(allowed))
            if not scope:
                raise HTTPException(status_code=404)
        data = await report(lab_id, scope)
        if data is None:
            raise HTTPException(status_code=404)
        name = f"lab-{lab_id}.csv"
        # BOM: без нього Excel відкриває UTF-8 CSV кракозябрами, і викладач
        # вирішує, що зламався експорт.
        body = "\ufeff" + report_csv(data)
        return StreamingResponse(iter([body]), media_type="text/csv; charset=utf-8",
                                 headers={"Content-Disposition":
                                          f'attachment; filename="{name}"'})

    @router.get("/events", response_class=HTMLResponse)
    async def events_screen(request: Request, days: str = "30"):
        _, allowed = await require_staff(request)
        if days not in PERIODS:
            days = "30"
        clause, params = "", ()
        if allowed is not None:
            clause = (" AND EXISTS (SELECT 1 FROM obs.student_groups sg"
                     " WHERE sg.student_id = s.id AND sg.until IS NULL"
                     "   AND sg.group_name = ANY(%s::text[]))")
            params = (allowed,)
        rows = await fetchall(f"""
            SELECT e.at, e.event, e.actor, e.details,
                   s.id::text, s.display_name,
                   coalesce(g.groups, ARRAY[]::text[])
              FROM obs.db_events e JOIN obs.students s ON s.id = e.student_id
              LEFT JOIN obs.student_current_groups g ON g.student_id = s.id
             WHERE e.at > {_interval(days)}{clause}
             ORDER BY e.at DESC LIMIT 300""", params)
        rows = [dict(zip(("at", "event", "actor", "details", "student_id",
                          "display_name", "groups"), r)) for r in rows]
        return render(request, "admin/events.html", rows=rows, days=days,
                      periods=PERIODS, gaps=await gaps(), nav="events")

    # ------------------------------------------------- 8. Викладачі (RBAC)
    # Лише is_admin: призначення ролі й груп викладача — керування правами,
    # а не перегляд журналу, тож require_staff тут не годиться (ADR-023).
    @router.get("/teachers", response_class=HTMLResponse)
    async def teachers_screen(request: Request, error: str = "", saved: str = ""):
        await require_admin(request)
        rows = await fetchall("""
            SELECT s.id::text, coalesce(s.display_name, s.email, s.oidc_sub), s.email,
                   coalesce(array_agg(tg.group_name ORDER BY tg.group_name)
                            FILTER (WHERE tg.group_name IS NOT NULL), ARRAY[]::text[])
              FROM obs.students s
              LEFT JOIN obs.teacher_groups tg ON tg.teacher_id = s.id
             WHERE s.is_teacher
             GROUP BY s.id, s.display_name, s.email, s.oidc_sub
             ORDER BY 2""")
        teachers = [dict(zip(("id", "name", "email", "groups"), r)) for r in rows]
        groups = [r[0] for r in await fetchall(
            "SELECT DISTINCT group_name FROM obs.student_groups"
            " WHERE until IS NULL ORDER BY 1")]
        # З27: список для пошуку замість ручного вводу пошти. Лише ті, хто вже
        # заходив через Mix (є email) і ще не викладач — уже призначені сюди
        # не мають сенсу.
        candidates = [dict(zip(("name", "email"), r)) for r in await fetchall(
            "SELECT coalesce(display_name, email), email FROM obs.students"
            " WHERE NOT is_teacher AND email IS NOT NULL ORDER BY 1")]
        return render(request, "admin/teachers.html", teachers=teachers, groups=groups,
                      candidates=candidates,
                      error=error, saved=saved, gaps=await gaps(), nav="teachers")

    @router.post("/teachers/assign")
    async def teachers_assign(request: Request, csrf_token: str = Form(""),
                              email: str = Form("")):
        """Позначити студента викладачем. Шукаємо за поштою — вона видна в
        інших екранах адмінки, а UUID студента навпаки треба спершу знайти."""
        admin = await require_admin(request)
        require_csrf(request, csrf_token)
        email = email.strip()
        # email nullable і без UNIQUE у схемі: точний регістр дав би «не
        # знайдено» при коректній адресі іншим регістром, а два рядки з тим
        # самим email мовчки перетворили б випадковий на викладача.
        candidates = await fetchall(
            "SELECT id FROM obs.students WHERE lower(email) = lower(%s)", (email,))
        if not candidates:
            return RedirectResponse(
                f"/admin/teachers?error={quote('студента з такою поштою не знайдено')}",
                status_code=303)
        if len(candidates) > 1:
            return RedirectResponse(
                f"/admin/teachers?error={quote('кілька студентів з такою поштою — призначте вручну')}",
                status_code=303)
        teacher_id = candidates[0][0]
        await execute("UPDATE obs.students SET is_teacher = true WHERE id = %s::uuid",
                      (teacher_id,))
        await log_event(teacher_id, "teacher_granted", f"admin:{admin['id']}", email=email)
        return RedirectResponse("/admin/teachers?saved=1", status_code=303)

    @router.post("/teachers/{teacher_id}/revoke")
    async def teachers_revoke(request: Request, teacher_id: UUID,
                              csrf_token: str = Form("")):
        """Забрати роль викладача. Групи в obs.teacher_groups ВИДАЛЯЮТЬСЯ, а не
        лишаються про запас: інакше повторне призначення тій самій людині
        мовчки повертало б усі раніше видані групи, і адмін, який роздав нову
        роль «з нуля», отримав би невідому йому мапу доступу."""
        admin = await require_admin(request)
        require_csrf(request, csrf_token)
        await execute("UPDATE obs.students SET is_teacher = false WHERE id = %s::uuid",
                      (str(teacher_id),))
        await execute("DELETE FROM obs.teacher_groups WHERE teacher_id = %s::uuid",
                      (str(teacher_id),))
        await log_event(str(teacher_id), "teacher_revoked", f"admin:{admin['id']}")
        return RedirectResponse("/admin/teachers?saved=1", status_code=303)

    @router.post("/teachers/{teacher_id}/groups")
    async def teachers_group_add(request: Request, teacher_id: UUID,
                                 csrf_token: str = Form(""), group_name: str = Form("")):
        admin = await require_admin(request)
        require_csrf(request, csrf_token)
        group_name = group_name.strip()
        if not group_name:
            return RedirectResponse(
                f"/admin/teachers?error={quote('назва групи не може бути порожньою')}",
                status_code=303)
        await execute(
            "INSERT INTO obs.teacher_groups (teacher_id, group_name) VALUES (%s::uuid, %s)"
            " ON CONFLICT DO NOTHING", (str(teacher_id), group_name))
        await log_event(str(teacher_id), "teacher_group_added", f"admin:{admin['id']}",
                        group_name=group_name)
        return RedirectResponse("/admin/teachers?saved=1", status_code=303)

    @router.post("/teachers/{teacher_id}/groups/remove")
    async def teachers_group_remove(request: Request, teacher_id: UUID,
                                    csrf_token: str = Form(""), group_name: str = Form("")):
        admin = await require_admin(request)
        require_csrf(request, csrf_token)
        await execute(
            "DELETE FROM obs.teacher_groups WHERE teacher_id = %s::uuid AND group_name = %s",
            (str(teacher_id), group_name))
        await log_event(str(teacher_id), "teacher_group_removed", f"admin:{admin['id']}",
                        group_name=group_name)
        return RedirectResponse("/admin/teachers?saved=1", status_code=303)

    app.include_router(router)
