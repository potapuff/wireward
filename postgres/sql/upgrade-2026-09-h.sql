-- Доведення вже піднятого кластера до стану від 12.09.2026: перше видання
-- пароля без явного натискання кнопки (З25, docs/roadmap/z25-show-password-first-login.md).
--
-- Що додається:
--   * obs.db_events.event — нове значення 'credential_issued'.
--
-- ЧОМУ ЦЕ ПОТРІБНО. До цієї зміни студент отримував пароль лише натиснувши
-- «Випустити пароль» у кабінеті — зайвий крок для ~90%, кому він потрібен
-- одразу після першого входу. Тепер /cabinet видає токен сам при першому
-- відкритті (student.db_token_set_at IS NULL), якщо його ще не було. Подія
-- окрема від 'credential_rotated': ротація замінює вже виданий секрет,
-- 'credential_issued' — це його поява вперше, і плутати їх в аудиті не варто.
--
-- Запускати В БАЗІ observer від імені суперкористувача:
--   docker compose exec -T postgres psql -U postgres -d observer \
--     -f /dev/stdin < postgres/sql/upgrade-2026-09-h.sql
--
-- Ідемпотентний: можна виконувати повторно.

\set ON_ERROR_STOP on

BEGIN;

-- CHECK доводиться перебудовувати цілком: додати значення до IN-списку
-- інакше не можна. Перелік нижче мусить збігатися з initdb дослівно — саме
-- тому він продубльований, а не «доповнений».
ALTER TABLE obs.db_events DROP CONSTRAINT IF EXISTS db_events_event_check;
ALTER TABLE obs.db_events ADD CONSTRAINT db_events_event_check CHECK (event IN (
    'created','dropped','recreated','quota_warning','quota_exceeded',
    'quota_changed',
    'vacuum_run',
    'credential_rotated','credential_issued','consent_given','disabled','enabled',
    'proxy_passthrough_on','proxy_passthrough_off',
    'rate_limited',
    'research_consent_given','research_consent_declined',
    'dev_login',
    'teacher_granted','teacher_revoked',
    'teacher_group_added','teacher_group_removed'));

COMMIT;

-- Перевірка на місці: нове значення має бути прийнятне.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'obs.db_events'::regclass
           AND conname = 'db_events_event_check'
           AND pg_get_constraintdef(oid) LIKE '%credential_issued%'
    ) THEN
        RAISE EXCEPTION 'db_events_event_check без нового значення credential_issued';
    END IF;
END $$;
