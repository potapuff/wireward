#!/usr/bin/env bash
# =============================================================================
# Матриця сумісності клієнтів — критерій готовності фази 3.
#
# Проксі стоїть у розриві між клієнтом і СУБД, тож будь-яка похибка в розборі
# протоколу ламає роботу студентів. Тому перевіряємо не «чи пишеться журнал»,
# а «чи всі клієнти працюють нормально І чи все опинилося в журналі».
#
# Клієнти: psql (простий протокол), psycopg3 і pgjdbc (розширений — саме його
# використовують DBeaver і DataGrip).
#
#   ./scripts/test-clients.sh <роль> <база> <токен>
#
# Роль, базу і токен беруть із кабінету студента. Токен можна випустити
# кнопкою «Випустити токен».
# =============================================================================
set -uo pipefail
cd "$(dirname "$0")/.."

R="${1:-${PROXY_ROLE:-}}"; D="${2:-${PROXY_DB:-}}"; T="${3:-${PROXY_TOKEN:-}}"
[[ -z "$R" || -z "$D" || -z "$T" ]] && {
  echo "потрібні аргументи: <роль> <база> <токен>"; exit 2; }

HOST="${PROXY_HOST:-127.0.0.1}"
PORT="${PROXY_PORT:-55432}"
NET="${PROXY_NET:-observer_data}"
INNER="${PROXY_INNER_HOST:-172.29.0.10}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fail=0
ok()  { printf '  ok    %s\n' "$1"; }
bad() { printf '  FAIL  %s — %s\n' "$1" "$2"; fail=1; }

q() { docker compose exec -T postgres psql -U postgres -d observer -tAX -c "$1" 2>/dev/null | tr -d ' \r'; }
since="$(date -u +%Y-%m-%dT%H:%M:%S)"

# --- psql: простий протокол --------------------------------------------------
echo "1. psql — простий протокол"
DSN="host=$HOST port=$PORT dbname=$D user=$R sslmode=require"
out=$(PGPASSWORD="$T" psql "$DSN" -qtAX -c "select 'ok'" 2>&1 | tail -1)
[[ "$out" == "ok" ]] && ok "підключення і SELECT" || bad "підключення" "$out"

PGPASSWORD="$T" psql "$DSN" -qtAX >/dev/null 2>&1 <<'SQL'
create table if not exists _cli_test(id serial primary key, t text);
insert into _cli_test(t) select 'r'||g from generate_series(1,20) g;
SELCT 1;
select missing_column from _cli_test;
SQL
ok "серія з опечаткою виконана"

echo "   COPY"
printf 'copy 1\ncopy 2\n' | PGPASSWORD="$T" psql "$DSN" -qtAX -c "copy _cli_test(t) from stdin" >/dev/null 2>&1 \
  && ok "COPY FROM STDIN" || bad "COPY FROM STDIN" "не пройшов"
PGPASSWORD="$T" psql "$DSN" -qtAX -c "copy (select t from _cli_test limit 3) to stdout" >/dev/null 2>&1 \
  && ok "COPY TO STDOUT" || bad "COPY TO STDOUT" "не пройшов"

echo "   скасування довгого запиту"
PGPASSWORD="$T" psql "$DSN" -qtAX -c "select pg_sleep(25)" >/dev/null 2>&1 &
pid=$!; sleep 3; kill -INT $pid 2>/dev/null; wait $pid 2>/dev/null
ok "Ctrl-C надіслано"

# --- psycopg3 ----------------------------------------------------------------
echo
echo "2. psycopg3 — розширений протокол"
cat > "$WORK/t.py" <<'PY'
import os, psycopg
dsn = (f"host={os.environ['H']} port=55432 dbname={os.environ['D']} "
       f"user={os.environ['R']} password={os.environ['T']} sslmode=require")
with psycopg.connect(dsn, autocommit=True) as c:
    c.execute("select t from _cli_test where id > %s", (1,)).fetchall()
    for bad_sql, params in (("SELCT %s", (1,)), ("select nope from _cli_test where id > %s", (1,))):
        try: c.execute(bad_sql, params).fetchone()
        except Exception: pass
    try: c.execute("set work_mem = %s", ('2GB',))
    except Exception: pass
print("done")
PY
# `docker compose cp` не вміє писати на tmpfs, коли rootfs контейнера
# read_only (front саме такий, compose.yaml): падає з "container rootfs is
# marked read-only", хоч ціль — /tmp, змонтований записуваним. `exec cat >`
# пише напряму в tmpfs і цю заборону обходить. Знайдено 04.09.2026.
docker compose exec -T front sh -c 'cat > /tmp/cli_t.py' < "$WORK/t.py" >/dev/null 2>&1
out=$(docker compose exec -T -e H="$INNER" -e T="$T" -e R="$R" -e D="$D" \
      front python /tmp/cli_t.py 2>&1 | tail -1)
