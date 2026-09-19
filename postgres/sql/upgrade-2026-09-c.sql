-- Доведення вже піднятого кластера до стану від 07.09.2026: З13/З14 (треймап
-- активності по лабораторних) і З15а (ручна зміна квоти адміністратором).
--
-- Що додається:
--   * obs.fingerprint_labs        — кеш привʼязки «студент+відбиток → лаба»;
--   * obs.lab_activity_daily()    — розтин студент × лаба × день для треймапа;
--   * obs.db_events.event         — нове значення 'quota_changed';
--   * obs.ts_slots.quota_bytes    — CHECK (> 0).
--
-- Ідемпотентний: можна виконувати повторно.
--
-- Запускати В БАЗІ observer від імені суперкористувача:
--   docker compose exec -T postgres psql -U postgres -d observer \
--     -f /dev/stdin < postgres/sql/upgrade-2026-09-c.sql
--
-- initdb/10-observer-schema.sql оновлено тим самим; цей файл потрібен лише
-- тому, що initdb виконується РІВНО ОДИН РАЗ, на порожньому PGDATA.

\set ON_ERROR_STOP on

BEGIN;

-- 1. Кеш привʼязки відбитків до лабораторних.
--    Обґрунтування — коментар у initdb/10-observer-schema.sql і
--    docs/03-data-model.md; коротко: збіг нечіткий і рахується в Python,
--    у SQL його відтворити неможливо, а треймап потрібен на кожне відкриття.
CREATE TABLE IF NOT EXISTS obs.fingerprint_labs (
    student_id      uuid     NOT NULL REFERENCES obs.students(id) ON DELETE CASCADE,
    sql_fingerprint text     NOT NULL,
    lab_id          smallint REFERENCES obs.labs(id) ON DELETE CASCADE,
    score           real,
    matched_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (student_id, sql_fingerprint)
);
CREATE INDEX IF NOT EXISTS fingerprint_labs_lab_idx
    ON obs.fingerprint_labs (lab_id) WHERE lab_id IS NOT NULL;

GRANT SELECT, INSERT, UPDATE, DELETE ON obs.fingerprint_labs TO obs_app;

-- 2. Розтин для треймапа. CREATE OR REPLACE, тож повторний запуск безпечний.
CREATE OR REPLACE FUNCTION obs.lab_activity_daily(p_since timestamptz,
                                                  p_group text DEFAULT NULL)
RETURNS TABLE (student_id uuid, lab_id smallint, day date,
               queries bigint, errors bigint, active_seconds bigint,
               last_at timestamptz)
LANGUAGE sql STABLE SET search_path = pg_catalog, obs AS $fn$
    SELECT q.student_id,
           fl.lab_id,
           (q.started_at AT TIME ZONE 'Europe/Kyiv')::date,
           count(*),
           count(*) FILTER (WHERE q.status = 'error'),
           count(DISTINCT date_trunc('second', q.started_at)),
           max(q.started_at)
      FROM obs.queries q
      JOIN obs.fingerprint_labs fl
        ON fl.student_id = q.student_id
       AND fl.sql_fingerprint = q.sql_fingerprint
     WHERE q.started_at >= p_since
       AND fl.lab_id IS NOT NULL
       AND NOT (q.is_catalog OR q.is_service)
       AND (p_group IS NULL OR EXISTS (
               SELECT 1 FROM obs.student_groups g
                WHERE g.student_id = q.student_id
                  AND g.until IS NULL
                  AND g.group_name = p_group))
     GROUP BY 1, 2, 3
$fn$;
REVOKE EXECUTE ON FUNCTION obs.lab_activity_daily(timestamptz, text) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION obs.lab_activity_daily(timestamptz, text) TO obs_app;

-- 3. Нова подія 'quota_changed' у CHECK obs.db_events.
--    CHECK доводиться перебудовувати цілком: додати одне значення до IN-списку
--    інакше не можна. Перелік нижче мусить збігатися з initdb дослівно —
--    саме тому він продубльований, а не «доповнений».
ALTER TABLE obs.db_events DROP CONSTRAINT IF EXISTS db_events_event_check;
ALTER TABLE obs.db_events ADD CONSTRAINT db_events_event_check CHECK (event IN (
    'created','dropped','recreated','quota_warning','quota_exceeded',
    'quota_changed',
    'credential_rotated','consent_given','disabled','enabled',
    'proxy_passthrough_on','proxy_passthrough_off',
    'rate_limited',
    'research_consent_given','research_consent_declined',
    'dev_login'));

-- 4. Нуль у quota_bytes — не «без квоти», а тиха відмова механізму: і проксі
--    (pgproxy/internal/auth/auth.go), і фоновий job трактують 0 як «квоти
--    немає». Після появи кнопки «змінити квоту» це вже не теоретична
--    можливість, а один хибний ввід в адмінці.
ALTER TABLE obs.ts_slots DROP CONSTRAINT IF EXISTS ts_slots_quota_bytes_check;
ALTER TABLE obs.ts_slots ADD CONSTRAINT ts_slots_quota_bytes_check
    CHECK (quota_bytes > 0);

COMMIT;

-- Кеш наповнюється фоновим циклом front (activity.background_loop →
-- labs.refresh_fingerprint_labs). Щоб треймап показав історію, а не лише
-- останні години, після оновлення варто один раз прогнати повну перебудову:
--   docker compose exec -T front python -c \
--     "import asyncio; from app import db, labs; \
--      asyncio.run(db.open_pools()); \
--      asyncio.run(labs.rebuild_fingerprint_labs())"
