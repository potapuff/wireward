#!/usr/bin/env bash
# =============================================================================
# Розклад обслуговування Wireward (фаза 8, П0.6).
#
# Скрипти є давно; чого не було — розкладу. Різниця принципова: бекап, який
# ніхто не запускає, і health.sh, який ніхто не читає, дають рівно нуль. Мовчазна
# поломка тим і небезпечна, що виглядає як спокійний тиждень.
#
# Ставиться ТРИ записи, і кожен закриває свій вид тихої втрати:
#
#   щогодини  health.sh   — журнал перестав наповнюватися, служба лежить.
#                           Лист лише на ТРИВОЗІ (код 2). Попередження сюди не
#                           йдуть навмисно: «останній бекап 3 дн тому» висить
#                           доти, доки його не полагодять, і щогодинний лист про
#                           це навчає ігнорувати всю розсилку — разом із тим
#                           одним листом, який справді важливий;
#   щоранку   health.sh   — те саме, але з попередженнями (код 1). Раз на добу
#                           їх читають, щогодини — ні;
#   щоночі    analyze_journal() — статистика БАТЬКІВСЬКОЇ obs.queries. Її не
#                           збирає ніхто: автовакуум ходить по партиціях, а
#                           батько власних рядків не має і в його чергу не
#                           потрапляє. Без цього планувальник вічно вважає
#                           журнал порожнім — і це та сама тиха поломка, просто
#                           у вигляді «екран став думати хвилину»;
#   щоночі    backup.sh   — журнал і є продукт; студентські бази навмисно не
#                           копіюються (ADR-017);
#   щоночі    backup-s3.sh — офсайт-копія всього кластера у S3-сумісне сховище
#                           (ADR-022). Рядок з'являється, лише якщо в .env
#                           заданий WALG_S3_PREFIX: інакше це щонічний лист про
#                           несправність, якої немає;
#   щонеділі  restore.sh  — копія, яку жодного разу не відновили, не є копією.
#                           Це не параноя: дамп, який не читається, виглядає
#                           точно так само, як дамп, який читається.
#
# За замовчуванням скрипт НІЧОГО не встановлює — друкує те, що збирається
# зробити. Установка — явним `--apply`: crontab це вже поза docker-стеком
# проєкту, і мовчки туди лізти не годиться.
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

MAILTO="${OBSERVER_MAILTO:-}"
APPLY=0
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    --mailto=*) MAILTO="${arg#--mailto=}" ;;
    -h|--help) sed -n '3,37p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "невідомий аргумент: $arg" >&2; exit 2 ;;
  esac
done

MARK_BEGIN="# >>> observer >>>"
MARK_END="# <<< observer <<<"

# --- перевірки, без яких розклад буде тихо неробочим -------------------------
problems=0
note() { printf '  ⚠ %s\n' "$1"; problems=$((problems + 1)); }

echo "Перевірка передумов"

# cron запускається з мінімальним PATH (типово /usr/bin:/bin) і без вашого
# оточення. docker compose там може просто не знайтися — і запис мовчки нічого
# не робитиме щоночі.
DOCKER_BIN="$(command -v docker || true)"
[[ -n "$DOCKER_BIN" ]] || note "docker не знайдено в PATH"
if [[ -n "$DOCKER_BIN" ]]; then
  docker compose version >/dev/null 2>&1 \
    && echo "  ok docker compose: $DOCKER_BIN" \
    || note "docker є, а 'docker compose' не працює (стара версія?)"
fi

# Той, хто запускає cron, має право на docker без sudo — інакше запис падає
# з permission denied, і побачити це можна лише в пошті, якої немає.
docker info >/dev/null 2>&1 \
  && echo "  ok доступ до docker без sudo є" \
  || note "docker недоступний без sudo — cron-запис не спрацює"

command -v crontab >/dev/null 2>&1 && echo "  ok crontab є" || note "crontab не встановлено"

# Пошта. Без неї health.sh кричить у порожнечу.
MAILER="$(command -v mail || command -v mailx || true)"
if [[ -z "$MAILER" ]]; then
  note "немає ні mail, ні mailx — лист не піде нікуди (apt install bsd-mailx)"
else
  echo "  ok відправник пошти: $MAILER"
fi
[[ -n "$MAILTO" ]] \
  && echo "  ok адреса: $MAILTO" \
  || note "адреса не задана: --mailto=ви@sumdu.edu.ua або OBSERVER_MAILTO"

[[ -d "$ROOT/backups" ]] || echo "  · каталогу backups ще немає — створить перший запуск"

