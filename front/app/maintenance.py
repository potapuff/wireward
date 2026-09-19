"""Примусовий VACUUM ANALYZE на персональній базі студента (З15б).

На відміну від `quota.py`, тут немає спільного пулу: `obs_maintenance`
підключається окремим одноразовим з'єднанням до КОНКРЕТНОЇ бази студента
(`obs.students.db_name`), напряму до кластера, минаючи pgproxy — так само,
як `obs_app`/`obs_provisioner` (`front/app/db.py`, `config.py`). Тримати
пул на 150 можливих баз студентів заради операції, яку адміністратор тисне
раз на кілька тижнів, немає сенсу.

VACUUM не виконується всередині транзакції — з'єднання відкривається в
autocommit, аналогічно `provisioner()` у `db.py`.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import psycopg

from .config import settings
from .db import fetchone
from .provisioning import log_event

log = logging.getLogger("observer.maintenance")

# VACUUM студентської бази теоретично може йти довго на захаращеній базі, але
# лабораторні — це одиниці-десятки таблиць з навчальними обсягами даних, тож
# межа тут — запобіжник від зависання адмінського запиту, а не реальний сценарій.
# 60 с, а не 5 хв: без temp_file_limit на сесії obs_maintenance (окремо
# закриває sql-dev у postgres/) п'ятихвилинний бюджет — це бюджет для пастки,
# а не для роботи (аудит безпеки З15б, F1).
STATEMENT_TIMEOUT_MS = 60 * 1000

# Кластер розрахований на 4 ГБ (docs/06-sizing.md); глобальний
# maintenance_work_mem = 128MB витримує двох одночасних споживачів
# (autovacuum-воркери), але не трьох. Адмінський VACUUM — рідкий, тож урізаний
# бюджет не шкодить, а от повний — ризик OOM, якщо збігся з автовакуумом.
MAINTENANCE_WORK_MEM = "32MB"


@dataclass
class VacuumResult:
    duration_ms: int


class MaintenanceError(ValueError):
    """Операцію не виконано: немає бази, не вдалося підключитися або сам
    VACUUM ANALYZE впав. Єдиний канал помилки — дзеркало `quota.QuotaError`."""


async def run_vacuum(student_id: str, admin_id: str) -> VacuumResult:
    """VACUUM ANALYZE всієї бази студента від імені `obs_maintenance`.

    Без параметрів — тобто на всі таблиці поточної (студентської) бази, а не
    на конкретний обʼєкт: адміністратору невідомо наперед, що саме роздулося,
    і індивідуальний вибір таблиці лишив би однакову задачу напівзробленою.

    Подія в `obs.db_events` пишеться ОКРЕМИМ викликом через `log_event()`
    (не тим самим autocommit-з'єднанням до бази студента): VACUUM ANALYZE —
    команда до бази студента обмеженою роллю `obs_maintenance`, а журнал подій
    лежить у базі `observer`, куди в цієї ролі немає доступу і не повинно бути.
    Якщо сам запис у журнал не вдасться (наприклад, стек ще не мігрований і
    CHECK-констрейнт на `obs.db_events` не знає нової події), це НЕ повинно
    перетворити успішний VACUUM на 500-помилку адміну — тому запис у журнал
    обгорнутий окремо і лише логується при збої.

    :raises MaintenanceError: у студента немає провізіонованої бази, або
        підключення/сам VACUUM ANALYZE не вдалися.
    """
    row = await fetchone(
        "SELECT db_name FROM obs.students WHERE id = %s::uuid", (student_id,))
    if not row or not row[0]:
        raise MaintenanceError("у студента немає провізіонованої бази")
    db_name = row[0]

    try:
        # Окреме, не пуловане з'єднання: див. docstring модуля.
        # connect_timeout — у самому DSN (config.py: maintenance_dsn), тут
        # лишається autocommit: інакше VACUUM (не транзакційна команда) впаде.
        async with await psycopg.AsyncConnection.connect(
                settings.maintenance_dsn(db_name), autocommit=True) as conn:
            # Відлік часу — після встановлення з'єднання: duration_ms має
            # відповідати самому VACUUM, а не мережі/TCP-хендшейку.
            started = time.monotonic()
            await conn.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
            await conn.execute(
                f"SET maintenance_work_mem = '{MAINTENANCE_WORK_MEM}'")
            await conn.execute("VACUUM ANALYZE")
    except Exception as e:  # psycopg.Error і мережеві помилки — адміну текст, не 500
        error = str(e)
        log.warning("VACUUM ANALYZE не вдався для студента %s (база %s): %s",
                    student_id, db_name, error)
        try:
            await log_event(student_id, "vacuum_run", f"admin:{admin_id}",
                            db_name=db_name, ok=False, error=error)
        except Exception:
            log.exception("не вдалося записати подію vacuum_run (невдача) "
                          "для студента %s", student_id)
        raise MaintenanceError(error) from e

    duration_ms = int((time.monotonic() - started) * 1000)
    log.info("VACUUM ANALYZE бази %s (студент %s) за %s мс — адмін %s",
             db_name, student_id, duration_ms, admin_id)
    try:
        await log_event(student_id, "vacuum_run", f"admin:{admin_id}",
                        db_name=db_name, ok=True, duration_ms=duration_ms)
    except Exception:
        # Сам VACUUM пройшов — це адмін має побачити. Відсутній слід у
        # журналі подій — прикро, але не причина ховати успіх за 500.
        log.exception("не вдалося записати подію vacuum_run (успіх) "
                      "для студента %s", student_id)
    return VacuumResult(duration_ms=duration_ms)
