"""Провізіонінг персональної бази студента.

Дзеркалить postgres/sql/provision_student.sql — той файл лишається канонічною
специфікацією для DBA і для scripts/test-provisioning.sh. Тут та сама
послідовність на psycopg, бо psql-плейсхолдери (:"role") з Python не виконати.
Розбіжність між файлом і цим модулем ловить `make test-parity`: він статично
(через ast, без кластера і без psycopg) звіряє послідовність кроків і значення
параметрів в обох описах. Другий рубіж — критерій фази 1: база, створена
застосунком, має проходити ті самі перевірки ізоляції.

Звести обидва описи в один неможливо: CREATE DATABASE не виконується всередині
транзакції, отже і всередині функції PL/pgSQL. Лишається не дати їм розійтися
мовчки.

Порядок кроків важливий, і крок 4 — критичний. Пояснення в SQL-файлі.
"""
from __future__ import annotations

import logging
from uuid import UUID

from psycopg import sql

from .credentials import db_name, role_name, role_password
from .db import app_pool, execute, fetchone, provisioner

log = logging.getLogger("observer.provisioning")


class NoFreeSlot(RuntimeError):
    pass


async def assign_slot(student_id: UUID) -> tuple[int, str]:
    """Видає вільний слот. FOR UPDATE SKIP LOCKED — щоб два одночасні входи
    не отримали один слот."""
    async with app_pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                """
                UPDATE obs.ts_slots SET student_id = %s, assigned_at = now()
                WHERE slot = (
                    SELECT slot FROM obs.ts_slots
                    WHERE student_id IS NULL
                    ORDER BY slot
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                RETURNING slot, tablespace_name
                """,
                (student_id,),
            )
            row = await cur.fetchone()
    if row is None:
        raise NoFreeSlot("вільних слотів tablespace немає — збільште OBSERVER_TS_SLOTS")
    return row[0], row[1]


async def database_exists(name: str) -> bool:
    async with provisioner() as conn:
        cur = await conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
        return await cur.fetchone() is not None


async def role_exists(name: str) -> bool:
    async with provisioner() as conn:
        cur = await conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
        return await cur.fetchone() is not None


async def create_database(student_id: UUID, tablespace: str) -> tuple[str, str]:
    role = role_name(student_id)
    db = db_name(student_id)
    pw = role_password(student_id)

    r = sql.Identifier(role)
    d = sql.Identifier(db)
    t = sql.Identifier(tablespace)

    async with provisioner() as conn:
        if not await role_exists(role):
            await conn.execute(sql.SQL(
                "CREATE ROLE {r} LOGIN PASSWORD {pw} CONNECTION LIMIT 3 "
                "NOSUPERUSER NOCREATEDB NOCREATEROLE"
            ).format(r=r, pw=sql.Literal(pw)))
        else:
            # Пароль виводиться з master_secret, тож після його ротації роль
            # треба перепідписати. Дешево і робить провізіонінг ідемпотентним.
            await conn.execute(sql.SQL("ALTER ROLE {r} PASSWORD {pw}").format(r=r, pw=sql.Literal(pw)))

        await conn.execute(sql.SQL("GRANT obs_students TO {r}").format(r=r))
        await conn.execute(sql.SQL("CREATE DATABASE {d} OWNER {r} TABLESPACE {t}")
                           .format(d=d, r=r, t=t))

        # КРИТИЧНО. Нова база отримує ACL '=Tc/owner' — CONNECT і TEMP для PUBLIC.
        # Без цього будь-який студент підключається до бази будь-якого іншого.
        # REVOKE мусить виконати ВЛАСНИК: provisioner власником не є і дістане
        # лише 'WARNING: no privileges could be revoked' — мовчазний провал.
        await conn.execute(sql.SQL("SET ROLE {r}").format(r=r))
        await conn.execute(sql.SQL("REVOKE ALL ON DATABASE {d} FROM PUBLIC").format(d=d))
        # З15б: обслуговувач (VACUUM ANALYZE з адмінки). Членство в pg_maintain
        # глобальне, а CONNECT — привілей самої бази, і видати його може лише
        # власник, тобто саме тут, поки діє SET ROLE.
        #
        # Дзеркалить \if-перевірку в provision_student.sql: правильний порядок
        # розгортання — спочатку postgres-міграція (upgrade-2026-09-d.sql),
        # потім образ front. Якщо переплутали, GRANT ... TO obs_maintenance дав
        # би `role "obs_maintenance" does not exist` і впав би ВЕСЬ провізіонінг
        # студента. Ціна помилки в порядку розгортання не має бути «студент не
        # отримав базу» — хай тимчасово не працює лише кнопка VACUUM в адмінці.
        maintenance_role_present = await role_exists("obs_maintenance")
        if maintenance_role_present:
            await conn.execute(sql.SQL(
                "GRANT CONNECT ON DATABASE {d} TO obs_maintenance").format(d=d))
        else:
            log.warning(
                "роль obs_maintenance відсутня — CONNECT на базу %s не видано; "
                "провізіонінг студента %s триває далі, обслуговування "
                "(VACUUM ANALYZE з адмінки) тимчасово недоступне для цієї бази, "
                "доки не накотять postgres-міграцію (upgrade-2026-09-d.sql)",
                db, student_id)
        await conn.execute("RESET ROLE")

        for param, value in (
            ("temp_tablespaces", tablespace),
            ("temp_file_limit", "64MB"),
            ("statement_timeout", "30s"),
            ("idle_in_transaction_session_timeout", "15min"),
            ("default_tablespace", tablespace),
        ):
            await conn.execute(sql.SQL("ALTER ROLE {r} IN DATABASE {d} SET {p} = {v}")
                               .format(r=r, d=d, p=sql.Identifier(param), v=sql.Literal(value)))

    log.info("створено базу %s для студента %s у %s", db, student_id, tablespace)
    if not maintenance_role_present:
        # F3 (аудит З15б): пропущений GRANT має бути ВИДНО адміну в тому ж
        # журналі, де він дивиться на решту подій бази, а не мовчати до
        # випадкового 42501 через тижні. Окреме значення події в CHECK
        # obs.db_events довелося б узгоджувати новою міграцією — натомість
        # використано вже дозволене 'vacuum_run' з ok=false: семантично це
        # той самий факт («обслуговування цієї бази недоступне»), який
        # побачить адмін і на екрані самої кнопки VACUUM.
        try:
            await log_event(student_id, "vacuum_run", "system",
                            db_name=db, ok=False,
                            reason="obs_maintenance role missing")
        except Exception:
            log.exception("не вдалося записати подію vacuum_run (пропущений "
                          "grant) для студента %s", student_id)
    return role, db


