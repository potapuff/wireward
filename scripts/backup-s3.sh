#!/usr/bin/env bash
# =============================================================================
# Базовий бекап фізичного кластера просто в S3-сумісне сховище (ADR-022).
#
# Це ДРУГИЙ шар, а не заміна ADR-017. Локальний `pg_dump` лишається: він
# логічний, вибірковий і читається чим завгодно. Тут — фізична копія всього
# кластера плюс безперервний архів WAL, тобто те, чого дамп не дає в принципі:
# відновлення на момент часу і копія, яка лежить не на тій самій машині.
#
# Проміжного файла на диску немає навмисно: wal-g стрімить у сховище. На
# сервері з 4 ГБ пам'яті й одним диском місця під «спочатку локально, потім
# відвантажити» просто немає.
#
#   ./scripts/backup-s3.sh            базовий бекап + ротація
#   ./scripts/backup-s3.sh --list     лише показати, що лежить у сховищі
#
# Змінні: BACKUP_S3_KEEP (скільки повних копій тримати, типово 7),
#         COMPOSE_FILES (перелік -f для docker compose; порожній = compose.yaml).
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

DC=(docker compose)
for f in ${COMPOSE_FILES:-}; do DC+=(-f "$f"); done

KEEP="${BACKUP_S3_KEEP:-7}"
MODE="${1:-}"

# Розбір аргументів окремим кроком і з відмовою за замовчуванням. Раніше
# перевірялося лише `--list`, а все інше провалювалося в гілку ПОВНОГО бекапу:
# друкарська помилка в `--lst` замість переліку запускала відвантаження всього
# кластера. Так само строго, як у restore-s3.sh.
case "$MODE" in
  ''|--list) ;;
  *) echo "невідомий аргумент: $MODE" >&2
     echo "вжиток: $0 [--list]" >&2
     exit 2 ;;
esac
[[ $# -le 1 ]] || { echo "зайві аргументи: ${*:2}" >&2; exit 2; }

pg()   { "${DC[@]}" exec -T postgres psql -U postgres -d observer -tAX -c "$1"; }
walg() { "${DC[@]}" exec -T -u postgres postgres walg "$@"; }

# Архівування вимкнене — це не «нічого страшного»: базовий бекап без потоку
# WAL непридатний до відновлення (wal-g і сам зависне, чекаючи на архівацію
# сегмента, яким закінчується копія).
# `on` і `always` — обидва означають «архів увімкнено» (друге штатне для
# standby). Умова та сама, що в health.sh і check-phase0.sh.
mode="$(pg 'show archive_mode' | tr -d '\r')"
if [[ "$mode" != "on" && "$mode" != "always" ]]; then
  cat >&2 <<EOF
archive_mode = $mode — офсайт-бекап не налаштований.
Увімкнути: WALG_S3_PREFIX і AWS_ENDPOINT у .env, ключі в secrets/s3_*,
OFFSITE_INTERNAL=false, далі \`docker compose up -d postgres\`.
Подробиці — docs/02-decisions.md, ADR-022.
EOF
  exit 2
fi

if [[ "$MODE" == "--list" ]]; then
  walg backup-list --detail --pretty
  exit 0
fi

# Стан архіватора ДО копії: якщо wal-push уже валиться, копія все одно вийде
# непридатною, і краще сказати це зараз, ніж через 40 хвилин відвантаження.
failed_before="$(pg "select coalesce(last_failed_wal,'-') from pg_stat_archiver" | tr -d '\r')"
last_arch="$(pg "select coalesce(last_archived_wal,'-') from pg_stat_archiver" | tr -d '\r')"
if [[ "$failed_before" != "-" && "$failed_before" > "$last_arch" ]]; then
  echo "архіватор не встигає або падає: останній провал $failed_before, останній успіх $last_arch" >&2
  exit 3
fi

echo "базовий бекап…"
# $PGDATA розкривається ВСЕРЕДИНІ контейнера: на хості цієї змінної немає, а
# каталог даних — частина конфігурації образу, не скрипта.
"${DC[@]}" exec -T -u postgres postgres sh -c 'walg backup-push "$PGDATA"'

# Ротація на боці сховища. `retain FULL N` прибирає і зайві базові копії, і
# WAL, старший за найдавнішу з тих, що лишилися: інакше архів росте вічно, і
# рахунок за сховище — теж.
walg delete retain FULL "$KEEP" --confirm

# Перевірка після копії — та сама думка, що в ADR-017: копія, якої ніхто не
# бачив у переліку, ще не копія. Повне відновлення перевіряє
# `scripts/restore-s3.sh` (і `make test-backup-s3` — цілим циклом).
walg backup-list --detail --pretty
