-- Доведення вже піднятого кластера до стану від 12.09.2026: модуль оцінок
-- прибрано цілком (З28, docs/roadmap/z28-remove-grading-module.md, ADR-024).
--
-- Що видаляється:
--   * obs.assessments — оцінки (peer/work/defense/final) разом з індексами;
--   * obs.admin_views — журнал персональних переглядів адмінки.
--
-- ЧОМУ ЦЕ ПОТРІБНО. Ручне виставлення оцінок — поза межами інструмента:
-- Wireward логує SQL-активність і показує аналітику, а відомість веде LMS.
-- Екран /admin/grades був єдиним, хто в ці таблиці писав; разом з ним зникає
-- і сенс обох. Тримати порожню схему «про всяк випадок» — це тримати
-- нездійснену обіцянку в моделі даних: наступний читач docs/03 вважатиме, що
-- механізм існує. Оцінки для кореляції з показниками братимуться зовнішнім
-- джерелом (разовий імпорт поза схемою obs.*, деталі TBD).
--
-- ПЕРЕД ЗАПУСКОМ. Рішення спирається на те, що модулем не користувалися.
-- На dev-кластері 12.09.2026: obs.assessments — 0 рядків, obs.admin_views —
-- 59 рядків, усі від акаунтів *@example.test (тестові входи). Якщо на вашому
-- кластері в obs.assessments є рядки — це РЕАЛЬНІ оцінки, які не
-- відтворюються; міграція нижче навмисно впаде, і їх треба спершу вивантажити
-- (\copy obs.assessments TO 'assessments.csv' CSV HEADER). obs.admin_views
-- такого запобіжника не має (журнал переглядів без оцінок не вважали втратою
-- первинних даних) — але на проді це може бути єдиний слід того, хто з
-- персоналу дивився картку якого студента; якщо кількість рядків велика,
-- вивантажте і її (\copy obs.admin_views TO 'admin_views.csv' CSV HEADER)
-- перед запуском. Скрипт лише сповіщає про кількість, не блокує.
--
-- CASCADE тут лише для страховки: FK на ці таблиці ніхто не тримає
-- (перевірено pg_constraint), залежні обʼєкти — самі індекси й послідовності
-- identity.
--
-- Запускати В БАЗІ observer від імені суперкористувача:
--   docker compose exec -T postgres psql -U postgres -d observer \
--     -f /dev/stdin < postgres/sql/upgrade-2026-09-i.sql
--
-- Ідемпотентний: можна виконувати повторно.

\set ON_ERROR_STOP on

-- Запобіжник від застосування не проти тієї бази: obs.queries — базова
-- таблиця журналу, якої без схеми observer просто немає. Без цієї перевірки
-- DROP TABLE IF EXISTS мовчки не робить нічого проти чужої/порожньої бази,
-- скрипт виходить з 0, і апгрейд вважається застосованим, хоча нічого не
-- відбулося.
DO $$
BEGIN
    IF to_regclass('obs.queries') IS NULL THEN
        RAISE EXCEPTION
            'не та база: % (очікується observer зі схемою obs)', current_database();
    END IF;
END $$;

-- Запобіжник від тихої втрати первинних даних. Спрацьовує ДО DROP і лише на
-- оцінках: журнал переглядів без оцінок нічого не доводить і втрати не варт.
DO $$
DECLARE
    n bigint;
BEGIN
    IF to_regclass('obs.assessments') IS NULL THEN
        RETURN;
    END IF;
    EXECUTE 'SELECT count(*) FROM obs.assessments' INTO n;
    IF n > 0 THEN
        RAISE EXCEPTION
            'obs.assessments містить % рядків — вивантажте їх у CSV перед видаленням', n;
    END IF;
END $$;

-- Лише сповіщення (не блокує): скільки рядків журналу переглядів буде
-- втрачено назавжди.
DO $$
DECLARE
    n bigint;
BEGIN
    IF to_regclass('obs.admin_views') IS NULL THEN
        RETURN;
    END IF;
    EXECUTE 'SELECT count(*) FROM obs.admin_views' INTO n;
    IF n > 0 THEN
        RAISE NOTICE 'obs.admin_views: % рядків буде видалено безповоротно', n;
    END IF;
END $$;

BEGIN;

DROP TABLE IF EXISTS obs.assessments CASCADE;
DROP TABLE IF EXISTS obs.admin_views CASCADE;

COMMIT;

-- Перевірка на місці: жодної з таблиць не лишилося.
DO $$
BEGIN
    IF to_regclass('obs.assessments') IS NOT NULL
       OR to_regclass('obs.admin_views') IS NOT NULL THEN
        RAISE EXCEPTION 'таблиці модуля оцінок досі існують';
    END IF;
END $$;
