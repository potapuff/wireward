#!/usr/bin/env bash
# =============================================================================
# Синтетичний навчальний рік: 40 студентів, ~36 тижнів, реальні обсяги.
#
# Потрібен, щоб відповісти на питання «а що буде через рік» ВИМІРОМ, а не
# оцінкою. Пише в журнал аудиту, тому — тільки на стенді.
#
#   OBSERVER_ALLOW_DEMO_SEED=1 ./scripts/seed-year.sh [студентів] [тижнів]
#   ./scripts/seed-year.sh --clean
#
# Профіль навантаження: 3 заняття на тиждень, ~350 змістовних запитів за
# заняття. Службові запити до pg_catalog у журнал не потрапляють (ADR-008),
# тож моделювати їх не треба.
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

psql() { docker compose exec -T postgres psql -U postgres -d observer -v ON_ERROR_STOP=1 "$@"; }

if [[ "${1:-}" == "--clean" ]]; then
  psql -q <<'SQL'
DELETE FROM obs.queries          WHERE student_id IN (SELECT id FROM obs.students WHERE oidc_sub LIKE 'year-%');
DELETE FROM obs.sessions         WHERE student_id IN (SELECT id FROM obs.students WHERE oidc_sub LIKE 'year-%');
DELETE FROM obs.activity_buckets WHERE student_id IN (SELECT id FROM obs.students WHERE oidc_sub LIKE 'year-%');
DELETE FROM obs.sticking_points  WHERE student_id IN (SELECT id FROM obs.students WHERE oidc_sub LIKE 'year-%');
DELETE FROM obs.db_events        WHERE student_id IN (SELECT id FROM obs.students WHERE oidc_sub LIKE 'year-%');
UPDATE obs.ts_slots SET student_id = NULL, assigned_at = NULL, used_bytes = NULL
 WHERE student_id IN (SELECT id FROM obs.students WHERE oidc_sub LIKE 'year-%');
DELETE FROM obs.students WHERE oidc_sub LIKE 'year-%';
SQL
  echo "річні дані прибрано"
  exit 0
fi

[[ "${OBSERVER_ALLOW_DEMO_SEED:-0}" == "1" ]] || {
  echo "Пише синтетичні рядки в журнал. Лише на стенді:"
  echo "  OBSERVER_ALLOW_DEMO_SEED=1 $0"; exit 2; }

N="${1:-40}"; WEEKS="${2:-36}"
echo "генерую: $N студентів × $WEEKS тижнів × 3 заняття × ~350 запитів"

# Спершу прибираємо попередні дані. Без цього повторний запуск (зокрема після
# невдалого) створює сесії ВДРУГЕ, бо їх INSERT не ідемпотентний, — і вимір
# показує вдвічі більший обсяг, ніж є насправді. Саме так і сталося.
"$0" --clean >/dev/null

psql -q <<SQL
-- Партиції на весь період: без них усе впаде в DEFAULT і вимір буде брехливим.
SELECT obs.ensure_query_partition((current_date - make_interval(months => m))::date)
  FROM generate_series(0, 13) m;

INSERT INTO obs.students (oidc_sub, email, display_name, consented_at, last_seen_at)
SELECT 'year-'||lpad(i::text,3,'0'), 'y'||i||'@year.test',
       'Рік · студент '||lpad(i::text,3,'0'), now(), now()
  FROM generate_series(1, $N) i
ON CONFLICT (oidc_sub) DO NOTHING;

-- Той самий bootstrap-ризик, що й у seed-demo.sh: на порожній obs.students
-- перший вставлений рядок отримує is_admin (ADR-023). Знімаємо його з
-- сіяних акаунтів одразу.
UPDATE obs.students SET is_admin = false, is_teacher = false
 WHERE oidc_sub LIKE 'year-%';

INSERT INTO obs.student_groups (student_id, group_name, since)
SELECT id, to_char(created_at, 'YYYY'), created_at::date
  FROM obs.students WHERE oidc_sub LIKE 'year-%'
ON CONFLICT DO NOTHING;

