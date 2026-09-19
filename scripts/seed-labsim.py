#!/usr/bin/env python3
"""Симуляція семестру: 20 студентів, лаби 02–10, 01.08–01.09.2026.

Навіщо. `check-shape-files` показує, як міра схожості поводиться на СТАТИЧНОМУ
наборі звітів. Це питання інше: як усе це виглядає на екранах Observer (стрічка
сесій, теплокарта, «де студент зараз»), коли роботу над лабами розтягнуто в
часі нерівномірно, з пропусками, як і буває насправді.

Матеріал — РЕАЛЬНІ запити студентів з `labs/labs2/_extracted/queries.jsonl`
(готовий тим самим `extract-lab-queries.py`, що й для З7). Одна «лаба-студент
цієї симуляції» = одна реальна (лаба, студент) з екстракту, тобто фактично один
зданий файл: беремо всі його запити як є, лише розкладаємо в часі.

Пише НАПРЯМУ в obs.queries/obs.sessions/obs.students через `psql -U postgres`,
як `seed-demo.sh` і `seed-year.sh` — той самий запобіжник і той самий спосіб
прибирання, лише префікс інший:

    OBSERVER_ALLOW_DEMO_SEED=1 python3 scripts/seed-labsim.py [--students 20]
    python3 scripts/seed-labsim.py --clean
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JSONL = ROOT / "labs" / "labs2" / "_extracted" / "queries.jsonl"
PREFIX = "labsim"

WINDOW_START = datetime(2026, 8, 1)
WINDOW_END = datetime(2026, 9, 1)
WINDOW_DAYS = (WINDOW_END - WINDOW_START).days

# --------------------------------------------------------- Fingerprint/Kind
# Точна копія pgproxy/internal/proxy/classify.go — щоб симульований журнал
# групувався так само, як зробив би справжній проксі.
_RE_COMMENT = re.compile(r"(?s)/\*.*?\*/|--[^\n]*")
_RE_STRING = re.compile(r"'(?:[^']|'')*'")
_RE_NUMBER = re.compile(r"\b\d+(\.\d+)?\b")
_RE_SPACE = re.compile(r"\s+")


def fingerprint(sql: str) -> str:
    s = _RE_COMMENT.sub(" ", sql)
    s = _RE_STRING.sub("?", s)
    s = _RE_NUMBER.sub("?", s)
    s = _RE_SPACE.sub(" ", s)
    return s.strip().lower()


_KIND_MAP = {
    "select": "select", "table": "select", "values": "select", "with": "select",
    "insert": "insert", "update": "update", "delete": "delete", "copy": "copy",
    "create": "ddl", "alter": "ddl", "drop": "ddl", "truncate": "ddl",
    "comment": "ddl", "grant": "ddl", "revoke": "ddl", "reindex": "ddl",
    "begin": "tcl", "commit": "tcl", "rollback": "tcl", "savepoint": "tcl",
    "start": "tcl", "end": "tcl", "release": "tcl",
}


def kind(sql: str) -> str:
    s = _RE_COMMENT.sub(" ", sql).strip().lstrip("( \t\r\n")
    first = s.split(None, 1)[0].lower() if s.split() else ""
    return _KIND_MAP.get(first, "other")


def sql_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def sql_ts(dt: datetime) -> str:
    # серпень-вересень у Києві — літній час, EEST = UTC+3.
    return f"timestamptz '{dt.isoformat(sep=' ')}+03'"


# ------------------------------------------------------------------- дані
def load_by_lab() -> dict[str, dict[str, list[str]]]:
    if not JSONL.exists():
        sys.exit(f"немає {JSONL} — спершу python3 scripts/extract-lab-queries.py labs/labs2")
    by_lab: dict[str, dict[str, list[str]]] = {}
    for line in JSONL.read_text().splitlines():
        r = json.loads(line)
        by_lab.setdefault(r["lab"], {}).setdefault(r["student"], []).append(r["sql"])
    # лише лаби 02–10 з непорожніми студентами
    return {lab: {s: qs for s, qs in stus.items() if qs}
            for lab, stus in by_lab.items()
            if re.search(r"Lab (0[2-9]|10)\b", lab) and stus}


# ----------------------------------------------------------- симуляція часу
GAP_DAYS = [1, 1, 2, 2, 3, 4, 5, 7, 9]     # пропуски між лабами: більшість малі, деякі великі
CLIENTS = [("DBeaver 24.3.0 - Main <db>", "DBeaver", "24.3.0", "Main", 55432),
           ("DataGrip 2025.2", "DataGrip", "2025.2", None, 55432),
           ("psql", "psql", None, None, 55432),
           ("psql", "psql", None, None, 8080)]


def student_timeline(rng: random.Random, labs: list[str]) -> list[tuple[str, int]]:
    """[(лаба, день_від_старту)] — скільки лаб студент устигає й коли починає
    кожну. Нерівномірно: малі кроки частіше, великі пропуски зрідка; частина
    студентів не доходить до кінця вікна взагалі."""
    out, day = [], rng.choice([0, 0, 1, 2, 3, 5])   # хтось стартує одразу, хтось запізно
    for lab in labs:
        if day > WINDOW_DAYS:
            break
        out.append((lab, day))
        day += rng.choice(GAP_DAYS)
    return out


def build_sessions_for_lab(rng: random.Random, queries: list[str], start_day: int):
    """Розкладає запити однієї лаби на 1–4 сесії (заходи) з паузами між ними
    в годинах-днях і паузами між запитами в секундах усередині сесії."""
    n_sessions = 1 if len(queries) <= 4 else rng.randint(1, min(4, max(1, len(queries) // 5)))
    # межі частин майже рівні, з невеликим випадковим зсувом
    cuts = sorted(rng.sample(range(1, len(queries)), n_sessions - 1)) if n_sessions > 1 else []
    parts, prev = [], 0
    for c in cuts + [len(queries)]:
        parts.append(queries[prev:c])
        prev = c

    sessions = []
    day_offset = 0.0
    for part in parts:
        if not part:
            continue
        day_offset += rng.choice([0, 0, 0, 1, 1, 2])          # частина заходів того самого дня
        hour = rng.uniform(8, 23)
        start = WINDOW_START + timedelta(days=start_day + day_offset, hours=hour)
        if start >= WINDOW_END:
            start = WINDOW_END - timedelta(hours=rng.uniform(1, 5))
        sessions.append((start, part))
    return sessions


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--students", type=int, default=20)
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--clean", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="лише написати SQL у файл, не виконувати")
    a = ap.parse_args()

    if a.clean:
        clean_sql = f"""
