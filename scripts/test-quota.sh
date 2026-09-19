#!/usr/bin/env bash
# =============================================================================
# Перевірка поведінки при вичерпанні квоти — критерій готовності фази 6.
#
# Перевіряємо ДВІ речі, і друга важливіша за першу:
#   1. студент, що вичерпав місце, не може додавати дані, але може їх прибирати;
#   2. це НЕ зачіпає інших студентів і не валить кластер.
#
# Другий пункт — не формальність. `ENOSPC` усередині PostgreSQL за деяких
# обставин призводить до PANIC, а це аварійне відновлення і розрив зʼєднань
# у ВСІХ одночасно. Саме тому WAL і pg_default лежать на окремому томі від
# слотів студентів, і саме це тут і перевіряється.
#
#   ./scripts/test-quota.sh <роль> <база> <токен>
# =============================================================================
set -uo pipefail
cd "$(dirname "$0")/.."

R="${1:?роль}"; D="${2:?база}"; T="${3:?токен}"
HOST="${PROXY_HOST:-127.0.0.1}"; PORT="${PROXY_PORT:-55432}"
DSN="host=$HOST port=$PORT dbname=$D user=$R sslmode=require"
fail=0
ok()  { printf '  ok    %s\n' "$1"; }
bad() { printf '  FAIL  %s — %s\n' "$1" "$2"; fail=1; }

q()   { docker compose exec -T postgres psql -U postgres -d observer -tAX -c "$1" 2>/dev/null | tr -d ' \r'; }
# Без -q: він глушить теги команд, і «DELETE 21600» не побачити.
# Беремо ВЕСЬ вивід: psql друкує ERROR у першому рядку, а HINT у наступному,
# тож tail -1 показував би підказку замість помилки.
stu() { PGPASSWORD="$T" psql "$DSN" -tAX -c "$1" 2>&1; }

SLOT=$(q "SELECT slot FROM obs.students WHERE role_name = '$R'")
QUOTA=$(q "SELECT quota_bytes FROM obs.ts_slots WHERE slot = $SLOT")
echo "роль $R, слот $SLOT, квота $((QUOTA / 1048576)) МБ"

cleanup() {
  stu "DROP TABLE IF EXISTS _quota_fill" >/dev/null 2>&1
  q "UPDATE obs.ts_slots SET used_bytes = NULL WHERE slot = $SLOT" >/dev/null
}
trap cleanup EXIT

echo
echo "1. Наповнюємо базу до межі"
stu "CREATE TABLE IF NOT EXISTS _quota_fill(id serial primary key, pad text)" >/dev/null
for i in $(seq 1 60); do
  out=$(stu "INSERT INTO _quota_fill(pad) SELECT repeat('x', 900) FROM generate_series(1, 1200)")
  used=$(docker compose exec -T postgres psql -U postgres -d observer -tAX \
         -c "SELECT pg_tablespace_size(tablespace_name) FROM obs.ts_slots WHERE slot = $SLOT" 2>/dev/null | tr -d ' \r')
  pct=$(( used * 100 / QUOTA ))
  printf '\r  заповнено %s%%  (%s МБ)   ' "$pct" "$((used / 1048576))"
  [[ "$out" == *ERROR* || "$out" == *FATAL* ]] && { echo; echo "  СУБД: $(head -1 <<<"$out")"; break; }
  [[ $pct -ge 100 ]] && break
done
echo

# Синхронізуємо кеш, який читає проксі (фоновий job робить це раз на хвилину).
q "UPDATE obs.ts_slots SET used_bytes = pg_tablespace_size(tablespace_name), checked_at = now()
    WHERE slot = $SLOT" >/dev/null
used=$(q "SELECT used_bytes FROM obs.ts_slots WHERE slot = $SLOT")
echo "2. Кеш квоти: $((used / 1048576)) МБ з $((QUOTA / 1048576)) МБ"

echo
echo "3. Що дозволено, а що ні при вичерпаній квоті"
sleep 31   # проксі кешує стан квоти на 30 секунд

out=$(stu "INSERT INTO _quota_fill(pad) VALUES ('ще')")
[[ "$out" == *53100* || "$out" == *"Місце вичерпано"* ]] \
  && ok "INSERT заблоковано з поясненням" || bad "INSERT" "$(head -1 <<<"$out")"

out=$(stu "CREATE TABLE _quota_more(i int)")
[[ "$out" == *53100* || "$out" == *"Місце вичерпано"* ]] \
  && ok "CREATE TABLE заблоковано" || bad "CREATE TABLE" "$(head -1 <<<"$out")"

out=$(stu "SELECT count(*) FROM _quota_fill" | tr -d ' \r')
[[ "$out" =~ ^[0-9]+$ ]] && ok "SELECT працює ($out рядків)" || bad "SELECT" "$out"

out=$(stu "DELETE FROM _quota_fill WHERE id % 2 = 0")
[[ "$out" == DELETE* ]] && ok "DELETE працює — вихід із пастки є" || bad "DELETE" "$(head -1 <<<"$out")"

out=$(stu "DROP TABLE IF EXISTS _quota_more")
[[ "$out" == DROP* || "$out" == NOTICE* ]] && ok "DROP працює" || bad "DROP" "$(head -1 <<<"$out")"

echo
echo "4. Чи зачепило решту кластера"
alive=$(q "SELECT 1")
[[ "$alive" == "1" ]] && ok "кластер живий, аварійного відновлення не було" \
                      || bad "кластер" "не відповідає"
restarts=$(docker inspect -f '{{.RestartCount}}' observer-postgres 2>/dev/null)
[[ "${restarts:-0}" == "0" ]] && ok "контейнер postgres не перезапускався" \
                              || bad "postgres" "перезапусків: $restarts"
panic=$(docker compose logs postgres 2>&1 | grep -c 'PANIC' || true)
[[ "$panic" == "0" ]] && ok "PANIC у логах немає" || bad "PANIC" "знайдено $panic"

others=$(q "SELECT count(*) FROM obs.students WHERE role_name IS NOT NULL AND role_name <> '$R'")
ok "інших студентів у кластері: $others (їхні бази не чіпалися)"

echo
echo "5. Подія у журналі"
ev=$(q "SELECT event FROM obs.db_events e JOIN obs.students s ON s.id = e.student_id
         WHERE s.role_name = '$R' AND e.event IN ('quota_warning','quota_exceeded')
         ORDER BY e.at DESC LIMIT 1")
[[ -n "$ev" ]] && ok "зафіксовано подію $ev" \
               || bad "подія квоти" "фоновий job ще не встиг — перевірте за хвилину"

echo
[[ $fail -eq 0 ]] && echo "Квота: усі перевірки пройдено." || echo "Квота: є провали."
exit $fail
