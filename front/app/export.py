"""Експорт журналу у формі, близькій до ProgSnap2 (П2.7).

ProgSnap2 (Price, Dong, Edwards, ITiCSE 2020) — усталений формат даних про
ПРОЦЕС програмування. Двадцять років літератури про новачків (Jadud, Watwin,
Becker) працюють саме з такими подіями, і поки наш журнал лишається у власній
схемі, порівняти наші числа з їхніми не може ніхто, включно з нами.

## Що з чим зіставлено, і де межа зіставлення

ProgSnap2 писався під компільовані мови, тож пряме перенесення неможливе.
Рішення й підстави:

| ProgSnap2            | у нас                                              |
|----------------------|----------------------------------------------------|
| `Run.Program`        | запит виконався (`status='ok'`)                     |
| `Compile.Error`      | запит не виконався (`status='error'`)               |
| `CompileMessageType` | `SQLSTATE`                                          |
| `CodeStateID`        | сам запит                                           |
| `AssignmentID`       | лабораторна, знайдена нечітким збігом (ADR-019)     |
| `SubjectID`          | псевдонім студента                                  |

Головна відмінність, яку треба назвати вголос: у ProgSnap2 `Compile` і `Run`
— різні події над одним станом коду, а SQL і компілюється, і виконується одним
викликом. Тому одна наша подія стає однією подією ProgSnap2, а не парою; поле
`EventType` розрізняє результат. Хто порівнюватиме наші EQ з чужими, має це
знати: у нас на пару подій припадає рівно два запити, а не два компілювання
одного коду.

## Знеособлення

`SubjectID` — HMAC від `master_secret`: стабільний між експортами (той самий
студент — той самий код) і незворотний без секрету, який у набір даних не
потрапляє.

Потрапляють лише студенти з `obs.research_cohort` — тобто ті, хто дав окрему
згоду на дослідження ([ADR-020](../../docs/02-decisions.md)). Це єдина вхідна
точка навмисно: умову `research_consent = 'given'`, написану руками в
черговому запиті, досить забути один раз.

За замовчуванням тексти запитів **очищаються від літералів** (`sql_fingerprint`
замість `sql_text`): студент міг вписати в `INSERT` що завгодно, і набір даних
для публікації не місце це перевіряти. `--raw` вимикає очищення — лише для
розбору всередині курсу, не для викладання назовні.
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
from pathlib import Path

PROGSNAP_VERSION = "6"

MAIN_COLUMNS = [
    "EventID", "SubjectID", "Order", "EventType", "ServerTimestamp",
    "ServerTimezone", "SessionID", "CourseID", "AssignmentID",
    "CodeStateID", "CompileResult", "CompileMessageType", "CompileMessageData",
    "ExecutionResult", "ToolInstances", "X-Duration-ms", "X-Rows", "X-SqlKind",
]


def pseudonym(master: bytes, value: str, prefix: str = "S") -> str:
    """Стабільно і незворотно. Без master_secret зіставити з людиною не можна,
    а сам секрет у набір даних не потрапляє ніколи."""
    mac = hmac.new(master, value.encode(), hashlib.sha256).hexdigest()
    return f"{prefix}{mac[:12]}"


def event_type(status: str) -> str:
    return "Compile.Error" if status == "error" else "Run.Program"


def code_state_id(sql: str) -> str:
    """Ідентифікатор стану коду — хеш самого тексту. Однаковий запит,
    виконаний двічі, дає той самий CodeStateID: у ProgSnap2 це саме те, що
    означає «стан коду не змінився»."""
    return hashlib.sha256(sql.encode()).hexdigest()[:16]


def main_row(rec: dict, subject: str, order: int, session: str,
             course: str, sql: str) -> dict:
    return {
        "EventID": rec["id"],
        "SubjectID": subject,
        "Order": order,
        "EventType": event_type(rec["status"]),
        "ServerTimestamp": rec["started_at"].isoformat(timespec="milliseconds"),
        "ServerTimezone": rec["started_at"].strftime("%z"),
        "SessionID": session,
        "CourseID": course,
        "AssignmentID": rec.get("lab_id") or "",
        "CodeStateID": code_state_id(sql),
        # ProgSnap2 очікує Success/Error саме в CompileResult.
        "CompileResult": "Error" if rec["status"] == "error" else "Success",
        "CompileMessageType": rec.get("sqlstate") or "",
        "CompileMessageData": (rec.get("error_message") or "").replace("\n", " "),
        "ExecutionResult": "Success" if rec["status"] == "ok" else "",
        "ToolInstances": rec.get("client") or "",
        "X-Duration-ms": rec.get("duration_ms") or "",
        "X-Rows": rec.get("rows_affected") if rec.get("rows_affected") is not None else "",
        "X-SqlKind": rec.get("sql_kind") or "",
    }


def metadata_csv() -> str:
    """DatasetMetadata: без нього набір формально не є ProgSnap2."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Property", "Value"])
    for k, v in [
        ("Version", PROGSNAP_VERSION),
        ("IsEventOrderingConsistent", "true"),
        ("EventOrderScope", "Restricted"),
        ("EventOrderScopeColumns", "SubjectID;SessionID"),
        ("CodeStateRepresentation", "Directory"),
    ]:
        w.writerow([k, v])
    return buf.getvalue()


