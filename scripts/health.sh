#!/usr/bin/env bash
# =============================================================================
# Перевірка стану Wireward — те, що має помітити людина, поки воно не стало
# втратою (фаза 8).
#
# Кожна перевірка тут стоїть тому, що відповідна поломка вже траплялася або
# траплялася б мовчки: журнал, який не наповнюється, виглядає точно так само,
# як тиждень канікул. Скрипт навмисно не надсилає нічого сам — він придатний і
# для ока, і для cron (`make health || пошта`), і не тягне за собою залежності.
#
# Код виходу: 0 — усе гаразд, 1 — є попередження, 2 — є тривога.
# =============================================================================
set -uo pipefail
cd "$(dirname "$0")/.."

rc=0
warn() { printf '  ⚠ %s\n' "$1"; [[ $rc -lt 1 ]] && rc=1; return 0; }
bad()  { printf '  ✖ %s\n' "$1"; rc=2; return 0; }
ok()   { printf '  ok %s\n' "$1"; }

q() { docker compose exec -T postgres psql -U postgres -d observer -tAX -c "$1" 2>/dev/null | tr -d '\r'; }

echo "Wireward — стан на $(date '+%d.%m.%Y %H:%M')"

# --- 1. Журнал наповнюється ---------------------------------------------------
# Відкритий проміжок означає, що shipper застряг просто зараз.
open_gaps=$(q "select count(*) from obs.journal_gaps where ended_at is null")
if [[ "${open_gaps:-0}" -gt 0 ]]; then
  bad "журнал НЕ наповнюється: відкритих проміжків $open_gaps"
  q "select id, started_at, reason, details->>'error' from obs.journal_gaps
      where ended_at is null order by started_at" | sed 's/^/     /'
else
  ok "відкритих проміжків у журналі немає"
fi

