#!/usr/bin/env bash
# Чекліст безпеки, який перевіряється сам (docs/05-security.md).
#
# Відрізняється від `make preflight` тим, ЩО перевіряє. Preflight дивиться на
# конкретну машину: чи вимкнено DEV_LOGIN, чи справжній сертифікат, чи є
# бекап. Цей скрипт дивиться на РЕПОЗИТОРІЙ — інваріанти, які мусять триматися
# незалежно від машини, і які найлегше зламати необережною правкою.
#
# Ці перевірки навмисно текстові, а не запуск стека: половина з них про те,
# чого на працюючому стенді не видно взагалі (чи скинуто capabilities, чи є
# CSRF на маршруті, який ніхто не викликав). Ціна такого підходу відома —
# перевіряється написане, а не виконане; тому там, де поведінку можна
# перевірити по-справжньому, це роблять інші цілі: test-provisioning,
# test-clients, test-go.
#
#   ./scripts/audit.sh        0 — усе гаразд, 1 — є розбіжності
set -uo pipefail
cd "$(dirname "$0")/.."

bad=0
ok()   { printf '  ok    %s\n' "$1"; }
fail() { printf '  ЗЛАМАНО  %s\n' "$1"; bad=1; }
note() { printf '        %s\n' "$1"; }

# has <файл> <regex> <опис>  — рядок мусить бути
has() { grep -qE "$2" "$1" && ok "$3" || fail "$3"; }
# hasnt <файл> <regex> <опис> — рядка бути НЕ мусить
hasnt() { grep -qE "$2" "$1" 2>/dev/null && fail "$3" || ok "$3"; }

echo "Мережа й контейнери"
# postgres не публікує порт: єдиний шлях до даних студента — через проксі,
# тобто через журнал. Опублікований 5432 обходить продукт цілком.
#
# Блок матеріалізується в змінну заздалегідь — двічі, з двох причин:
# 1) `/^  postgres:/,/^  [a-z-]+:$/` як діапазон awk самозамикається: перший
#    же рядок "  postgres:" збігається водночас і з початком, і із загальним
#    кінцем діапазону, тож awk віддає РІВНО один рядок замість усього блоку —
#    перевірка мовчки ніколи нічого не сканувала (не моя правка, застаріла
#    пастка; знайдено 14.09.2026 саме через це). Той самий прапорцевий
#    патерн, що вже вище для read_only/cap_drop/no-new-privileges, і тут
#    коректно зупиняється на НАСТУПНОМУ ключі, а не на першому ж рядку.
# 2) пряме `awk ... | grep -q` під `set -o pipefail` час від часу давало
#    ЗЛАМАНО на позитивному збігу — grep закриває канал одразу після першого
#    влучання, awk ще пише і встигає отримати SIGPIPE, а pipefail бере код
#    виходу саме awk (141), а не grep (0). Матеріалізація в змінну прибирає
#    канал між ними — echo встигає віддати весь рядок одним записом, перш
#    ніж grep щось прочитає.
postgres_blk=$(awk -v s="  postgres:" '$0==s{f=1;next} /^  [a-z-]+:$/{f=0} f' compose.yaml)
if echo "$postgres_blk" | grep -qE '^\s+ports:'; then
  fail "postgres не публікує портів"
else ok "postgres не публікує портів"; fi
has compose.yaml '^\s+internal: true' "мережа data — internal"

for svc in postgres front pgproxy caddy; do
  blk=$(awk -v s="  $svc:" '$0==s{f=1;next} /^  [a-z-]+:$/{f=0} f' compose.yaml)
  for opt in 'read_only: true' 'cap_drop: [ALL]' 'no-new-privileges:true'; do
    echo "$blk" | grep -qF "$opt" && ok "$svc · $opt" || fail "$svc · $opt"
  done
done
# Скидати capabilities і лишати root у контейнері — половина справи.
has front/Dockerfile '^USER [0-9]+' "front запускається не від root"
has pgproxy/Dockerfile '^USER [0-9]+' "pgproxy запускається не від root"

echo
echo "Доступ до кластера"
HBA=postgres/conf/pg_hba.conf.tmpl
has "$HBA" 'host\s+all\s+all\s+0\.0\.0\.0/0\s+reject' "pg_hba: усе інше — reject"
has "$HBA" 'scram-sha-256' "pg_hba: scram-sha-256"
# `trust` для host — це вхід без пароля з мережі. Для local він штатний:
# сокет доступний лише всередині контейнера.
host_lines=$(grep -E '^host' "$HBA")
if echo "$host_lines" | grep -q 'trust'; then
  fail "pg_hba: жодного trust для host"
