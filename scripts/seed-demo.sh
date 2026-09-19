#!/usr/bin/env bash
# =============================================================================
# Демонстраційні дані для екрана «Активність».
#
# УВАГА. Скрипт пише синтетичні рядки просто в obs.queries, тобто в журнал
# аудиту. На робочому сервері цього робити не можна ніколи: журнал цінний саме
# тим, що в ньому лише те, що справді сталося. Тому:
#   * потрібна змінна OBSERVER_ALLOW_DEMO_SEED=1;
#   * усі демо-студенти мають oidc_sub з префіксом 'demo-' і видимі як демо;
#   * `./scripts/seed-demo.sh --clean` прибирає їх повністю.
#
# Призначення — перевірити, що екран активності справді показує різні
# картини роботи, а не однорідну кашу.
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

psql() { docker compose exec -T postgres psql -U postgres -d observer -v ON_ERROR_STOP=1 "$@"; }

if [[ "${1:-}" == "--clean" ]]; then
  psql -q <<'SQL'
DELETE FROM obs.queries       WHERE student_id IN (SELECT id FROM obs.students WHERE oidc_sub LIKE 'demo-%');
DELETE FROM obs.sessions      WHERE student_id IN (SELECT id FROM obs.students WHERE oidc_sub LIKE 'demo-%');
DELETE FROM obs.db_events     WHERE student_id IN (SELECT id FROM obs.students WHERE oidc_sub LIKE 'demo-%');
DELETE FROM obs.activity_buckets WHERE student_id IN (SELECT id FROM obs.students WHERE oidc_sub LIKE 'demo-%');
DELETE FROM obs.sticking_points  WHERE student_id IN (SELECT id FROM obs.students WHERE oidc_sub LIKE 'demo-%');
UPDATE obs.ts_slots SET student_id = NULL, assigned_at = NULL
 WHERE student_id IN (SELECT id FROM obs.students WHERE oidc_sub LIKE 'demo-%');
DELETE FROM obs.students WHERE oidc_sub LIKE 'demo-%';
SQL
  echo "демо-дані прибрано"
  exit 0
fi

[[ "${OBSERVER_ALLOW_DEMO_SEED:-0}" == "1" ]] || {
  echo "Це пише синтетичні рядки в журнал аудиту."
  echo "Запускати лише на стенді: OBSERVER_ALLOW_DEMO_SEED=1 $0"
  exit 2
}

psql -q <<'SQL'
-- Три різні картини роботи. Саме їх екран «Активність» і має розрізняти.
INSERT INTO obs.students (oidc_sub, email, display_name, consented_at, last_seen_at)
VALUES ('demo-steady', 'steady@demo.test',  'Демо · рівна робота',      now(), now()),
       ('demo-night',  'night@demo.test',   'Демо · ніч перед здачею',  now(), now()),
       ('demo-stuck',  'stuck@demo.test',   'Демо · застряг',           now(), now())
ON CONFLICT (oidc_sub) DO NOTHING;

-- На порожньому obs.students тригер students_bootstrap_admin (ADR-023) видає
-- is_admin першому вставленому рядку — тобто на свіжому стенді демо-акаунт
-- стане адміністратором. Знімаємо це одразу: демо-дані не мають бути шляхом
-- до адмінки, особливо разом із DEV_LOGIN_ENABLED.
UPDATE obs.students SET is_admin = false, is_teacher = false
 WHERE oidc_sub LIKE 'demo-%';

-- Група — рік реєстрації, як і в застосунку (З3).
INSERT INTO obs.student_groups (student_id, group_name, since)
SELECT id, to_char(created_at, 'YYYY'), created_at::date
  FROM obs.students WHERE oidc_sub LIKE 'demo-%'
ON CONFLICT DO NOTHING;