-- Пул відбитків: у курсі з баз даних їх сотні, не тисячі. Саме низька
-- кардинальність відбитків і робить осмисленим групування «де застряг».
-- Звичайна таблиця, а не TEMP: наповнення журналу йде партіями в ОКРЕМИХ
-- сесіях psql, а тимчасова таблиця живе лише в межах однієї.
DROP TABLE IF EXISTS public.seed_fp;
CREATE TABLE public.seed_fp(i int, kind text, tmpl text, bad boolean, sqlstate text, msg text);
INSERT INTO public.seed_fp
SELECT g,
       (ARRAY['select','select','select','insert','update','delete','ddl'])[1 + g % 7],
       (ARRAY[
         'select * from orders where total > %L and status = ''%s''',
         'select c.name, count(*) from customers c join orders o on o.customer_id = c.id where c.city = ''%s'' group by 1 having count(*) > %L',
         'select o.id, o.total from orders o where o.created_at > date ''2026-01-%L''',
         'insert into orders(customer_id, total) values (%L, %L)',
         'update orders set total = total * 1.%L where id = %L',
         'delete from orders where id = %L',
         'create index idx_%L on orders(total)'
       ])[1 + g % 7],
       (g % 10) < 3,
       (ARRAY['42601','42P01','42703','23505','22012'])[1 + g % 5],
       (ARRAY['syntax error at or near "SELCT"','relation "oders" does not exist',
              'column "prise" does not exist','duplicate key value violates unique constraint',
              'division by zero'])[1 + g % 5]
  FROM generate_series(1, 150) g;

-- Сесії. Три на тиждень, у навчальний час.
INSERT INTO obs.sessions (id, student_id, client_addr, application_name,
                          client_name, client_version, client_purpose,
                          listen_port, tls_version, started_at, ended_at,
                          end_reason, queries_total)
SELECT gen_random_uuid(), s.id, ('10.44.2.'||(1 + (i % 200)))::inet,
       (ARRAY['DBeaver 24.3.0 - Main <db>','DataGrip 2025.2','psql'])[1 + (i % 3)],
       (ARRAY['DBeaver','DataGrip','psql'])[1 + (i % 3)],
       (ARRAY['24.3.0','2025.2',NULL])[1 + (i % 3)],
       (ARRAY['Main',NULL,NULL])[1 + (i % 3)],
       CASE WHEN i % 9 = 0 THEN 8080 ELSE 55432 END, 'TLSv1.3',
       date_trunc('day', now()) - make_interval(days => (w * 7 + (i % 5))) + interval '10 hours'
           + make_interval(hours => (i % 8)),
       date_trunc('day', now()) - make_interval(days => (w * 7 + (i % 5))) + interval '12 hours'
           + make_interval(hours => (i % 8)),
       'terminate', 0
  FROM obs.students s
  CROSS JOIN generate_series(1, $WEEKS) w
  CROSS JOIN generate_series(1, 3) i
 WHERE s.oidc_sub LIKE 'year-%';
SQL

echo "сесій створено, наповнюю журнал (це найдовше)…"

# Партіями по місяцях, щоб не тримати мільйони рядків в одній транзакції.
for m in $(seq 0 8); do
  psql -q <<SQL
INSERT INTO obs.queries (session_id, student_id, seq, started_at, duration_ms,
                         sql_text, sql_fingerprint, sql_kind, protocol, params,
                         status, sqlstate, error_message, rows_affected, is_catalog)
SELECT se.id, se.student_id, g,
       se.started_at + make_interval(secs => g * 20 + (h % 19)),
       ((h % 400) / 10.0 + 0.2)::numeric(12,3),
       format(f.tmpl, 1 + h % 900, chr(97 + h % 26)),
       format(f.tmpl, '?', '?'),
       f.kind, 'extended',
       CASE WHEN h % 3 = 0 THEN ('["'||(h % 900)||'"]')::jsonb END,
       CASE WHEN f.bad THEN 'error' ELSE 'ok' END,
       CASE WHEN f.bad THEN f.sqlstate END,
       CASE WHEN f.bad THEN f.msg END,
       CASE WHEN f.bad THEN NULL ELSE (h % 120) END,
       false
  FROM obs.sessions se
  JOIN obs.students st ON st.id = se.student_id AND st.oidc_sub LIKE 'year-%'
  CROSS JOIN generate_series(1, 350) g
  CROSS JOIN LATERAL (SELECT ('x'||substr(md5(se.id::text||':'||g),1,7))::bit(28)::int AS h) x
  JOIN public.seed_fp f ON f.i = 1 + (x.h % 150)
 WHERE se.started_at >= date_trunc('month', now()) - make_interval(months => $m)
   AND se.started_at <  date_trunc('month', now()) - make_interval(months => $m - 1);
SQL
  printf '\r  місяць -%s готовий   ' "$m"
done
echo

psql -q -c "UPDATE obs.sessions se SET queries_total = x.n, errors_total = x.e
             FROM (SELECT session_id, count(*) n, count(*) FILTER (WHERE status='error') e
                     FROM obs.queries GROUP BY 1) x WHERE x.session_id = se.id"
psql -q -c "DROP TABLE IF EXISTS public.seed_fp"
psql -q -c "ANALYZE obs.queries; ANALYZE obs.sessions;"
psql -tAX -c "SELECT '  рядків у журналі: '||count(*) FROM obs.queries"
echo "прибрати: ./scripts/seed-year.sh --clean"