else ok "pg_hba: жодного trust для host"; fi
# Напівініціалізований кластер бадьоро рапортує «healthy», якщо питати лише
# pg_isready: PGDATA існує, initdb-скрипти пропущені, схеми немає.
echo "$postgres_blk" | grep -q 'obs\.' \
  && ok "healthcheck дивиться далі за pg_isready" \
  || fail "healthcheck дивиться далі за pg_isready"

echo
echo "Проксі"
has pgproxy/internal/proxy/classify.go 'work_mem' "ADR-010: work_mem під забороною"
has pgproxy/internal/proxy/classify.go 'statement_timeout' "ADR-010: statement_timeout під забороною"
grep -q 'SSLRequest\|sslRequest' pgproxy/internal/proxy/*.go \
  && ok "TLS: SSLRequest обробляється явно" || fail "TLS: SSLRequest обробляється явно"

echo
echo "Веб"
has front/app/security.py 'httponly=True' "кукі сесії HttpOnly"
has front/app/security.py 'samesite="lax"' "кукі сесії SameSite=Lax"
has front/app/security.py 'secure=settings\.cookie_secure' "кукі сесії Secure з конфігурації"
has front/app/main.py 'write_session\(resp' "ротація сесії при вході"
has front/app/oidc.py 'code_challenge_method' "OIDC: PKCE підтримується (за прапорцем OIDC_PKCE)"
has front/app/main.py 'consteq\(p\["state"\], state\)' "OIDC: state звіряється з сесією (константний час)"
has front/app/config.py 'not self\.oidc_pkce and not optional_secret' \
  "OIDC: без client_secret і без PKCE обмін коду зупиняється навмисно"
# ADR-012: ключа Mix у нас немає навмисно, і поява його в конфігурації
# означала б, що хтось вирішив перевіряти підпис id_token самотужки.
if grep -rn 'signing_key' front/app/ compose.yaml 2>/dev/null | grep -v '^Binary'; then
  fail "ADR-012: signing_key Mix у конфігурації немає"
else ok "ADR-012: signing_key Mix у конфігурації немає"; fi

# CSRF на КОЖНІЙ мутації. Перевіряється не наявність слова в файлі, а те, що
# воно є в тілі кожного POST-маршруту: один забутий маршрут — уся дірка.
miss=""
while read -r file line; do
  body=$(sed -n "${line},$((line + 16))p" "$file")
  echo "$body" | grep -q 'csrf' || miss="$miss $file:$line"
done < <(grep -nE '@(app|router)\.post\(' front/app/main.py front/app/admin.py \
         | grep -v '/auth/dev' | awk -F: '{print $1" "$2}')
if [ -n "$miss" ]; then fail "CSRF на кожній мутації —$miss"
else ok "CSRF на кожній мутації"; fi
note "/auth/dev виключено свідомо: сесії ще немає, а сам маршрут дає 404 при DEV_LOGIN_ENABLED=0"

# Те саме для адмінки: require_admin ЧИ require_staff (ADR-023 — викладач
# бачить звужені екрани тим самим кодом) має стояти в кожному маршруті, а не
# лише перед промальовуванням меню.
miss=""
while read -r line; do
  body=$(sed -n "${line},$((line + 12))p" front/app/admin.py)
  echo "$body" | grep -qE 'require_admin|require_staff' || miss="$miss admin.py:$line"
done < <(grep -nE '@router\.(get|post)\(' front/app/admin.py | awk -F: '{print $1}')
if [ -n "$miss" ]; then fail "require_admin/require_staff у кожному маршруті адмінки —$miss"
else ok "require_admin/require_staff у кожному маршруті адмінки"; fi

echo
echo "Секрети"
has .gitignore '^/secrets/\*' "secrets/ поза git"
has .gitignore '^\.env' ".env поза git"
for f in $(git ls-files | grep -E '^(secrets/|\.env$)' | grep -v '\.gitkeep'); do
  fail "у git потрапив секрет: $f"
done
git ls-files | grep -qE '^(secrets/[^.]|\.env$)' || ok "у git немає жодного секрету"

echo
if [ $bad -eq 0 ]; then
  echo "Чекліст: усе, що перевіряється статично, на місці."
  echo "Решта — на сервері: make preflight."
else
  echo "Є розбіжності з docs/05-security.md. Кожна з них колись була рішенням;"
  echo "якщо рішення змінилося — змінити треба і документ, а не лише код."
fi
exit $bad
