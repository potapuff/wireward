.PHONY: help secrets up down logs psql reset check test-go test-provisioning test-clients test-quota test-load backup restore-test backup-s3 test-backup-s3 apply-autovacuum test-parity test-rules test-shape test-ratelimit test-labfile test-indicators test-export test-surveys test-position test-rulecheck seed-labs check-shape check-rules indicators export-dataset health audit preflight cron images-export

help:
	@echo "make secrets   згенерувати ./secrets/*"
	@echo "make up        підняти кластер (фаза 0)"
	@echo "make down      зупинити"
	@echo "make logs      логи postgres"
	@echo "make psql      psql суперкористувачем у базу observer"
	@echo "make check     перевірка критеріїв готовності фази 0"
	@echo "make apply-autovacuum  догін autovacuum-параметрів на наявних партиціях журналу"
	@echo "make test-go   юніт-тести pgproxy (у контейнері golang)"
	@echo "make test-provisioning  цикл провізіонінгу та ізоляція студентів"
	@echo "make test-parity        звірка provision_student.sql і provisioning.py (без кластера)"
	@echo "make test-rules         правила підсвічування типових помилок (без кластера)"
	@echo "make test-shape         міра схожості запитів для лабораторних (без кластера)"
	@echo "make test-ratelimit     обмежувач частоти на боці веба (без кластера)"
	@echo "make test-labfile       розбір файлу лабораторних (без кластера)"
	@echo "make test-indicators    заморожені формули показників (без кластера)"
	@echo "make test-position      «де студент зараз» по лабораторних (без кластера)"
	@echo "make test-rulecheck     підрахунок тихих помилок у корпусі (без кластера)"
	@echo "make test-export        знеособлення в експорті ProgSnap2 (без кластера)"
	@echo "make test-surveys       підрахунок SUS (без кластера)"
	@echo "make seed-labs FILE=labs/….md [ARGS=--apply]   завести лабораторні з файлу"
	@echo "make check-shape        З7: як міра схожості поводиться на живому журналі"
	@echo "make check-rules        П3.1: частоти тихих помилок у всьому корпусі"
	@echo "make indicators [DAYS=…] показники процесу за замороженими формулами"
	@echo "make export-dataset      набір даних ProgSnap2 по дослідницькій когорті"
	@echo "make test-clients ROLE=.. DB=.. TOKEN=..   матриця клієнтів (psql/psycopg/pgjdbc)"
	@echo "make test-quota   ROLE=.. DB=.. TOKEN=..   вичерпання квоти"
	@echo "make test-load    [N=40] [SECS=30]         навантаження через проксі"
	@echo "make health    стан: журнал, партиції, квоти, бекап, служби"
	@echo "make cron      показати розклад обслуговування (не встановлює)"
	@echo "make audit     чекліст безпеки по репозиторію (без стека)"
	@echo "make preflight готовність до розгортання: режими, імена, секрети, порти"
	@echo "make images-export  зібрати образи локально й скласти в dist/ для перенесення на сервер"
	@echo "make backup    резервна копія журналу в ./backups"
	@echo "make restore-test  відновити останню копію в тимчасову базу і звірити"
	@echo "make backup-s3     базова копія кластера просто в S3-сумісне сховище (wal-g)"
	@echo "make test-backup-s3  повний цикл офсайт-бекапу проти локального MinIO"
	@echo "make reset     ЗНИЩИТИ томи і підняти з нуля"

secrets:
	@./scripts/make-secrets.sh

# BUILD_COMMIT/BUILD_DATE ідуть у front як build ARG (З16, footer + версія
# статики) — обчислюються тут, бо контейнер .git не бачить. -dirty, якщо
# є незакомічені зміни: інакше правка style.css без коміту не зрушить `?v=`
# і залипання кешу, яке З16 має лікувати, лишиться в самому dev-циклі.
up: secrets
	BUILD_COMMIT="$$(git rev-parse --short HEAD)$$(git diff --quiet || echo -dirty)" \
	BUILD_DATE="$$(git show -s --format=%cI HEAD)" \
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f postgres

psql:
	docker compose exec -it postgres psql -U postgres -d observer

check:
	@./scripts/check-phase0.sh

