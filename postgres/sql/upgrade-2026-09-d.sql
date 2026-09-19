-- Доведення вже піднятого кластера до стану від 07.09.2026: З15б — примусовий
-- VACUUM ANALYZE на персональній базі студента з адмінки.
--
-- Що додається:
--   * роль obs_maintenance   — член вбудованої pg_maintain (PG17+);
--   * CONNECT на КОЖНУ вже наявну базу студента для цієї ролі;
--   * obs.db_events.event    — нове значення 'vacuum_run'.
--
-- ЧОМУ ОКРЕМА РОЛЬ, А НЕ SUPERUSER. VACUUM чужої таблиці вимагає або володіння
-- нею, або членства в pg_maintain, яка дає рівно VACUUM, ANALYZE, CLUSTER,
-- REINDEX, REFRESH MATERIALIZED VIEW і LOCK TABLE — і не дає читати дані
-- студента. Superuser у рантаймі не потрібен ніде (ADR-007),
-- і заводити його заради кнопки в адмінці — крок назад.
--
-- ЧОМУ CONNECT ДОВОДИТЬСЯ ДАВАТИ ОКРЕМО. Членство в pg_maintain глобальне: одного
-- GRANT на кластер досить для всіх баз, теперішніх і майбутніх. А CONNECT —
-- привілей самої бази, і персональна база після `REVOKE ALL ... FROM PUBLIC`
-- (provision_student.sql, крок 4) не пускає нікого, крім власника. Без цього
-- скрипта кнопка «VACUUM» мовчки впаде на всіх уже наявних базах з 42501, і виглядати
-- це буде як «вакуум не спрацював», а не як «немає права підключитися».
--
-- ЧОМУ БЕЗ dblink (на відміну від upgrade-2026-09-b.sql). Там правився ACL
-- СИСТЕМНОГО КАТАЛОГУ, який фізично лежить у кожній базі окремо, тож туди треба
-- було підключатися. Тут же ACL бази зберігається в pg_database — спільному
-- каталозі кластера, — і GRANT CONNECT на будь-яку базу видається з будь-якої
-- іншої. Зайве розширення в службовій базі не потрібне.
--
-- Для НОВИХ баз те саме робить provision_student.sql (і його дзеркало
-- front/app/provisioning.py), а роль створює initdb/00-roles.sh. Цей файл
-- потрібен лише тому, що initdb виконується РІВНО ОДИН РАЗ, на порожньому PGDATA.
--
-- ПЕРЕДУМОВА: секрет має бути в контейнері. Тобто спершу
--   ./scripts/make-secrets.sh && docker compose up -d --force-recreate postgres
-- (у compose.yaml сервіс postgres уже перелічує pg_maintenance_password).
--
-- Запускати В БАЗІ observer від імені суперкористувача:
--   docker compose exec -T postgres psql -U postgres -d observer \
--     -f /dev/stdin < postgres/sql/upgrade-2026-09-d.sql
--
-- Ідемпотентний: можна виконувати повторно.

\set ON_ERROR_STOP on

-- Пароль читається з того самого файлу, який виставляє initdb/00-roles.sh, щоб
-- роль і .env фронта не розійшлися. Далі він іде тільки через \gexec, тобто в
-- транскрипт не друкується.
\set pw `cat /run/secrets/pg_maintenance_password 2>/dev/null`

SELECT :'pw' <> '' AS have_pw \gset
\if :have_pw
\else
DO $$ BEGIN
    RAISE EXCEPTION 'немає /run/secrets/pg_maintenance_password у контейнері: спершу ./scripts/make-secrets.sh і docker compose up -d --force-recreate postgres';
END $$;
\endif

BEGIN;

-- 1. Роль обслуговування. CREATE ROLE не має IF NOT EXISTS, тому умова —
--    у WHERE, а сам оператор виконує \gexec (він же й не показує пароль).
SELECT format('CREATE ROLE obs_maintenance LOGIN PASSWORD %L CONNECTION LIMIT 2', :'pw')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'obs_maintenance')
\gexec

-- Повторний запуск після ротації секрету має привести пароль і ліміт до файлу,
-- інакше «скрипт же відпрацював», а фронт не логіниться.
SELECT format('ALTER ROLE obs_maintenance PASSWORD %L CONNECTION LIMIT 2', :'pw')
\gexec

-- 2. Вбудована роль PG17+: рівно право обслуговувати, без права читати.
--    GRANT глобальний — на майбутні бази студентів його повторювати не треба.
GRANT pg_maintain TO obs_maintenance;