[[ "$out" == "done" ]] && ok "параметри, опечатка, заборонений SET" || bad "psycopg3" "$out"

# --- pgjdbc (DBeaver / DataGrip) ---------------------------------------------
echo
echo "3. pgjdbc — те, на чому працюють DBeaver і DataGrip"
JAR="$WORK/postgresql.jar"
if ! curl -sS -L --max-time 60 -o "$JAR" \
     https://repo1.maven.org/maven2/org/postgresql/postgresql/42.7.4/postgresql-42.7.4.jar; then
  echo "  — драйвер не завантажився, крок пропущено"
else
  cat > "$WORK/T.java" <<'JAVA'
import java.sql.*; import java.util.Properties;
public class T {
  public static void main(String[] a) throws Exception {
    Properties p = new Properties();
    p.setProperty("user", System.getenv("R"));
    p.setProperty("password", System.getenv("T"));
    p.setProperty("ssl","true"); p.setProperty("sslmode","require");
    p.setProperty("prepareThreshold","5");
    p.setProperty("ApplicationName","observer-client-matrix");
    Connection c = DriverManager.getConnection(
        "jdbc:postgresql://"+System.getenv("H")+":55432/"+System.getenv("D"), p);
    try (PreparedStatement ps = c.prepareStatement("select t from _cli_test where id > ?")) {
      for (int i=0;i<6;i++){ ps.setInt(1,i); try(ResultSet r=ps.executeQuery()){while(r.next()){}} } }
    try (PreparedStatement ps = c.prepareStatement("SELCT t FROM _cli_test WHERE id > ?")) {
      ps.setInt(1,1); ps.executeQuery(); } catch (SQLException e) {}
    c.setAutoCommit(false);
    try (Statement s=c.createStatement()){ s.setFetchSize(7);
      try(ResultSet r=s.executeQuery("select id,t from _cli_test order by id")){while(r.next()){}} }
    c.commit(); c.setAutoCommit(true);
    try (Statement s=c.createStatement()){
      s.addBatch("insert into _cli_test(t) values ('пакет 1')");
      s.addBatch("insert into _cli_test(id,t) values (1,'дубль')");
      s.addBatch("insert into _cli_test(t) values ('пакет 3')");
      s.executeBatch(); } catch (SQLException e) {}
    c.close(); System.out.println("done");
  }
}
JAVA
  out=$(docker run --rm --network "$NET" -v "$WORK":/w -w /w \
        -e T="$T" -e R="$R" -e D="$D" -e H="$INNER" eclipse-temurin:21-jdk \
        sh -c 'javac -cp postgresql.jar T.java && java -cp postgresql.jar:. T' 2>&1 | tail -1)
  [[ "$out" == "done" ]] && ok "параметри, іменовані оператори, порційне читання, пакет" \
                         || bad "pgjdbc" "$out"
fi

# --- журнал ------------------------------------------------------------------
echo
echo "4. Чи все опинилося в журналі"
sleep 3
want() { # <опис> <умова>
  local n; n=$(q "select count(*) from obs.queries where started_at > '$since'::timestamp and ($2)")
  [[ "${n:-0}" -gt 0 ]] && ok "$1" || bad "$1" "жодного запису"
}
want "синтаксична помилка (42601)"          "sqlstate='42601'"
want "неіснуюча колонка (42703)"            "sqlstate='42703'"
want "порушення унікальності (23505)"       "sqlstate='23505'"
want "пропущено після помилки в пакеті (25P02)" "sqlstate='25P02'"
want "скасований запит (57014)"             "sqlstate='57014'"
want "заборонений SET (42501)"              "sqlstate='42501'"
want "COPY"                                 "sql_kind='copy'"
want "розширений протокол"                  "protocol='extended'"
want "простий протокол"                     "protocol='simple'"
want "значення параметрів збережені"        "params is not null"

# Порційне читання має дати ОДИН запис на весь результат, а не по одному на порцію.
rows=$(q "select max(rows_affected) from obs.queries
          where started_at > '$since'::timestamp and sql_text like 'select id,t from _cli_test%'")
[[ "${rows:-0}" -ge 20 ]] \
  && ok "порційне читання — один запис на весь результат ($rows рядків)" \
  || bad "порційне читання" "очікували >=20 рядків в одному записі, маємо ${rows:-0}"

echo
[[ $fail -eq 0 ]] && echo "Матриця клієнтів: усі перевірки пройдено." \
                  || echo "Матриця клієнтів: є провали."
exit $fail
