#!/usr/bin/env bash
# =============================================================================
# Крок 1/4: службові ролі та глобальні заборони кластера.
# Має виконатися ДО 10-observer-schema.sql, бо той робить GRANT на ці ролі.
# =============================================================================
set -euo pipefail

read_secret() { cat "/run/secrets/$1"; }
PW_PROVISIONER="$(read_secret pg_provisioner_password)"
PW_APP="$(read_secret pg_app_password)"
PW_SHIPPER="$(read_secret pg_shipper_password)"
PW_MAINTENANCE="$(read_secret pg_maintenance_password)"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres --no-psqlrc <<SQL
-- Ключовий рядок усієї схеми з квотами. Без нього студент пише
-- CREATE TABLE ... TABLESPACE pg_default і обходить квоту повністю.
REVOKE CREATE ON TABLESPACE pg_default FROM PUBLIC;

-- Службова база postgres: підключатися можуть лише студенти (щоб мати змогу
-- виконати DROP DATABASE своєї бази) і provisioner. Більше ніхто.
-- УВАГА: REVOKE ... FROM PUBLIC забирає CONNECT і в службових ролей теж,
-- тому кожній з них треба повернути його явно (знайдено тестом фази 0).
REVOKE ALL ON DATABASE postgres FROM PUBLIC;

-- База observer: доступ лише застосунку і shipper'у.
REVOKE ALL ON DATABASE observer FROM PUBLIC;

-- template1 роздає CONNECT для PUBLIC за замовчуванням, і цього ніхто не
-- прибирає. Підключатися туди не потрібно нікому: провізіонінг використовує
-- template1 як ШАБЛОН (datistemplate=true + CREATEDB), а не як базу, тож
-- CREATE DATABASE від obs_provisioner працює й без CONNECT. Натомість
-- відкритий CONNECT — це зайва база, у якій будь-яка роль (зокрема
-- obs_maintenance із pg_maintain) може щось робити повз наш контроль.
-- Загальнокластерна гігієна; знайдено аудитом З15б (F4).
REVOKE CONNECT ON DATABASE template1 FROM PUBLIC;

-- Групова роль студентів. Особисті ролі u_* стають її членами при провізіонінгу.
CREATE ROLE obs_students NOLOGIN;
GRANT CONNECT ON DATABASE postgres TO obs_students;

-- Провізіонінг обходиться БЕЗ superuser: CREATEDB + CREATEROLE достатньо,
-- бо всі tablespace'и вже створені (20-slots.sh). CREATEROLE у PG16+ дає
-- владу лише над ролями, які ця роль створила сама.
CREATE ROLE obs_provisioner LOGIN CREATEDB CREATEROLE
    PASSWORD '${PW_PROVISIONER}' CONNECTION LIMIT 4;
GRANT CONNECT ON DATABASE postgres TO obs_provisioner;

-- PostgreSQL 16+ авто-видає створену роль її творцеві з ADMIN OPTION, але БЕЗ
-- права SET ROLE. Тим часом CREATE DATABASE ... OWNER u_x і подальший
-- DROP DATABASE вимагають саме SET ROLE. Керує цим createrole_self_grant.
-- Беремо тільки 'set', не 'inherit': provisioner має вміти на час операції
-- стати студентом, але не отримувати його права неявно, просто так.
ALTER ROLE obs_provisioner SET createrole_self_grant = 'set';

-- Щоб provisioner міг зараховувати студентів у групову роль.
GRANT obs_students TO obs_provisioner WITH ADMIN OPTION;

-- temp_file_limit — параметр рівня SUSET, звичайна роль його не виставить
-- навіть через ALTER ROLE. PostgreSQL 15+ дозволяє делегувати рівно один
-- параметр, без роздачі superuser. Саме він і робить квоту на тимчасові
-- файли непереборною для студента (знайдено тестом фази 0).
GRANT SET ON PARAMETER temp_file_limit TO obs_provisioner;

