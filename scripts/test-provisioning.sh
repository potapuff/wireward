#!/usr/bin/env bash
# =============================================================================
# Перевірка провізіонінгу та ізоляції студентів на живому кластері.
# Виконує повний цикл від імені obs_provisioner (НЕ superuser) і перевіряє,
# що студент замкнений у своїй базі й не може зняти ліміти.
#
#   PROV_PW=... PGHOST=... PGUSER=postgres ./scripts/test-provisioning.sh
#
# Створює u_t1/u_t2 + db_t1/db_t2 і прибирає їх за собою.
# =============================================================================
set -uo pipefail
cd "$(dirname "$0")/.."

SQLDIR=postgres/sql
PROV_PW="${PROV_PW:?задайте PROV_PW — пароль obs_provisioner}"
STU_PW="${STU_PW:-testpass}"
fail=0

ok()  { printf '  ok    %s\n' "$1"; }
bad() { printf '  FAIL  %s — %s\n' "$1" "$2"; fail=1; }
expect() {  # <опис> <ok|deny> <команда...>
  local desc="$1" mode="$2"; shift 2
  # Дивимось на ВЕСЬ вивід: psql кладе ERROR у перший рядок, а HINT/DETAIL — у наступні.
  local out; out="$("$@" 2>&1)"
  local last; last="$(printf '%s' "$out" | tail -1)"
  if [[ "$out" == *ERROR:* || "$out" == *FATAL:* ]]; then
    [[ "$mode" == deny ]] && ok "$desc — заборонено" || bad "$desc" "$(printf '%s' "$out" | head -1)"
  else
    [[ "$mode" == ok ]] && ok "$desc" || bad "$desc" "мало бути заборонено, отримали: $last"
  fi
}

cleanup() {
  for s in t1 t2; do
    PGPASSWORD="$PROV_PW" psql -qU obs_provisioner -d postgres >/dev/null 2>&1 \
      -c "SET ROLE u_$s" -c "DROP DATABASE IF EXISTS db_$s WITH (FORCE)" -c "RESET ROLE"
    PGPASSWORD="$PROV_PW" psql -qU obs_provisioner -d postgres >/dev/null 2>&1 \
      -c "DROP ROLE IF EXISTS u_$s"
  done
}
trap cleanup EXIT
cleanup

echo "1. Провізіонінг від імені obs_provisioner (без superuser)"
n=0
for s in t1 t2; do
  n=$((n+1)); ts=$(printf 'ts_%04d' "$n")
  out=$(PGPASSWORD="$PROV_PW" psql -v ON_ERROR_STOP=1 -qU obs_provisioner -d postgres \
        -v role="u_$s" -v db="db_$s" -v ts="$ts" -v pw="$STU_PW" \
        -f "$SQLDIR/provision_student.sql" 2>&1)
  [[ -z "$out" ]] && ok "u_$s / db_$s у $ts" || bad "u_$s" "$out"
done

echo
echo "2. ACL бази: PUBLIC не має бути в списку"
acl=$(psql -tAX -d postgres -c "select coalesce(array_to_string(datacl,','),'') from pg_database where datname='db_t1'")
# Запис PUBLIC в aclitem — це елемент БЕЗ імені ролі перед '=': `=Tc/u_t1`.
# Тому шукати треба саме елемент, а не підрядок: після появи obs_maintenance
# (З15б) datacl виглядає як `u_t1=CTc/u_t1,obs_maintenance=c/u_t1`, і наївний
# glob `*"=c/"*` знаходив '=c/' усередині імені ролі й падав на кожному
# провізіонінгу. Елемент PUBLIC може бути або першим (рядок починається з '='),
# або після коми (',=').
# Порожній datacl (NULL у каталозі) — теж провал, а не успіх: це «привілеї за
# замовчуванням», а вони для бази означають CONNECT+TEMPORARY для PUBLIC.
if [[ -z "$acl" ]]
  then bad "ACL db_t1" "datacl порожній — діють типові привілеї, тобто PUBLIC має CONNECT/TEMP"
elif [[ "$acl" == "="* || "$acl" == *",="* ]]
  then bad "ACL db_t1" "PUBLIC має привілеї: $acl"
  else ok "ACL db_t1 без PUBLIC ($acl)"; fi