DELETE FROM obs.queries          WHERE student_id IN (SELECT id FROM obs.students WHERE oidc_sub LIKE '{PREFIX}-%');
DELETE FROM obs.sessions         WHERE student_id IN (SELECT id FROM obs.students WHERE oidc_sub LIKE '{PREFIX}-%');
DELETE FROM obs.activity_buckets WHERE student_id IN (SELECT id FROM obs.students WHERE oidc_sub LIKE '{PREFIX}-%');
DELETE FROM obs.sticking_points  WHERE student_id IN (SELECT id FROM obs.students WHERE oidc_sub LIKE '{PREFIX}-%');
DELETE FROM obs.db_events        WHERE student_id IN (SELECT id FROM obs.students WHERE oidc_sub LIKE '{PREFIX}-%');
DELETE FROM obs.student_groups   WHERE student_id IN (SELECT id FROM obs.students WHERE oidc_sub LIKE '{PREFIX}-%');
DELETE FROM obs.students WHERE oidc_sub LIKE '{PREFIX}-%';
"""
        run_psql(clean_sql)
        print("симуляцію прибрано")
        return 0

    if os.environ.get("OBSERVER_ALLOW_DEMO_SEED") != "1":
        print("Це пише синтетичні рядки в журнал аудиту.")
        print(f"Лише на стенді: OBSERVER_ALLOW_DEMO_SEED=1 python3 {sys.argv[0]}")
        return 2

    by_lab = load_by_lab()
    labs = sorted(by_lab)
    print(f"лаб у наборі: {len(labs)} — {', '.join(l.split()[1] for l in labs)}")

    rng = random.Random(a.seed)
    students = [f"{PREFIX}-{i:03d}" for i in range(1, a.students + 1)]

    sql_parts = [
        "SELECT obs.ensure_query_partition(d::date) FROM "
        "generate_series(date '2026-08-01', date '2026-09-01', interval '1 month') d;",
        f"""
INSERT INTO obs.students (oidc_sub, email, display_name, consented_at, last_seen_at)
SELECT '{PREFIX}-'||lpad(i::text,3,'0'), '{PREFIX}'||i||'@sim.test',
       'Симуляція · студент '||lpad(i::text,3,'0'),
       {sql_ts(WINDOW_START)}, {sql_ts(WINDOW_END)}
  FROM generate_series(1, {a.students}) i
ON CONFLICT (oidc_sub) DO NOTHING;

INSERT INTO obs.student_groups (student_id, group_name, since)
SELECT id, to_char(created_at, 'YYYY'), created_at::date
  FROM obs.students WHERE oidc_sub LIKE '{PREFIX}-%'
ON CONFLICT DO NOTHING;

-- Bootstrap-тригер (ADR-023) видає is_admin першому рядку порожньої
-- obs.students — на свіжому стенді це був би симуляційний акаунт.
UPDATE obs.students SET is_admin = false, is_teacher = false
 WHERE oidc_sub LIKE '{PREFIX}-%';
