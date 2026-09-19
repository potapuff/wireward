#!/usr/bin/env python3
"""Перевірка міри схожості v2 (`sqlshape2`) без бази — docs/research/05.

v2 навмисно жорсткіша за v1 до переформулювань: розклад на компоненти Aligon
(`predicates`, `joins`, `projection`, `group_by`) означає, що «підзапит замість
зʼєднання» вже НЕ те саме, що точний збіг. Тому перевіряється не фіксований
поріг 0.5, а **порядок**: та сама задача — вище за іншу задачу тих самих
таблиць — вище за іншу схему — вище за DDL. Плюс канонізаційні кроки.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "front"))
from app.sqlshape2 import shape, similarity  # noqa: E402

bad = 0


def check(name, cond):
    global bad
    print(f"  {'✓' if cond else '✗'} {name}")
    bad |= (not cond)


def sim(a, b):
    return similarity(shape(a), shape(b))


REF = ("select c.name, count(*) from customers c "
       "join orders o on o.customer_id = c.id where o.total > 100 group by c.name")

same_solution = "SELECT cu.name, COUNT(o.id) FROM orders o JOIN customers cu ON cu.id = o.customer_id WHERE o.total > 100 GROUP BY 1"
same_lab      = "select o.id, o.total from orders o join customers c on c.id = o.customer_id where o.total > 1000"
typo          = "SELCT c.name, count(*) from customers c join orders o on o.customer_id = c.id where o.total > 100 group by c.name"
reformulated  = "select c.name from customers c where c.id in (select o.customer_id from orders o where o.total > 100)"
other_tables  = "select p.title from products p join categories k on k.id = p.category_id"
ddl           = "create table students(id serial primary key, name text)"

s_same = sim(REF, same_solution)
s_lab  = sim(REF, same_lab)
s_typo = sim(REF, typo)
s_ref  = sim(REF, reformulated)
s_oth  = sim(REF, other_tables)
s_ddl  = sim(REF, ddl)
for n, v in [("той самий розвʼязок, інші аліаси/порядок", s_same),
             ("інша задача, ті самі таблиці", s_lab),
             ("опечатка — та сама робота", s_typo),
             ("підзапит замість зʼєднання", s_ref),
             ("інші таблиці", s_oth), ("DDL", s_ddl)]:
    print(f"  {v:5.2f}  {n}")

check("той самий розвʼязок ≈ дуже високо (≥0.8)", s_same >= 0.8)
check("опечатка не карається (≥0.8)", s_typo >= 0.8)
check("та сама задача > інша задача тих самих таблиць", s_same > s_lab)
check("переформулювання ≥ інша схема", s_ref >= s_oth)
check("будь-що реальне > DDL", min(s_same, s_lab, s_ref) > s_ddl)
check("порядок: свій розвʼязок > переформулювання > інша схема",
      s_same > s_ref and s_ref >= s_oth)

# --- канонізація ------------------------------------------------------------
sh = shape("SELECT e1.ename FROM Emp e1 JOIN Emp e2 ON e1.mgr = e2.empno WHERE e1.sal > 1000")
check("резолвінг аліасів: 'emp' одна таблиця", sh["tables"] == {"emp"})
check("зʼєднання по резолвлених іменах", "emp.empno|=|emp.mgr" in sh["joins"] or "emp.mgr|=|emp.empno" in sh["joins"])

sh = shape("SELECT x FROM t WHERE a BETWEEN 1 AND 9 AND b IN (1,2,3)")
check("BETWEEN → предикат <=", any(p.startswith("a|<=") for p in sh["predicates"]))
check("IN(список) → один предикат =/in", any(p.startswith("b|in|") for p in sh["predicates"]))

sh = shape("SELECT x FROM r,s,t WHERE r.a = s.a AND s.a = t.a")
check("транзитивне замикання рівностей: r.a=t.a виведено",
      "r.a|=|t.a" in sh["joins"])

a = shape("select name, price from products where price > 100")
b = shape("select price, name from products where 100 < price")
check("комутативність оператора: a>100 = 100<a", similarity(a, b) >= 0.95)

print("\nміра схожості v2: усі перевірки пройдено" if not bad else "\nє розбіжності")
sys.exit(bad)
