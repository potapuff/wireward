#!/usr/bin/env bash
# =============================================================================
# Крок 4/4: ACL системного каталогу для студентів (З6 у docs/07-roadmap.md).
#
# Задум був сховати чужі бази й ролі: студент бачив у дереві DBeaver 51 базу і
# 87 ролей, доступу до них немає — проксі відмовляє, — але сам перелік показує,
# хто ще є в системі. Обидві частини задуму довелося скасувати (нижче — чому);
# лишився явний, адресний GRANT замість неявного PUBLIC.
#
# ПЕРЕЛІК БАЗ ЗАКРИТИ НЕ ВИЙШЛО — REVOKE на pg_database СКАСОВАНО 06.09.2026.
# Живе тестування викладача показало: DataGrip будує дерево обʼєктів запитом
#     select ... from pg_catalog.pg_database left join pg_shdescription ... order by ...
# і на `42501 permission denied` зупиняє ВСЮ подальшу інтроспекцію — жодного
# запиту до pg_class/pg_attribute вже не надсилає, дерево лишається порожнім
# назавжди. DBeaver ту саму помилку (на своєму
#     SELECT db.oid,db.* FROM pg_catalog.pg_database db WHERE datname=$1
# ) толерує і працює далі — тому проблему й не побачили на DBeaver.
# Студенти сидять у DataGrip, тож перелік баз повертаємо: ізоляція самих даних
# від цього не залежить (її тримають datacl і проксі), страждає лише
# невидимість переліку імен.
#
# ПЕРЕЛІК РОЛЕЙ ТЕЖ ПОВЕРНЕНО — 10.09.2026, та сама помилка, що й із
# pg_database. Тут довго стояло «pg_roles / pg_user лишаються закритими: для
# дерева обʼєктів вони не потрібні, власника бази клієнт бере через
# pg_get_userbyid(), а та функція ACL не перевіряє». Про дерево це правда, але
# воно не все: драйвер pgjdbc у DataGrip окремо, поза інтроспекцією, шле
#     select usesuper from pg_user where usename = current_user
# (спіймано в живому трафіку, docs/07-roadmap.md). На студентській базі це
# давало `42501 permission denied for table pg_user` просто в сеансі — помилка
# видима, виглядає так, ніби щось зламалося, і лякає студента. Ізоляція даних
# від переліку ролей не залежить (її тримають datacl і проксі), тож ціна
# закритого каталогу вища за користь.
#
# ЧОМУ САМЕ REVOKE, а не RULE чи RLS. PostgreSQL відмовляє на системному
# каталозі навіть суперкористувачеві: CREATE RULE дає "permission denied",
# ENABLE ROW LEVEL SECURITY — "pg_database is a system catalog". REVOKE від
# PUBLIC — це той самий прийом, яким сам PostgreSQL закриває pg_authid.
#
# ЧОМУ В template1, А НЕ В provision_student.sql. ACL каталогу зберігається В
# КОЖНІЙ БАЗІ ОКРЕМО, у власному рядку pg_class. REVOKE, виконаний в одній
# базі, на інші не впливає взагалі. Крім того, REVOKE на каталозі має право
# зробити лише власник, тобто суперкористувач, — а провізіонінг виконує
# obs_provisioner. Обидві причини ведуть в одне місце: template1 на етапі
# ініціалізації, звідки нові бази успадковують ACL фізично.
#
# ЧОМУ НЕ В observer І НЕ В postgres:
#   observer — obs_app читає там статистику;
#   postgres — provisioning.database_exists() питає pg_database саме там від
#              імені obs_provisioner. Обидві бази вже існують на цей момент,
#              тож зміна template1 їх не зачіпає — і це навмисно.
# =============================================================================
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname template1 --no-psqlrc <<'SQL'
REVOKE SELECT ON pg_catalog.pg_roles FROM PUBLIC;
REVOKE SELECT ON pg_catalog.pg_user  FROM PUBLIC;

-- Перелік баз і ролей лишається читабельним для студентів (див. шапку: інакше
-- DataGrip не будує дерево взагалі, а на pg_user падає з видимою помилкою).
-- GRANT саме груповій ролі obs_students, а не назад у PUBLIC: у PUBLIC воно
-- вже є за замовчуванням, і явний GRANT робить намір видимим у relacl —
-- випадковий REVOKE FROM PUBLIC більше не зламає клієнтів. REVOKE вище
-- лишається навмисно: він фіксує, що доступ дається адресно, а не «усім».
GRANT SELECT ON pg_catalog.pg_database, pg_catalog.pg_roles, pg_catalog.pg_user
   TO obs_students;

-- Службовим ролям перелік потрібен, і майбутній ролі викладача теж, якщо вона
-- колись підключатиметься до баз студентів напряму.
GRANT SELECT ON pg_catalog.pg_database, pg_catalog.pg_roles, pg_catalog.pg_user
   TO obs_provisioner, obs_app, obs_shipper;
SQL

echo "observer: ACL каталогу в template1 — pg_roles/pg_user відкликано в PUBLIC (pg_database лишається в PUBLIC як був), усі три видано адресно obs_students та службовим ролям"