-- Веб-застосунок: читає і пише метадані, журналу не змінює.
CREATE ROLE obs_app      LOGIN PASSWORD '${PW_APP}'     CONNECTION LIMIT 20;
-- Shipper: єдина роль, якій дозволено писати в журнал. Нею ж проксі читає
-- студента при вході й перевіряє квоту, тож ліміт має перевищувати розмір
-- пулу проксі (PGPROXY_POOL_MAX, типово 8) із запасом. При ліміті 4 під
-- 40 одночасними входами автентифікація падала з 53300 — знайдено
-- навантажувальним тестом, а не оглядом коду.
CREATE ROLE obs_shipper  LOGIN PASSWORD '${PW_SHIPPER}' CONNECTION LIMIT 12;
GRANT CONNECT ON DATABASE observer TO obs_app, obs_shipper;

-- Обслуговування баз студентів (З15б): примусовий VACUUM ANALYZE з адмінки.
-- Окрема роль, а не obs_app і не superuser: VACUUM чужої таблиці вимагає
-- або володіння нею, або членства у вбудованій pg_maintain (PG17+), яка дає
-- рівно VACUUM, ANALYZE, CLUSTER, REINDEX, REFRESH MATERIALIZED VIEW і
-- LOCK TABLE — і НЕ дає читати дані студента (перевіряє test-provisioning.sh,
-- секція 4б).
-- Ліміт 2 — це рідка ручна дія адміністратора, а не робочий шлях; малий
-- ліміт заодно не дає випадковим циклом запустити вакуум на всіх базах разом.
CREATE ROLE obs_maintenance LOGIN PASSWORD '${PW_MAINTENANCE}' CONNECTION LIMIT 2;

-- pg_maintain — ГЛОБАЛЬНА вбудована роль: членство видається один раз на
-- кластер і діє в усіх базах, тож для майбутніх баз студентів тут більше
-- нічого не треба. А от CONNECT — привілей самої бази, і його доводиться
-- давати окремо на кожну базу (provision_student.sql, крок 5): персональна
-- база після REVOKE ALL ... FROM PUBLIC не пускає нікого, крім власника.
GRANT pg_maintain TO obs_maintenance;

-- ГОЛОВНИЙ ЗАПОБІЖНИК ЦІЄЇ РОЛІ (аудит З15б, F1). ANALYZE обчислює вирази
-- функціональних індексів, тобто ВИКОНУЄ КОД СТУДЕНТА в сесії обслуговувача.
-- Студент може оголосити функцію IMMUTABLE збрехавши і змусити її матеріалізувати
-- гігабайти. Персональні ліміти студента накладені через
-- ALTER ROLE u_x IN DATABASE db_x SET ... і на цю сесію НЕ діють: перевірено
-- живцем — один ANALYZE дав 817 МБ тимчасових файлів за 22 с у pg_default,
-- на тому ж томі, де WAL і база observer.
-- 64 МБ — та сама стеля, що й у студента (provision_student.sql): обслуговування
-- чесної бази до 50 МБ у неї вкладається із запасом, а зловмисний вираз
-- обривається на 53400 замість того, щоб доїсти диск.
-- temp_file_limit — SUSET, тож сам obs_maintenance цього зняти не може; у
-- нього немає ні superuser, ні GRANT SET ON PARAMETER (на відміну від
-- obs_provisioner, якому цей параметр делеговано вище).
ALTER ROLE obs_maintenance SET temp_file_limit = '64MB';

-- CONNECT на службові бази обслуговувачу НЕ дається навмисно: разом із
-- pg_maintain це відкрило б йому обслуговування журналу obs.queries, до якого
-- він стосунку не має (автовакуум журналу — ADR-022, роль obs_shipper).

-- pg_tablespace_size вимагає CREATE на tablespace або членства в
-- pg_read_all_stats. Друге вужче: дає читати статистику й нічого не дає
-- створювати. Потрібне кабінету студента, щоб показати смужку квоти.
GRANT pg_read_all_stats TO obs_app;
SQL

echo "observer: службові ролі створено"
