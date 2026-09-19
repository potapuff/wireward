#!/usr/bin/env bash
# =============================================================================
# Відновлення журналу з дампа.
#
# Два режими:
#   ./scripts/restore.sh --check ФАЙЛ        — відновити в тимчасову базу
#                                              observer_restore_check, звірити
#                                              кількість рядків із робочою і
#                                              прибрати за собою. Нічого не псує.
#   ./scripts/restore.sh --into-observer ФАЙЛ --yes
#                                            — відновити в робочу базу.
#
# Другий режим відмовляється працювати, якщо obs.queries не порожня: журнал
# дописується лише вперед, і накат дампа поверх живих даних дає суміш, яку
# потім не розплутати. Порожня база — це або новий кластер, або усвідомлено
# зачищений; будь-що інше вимагає рішення людини, а не прапорця.
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

MODE="${1:-}"
FILE="${2:-}"
[[ -n "$FILE" && -f "$FILE" ]] || { echo "вжиток: $0 --check|--into-observer ФАЙЛ [--yes]"; exit 2; }

psql_() { docker compose exec -T postgres psql -U postgres -tAX -v ON_ERROR_STOP=1 "$@"; }

case "$MODE" in
--check)
  TMPDB=observer_restore_check
  psql_ -d postgres -c "DROP DATABASE IF EXISTS $TMPDB" >/dev/null
  psql_ -d postgres -c "CREATE DATABASE $TMPDB" >/dev/null
  # Помилки на GRANT неіснуючим ролям і на розширеннях у тимчасовій базі не
  # роблять копію непридатною, тому без ON_ERROR_STOP; підсумок дають рядки.
  docker compose exec -T postgres sh -c \
    "cat > /tmp/restore.dump && pg_restore -U postgres -d $TMPDB --no-owner --no-privileges /tmp/restore.dump; rc=\$?; rm -f /tmp/restore.dump; exit \$rc" \
    < "$FILE" || echo "pg_restore завершився з попередженнями (див. вище)"

  printf '%-22s %12s %12s\n' таблиця живе відновлено
  bad=0
  # Перелік звіряння має рости разом зі схемою. Таблиця, якої тут немає,
  # перевіряється рівно ніяк: копія відновиться, звіт скаже «ok», а даних у
  # ній не буде.
  # fingerprint_labs навмисно тут немає: це кеш (DELETE+INSERT при
  # `rebuild_fingerprint_labs()`), а не первинні дані — звірка `r <= l1`
  # неминуче хибно спрацювала б, якщо перебудова йде саме під час звіряння.
  for t in students student_groups sessions queries db_events activity_buckets \
           ts_slots labs lab_expectations; do
    live=$(psql_ -d observer -c "select count(*) from obs.$t")
    rest=$(psql_ -d "$TMPDB"  -c "select count(*) from obs.$t")
    printf '%-22s %12s %12s' "$t" "$live" "$rest"
    if [[ "$live" == "$rest" ]]; then echo "  ok"; else echo "  РОЗБІЖНІСТЬ"; bad=1; fi
  done
  psql_ -d postgres -c "DROP DATABASE $TMPDB" >/dev/null
  # Розбіжність очікувана, якщо журнал дописувався після зняття дампа —
  # тому звіряти треба свіжу копію, і саме це робить `make restore-test`.
  [[ "$bad" == 0 ]] || { echo "копія не збігається з живою базою"; exit 1; }
  echo "копія відновлюється і збігається з живою базою"
  ;;

--into-observer)
  [[ "${3:-}" == "--yes" ]] || { echo "додайте --yes: це запис у робочу базу"; exit 2; }
  n=$(psql_ -d observer -c "select count(*) from obs.queries")
  [[ "$n" == 0 ]] || { echo "obs.queries містить $n записів — накат поверх живого журналу заборонено"; exit 3; }
  docker compose exec -T postgres sh -c \
    "cat > /tmp/restore.dump && pg_restore -U postgres -d observer --no-owner --single-transaction /tmp/restore.dump; rc=\$?; rm -f /tmp/restore.dump; exit \$rc" \
    < "$FILE"
  echo "відновлено: $(psql_ -d observer -c 'select count(*) from obs.queries') записів у obs.queries"
  ;;

*) echo "невідомий режим: $MODE"; exit 2 ;;
esac
