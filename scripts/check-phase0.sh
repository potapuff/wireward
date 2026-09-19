#!/usr/bin/env bash
# Критерії готовності фази 0 (docs/07-roadmap.md). Виконується проти живого кластера.
set -uo pipefail
cd "$(dirname "$0")/.."

fail=0
q() { docker compose exec -T postgres psql -U postgres -d observer -tAX -c "$1" 2>/dev/null; }
# ACL системного каталогу зберігається В КОЖНІЙ БАЗІ ОКРЕМО, тож перевірити
# template1 з бази observer неможливо — тільки підключившись до нього.
qt() { docker compose exec -T postgres psql -U postgres -d template1 -tAX -c "$1" 2>/dev/null; }
check() { # опис, очікуване, отримане
  if [[ "$2" == "$3" ]]; then printf '  ok   %s\n' "$1"
  else printf '  FAIL %s (очікували %s, отримали %s)\n' "$1" "$2" "$3"; fail=1; fi
}

echo "Фаза 0 — перевірка"

check "кластер відповідає" "1" "$(q 'select 1')"
check "версія PostgreSQL 18" "18" "$(q "select split_part(current_setting('server_version'),'.',1)")"
check "JIT вимкнено" "off" "$(q "select current_setting('jit')")"
check "паралелізм у gather вимкнено" "0" "$(q "select current_setting('max_parallel_workers_per_gather')")"
check "PUBLIC не може створювати в pg_default" "f" \
      "$(q "select has_tablespace_privilege('public','pg_default','create')")"
check "схема obs існує" "1" "$(q "select count(*) from information_schema.schemata where schema_name='obs'")"
check "obs.queries партиційована" "p" \
      "$(q "select relkind from pg_class where oid='obs.queries'::regclass")"
check "партиція поточного місяця є" "1" \
      "$(q "select count(*) from pg_class where relname='queries_'||to_char(current_date,'YYYY_MM')")"

# Повнота схеми. Раніше тут перевірялося лише партиціонування, і через це
# непомітно зникли колонка is_catalog та всі пʼять індексів obs.queries:
# журнал мовчки не наповнювався, бо COPY падав на неіснуючій колонці.
check "колонки obs.queries на місці" "0" \
      "$(q "select count(*) from (values ('is_catalog'),('is_service'),('sql_fingerprint'),('sql_truncated'),
                                         ('sqlstate'),('error_position'),('rows_affected'),
                                         ('protocol'),('params')) v(c)
            where c not in (select column_name from information_schema.columns
                            where table_schema='obs' and table_name='queries')")"
# Перелік РОЛЕЙ мусить бути ВІДКРИТИМ для obs_students — pgjdbc у DataGrip
# окремо шле `select usesuper from pg_user where usename = current_user`, і
# 42501 на ньому видно студентові як поломку (10.09.2026, З6). REVOKE FROM
# PUBLIC при цьому лишається — доступ видано адресним GRANT-ом obs_students,
# а не назад у PUBLIC (30-catalog-visibility.sh); обидва боки цієї вимоги
# можуть зникнути непомітно, тож перевіряємо обидва. Перевіряти треба саме
# template1: ACL каталогу лежить у кожній базі окремо.
check "template1: obs_students читає pg_roles" "t" \
      "$(qt "select has_table_privilege('obs_students','pg_catalog.pg_roles','SELECT')")"
check "template1: obs_students читає pg_user" "t" \
      "$(qt "select has_table_privilege('obs_students','pg_catalog.pg_user','SELECT')")"
check "template1: PUBLIC не читає pg_roles" "f" \
      "$(qt "select has_table_privilege('public','pg_catalog.pg_roles','SELECT')")"
check "template1: PUBLIC не читає pg_user" "f" \
      "$(qt "select has_table_privilege('public','pg_catalog.pg_user','SELECT')")"
# Перелік БАЗ так само мусить бути ВІДКРИТИМ — і ця вимога теж
# може зникнути непомітно. REVOKE SELECT ON pg_database від PUBLIC ламає
# DataGrip повністю: він зупиняє всю інтроспекцію на permission denied цього
# запиту і більше не питає ні pg_class, ні pg_attribute (знайдено живим
# тестуванням 06.09.2026, див. З6). Перевіряємо не PUBLIC, а саме
# obs_students — так вимога лишається виконаною навіть якщо хтось знову
# закриє PUBLIC.
check "template1: obs_students читає pg_database" "t" \
      "$(qt "select has_table_privilege('obs_students','pg_catalog.pg_database','SELECT')")"

