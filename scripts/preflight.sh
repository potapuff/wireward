#!/usr/bin/env bash
# =============================================================================
# Готовність до розгортання: те, що на стенді нешкідливе, а на сервері —
# помилка. Кожна перевірка тут відповідає пункту чекліста з docs/05-security.md
# або кроку з docs/10-deploy.md.
#
# Чому окремо від health.sh: той стежить за працюючою системою («журнал
# наповнюється, служби живі»), а цей — за КОНФІГУРАЦІЄЮ перед виходом на люди.
# Обидва питання ставляться в різні моменти, і змішувати їх означає, що
# половину відповідей щоразу гортають повз.
#
# Секретів не друкує: перевіряє наявність і права, не вміст.
#
# Код виходу: 0 — можна розгортати, 1 — попередження, 2 — так не можна.
# =============================================================================
set -uo pipefail
cd "$(dirname "$0")/.."

rc=0
warn() { printf '  ⚠ %s\n' "$1"; [[ $rc -lt 1 ]] && rc=1; return 0; }
bad()  { printf '  ✖ %s\n' "$1"; rc=2; return 0; }
ok()   { printf '  ok %s\n' "$1"; }

# Значення з .env читаються з файлу, а не з оточення: на сервері важливо саме
# те, що піде в docker compose, а не те, що зараз у вашій оболонці.
env_get() {
  [[ -f .env ]] || return 1
  local v
  v=$(grep -E "^$1=" .env | tail -1 | cut -d= -f2- | sed 's/^"//;s/"$//')
  [[ -n "$v" ]] && printf '%s' "$v"
}

echo "Wireward — готовність до розгортання, $(date '+%d.%m.%Y %H:%M')"

# --- 1. Режими, які не можна лишати -------------------------------------------
echo
echo "1. Режими"
if [[ ! -f .env ]]; then
  bad ".env немає — скопіюйте .env.example і заповніть"
else
  dev=$(env_get DEV_LOGIN_ENABLED)
  # Це найдорожча помилка з усіх можливих: при DEV_LOGIN_ENABLED=1 будь-хто
  # входить будь-ким, включно з викладачем, і жоден інший захист не важить.
  [[ "${dev:-0}" == "1" ]] \
    && bad "DEV_LOGIN_ENABLED=1 — вхід в обхід Mix; будь-хто увійде будь-ким" \
    || ok "DEV_LOGIN_ENABLED вимкнено"

  sec=$(env_get SESSION_COOKIE_SECURE)
  [[ "${sec:-1}" == "1" ]] \
    && ok "SESSION_COOKIE_SECURE=1" \
    || bad "SESSION_COOKIE_SECURE=0 — сесійна кука піде відкритим текстом"
fi

# --- 2. Публічні імена --------------------------------------------------------
echo
echo "2. Імена й адреси"
web=$(env_get WEB_HOST)
case "${web:-}" in
  ""|observer.local|localhost|wireward.local)
    warn "WEB_HOST=${web:-порожньо} — Caddy не візьме сертифікат на таке імʼя" ;;
  *) ok "WEB_HOST=$web" ;;
esac
[[ -n "$(env_get ACME_EMAIL)" ]] \
  && ok "ACME_EMAIL задано" \
  || warn "ACME_EMAIL порожній — не буде попереджень про закінчення сертифіката"

pub=$(env_get PUBLIC_DB_HOST)
case "${pub:-}" in
  ""|localhost|127.0.0.1)
    bad "PUBLIC_DB_HOST=${pub:-порожньо} — студенти отримають рядок підключення до себе" ;;
  *) ok "PUBLIC_DB_HOST=$pub" ;;
esac

redir=$(env_get OIDC_REDIRECT_URI)
if [[ -z "${redir:-}" ]]; then
  bad "OIDC_REDIRECT_URI порожній — вхід через Mix не працюватиме"
