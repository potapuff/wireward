-- Доведення вже піднятого кластера до стану від 10.09.2026:
-- повернути студентам SELECT на pg_catalog.pg_roles і pg_catalog.pg_user.
--
-- ЧОМУ. З6 (docs/07-roadmap.md) закрив перелік баз і ролей через
-- `REVOKE ... FROM PUBLIC` у template1. Частину з pg_database скасували
-- 06.09.2026 (upgrade-2026-09-b.sql), а про ролі тоді записали: «pg_roles /
-- pg_user лишаються закритими, обом клієнтам для дерева вони не потрібні —
-- власника бази беруть через pg_get_userbyid(), а та функція ACL не
-- перевіряє». Міркування виявилося неповним: воно описувало лише побудову
-- дерева обʼєктів. Драйвер pgjdbc у DataGrip окремо, поза інтроспекцією, шле
--     select usesuper from pg_user where usename = current_user
-- (спіймано в живому трафіку, docs/07-roadmap.md; зафіксовано тестом
-- TestIsCatalogРозширенийПерелік у pgproxy/internal/proxy/classify_test.go).
-- На студентській базі це давало `42501 permission denied for table pg_user`
-- прямо в сеансі — помилка видима, виглядає як поломка і лякає студента.
-- Перевірено на живому стенді 10.09.2026. Це рівно та сама форма помилки, що
-- й із pg_database, тому й лікування те саме.
--
-- Ізоляція даних від цього не залежить: доступ до чужих баз тримають datacl
-- (provision_student.sql, крок 4) і проксі. Відкривається лише перелік імен
-- ролей.
--
-- ЧОМУ ЦЕ ОКРЕМИЙ СКРИПТ, А НЕ ПРАВКА initdb. ACL системного каталогу
-- зберігається В КОЖНІЙ БАЗІ ОКРЕМО, у власному рядку pg_class. Правка
-- postgres/initdb/30-catalog-visibility.sh діє лише на НОВІ бази, що
-- народяться з template1 на чистому томі. Усі бази, які вже існують на
-- стенді, несуть REVOKE у власній копії ACL, і зняти його треба
-- підключившись до кожної.
--
-- ЧОМУ dblink. GRANT на системному каталозі має право видати лише його
-- власник, тобто суперкористувач, і виконати його треба всередині кожної
-- бази. Ані `SET ROLE`, ані `\gexec` через бази не ходять; dblink дає
-- локальні підключення по unix-сокету без пароля, бо ми вже postgres.
--
-- Запускати В БАЗІ postgres від імені суперкористувача:
--   docker compose exec -T postgres psql -U postgres -d postgres \
--       -v ON_ERROR_STOP=1 -f /srv/observer/postgres/sql/upgrade-2026-09-g.sql
--
-- Скрипт ідемпотентний: повторний запуск нічого не ламає.
-- На чистому кластері запускати НЕ треба — усе це вже в
-- postgres/initdb/30-catalog-visibility.sh.

\set ON_ERROR_STOP on

-- Чи був dblink установлений до нас: якщо ні — приберемо його за собою, щоб
-- не лишати студентам зайвого розширення в службовій базі.
SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'dblink') AS dblink_was_here
\gset

CREATE EXTENSION IF NOT EXISTS dblink;

DO $$
DECLARE
    r   record;
    cnt int := 0;
BEGIN
    -- Усі бази, до яких узагалі можна підключитися: template1 (щоб майбутні
    -- бази успадкували ACL фізично при CREATE DATABASE), postgres, observer
    -- і всі db_* студентів. template0 має datallowconn = false і пропускається.
    FOR r IN
        SELECT datname FROM pg_database WHERE datallowconn ORDER BY datname
    LOOP
        PERFORM dblink_exec(
            'dbname=' || quote_ident(r.datname),
            'GRANT SELECT ON pg_catalog.pg_roles, pg_catalog.pg_user TO obs_students');
        cnt := cnt + 1;
    END LOOP;
    RAISE NOTICE 'pg_roles/pg_user: SELECT повернено obs_students у % базах', cnt;
END $$;

-- Перевірка на місці: жодна база, до якої можна підключитися, не має лишитися
-- без права. Якщо десь не спрацювало — краще впасти тут, ніж дізнатися від
-- студента, у якого DataGrip показує permission denied на рівному місці.
DO $$
DECLARE
    bad text[];
BEGIN
    SELECT array_agg(d.datname ORDER BY d.datname) INTO bad
      FROM pg_database d
     WHERE d.datallowconn
       AND NOT (SELECT ok FROM dblink(
                   'dbname=' || quote_ident(d.datname),
                   $q$SELECT has_table_privilege('obs_students',
                                                 'pg_catalog.pg_roles', 'SELECT')
                         AND has_table_privilege('obs_students',
                                                 'pg_catalog.pg_user', 'SELECT')$q$
               ) AS t(ok boolean));
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'obs_students досі без SELECT на pg_roles/pg_user у: %',
              array_to_string(bad, ', ');
    END IF;
END $$;

\if :dblink_was_here
\echo 'dblink лишається — він був установлений до цього скрипта'
\else
DROP EXTENSION dblink;
\endif