# Непорожня DEFAULT-партиція означає, що записи прийшли за місяць, для якого
# партиції немає. Це не косметика: далі створити ту партицію вже НЕ ВИЙДЕ,
# і обслуговування журналу зупиниться. Одного разу так і сталося непомітно.
check "DEFAULT-партиція порожня" "0" "$(q 'select count(*) from obs.queries_default')"
# queries_fingerprint_idx свідомо НЕМАЄ: виміряно, що він не пришвидшує
# групування, зате коштує чверть журналу (docs/08-scale.md).
check "індекси obs.queries на місці" "0" \
      "$(q "select count(*) from (values ('queries_student_time_idx'),('queries_session_idx'),
                                         ('queries_errors_idx'),('queries_brin_idx')) v(i)
            where i not in (select indexname from pg_indexes where schemaname='obs')")"
# Автовакуум партицій журналу. Параметри не успадковуються від батька (PG18
# взагалі не дає storage parameters на партиційованій таблиці), тож кожна
# партиція має отримати їх окремо — і партиції, створені ДО того, як ці
# параметри зʼявилися в схемі, лишаються без них мовчки: журнал працює,
# просто autovacuum по ньому не встигає, а BRIN не отримує підсумків.
# Звіряємо кожну наявну партицію з тим, що каже obs.journal_autovacuum_opts().
# Якщо тут FAIL на працюючому сервері — це `make apply-autovacuum`.
check "автовакуум на всіх партиціях obs.queries" "0" \
      "$(q "with opts as (
              select btrim(split_part(kv,'=',1), E' \t\n\r') as k,
                     btrim(split_part(kv,'=',2), E' \t\n\r') as v
                from regexp_split_to_table(obs.journal_autovacuum_opts(), ',') kv
               where btrim(kv, E' \t\n\r') <> ''
            ), parts as (
              select coalesce(c.reloptions,'{}') as ro, coalesce(t.reloptions,'{}') as tro
                from pg_inherits i
                join pg_class c on c.oid = i.inhrelid
                left join pg_class t on t.oid = c.reltoastrelid
               where i.inhparent = 'obs.queries'::regclass
            )
            select count(*) from parts p, opts o
             where case when o.k like 'toast.%'
                        then replace(o.k,'toast.','')||'='||o.v <> all(p.tro)
                        else o.k||'='||o.v <> all(p.ro) end")"
check "obs.analyze_journal() є (нічний cron)" "1" \
      "$(q "select count(*) from pg_proc p join pg_namespace n on n.oid = p.pronamespace
             where n.nspname='obs' and p.proname='analyze_journal'")"

# Кеш привʼязки «відбиток → лаба» і розтин для треймапа (З13/З14). Перевіряємо
# так само поколонково, як obs.queries: колонка, що зникла з похідної таблиці,
# ламає не одразу, а на першому ж наступному проході фонового циклу — тобто
# треймап просто лишається порожнім, і ніхто не бачить чому.
check "колонки obs.fingerprint_labs на місці" "0" \
      "$(q "select count(*) from (values ('student_id'),('sql_fingerprint'),('lab_id'),
                                         ('score'),('matched_at')) v(c)
            where c not in (select column_name from information_schema.columns
                            where table_schema='obs' and table_name='fingerprint_labs')")"
check "індекси obs.fingerprint_labs на місці" "0" \
      "$(q "select count(*) from (values ('fingerprint_labs_pkey'),('fingerprint_labs_lab_idx')) v(i)
            where i not in (select indexname from pg_indexes where schemaname='obs')")"
# Без цього гранту фоновий цикл падає, а обидва треймапи мовчки порожні:
# похідні дані «просто ще не порахувалися».
check "obs_app наповнює obs.fingerprint_labs" "t" \
      "$(q "select has_table_privilege('obs_app','obs.fingerprint_labs','insert')
                 and has_table_privilege('obs_app','obs.fingerprint_labs','delete')")"
