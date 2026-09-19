-- =============================================================================
-- Провізіонінг студента. Виконує front від імені obs_provisioner (НЕ superuser).
-- Плейсхолдери: :role, :db, :ts, :pw  — підставляє застосунок (фаза 1).
--
-- УВАГА до порядку. CREATE DATABASE / DROP DATABASE не виконуються всередині
-- транзакції, тому це послідовність команд в autocommit, а не один блок.
--
-- Цей файл — канонічна специфікація. front/app/provisioning.py дзеркалить його
-- на psycopg; будь-яка зміна тут має бути зроблена і там, інакше впаде
-- `make test-parity`.
-- =============================================================================

-- 1. Роль студента. Пароль — HMAC(master_secret, student_id), ніде не зберігається.
CREATE ROLE :"role" LOGIN PASSWORD :'pw'
    CONNECTION LIMIT 3
    NOSUPERUSER NOCREATEDB NOCREATEROLE;

-- 2. Групова роль: дає CONNECT на службову базу postgres, щоб студент міг
--    виконати DROP DATABASE своєї бази з psql.
GRANT obs_students TO :"role";

-- 3. База в персональному слоті tablespace.
CREATE DATABASE :"db" OWNER :"role" TABLESPACE :"ts";

-- 4. КРИТИЧНО. Нова база отримує ACL '=Tc/owner', тобто CONNECT і TEMP для
--    PUBLIC. Без наступних двох рядків БУДЬ-ЯКИЙ студент підключається до
--    бази будь-якого іншого студента.
--    REVOKE мусить виконати ВЛАСНИК бази: obs_provisioner власником не є і
--    отримає лише 'WARNING: no privileges could be revoked' — мовчазний провал.
--    Саме тому тут SET ROLE. (Знайдено тестом фази 0.)
SET ROLE :"role";
REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
-- 5. Обслуговування бази з адмінки (З15б, VACUUM ANALYZE). Членство в
--    pg_maintain видається один раз на кластер (initdb/00-roles.sh), але
--    CONNECT — привілей САМОЇ бази, і після REVOKE вище сюди не пускає ніхто,
--    крім власника. Тому грант тут, і теж від імені власника: obs_provisioner
--    видати CONNECT на чужу базу не може.
--
--    ЧОМУ ЦЕ ЗАГОРНУТО В ПЕРЕВІРКУ ІСНУВАННЯ РОЛІ. Правильний порядок
--    розгортання — спочатку міграція кластера (upgrade-2026-09-d.sql або
--    перебудова образу postgres), і лише потім новий образ front. Якщо адмін
--    переплутає порядок, `GRANT ... TO obs_maintenance` дасть
--    `role "obs_maintenance" does not exist`, а через ON_ERROR_STOP=1 упаде
--    ВЕСЬ провізіонінг: перший студент, що зайшов, не отримає навіть бази.
--    Ціна помилки в порядку розгортання не має бути «система не працює» —
--    хай краще тимчасово не працює одна кнопка VACUUM в адмінці, поки
--    міграцію не докотили. Тому — перевірка, а не голий GRANT.
SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'obs_maintenance')::text AS has_maint \gset
\if :has_maint
GRANT CONNECT ON DATABASE :"db" TO obs_maintenance;
\else
\warn 'obs_maintenance не існує: CONNECT не видано, кнопка VACUUM ANALYZE в адмінці працювати не буде. Докотіть postgres/sql/upgrade-2026-09-d.sql.'
\endif
RESET ROLE;

-- 6. Ліміти рівня ролі в межах її бази. Обґрунтування цифр — docs/06-sizing.md.
ALTER ROLE :"role" IN DATABASE :"db" SET temp_tablespaces = :'ts';
ALTER ROLE :"role" IN DATABASE :"db" SET temp_file_limit = '64MB';
ALTER ROLE :"role" IN DATABASE :"db" SET statement_timeout = '30s';
ALTER ROLE :"role" IN DATABASE :"db" SET idle_in_transaction_session_timeout = '15min';
ALTER ROLE :"role" IN DATABASE :"db" SET default_tablespace = :'ts';
