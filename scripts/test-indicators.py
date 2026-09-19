#!/usr/bin/env python3
"""Показники процесу (П1.1, П2.2, П2.8) — без бази й без psycopg.

Заморожену формулу треба вміти перевіряти найбільше: якщо вона тихо
зміниться між вереснем і січнем, у статті буде посилання на документ, який
описує вже не те, що рахувалося. Тому кожен показник тут перевіряється на
даних, зроблених так, щоб правильна відповідь була відома наперед.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "front"))
from app.indicators import (BASELINE_DAYS, EQ_SLOW_MIN_SAMPLES,  # noqa: E402
                            PASTE_CHARS, compute, error_quotient,
                            error_quotient_pos, resolve_bounds, slow_share,
                            without_traces)

fails = 0
T0 = datetime(2026, 9, 1, 10, 0, 0)


def check(name, cond):
    global fails
    print(("  ok  " if cond else "  FAIL") + "  " + name)
    if not cond:
        fails += 1


def q(sec, sql, status="ok", sqlstate=None, session="s1", fp=None,
      pos=None, ms=0):
    """Рядок журналу. Відбиток за замовчуванням — сам текст."""
    return (T0 + timedelta(seconds=sec), session, fp or sql, sql,
            status, sqlstate, len(sql), pos, ms)


print("І1 · з першої спроби")
check("усе з першої = 1.0",
      compute([q(0, "select 1"), q(1, "select 2")])["first_try"] == 1.0)
check("помилка першою, успіх другим — той самий відбиток = 0.0",
      compute([q(0, "select x", "error", "42703", fp="f"),
               q(1, "select y", fp="f")])["first_try"] == 0.0)
check("правка пробілу не робить спробу новою",
      compute([q(0, "select  x", "error", "42703", fp="f"),
               q(1, "select x", fp="f")])["fingerprints"] == 1)

print("І2 · вставка")
long_sql = "select " + ", ".join(f"c{i}" for i in range(60))
check("довгий запит рахується довгим", len(long_sql) >= PASTE_CHARS)
check("довгий одразу після попереднього = 1.0",
      compute([q(0, "select 1"), q(1, long_sql)])["paste"] == 1.0)
check("довгий після паузи = 0.0",
      compute([q(0, "select 1"), q(30, long_sql)])["paste"] == 0.0)
check("перший запит сесії не рахується вставкою: паузи ще немає",
      compute([q(0, long_sql)])["paste"] == 0.0)
check("пауза між СЕСІЯМИ не рахується",
      compute([q(0, "select 1", session="a"),
               q(1, long_sql, session="b")])["paste"] == 0.0)
check("коротких запитів немає в знаменнику",
      compute([q(0, "select 1"), q(1, "select 2")])["paste_of"] == 0)

print("І3 · стрибок складності")
base = [q(i, "select a from t1") for i in range(10)]
after = [q(int(timedelta(days=BASELINE_DAYS + 1).total_seconds()),
           "select a,b from t1 join t2 on x join t3 on y where a in "
           "(select z from t4) group by a having count(*) > 1 order by a")]
check("стрибок над власною лінією помічено", compute(base + after)["jump"] == 1.0)
check("рівна робота стрибка не дає",
      compute(base + [q(int(timedelta(days=BASELINE_DAYS + 1).total_seconds()),
                        "select b from t1")])["jump"] == 0.0)

print("І4 · склад помилок")
r = compute([q(0, "selct 1", "error", "42601"),
             q(1, "select x", "error", "42703"),
             q(2, "select 1")])
check("синтаксичних половина", r["syntax_share"] == 0.5)
check("семантичних половина", r["semantic_share"] == 0.5)
check("успіхи в знаменник не входять", r["errors"] == 2)

print("І5 · темп")
check("медіана пауз",
      compute([q(0, "a"), q(10, "b"), q(20, "c")])["tempo"] == 10.0)
check("перерва довша за 10 хв у темп не входить",
      compute([q(0, "a"), q(5, "b"), q(5000, "c")])["tempo"] == 5.0)

print("І7 · Error Quotient (Jadud)")
check("жодної помилки = 0",
      error_quotient([q(0, "a"), q(1, "b")]) == 0.0)
check("успіх між помилками розриває пару",
      error_quotient([q(0, "a", "error", "42601"), q(1, "b"),
                      q(2, "c", "error", "42601")]) == 0.0)
# Дві помилки поспіль з тим самим SQLSTATE: (8+3)/11 = 1.0 на одну пару.
check("дві однакові помилки поспіль = 1.0",
      error_quotient([q(0, "a", "error", "42601"),
                      q(1, "b", "error", "42601")]) == 1.0)
# Різні SQLSTATE: 8/11.
eq = error_quotient([q(0, "a", "error", "42601"), q(1, "b", "error", "42703")])
check("різні помилки поспіль = 8/11", abs(eq - 8 / 11) < 1e-9)
# Пари рахуються в межах сесії; середнє по сесіях.
eq = error_quotient([q(0, "a", "error", "42601", session="s1"),
                     q(1, "b", "error", "42601", session="s1"),
                     q(0, "c", session="s2"), q(1, "d", session="s2")])
check("середнє по сесіях: (1.0 + 0.0) / 2", abs(eq - 0.5) < 1e-9)
check("сесія з одного запиту пар не дає",
      error_quotient([q(0, "a", "error", "42601")]) == 0.0)

# --------------------------------------------------------------------------
# Еталонні вектори Jadud
#
# «EQ» — назва щонайменше двох різних формул: у дисертації Jadud 2005 чотири
# складові з нормуванням на 9, у версії ICER 2006 — дві з нормуванням на 11.
# Ми рахуємо другу, і ці дев'ять векторів доводять, що саме її: вони взяті з
# докстрингу еталонної реалізації `code/jadud.py` (Švábenský та ін., ITiCSE
# 2024, MIT), яка посилається на с. 6 статті ICER 2006.
#
# Перевірка не декоративна. Формула заморожена й піде в статтю; якщо колись
# хтось «підправить» ваги, число тихо перестане бути порівнюваним із
# двадцятирічним корпусом літератури — а виглядатиме так само.
# Розбір — docs/student-performance-metrics/01-error-quotient.md
JADUD_VECTORS = [
    ("A · усе успішно",                     [None, None],  0.0),
    ("B · помилки не підряд, різні типи",   ["X", None, "Y"], 0.0),
    ("C · помилки не підряд, той самий",    ["X", None, "X"], 0.0),
    ("D · дві підряд, різні типи",          ["X", "Y"], 8 / 11),
    ("E · дві підряд, той самий тип",       ["X", "X"], 1.0),
    ("F · три підряд, обидві пари однакові", ["X", "X", "X"], 1.0),
    ("G · три підряд, однакова перша пара", ["X", "X", "Y"], 19 / 22),
    ("H · три підряд, однакова друга пара", ["X", "Y", "Y"], 19 / 22),
    ("I · три підряд, усі різні",           ["X", "Y", "Z"], 8 / 11),
]
for name, states, want in JADUD_VECTORS:
    rows = [q(i, f"q{i}", "error" if st else "ok", st)
            for i, st in enumerate(states)]
    check(f"еталон {name} = {want:.6f}",
          abs(error_quotient(rows) - want) < 1e-9)

print("І7 · побудова пар (поправки Watson)")
# Дві лабораторні впереміш в одній сесії. Без розділення по лабах пара
# «помилка в лабі 1 → успіх у лабі 2» зарахувалася б як виправлення, якого не
# було, і EQ вийшов би НИЖЧИМ, ніж є насправді.
mixed = [q(0, "l1a", "error", "42601", fp="f1"),
         q(1, "l2a", fp="f2"),
         q(2, "l1b", "error", "42601", fp="f3")]
check("без мапи лаб — успіх чужої лаби гасить пару",
      error_quotient(mixed) == 0.0)
check("з мапою лаб — помилки лаби 1 стають парою",
      error_quotient(mixed, {"f1": 1, "f3": 1, "f2": 2}) == 1.0)

# Холостий повтор: та сама форма, той самий SQLSTATE, обидві невдалі.
idle = [q(0, "select x", "error", "42703", fp="f"),
        q(1, "select x", "error", "42703", fp="f"),
        q(2, "select y", "error", "42601", fp="g")]
check("холостий повтор не рахується парою",
      abs(error_quotient(idle) - 8 / 11) < 1e-9)
check("успішний повтор того самого запиту парою лишається",
      error_quotient([q(0, "select x", "error", "42703", fp="f"),
                      q(1, "select x", fp="f")]) == 0.0)

print("І7-поз · позиція помилки (експеримент, шкала /8)")
check("немає жодної позиції — None, а не нуль",
      error_quotient_pos([q(0, "a", "error", "42601"),
                          q(1, "b", "error", "42601")]) is None)
check("той самий SQLSTATE і та сама позиція = 1.0",
      error_quotient_pos([q(0, "a", "error", "42601", pos=12),
                          q(1, "b", "error", "42601", pos=12)]) == 1.0)
eqp = error_quotient_pos([q(0, "a", "error", "42601", pos=12),
                          q(1, "b", "error", "42601", pos=90)])
check("той самий SQLSTATE, інша позиція = 5/8", abs(eqp - 5 / 8) < 1e-9)
eqp = error_quotient_pos([q(0, "a", "error", "42601", pos=12),
                          q(1, "b", "error", "42703", pos=12)])
check("різні SQLSTATE — позиція не додається, 2/8", abs(eqp - 2 / 8) < 1e-9)

print("І8 · час розв'язання відносно однокурсників")
# Розподіл групи: 24 однакові спроби по 10 с і кілька довгих.
group = []
for i in range(24):
    group.append(q(i * 100, f"g{i}", "error", "42601", session=f"s{i}"))
    group.append(q(i * 100 + 10, f"h{i}", session=f"s{i}"))
bounds = resolve_bounds(group)
check("верхній квартиль порахований", bounds.get("42601") is not None)
check("рідкісний SQLSTATE у межі не потрапляє",
      resolve_bounds([q(0, "a", "error", "42703"), q(1, "b")]) == {})
check(f"менше за {EQ_SLOW_MIN_SAMPLES} спостережень — нічого не рахуємо",
      resolve_bounds(group[:10]) == {})
check("без меж І8 не рахується — None, а не нуль",
      slow_share(group, {}) is None)
check("студент, що вклався в норму — 0",
      slow_share([q(0, "a", "error", "42601", session="x"),
                  q(1, "b", session="x")], bounds) == 0.0)
check("студент, що думав удвадцятеро довше — 1.0",
      slow_share([q(0, "a", "error", "42601", session="x"),
                  q(200, "b", session="x")], bounds) == 1.0)
# Час виконання самого запиту з роздумів віднімається.
check("довге ВИКОНАННЯ не робить студента повільним",
      slow_share([q(0, "a", "error", "42601", session="x", ms=195000),
                  q(200, "b", session="x")], bounds) == 0.0)
# Вироджені мітки часу: генератор і навантажувальний тест шлють запити тим
# самим часом, і квартиль виходить нульовим. З таким порогом повільнішим
# виявляється КОЖЕН, і показник тихо стає одиницею для всієї групи.
instant = []
for i in range(24):
    instant.append(q(i * 100, f"z{i}", "error", "42P01", session=f"t{i}"))
    instant.append(q(i * 100, f"w{i}", session=f"t{i}"))
check("нульовий квартиль у межі не потрапляє", resolve_bounds(instant) == {})

check("помилка, для якої немає розподілу групи, не рахується",
      slow_share([q(0, "a", "error", "42P01", session="x"),
                  q(999, "b", session="x")], bounds) is None)

print("«зʼявилося без спроб» — те, чого воно НЕ має ловити")
simple = [q(i * 30, f"select a from t1 where a = {i}") for i in range(12)]
check("рівна робота не дає жодного випадку", without_traces(simple) == [])
check("успіх з першої спроби сам по собі — не сигнал",
      without_traces([q(0, "select a from t1")]) == [])
hard = ("select a,b from t1 join t2 on x join t3 on y where a in "
        "(select z from t4) group by a having count(*) > 1 order by a limit 5")
res = without_traces(simple + [q(400, hard)])
check("стрибок складності одразу — сигнал", len(res) == 1)
check("названо обидві ознаки", res and set(res[0]["signals"]) == {"одразу", "складніше"})
check("текст запиту повернуто для розмови", res and res[0]["sql"].startswith("select a,b"))
res2 = without_traces(simple + [q(400, hard, "error"), q(430, hard)])
check("якщо була невдала спроба до успіху — не сигнал", res2 == [])
long_simple = "select " + ", ".join(f"c{i}" for i in range(60)) + " from t1"
check("сама лише вставка без стрибка складності — не сигнал",
      without_traces(simple + [q(361, long_simple)]) == [])

check("DDL не потрапляє: скрипт схеми студентам видають",
      without_traces(simple + [q(400, "CREATE TABLE dept (deptno numeric(3) not null, "
                                      "dname varchar(14), loc varchar(13), "
                                      "constraint dept_pk primary key (deptno))")]) == [])
check("службовий запит до каталогу не потрапляє в перелік",
      without_traces(simple + [q(400, "select e.oid, d.objid from pg_extension e "
                                     "join pg_depend d on e.oid = d.refobjid "
                                     "where d.deptype = 'e' group by 1,2")]) == [])

print("порожній журнал")
check("нічого не падає", compute([]) == {"queries": 0})
check("без спроб на порожньому — порожньо", without_traces([]) == [])

print("FAIL" if fails else "усе гаразд")
sys.exit(1 if fails else 0)
