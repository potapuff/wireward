#!/usr/bin/env python3
"""Перевірка часового контексту (`app.temporal_context.smooth_sequence`) на
короткому штучному прикладі — docs/article-p4-research/07-10.

Не повторює синтетичний бенчмарк `scripts/synth-lab-timeline.py` (той міряє
F1 на реальних текстах запитів з навмисним шумом, тут — механіка Viterbi
сама по собі, на іграшкових score-векторах, як тест логіки, а не якості
міри). Дві властивості, яких вимагає завдання:

(а) сталий стан не міняється дрібним шумом одного викиду;
(б) явна зміна лаби (кілька поспіль інших спостережень) таки підхоплюється.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "front"))
from app.temporal_context import BOT, smooth_sequence  # noqa: E402

bad = 0


def check(name, cond):
    global bad
    print(f"  {'✓' if cond else '✗'} {name}")
    bad |= (not cond)


def vec(**kw):
    """Score-вектор по лабах A/B/C, решта — 0."""
    base = {"A": 0.0, "B": 0.0, "C": 0.0}
    base.update(kw)
    return base


LABS = ["A", "B", "C"]

# --- (а) дрібний одиничний викид посеред стабільної роботи над A -----------
seq_a = (
    [vec(A=0.9, B=0.1) for _ in range(4)]
    + [vec(A=0.2, B=0.75)]        # один "чужий" запит
    + [vec(A=0.9, B=0.1) for _ in range(4)]
)
out_a = smooth_sequence(seq_a, LABS, p_stay=0.85, t_lo=0.3)
check("довжина виходу = довжина входу", len(out_a) == len(seq_a))
check("одиничний викид не ламає стабільний стан A",
      out_a == ["A"] * len(seq_a))

# --- baseline без згладжування помилився б на цьому самому прикладі --------
def naive_argmax(v):
    return max(v, key=v.get)


naive_a = [naive_argmax(v) for v in seq_a]
check("сам приклад справді підступний: без контексту викид переміг би",
      naive_a[4] == "B" and naive_a != out_a)

# --- (б) явна й стійка зміна лаби підхоплюється -----------------------------
seq_b = (
    [vec(A=0.9, B=0.1) for _ in range(5)]
    + [vec(A=0.1, B=0.9) for _ in range(5)]   # справжній перехід на B
)
out_b = smooth_sequence(seq_b, LABS, p_stay=0.85, t_lo=0.3)
check("перша половина лишається A", out_b[:5] == ["A"] * 5)
check("друга половина розпізнана як B", out_b[5:] == ["B"] * 5)

# --- повернення до попередньої лаби (07: студент повертається) -------------
seq_c = (
    [vec(A=0.9) for _ in range(4)]
    + [vec(B=0.9) for _ in range(4)]
    + [vec(A=0.9) for _ in range(4)]   # повернення до A, не 1→5→2 навмання
)
out_c = smooth_sequence(seq_c, LABS, p_stay=0.85, t_lo=0.3)
check("повернення до раніше пройденої лаби розпізнається",
      out_c == ["A"] * 4 + ["B"] * 4 + ["A"] * 4)

# --- ⊥ (жодна лаба не підходить) --------------------------------------------
seq_d = [vec(A=0.05, B=0.05, C=0.05) for _ in range(3)]
out_d = smooth_sequence(seq_d, LABS, p_stay=0.85, t_lo=0.3)
check("послідовність без сигналу жодної лаби йде в ⊥", all(s is BOT for s in out_d))

# --- порожній вхід і валідація параметра ------------------------------------
check("порожній вхід → порожній вихід", smooth_sequence([], LABS) == [])
try:
    smooth_sequence(seq_a, LABS, p_stay=1.0)
    check("p_stay=1.0 відхиляється (ValueError)", False)
except ValueError:
    check("p_stay=1.0 відхиляється (ValueError)", True)

# ------------------------------------------------------------------------
# Інтеграція: `app.labs.temporal_context()` — допоміжний шар над стрічкою
# запитів студента (`admin/student.html`). Без кластера, як `test-position`:
# підміняємо `app.db` заглушкою, що знає лише ці два запити.
import asyncio  # noqa: E402
import types  # noqa: E402

_LABS = [(1, "Лаб 3 · зʼєднання і агрегати", 0.5),
         (2, "Лаб 4 · підзапити", 0.5),
         # Дві лабораторні різних груп з майже нерозрізненими зразками —
         # саме той "сусідній" випадок з З7/З12: без фільтра за групою
         # обидві однаково хороші кандидати, і виграє та, що йде першою.
         (4, "Лаб Y (IT-22)", 0.5),
         (3, "Лаб X (IT-21)", 0.5)]
_SAMPLES = {
    1: ["select o.id, c.name from orders o join customers c on c.id = o.customer_id"],
    2: ["select c.name from customers c where c.id in (select o.customer_id from orders o)"],
    3: ["select s.name, s.gpa from students s where s.gpa > 3.5"],
    4: ["select s.name, s.gpa from students s where s.gpa > 3.0"],
}
# Групи лаб для перевірки З12 (docs/07-roadmap.md). Лаби 1/2 без груп —
# доступні всім (стара поведінка); 3/4 — призначені різним групам.
_LAB_GROUPS: dict[int, list[str]] = {3: ["IT-21"], 4: ["IT-22"]}
_STUDENT_GROUPS: dict[str, list[str]] = {"s-it21": ["IT-21"], "s-it22": ["IT-22"]}


async def _fake_fetchall(sql: str, params: tuple = ()):
    if "FROM obs.labs l" in sql:
        return [(lid, title, thr) for lid, title, thr in _LABS]
    if "FROM obs.lab_expectations WHERE lab_id" in sql:
        (lab_id,) = params
        return [(s,) for s in _SAMPLES[lab_id]]
    if "FROM obs.lab_groups" in sql:
        (lab_ids,) = params
        return [(lid, gs) for lid, gs in _LAB_GROUPS.items() if lid in lab_ids and gs]
    raise AssertionError(f"неочікуваний запит у заглушці: {sql!r}")


async def _fake_fetchone(sql: str, params: tuple = ()):
    if "FROM obs.student_current_groups" in sql:
        (student_id,) = params
        gs = _STUDENT_GROUPS.get(student_id)
        return (gs,) if gs else None
    raise AssertionError(f"неочікуваний запит у заглушці: {sql!r}")


_db = types.ModuleType("app.db")
_db.WORK_ONLY = "true"
_db.fetchall = _fake_fetchall
_db.fetchone = _fake_fetchone
_db.execute = None
sys.modules["app.db"] = _db

from app.labs import temporal_context  # noqa: E402


def q(id_, at, sql):
    return {"id": id_, "started_at": at, "sql_text": sql}


JOIN_SQL = "select o.id, c.name from orders o join customers c on c.id = o.customer_id"
SUB_SQL = "select c.name from customers c where c.id in (select o.customer_id from orders o)"
NOISE_SQL = "select 1"

rows_int = (
    [q(i, i, JOIN_SQL) for i in range(5)]
    + [q(5, 5, NOISE_SQL)]                 # один випадковий чужий запит
    + [q(i, i, JOIN_SQL) for i in range(6, 11)]
)
out = asyncio.run(temporal_context(rows_int))
check("довжина відповіді = довжина стрічки", len(out) == len(rows_int))
check("одиничний шум не зриває приписку до Лаб 3 (контекст)",
      all(out[i] == "Лаб 3 · зʼєднання і агрегати" for i in range(11)))

rows_switch = (
    [q(i, i, JOIN_SQL) for i in range(5)]
    + [q(i, i, SUB_SQL) for i in range(5, 10)]
)
out2 = asyncio.run(temporal_context(rows_switch))
check("перехід на іншу лабу підхоплюється контекстом",
      all(out2[i] == "Лаб 3 · зʼєднання і агрегати" for i in range(5))
      and all(out2[i] == "Лаб 4 · підзапити" for i in range(5, 10)))

check("порожня стрічка → порожній результат", asyncio.run(temporal_context([])) == {})

# ------------------------------------------------------------------------
# З12 (docs/07-roadmap.md): кандидати мають звужуватись до груп студента.
# Лаб X (IT-21) і Лаб Y (IT-22) — навмисно "сусідні" зразки (той самий
# запит, різна константа в предикаті — вона не входить у форму), тож без
# фільтра за групою вони однаково хороші кандидати і виграє порядок
# ітерації, а не група студента.
AMBIGUOUS_SQL = "select s.name, s.gpa from students s where s.gpa > 3.5"
rows_amb = [q(i, i, AMBIGUOUS_SQL) for i in range(6)]

out_no_filter = asyncio.run(temporal_context(rows_amb))
out_it21 = asyncio.run(temporal_context(rows_amb, "s-it21"))
out_it22 = asyncio.run(temporal_context(rows_amb, "s-it22"))

check("без фільтра за групою плутанина справді можлива: зразки дають той самий, "
      "не «свій», результат для обох студентів",
      all(v == out_no_filter[0] for v in out_no_filter.values()))
check("з фільтром студент IT-21 матчиться на СВОЮ лабу (Лаб X)",
      all(v == "Лаб X (IT-21)" for v in out_it21.values()))
check("з фільтром студент IT-22 матчиться на СВОЮ лабу (Лаб Y)",
      all(v == "Лаб Y (IT-22)" for v in out_it22.values()))
check("фільтровані відповіді різняться між групами",
      set(out_it21.values()) != set(out_it22.values()))

# Едж-кейси (консервативно — не звужувати): студент без жодної поточної
# групи, і лаба взагалі без призначених груп (лаби 1/2 з основного сценарію).
out_no_group = asyncio.run(temporal_context(
    [q(i, i, JOIN_SQL) for i in range(5)], "s-nogroup"))
check("студент без поточної групи — фільтр не звужує (стара поведінка)",
      all(v == "Лаб 3 · зʼєднання і агрегати" for v in out_no_group.values()))

out_it21_universal = asyncio.run(temporal_context(
    [q(i, i, JOIN_SQL) for i in range(5)], "s-it21"))
check("лаба без призначених груп лишається кандидатом і для студента з групою",
      all(v == "Лаб 3 · зʼєднання і агрегати" for v in out_it21_universal.values()))

print("\nчасовий контекст: усі перевірки пройдено" if not bad else "\nє розбіжності")
sys.exit(bad)
