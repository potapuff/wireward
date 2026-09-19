-- =============================================================================
-- Wireward · схема бази `observer`
-- Виконується проти POSTGRES_DB=observer при першій ініціалізації тому.
-- Опис моделі та обґрунтування — docs/03-data-model.md
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

CREATE SCHEMA obs;

-- -----------------------------------------------------------------------------
-- Пул слотів tablespace.
-- Слоти створюються один раз при ініціалізації кластера (10-cluster.sh), бо
-- CREATE TABLESPACE вимагає superuser і не виконується всередині транзакції.
-- Провізіонінг студента зводиться до видачі вільного слота.
-- -----------------------------------------------------------------------------
CREATE TABLE obs.ts_slots (
    slot             integer PRIMARY KEY,
    tablespace_name  text        NOT NULL UNIQUE,
    fs_path          text        NOT NULL UNIQUE,
    -- Мʼяка межа. Змінюється не лише при провізіонінгу: адміністратор може
    -- підняти її окремому студентові (З15а) — front/app/quota.py:set_quota().
    -- CHECK > 0 не косметика: нуль тут не «без квоти», а тиха відмова
    -- механізму — і проксі, і фоновий job вважають 0 «квоти немає».
    quota_bytes      bigint      NOT NULL CHECK (quota_bytes > 0),
    student_id       uuid        UNIQUE,
    assigned_at      timestamptz,
    -- Кеш розміру. Проксі перевіряє квоту на кожному записуючому операторі,
    -- і рахувати pg_tablespace_size щоразу було б надто дорого. Оновлює
    -- фоновий job раз на хвилину; відставання до хвилини прийнятне, бо
    -- жорстку межу тримає квота файлової системи, а не це число.
    used_bytes       bigint,
    checked_at       timestamptz
);

-- -----------------------------------------------------------------------------
-- Студенти
-- -----------------------------------------------------------------------------
CREATE TABLE obs.students (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    oidc_sub        text        NOT NULL UNIQUE,
    email           text,
    -- Заповнюється з claim `name` Mix. Навмисно НЕ `full_name`: у Mix є окремий
    -- claim з такою назвою, який ми не запитуємо (ADR-004). Однакові імена тут
    -- гарантовано призвели б до плутанини.
    display_name    text,
    lms_roles       text[]      NOT NULL DEFAULT '{}',
    -- Ознака адміністратора проставляється ВРУЧНУ через psql і не змінюється
    -- жодним кодом застосунку: компрометація акаунта не має давати ескалації.
    -- Єдиний виняток — bootstrap першого користувача, див. тригер
    -- students_bootstrap_admin нижче.
    is_admin        boolean     NOT NULL DEFAULT false,
    -- Викладач: бачить лише СВОЇ групи (obs.teacher_groups), не бачить решти
    -- адмінки. Окремий прапорець, а не рівень у одній колонці: адміністратор і
    -- викладач — різні за природою ролі, і адміністратор НЕ зобовʼязаний бути
    -- викладачем (у нього немає своїх груп). Проставляє адміністратор.
    is_teacher      boolean     NOT NULL DEFAULT false,

    slot            integer     REFERENCES obs.ts_slots(slot),
    db_name         text        UNIQUE,
    role_name       text        UNIQUE,
    -- Токен для psql/DBeaver. Зберігається лише argon2id-хеш; сам токен
    -- показується один раз. Пароль ролі в PostgreSQL — окрема сутність,
    -- виводиться як HMAC(master_secret, id) і не зберігається ніде.
    db_token_hash   text,
    db_token_set_at timestamptz,
    db_token_expires_at timestamptz,

    consented_at    timestamptz,   -- підтвердження, що всі запити логуються

    -- Згода на ДОСЛІДЖЕННЯ — окрема сутність, і плутати її з попередньою не
    -- можна. `consented_at` — підстава вести журнал: без неї не працює курс.
    -- Ця — підстава включити студента у ВИБІРКУ статті; без неї курс працює
    -- рівно так само. Тому вона:
    --   * не впливає ні на доступ до бази, ні на оцінку;
    --   * має третій стан 'pending' — «ще не питали», який НЕ те саме, що
    --     відмова: мовчання за згоду тут не рахується;
    --   * змінюється в обидва боки в будь-який момент.
    research_consent    text NOT NULL DEFAULT 'pending'
                        CHECK (research_consent IN ('pending','given','declined')),
    research_consent_at timestamptz,
    CHECK ((research_consent = 'pending') = (research_consent_at IS NULL)),

    created_at      timestamptz NOT NULL DEFAULT now(),
    last_seen_at    timestamptz,
    disabled_at     timestamptz
);
CREATE UNIQUE INDEX students_email_lower_uq ON obs.students (lower(email)) WHERE email IS NOT NULL;

-- -----------------------------------------------------------------------------
-- Групи. Окрема таблиця, а не колонка: студент може бути одночасно в кількох
-- групах (перезарахування, вибіркова дисципліна, повторний курс), і кожне
-- членство має період. Колонка `study_group` описувала модель «одна група на
-- все життя», якої в дійсності немає.
--
-- Типова група — РІК РЕЄСТРАЦІЇ, а не константа з .env: константу треба
-- памʼятати міняти щовересня, і рік-два вона тихо бреше.
-- -----------------------------------------------------------------------------
CREATE TABLE obs.student_groups (
    student_id  uuid NOT NULL REFERENCES obs.students(id) ON DELETE CASCADE,
    group_name  text NOT NULL CHECK (group_name <> ''),
    since       date NOT NULL DEFAULT current_date,
    -- NULL означає «дотепер». Членство закривають, а не видаляють: журнал за
    -- минулий семестр має лишатися прив'язаним до групи, у якій студент тоді був.
    until       date,
    PRIMARY KEY (student_id, group_name, since),
    CHECK (until IS NULL OR until > since)
);
CREATE INDEX student_groups_current_idx ON obs.student_groups (group_name)
    WHERE until IS NULL;