echo
echo "3. Ліміти ролі застосовані"
for pair in "temp_file_limit|64MB" "statement_timeout|30s" "idle_in_transaction_session_timeout|15min"; do
  p="${pair%%|*}"; want="${pair##*|}"
  got=$(PGPASSWORD="$STU_PW" psql -tAX -U u_t1 -d db_t1 -c "show $p" 2>&1 | tail -1)
  [[ "$got" == "$want" ]] && ok "$p = $got" || bad "$p" "очікували $want, отримали $got"
done

echo
echo "4. Ізоляція"
export PGPASSWORD="$STU_PW"
expect "u_t1 -> своя db_t1"          ok   psql -tAX -U u_t1 -d db_t1 -c "select current_database()"
expect "u_t1 -> чужа db_t2"          deny psql -tAX -U u_t1 -d db_t2 -c "select 1"
expect "u_t2 -> чужа db_t1"          deny psql -tAX -U u_t2 -d db_t1 -c "select 1"
expect "u_t1 -> службова postgres"   ok   psql -tAX -U u_t1 -d postgres -c "select 1"
expect "u_t1 видаляє чужу базу"      deny psql -tAX -U u_t1 -d postgres -c "drop database db_t2"

echo
echo "4а. Переліки баз і ролей читаються студентом (З6)"
# ACL системного каталогу лежить у КОЖНІЙ базі окремо, тож перевіряти треба
# всередині свіжостворенної бази студента, а не в postgres. Успадковується
# він із template1 при CREATE DATABASE.
# pg_roles/pg_user: REVOKE FROM PUBLIC лишається (30-catalog-visibility.sh) —
# obs_students отримав доступ адресним GRANT-ом 10.09.2026, бо pgjdbc у
# DataGrip окремо шле запит про usesuper, і 42501 на ньому видно студентові
# як поломку.
expect "u_t1 бачить перелік ролей"   ok   psql -tAX -U u_t1 -d db_t1 -c "select rolname from pg_roles"
expect "запит pgjdbc про usesuper"   ok   psql -tAX -U u_t1 -d db_t1 -c \
  "select usesuper from pg_user where usename = current_user"
# pg_database навпаки МУСИТЬ читатися: REVOKE тут скасовано 06.09.2026, бо
# DataGrip на permission denied цього запиту зупиняє всю інтроспекцію і
# лишає дерево обʼєктів порожнім назавжди. Нижче — дослівні запити обох
# клієнтів із тієї діагностики.
expect "u_t1 бачить перелік баз"     ok   psql -tAX -U u_t1 -d db_t1 -c "select datname from pg_database"
expect "запит DataGrip до pg_database" ok psql -tAX -U u_t1 -d db_t1 -c \
  "select d.oid, d.datname, pg_get_userbyid(d.datdba) as owner, sd.description
     from pg_catalog.pg_database d
     left join pg_catalog.pg_shdescription sd
            on sd.objoid = d.oid and sd.classoid = 'pg_database'::regclass
    order by d.datname"
expect "запит DBeaver до pg_database"  ok psql -tAX -U u_t1 -d db_t1 -c \
  "select db.oid, db.* from pg_catalog.pg_database db where datname = current_database()"
# Звичайна робота не має від цього постраждати: сервер бере ці дані з
# системного кешу, а не через SQL із перевіркою прав.
expect "своя база далі відома"       ok   psql -tAX -U u_t1 -d db_t1 -c "select current_database()"
expect "розмір своєї бази читається" ok   psql -tAX -U u_t1 -d db_t1 -c "select pg_database_size(current_database())"

echo
echo "4б. obs_maintenance обслуговує, але не читає (З15б)"
# docs/05-security.md стверджує, що роль-обслуговувач дістає
# `permission denied for table` на звичайний SELECT. Досі це трималося на
# памʼяті автора — тепер на тесті. Без MAINT_PW секція просто пропускається,
# щоб скрипт лишався запускним і вручну.
if [[ -z "${MAINT_PW:-}" ]]; then
  echo "  skip  MAINT_PW не задано — перевірку obs_maintenance пропущено"
