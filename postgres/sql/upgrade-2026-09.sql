-- Доведення вже піднятого кластера до схеми вересня 2026.
--
-- initdb-скрипти виконуються лише на порожньому томі, тож стенд, який уже
-- працює, новин зі схеми не побачить. Тут рівно те, що додано:
--   * подія rate_limited у obs.db_events — слід спрацювання обмежувача (П0.8);
--   * згода на дослідження окремо від технічної + obs.research_cohort (П0.2);
--   * obs.indicators — показники процесу, пораховані наперед (П2.1);
--   * obs.surveys — SUS і коротка рефлексія (П2.6);
--   * автовакуум партицій журналу і obs.analyze_journal() — на вже наявних
--     партиціях їх немає, бо initdb на живому томі не виконується.
--
-- Скрипт ідемпотентний: повторний запуск нічого не ламає.
--
-- На чистому кластері запускати НЕ треба: усе це вже є в
-- postgres/initdb/10-observer-schema.sql.
--
-- obs.assessments і obs.admin_views, які тут раніше створювались (П0.4/П1.2),
-- прибрано за З28/ADR-024 — модуль оцінок скасовано. Якщо цей файл
-- застосовують на кластері, що ще не бачив вересневого раунду, порядок такий:
-- спершу цей файл (без блоків оцінок — вони сюди більше не пишуть), а
-- postgres/sql/upgrade-2026-09-i.sql не потрібен, бо створювати тут нічого.

BEGIN;

ALTER TABLE obs.db_events DROP CONSTRAINT IF EXISTS db_events_event_check;
ALTER TABLE obs.db_events ADD CONSTRAINT db_events_event_check CHECK (event IN (
    'created','dropped','recreated','quota_warning','quota_exceeded',
    'credential_rotated','consent_given','disabled','enabled',
    'proxy_passthrough_on','proxy_passthrough_off','rate_limited',
    'research_consent_given','research_consent_declined','dev_login'));
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA obs TO obs_app;
-- П0.2: згода на дослідження — окремо від технічної.
ALTER TABLE obs.students
    ADD COLUMN IF NOT EXISTS research_consent text NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS research_consent_at timestamptz;