""",
    ]

    total_q = total_sessions = 0
    for stu_idx, oidc in enumerate(students, start=1):
        client = rng.choice(CLIENTS)
        timeline = student_timeline(rng, labs)
        for lab, start_day in timeline:
            candidates = by_lab[lab]
            _real_student, queries = rng.choice(list(candidates.items()))
            queries = list(queries)
            half = len(queries) // 2
            tail = queries[half:]
            rng.shuffle(tail)                          # легке перемішування другої половини
            queries[half:] = tail
            for start, part in build_sessions_for_lab(rng, queries, start_day):
                session_id_expr = "gen_random_uuid()"
                app_name, cname, cver, cpurp, port = client
                end = start + timedelta(seconds=sum(rng.uniform(20, 90) for _ in part))
                sql_parts.append(f"""
WITH s AS (
  INSERT INTO obs.sessions (id, student_id, client_addr, application_name,
                            client_name, client_version, client_purpose,
                            listen_port, tls_version, protocol_version,
                            started_at, ended_at, end_reason)
  SELECT {session_id_expr}, id, ('10.50.{stu_idx % 200}.{(stu_idx * 7) % 250 + 1}')::inet,
         {sql_str(app_name)}, {sql_str(cname)},
         {sql_str(cver) if cver else 'NULL'}, {sql_str(cpurp) if cpurp else 'NULL'},
         {port}, 'TLSv1.3', '3.0', {sql_ts(start)}, {sql_ts(end)}, 'terminate'
    FROM obs.students WHERE oidc_sub = {sql_str(oidc)}
  RETURNING id, student_id
)
INSERT INTO obs.queries (session_id, student_id, seq, started_at, duration_ms,
                         sql_text, sql_fingerprint, sql_kind, protocol,
                         status, sqlstate, error_message, rows_affected)
SELECT s.id, s.student_id, v.seq, {sql_ts(start)} + make_interval(secs => v.off),
       v.dur, v.text, v.fp, v.kind, v.proto, v.status, v.sqlstate, v.msg, v.rows
  FROM s, (VALUES
""")
                off = 0.0
                rows = []
                for seq, text in enumerate(part, start=1):
                    off += rng.uniform(15, 90)
                    dur = round(rng.uniform(2, 180), 3)
                    fp, kd = fingerprint(text), kind(text)
                    proto = "simple" if cname == "psql" else "extended"
                    err = rng.random() < 0.08 and kd in ("select", "insert", "update", "delete")
                    if err:
                        stt = "error"
                        sqlstate = sql_str(rng.choice(["42601", "42703", "42P01", "23505"]))
                        msg = sql_str("synthetic: перша спроба не пройшла")
                        ra = "NULL"
                    else:
                        stt, sqlstate, msg = "ok", "NULL", "NULL"
                        ra = str(rng.randint(0, 40))
                    rows.append(f"({seq},{off:.2f},{dur},{sql_str(text)},{sql_str(fp)},"
                                f"{sql_str(kd)},{sql_str(proto)},{sql_str(stt)},{sqlstate},{msg},{ra})")
                    total_q += 1
                sql_parts.append(",\n".join(rows))
                sql_parts.append("""
       ) AS v(seq, off, dur, text, fp, kind, proto, status, sqlstate, msg, rows);
""")
                total_sessions += 1

    sql_parts.append("""
UPDATE obs.sessions se SET queries_total = x.n, errors_total = x.e
  FROM (SELECT session_id, count(*) n, count(*) FILTER (WHERE status='error') e
          FROM obs.queries GROUP BY 1) x
 WHERE x.session_id = se.id AND se.student_id IN
   (SELECT id FROM obs.students WHERE oidc_sub LIKE '""" + PREFIX + """-%');
""")

    print(f"студентів {len(students)}, сесій {total_sessions}, запитів {total_q}")
    full_sql = "\n".join(sql_parts)
    if a.dry_run:
        out = ROOT / "scratchpad-seed-labsim.sql"
        out.write_text(full_sql)
        print(f"--dry-run: SQL записано в {out}, нічого не виконано")
        return 0
    run_psql(full_sql)
    rebuild_activity()
    print("готово. прибрати: python3 scripts/seed-labsim.py --clean")
    return 0


def run_psql(sql: str) -> None:
    proc = subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", "psql",
         "-U", "postgres", "-d", "observer", "-v", "ON_ERROR_STOP=1", "-q"],
        input=sql, text=True, cwd=ROOT)
    if proc.returncode != 0:
        sys.exit(f"psql завершився з кодом {proc.returncode}")


def rebuild_activity() -> None:
    """`obs.activity_buckets`/`sticking_points` рахуються на 3/6 годин наперед
    фоновим циклом (`activity.background_loop`) — наш журнал заднім числом він
    не підхопить. `rebuild_all()` перебудовує з нуля."""
    code = ("import asyncio\n"
            "from app import db, activity\n"
            "async def main():\n"
            "    await db.open_pools()\n"
            "    try:\n"
            "        await activity.rebuild_all()\n"
            "    finally:\n"
            "        await db.close_pools()\n"
            "asyncio.run(main())\n")
    proc = subprocess.run(["docker", "compose", "exec", "-T", "front", "python", "-c", code],
                          cwd=ROOT)
    if proc.returncode != 0:
        print("! перебудова obs.activity_buckets не вдалася — див. лог front",
              file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