else
  PGPASSWORD="$STU_PW" psql -qX -U u_t1 -d db_t1 >/dev/null 2>&1 \
    -c "create table secret_data(i int)" -c "insert into secret_data values (42)"
  export PGPASSWORD="$MAINT_PW"
  expect "obs_maintenance -> db_t1 (CONNECT є)" ok \
    psql -tAX -U obs_maintenance -d db_t1 -c "select current_database()"
  expect "obs_maintenance читає дані студента" deny \
    psql -tAX -U obs_maintenance -d db_t1 -c "select * from secret_data"
  expect "obs_maintenance робить VACUUM ANALYZE" ok \
    psql -tAX -U obs_maintenance -d db_t1 -c "vacuum analyze secret_data"
  # F1 (аудит З15б). ANALYZE обчислює вирази функціональних індексів, тобто
  # ВИКОНУЄ код студента ось у цій сесії — а персональні ліміти студента
  # накладені через ALTER ROLE u_t1 IN DATABASE db_t1 і сюди не поширюються.
  # Тому ліміт мусить бути свій, на роль, і сесія має бачити саме його
  # (заміряно живцем: без нього один ANALYZE дав 817 МБ у pg_default).
  got=$(psql -tAX -U obs_maintenance -d db_t1 -c "show temp_file_limit" 2>&1 | tail -1)
  [[ "$got" == "64MB" ]] && ok "obs_maintenance: temp_file_limit = $got" \
    || bad "obs_maintenance: temp_file_limit" "очікували 64MB, отримали $got"
  expect "obs_maintenance знімає temp_file_limit" deny \
    psql -tAX -U obs_maintenance -d db_t1 -c "set temp_file_limit=-1"
  # F4: template1 більше не роздає CONNECT для PUBLIC. Провізіонінгу це не
  # заважає — template1 береться як ШАБЛОН (секція 1 вище це й доводить).
  expect "obs_maintenance -> template1" deny \
    psql -tAX -U obs_maintenance -d template1 -c "select 1"
  # І навпаки: службова база журналу для неї закрита (00-roles.sh, коментар
  # про obs.queries). pg_maintain діє в КОЖНІЙ базі, тож єдине, що тримає цю
  # межу, — відсутність CONNECT.
  expect "obs_maintenance -> службова observer" deny \
    psql -tAX -U obs_maintenance -d observer -c "select 1"
  export PGPASSWORD="$STU_PW"
fi

echo
echo "5. Обхід квоти та ескалація"
expect "CREATE TABLE ... TABLESPACE pg_default" deny psql -tAX -U u_t1 -d db_t1 -c "create table x(i int) tablespace pg_default"
expect "SET default_tablespace = pg_default"    deny psql -tAX -U u_t1 -d db_t1 -c "set default_tablespace=pg_default" -c "create table x2(i int)"
expect "CREATE TABLESPACE"                      deny psql -tAX -U u_t1 -d db_t1 -c "create tablespace evil location '/tmp'"
expect "SET temp_file_limit = -1"               deny psql -tAX -U u_t1 -d db_t1 -c "set temp_file_limit=-1"
expect "CREATE EXTENSION dblink"                deny psql -tAX -U u_t1 -d db_t1 -c "create extension dblink"
expect "CREATE EXTENSION postgres_fdw"          deny psql -tAX -U u_t1 -d db_t1 -c "create extension postgres_fdw"
expect "ALTER ROLE u_t1 SUPERUSER"              deny psql -tAX -U u_t1 -d db_t1 -c "alter role u_t1 superuser"
expect "CREATE DATABASE"                        deny psql -tAX -U u_t1 -d db_t1 -c "create database sneaky"
expect "CREATE EXTENSION pgcrypto (дозволено)"  ok   psql -tAX -U u_t1 -d db_t1 -c "create extension pgcrypto"
expect "студент видаляє СВОЮ базу"              ok   psql -tAX -U u_t1 -d postgres -c "drop database db_t1"

echo
[[ $fail -eq 0 ]] && echo "Провізіонінг та ізоляція: усі перевірки пройдено." \
                  || echo "Провізіонінг та ізоляція: є провали."
exit $fail
