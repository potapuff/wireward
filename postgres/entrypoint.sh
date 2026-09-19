#!/usr/bin/env bash
# Рендеримо pg_hba.conf при КОЖНОМУ старті, а не в initdb.
# Причина: initdb-скрипти виконуються лише при першій ініціалізації тому,
# а конфіги живуть у шарі образу. Перестворення контейнера без перестворення
# тому лишило б сервер без pg_hba.conf.
#
# /etc/postgresql — tmpfs (див. compose.yaml), тому конфіги щоразу
# розкладаються туди зі зразків у /usr/local/share/observer. Це те, що
# дозволяє тримати корінь контейнера read-only: запис іде в tmpfs, а не в
# шар образу.
set -euo pipefail

SRC=/usr/local/share/observer
install -d -m 0755 /etc/postgresql

# Архівування WAL вмикається САМИМ фактом налаштованого сховища (ADR-022).
# archive_mode змінюється лише перезапуском, тож рішення ухвалюється тут, а не
# в конфізі: з увімкненим archive_mode і ненастроєним wal-g кластер складав би
# сегменти в pg_wal, доки не скінчиться диск, — і мовчки.
ARCHIVE_MODE=off
if [ -n "${WALG_S3_PREFIX:-}" ]; then
    ARCHIVE_MODE=on
    echo "observer-entrypoint: архів WAL увімкнено, ціль ${WALG_S3_PREFIX}" >&2
else
    echo "observer-entrypoint: WALG_S3_PREFIX порожній — архів WAL вимкнено" >&2
fi

sed -e "s|\${ARCHIVE_MODE}|${ARCHIVE_MODE}|g" \
    "$SRC/postgresql.conf" > /etc/postgresql/postgresql.conf

: "${FRONT_IP:=172.29.0.11}"
: "${PGPROXY_IP:=172.29.0.10}"

sed -e "s|\${FRONT_IP}|${FRONT_IP}|g" \
    -e "s|\${PGPROXY_IP}|${PGPROXY_IP}|g" \
    "$SRC/pg_hba.conf.tmpl" > /etc/postgresql/pg_hba.conf

# Цей скрипт виконується від root (docker-entrypoint.sh скидає привілеї пізніше),
# тому створений файл належить root. Без chown сервер під користувачем postgres
# отримає "could not open file: Permission denied" і не стартує.
if [ "$(id -u)" = "0" ]; then
    chown -R postgres:postgres /etc/postgresql
fi
chmod 600 /etc/postgresql/pg_hba.conf
chmod 644 /etc/postgresql/postgresql.conf

exec docker-entrypoint.sh "$@"