recent_gaps=$(q "select count(*) from obs.journal_gaps
                  where ended_at > now() - interval '7 days'")
[[ "${recent_gaps:-0}" -gt 0 ]] && warn "за тиждень журнал зупинявся $recent_gaps раз(и) — подивіться причину"

# --- 2. Записи доходять -------------------------------------------------------
# «Давно нічого не писалося» саме по собі не є поломкою (канікули, ніч), тому
# це попередження, а не тривога. Але мовчазний shipper виглядає так само.
last=$(q "select coalesce(extract(epoch from now() - max(started_at))::bigint, -1) from obs.queries")
if [[ "${last:--1}" -lt 0 ]]; then
  warn "журнал порожній"
elif [[ "$last" -gt 86400 ]]; then
  warn "останній запис $((last / 3600)) год тому"
else
  ok "останній запис $((last / 60)) хв тому"
fi

# --- 3. DEFAULT-партиція ------------------------------------------------------
# Непорожня DEFAULT-партиція — не косметика: створити партицію за той місяць
# після цього вже НЕ ВИЙДЕ, і обслуговування журналу зупиниться.
def_rows=$(q "select count(*) from obs.queries_default")
if [[ "${def_rows:-0}" -gt 0 ]]; then
  bad "DEFAULT-партиція містить $def_rows записів — потрібен obs.rescue_default_partition()"
else
  ok "DEFAULT-партиція порожня"
fi

# Партиція на наступний місяць має існувати ЗАЗДАЛЕГІДЬ, інакше записи впадуть
# у DEFAULT саме опівночі першого числа.
next=$(q "select count(*) from pg_class
           where relname = 'queries_' || to_char(current_date + interval '1 month', 'YYYY_MM')")
[[ "${next:-0}" -eq 1 ]] && ok "партиція на наступний місяць є" \
                         || bad "немає партиції на наступний місяць"

# --- 4. Квоти -----------------------------------------------------------------
over=$(q "select count(*) from obs.ts_slots
           where student_id is not null and quota_bytes > 0 and used_bytes >= quota_bytes")
near=$(q "select count(*) from obs.ts_slots
           where student_id is not null and quota_bytes > 0
             and used_bytes >= quota_bytes * 0.9 and used_bytes < quota_bytes")
[[ "${over:-0}" -gt 0 ]] && warn "вичерпали квоту: $over студент(ів)"
[[ "${near:-0}" -gt 0 ]] && warn "підійшли до квоти впритул: $near студент(ів)"
[[ "${over:-0}" -eq 0 && "${near:-0}" -eq 0 ]] && ok "квоти в межах"

stale=$(q "select coalesce(extract(epoch from now() - min(checked_at))::bigint, -1)
             from obs.ts_slots where student_id is not null")
[[ "${stale:--1}" -gt 3600 ]] && warn "розмір слотів не оновлювався $((stale / 60)) хв — job квоти стоїть?"

# Кеш розміру слота живиться тим самим фоновим циклом front раз на хвилину
# (front/app/quota.py:background_loop). Перевірка вище ловить "стоїть уже
# годину загалом", ця — скільки саме слотів устигли протухнути: якщо front
# стояв коротше, окремі слоти могли не оновитися й досі, і треймап диска
# (З14) для них показує вчорашній стан як сьогоднішній.
stale_slots=$(q "select count(*) from obs.ts_slots
                  where student_id is not null
                    and (checked_at is null or checked_at < now() - interval '1 hour')")
if [[ "${stale_slots:-0}" -gt 0 ]]; then
  warn "used_bytes застарів у $stale_slots слот(ах) — треймап диска (З14) бреше"
else
  ok "used_bytes свіжий у всіх слотах"
fi

# --- 5. Вхід в обхід Mix ------------------------------------------------------
# DEV_LOGIN на продакшені — це вхід під будь-ким без пароля.
if grep -q '^DEV_LOGIN_ENABLED=1' .env 2>/dev/null; then
  warn "DEV_LOGIN_ENABLED=1 у .env — на сервері має бути 0"
fi
dev=$(q "select count(*) from obs.db_events where event='dev_login' and at > now() - interval '7 days'")
[[ "${dev:-0}" -gt 0 ]] && warn "за тиждень $dev вхід(ів) в обхід Mix (dev_login)"

# --- 6. Резервна копія --------------------------------------------------------
# Копія, знята місяць тому, гірша за відсутність копії: вона створює
# впевненість, якої немає.
newest=$(ls -1t backups/observer-*.dump 2>/dev/null | head -1)
if [[ -z "$newest" ]]; then
  bad "резервної копії немає жодної — ./scripts/backup.sh"
else
  age=$(( ($(date +%s) - $(stat -c %Y "$newest")) / 3600 ))
  [[ $age -gt 48 ]] && warn "останній бекап $((age / 24)) дн тому: $newest" \
                    || ok "останній бекап $age год тому"
fi

# --- 6a. Офсайт-копія: архів WAL (ADR-022) ------------------------------------
# Локальний дамп і офсайт ламаються по-різному, тому й перевіряються окремо.
# Найтихіший спосіб втратити офсайт — archive_command, який перестав доходити
# до сховища: кластер живий, дамп на місці, а PITR уже немає, і дізнаються про
# це в день, коли машини не стало.
# `always` — теж увімкнений архів (штатне значення для standby, який архівує і
# власний, і отриманий по реплікації WAL). Умова тут, у check-phase0.sh і в
# backup-s3.sh має бути ОДНА: свого часу вона розійшлася, і `always` давав три
# різні відповіді на одне питання — «архів увімкнено?».
arch_mode=$(q "select current_setting('archive_mode')")
if [[ "$arch_mode" != "on" && "$arch_mode" != "always" ]]; then
  # На локальному стенді це норма; на сервері — привід поставити офсайт.
  ok "офсайт-архів WAL вимкнено (ADR-022: WALG_S3_PREFIX порожній)"
else
  stuck=$(q "select case when last_failed_wal is not null
                     and (last_archived_wal is null or last_failed_wal > last_archived_wal)
                   then 1 else 0 end from pg_stat_archiver")
  if [[ "${stuck:-1}" == "1" ]]; then
    bad "архіватор WAL стоїть: $(q "select coalesce(last_failed_wal,'?')||', провалів '||failed_count from pg_stat_archiver")"
  else
    lag=$(q "select coalesce(extract(epoch from now() - last_archived_time)::bigint, -1) from pg_stat_archiver")
    # archive_timeout = 15min, тож пауза понад годину означає, що сегменти не
    # доїжджають, навіть якщо лічильник провалів чистий.
    [[ "${lag:--1}" -lt 0 || "${lag}" -gt 3600 ]] \
      && warn "останній сегмент WAL заархівовано $((lag / 60)) хв тому" \
      || ok "архів WAL свіжий ($((lag / 60)) хв)"
  fi
fi

# --- 6б. Стеля на pg_wal ------------------------------------------------------
# Архіватор, який не доходить до сховища, НЕ втрачає сегменти — він їх копить:
# PostgreSQL не переробить WAL, поки той не заархівовано. Тобто зламана
# архівація перетворюється на заповнений диск, і кластер зупиняється насухо
# (PANIC: could not write to file … No space left on device). Перевірка вище
# ловить сам факт зупинки архіватора, ця — наслідок, який доходить до відмови
# швидше, ніж хтось прочитає лист.
#
# Поріг — удвічі більший за max_wal_size (1 ГБ у нашому конфізі), але не менший
# за 1 ГБ. Штатний ріст до max_wal_size між контрольними точками нормальний;
# удвічі більший означає, що щось тримає сегменти: архів, слот реплікації або
# зависла довга транзакція. Це ТРИВОГА, не попередження: місце на диску
# закінчується монотонно, і «подивимося вранці» тут не працює.
walsz=$(q "select coalesce((select sum(size) from pg_ls_waldir()), 0)")
wallim=$(q "select greatest(pg_size_bytes('1GB'), 2 * pg_size_bytes(current_setting('max_wal_size')))")
walh=$(q "select pg_size_pretty(coalesce((select sum(size) from pg_ls_waldir()), 0))")
if [[ -z "${walsz:-}" || -z "${wallim:-}" ]]; then
  warn "розмір pg_wal не читається"
elif [[ "$walsz" -gt "$wallim" ]]; then
  bad "pg_wal розрісся до $walh (стеля $(q "select pg_size_pretty($wallim)")) — сегменти не переробляються: архів, слот реплікації або довга транзакція"
  q "select slot_name, active, pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn))
       from pg_replication_slots" | sed 's/^/     слот: /'
else
  ok "pg_wal у межах норми ($walh)"
fi

# --- 7. Служби ----------------------------------------------------------------
for svc in postgres pgproxy front; do
  st=$(docker compose ps --format '{{.Service}} {{.State}}' 2>/dev/null | awk -v s="$svc" '$1==s{print $2}')
  [[ "$st" == "running" ]] && ok "$svc працює" || bad "$svc: ${st:-не запущено}"
done

echo
case $rc in
  0) echo "Усе гаразд." ;;
  1) echo "Є попередження — подивитися, але не горить." ;;
  2) echo "ТРИВОГА: щось із переліченого треба лагодити зараз." ;;
esac
exit $rc