check "obs.lab_activity_daily() є (треймап)" "1" \
      "$(q "select count(*) from pg_proc p join pg_namespace n on n.oid = p.pronamespace
             where n.nspname='obs' and p.proname='lab_activity_daily'")"
check "obs_app виконує obs.lab_activity_daily()" "t" \
      "$(q "select has_function_privilege('obs_app','obs.lab_activity_daily(timestamptz,text)','execute')")"
# З15а: адміністратор змінює квоту з адмінки, і подія мусить лягати в журнал.
# Якщо значення немає в CHECK, UPDATE квоти пройде, а INSERT події впаде —
# квота зміниться без сліду.
check "подія quota_changed дозволена (З15а)" "1" \
      "$(q "select count(*) from pg_constraint
             where conrelid='obs.db_events'::regclass and contype='c'
               and pg_get_constraintdef(oid) like '%quota_changed%'")"
check "obs_app може міняти квоту (З15а)" "t" \
      "$(q "select has_table_privilege('obs_app','obs.ts_slots','update')")"
# Нуль у quota_bytes — не «без квоти», а тиха відмова: і проксі, і фоновий job
# трактують 0 як «квоти немає». З появою ручного вводу це один друк в адмінці.
check "quota_bytes не може бути нулем" "1" \
      "$(q "select count(*) from pg_constraint
             where conrelid='obs.ts_slots'::regclass and contype='c'
               and pg_get_constraintdef(oid) like '%quota_bytes > 0%'")"
# З15б: примусовий VACUUM ANALYZE на базі студента з адмінки. Та сама логіка,
# що і в quota_changed: немає значення в CHECK — вакуум відпрацює, а INSERT
# події впаде, і адміністратор побачить помилку вже ПІСЛЯ виконаної роботи.
check "подія vacuum_run дозволена (З15б)" "1" \
      "$(q "select count(*) from pg_constraint
             where conrelid='obs.db_events'::regclass and contype='c'
               and pg_get_constraintdef(oid) like '%vacuum_run%'")"
# Роль «викладач» (скорочена адмінка: лише свої групи). Кожна з чотирьох умов
# ламається тихо і по-різному: без is_teacher адмінка вважає викладача
# студентом; без obs.teacher_groups чи без гранту збереження призначення падає
# з 42501 вже після того, як адміністратор його зробив; без індексу за
# group_name зворотний запит «хто веде цю групу» йде seqscan-ом.
check "колонка obs.students.is_teacher на місці" "1" \
      "$(q "select count(*) from information_schema.columns
            where table_schema='obs' and table_name='students' and column_name='is_teacher'")"
check "колонки obs.teacher_groups на місці" "0" \
      "$(q "select count(*) from (values ('teacher_id'),('group_name'),('granted_at')) v(c)
            where c not in (select column_name from information_schema.columns
                            where table_schema='obs' and table_name='teacher_groups')")"
check "індекси obs.teacher_groups на місці" "0" \
      "$(q "select count(*) from (values ('teacher_groups_pkey'),('teacher_groups_group_idx')) v(i)
            where i not in (select indexname from pg_indexes where schemaname='obs')")"
check "obs_app веде obs.teacher_groups" "t" \
      "$(q "select has_table_privilege('obs_app','obs.teacher_groups','select')
                 and has_table_privilege('obs_app','obs.teacher_groups','insert')
                 and has_table_privilege('obs_app','obs.teacher_groups','delete')")"
# UPDATE не дано навмисно: обидві змістовні колонки в PK, «змінити рядок» тут
# не існує як операція. Якщо грант колись розширять — це має бути помітно.
check "obs_app не оновлює obs.teacher_groups" "f" \
      "$(q "select has_table_privilege('obs_app','obs.teacher_groups','update')")"
# Bootstrap першого адміністратора. Без тригера свіжий стенд не має жодного
# is_admin, а проставити його можна лише через psql на самому сервері — тобто
# розгортання «пройшло успішно», а зайти в адмінку нікому.
check "тригер students_bootstrap_admin є" "1" \
      "$(q "select count(*) from pg_trigger where tgrelid='obs.students'::regclass
              and tgname='students_bootstrap_admin' and not tgisinternal")"