-- Поточні групи одним рядком на студента — щоб екрани не повторювали агрегацію.
-- Хто в дослідницькій вибірці. Заведено окремим вʼю навмисно: будь-який
-- аналіз і будь-який експорт мають ходити через нього, а не повторювати
-- умову `research_consent = 'given'` руками. Забути її один раз — і в статті
-- опиняються дані студента, який відмовився.
CREATE VIEW obs.research_cohort AS
    SELECT id AS student_id, research_consent_at
      FROM obs.students
     WHERE research_consent = 'given' AND disabled_at IS NULL;

CREATE VIEW obs.student_current_groups AS
    SELECT student_id, array_agg(group_name ORDER BY group_name) AS groups
      FROM obs.student_groups WHERE until IS NULL GROUP BY student_id;

-- -----------------------------------------------------------------------------
-- Які групи бачить викладач. Видимість адмінки для is_teacher звужується рівно
-- цим переліком; адміністратор бачить усе і сам розподіляє групи.
--
-- FK на групу НЕМАЄ і бути не може: довідника груп у моделі не існує —
-- obs.student_groups.group_name це вільний текст, що приходить із claim `groups`
-- від Mix. Тому єдина перевірка тут — непорожність рядка, така сама, як там.
-- Наслідок, який треба памʼятати: викладачеві можна призначити групу, у якій
-- ще нікого немає (і це нормально — Mix віддасть склад пізніше), а помилка в
-- назві дасть просто порожній екран, а не помилку.
--
-- Періоду (since/until) тут навмисно немає, на відміну від obs.student_groups:
-- членство студента — історичний факт, який визначає привʼязку журналу, а
-- призначення викладача — це ПОТОЧНЕ право доступу. Право не «діяло в
-- минулому»: його або мають зараз, або ні. Забрати групу = видалити рядок.
-- -----------------------------------------------------------------------------
CREATE TABLE obs.teacher_groups (
    teacher_id  uuid NOT NULL REFERENCES obs.students(id) ON DELETE CASCADE,
    group_name  text NOT NULL CHECK (group_name <> ''),
    granted_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (teacher_id, group_name)
);
-- Зворотний напрям: «хто веде цю групу». PK покриває лише teacher_id → group.
CREATE INDEX teacher_groups_group_idx ON obs.teacher_groups (group_name);

-- -----------------------------------------------------------------------------
-- Bootstrap: перший, хто взагалі увійшов, стає адміністратором.
--
-- Інакше свіжий стенд неможливо адмініструвати: is_admin проставляють руками
-- через psql (ADR-006), а щоб дійти до psql, треба вже мати доступ до сервера.
-- Підстава і межі винятку — ADR-023, який частково скасовує ADR-006.
-- Правило звужене до краю: спрацьовує РІВНО на порожній таблиці й ніколи
-- більше, тож ескалації воно не дає — другий і всі наступні входи проходять
-- повз нього.
--
-- Чому тригер, а не підзапит у INSERT застосунку: правило безпеки не має
-- залежати від того, чи згадав його автор чергового місця, звідки заводять
-- студента (їх уже двоє — front/app/students.py і scripts/seed-labsim.py).
--
-- Чому advisory lock: `NOT EXISTS` двох одночасних транзакцій обидва разу
-- поверне true, і адміністраторів стане двоє. Швидкий шлях (таблиця вже
-- непорожня) блокування НЕ бере — на кожен наступний вхід це просто один
-- дешевий LIMIT 1.
-- -----------------------------------------------------------------------------
CREATE FUNCTION obs.bootstrap_first_admin() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM obs.students) THEN
        RETURN NEW;
    END IF;
    -- Ключ — довільна стала, аби вона не збігалася з іншими advisory-локами
    -- проєкту. Знімається на COMMIT/ROLLBACK самою СУБД.
    PERFORM pg_advisory_xact_lock(2026090701);
    IF NOT EXISTS (SELECT 1 FROM obs.students) THEN
        NEW.is_admin := true;
        RAISE NOTICE 'Wireward: % — перший користувач, отримує is_admin', NEW.oidc_sub;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER students_bootstrap_admin
    BEFORE INSERT ON obs.students
    FOR EACH ROW EXECUTE FUNCTION obs.bootstrap_first_admin();

ALTER TABLE obs.ts_slots
    ADD CONSTRAINT ts_slots_student_fk FOREIGN KEY (student_id) REFERENCES obs.students(id);

