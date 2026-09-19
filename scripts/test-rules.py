#!/usr/bin/env python3
"""Перевірка правил З5 без бази й без psycopg.

Правила адресовані викладачеві й нічого не блокують, тому хибнопозитивне
спрацювання дешеве. Дорогим лишається протилежне — пропустити помилку, яку
СУБД не помітила, — тому в переліку `catches` кожен випадок узятий із того, що
студенти справді пишуть.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "front"))
from app.rules import check  # noqa: E402

catches = [
    ("eq_null", "select * from t where name = NULL", None),
    ("eq_null", "select * from t where name <> null", None),
    # присвоєння в SET не помилка, але порівняння після нього — помилка
    ("eq_null", "update t set note = null where name = null", None),
    ("eq_null", "select * from a join b on a.x = null", None),
    # підзапит-значення з власним WHERE — порівняння всередині ловиться,
    # незалежно від того, що весь вираз стоїть у списку значень VALUES
    ("eq_null",
     "insert into t (a, b) values (1, (select x from s where x = null))",
     None),
    ("not_in_null", "select * from s where id not in (1, 2, NULL)", None),
    ("not_in_null", "select * from s where id NOT IN (null)", None),
    ("not_in_empty", "select * from a where id not in (select b_id from b)", 0),
    ("having_no_group", "select count(*) from t having count(*) > 5", None),
    ("cross_join", "select * from a, b", None),
    # декартів добуток в одній гілці об'єднання ловиться так само
    ("cross_join", "select x from t where x > 1 union select a.x from a, b", None),
    ("count_column", "select count(grade) from marks", None),
    # ідентифікатор у лапках зберігає матчабельність, а не зникає з тексту
    ("count_column", 'select count("оцінка") from marks', None),
]

clean = [
    "select * from t where name is null",
    "select * from t where note = 'x = NULL'",          # літерал, не порівняння
    "select * from t -- тут було = NULL\n where a = 1",  # коментар
    "select * from s where id not in (1, 2, 3)",
    "select * from a where id not in (select b_id from b)",  # 0 рядків не вказано
    "select count(*) from t group by g having count(*) > 5",
    "select * from a, b where a.id = b.a_id",
    "select count(*) from marks",
    "select count(distinct student_id) from marks",
    # присвоєння NULL — єдиний спосіб це зробити, не помилка
    "update emp set comm = null",
    "update emp set comm = null where comm is not null",
    "insert into t (a, b) values (null, 5)",
    "insert into tbl1 values (null, a = null, b = null)",
    "update emp set bonus = case when dept = 1 then 100 else 0 end, comm = null",
    "update t set a = 1 end, b = null",  # обірваний CASE (артефакт витягу)
    # WHERE усередині підзапиту в SET не протікає на наступне присвоєння
    "update t set comm = null, dept = (select id from d where name = 'x')",
    # захист від пастки NOT IN — протилежне до помилки
    "select d from dept where d not in (select d from emp where d is not null)",
    "select d from dept where d not in (select d from emp)",
    # кома в списку вибірки сусідньої гілки — не з'єднання
    "select a, b from x intersect select a, b from y",
    "select a, b from x except select a, b from y",
    # при зовнішньому з'єднанні count(колонка) — потрібна ідіома
    "select c.id, count(o.id) from c left join o on o.c_id = c.id group by c.id",
    # ключове слово в лапках — легальний псевдонім, не клауза FROM
    'select city1 as "from", city2 as "to" from routes',
]

bad = 0
for code, sql, rows in catches:
    got = check(sql, rows)
    if code not in got:
        print(f"НЕ ЗНАЙДЕНО {code}: {sql!r} -> {got}")
        bad = 1

for sql in clean:
    got = check(sql, None)
    if got:
        print(f"ХИБНЕ СПРАЦЮВАННЯ {got}: {sql!r}")
        bad = 1

# Рівень «текст + результат» не має спрацьовувати, поки результату немає.
if check("select * from a where id not in (select b_id from b)", 5):
    print("not_in_empty спрацював при непорожньому результаті")
    bad = 1

print("правила З5: усі перевірки пройдено" if not bad else "є розбіжності")
sys.exit(bad)