# Догін автовакууму на партиціях журналу, що вже існують. `initdb/*` виконується
# лише на порожньому томі, тож після `git pull` на працюючому сервері параметри
# отримають лише НОВІ партиції, а сімнадцять старих лишаться без них — мовчки.
# Ідемпотентно: виконувати повторно безпечно. `make check` це й перевіряє.
apply-autovacuum:
	@docker compose exec -T postgres psql -U postgres -d observer -tAX \
	    -c "SELECT obs.apply_journal_autovacuum_opts() AS partitions_updated"

# Локального Go на машині немає — тести йдуть у тому ж образі, що й збірка.
# Кеші лежать у pgproxy/.gocache, щоб не тягнути модулі щоразу.
GO_IMAGE ?= golang:1.23-bookworm
test-go:
	@mkdir -p pgproxy/.gocache/build pgproxy/.gocache/mod
	@docker run --rm \
	  -v $(CURDIR)/pgproxy:/src -w /src \
	  -u $$(id -u):$$(id -g) \
	  -e GOFLAGS=-mod=readonly \
	  -e GOCACHE=/src/.gocache/build -e GOMODCACHE=/src/.gocache/mod \
	  $(GO_IMAGE) go test ./...

# Виконується ВСЕРЕДИНІ контейнера: postgres навмисно не публікує портів,
# тож з хоста до кластера не дістатися. Підключення — локальним сокетом.
test-provisioning:
	@docker compose exec -T \
	  -e PROV_PW="$$(cat secrets/pg_provisioner_password)" \
	  -e MAINT_PW="$$(cat secrets/pg_maintenance_password)" \
	  -e PGUSER=postgres -e PGHOST=/var/run/postgresql \
	  -w /srv/observer postgres bash scripts/test-provisioning.sh

# Статична звірка: кластер не потрібен, тому її можна ганяти перед кожним
# комітом, а не лише коли піднято стек.
test-parity:
	@python3 ./scripts/test-provisioning-parity.py

test-rules:
	@python3 ./scripts/test-rules.py

test-shape:
	@python3 ./scripts/test-shape.py
	@python3 ./scripts/test-shape2.py
	@python3 ./scripts/test-extract.py
	@python3 ./scripts/test-temporal-context.py

test-ratelimit:
	@python3 ./scripts/test-ratelimit.py

test-labfile:
	@python3 ./scripts/test-labfile.py

test-indicators:
	@python3 ./scripts/test-indicators.py

test-position:
	@python3 ./scripts/test-position.py

test-rulecheck:
	@python3 ./scripts/test-rulecheck.py

test-export:
	@python3 ./scripts/test-export.py

test-surveys:
	@python3 ./scripts/test-surveys.py

# З7: чи вгадує міра схожості лабораторну на журналі, який уже є.
# Нічого не змінює — лише рахує й друкує; вердикт за викладачем.
check-shape:
	@docker compose exec -T front python -m app.shapecheck

# П3.1: частоти тихих помилок по всьому журналу — матеріал статті P3.
# Рахує СПРАЦЮВАННЯ ПРАВИЛ, а не помилки: точність без ручної розмітки
# невідома, і скрипт друкує вибірку саме для неї.
check-rules:
	@docker compose exec -T front python -m app.rulecheck $(or $(DAYS),0)

# З7 офлайн: те саме питання на реальних звітах студентів із labs/labs/.
# Спершу витягає SQL зі звітів, тоді рахує розділення й відбраковку.
# Без бази й без docker — вердикт так само за викладачем.
check-shape-files:
	@python3 ./scripts/extract-lab-queries.py
	@python3 ./front/app/shapecheck_files.py

# Те саме питання, що й check-rules, але на реальних звітах студентів:
# скільки спрацювань каталогу тихих помилок витримує розбір, а скільки
# пояснюється контекстом. Без бази й без docker.
check-rules-files:
	@python3 ./front/app/rulecheck_files.py $(or $(DATASET),)

# Набір даних для публікації: лише дослідницька когорта, тексти без літералів.
# OUT=… каталог, ARGS=--raw вимикає очищення (назовні так не віддавати).
export-dataset:
	@docker compose exec -T front python -m app.export $(or $(OUT),/tmp/wireward-dataset) $(ARGS)

# Показники процесу за замороженими формулами (docs/11-indicators.md).
# DAYS=0 — увесь журнал.
indicators:
	@docker compose exec -T front python -m app.indicators $(or $(DAYS),0)

# Показує, що вийде; записує лише з --apply.
seed-labs:
	@./scripts/seed-labs.sh $(or $(FILE),labs/example.md) $(ARGS)

test-clients:
	@./scripts/test-clients.sh "$(ROLE)" "$(DB)" "$(TOKEN)"

test-quota:
	@./scripts/test-quota.sh "$(ROLE)" "$(DB)" "$(TOKEN)"

test-load:
	@docker compose exec -T front python -m app.loadtest run $(or $(N),40) $(or $(SECS),30)

test-load-clean:
	@docker compose exec -T front python -m app.loadtest clean

# Попередження (код 1) для make не є провалом — інакше «підійшли до квоти»
# ламало б збірку. Тривога (код 2) провалює. У cron зручніше кликати сам
# скрипт: `./scripts/health.sh || mail -s 'Observer' …`.
health:
	@./scripts/health.sh || [ $$? -eq 1 ]

# Готовність до розгортання: конфігурація, а не працездатність. Попередження
# (код 1) не провалюють ціль, заборона (код 2) — провалює.
# Чекліст безпеки, який перевіряє себе сам. Дивиться на РЕПОЗИТОРІЙ —
# інваріанти, що мусять триматися незалежно від машини; preflight поруч
# дивиться на саму машину.
#
# На СЕРВЕРІ авторитетний лише частково: деплой синхронізує compose.yaml,
# postgres/ і scripts/, але НЕ front/app і pgproxy — вони живуть в образах
# (docs/10-deploy.md, розділ 2), і checkout на сервері лишається знімком
# першого розгортання. Перевірки, що читають ці джерела (наприклад OIDC у
# front/app/main.py), там даватимуть ЗЛАМАНО на застарілому тексті, навіть
# коли деплой свіжий. Авторитетний прогін — тут, на машині розробки, перед
# експортом образів.
audit:
	@./scripts/audit.sh

preflight:
	@./scripts/preflight.sh || [ $$? -eq 1 ]

# Показує розклад і перевіряє передумови; НІЧОГО не встановлює. Установка —
# ./scripts/install-cron.sh --apply --mailto=..., бо crontab це вже поза
# docker-стеком проєкту.
cron:
	@./scripts/install-cron.sh

# Бекап і перевірка відновлення — окремі цілі навмисно: копія, яку ніхто
# жодного разу не відновив, не є копією.
# Сервер збірку не робить — тільки docker load + up. Архітектура однакова
# (x86), тому образи, зібрані тут, підуть на сервері без змін.
images-export:
	@./scripts/export-images.sh

backup:
	@./scripts/backup.sh

restore-test: backup
	@./scripts/restore.sh --check "$$(ls -1t backups/observer-*.dump | head -1)"

# Другий шар копії — фізичний кластер і WAL просто в S3-сумісне сховище,
# без проміжного файла на диску (ADR-022). Локальний дамп журналу (ADR-017)
# лишається як був: він логічний і вибірковий, цей — про втрату машини цілком.
backup-s3:
	@./scripts/backup-s3.sh

# Повний цикл проти локального MinIO: архівування, базова копія, зміна після
# копії, відновлення в порожній кластер, звірка рядків. Робочий том не
# чіпається. KEEP=1 лишає MinIO і відновлений кластер піднятими.
#
# НЕ ДЛЯ СЕРВЕРА З БОЙОВИМ ОФСАЙТОМ: тест перемикає архівування кластера на
# MinIO і лишає archive_mode = off, тобто робить діру в бойовому потоці WAL.
# Скрипт сам відмовляється працювати, якщо в .env заданий WALG_S3_PREFIX;
# свідомий обхід — ALLOW_ON_PROD=1 make test-backup-s3.
test-backup-s3:
	@./scripts/test-backup-s3.sh

reset:
	docker compose down -v
	BUILD_COMMIT="$$(git rev-parse --short HEAD)$$(git diff --quiet || echo -dirty)" \
	BUILD_DATE="$$(git show -s --format=%cI HEAD)" \
	docker compose up -d --build
