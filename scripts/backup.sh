#!/usr/bin/env bash
# =============================================================================
# Резервна копія бази `observer` — журналу, який і є продукт.
#
# Що НЕ входить і чому:
#   * бази студентів. Їх відновлює провізіонінг (порожня база з нуля), а вміст —
#     це робота студента, яку він і так тримає в себе. Журнал того, що він
#     робив, відновити нізвідки — тому копіюємо саме його;
#   * файли `secrets/*`. Копія журналу не має тягнути за собою ключі.
#
# Ролі (`--globals-only`) кладемо окремим файлом: у ньому SCRAM-хеші паролів
# службових ролей і студентів, тож права на нього 0600, а не 0644 як у дампа.
#
# Формат `-Fc`: стиснутий, дозволяє вибіркове відновлення і не залежить від
# порядку об'єктів у файлі.
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

DIR="${BACKUP_DIR:-./backups}"
STAMP="$(date +%Y%m%d-%H%M%S)"
DUMP="$DIR/observer-$STAMP.dump"
GLOBALS="$DIR/globals-$STAMP.sql"
KEEP="${BACKUP_KEEP:-14}"

mkdir -p "$DIR"

# Дамп іде транзакційно узгодженим знімком (--serializable-deferrable),
# інакше shipper, який пише в obs.queries просто зараз, дає копію, де
# запис є, а сесія до нього — ще ні.
docker compose exec -T postgres \
  pg_dump -U postgres -d observer -Fc --serializable-deferrable > "$DUMP.part"
mv "$DUMP.part" "$DUMP"

umask 077
docker compose exec -T postgres \
  pg_dumpall -U postgres --globals-only > "$GLOBALS.part"
mv "$GLOBALS.part" "$GLOBALS"
chmod 600 "$GLOBALS"

# Перевірка, що файл читається саме як дамп, а не як 0 байтів чи текст помилки.
# Без цього «бекап є» означає лише «файл створено».
# pg_restore читає дамп із файла, а не з труби (`-Fc` потребує позиціонування),
# тож копію віддаємо в контейнер тимчасовим файлом.
rows=$(docker compose exec -T postgres sh -c \
  'cat > /tmp/verify.dump && pg_restore --list /tmp/verify.dump; rm -f /tmp/verify.dump' \
  < "$DUMP" | grep -c 'TABLE DATA' || true)
[[ "$rows" -gt 0 ]] || { echo "дамп не містить даних таблиць — копія непридатна"; exit 1; }

echo "дамп:   $DUMP ($(du -h "$DUMP" | cut -f1), таблиць із даними: $rows)"
echo "ролі:   $GLOBALS"

# Ротація за кількістю, а не за віком: так остання копія лишається навіть
# якщо бекап не запускався місяць.
ls -1t "$DIR"/observer-*.dump 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm --
ls -1t "$DIR"/globals-*.sql   2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm --
