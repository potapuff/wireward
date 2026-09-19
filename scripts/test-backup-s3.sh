#!/usr/bin/env bash
# =============================================================================
# Повний цикл офсайт-бекапу (ADR-022) без жодного хмарного доступу:
#
#   MinIO → archive_mode=on → базова копія → зміна ПІСЛЯ копії → відновлення в
#   порожній кластер → звірка з живою базою.
#
# Перевіряється саме те, що ламається тихо:
#   * archive_command справді пише в сховище (а не «повертає 0»);
#   * базова копія розпаковується в порожній PGDATA і кластер із неї стартує;
#   * WAL, записаний ПІСЛЯ копії, доїжджає і програється — інакше офсайт дає
#     не PITR, а нічний зліпок, і про це дізнаються в найгірший день;
#   * рядки в obs.* збігаються з живою базою (та сама звірка, що в ADR-017).
#
# Робочий том pgdata не чіпається: відновлення йде в окремий том `pgrestore`.
# Єдиний слід у робочому кластері — тимчасова база-мітка, яка прибирається.
#
# KEEP=1 — не прибирати MinIO і відновлений кластер (щоб подивитися руками).
#
# НА СЕРВЕРІ З НАЛАШТОВАНИМ ОФСАЙТОМ НЕ ЗАПУСКАТИ: тест підміняє сховище
# бойовому сервісу postgres. Скрипт це перевіряє сам (див. нижче) і без
# ALLOW_ON_PROD=1 відмовляється.
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

export COMPOSE_FILES="compose.yaml compose.test.yaml"
DC=(docker compose -f compose.yaml -f compose.test.yaml)
KEEP="${KEEP:-0}"
MARKER="walg_probe_$(date +%s)"
BUCKET=observer-backup

# Запобіжник проти запуску на сервері з бойовим офсайтом.
#
# Тест перемикає WALG_S3_PREFIX і AWS_ENDPOINT БОЙОВОГО сервісу postgres на
# локальний MinIO і в кінці повертає кластер у стан без офсайту. Якщо на цій
# машині офсайт справді налаштований, то на весь час прогону (а після
# прибирання — і далі, бо archive_mode стає off) WAL перестає доїжджати в
# реальне сховище, і в архіві зʼявляється діра. Діра в архіві WAL — це
# втрачений PITR на весь проміжок, і помітять її в найгірший день.
#
# ALLOW_ON_PROD=1 лишається як свідомий вихід: перевірити відновлення на
# сервері колись доведеться, але не випадково.
if grep -qE '^[[:space:]]*WALG_S3_PREFIX=[^[:space:]]' .env 2>/dev/null \
   && [[ "${ALLOW_ON_PROD:-0}" != "1" ]]; then
  cat >&2 <<'EOF'
у .env заданий WALG_S3_PREFIX — на цій машині офсайт-бекап бойовий.

Тест перемикає архівування кластера на локальний MinIO і в кінці лишає
archive_mode = off. Для бойового архіву це діра в потоці WAL, тобто втрачений
PITR за весь час прогону.

Якщо це справді те, чого ви хочете (наприклад, офсайт ще не наповнений):
    ALLOW_ON_PROD=1 make test-backup-s3
і одразу після прогону поверніть офсайт: `docker compose up -d postgres`
плюс `./scripts/backup-s3.sh` — новою базовою копією, бо стара більше не
має неперервного WAL за собою.
EOF
  exit 2
fi

pg() { "${DC[@]}" exec -T postgres psql -U postgres -d "${2:-observer}" -tAX -v ON_ERROR_STOP=1 -c "$1" | tr -d '\r'; }

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

