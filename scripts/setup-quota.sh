#!/usr/bin/env bash
# =============================================================================
# Жорсткі квоти на слоти tablespace через project quota файлової системи.
#
# Запускати НА ХОСТІ, після першого підйому кластера (слоти вже створені).
# Аргумент — шлях до тому pgts на хості, наприклад:
#   sudo ./scripts/setup-quota.sh /var/lib/docker/volumes/observer_pgts/_data
#
# Якщо ФС не підтримує project quota (типово — WSL2 без перемонтування),
# скрипт скаже про це і вийде з кодом 2. Тоді залишається лише мʼяка квота:
# фоновий job звіряє pg_database_size і блокує запис на рівні pgproxy.
# =============================================================================
set -euo pipefail

ROOT="${1:?вкажіть шлях до каталогу слотів (том pgts)}"
QUOTA="${OBSERVER_QUOTA_BYTES:-52428800}"
QUOTA_M=$(( QUOTA / 1024 / 1024 ))

FSTYPE=$(stat -f -c %T "$ROOT")
DEV=$(df --output=source "$ROOT" | tail -1)
MNT=$(df --output=target "$ROOT" | tail -1)

echo "каталог : $ROOT"
echo "ФС      : $FSTYPE на $DEV (точка монтування $MNT)"
echo "квота   : ${QUOTA_M} МБ на слот"

case "$FSTYPE" in
  xfs)
    command -v xfs_quota >/dev/null || { echo "немає xfs_quota (apt install xfsprogs)"; exit 2; }
    grep -qE " $MNT .*(prjquota|pquota)" /proc/mounts \
      || { echo "ПОМИЛКА: $MNT змонтовано без prjquota. Додайте prjquota в fstab і перемонтуйте."; exit 2; }
    id=1000
    for d in "$ROOT"/slot_*; do
      [[ -d "$d" ]] || continue
      name="obs_$(basename "$d")"
      grep -q "^${name}:" /etc/projid 2>/dev/null || echo "${name}:${id}" >> /etc/projid
      grep -q "^${name}:${d}" /etc/projects 2>/dev/null || echo "${name}:${d}" >> /etc/projects
      xfs_quota -x -c "project -s ${name}" -c "limit -p bhard=${QUOTA_M}m ${name}" "$MNT"
      id=$((id+1))
    done
    ;;
  ext2/ext3|ext4)
    command -v setquota >/dev/null || { echo "немає setquota (apt install quota)"; exit 2; }
    tune2fs -l "$DEV" 2>/dev/null | grep -q project \
      || { echo "ПОМИЛКА: на $DEV не увімкнено feature 'project' (tune2fs -O project,quota)"; exit 2; }
    id=1000
    for d in "$ROOT"/slot_*; do
      [[ -d "$d" ]] || continue
      chattr -p "$id" +P "$d"
      setquota -P "$id" 0 "${QUOTA_M}M" 0 0 "$MNT"
      id=$((id+1))
    done
    ;;
  *)
    echo "ФС $FSTYPE не підтримує project quota."
    echo "Працюємо на мʼякій квоті: pg_database_size + блокування на pgproxy."
    exit 2
    ;;
esac

echo "Готово."