async def dump(out_dir: str, redact: bool = True, course: str = "DB-LAB") -> dict:
    """Вивантажити когорту в каталог. Повертає підсумок для звіту."""
    from .config import settings
    from .db import WORK_ONLY, fetchall

    master = settings.master_secret
    root = Path(out_dir)
    (root / "CodeStates").mkdir(parents=True, exist_ok=True)
    (root / "LinkTables").mkdir(parents=True, exist_ok=True)

    cohort = await fetchall("SELECT student_id::text FROM obs.research_cohort")
    ids = [r[0] for r in cohort]
    if not ids:
        return {"subjects": 0, "events": 0, "note": "у дослідницькій когорті нікого"}

    rows = await fetchall(f"""
        SELECT q.id, q.student_id::text, q.session_id::text, q.started_at,
               q.duration_ms, q.sql_text, q.sql_fingerprint, q.sql_kind,
               q.status, q.sqlstate, q.error_message, q.rows_affected,
               s.client_name
          FROM obs.queries q
          LEFT JOIN obs.sessions s ON s.id = q.session_id
         WHERE q.student_id = ANY(%s::uuid[]) AND {WORK_ONLY}
         ORDER BY q.student_id, q.started_at, q.id
    """, (ids,))

    from .labs import fingerprint_map

    labs = await fingerprint_map(cohort_only=True)

    main = io.StringIO()
    writer = csv.DictWriter(main, fieldnames=MAIN_COLUMNS, extrasaction="ignore")
    writer.writeheader()

    seen_states: set[str] = set()
    orders: dict[str, int] = {}
    subjects: set[str] = set()
    for (qid, sid, sess, at, ms, text, fp, kind, status,
         sqlstate, err, rows_aff, client) in rows:
        sql = fp if redact else text
        subject = pseudonym(master, sid, "S")
        subjects.add(subject)
        orders[subject] = orders.get(subject, 0) + 1
        writer.writerow(main_row(
            {"id": qid, "started_at": at, "status": status, "sqlstate": sqlstate,
             "error_message": err, "duration_ms": ms, "rows_affected": rows_aff,
             "sql_kind": kind, "client": client, "lab_id": labs.get(fp)},
            subject, orders[subject], pseudonym(master, sess, "N"), course, sql))

        state = code_state_id(sql)
        if state not in seen_states:
            d = root / "CodeStates" / state
            d.mkdir(parents=True, exist_ok=True)
            (d / "Main.sql").write_text(sql, encoding="utf-8")
            seen_states.add(state)

    (root / "MainTable.csv").write_text(main.getvalue(), encoding="utf-8")
    (root / "DatasetMetadata.csv").write_text(metadata_csv(), encoding="utf-8")

    subj = io.StringIO()
    sw = csv.writer(subj)
    sw.writerow(["SubjectID", "X-Consented"])
    for s in sorted(subjects):
        sw.writerow([s, "research"])
    (root / "LinkTables" / "Subject.csv").write_text(subj.getvalue(), encoding="utf-8")

    # README поруч із даними, а не лише в репозиторії: набір даних живе окремим
    # життям, і той, хто його відкриє, читатиме те, що лежить у ньому.
    (root / "README.md").write_text(
        "# Набір даних SQL-процесу (формат, близький до ProgSnap2 v6)\n\n"
        f"Подій: {len(rows)}. Учасників: {len(subjects)}. "
        f"Тексти запитів: {'очищені від літералів' if redact else 'СИРІ'}.\n\n"
        "Учасники — лише ті, хто дав окрему згоду на використання даних у\n"
        "дослідженні. `SubjectID` — HMAC, незворотний без ключа, якого в наборі\n"
        "немає.\n\n"
        "Зіставлення з ProgSnap2 і його межі описані у `front/app/export.py`\n"
        "вихідного репозиторію: SQL компілюється й виконується одним викликом,\n"
        "тому одна подія тут — це один запит, а не пара «компіляція + запуск».\n",
        encoding="utf-8")

    assigned = sum(1 for r in rows if labs.get(r[6]))
    return {"subjects": len(subjects), "events": len(rows),
            "code_states": len(seen_states), "with_assignment": assigned,
            "labs": len(set(labs.values())), "redacted": redact, "dir": str(root)}


async def _main() -> int:
    import sys

    from .db import close_pools, open_pools

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    out = args[0] if args else "/tmp/wireward-dataset"
    redact = "--raw" not in sys.argv

    await open_pools()
    try:
        res = await dump(out, redact=redact)
    finally:
        await close_pools()

    print(json.dumps(res, ensure_ascii=False, indent=2))
    if not res.get("events"):
        print("\nПорожньо. Найімовірніша причина — у когорті ще нікого: "
              "згоду на дослідження дають окремим екраном.", file=sys.stderr)
        return 1
    if not redact:
        print("\n⚠ Тексти запитів СИРІ — назовні такий набір віддавати не можна.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    import asyncio
    import sys
    sys.exit(asyncio.run(_main()))
