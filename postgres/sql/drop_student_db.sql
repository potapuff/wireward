-- =============================================================================
-- Видалення бази студента (кнопка «Перестворити БД», або прибирання після
-- відрахування). Виконує front від імені obs_provisioner.
--
-- obs_provisioner НЕ власник бази, тому потрібен SET ROLE. Це працює завдяки
-- ALTER ROLE obs_provisioner SET createrole_self_grant = 'set' — див.
-- postgres/initdb/00-roles.sh і docs/02-decisions.md, ADR-007.
-- =============================================================================
SET ROLE :"role";
DROP DATABASE IF EXISTS :"db" WITH (FORCE);
RESET ROLE;