async def drop_database(student_id: UUID) -> None:
    role = role_name(student_id)
    db = db_name(student_id)
    async with provisioner() as conn:
        await conn.execute(sql.SQL("SET ROLE {r}").format(r=sql.Identifier(role)))
        await conn.execute(sql.SQL("DROP DATABASE IF EXISTS {d} WITH (FORCE)")
                           .format(d=sql.Identifier(db)))
        await conn.execute("RESET ROLE")
    log.info("видалено базу %s", db)


async def tablespace_usage(tablespace: str) -> int:
    """Розмір слота. Береться саме tablespace, а не pg_database_size: у студента
    власний tablespace, тож це те саме число, але без потреби в CONNECT на
    його базу (obs_app має pg_read_all_stats)."""
    row = await fetchone("SELECT pg_tablespace_size(%s)", (tablespace,))
    return int(row[0]) if row else 0


async def drop_was_logged(student_id: UUID) -> bool:
    """Чи є подія `dropped` після останнього створення бази."""
    row = await fetchone(
        """
        SELECT EXISTS (
            SELECT 1 FROM obs.db_events
             WHERE student_id = %s AND event = 'dropped'
               AND at > coalesce((SELECT max(at) FROM obs.db_events
                                   WHERE student_id = %s
                                     AND event IN ('created','recreated')),
                                 '-infinity'::timestamptz))
        """, (student_id, student_id))
    return bool(row and row[0])


async def log_event(student_id: UUID, event: str, actor: str, **details) -> None:
    import json
    await execute(
        "INSERT INTO obs.db_events(student_id, event, actor, details) VALUES (%s,%s,%s,%s)",
        (student_id, event, actor, json.dumps(details, ensure_ascii=False)),
    )


async def ensure_database(student_id: UUID, actor: str,
                          trigger: str = "login") -> tuple[str, str, bool]:
    """Головна операція фази 1: гарантує наявність бази при кожному вході.

    Повертає (role, db, created). created=True означає, що бази не було —
    або це перший вхід, або студент її видалив.

    `trigger` каже, ЩО спричинило перестворення: "login" чи "recreate_button".
    Це окремий факт від `drop_observed`, який каже, чи зафіксовано МОМЕНТ
    видалення. Зливати їх в один прапорець не можна: базу можна видалити з
    psql (проксі це побачить) і перестворити входом.
    """
    row = await fetchone(
        """
        SELECT st.slot, s.tablespace_name, s.quota_bytes
          FROM obs.students st
          LEFT JOIN obs.ts_slots s ON s.slot = st.slot
         WHERE st.id = %s
        """, (student_id,))
    slot, tablespace, quota = row if row else (None, None, None)
    had_slot = slot is not None

    if slot is None:
        slot, tablespace = await assign_slot(student_id)
        await execute("UPDATE obs.students SET slot = %s WHERE id = %s", (slot, student_id))
        quota_row = await fetchone("SELECT quota_bytes FROM obs.ts_slots WHERE slot = %s", (slot,))
        quota = quota_row[0] if quota_row else None

    db = db_name(student_id)
    if await database_exists(db):
        return role_name(student_id), db, False

    # Момент видалення вважається зафіксованим, якщо є подія `dropped` після
    # останнього створення бази: її пише або кабінет (кнопка), або проксі,
    # який побачив сам оператор DROP DATABASE.
    drop_observed = await drop_was_logged(student_id)

    role, db = await create_database(student_id, tablespace)
    await execute(
        "UPDATE obs.students SET role_name = %s, db_name = %s WHERE id = %s",
        (role, db, student_id))
    # had_slot=True і бази немає => вона зникла після першого входу.
    # Актор — system: базу створює застосунок, а не людина.
    #
    # drop_observed розрізняє два випадки, і це головна цінність поля:
    #   True  — видалення пройшло через кабінет, подія `dropped` вже в журналі;
    #   False — бази просто немає. Найімовірніше студент зробив DROP DATABASE
    #           з psql, і моменту видалення ми не бачили. Закриє це pgproxy у
    #           фазі 3: він бачить сам оператор і запише подію вчасно.
    await log_event(student_id, "recreated" if had_slot else "created", "system",
                    slot=slot, tablespace=tablespace, quota_bytes=quota,
                    trigger=trigger,
                    login_actor=actor,
                    drop_observed=drop_observed if had_slot else None)
    return role, db, True
