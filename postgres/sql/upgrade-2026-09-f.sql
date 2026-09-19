-- Доведення вже піднятого кластера до стану від 07.09.2026: аудит зміни прав
-- викладача (RBAC-аудит, security-review до fef5096/f910254).
--
-- Що додається:
--   * obs.db_events.event — чотири нові значення:
--     'teacher_granted', 'teacher_revoked',
--     'teacher_group_added', 'teacher_group_removed'.
--
-- ЧОМУ ЦЕ ПОТРІБНО. Квота і VACUUM з адмінки пишуть подію в obs.db_events
-- через provisioning.log_event(); призначення/відкликання ролі викладача і
-- зміна його груп — ні. Без сліду неможливо реконструювати, хто і коли
-- роздав доступ до чужого журналу — а це керування правами, а не перегляд.
--
-- Запускати В БАЗІ observer від імені суперкористувача:
--   docker compose exec -T postgres psql -U postgres -d observer \
--     -f /dev/stdin < postgres/sql/upgrade-2026-09-f.sql
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
    'credential_rotated','consent_given','disabled','enabled',
    'proxy_passthrough_on','proxy_passthrough_off',
    'rate_limited',
    'research_consent_given','research_consent_declined',
    'dev_login',
    'teacher_granted','teacher_revoked',
    'teacher_group_added','teacher_group_removed'));

COMMIT;

-- Перевірка на місці: усі чотири нові значення мають бути прийнятні.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'obs.db_events'::regclass
           AND conname = 'db_events_event_check'
           AND pg_get_constraintdef(oid) LIKE '%teacher_group_removed%'
    ) THEN
        RAISE EXCEPTION 'db_events_event_check без нових значень teacher_*';
    END IF;
END $$;