# На заселеній базі тригер уже не спрацює ніколи, тож відсутність
# адміністратора тут — стан, з якого система сама не вийде.
check "адміністратор у базі є" "true" \
      "$(q "select case when not exists (select 1 from obs.students)
                          or exists (select 1 from obs.students where is_admin)
                     then 'true' else 'false' end")"

# Модуль оцінок прибрано цілком (ADR-024, З28): таблиць не має бути ні на
# свіжому стенді (initdb), ні на оновленому (upgrade-2026-09-i.sql). Перевірка
# двостороння — саме тут раніше розходилися initdb і кластер після апгрейду.
check "таблиць модуля оцінок немає (ADR-024)" "0" \
      "$(q "select count(*) from information_schema.tables
            where table_schema='obs' and table_name in ('assessments','admin_views')")"
check "індексів модуля оцінок немає" "0" \
      "$(q "select count(*) from pg_indexes where schemaname='obs'
              and (indexname like 'assessments%' or indexname like 'admin_views%')")"

check "ланцюжка хешів немає (ADR-015)" "0" \
      "$(q "select count(*) from information_schema.columns
            where table_schema='obs' and table_name='queries'
              and column_name in ('prev_hash','row_hash')")"
check "зайвого індексу за відбитком немає" "0" \
      "$(q "select count(*) from pg_indexes where schemaname='obs' and indexname='queries_fingerprint_idx'")"

slots_db=$(q "select count(*) from obs.ts_slots")
slots_pg=$(q "select count(*) from pg_tablespace where spcname like 'ts_%'")
check "слоти в каталозі = слоти в кластері" "$slots_db" "$slots_pg"
check "слоти створені" "true" "$([[ ${slots_db:-0} -gt 0 ]] && echo true || echo false)"

check "obs_provisioner без superuser" "f" "$(q "select rolsuper from pg_roles where rolname='obs_provisioner'")"
check "obs_provisioner має CREATEDB" "t" "$(q "select rolcreatedb from pg_roles where rolname='obs_provisioner'")"
check "obs_app не пише в журнал" "f" \
      "$(q "select has_table_privilege('obs_app','obs.queries','update')")"
check "obs_app не видаляє журнал" "f" \
      "$(q "select has_table_privilege('obs_app','obs.queries','delete')")"
check "PUBLIC не підключається до postgres" "f" \
      "$(q "select has_database_privilege('public','postgres','connect')")"
check "PUBLIC не підключається до observer" "f" \
      "$(q "select has_database_privilege('public','observer','connect')")"
# Наступні три знайдені тестом фази 0; без них провізіонінг мовчки ламається.
check "obs_provisioner має CONNECT на postgres" "t" \
      "$(q "select has_database_privilege('obs_provisioner','postgres','connect')")"
check "obs_provisioner може SET ROLE у створені ролі" "set" \
      "$(q "select setconfig[1] from pg_db_role_setting s join pg_roles r on r.oid=s.setrole where r.rolname='obs_provisioner'" | sed 's/.*=//')"
check "obs_provisioner має ADMIN на obs_students" "t" \
      "$(q "select admin_option from pg_auth_members am join pg_roles r on r.oid=am.roleid join pg_roles m on m.oid=am.member where r.rolname='obs_students' and m.rolname='obs_provisioner'")"
check "obs_provisioner може виставляти temp_file_limit" "t" \
      "$(q "select has_parameter_privilege('obs_provisioner','temp_file_limit','set')")"

# Обслуговувач баз студентів (З15б). Кожна з чотирьох умов ламається тихо:
# без ролі чи без pg_maintain кнопка «VACUUM» дає 42501 вже на самій команді,
# без CONNECT — на підключенні, а зайвий CONNECT на observer віддав би цій
# ролі обслуговування журналу, до якого вона стосунку не має.
check "obs_maintenance існує" "1" \
      "$(q "select count(*) from pg_roles where rolname='obs_maintenance'")"
check "obs_maintenance без superuser" "f" \
      "$(q "select rolsuper from pg_roles where rolname='obs_maintenance'")"
check "obs_maintenance у pg_maintain" "t" \
      "$(q "select pg_has_role('obs_maintenance','pg_maintain','usage')")"