elif [[ "$redir" != https://* ]]; then
  bad "OIDC_REDIRECT_URI не на https: $redir"
elif [[ -z "${web:-}" ]]; then
  warn "OIDC_REDIRECT_URI на https, але WEB_HOST порожній — звірити нема з чим"
elif [[ "$redir" != *"$web"* ]]; then
  # Найтихіша з помилок конфігурації: Mix віддає код на адресу, якої вже немає,
  # і виглядає це як «вхід не працює», а не як розбіжність у двох файлах.
  warn "OIDC_REDIRECT_URI ($redir) не збігається з WEB_HOST ($web)"
else
  ok "OIDC_REDIRECT_URI на https і збігається з WEB_HOST"
fi
[[ -n "$(env_get OIDC_CLIENT_ID)" ]] \
  && ok "OIDC_CLIENT_ID задано" \
  || bad "OIDC_CLIENT_ID порожній"

# --- 3. Секрети ---------------------------------------------------------------
echo
echo "3. Секрети (перевіряються наявність і права, не вміст)"
if [[ ! -d secrets ]]; then
  bad "каталогу secrets/ немає — ./scripts/make-secrets.sh"
else
  mode=$(stat -c %a secrets)
  [[ "$mode" == "700" ]] && ok "secrets/ має режим 700" \
                         || warn "secrets/ має режим $mode, очікується 700"
  missing=0
  # s3_* тут теж обов'язкові: compose монтує їх як docker secret у postgres, і
  # без файла стек не підніметься взагалі — незалежно від того, чи налаштований
  # офсайт (ADR-022). `make secrets` кладе туди випадкову пару для тесту.
  # pg_maintenance_password теж обов'язковий: initdb/00-roles.sh створює нею
  # роль obs_maintenance (З15б), і без файла ініціалізація кластера впаде.
  for s in pg_superuser_password pg_provisioner_password pg_app_password \
           pg_shipper_password pg_maintenance_password \
           master_secret session_secret \
           s3_access_key s3_secret_key walg_libsodium_key; do
    if [[ ! -s "secrets/$s" ]]; then
      bad "немає секрету secrets/$s"; missing=1
    else
      # 0644 навмисно: docker монтує секрети з правами хоста, і при 0600
      # користувач postgres (uid 999) у контейнері їх не прочитає.
      fm=$(stat -c %a "secrets/$s")
      [[ "$fm" == "644" ]] || warn "secrets/$s має режим $fm, очікується 644"
    fi
  done
  # Десять обов'язкових. Одинадцятий, oidc_client_secret, свідомо не тут:
  # порожній файл — робочий стан (публічний клієнт із PKCE, compose.yaml).
  [[ $missing -eq 0 ]] && ok "усі десять обов'язкових секретів на місці"
  # Ключ шифрування копій має бути рівно 32 байти: crypto_secretstream іншої
  # довжини не приймає, і дізнатися про це на першому ж backup-push — пів біди,
  # а от дізнатися при відновленні — біда.
  if [[ -s secrets/walg_libsodium_key ]]; then
    kl=$(wc -c < secrets/walg_libsodium_key)
    [[ "$kl" -eq 32 ]] && ok "ключ шифрування копій має 32 байти" \
                       || bad "secrets/walg_libsodium_key має $kl байт замість 32 (ADR-022)"
  fi
fi
# Перевіряється саме ФАЙЛ у secrets/, а не каталог: типовий шаблон
# `/secrets/*` ігнорує вміст, а сам каталог — ні, і `git check-ignore secrets`
# на справному .gitignore дає хибну тривогу.
# `--quiet` приймає рівно один шлях, тому перевіряються обидва по черзі.
ign=0
for path in secrets/master_secret .env; do
  git check-ignore -q "$path" 2>/dev/null || { bad "$path НЕ ігнорується git"; ign=1; }
done
[[ $ign -eq 0 ]] && ok "secrets/* і .env під ігноруванням git"
# .gitkeep відстежується навмисно — він і тримає порожній каталог у репозиторії.
tracked=$(git ls-files secrets .env 2>/dev/null | grep -v '^secrets/\.gitkeep$')
[[ -z "$tracked" ]] || bad "у git уже відстежуються: $tracked"

# --- 4. Сертифікат проксі -----------------------------------------------------
echo
echo "4. TLS проксі"
if ! docker compose ps --format '{{.Service}}' 2>/dev/null | grep -q pgproxy; then
  warn "pgproxy не запущено — перевірку сертифіката пропущено"
else
  # Самопідписаний сертифікат працює, але змушує студентів писати
  # sslmode=require замість verify-full, тобто не перевіряти нікого.
  # openssl читається на ХОСТІ: образ проксі мінімальний і своєї копії не має.
  issuer=$(docker compose exec -T pgproxy cat /var/lib/pgproxy/server.crt 2>/dev/null \
           | openssl x509 -noout -issuer 2>/dev/null | tr -d '\r')
  if [[ -z "$issuer" ]]; then
    warn "сертифіката проксі прочитати не вдалося — перевірте вручну"
  elif [[ "$issuer" == *"observer-pgproxy"* ]]; then
    bad "сертифікат проксі САМОПІДПИСАНИЙ — студенти не зможуть verify-full"
  else
    ok "сертифікат проксі виданий: ${issuer#issuer=}"
  fi
fi

# --- 5. Порти -----------------------------------------------------------------
echo
echo "5. Порти"
if docker compose ps --format '{{.Service}} {{.Ports}}' 2>/dev/null | grep -q .; then
  # Публікація — це рядок зі стрілкою (0.0.0.0:5432->5432/tcp). Голе
  # «5432/tcp» означає лише EXPOSE всередині мережі docker, і це норма.
  pg_pub=$(docker compose ps --format '{{.Service}} {{.Ports}}' 2>/dev/null \
           | awk '$1=="postgres"{$1="";print}' | grep -o '[^ ,]*->[^ ,]*')
  [[ -z "$pg_pub" ]] \
    && ok "postgres назовні не опублікований" \
    || bad "postgres опублікований назовні: $pg_pub — це шлях в обхід журналу"
  front_pub=$(docker compose ps --format '{{.Service}} {{.Ports}}' 2>/dev/null \
              | awk '$1=="front"{$1="";print}' | grep -o '[^ ,]*->[^ ,]*')
  if [[ "$front_pub" == *"0.0.0.0"* || "$front_pub" == *"[::]"* ]]; then
    bad "front опублікований на всі інтерфейси:$front_pub — має бути 127.0.0.1"
  else
    ok "front слухає лише на 127.0.0.1"
  fi
fi

# --- 6. Квота файлової системи ------------------------------------------------
echo
echo "6. Квота"
q=$(docker compose exec -T postgres psql -U postgres -d observer -tAX -c \
      "select count(*) from obs.ts_slots where quota_bytes is null" 2>/dev/null | tr -d '\r')
[[ "${q:-1}" == "0" ]] \
  && ok "у всіх слотів задана квота" \
  || warn "слотів без квоти: ${q:-?} — ./scripts/setup-quota.sh"

# --- 7. Копія й розклад -------------------------------------------------------
echo
echo "7. Копія й розклад"
newest=$(ls -1t backups/observer-*.dump 2>/dev/null | head -1)
[[ -n "$newest" ]] && ok "копія є: $(basename "$newest")" \
                   || bad "жодної резервної копії — make backup"
crontab -l 2>/dev/null | grep -q '>>> observer >>>' \
  && ok "розклад обслуговування встановлено" \
  || warn "розкладу немає — ./scripts/install-cron.sh --apply --mailto=…"

echo
case $rc in
  0) echo "Готово до розгортання." ;;
  1) echo "Можна, але подивіться на попередження вище." ;;
  2) echo "ТАК РОЗГОРТАТИ НЕ МОЖНА: виправте позначене ✖." ;;
esac
exit $rc
