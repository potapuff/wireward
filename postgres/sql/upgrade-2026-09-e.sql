-- Доведення вже піднятого кластера до стану від 07.09.2026: роль «викладач».
--
-- Що додається:
--   * obs.students.is_teacher      — прапорець скороченого доступу до адмінки;
--   * obs.teacher_groups           — які групи бачить конкретний викладач;
--   * obs.bootstrap_first_admin()  — перший користувач стає адміністратором;
--   * GRANT SELECT/INSERT/DELETE на obs.teacher_groups для obs_app.
--
-- ЧОМУ ОКРЕМИЙ ПРАПОРЕЦЬ, А НЕ РІВЕНЬ В ОДНІЙ КОЛОНЦІ. «Адміністратор» і
-- «викладач» не лежать на одній шкалі: адміністратор бачить усі групи, але
-- своїх груп у нього немає взагалі, а викладач бачить рівно свої й не має
-- доступу до провізіонінгу. Спроба звести це до `level int` вимагала б
-- вирішити, чи «більший» рівень успадковує чужі групи, — питання без
-- осмисленої відповіді.
--
-- ЧОМУ НЕМАЄ FK НА ГРУПУ. Довідника груп у моделі не існує: group_name — це
-- вільний текст із claim `groups` від Mix (obs.student_groups). Тому єдина
-- перевірка — непорожність рядка. Наслідок, який треба памʼятати: помилка в
-- назві групи дасть викладачеві ПОРОЖНІЙ екран, а не помилку.
--
-- ЧОМУ BOOTSTRAP ТРИГЕРОМ, А НЕ ПІДЗАПИТОМ У INSERT ЗАСТОСУНКУ. Свіжий стенд
-- зараз неможливо адмініструвати: is_admin проставляють руками через psql
-- (ADR-006), а щоб дійти до psql, треба вже мати доступ до сервера. Правило
-- «перший увійшов — адміністратор» звужене до краю: спрацьовує рівно на
-- порожній obs.students і ніколи більше. Тригер, а не SQL у застосунку, бо
-- місць, звідки заводять студента, уже двоє (front/app/students.py і
-- scripts/seed-labsim.py), і правило безпеки не має залежати від того, чи
-- згадав його автор третього.
--
-- НА ВЖЕ ЗАСЕЛЕНІЙ БАЗІ ТРИГЕР НЕ РОБИТЬ НІЧОГО: студенти там є, отже перша
-- ж перевірка виходить швидким шляхом. Тобто цей скрипт НЕ роздає прав
-- заднім числом — якщо адміністраторів у базі ще немає, їх усе одно треба
-- проставити руками (нижче скрипт про це попереджає).
--
-- Для НОВИХ інсталяцій те саме робить initdb/10-observer-schema.sql. Цей файл
-- потрібен лише тому, що initdb виконується РІВНО ОДИН РАЗ, на порожньому PGDATA.
--
-- Запускати В БАЗІ observer від імені суперкористувача:
--   docker compose exec -T postgres psql -U postgres -d observer \
--     -f /dev/stdin < postgres/sql/upgrade-2026-09-e.sql
--
-- Ідемпотентний: можна виконувати повторно.

\set ON_ERROR_STOP on

BEGIN;

-- 1. Прапорець викладача. NOT NULL DEFAULT false на PG11+ не переписує таблицю.
ALTER TABLE obs.students
    ADD COLUMN IF NOT EXISTS is_teacher boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN obs.students.is_teacher IS
    'Викладач: бачить лише групи з obs.teacher_groups. Проставляє адміністратор.';

-- 2. Групи викладача.
CREATE TABLE IF NOT EXISTS obs.teacher_groups (
    teacher_id  uuid NOT NULL REFERENCES obs.students(id) ON DELETE CASCADE,
    group_name  text NOT NULL CHECK (group_name <> ''),
    granted_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (teacher_id, group_name)
);
-- Зворотний напрям: «хто веде цю групу». PK покриває лише teacher_id → group.
CREATE INDEX IF NOT EXISTS teacher_groups_group_idx ON obs.teacher_groups (group_name);