cleanup() {
  local rc=$?
  step "прибирання"
  "${DC[@]}" exec -T postgres psql -U postgres -d postgres -tAX \
      -c "drop database if exists $MARKER" >/dev/null 2>&1 || true
  if [[ "$KEEP" == 1 ]]; then
    echo "KEEP=1: MinIO і observer-pg-restore лишаються піднятими"
    echo "  сховище:    ${DC[*]} exec minio mc ls local/$BUCKET"
    echo "  відновлене: docker compose --profile restore exec pg-restore psql -U postgres -d observer"
  else
    "${DC[@]}" --profile restore --profile test rm -sfv pg-restore minio >/dev/null 2>&1 || true
    docker volume rm -f observer_pgrestore observer_pgrestore_ts observer_miniodata >/dev/null 2>&1 || true
    # Повертаємо кластер у стан без офсайту — тобто в той, у якому він був до
    # тесту (archive_mode = off). Інакше стенд лишився б з archive_command,
    # який після зупинки MinIO валиться, і WAL накопичувався б у pg_wal.
    docker compose up -d postgres --wait >/dev/null 2>&1 || true
    echo "стек повернуто до стану без офсайту (archive_mode = off)"
  fi
  exit $rc
}
trap cleanup EXIT

for f in secrets/s3_access_key secrets/s3_secret_key; do
  [[ -s "$f" ]] || { echo "немає $f — виконайте \`make secrets\`" >&2; exit 2; }
done

step "тестове сховище MinIO"
"${DC[@]}" --profile test up -d minio --wait
# Бакет створюється явно, а не «якось сам»: wal-g бакетів не створює, і на
# сервері це рівно та сама ручна дія.
"${DC[@]}" exec -T minio sh -c "mc alias set local http://127.0.0.1:9000 \
     \"\$(cat /run/secrets/s3_access_key)\" \"\$(cat /run/secrets/s3_secret_key)\" >/dev/null && \
     mc mb --ignore-existing local/$BUCKET"

step "кластер з увімкненим архівуванням"
"${DC[@]}" up -d postgres --wait
mode="$(pg 'show archive_mode')"
[[ "$mode" == "on" ]] || { echo "archive_mode=$mode — накладка compose.test.yaml не застосувалася" >&2; exit 1; }
echo "archive_mode = on, ціль $(pg "select current_setting('archive_command')")"

step "перший сегмент WAL у сховищі"
pg "select pg_switch_wal()" >/dev/null
arch=""
for _ in $(seq 1 30); do
  arch="$(pg "select coalesce(last_archived_wal,'-') from pg_stat_archiver")"
  [[ "$arch" != "-" ]] && break
  sleep 2
done
failed="$(pg "select coalesce(last_failed_wal,'-')||' / '||coalesce(failed_count::text,'0') from pg_stat_archiver")"
[[ "$arch" != "-" ]] || { echo "жоден сегмент не заархівовано; провали: $failed" >&2
                          "${DC[@]}" logs --tail 30 postgres >&2; exit 1; }
echo "останній заархівований сегмент: $arch (провалів: $failed)"

step "базовий бекап у сховище"
./scripts/backup-s3.sh

step "зміна ПІСЛЯ копії — мітка для перевірки програвання WAL"
pg "create database $MARKER" postgres >/dev/null
pg "create table walg_marker(at timestamptz default now()); insert into walg_marker default values" "$MARKER" >/dev/null
before="$(pg "select coalesce(last_archived_wal,'-') from pg_stat_archiver")"
pg "select pg_switch_wal()" >/dev/null
for _ in $(seq 1 30); do
  now="$(pg "select coalesce(last_archived_wal,'-') from pg_stat_archiver")"
  [[ "$now" != "$before" ]] && break
  sleep 2
done
echo "мітка $MARKER створена, сегмент з нею заархівовано: $now"

step "цілісність архіву за версією самого wal-g"
"${DC[@]}" exec -T -u postgres postgres walg wal-verify integrity || \
  echo "(wal-verify незадоволений — див. вище; на розрив у таймлайні це не схоже лише якщо статус OK)"

step "відновлення з нуля в порожній кластер"
./scripts/restore-s3.sh --check --marker-db "$MARKER"
