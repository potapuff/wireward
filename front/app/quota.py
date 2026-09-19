"""Мʼяка квота і обслуговування журналу.

Жорстку межу тримає project quota файлової системи. Але на машині розробника її
взагалі немає (том лежить у VM Docker Desktop, ADR-011), а на сервері вона
спрацьовує різко: `ENOSPC` посеред транзакції без попередження. Тому мʼяка
квота — не запасний варіант, а обовʼязковий механізм:

* попереджає студента на 80%, поки лабораторна ще не зламалася;
* на 100% проксі перестає пропускати оператори, що ЗБІЛЬШУЮТЬ дані, але
  лишає ті, що звільняють місце. Інакше студент опинився б у пастці, де
  нічого не можна зробити, зокрема й прибрати зайве.
"""
from __future__ import annotations

import asyncio
import logging

from .db import execute, fetchall, fetchone

log = logging.getLogger("observer.quota")

WARN_AT = 0.80


async def refresh_usage() -> list[dict]:
    """Оновлює кеш розмірів і повертає слоти, що перетнули межі.

    `pg_tablespace_size` — системний виклик на кожен слот. Для 150 слотів раз
    на хвилину це дешево, а от робити його на кожен запит студента (як мусив би
    проксі) — ні. Звідси й кеш.
    """
    rows = await fetchall("""
        UPDATE obs.ts_slots s
           SET used_bytes = pg_tablespace_size(s.tablespace_name),
               checked_at = now()
         WHERE s.student_id IS NOT NULL
        RETURNING s.student_id::text, s.tablespace_name, s.used_bytes, s.quota_bytes
    """)
    out = []
    for sid, ts, used, quota in rows:
        if not quota:
            continue
        ratio = (used or 0) / quota
        if ratio >= 1.0:
            out.append({"student_id": sid, "state": "quota_exceeded",
                        "used": used, "quota": quota})
        elif ratio >= WARN_AT:
            out.append({"student_id": sid, "state": "quota_warning",
                        "used": used, "quota": quota})
    return out


async def announce(crossed: list[dict]) -> None:
    """Пише подію, але лише коли стан ЗМІНИВСЯ.

    Без цієї перевірки студент, що сидить на 85%, отримував би попередження
    щохвилини, і журнал подій перетворився б на шум, у якому не видно
    справжніх переходів.
    """
    for item in crossed:
        await execute("""
            INSERT INTO obs.db_events (student_id, event, actor, details)
            SELECT %s::uuid, %s, 'system',
                   jsonb_build_object('used_bytes', %s::bigint,
                                      'quota_bytes', %s::bigint,
                                      'pct', round(%s::numeric * 100 / %s::numeric))
             WHERE NOT EXISTS (
                 SELECT 1 FROM obs.db_events
                  WHERE student_id = %s::uuid
                    AND event IN ('quota_warning', 'quota_exceeded')
                    AND at > now() - interval '1 day'
                    AND event = %s
                  ORDER BY at DESC LIMIT 1)
        """, (item["student_id"], item["state"], item["used"], item["quota"],
              item["used"], item["quota"], item["student_id"], item["state"]))


# ------------------------------------------------------------- зміна квоти (З15а)

# Межі того, що адміністратор може ввести руками. Обидві — захист від друкарської
# помилки, а не політика:
#   * нижня — квота, менша за вже зайняте службовими даними, миттєво заблокувала б
#     студентові будь-який запис і виглядала б як «база зламалася»;
#   * верхня — три нулі зайвих у полі вводу не мають перетворювати мʼяку квоту на
#     її відсутність.
MIN_QUOTA_MB = 10
MAX_QUOTA_MB = 1024

# Квота файлової системи (scripts/setup-quota.sh, типово 50 МБ на слот) НЕ
# рухається цією функцією: project quota ставиться на хості під root, а front
# ходить у СУБД під obs_app. Тобто підняти quota_bytes вище за жорстку межу ФС
# можна, і мʼяка квота тоді просто перестане спрацьовувати раніше за ENOSPC.
# Тому такий випадок гучно йде в лог, а сам факт лишається в db_events.
FS_HARD_LIMIT_MB = 50


class QuotaError(ValueError):
    """Ввід, який не можна виконати. Окремий тип, щоб роут відрізняв
    «адміністратор помилився» від «зламалася база»."""


