#!/usr/bin/env bash
# =============================================================================
# Прибирання слідів навантажувального тесту з ЖУРНАЛУ.
#
# Окремий скрипт, а не частина `loadtest clean`, з принципової причини:
# obs_app має на obs.queries лише SELECT, щоб адмінка не могла відредагувати
# журнал. Отже навіть власне прибирання застосунок зробити не в змозі — і це
# правильно. Видалити записи може лише той, хто має прямий доступ до СУБД.
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

read -r n < <(docker compose exec -T postgres psql -U postgres -d observer -tAX -c "
  SELECT count(*) FROM obs.queries q JOIN obs.students s ON s.id = q.student_id
   WHERE s.oidc_sub LIKE 'load-%'" | tr -d ' \r')
echo "записів навантаження в журналі: $n"
[[ "${1:-}" == "--yes" ]] || { echo "додайте --yes, щоб видалити"; exit 2; }

docker compose exec -T postgres psql -U postgres -d observer -v ON_ERROR_STOP=1 -q <<'SQL'
DELETE FROM obs.queries          WHERE student_id IN (SELECT id FROM obs.students WHERE oidc_sub LIKE 'load-%');
DELETE FROM obs.sessions         WHERE student_id IN (SELECT id FROM obs.students WHERE oidc_sub LIKE 'load-%');
DELETE FROM obs.activity_buckets WHERE student_id IN (SELECT id FROM obs.students WHERE oidc_sub LIKE 'load-%');
UPDATE obs.ts_slots SET student_id = NULL, assigned_at = NULL, used_bytes = NULL
 WHERE student_id IN (SELECT id FROM obs.students WHERE oidc_sub LIKE 'load-%');
DELETE FROM obs.students WHERE oidc_sub LIKE 'load-%';
SQL
echo "прибрано"