-- Шаблони пронумеровані окремо серед вдалих і серед помилкових: вибір іде
-- за індексом, а не через ORDER BY random(). Причина — не педантизм.
-- LATERAL, який не залежить від номера запиту, PostgreSQL обчислює ОДИН РАЗ
-- на зовнішній рядок, тож усі запити сесії отримували однаковий шаблон:
-- у журналі це виглядало як 34 однакові помилки підряд, тобто фальшиве
-- «застрягання». Детермінований хеш від (сесія, номер) цю пастку знімає
-- і заразом робить дані відтворюваними.
CREATE TEMP TABLE tpl(i int, kind text, sql text, fp text, bad boolean);
INSERT INTO tpl VALUES
 (0,'select','select * from orders where total > 100','select * from orders where total > ?',false),
 (1,'select','select c.name, count(*) from customers c join orders o on o.customer_id = c.id group by 1','select c.name, count(*) from customers c join orders o on o.customer_id = c.id group by ?',false),
 (2,'insert','insert into orders(customer_id, total) values (3, 250)','insert into orders(customer_id, total) values (?, ?)',false),
 (3,'ddl','create index idx_orders_total on orders(total)','create index idx_orders_total on orders(total)',false),
 (4,'update','update orders set total = total * 1.1 where id = 5','update orders set total = total * ? where id = ?',false),
 (0,'select','SELCT * FROM orders','selct * from orders',true),
 (1,'select','select * from oders','select * from oders',true),
 (2,'select','select name, prise from products','select name, prise from products',true),
 (3,'insert','insert into orders(id, total) values (1, 10)','insert into orders(id, total) values (?, ?)',true);

CREATE TEMP VIEW ncnt AS
  SELECT bad, count(*)::int AS n FROM tpl GROUP BY bad;

-- ── 1. Рівна робота: вечорами через день, частка помилок спадає ────────────
WITH s AS (SELECT id FROM obs.students WHERE oidc_sub = 'demo-steady'),
ses AS (
  INSERT INTO obs.sessions (id, student_id, client_addr, application_name,
                            client_name, client_version, client_purpose, listen_port,
                            tls_version, started_at, ended_at, end_reason, queries_total)
  SELECT gen_random_uuid(), s.id, '10.44.2.11'::inet, 'DBeaver 24.3.0 - Main <db>',
         'DBeaver', '24.3.0', 'Main', 55432, 'TLSv1.3',
         date_trunc('day', now()) - (d.back || ' days')::interval + interval '18 hours',
         date_trunc('day', now()) - (d.back || ' days')::interval + interval '19 hours 40 minutes',
         'terminate', 0
  -- Починаємо з учора, а не з сьогодні: «сьогодні о 18:00» вранці — це
  -- майбутнє, і теплокарта показувала б роботу, якої ще не було.
  FROM s, (SELECT generate_series(1, 21, 2) AS back) d
  RETURNING id, student_id, started_at),
gen AS (
  SELECT ses.id AS sid, ses.student_id, ses.started_at, g,
         (now()::date - ses.started_at::date) AS back,
         ('x' || substr(md5(ses.id::text || ':' || g), 1, 7))::bit(28)::int AS h
    FROM ses, generate_series(1, 34) g)
INSERT INTO obs.queries (session_id, student_id, seq, started_at, duration_ms, sql_text,
                         sql_fingerprint, sql_kind, protocol, status, sqlstate,
                         error_message, rows_affected, is_catalog)
SELECT gen.sid, gen.student_id, gen.g,
       gen.started_at + (gen.g * 55 + (gen.h % 40) || ' seconds')::interval,
       ((gen.h % 120) / 10.0 + 0.4)::numeric(12,3), t.sql, t.fp, t.kind, 'extended',
       CASE WHEN t.bad THEN 'error' ELSE 'ok' END,
       CASE WHEN t.bad THEN '42601' END,
       CASE WHEN t.bad THEN 'syntax error at or near "SELCT"' END,
       CASE WHEN t.bad THEN NULL ELSE (gen.h % 40) END, false
FROM gen
-- частка помилок падає з ~45% три тижні тому до ~10% сьогодні
CROSS JOIN LATERAL (SELECT (gen.h % 100) < (10 + 35 * gen.back / 21) AS is_bad) d
JOIN ncnt c ON c.bad = d.is_bad
JOIN tpl  t ON t.bad = d.is_bad AND t.i = ((gen.h / 100) % c.n);

-- ── 2. Ніч перед здачею: три тижні тиші, потім 262 запити з 23:00 до 04:00 ─
WITH s AS (SELECT id FROM obs.students WHERE oidc_sub = 'demo-night'),
ses AS (
  INSERT INTO obs.sessions (id, student_id, client_addr, application_name,
                            client_name, client_version, client_purpose, listen_port,
                            tls_version, started_at, ended_at, end_reason, queries_total)
  SELECT gen_random_uuid(), s.id, '10.44.2.19'::inet, 'DataGrip 2025.2',
         'DataGrip', '2025.2', NULL, 8080, 'TLSv1.3',
         date_trunc('day', now()) - interval '2 days' + interval '23 hours',
         date_trunc('day', now()) - interval '1 day' + interval '4 hours', 'terminate', 0
  FROM s RETURNING id, student_id, started_at),