-- 2а. Стеля тимчасових файлів для обслуговувача (аудит З15б, F1).
--     ANALYZE обчислює вирази функціональних індексів, тобто виконує код
--     студента в ЦІЙ сесії. Персональні ліміти студента навішані через
--     ALTER ROLE u_x IN DATABASE db_x SET ... і сюди не поширюються: живцем
--     один ANALYZE дав 817 МБ тимчасових файлів за 22 с у pg_default, поруч
--     із WAL і базою observer. 64 МБ — та сама стеля, що й у студента.
--     temp_file_limit має рівень SUSET, а obs_maintenance не superuser і не
--     має GRANT SET ON PARAMETER, тож зняти це з себе не може.
--     ALTER ROLE ідемпотентний: повторний запуск лише перезаписує значення.
ALTER ROLE obs_maintenance SET temp_file_limit = '64MB';

-- 2б. Гігієна кластера (аудит З15б, F4): template1 роздає CONNECT для PUBLIC
--     за замовчуванням. Шляху експлуатації зараз немає (ім'я бази береться з
--     довіреної obs.students.db_name), але це зайва база, у якій будь-яка
--     роль — зокрема obs_maintenance із pg_maintain — може діяти повз наш
--     контроль. На провізіонінг це не впливає: template1 використовується як
--     ШАБЛОН (datistemplate=true + CREATEDB), а не як база для підключення.
REVOKE CONNECT ON DATABASE template1 FROM PUBLIC;

-- 3. CONNECT на всі наявні персональні бази. Іменування db_* задає
--    front/app/credentials.py:db_name(); службові бази свідомо пропускаємо —
--    обслуговування журналу до цієї ролі стосунку не має.
DO $$
DECLARE
    r   record;
    cnt int := 0;
BEGIN
    FOR r IN
        SELECT datname FROM pg_database
         WHERE datallowconn AND datname LIKE 'db\_%' ORDER BY datname
    LOOP
        EXECUTE format('GRANT CONNECT ON DATABASE %I TO obs_maintenance', r.datname);
        cnt := cnt + 1;
    END LOOP;
    RAISE NOTICE 'obs_maintenance: CONNECT видано на % баз студентів', cnt;
END $$;

-- 4. Нова подія 'vacuum_run' у CHECK obs.db_events.
--    CHECK доводиться перебудовувати цілком: додати одне значення до IN-списку
--    інакше не можна. Перелік нижче мусить збігатися з initdb дослівно —
--    саме тому він продубльований, а не «доповнений».
ALTER TABLE obs.db_events DROP CONSTRAINT IF EXISTS db_events_event_check;
ALTER TABLE obs.db_events ADD CONSTRAINT db_events_event_check CHECK (event IN (
    'created','dropped','recreated','quota_warning','quota_exceeded',
    'quota_changed',
    'vacuum_run',
    'credential_rotated','consent_given','disabled','enabled',
    'proxy_passthrough_on','proxy_passthrough_off',
    'rate_limited',
    'research_consent_given','research_consent_declined',
    'dev_login'));

COMMIT;

-- Перевірка на місці: жодна база студента не має лишитися без CONNECT. Краще
-- впасти тут, ніж дізнатися від адміністратора, у якого кнопка «VACUUM»
-- працює на одних студентах і не працює на інших.
DO $$
DECLARE
    bad text[];
BEGIN
    SELECT array_agg(d.datname ORDER BY d.datname) INTO bad
      FROM pg_database d
     WHERE d.datallowconn AND d.datname LIKE 'db\_%'
       AND NOT has_database_privilege('obs_maintenance', d.datname, 'CONNECT');
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'obs_maintenance без CONNECT на: %', array_to_string(bad, ', ');
    END IF;
END $$;

-- І навпаки: службові бази лишаються закритими. pg_maintain діє в кожній базі,
-- до якої роль може підключитися, тож зайвий CONNECT на observer віддав би їй
-- обслуговування журналу.
DO $$
BEGIN
    IF has_database_privilege('obs_maintenance', 'observer', 'CONNECT') THEN
        RAISE EXCEPTION 'obs_maintenance має CONNECT на observer — приберіть: REVOKE CONNECT ON DATABASE observer FROM obs_maintenance';
    END IF;
END $$;

-- І запобіжники з F1/F4 — щоб «скрипт відпрацював» не означало «а ліміт не ліг».
DO $$
DECLARE
    v text;
BEGIN
    SELECT s.setconfig[1] INTO v
      FROM pg_db_role_setting s JOIN pg_roles r ON r.oid = s.setrole
     WHERE r.rolname = 'obs_maintenance' AND s.setdatabase = 0
       AND s.setconfig[1] LIKE 'temp_file_limit=%';
    IF v IS DISTINCT FROM 'temp_file_limit=64MB' THEN
        RAISE EXCEPTION 'obs_maintenance без temp_file_limit=64MB (маємо %) — ANALYZE функціонального індексу студента лишиться безлімітним', coalesce(v, 'нічого');
    END IF;

    IF has_database_privilege('public', 'template1', 'CONNECT') THEN
        RAISE EXCEPTION 'PUBLIC досі має CONNECT на template1';
    END IF;
END $$;