# Бази перебираються всі одразу: грант, забутий на одній базі, інакше
# помічається лише тоді, коли адміністратор натисне кнопку саме тому студенту.
check "obs_maintenance має CONNECT на всі бази студентів" "0" \
      "$(q "select count(*) from pg_database
             where datallowconn and datname like 'db\\_%'
               and not has_database_privilege('obs_maintenance', datname, 'connect')")"
check "obs_maintenance не підключається до observer" "f" \
      "$(q "select has_database_privilege('obs_maintenance','observer','connect')")"
# F1 (аудит З15б): ANALYZE обчислює вирази функціональних індексів, тобто
# ВИКОНУЄ код студента в сесії обслуговувача, куди персональні ліміти
# ALTER ROLE u_x IN DATABASE db_x не дістають. Без цього рядка кнопка «VACUUM»
# на брехливо-IMMUTABLE функції з'їдає диск під WAL (заміряно 817 МБ за 22 с).
check "obs_maintenance обмежений temp_file_limit" "temp_file_limit=64MB" \
      "$(q "select setconfig[1] from pg_db_role_setting s join pg_roles r on r.oid=s.setrole
             where r.rolname='obs_maintenance' and s.setdatabase=0
               and s.setconfig[1] like 'temp_file_limit=%'")"
# Роль не має права зняти це з себе: temp_file_limit — SUSET, а делегування
# через GRANT SET ON PARAMETER видано лише obs_provisioner.
check "obs_maintenance не може змінити temp_file_limit" "f" \
      "$(q "select has_parameter_privilege('obs_maintenance','temp_file_limit','set')")"
# Воркерам автовакууму роль не призначиш — той самий вектор через них
# існував і БЕЗ кнопки в адмінці. Накриває лише глобальне значення.
check "глобальний temp_file_limit виставлено" "786432" \
      "$(q "select setting from pg_settings where name='temp_file_limit'")"
# F4 (аудит З15б): typові CONNECT для PUBLIC у template1 лишаються після
# initdb; провізіонінгу вони не потрібні (template1 — шаблон, не база).
check "PUBLIC не підключається до template1" "f" \
      "$(q "select has_database_privilege('public','template1','connect')")"
check "obs_maintenance не підключається до template1" "f" \
      "$(q "select has_database_privilege('obs_maintenance','template1','connect')")"

# Офсайт-архів WAL (ADR-022). Вимкнений архів — законний стан (локальний
# стенд, сервер без сховища), і сам по собі не помилка. А от увімкнений
# archive_mode із archive_command, який мовчки повертає 0 або валиться, — це
# рівно та тиха поломка, заради якої тут узагалі є перевірки: сегменти
# накопичуються в pg_wal до заповнення диска, а «бекап є».
# Умова «архів увімкнено» однакова в health.sh, backup-s3.sh і тут: `on` АБО
# `always`. Раніше тут перевірялося `== off`, і на `always` перевірка мовчки
# вважала архів увімкненим, тоді як health.sh — вимкненим.
arch_mode="$(q "select current_setting('archive_mode')")"
if [[ "$arch_mode" != "on" && "$arch_mode" != "always" ]]; then
  printf '  ok   архів WAL вимкнено (офсайт не налаштований, ADR-022)\n'
else
  check "archive_command веде на wal-g" "1" \
        "$(q "select case when current_setting('archive_command') like '%walg-archive.sh%' then 1 else 0 end")"
  # last_failed_wal новіший за last_archived_wal означає, що архіватор стоїть
  # ПРОСТО ЗАРАЗ, а не колись спіткнувся і наздогнав.
  check "архіватор не відстає" "0" \
        "$(q "select case when last_failed_wal is not null
                       and (last_archived_wal is null or last_failed_wal > last_archived_wal)
                     then 1 else 0 end from pg_stat_archiver")"
  printf '  ..   останній заархівований сегмент: %s\n' \
        "$(q "select coalesce(last_archived_wal||' о '||to_char(last_archived_time,'DD.MM HH24:MI'),'жодного')
              from pg_stat_archiver")"
fi

echo
if [[ $fail -eq 0 ]]; then echo "Фаза 0: усі критерії виконано."
else echo "Фаза 0: є невиконані критерії."; fi
exit $fail
