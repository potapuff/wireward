#!/usr/bin/env bash
# =============================================================================
# Крок 3/4: пул слотів tablespace (ADR-007).
# Має виконатися ПІСЛЯ 10-observer-schema.sql — реєструє слоти в obs.ts_slots.
# =============================================================================
set -euo pipefail

SLOTS="${OBSERVER_TS_SLOTS:-150}"
QUOTA="${OBSERVER_QUOTA_BYTES:-52428800}"
psql=(psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --no-psqlrc)

echo "observer: створення ${SLOTS} слотів tablespace"
{
  for i in $(seq -f '%04g' 1 "$SLOTS"); do
    mkdir -p "/srv/pgts/slot_${i}"
    chmod 700 "/srv/pgts/slot_${i}"
    echo "CREATE TABLESPACE ts_${i} LOCATION '/srv/pgts/slot_${i}';"
    echo "REVOKE CREATE ON TABLESPACE ts_${i} FROM PUBLIC;"
    echo "GRANT CREATE ON TABLESPACE ts_${i} TO obs_provisioner;"
  done
} | "${psql[@]}" --dbname postgres

echo "observer: реєстрація слотів у каталозі"
{
  echo "INSERT INTO obs.ts_slots(slot, tablespace_name, fs_path, quota_bytes) VALUES"
  for i in $(seq -f '%04g' 1 "$SLOTS"); do
    printf "  (%d,'ts_%s','/srv/pgts/slot_%s',%s),\n" "$((10#$i))" "$i" "$i" "$QUOTA"
  done | sed '$ s/,$//'
  echo ";"
} | "${psql[@]}" --dbname observer

echo "observer: ${SLOTS} слотів, квота ${QUOTA} байт на слот"
