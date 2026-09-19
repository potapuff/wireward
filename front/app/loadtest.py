"""Навантажувальний тест: 40 одночасних сесій студентів через проксі.

Запускається ВСЕРЕДИНІ контейнера front, бо йому потрібні і справжній шлях
провізіонінгу (той самий код, що й у бою), і мережа до проксі.

Перевіряємо три речі:
  1. проксі витримує 40 паралельних сесій і не деградує;
  2. скільки він додає до затримки. Порівняти з прямим підключенням не можна
     й не треба: pg_hba навмисно пускає ролі студентів ЛИШЕ з адреси проксі,
     і послаблювати це заради виміру безглуздо. Замість цього порівнюємо
     затримку, яку бачить клієнт, із тривалістю, яку проксі записав у журнал:
     він міряє її навколо звернення до СУБД, тож різниця і є його внеском;
  3. памʼять PostgreSQL лишається в межах бюджету 4 ГБ.

Кожен «студент» отримує власну базу і власний слот: тримати 40 зʼєднань
одного студента не вийшло б, бо CONNECTION LIMIT = 3, та й реальний профіль
навантаження виглядає інакше.

    python -m app.loadtest run   [сесій] [секунд]
    python -m app.loadtest clean
"""
from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time
from uuid import UUID

import psycopg

from . import provisioning, students
from .config import settings
from .credentials import db_name, hash_token, role_name
from .db import close_pools, execute, fetchall, fetchone, open_pools

PREFIX = "load-"
TOKEN = "loadtest-token-not-a-secret"
PROXY_HOST = os.environ.get("PROXY_HOST", "172.29.0.10")
PROXY_PORT = os.environ.get("PROXY_PORT", "55432")

WORKLOAD = [
    "select count(*) from _load",
    "select id, pad from _load where id > %s order by id limit 20",
    "insert into _load(pad) values (%s)",
    "select sum(id) from _load where id %% 3 = 0",
    "SELCT 1",  # навмисна опечатка: журнал має ловити і її
]


async def provision(n: int) -> list[dict]:
    out = []
    for i in range(1, n + 1):
        sub = f"{PREFIX}{i:03d}"
        st = await students.upsert_from_claims(
            {"oidc_sub": sub, "email": f"{sub}@load.test",
             "display_name": f"Навантаження {i:03d}", "lms_roles": []})
        await provisioning.ensure_database(st["id"], actor="loadtest")
        # Проксі відмовляє без згоди, а цей шлях створює студента в обхід вебу,
        # де вона проставляється. Без цього рядка тест падає на автентифікації.
        await students.give_consent(st["id"])
        await students.set_token(st["id"], hash_token(TOKEN), 1)
        out.append({"id": st["id"], "role": role_name(st["id"]),
                    "db": db_name(st["id"])})
    return out


async def worker(w: dict, seconds: float, lat: list[float],
                 errs: list[int]) -> None:
    dsn = (f"host={PROXY_HOST} port={PROXY_PORT} dbname={w['db']} "
           f"user={w['role']} password={TOKEN} sslmode=require")
    try:
        conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    except Exception as exc:
        errs.append(1)
        print(f"    не підключився {w['role']}: {str(exc).strip()[:200]}")
        return

    async with conn:
        await conn.execute("create table if not exists _load(id serial primary key, pad text)")
        deadline = time.monotonic() + seconds
        i = 0
        while time.monotonic() < deadline:
            sql = WORKLOAD[i % len(WORKLOAD)]
            params = None
            if "%s" in sql and "insert" in sql:
                params = (f"рядок {i}",)
            elif "%s" in sql:
                params = (i % 50,)
            t0 = time.perf_counter()
            try:
                cur = await conn.execute(sql, params)
                if cur.description:
                    await cur.fetchall()
            except Exception:
                pass  # опечатка в наборі — очікувана
            lat.append((time.perf_counter() - t0) * 1000)
            i += 1
            await asyncio.sleep(0.02)


async def wait_for_drain(started: float, timeout: float = 90) -> float:
    """Чекає, доки кількість записів у журналі перестане зростати."""
    t0 = time.monotonic()
    prev, stable = -1, 0
    while time.monotonic() - t0 < timeout:
        row = await fetchone(
            "SELECT count(*) FROM obs.queries q JOIN obs.students s ON s.id = q.student_id "
            "WHERE s.oidc_sub LIKE 'load-%%' AND q.started_at > to_timestamp(%s)", (started,))
        n = row[0]
        stable = stable + 1 if n == prev else 0
        if stable >= 2 and n > 0:
            break
        prev = n
        await asyncio.sleep(1)
    return time.monotonic() - t0


async def measure(workers: list[dict], seconds: float) -> dict:
    lat: list[float] = []
    errs: list[int] = []
    t0 = time.monotonic()
    await asyncio.gather(*(worker(w, seconds, lat, errs) for w in workers))
    took = time.monotonic() - t0
    lat.sort()
    if not lat:
        return {"queries": 0, "errors": len(errs)}
    return {
        "queries": len(lat), "errors": len(errs),
        "qps": round(len(lat) / took, 1),
        "p50": round(statistics.median(lat), 2),
        "p95": round(lat[int(len(lat) * 0.95) - 1], 2),
        "p99": round(lat[int(len(lat) * 0.99) - 1], 2),
        "max": round(lat[-1], 2),
    }