DO $$
BEGIN
    ALTER TABLE obs.students ADD CONSTRAINT students_research_consent_check
        CHECK (research_consent IN ('pending','given','declined'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    ALTER TABLE obs.students ADD CONSTRAINT students_research_consent_at_check
        CHECK ((research_consent = 'pending') = (research_consent_at IS NULL));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE OR REPLACE VIEW obs.research_cohort AS
    SELECT id AS student_id, research_consent_at
      FROM obs.students
     WHERE research_consent = 'given' AND disabled_at IS NULL;
GRANT SELECT ON obs.research_cohort TO obs_app;

-- П2.1: показники процесу, пораховані наперед.
CREATE TABLE IF NOT EXISTS obs.indicators (
    student_id      uuid NOT NULL REFERENCES obs.students(id),
    period_days     int  NOT NULL CHECK (period_days >= 0),
    lab_id          smallint NOT NULL DEFAULT 0,
    formula_version text NOT NULL,
    values          jsonb NOT NULL,
    computed_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (student_id, period_days, lab_id, formula_version)
);
-- Для стендів, де таблиця вже є без lab_id: колонка додається, а ключ
-- перескладається. Порожня таблиця робить це безболісним; непорожню все одно
-- перерахує фонове завдання, бо значення похідні.
ALTER TABLE obs.indicators ADD COLUMN IF NOT EXISTS lab_id smallint NOT NULL DEFAULT 0;
DO $$
BEGIN
    ALTER TABLE obs.indicators DROP CONSTRAINT indicators_pkey;
    ALTER TABLE obs.indicators
        ADD CONSTRAINT indicators_pkey
        PRIMARY KEY (student_id, period_days, lab_id, formula_version);
EXCEPTION WHEN others THEN NULL;
END $$;
CREATE INDEX IF NOT EXISTS indicators_computed_idx ON obs.indicators (computed_at);
GRANT SELECT, INSERT, UPDATE, DELETE ON obs.indicators TO obs_app;

-- П2.6: анкети.
CREATE TABLE IF NOT EXISTS obs.surveys (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    student_id  uuid NOT NULL REFERENCES obs.students(id),
    kind        text NOT NULL CHECK (kind IN ('sus', 'reflection')),
    answers     jsonb NOT NULL,
    score       numeric(5,1),
    at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (student_id, kind)
);
CREATE INDEX IF NOT EXISTS surveys_kind_idx ON obs.surveys (kind, at DESC);
GRANT SELECT, INSERT, UPDATE, DELETE ON obs.surveys TO obs_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA obs TO obs_app;

-- -----------------------------------------------------------------------------
-- Автовакуум партицій журналу і статистика батьківської obs.queries.
--
-- Три функції нижче — ДОСЛІВНА копія того, що в
-- postgres/initdb/10-observer-schema.sql (там же й усі пояснення, чому саме ці
-- пороги). Копія потрібна тому, що initdb на працюючому томі не виконується
-- ніколи, а стенд, який живе рік, має вже сімнадцять партицій без жодного з
-- цих параметрів. Правити треба ОБА місця: перелік параметрів мусить збігатися,
-- інакше партиції розійдуться порогами й ніхто про це не дізнається.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION obs.journal_autovacuum_opts() RETURNS text
LANGUAGE sql IMMUTABLE AS $fn$
    SELECT $opts$
        autovacuum_vacuum_scale_factor = 0.0,
        autovacuum_vacuum_threshold = 10000,
        autovacuum_vacuum_insert_scale_factor = 0.0,
        autovacuum_vacuum_insert_threshold = 50000,
        autovacuum_analyze_scale_factor = 0.0,
        autovacuum_analyze_threshold = 5000,
        autovacuum_freeze_min_age = 0,
        autovacuum_vacuum_cost_delay = 1,
        autovacuum_vacuum_cost_limit = 600,
        toast.autovacuum_vacuum_insert_scale_factor = 0.0,
        toast.autovacuum_vacuum_insert_threshold = 50000,
        toast.autovacuum_freeze_min_age = 0,
        toast.autovacuum_vacuum_cost_delay = 1
    $opts$;
$fn$;

CREATE OR REPLACE FUNCTION obs.apply_journal_autovacuum_opts() RETURNS int
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, obs AS $fn$
DECLARE
    r record;
    n int := 0;
BEGIN
    FOR r IN
        SELECT i.inhrelid::regclass AS part
          FROM pg_inherits i
         WHERE i.inhparent = 'obs.queries'::regclass
         ORDER BY 1
    LOOP
        EXECUTE format('ALTER TABLE %s SET (%s)', r.part, obs.journal_autovacuum_opts());
        n := n + 1;
    END LOOP;
    RETURN n;
END
$fn$;
REVOKE EXECUTE ON FUNCTION obs.apply_journal_autovacuum_opts() FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION obs.apply_journal_autovacuum_opts() TO obs_app;

-- Нові партиції мають народжуватися вже з параметрами — інакше догін
-- доведеться повторювати щомісяця.
CREATE OR REPLACE FUNCTION obs.ensure_query_partition(p_month date) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, obs AS $fn$
DECLARE
    lo   date := date_trunc('month', p_month)::date;
    hi   date := (date_trunc('month', p_month) + interval '1 month')::date;
    name text := format('queries_%s', to_char(lo, 'YYYY_MM'));
BEGIN
    IF to_regclass('obs.' || name) IS NULL THEN
        EXECUTE format(
            'CREATE TABLE obs.%I PARTITION OF obs.queries FOR VALUES FROM (%L) TO (%L) '
            'WITH (' || obs.journal_autovacuum_opts() || ')',
            name, lo, hi);
    END IF;
    RETURN name;
END
$fn$;

CREATE OR REPLACE FUNCTION obs.analyze_journal() RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, obs AS $fn$
BEGIN
    ANALYZE ONLY obs.queries;
END
$fn$;
REVOKE EXECUTE ON FUNCTION obs.analyze_journal() FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION obs.analyze_journal() TO obs_app;

-- Догін наявних партицій. Ідемпотентно, тож повторний запуск скрипта нічого
-- не ламає. Те саме окремо — `make apply-autovacuum`.
SELECT obs.apply_journal_autovacuum_opts();

COMMIT;