async def set_quota(student_id: str, new_mb: int, admin_id: str) -> dict:
    """Змінює мʼяку квоту студента і лишає слід у журналі подій (З15а).

    Досі `quota_bytes` виставлявся один раз, під час провізіонінгу
    (`postgres/sql/provision_student.sql`). Тут — та сама колонка, але з боку
    застосунку: `obs_app` має UPDATE на `obs.ts_slots` від самого початку
    (`initdb/10-observer-schema.sql`, блок прав), нового гранту не потрібно.

    Обидва боки операції — і нове значення, і подія — в ОДНОМУ операторі:
    квота, змінена без сліду в `obs.db_events`, це рівно та тиха зміна, через
    яку потім неможливо пояснити, чому в студента 200 МБ. Старе значення
    береться самоприєднанням (`FROM obs.ts_slots prev`): воно читається зі
    знімка до оновлення, і інакше його вже не дістати.

    :param student_id: uuid студента (не слота).
    :param new_mb: нова квота в мегабайтах, `MIN_QUOTA_MB`..`MAX_QUOTA_MB`.
    :param admin_id: uuid адміністратора; в `actor` лягає як `admin:<uuid>` —
        та сама нотація, що в `credential_rotated`.
    :returns: `{"old_bytes", "new_bytes", "used_bytes"}`.
    :raises QuotaError: значення поза межами або в студента немає слота.
    """
    if not isinstance(new_mb, int) or not (MIN_QUOTA_MB <= new_mb <= MAX_QUOTA_MB):
        raise QuotaError(
            f"квота має бути цілим числом від {MIN_QUOTA_MB} до {MAX_QUOTA_MB} МБ")

    new_bytes = new_mb * 1024 * 1024
    row = await fetchone("""
        WITH upd AS (
            UPDATE obs.ts_slots s
               SET quota_bytes = %s
              FROM obs.ts_slots prev
             WHERE prev.slot = s.slot
               AND s.student_id = %s::uuid
         RETURNING s.student_id, prev.quota_bytes AS old_bytes,
                   s.quota_bytes AS new_bytes, s.used_bytes
        )
        INSERT INTO obs.db_events (student_id, event, actor, details)
        SELECT u.student_id, 'quota_changed', %s,
               jsonb_build_object('old_bytes', u.old_bytes,
                                  'new_bytes', u.new_bytes,
                                  'used_bytes', u.used_bytes)
          FROM upd u
        RETURNING (details->>'old_bytes')::bigint,
                  (details->>'new_bytes')::bigint,
                  (details->>'used_bytes')::bigint
    """, (new_bytes, student_id, f"admin:{admin_id}"))

    if row is None:
        # Слота немає — студентові ще не провізіонували базу. Мовчазне «нічого
        # не сталося» тут гірше за помилку: в адмінці кнопка виглядала б так,
        # ніби спрацювала.
        raise QuotaError("у студента немає виданого слота tablespace")

    old_bytes, got_bytes, used_bytes = row
    if new_mb > FS_HARD_LIMIT_MB:
        log.warning("квота студента %s піднята до %s МБ — вище за жорстку межу "
                    "ФС (%s МБ). Мʼяка квота тепер не спрацює раніше за ENOSPC; "
                    "межу ФС міняють scripts/setup-quota.sh на хості",
                    student_id, new_mb, FS_HARD_LIMIT_MB)
    log.info("квота студента %s: %s → %s байт (адмін %s)",
             student_id, old_bytes, got_bytes, admin_id)
    return {"old_bytes": old_bytes, "new_bytes": got_bytes, "used_bytes": used_bytes}


# -------------------------------------------------------- треймап диска (З14)

async def disk_usage() -> list[dict]:
    """Слоти tablespace, ЗАЙНЯТІ студентами, для треймапа диска на адмін-дашборді.

    Слот без студента (`student_id IS NULL`) — вільний, ще не виданий
    провізіонінгом; на стенді таких десятки на кожен зайнятий (137 із 150).
    Задача екрана (docs/07-roadmap.md, З14) — «хто скільки займає», а не
    інвентаризація вільних слотів, тож вони сюди взагалі не потрапляють:
    інакше треймап малював би десятки порожніх плиток на кожну змістовну.
    """
    rows = await fetchall("""
        SELECT ts.slot, ts.student_id::text,
               coalesce(s.display_name, s.email), ts.quota_bytes, ts.used_bytes
          FROM obs.ts_slots ts
          JOIN obs.students s ON s.id = ts.student_id
         ORDER BY ts.slot
    """)
    out = []
    for slot, sid, name, quota_bytes, used_bytes in rows:
        out.append({
            "slot": slot, "student_id": sid, "student": name,
            "quota_bytes": quota_bytes, "used_bytes": used_bytes,
            "pct": (used_bytes / quota_bytes)
                   if (used_bytes is not None and quota_bytes) else None,
        })
    return out


# ------------------------------------------------------- обслуговування журналу

async def maintain_partitions(keep_months: int = 18) -> None:
    """Створює партиції наперед і прибирає застарілі.

    `DROP PARTITION` на порядки дешевше за `DELETE`: саме заради цього журнал і
    партиційований. Партиції створюємо на два місяці вперед, щоб перехід через
    межу місяця ніколи не впав у DEFAULT-партицію — непорожня DEFAULT це алярм,
    а не норма.
    """
    # Спершу порятунок: якщо в DEFAULT є рядки за місяць, партиції на який
    # немає, створити ту партицію вже неможливо — PostgreSQL відмовиться.
    # Одна пропущена партиція так блокує всі наступні, і мовчки.
    rescued = await fetchall("SELECT obs.rescue_default_partition()")
    if rescued and rescued[0][0]:
        log.warning("врятовано %s записів із DEFAULT-партиції: партиції на "
                    "їхній місяць не існувало", rescued[0][0])

    for offset in (0, 1, 2):
        await execute(
            "SELECT obs.ensure_query_partition((current_date + "
            f"interval '{offset} months')::date)")

    # Обидві операції йдуть через SECURITY DEFINER-функції: obs_app не має
    # прав на схему obs і не повинен їх мати.
    dropped = await fetchall("SELECT obs.prune_query_partitions(%s)", (keep_months,))
    for (name,) in dropped:
        # Тут журнал зникає назавжди, тому — гучно в лог.
        log.warning("прибрано партицію журналу за строком зберігання: %s", name)


async def background_loop(interval: int = 60) -> None:
    tick = 0
    while True:
        try:
            await announce(await refresh_usage())
            # Партиції рухаються раз на добу: частіше немає сенсу.
            if tick % (24 * 60) == 0:
                await maintain_partitions()
            tick += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("обслуговування квот і партицій не вдалося")
        await asyncio.sleep(interval)