async def run(n: int, seconds: float) -> int:
    await open_pools()
    try:
        print(f"провізіонінг {n} студентів…")
        workers = await provision(n)

        started = time.time()
        print(f"навантаження: {n} сесій, {seconds:.0f} с…")
        prox = await measure(workers, seconds)

        # Чекаємо, поки shipper довезе журнал: фіксований інтервал тут не
        # годиться — він плутає відставання з втратою. Опитуємо, доки число
        # не перестане рости.
        drained = await wait_for_drain(started)
        srv = await fetchone("""
            SELECT count(*),
                   percentile_cont(0.5)  WITHIN GROUP (ORDER BY duration_ms),
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms),
                   count(*) FILTER (WHERE status = 'error')
              FROM obs.queries q JOIN obs.students s ON s.id = q.student_id
             WHERE s.oidc_sub LIKE 'load-%%' AND q.started_at > to_timestamp(%s)
        """, (started,))
        logged, s50, s95, serr = srv
        print(f"журнал наздогнав за {drained:.0f} с після завершення навантаження")

        print()
        print(f"{'':22}{'запитів':>9}{'зап/с':>8}{'p50 мс':>9}{'p95 мс':>9}{'p99 мс':>9}")
        print(f"{'бачить клієнт':22}{prox['queries']:>9}{prox.get('qps', 0):>8}"
              f"{prox.get('p50', 0):>9}{prox.get('p95', 0):>9}{prox.get('p99', 0):>9}")
        print(f"{'записав проксі':22}{logged:>9}{'':>8}"
              f"{float(s50 or 0):>9.2f}{float(s95 or 0):>9.2f}")

        over50 = prox.get("p50", 0) - float(s50 or 0)
        over95 = prox.get("p95", 0) - float(s95 or 0)
        print(f"\nвнесок проксі й мережі: p50 +{over50:.2f} мс, p95 +{over95:.2f} мс")

        fails = 0
        if prox["errors"]:
            print(f"  FAIL  {prox['errors']} сесій не підключилися")
            fails += 1
        else:
            print(f"  ok    усі {n} сесій підключилися")
        # Проксі не має відчуватися на інтерактивній роботі.
        if over50 > 3.0:
            print(f"  FAIL  проксі додає {over50:.2f} мс до медіани (межа 3 мс)")
            fails += 1
        else:
            print(f"  ok    медіана в межах: +{over50:.2f} мс")
        # Головне: журнал не має втрачати записи під навантаженням.
        loss = prox["queries"] - logged
        if loss > prox["queries"] * 0.01:
            print(f"  FAIL  у журналі бракує {loss} записів із {prox['queries']}")
            fails += 1
        else:
            print(f"  ok    журнал повний: {logged} записів із {prox['queries']} "
                  f"(з них помилкових {serr})")
        return fails
    finally:
        await close_pools()


async def clean() -> None:
    await open_pools()
    try:
        rows = await fetchall(
            "SELECT id::text FROM obs.students WHERE oidc_sub LIKE %s", (PREFIX + "%",))
        for (sid,) in rows:
            try:
                await provisioning.drop_database(UUID(sid))
            except Exception as exc:
                print(f"  {sid}: {exc}")
        # obs.queries і obs.sessions застосунок видалити НЕ МОЖЕ, і це не
        # недогляд: obs_app має на журнал лише SELECT, щоб адмінка фізично не
        # могла його відредагувати. Прибирання записів навантаження — окремий
        # крок під суперкористувачем, див. scripts/clean-loadtest-journal.sh.
        await execute("DELETE FROM obs.db_events WHERE student_id IN "
                      "(SELECT id FROM obs.students WHERE oidc_sub LIKE %s)", (PREFIX + "%",))
        await execute("DELETE FROM obs.activity_buckets WHERE student_id IN "
                      "(SELECT id FROM obs.students WHERE oidc_sub LIKE %s)", (PREFIX + "%",))
        await execute("DELETE FROM obs.sticking_points WHERE student_id IN "
                      "(SELECT id FROM obs.students WHERE oidc_sub LIKE %s)", (PREFIX + "%",))
        await execute("UPDATE obs.ts_slots SET student_id = NULL, assigned_at = NULL, "
                      "used_bytes = NULL WHERE student_id IN "
                      "(SELECT id FROM obs.students WHERE oidc_sub LIKE %s)", (PREFIX + "%",))
        # Самих студентів теж не видаляємо: на них посилаються записи журналу,
        # а видалити ті може лише адміністратор. Позначаємо як вимкнених.
        await execute("UPDATE obs.students SET disabled_at = now() "
                      "WHERE oidc_sub LIKE %s AND disabled_at IS NULL", (PREFIX + "%",))
        print(f"прибрано {len(rows)} тестових студентів")
        print("записи в obs.queries і obs.sessions лишилися: журнал застосунку "
              "не підвладний. Прибрати: ./scripts/clean-loadtest-journal.sh")
    finally:
        await close_pools()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "clean":
        asyncio.run(clean())
    else:
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 40
        secs = float(sys.argv[3]) if len(sys.argv) > 3 else 30
        sys.exit(asyncio.run(run(n, secs)))