-- -----------------------------------------------------------------------------
-- Життєвий цикл персональної БД
-- -----------------------------------------------------------------------------
CREATE TABLE obs.db_events (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    student_id  uuid        NOT NULL REFERENCES obs.students(id),
    event       text        NOT NULL CHECK (event IN (
                    'created','dropped','recreated','quota_warning','quota_exceeded',
                    -- адміністратор вручну змінив квоту слота (З15а).
                    -- Окрема подія, а не 'quota_warning' з іншим details:
                    -- попередження — це стан студента, а це — ДІЯ людини,
                    -- і саме її має бути видно викладачеві в стрічці подій.
                    'quota_changed',
                    -- адміністратор вручну запустив VACUUM ANALYZE на базі
                    -- студента (З15б). `details` несе тривалість і результат;
                    -- подія потрібна саме тому, що вакуум міняє картину
                    -- зайнятого місця, і без сліду виглядав би як «диск сам
                    -- звільнився»
                    'vacuum_run',
                    'credential_rotated',
                    -- перше видання пароля (З25): кабінет видає його сам при
                    -- першому відкритті, без явного натискання кнопки —
                    -- окрема подія, а не 'credential_rotated', бо це не заміна
                    -- вже виданого секрету, а його поява.
                    'credential_issued',
                    'consent_given','disabled','enabled',
                    'proxy_passthrough_on','proxy_passthrough_off',
                    -- спрацював обмежувач частоти: `details.where` каже, де саме
                    -- ('proxy_login' | 'token_rotate'). Пишеться раз на вікно, а
                    -- не на кожну відмову — інакше потік спроб залив би саме цю
                    -- таблицю. Мовчазна відмова на вході гірша за її відсутність:
                    -- студент бачить «не пускає», викладач не бачить нічого
                    'rate_limited',
                    -- рішення щодо участі в дослідженні; пишуться обидва,
                    -- бо відмова — це теж рішення, і слід від неї потрібен
                    'research_consent_given','research_consent_declined',
                    -- вхід в обхід Mix (DEV_LOGIN_ENABLED); на сервері не має
                    -- зустрічатися жодного разу, поява в журналі — це алярм
                    'dev_login',
                    -- керування правами викладача з адмінки (RBAC-аудит):
                    -- без сліду неможливо реконструювати, хто і коли роздав
                    -- доступ до чужого журналу.
                    'teacher_granted','teacher_revoked',
                    'teacher_group_added','teacher_group_removed')),
    actor       text        NOT NULL,      -- 'student' | 'system' | 'admin:<uuid>'
    details     jsonb       NOT NULL DEFAULT '{}',
    at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX db_events_student_idx ON obs.db_events (student_id, at DESC);
CREATE INDEX db_events_at_idx      ON obs.db_events (at DESC);

-- -----------------------------------------------------------------------------
-- Сесії до СУБД (одна на TCP-зʼєднання через pgproxy)
-- -----------------------------------------------------------------------------
CREATE TABLE obs.sessions (
    id                uuid PRIMARY KEY,
    student_id        uuid        NOT NULL REFERENCES obs.students(id),
    client_addr       inet,
    -- Клієнт представляється сам, у StartupMessage. Сирий рядок лишаємо для
    -- розбору незнайомих клієнтів, а розкладені поля — щоб можна було
    -- фільтрувати й рахувати.
    application_name  text,
    client_name       text,      -- DBeaver, DataGrip, psql, pgAdmin, …
    client_version    text,
    client_purpose    text,      -- DBeaver і DataGrip дописують: Main, Metadata
    -- true => клієнт не представився, назву виведено з непрямих ознак.
    -- Здогад ніколи не показується як факт.
    client_guessed    boolean     NOT NULL DEFAULT false,
    startup_params    jsonb       NOT NULL DEFAULT '{}',
    listen_port       integer     NOT NULL,       -- 55432 або 8080
    tls_version       text,
    protocol_version  text,                       -- 3.0 | 3.2
    started_at        timestamptz NOT NULL,
    ended_at          timestamptz,
    end_reason        text,
    queries_total     integer     NOT NULL DEFAULT 0,
    errors_total      integer     NOT NULL DEFAULT 0,
    -- Службові запити клієнта до pg_catalog не зберігаються поштучно
    -- (DBeaver дає їх сотнями), лише рахуються. Див. ADR-008.
    catalog_queries   integer     NOT NULL DEFAULT 0
);
CREATE INDEX sessions_student_idx ON obs.sessions (student_id, started_at DESC);
CREATE INDEX sessions_started_idx ON obs.sessions (started_at DESC);
CREATE INDEX sessions_client_idx  ON obs.sessions (client_name, started_at DESC)
    WHERE client_name IS NOT NULL;

-- -----------------------------------------------------------------------------
-- Лабораторні роботи (З4).
--
-- Лабораторна — НЕ період у часі. Різні студенти приходять до неї коли
-- завгодно, повертаються через тиждень, роблять частинами. Тому тут немає
-- жодної дати початку чи кінця: привʼязка йде за ЗМІСТОМ запиту, а не за
-- моментом, коли він виконаний.
--
-- Лабораторна — це набір ЗРАЗКІВ викладача плюс перелік конструкцій, яких у
-- ній бути не повинно (у лабі про підзапити заборонено JOIN). Зразок не
-- зобовʼязує студента написати так само: збіг рахується нечітко, за перетином
-- форм (див. front/app/sqlshape.py).
-- -----------------------------------------------------------------------------
CREATE TABLE obs.labs (
    id          smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title       text NOT NULL CHECK (title <> ''),
    note        text,
    -- Поріг схожості, з якого запит вважається роботою над цією лабою.
    -- Свій на кожну лабу: у лабі, де всі задачі на одній таблиці, доводиться
    -- піднімати, у лабі з розгалуженою схемою — навпаки.
    threshold   real NOT NULL DEFAULT 0.5 CHECK (threshold > 0 AND threshold <= 1),
    created_at  timestamptz NOT NULL DEFAULT now(),
    archived_at timestamptz
);

-- Лабораторна належить одній або кільком групам.
CREATE TABLE obs.lab_groups (
    lab_id     smallint NOT NULL REFERENCES obs.labs(id) ON DELETE CASCADE,
    group_name text NOT NULL,
    PRIMARY KEY (lab_id, group_name)
);

-- Очікувані запити. Перелік свідомо ЧАСТКОВИЙ: студентські розвʼязки не
-- збігаються зі зразком один в один, і саме тому збіг нечіткий.
CREATE TABLE obs.lab_expectations (
    id       int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    lab_id   smallint NOT NULL REFERENCES obs.labs(id) ON DELETE CASCADE,
    title    text,
    sql_sample text NOT NULL CHECK (sql_sample <> '')
);
CREATE INDEX lab_expectations_lab_idx ON obs.lab_expectations (lab_id);

-- Чого бути не повинно. Код конструкції — зі словника sqlshape.CONSTRUCT_TITLES.
CREATE TABLE obs.lab_forbidden (
    lab_id    smallint NOT NULL REFERENCES obs.labs(id) ON DELETE CASCADE,
    construct text NOT NULL,
    PRIMARY KEY (lab_id, construct)
);

-- -----------------------------------------------------------------------------
-- Показники процесу, пораховані наперед. Причина та сама, що й у
-- obs.activity_buckets: рахувати їх на кожне відкриття сторінки означало б
-- витягати весь журнал студента в памʼять застосунку разом із текстами
-- запитів. На 1,5 млн записів це не «повільно», а неможливо.
--
-- Формули заморожені (docs/11-indicators.md), і `formula_version` тут саме
-- тому: коли формула зміниться, старі значення мають бути видимо старими, а
-- не тихо змішатися з новими в одній вибірці.
--
-- Значення похідні й повністю відновлювані з журналу — втрата не критична.
-- -----------------------------------------------------------------------------
CREATE TABLE obs.indicators (
    student_id      uuid NOT NULL REFERENCES obs.students(id),
    -- 0 = увесь журнал. Інші значення — вікна, які показують екрани.
    period_days     int  NOT NULL CHECK (period_days >= 0),
    -- 0 = усі запити студента; інакше — лише ті, що зіставилися з лабою.
    -- Нуль, а не NULL: значення входить у первинний ключ, а NULL у ньому
    -- не працює. Та сама домовленість, що й з period_days.
    lab_id          smallint NOT NULL DEFAULT 0,
    formula_version text NOT NULL,
    values          jsonb NOT NULL,
    computed_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (student_id, period_days, lab_id, formula_version)
);
CREATE INDEX indicators_computed_idx ON obs.indicators (computed_at);

-- -----------------------------------------------------------------------------
-- Анкети: SUS (System Usability Scale) і коротка рефлексія (П2.6).
--
-- SUS потрібен не для звіту, а тому, що в статті про засіб мусить бути число
-- про засіб. «Викладачам сподобалося» — не результат; SUS — стандартна шкала
-- з тридцятирічним корпусом порівняння, і 68 у ній означає те саме, що й у
-- будь-кого іншого.
--
-- Відповіді знеособлені так само, як усе решта: у вибірку йдуть лише ті, хто
-- дав згоду на дослідження. Але заповнити анкету може будь-хто — відмова від
-- участі в дослідженні не має заодно забирати право сказати, що засіб
-- незручний.
-- -----------------------------------------------------------------------------
CREATE TABLE obs.surveys (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    student_id  uuid NOT NULL REFERENCES obs.students(id),
    -- 'sus' — десять тверджень шкали; 'reflection' — три відкриті питання.
    kind        text NOT NULL CHECK (kind IN ('sus', 'reflection')),
    -- SUS: {"q1": 1..5, ...}. Рефлексія: {"learned": "...", ...}.
    answers     jsonb NOT NULL,
    -- Рахується при збереженні: формула SUS не з тих, які варто перераховувати
    -- в кожному запиті наново.
    score       numeric(5,1),
    at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (student_id, kind)
);
CREATE INDEX surveys_kind_idx ON obs.surveys (kind, at DESC);

-- -----------------------------------------------------------------------------
-- Журнал запитів. Це і є продукт.
-- Партиціонування помісячно: видаляти старе через DROP PARTITION на порядки
-- дешевше, ніж через DELETE.
-- -----------------------------------------------------------------------------
CREATE TABLE obs.queries (
    id              bigint      GENERATED ALWAYS AS IDENTITY,
    session_id      uuid        NOT NULL,
    student_id      uuid        NOT NULL,
    seq             integer     NOT NULL,       -- порядковий номер у межах сесії

    started_at      timestamptz NOT NULL,
    duration_ms     numeric(12,3),

    sql_text        text        NOT NULL,
    sql_truncated   boolean     NOT NULL DEFAULT false,
    -- Нормалізований відбиток: літерали замінені на ?, регістр і пробіли зведені.
    -- Дає групування «N спроб одного й того самого запиту» — сигнал «застряг».
    sql_fingerprint text        NOT NULL,
    sql_kind        text        NOT NULL CHECK (sql_kind IN
                        ('select','insert','update','delete','ddl','tcl','copy','other')),
    protocol        text        NOT NULL CHECK (protocol IN ('simple','extended')),
    params          jsonb,

    status          text        NOT NULL CHECK (status IN ('ok','error')),
    sqlstate        text,
    error_message   text,
    error_position  integer,
    rows_affected   bigint,
    -- Дві категорії службового трафіку клієнта. Обидві ЗБЕРІГАЮТЬСЯ і лише
    -- ховаються при показі (ADR-018): `select * from pg_class` у курсі баз
    -- даних — нормальна навчальна вправа, і викидати такі запити означало б
    -- втрачати роботу студента назавжди й непомітно.
    --   is_catalog — джерела запиту лежать у pg_catalog/information_schema;
    --   is_service — розмова клієнта із сервером без каталогу взагалі:
    --                SHOW TRANSACTION ISOLATION LEVEL перед КОЖНИМ запитом
    --                студента, SET extra_float_digits, SELECT version(),
    --                а також порожній текст запиту.
    is_catalog      boolean     NOT NULL DEFAULT false,
    is_service      boolean     NOT NULL DEFAULT false,

    -- Ланцюжка хешів тут НЕМАЄ. Він був, і його прибрано свідомо: див.
    -- ADR-015 у docs/02-decisions.md. Коротко — цілісність журналу тримають
    -- права (нікому, крім obs_shipper, не дано навіть INSERT, а UPDATE і
    -- DELETE не має ніхто), а випадкове пошкодження ловлять контрольні суми
    -- сторінок. Ланцюжок додавав 180 МБ на рік і хибну впевненість.

    PRIMARY KEY (started_at, id)
) PARTITION BY RANGE (started_at);

CREATE INDEX queries_student_time_idx ON obs.queries (student_id, started_at DESC);
CREATE INDEX queries_session_idx      ON obs.queries (session_id, seq);
CREATE INDEX queries_errors_idx       ON obs.queries (started_at DESC) WHERE status = 'error';
-- Індексу за (student_id, sql_fingerprint, started_at) тут НЕМАЄ навмисно.
-- Він виглядав очевидним для групування «де застряг», але виміряний на
-- річному обсязі не дав нічого: 15 мс із ним і 14 мс без. Причина в тому, що
-- запит фільтрує за студентом і часом, а за відбитком лише ГРУПУЄ — індекс
-- у групуванні не бере участі. Коштував він 458 МБ на рік, тобто чверть
-- усього журналу.
CREATE INDEX queries_brin_idx         ON obs.queries USING brin (started_at) WITH (pages_per_range = 32);

-- Партиція-пастка: сюди потрапить усе, що не влізло в місячні партиції.
-- Порожня DEFAULT-партиція — це алярм, а не норма.
CREATE TABLE obs.queries_default PARTITION OF obs.queries DEFAULT;

-- Один перелік параметрів автовакууму на всі партиції журналу — і на ті, що
-- створюються тут, і на ті, що зʼявляться через рік. Дублювати його руками
-- означало б рано чи пізно мати партиції з різними порогами й не знати про це:
-- `make check` (scripts/check-phase0.sh) звіряє reloptions наявних партицій
-- саме з тим, що повертає ця функція.
CREATE FUNCTION obs.journal_autovacuum_opts() RETURNS text
LANGUAGE sql IMMUTABLE AS $fn$
    SELECT $opts$
        autovacuum_vacuum_scale_factor = 0.0,
        autovacuum_vacuum_threshold = 10000,
        autovacuum_vacuum_insert_scale_factor = 0.0,
        autovacuum_vacuum_insert_threshold = 50000,
        autovacuum_analyze_scale_factor = 0.0,
        autovacuum_analyze_threshold = 5000,
        autovacuum_freeze_min_age = 0,
        autovacuum_vacuum_cost_delay = 1,
        autovacuum_vacuum_cost_limit = 600,
        toast.autovacuum_vacuum_insert_scale_factor = 0.0,
        toast.autovacuum_vacuum_insert_threshold = 50000,
        toast.autovacuum_freeze_min_age = 0,
        toast.autovacuum_vacuum_cost_delay = 1
    $opts$;
$fn$;

-- Автовакуум партицій журналу.
--
-- Журнал — append-only: INSERT робить лише obs_shipper, UPDATE і DELETE не має
-- ніхто (прав немає ні в кого, старе прибирається DROP PARTITION). Мертві
-- рядки тут беруться лише з обірваних транзакцій, тож класичний bloat від
-- UPDATE не загрожує, і глобальний autovacuum_vacuum_scale_factor тут не
-- працює взагалі: 5 % від партиції, яка росте весь місяць, — це поріг, що
-- тікає разом із таблицею.
--
-- Тому пороги ФІКСОВАНІ (scale_factor = 0), а головні тут два:
--   * insert_threshold — вакуум таблиці, що тільки росте (PG13+). Без нього
--     сторінки не потрапляють у visibility map: BRIN-індекс не отримує
--     підсумків, index-only scan перетворюється на звичайний, а вся робота із
--     заморожування збирається в одну анти-wraparound пробіжку по всій
--     партиції в найгірший момент;
--   * analyze_threshold — статистика планувальника. Для журналу це важливіше
--     за вакуум: межа started_at у поточній партиції їде щохвилини, і саме
--     застаріле уявлення про неї змушує планувальник читати місяць там, де
--     екран просив годину.
-- freeze_min_age = 0 — заморожувати одразу на першій же вакуумній пробіжці:
-- рядки журналу не змінюються ніколи, і відкладене заморожування означає лише
-- переписати ті самі сторінки вдруге (часто — партиції, яку скоро видалять).
-- cost_delay = 1 мс замість 2 — вакуум журналу майже завжди послідовний, і
-- саме гальмо бюджету робить так, що autovacuum відстає від потоку записів.
-- Окрема секція toast.* потрібна, бо довгі sql_text живуть у TOAST-таблиці,
-- а вона має власні лічильники й власний автовакуум.
--
-- Параметри доводиться ставити на КОЖНУ партицію: PG18 відмовляє в storage
-- parameters на партиційованій таблиці («cannot specify storage parameters for
-- a partitioned table»), і успадкування їх новими партиціями теж немає.
-- Через це вони живуть тут, у функції, яка партиції й створює.

-- SECURITY DEFINER, бо створення й видалення партицій вимагає прав на схему
-- obs, а obs_app навмисно їх не має: дати йому CREATE ON SCHEMA означало б
-- дозволити створювати в журналі будь-що. Замість цього — дві вузькі функції
-- з фіксованим search_path (без нього SECURITY DEFINER є дірою).
CREATE FUNCTION obs.ensure_query_partition(p_month date) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, obs AS $fn$
DECLARE
    lo   date := date_trunc('month', p_month)::date;
    hi   date := (date_trunc('month', p_month) + interval '1 month')::date;
    name text := format('queries_%s', to_char(lo, 'YYYY_MM'));
BEGIN
    IF to_regclass('obs.' || name) IS NULL THEN
        EXECUTE format(
            'CREATE TABLE obs.%I PARTITION OF obs.queries FOR VALUES FROM (%L) TO (%L) '
            'WITH (' || obs.journal_autovacuum_opts() || ')',
            name, lo, hi);
    END IF;
    RETURN name;
END
$fn$;

-- Прибирання партицій за строком зберігання. Повертає імена видалених, щоб
-- застосунок міг записати це в лог: тут журнал зникає назавжди.
CREATE FUNCTION obs.prune_query_partitions(keep_months int) RETURNS SETOF text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, obs AS $fn$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT c.relname
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'obs'
           AND c.relname ~ '^queries_[0-9]{4}_[0-9]{2}$'
           AND to_date(right(c.relname, 7), 'YYYY_MM')
               < date_trunc('month', current_date) - make_interval(months => keep_months)
    LOOP
        EXECUTE format('DROP TABLE obs.%I', r.relname);
        RETURN NEXT r.relname;
    END LOOP;
END
$fn$;


-- Порятунок DEFAULT-партиції.
--
-- Якщо записи приходять за місяць, для якого партиції ще немає, вони падають
-- у DEFAULT. Далі створити партицію на цей місяць уже НЕМОЖЛИВО: PostgreSQL
-- відмовиться, бо в DEFAULT є рядки, які мали б до неї належати. Тобто одна
-- пропущена партиція блокує створення наступних — і мовчки, доки хтось не
-- спробує її створити.
--
-- Процедура стандартна: відчепити DEFAULT, створити потрібні партиції,
-- перезалити рядки через батьківську таблицю, причепити DEFAULT назад.
CREATE OR REPLACE FUNCTION obs.rescue_default_partition() RETURNS int
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, obs AS $fn$
DECLARE
    n int;
    m date;
BEGIN
    SELECT count(*) INTO n FROM obs.queries_default;
    IF n = 0 THEN
        RETURN 0;
    END IF;

    ALTER TABLE obs.queries DETACH PARTITION obs.queries_default;

    FOR m IN SELECT DISTINCT date_trunc('month', started_at)::date
               FROM obs.queries_default ORDER BY 1
    LOOP
        PERFORM obs.ensure_query_partition(m);
    END LOOP;

    INSERT INTO obs.queries (session_id, student_id, seq, started_at, duration_ms,
                             sql_text, sql_truncated, sql_fingerprint, sql_kind,
                             protocol, params, status, sqlstate, error_message,
                             error_position, rows_affected, is_catalog, is_service)
    SELECT session_id, student_id, seq, started_at, duration_ms,
           sql_text, sql_truncated, sql_fingerprint, sql_kind,
           protocol, params, status, sqlstate, error_message,
           error_position, rows_affected, is_catalog, is_service
      FROM obs.queries_default;

    TRUNCATE obs.queries_default;
    ALTER TABLE obs.queries ATTACH PARTITION obs.queries_default DEFAULT;
    RETURN n;
END
$fn$;
REVOKE EXECUTE ON FUNCTION obs.rescue_default_partition() FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION obs.rescue_default_partition() TO obs_app;

-- Поточний і наступний місяць — щоб перший запис не впав у DEFAULT.
SELECT obs.ensure_query_partition(current_date);
SELECT obs.ensure_query_partition((current_date + interval '1 month')::date);

-- Догін для партицій, які вже існують.
--
-- `initdb/*` виконується РІВНО ОДИН РАЗ, на порожньому PGDATA. На стенді, який
-- живе рік, партицій уже сімнадцять, і жодна з них про ці параметри не
-- дізнається: ensure_query_partition() ставить їх лише на ті, що створює. Тому
-- окрема функція, яка проходить по всіх наявних партиціях obs.queries
-- (DEFAULT теж партиція і теж є в pg_inherits) і накладає той самий рядок.
--
-- Ідемпотентна: ALTER TABLE ... SET перезаписує значення, не додаючи нічого
-- нового, тож повторний виклик нічого не змінює. Повертає кількість партицій,
-- яких торкнулася, — щоб у виводі було видно, що вона взагалі щось зробила.
--
-- На свіжому кластері виклик нижче зайвий (партиції щойно створені з цими ж
-- параметрами), але нешкідливий. На існуючому сервері після `git pull` її
-- треба виконати руками: `make apply-autovacuum` (docs/10-deploy.md).
CREATE FUNCTION obs.apply_journal_autovacuum_opts() RETURNS int
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, obs AS $fn$
DECLARE
    r record;
    n int := 0;
BEGIN
    FOR r IN
        SELECT i.inhrelid::regclass AS part
          FROM pg_inherits i
         WHERE i.inhparent = 'obs.queries'::regclass
         ORDER BY 1
    LOOP
        EXECUTE format('ALTER TABLE %s SET (%s)', r.part, obs.journal_autovacuum_opts());
        n := n + 1;
    END LOOP;
    RETURN n;
END
$fn$;
REVOKE EXECUTE ON FUNCTION obs.apply_journal_autovacuum_opts() FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION obs.apply_journal_autovacuum_opts() TO obs_app;

SELECT obs.apply_journal_autovacuum_opts();

-- Статистика ПАРТИЦІЙОВАНОЇ таблиці. Автовакуум її не збирає: він ходить по
-- партиціях, а батьківський obs.queries не має власних рядків і в чергу
-- autovacuum ніколи не потрапляє (перевірено на кластері: last_autoanalyze
-- порожній). Планувальник при цьому користується успадкованою статистикою
-- батька для оцінок поверх усіх партицій, і без ANALYZE вона лишається такою,
-- якою була на порожній таблиці, — назавжди.
--
-- SECURITY DEFINER з тієї ж причини, що й у сусідніх функцій: obs_app не
-- володіє журналом, а видавати йому MAINTAIN на obs.queries заради однієї
-- добової операції — ширше право, ніж потрібно.
CREATE FUNCTION obs.analyze_journal() RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, obs AS $fn$
BEGIN
    -- ONLY (PG18) — саме батько, без рекурсії в партиції. Без ONLY одна нічна
    -- задача перечитувала б усі сімнадцять партицій підряд, хоч кожну з них
    -- автовакуум аналізує сам за власними порогами (див. вище). Потрібна тут
    -- лише успадкована статистика батька — та, якої автовакуум не збирає.
    ANALYZE ONLY obs.queries;
END
$fn$;
REVOKE EXECUTE ON FUNCTION obs.analyze_journal() FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION obs.analyze_journal() TO obs_app;

-- -----------------------------------------------------------------------------
-- Похідні агрегати: живлять екран «Активність».
-- Перебудовуються фоново; при втраті відновлюються з obs.queries.
-- -----------------------------------------------------------------------------
CREATE TABLE obs.activity_buckets (
    student_id             uuid        NOT NULL REFERENCES obs.students(id),
    bucket                 timestamptz NOT NULL,       -- крок 15 хвилин
    queries                integer     NOT NULL DEFAULT 0,
    errors                 integer     NOT NULL DEFAULT 0,
    catalog_queries        integer     NOT NULL DEFAULT 0,
    distinct_fingerprints  integer     NOT NULL DEFAULT 0,
    active_seconds         integer     NOT NULL DEFAULT 0,
    kinds                  jsonb       NOT NULL DEFAULT '{}',
    PRIMARY KEY (student_id, bucket)
);
CREATE INDEX activity_bucket_idx ON obs.activity_buckets (bucket DESC);

-- -----------------------------------------------------------------------------
-- Кеш привʼязки «відбиток → лабораторна» (З13/З14).
--
-- Привʼязка запиту до лаби рахується НЕ в SQL: це нечіткий збіг за формою
-- (`front/app/sqlshape2.py`, зважений Жаккар з IDF по активних лабах,
-- ADR-019). Перерахувати її означає розібрати кожен відбиток і зіставити з
-- кожним зразком кожної лаби — десятки тисяч порівнянь. Для екрана лаби це
-- прийнятно (одне відкриття), для треймапа активності — ні: він малюється на
-- КОЖНОМУ відкритті екрана групи й адмін-панелі.
--
-- Тому результат зіставлення осідає тут, а треймап уже агрегує його на льоту
-- (`obs.lab_activity_daily()`). Матеріалізувати ще й сам агрегат не треба:
-- тиждень журналу групи — це десятки тисяч рядків, які беруться індексом
-- (student_id, started_at); дублювати їх у другу таблицю означало б завести
-- другу правду про ті самі числа.
--
-- Ключ — (студент, відбиток), а не сам відбиток: кандидатів звужено групами
-- студента (З12), тож один і той самий текст запиту у двох студентів різних
-- груп законно належить різним лабам.
--
-- `lab_id IS NULL` — це НЕ «ще не рахували», а «рахували й не збіглося»:
-- негативний результат кешується так само, як позитивний, інакше кожен
-- прохід фонового циклу знову зіставляв би весь довгий хвіст одноразових
-- запитів, який нікуди не потрапляє.
--
-- Дані похідні: втрата некритична, `labs.rebuild_fingerprint_labs()` збирає
-- таблицю з журналу наново.
-- -----------------------------------------------------------------------------
CREATE TABLE obs.fingerprint_labs (
    student_id      uuid     NOT NULL REFERENCES obs.students(id) ON DELETE CASCADE,
    sql_fingerprint text     NOT NULL,
    lab_id          smallint REFERENCES obs.labs(id) ON DELETE CASCADE,
    -- Схожість із найкращим зразком лаби. Зберігається заради того ж, заради
    -- чого екран лаби показує число, а не вердикт: рішення про межу лишається
    -- за викладачем, і без бала кеш неможливо перевірити очима.
    score           real,
    matched_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (student_id, sql_fingerprint)
);
CREATE INDEX fingerprint_labs_lab_idx ON obs.fingerprint_labs (lab_id)
    WHERE lab_id IS NOT NULL;

-- Розтин для треймапа: студент × лабораторна × день.
--
-- Функція, а не вʼю, свідомо. У вʼю межу вікна довелося б класти на
-- обчислену колонку `day`, і планувальник не зміг би звести її до
-- `started_at` — кожне відкриття треймапа читало б увесь журнал, а не
-- тиждень. Тут вікно є параметром і лягає прямо на індекс
-- (student_id, started_at).
--
-- `p_group = NULL` — уся система (З14); назва групи — одна група (З13).
-- Часовий пояс той самий, що на екранах (front/app/activity.py:TZ): «день»
-- у треймапі має збігатися з «днем» у теплокарті, інакше два представлення
-- того самого тижня розійдуться на межі доби.
CREATE FUNCTION obs.lab_activity_daily(p_since timestamptz, p_group text DEFAULT NULL)
RETURNS TABLE (student_id uuid, lab_id smallint, day date,
               queries bigint, errors bigint, active_seconds bigint,
               last_at timestamptz)
LANGUAGE sql STABLE SET search_path = pg_catalog, obs AS $fn$
    SELECT q.student_id,
           fl.lab_id,
           (q.started_at AT TIME ZONE 'Europe/Kyiv')::date,
           count(*),
           count(*) FILTER (WHERE q.status = 'error'),
           -- Та сама груба, але чесна міра «скільки часу справді працював»,
           -- що й в obs.activity_buckets.active_seconds: різні секунди, у
           -- які студент щось виконував. Пауза між запитами в неї не входить.
           count(DISTINCT date_trunc('second', q.started_at)),
           max(q.started_at)
      FROM obs.queries q
      JOIN obs.fingerprint_labs fl
        ON fl.student_id = q.student_id
       AND fl.sql_fingerprint = q.sql_fingerprint
     WHERE q.started_at >= p_since
       AND fl.lab_id IS NOT NULL
       -- ADR-018: службовий трафік клієнта зберігається, але роботою студента
       -- не рахується. Умова дослівно та сама, що у front/app/db.py:WORK_ONLY.
       AND NOT (q.is_catalog OR q.is_service)
       AND (p_group IS NULL OR EXISTS (
               SELECT 1 FROM obs.student_groups g
                WHERE g.student_id = q.student_id
                  AND g.until IS NULL
                  AND g.group_name = p_group))
     GROUP BY 1, 2, 3
$fn$;
REVOKE EXECUTE ON FUNCTION obs.lab_activity_daily(timestamptz, text) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION obs.lab_activity_daily(timestamptz, text) TO obs_app;

-- Точки застрягання: ≥5 послідовних невдач з однаковим відбитком.
CREATE TABLE obs.sticking_points (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    student_id    uuid        NOT NULL REFERENCES obs.students(id),
    session_id    uuid        NOT NULL,
    fingerprint   text        NOT NULL,
    attempts      integer     NOT NULL,
    first_at      timestamptz NOT NULL,
    last_at       timestamptz NOT NULL,
    sqlstate      text,
    error_message text,
    resolved      boolean     NOT NULL DEFAULT false,  -- чи був потім успіх
    sample_sql    text
);
CREATE INDEX sticking_student_idx ON obs.sticking_points (student_id, last_at DESC);

-- Інтервали, коли журнал свідомо неповний (аварійний режим pgproxy).
-- Адмінка зобовʼязана показувати ці проміжки поверх усіх графіків.
CREATE TABLE obs.journal_gaps (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at timestamptz NOT NULL,
    ended_at   timestamptz,
    reason     text        NOT NULL,
    details    jsonb       NOT NULL DEFAULT '{}'
);

-- -----------------------------------------------------------------------------
-- Права. Головне тут — те, чого НЕ видано.
-- -----------------------------------------------------------------------------
GRANT USAGE ON SCHEMA obs TO obs_app, obs_shipper;

-- Застосунок: повний доступ до метаданих, журнал — ТІЛЬКИ читання.
GRANT SELECT, INSERT, UPDATE, DELETE ON
    obs.students, obs.ts_slots, obs.db_events, obs.sticking_points,
    obs.activity_buckets, obs.fingerprint_labs, obs.journal_gaps, obs.student_groups,
    obs.labs, obs.lab_groups, obs.lab_expectations, obs.lab_forbidden,
    obs.indicators, obs.surveys TO obs_app;
-- obs.assessments і obs.admin_views у цьому списку більше немає: модуль оцінок
-- прибрано разом із таблицями (ADR-024, З28). Джерело оцінок для аналізу —
-- зовнішнє.
-- Призначення викладачів. UPDATE навмисно НЕ дано: обидві колонки входять до
-- PK, тож «змінити рядок» тут не існує як операція — групу або додають, або
-- забирають. Зайвий UPDATE лише дав би спосіб непомітно переписати чуже
-- призначення замість пари INSERT/DELETE, видимої в коді адмінки.
GRANT SELECT, INSERT, DELETE ON obs.teacher_groups TO obs_app;
GRANT SELECT ON obs.student_current_groups, obs.research_cohort TO obs_app;
GRANT SELECT ON obs.queries, obs.sessions TO obs_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA obs TO obs_app;

-- Обслуговування партицій — тільки через ці дві функції й тільки застосунку.
REVOKE EXECUTE ON FUNCTION obs.ensure_query_partition(date)  FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION obs.prune_query_partitions(int)   FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION obs.ensure_query_partition(date)  TO obs_app;
GRANT  EXECUTE ON FUNCTION obs.prune_query_partitions(int)   TO obs_app;

-- Shipper: єдиний, хто пише журнал, і теж лише додає.
GRANT INSERT, SELECT ON obs.queries TO obs_shipper;
GRANT INSERT, SELECT, UPDATE ON obs.sessions TO obs_shipper;
GRANT INSERT ON obs.db_events TO obs_shipper;
-- Shipper сам реєструє власне застрягання: журнал, який не наповнюється,
-- має лишати слід у даних, а не лише в логах контейнера.
GRANT INSERT, SELECT, UPDATE ON obs.journal_gaps TO obs_shipper;
GRANT SELECT ON obs.students TO obs_shipper;
-- Проксі читає кешований розмір слота, щоб не пропускати оператори, які
-- збільшують дані понад квоту. Без цього гранту читання падає, а проксі
-- (свідомо) працює далі — і квота мовчки не діє.
GRANT SELECT ON obs.ts_slots TO obs_shipper;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA obs TO obs_shipper;

-- Нікому, крім shipper'а, не дано UPDATE/DELETE на obs.queries. Адмінка
-- ходить під obs_app і фізично не може відредагувати журнал.
REVOKE ALL ON SCHEMA public FROM PUBLIC;
