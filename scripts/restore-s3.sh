#!/usr/bin/env bash
# =============================================================================
# Відновлення фізичного кластера з S3-сумісного сховища (ADR-022).
#
# Відновлюється НЕ в робочий том, а в окремий (сервіс `pg-restore`, профіль
# `restore`, том `pgrestore`). Так само, як `restore.sh --check` відновлює дамп
# у тимчасову базу: перевірка копії не має жодної технічної можливості
# зіпсувати те, що вона перевіряє.
#
#   ./scripts/restore-s3.sh --check              остання копія + увесь WAL
#   ./scripts/restore-s3.sh --check --at '2026-09-06 10:00:00+03'   на момент
#   ./scripts/restore-s3.sh --check --backup base_00000001…          конкретна
#
# Прапорці:
#   --marker-db ІМ'Я   додатково вимагати, щоб у відновленому кластері була ця
#                      база. Її створює `make test-backup-s3` уже ПІСЛЯ базової
#                      копії — тобто вона є доказом, що програвся ще й WAL, а
#                      не лише розпакувався базовий бекап.
#   --keep             не гасити контейнер відновлення (щоб залізти psql-ем).
#
# Змінні: COMPOSE_FILES — перелік -f для docker compose.
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

DC=(docker compose)
for f in ${COMPOSE_FILES:-}; do DC+=(-f "$f"); done

MODE=""; TARGET_TIME=""; BACKUP="LATEST"; MARKER_DB=""; KEEP=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --check)      MODE=check; shift ;;
    --at)         TARGET_TIME="$2"; shift 2 ;;
    --backup)     BACKUP="$2"; shift 2 ;;
    --marker-db)  MARKER_DB="$2"; shift 2 ;;
    --keep)       KEEP=1; shift ;;
    *) echo "невідомий аргумент: $1" >&2; exit 2 ;;
  esac
done
[[ "$MODE" == "check" ]] || { echo "вжиток: $0 --check [--at ЧАС] [--backup ІМ'Я] [--marker-db БАЗА] [--keep]"; exit 2; }

# fingerprint_labs навмисно тут немає: це кеш (DELETE+INSERT при
# `rebuild_fingerprint_labs()`), а не первинні дані — звірка `r <= l1`
# неминуче хибно спрацювала б, якщо перебудова йде саме під час нічного
# `--check`.
TABLES=(students student_groups sessions queries db_events activity_buckets
        ts_slots labs lab_expectations)

live() { "${DC[@]}" exec -T postgres    psql -U postgres -d observer -tAX -c "$1" | tr -d '\r'; }
rest() { "${DC[@]}" exec -T pg-restore  psql -U postgres -d "${2:-observer}" -tAX -c "$1" | tr -d '\r'; }
rsh()  { "${DC[@]}" exec -T pg-restore sh -c "$1"; }
rpg()  { "${DC[@]}" exec -T -u postgres pg-restore sh -c "$1"; }

# pg-restore навмисно не має власного build: він бере вже зібраний образ
# сервісу postgres (там і wal-g). Якщо образу немає, compose піде шукати його
# на Docker Hub і скаже щось геть не про це.
if ! docker image inspect observer-postgres >/dev/null 2>&1; then
  echo "немає образу observer-postgres — спершу \`docker compose build postgres\`" >&2
  exit 2
fi

