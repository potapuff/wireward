#!/usr/bin/env bash
# Генерує секрети в ./secrets/. Ідемпотентний: наявні файли не чіпає.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p secrets
chmod 700 secrets

gen() {
  local f="secrets/$1"
  if [[ -s "$f" ]]; then echo "  = $1 (вже є)"; return; fi
  # printf без переводу рядка: POSTGRES_PASSWORD_FILE читає файл цілком,
  # зайвий \n став би частиною пароля.
  printf '%s' "$(openssl rand -base64 33 | tr -d '\n/+=' | head -c 40)" > "$f"
  # 0644, а не 0600. Compose монтує ці файли як docker secrets із їхніми ж
  # правами, а всередині контейнера вони читаються користувачем postgres
  # (uid 999) — при 0600 initdb-скрипт падає з Permission denied.
  # Захист дає каталог: ./secrets має режим 0700, тож на хості до файлів
  # ніхто, крім власника, не дістанеться.
  chmod 644 "$f"
  echo "  + $1"
}

gen pg_superuser_password
gen pg_provisioner_password
gen pg_app_password
gen pg_shipper_password
# Роль обслуговування баз студентів (З15б): член pg_maintain, вміє лише
# VACUUM/ANALYZE і лише в базах, куди їй дали CONNECT.
gen pg_maintenance_password
# Ключ, з якого виводяться паролі ролей студентів: HMAC(master_secret, student_id).
# Ротація цього файлу вимагає перевидачі паролів усіх ролей u_*.
gen master_secret
# Ключ підпису сесійної куки веба. До СУБД стосунку не має.
gen session_secret

# Ключі до S3-сумісного сховища бекапів (ADR-022). Локально це випадкова пара,
# якою піднімається тестовий MinIO (`make test-backup-s3`) — тобто повний цикл
# архівування й відновлення перевіряється без жодного хмарного доступу.
# НА СЕРВЕРІ обидва файли треба ПЕРЕЗАПИСАТИ ключами реального сховища:
#   printf '%s' 'AKIA…'  > secrets/s3_access_key
#   printf '%s' 'секрет' > secrets/s3_secret_key
# Скрипт наявні файли не чіпає, тож повторний `make secrets` бойові ключі не
# затре.
gen s3_access_key
gen s3_secret_key

# Ключ шифрування самих копій (libsodium, ADR-022). S3-ключі захищають доступ
# ДО сховища; цей — вміст копії, яка в тому сховищі лежить. Без нього повний
# журнал усіх студентів разом із їхніми базами зберігається в чужого провайдера
# відкритим текстом, і компрометація бакета (чи просто помилка в його
# політиці) віддає все одразу.
#
# РІВНО 32 БАЙТИ: crypto_secretstream приймає ключ фіксованої довжини, а
# WALG_LIBSODIUM_KEY_TRANSFORM ми не виставляємо — тобто вміст файлу йде як
# сирі байти. Алфавіт узятий друкований, щоб ключ можна було переписати руками
# з паперу; це не зменшує ентропію нижче ~190 біт.
#
# КЛЮЧ МАЄ ЖИТИ НЕ ЛИШЕ НА ЦІЙ МАШИНІ. Копія, зашифрована ключем, який згорів
# разом із сервером, — це не копія (docs/10-deploy.md §6б).
gen_key32() {
  local f="secrets/$1"
  if [[ -s "$f" ]]; then echo "  = $1 (вже є)"; return; fi
  printf '%s' "$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32)" > "$f"
  chmod 644 "$f"
  echo "  + $1 (32 байти; ЗБЕРЕЖІТЬ КОПІЮ ПОЗА СЕРВЕРОМ)"
}
gen_key32 walg_libsodium_key

# Не пароль, а секрет клієнта Doorkeeper — порожній, якщо клієнт публічний
# (PKCE). Заповнюється вручну значенням із реєстрації в Mix, коли клієнт
# конфіденційний.
if [[ ! -f secrets/oidc_client_secret ]]; then
  : > secrets/oidc_client_secret
  echo "  + oidc_client_secret (порожній — публічний клієнт; заповнити вручну, якщо конфіденційний)"
else
  echo "  = oidc_client_secret (вже є)"
fi

chmod 700 secrets; chmod 644 secrets/* 2>/dev/null || true
echo "Готово. Файли в ./secrets/ не потрапляють у git (.gitignore)."