-- 3. Права. UPDATE навмисно НЕ дано: обидві змістовні колонки входять до PK,
--    тож «змінити рядок» тут не існує як операція — групу або додають, або
--    забирають. Зайвий UPDATE лише дав би спосіб непомітно переписати чуже
--    призначення замість пари INSERT/DELETE, видимої в коді адмінки.
--    obs_shipper не отримує нічого: до журналювання ця таблиця не має стосунку.
GRANT SELECT, INSERT, DELETE ON obs.teacher_groups TO obs_app;

-- 4. Bootstrap першого адміністратора.
--    OR REPLACE замість IF NOT EXISTS — щоб повторний запуск після правки
--    тіла функції привів кластер до файлу, а не лишив стару версію.
CREATE OR REPLACE FUNCTION obs.bootstrap_first_admin() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM obs.students) THEN
        RETURN NEW;
    END IF;
    -- Без блокування `NOT EXISTS` двох одночасних транзакцій обидва рази
    -- поверне true, і адміністраторів стане двоє. Швидкий шлях вище
    -- блокування НЕ бере: на кожен наступний вхід це просто один дешевий
    -- LIMIT 1. Ключ — довільна стала; знімається на COMMIT самою СУБД.
    PERFORM pg_advisory_xact_lock(2026090701);
    IF NOT EXISTS (SELECT 1 FROM obs.students) THEN
        NEW.is_admin := true;
        RAISE NOTICE 'Wireward: % — перший користувач, отримує is_admin', NEW.oidc_sub;
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS students_bootstrap_admin ON obs.students;
CREATE TRIGGER students_bootstrap_admin
    BEFORE INSERT ON obs.students
    FOR EACH ROW EXECUTE FUNCTION obs.bootstrap_first_admin();

COMMIT;

-- Перевірка на місці: усі чотири обʼєкти мають бути на місці разом. Прапорець
-- без таблиці груп дав би викладача, який не бачить нічого; таблиця без
-- гранту — адмінку, що падає з 42501 на збереженні призначення.
DO $$
DECLARE
    missing text[] := '{}';
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_schema='obs' AND table_name='students'
                      AND column_name='is_teacher') THEN
        missing := missing || 'obs.students.is_teacher';
    END IF;
    IF to_regclass('obs.teacher_groups') IS NULL THEN
        missing := missing || 'obs.teacher_groups';
    ELSIF NOT (has_table_privilege('obs_app','obs.teacher_groups','SELECT')
           AND has_table_privilege('obs_app','obs.teacher_groups','INSERT')
           AND has_table_privilege('obs_app','obs.teacher_groups','DELETE')) THEN
        missing := missing || 'GRANT SELECT,INSERT,DELETE ON obs.teacher_groups TO obs_app';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger
                    WHERE tgrelid='obs.students'::regclass
                      AND tgname='students_bootstrap_admin' AND NOT tgisinternal) THEN
        missing := missing || 'тригер students_bootstrap_admin';
    END IF;
    IF missing <> '{}' THEN
        RAISE EXCEPTION 'не доїхало: %', array_to_string(missing, ', ');
    END IF;
END $$;

-- Попередження, а не помилка: на вже заселеній базі тригер нічого не роздає
-- заднім числом, і кластер без жодного адміністратора лишиться некерованим.
DO $$
DECLARE
    n int;
BEGIN
    SELECT count(*) INTO n FROM obs.students WHERE is_admin;
    IF n = 0 AND EXISTS (SELECT 1 FROM obs.students) THEN
        RAISE WARNING 'у obs.students немає жодного is_admin, а тригер спрацьовує лише на порожній таблиці — проставте руками: UPDATE obs.students SET is_admin = true WHERE email = ''…'';';
    ELSE
        RAISE NOTICE 'адміністраторів: %, викладачів: %',
            n, (SELECT count(*) FROM obs.students WHERE is_teacher);
    END IF;
END $$;