echo "== порожній кластер під відновлення =="
"${DC[@]}" --profile restore up -d pg-restore >/dev/null
# Том переживає попередні прогони; починати треба з чистого каталогу, інакше
# backup-fetch відмовиться писати в непорожній PGDATA.
rsh 'pg_ctl -D "$PGDATA" -m immediate stop >/dev/null 2>&1 || true
     rm -rf "$PGDATA"; mkdir -p "$PGDATA"
     # Копія фізична, тож тягне і tablespace’и студентів: їхні шляхи прописані
     # в самому кластері (tablespace_map), і залишки попереднього прогону в
     # /srv/pgts зіпсували б наступний.
     rm -rf /srv/pgts/*; mkdir -p /srv/pgts
     chown -R postgres:postgres /var/lib/postgresql/data /srv/pgts
     chmod 700 "$PGDATA" /srv/pgts'

echo "== базова копія зі сховища ($BACKUP) =="
rpg "walg backup-fetch \"\$PGDATA\" $BACKUP"

echo "== конфігурація відновлення =="
# Пишемо в postgresql.auto.conf, а не в postgresql.conf: наші рядки лягають
# останніми й перебивають усе, що прийшло з копії.
#
# archive_mode = off — не косметика, а запобіжник: відновлений кластер, який
# після промоції почне писати WAL у ТОЙ САМИЙ префікс, зіпсує архів робочого
# (дві історії з одним system identifier).
#
# hot_standby = off — щоб recovery не звірявся з max_connections із pg_control:
# у копії, знятій із конфігом на 140 з'єднань, дефолтні 100 у PGDATA дали б
# FATAL ще до першого програного запису.
target=""
if [[ -n "$TARGET_TIME" ]]; then
  target="recovery_target_time = '$TARGET_TIME'
recovery_target_action = 'promote'"
fi
rpg "cat >> \"\$PGDATA/postgresql.auto.conf\" <<'EOF'
restore_command = '/usr/local/bin/walg wal-fetch %f %p'
recovery_target_timeline = 'latest'
archive_mode = off
hot_standby = off
listen_addresses = ''
$target
EOF
touch \"\$PGDATA/recovery.signal\""

echo "== старт і програвання WAL =="
rpg 'pg_ctl -D "$PGDATA" -l /tmp/restore.log start' || true

ok=0
for _ in $(seq 1 90); do
  if rsh 'pg_isready -q -U postgres' >/dev/null 2>&1; then ok=1; break; fi
  sleep 2
done
if [[ "$ok" != 1 ]]; then
  echo "кластер не піднявся; журнал відновлення:" >&2
  rsh 'tail -40 /tmp/restore.log' >&2 || true
  exit 1
fi
# Промоція за відсутності цілі відбувається сама, коли архів вичерпано; але
# перевірити це треба явно, інакше «піднявся» може означати «досі в recovery».
for _ in $(seq 1 30); do
  [[ "$(rest 'select pg_is_in_recovery()' postgres)" == "f" ]] && break
  sleep 2
done
echo "   відновлено до: $(rest 'select now()' postgres), WAL: $(rest 'select pg_walfile_name(pg_current_wal_lsn())' postgres)"

echo
echo "== звірка з живою базою =="
printf '%-22s %12s %12s\n' таблиця живе відновлено
bad=0; drift=0
for t in "${TABLES[@]}"; do
  l1=$(live "select count(*) from obs.$t")
  r=$(rest  "select count(*) from obs.$t")
  printf '%-22s %12s %12s' "$t" "$l1" "$r"
  if [[ "$l1" == "$r" ]]; then echo "  ok"; continue; fi
  # Журнал дописується вперед, тож розбіжність сама по собі ще не вирок — на
  # відміну від restore.sh:49, де дамп звіряється з базою, яка стоїть. Але
  # «щось змінилося» теж не є виправданням: раніше будь-яка зміна лічильника
  # між двома читаннями оголошувалася нормою, і відновлення, у якому не
  # вистачало половини журналу, звітувало про успіх.
  #
  # Два окремі критерії:
  #   * r <= l1 — відновлене НЕ МОЖЕ бути більшим за живе. Якщо більше, це не
  #     приріст, а не та база / не той бекап;
  #   * величина нестачі. Природний приріст вимірюємо тут же другим читанням:
  #     l2 - l1 — це скільки рядків прийшло за час звірки. Нестача l2 - r має
  #     бути того ж порядку; удесятеро більша — це втрачені дані, а не час.
  l2=$(live "select count(*) from obs.$t")
  growth=$(( l2 - l1 ))
  short=$(( l2 - r ))
  # +8 — запас на таблиці, куди за час звірки не прилетіло нічого (growth = 0),
  # інакше будь-яка одиниця різниці стала б тривогою.
  allow=$(( growth * 10 + 8 ))
  if [[ "$r" -le "$l1" && "$short" -le "$allow" ]]; then
    echo "  журнал росте ($l1→$l2, нестача $short ≤ $allow)"; drift=1
  else
    echo "  РОЗБІЖНІСТЬ (живе $l1→$l2, відновлено $r, нестача $short > $allow)"; bad=1
  fi
done

if [[ -n "$MARKER_DB" ]]; then
  echo
  n=$(rest "select count(*) from pg_database where datname='$MARKER_DB'" postgres)
  m=$(rest "select count(*) from walg_marker" "$MARKER_DB" 2>/dev/null || echo 0)
  if [[ "$n" == 1 && "$m" -gt 0 ]]; then
    echo "мітка $MARKER_DB на місці — WAL, записаний ПІСЛЯ базової копії, програно"
  else
    echo "мітки $MARKER_DB у відновленому кластері немає — програно лише базову копію"
    bad=1
  fi
fi

if [[ "$KEEP" == 1 ]]; then
  echo "контейнер observer-pg-restore лишається піднятим (--keep)"
else
  rsh 'pg_ctl -D "$PGDATA" -m fast stop' >/dev/null 2>&1 || true
fi

echo
[[ "$bad" == 0 ]] || { echo "фізична копія НЕ збігається з живою базою"; exit 1; }
[[ "$drift" == 0 ]] && echo "фізична копія відновлюється і збігається з живою базою" \
                    || echo "фізична копія відновлюється; розбіжності лише там, де журнал дописувався під час перевірки"