gen AS (
  SELECT ses.id AS sid, ses.student_id, ses.started_at, g,
         ('x' || substr(md5(ses.id::text || ':' || g), 1, 7))::bit(28)::int AS h
    FROM ses, generate_series(1, 262) g)
INSERT INTO obs.queries (session_id, student_id, seq, started_at, duration_ms, sql_text,
                         sql_fingerprint, sql_kind, protocol, status, sqlstate,
                         error_message, rows_affected, is_catalog)
SELECT gen.sid, gen.student_id, gen.g,
       gen.started_at + (gen.g * 68 || ' seconds')::interval,
       ((gen.h % 300) / 10.0 + 0.5)::numeric(12,3), t.sql, t.fp, t.kind, 'extended',
       CASE WHEN t.bad THEN 'error' ELSE 'ok' END,
       CASE WHEN t.bad THEN '42P01' END,
       CASE WHEN t.bad THEN 'relation "oders" does not exist' END,
       CASE WHEN t.bad THEN NULL ELSE (gen.h % 60) END, false
FROM gen
CROSS JOIN LATERAL (SELECT (gen.h % 100) < 38 AS is_bad) d
JOIN ncnt c ON c.bad = d.is_bad
JOIN tpl  t ON t.bad = d.is_bad AND t.i = ((gen.h / 100) % c.n);

-- ── 3. Застряг: помірна робота і серія з 9 однакових невдач підряд ─────────
WITH s AS (SELECT id FROM obs.students WHERE oidc_sub = 'demo-stuck'),
ses AS (
  INSERT INTO obs.sessions (id, student_id, client_addr, application_name,
                            client_name, client_version, client_purpose, listen_port,
                            tls_version, started_at, ended_at, end_reason, queries_total)
  SELECT gen_random_uuid(), s.id, '10.44.2.31'::inet, 'DBeaver 24.3.0 - Main <db>',
         'DBeaver', '24.3.0', 'Main', 55432, 'TLSv1.3',
         date_trunc('day', now()) - interval '4 days' + interval '14 hours',
         date_trunc('day', now()) - interval '4 days' + interval '16 hours', 'terminate', 0
  FROM s RETURNING id, student_id, started_at),
gen AS (
  SELECT ses.id AS sid, ses.student_id, ses.started_at, g,
         ('x' || substr(md5(ses.id::text || ':w' || g), 1, 7))::bit(28)::int AS h
    FROM ses, generate_series(1, 12) g),
warm AS (
  INSERT INTO obs.queries (session_id, student_id, seq, started_at, duration_ms, sql_text,
                           sql_fingerprint, sql_kind, protocol, status, rows_affected, is_catalog)
  SELECT gen.sid, gen.student_id, gen.g,
         gen.started_at + (gen.g * 50 || ' seconds')::interval,
         ((gen.h % 80) / 10.0 + 0.3)::numeric(12,3), t.sql, t.fp, t.kind, 'extended', 'ok',
         (gen.h % 30), false
  FROM gen JOIN tpl t ON NOT t.bad AND t.i = (gen.h % 5)
  RETURNING session_id)
INSERT INTO obs.queries (session_id, student_id, seq, started_at, duration_ms, sql_text,
                         sql_fingerprint, sql_kind, protocol, status, sqlstate,
                         error_message, error_position, is_catalog)
SELECT ses.id, ses.student_id, 12 + g,
       ses.started_at + interval '10 minutes' + (g * 95 || ' seconds')::interval,
       (g * 0.3 + 0.2)::numeric(12,3),
       'select o.id, c.name from orders o join customers c on c.id = o.custmer_id where o.total > ' || (g * 10),
       'select o.id, c.name from orders o join customers c on c.id = o.custmer_id where o.total > ?',
       'select', 'extended', 'error', '42703',
       'column o.custmer_id does not exist', 62, false
FROM ses, generate_series(1, 9) g;

-- Лічильники сесій узгоджуємо з фактично вставленими запитами.
UPDATE obs.sessions se
   SET queries_total = x.n, errors_total = x.e
  FROM (SELECT session_id, count(*) n, count(*) FILTER (WHERE status='error') e
          FROM obs.queries GROUP BY 1) x
 WHERE x.session_id = se.id;
SQL

echo "демо-дані створено: три студенти з різними картинами роботи"
echo "прибрати: ./scripts/seed-demo.sh --clean"