# --- самі записи -------------------------------------------------------------
# Ключове в кожному рядку — `|| ...mail`: пошта йде лише за ненульового коду.
# health.sh віддає 1 на попередження і 2 на тривогу, і обидва варті листа.
HEALTH="$ROOT/scripts/health.sh"
BACKUP="$ROOT/scripts/backup.sh"
RESTORE="$ROOT/scripts/restore.sh"
BACKUP_S3="$ROOT/scripts/backup-s3.sh"

mailer_cmd() { # $1 — тема
  [[ -n "$MAILER" && -n "$MAILTO" ]] \
    && printf '%s -s "%s" %s' "$MAILER" "$1" "$MAILTO" \
    || printf 'cat >> %s/backups/cron.log' "$ROOT"
}

# Офсайт-копію (ADR-022) ставимо в розклад ЛИШЕ якщо сховище налаштоване.
# Рядок cron, який щоночі падає з «WALG_S3_PREFIX порожній», — це щонічний
# лист про несправність, якої немає, і привчає не читати всю розсилку.
OFFSITE_LINE="# офсайт-копії немає: WALG_S3_PREFIX порожній (ADR-022, docs/10-deploy.md §6а)"
if grep -qE '^[[:space:]]*WALG_S3_PREFIX=[^[:space:]]' .env 2>/dev/null; then
  OFFSITE_LINE="10 3 * * * cd $ROOT && $BACKUP_S3 > /tmp/observer-backup-s3.log 2>&1 || $(mailer_cmd 'Wireward: ОФСАЙТ-КОПІЯ НЕ ВДАЛАСЯ') < /tmp/observer-backup-s3.log"
fi

BLOCK=$(cat <<CRON
$MARK_BEGIN
# Розклад Wireward. Керується scripts/install-cron.sh — правити краще там.
# Виведення йде поштою ЛИШЕ при ненульовому коді виходу: щогодинний лист
# «усе гаразд» перестають читати швидше, ніж він устигає стати в пригоді.
#
# Форма всюди однакова — «вивід у файл, лист лише на помилці». Вона нудна
# навмисно: cron виконує рядки через /bin/sh, і будь-який башизм (PIPESTATUS,
# підстановка процесів) тут мовчки не спрацює. Мовчки — тобто саме так, як не
# можна.
SHELL=/bin/sh
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
17 * * * * cd $ROOT && { $HEALTH > /tmp/observer-health.log 2>&1; [ \$? -ge 2 ] && $(mailer_cmd 'Wireward: ТРИВОГА') < /tmp/observer-health.log; } || true
05 8 * * * cd $ROOT && { $HEALTH > /tmp/observer-health.log 2>&1; [ \$? -ge 1 ] && $(mailer_cmd 'Wireward: є на що подивитися') < /tmp/observer-health.log; } || true
55 2 * * * cd $ROOT && docker compose exec -T postgres psql -U postgres -d observer -tAX -c 'SELECT obs.analyze_journal()' > /tmp/observer-analyze.log 2>&1 || $(mailer_cmd 'Wireward: СТАТИСТИКА ЖУРНАЛУ НЕ ОНОВЛЮЄТЬСЯ') < /tmp/observer-analyze.log
30 3 * * * cd $ROOT && $BACKUP > /tmp/observer-backup.log 2>&1 || $(mailer_cmd 'Wireward: БЕКАП НЕ ВДАВСЯ') < /tmp/observer-backup.log
$OFFSITE_LINE
40 4 * * 0 cd $ROOT && $RESTORE --check "\$(ls -1t backups/observer-*.dump | head -1)" > /tmp/observer-restore.log 2>&1 || $(mailer_cmd 'Wireward: ВІДНОВЛЕННЯ НЕ ПЕРЕВІРЯЄТЬСЯ') < /tmp/observer-restore.log
$MARK_END
CRON
)

echo
echo "Записи crontab:"
echo "$BLOCK" | sed 's/^/  /'

if [[ $APPLY -eq 0 ]]; then
  echo
  echo "Нічого не встановлено. Щоб застосувати: $0 --apply --mailto=ви@sumdu.edu.ua"
  [[ $problems -gt 0 ]] && echo "Спершу закрийте $problems зауваження вище."
  exit 0
fi

if [[ $problems -gt 0 ]]; then
  echo
  echo "Відмова: $problems зауваження вище. Розклад, який мовчки не працює," >&2
  echo "гірший за відсутність розкладу — саме тому це помилка, а не попередження." >&2
  exit 1
fi

current="$(crontab -l 2>/dev/null || true)"
cleaned="$(printf '%s\n' "$current" | sed "/^${MARK_BEGIN}$/,/^${MARK_END}$/d")"
printf '%s\n%s\n' "$cleaned" "$BLOCK" | sed '/^$/N;/^\n$/D' | crontab -

echo
echo "Встановлено. Перевірити: crontab -l"
echo "Перший лист має прийти лише тоді, коли справді щось не так."
