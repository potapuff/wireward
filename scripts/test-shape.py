#!/usr/bin/env python3
"""Перевірка міри схожості запитів (З4) без бази й без psycopg.

Міра має робити рівно одне: відрізняти роботу над ЦІЄЮ лабораторною від усього
іншого, при тому що студентські розвʼязки варіативні. Тому перевіряються не
абсолютні числа, а ПОРЯДОК: будь-яка пара «та сама лаба» має бути вищою за
будь-яку пару «інша лаба», і межа між ними — там, де стоїть типовий поріг 0.5.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "front"))
from app.sqlshape import constructs, shape, similarity, tables  # noqa: E402

def sim(a, b):
    return similarity(shape(a), shape(b))

REF = "select c.name, count(*) from customers c join orders o on o.customer_id = c.id group by c.name"

same = [
    ("той самий розвʼязок, інші псевдоніми й порядок",
     "SELECT cu.name, COUNT(o.id) FROM orders o JOIN customers cu ON cu.id = o.customer_id GROUP BY 1"),
    ("інша задача тієї ж лаби, ті самі таблиці",
     "select o.id, o.total from orders o join customers c on c.id = o.customer_id where o.total > 1000"),
    ("опечатка — це та сама робота",
     "SELCT c.name, count(*) from customers c join orders o on o.customer_id = c.id group by c.name"),
    ("підзапит замість зʼєднання",
     "select name from customers where id in (select customer_id from orders)"),
]

other = [
    ("інші таблиці", "select p.title from products p join categories k on k.id = p.category_id"),
    ("DDL", "create table students(id serial primary key, name text)"),
    ("службовий запит", "SHOW TRANSACTION ISOLATION LEVEL"),
    ("вставка", "insert into orders(customer_id, total) values (3, 250)"),
]

bad = 0
lo_same = min(sim(REF, q) for _t, q in same)
hi_other = max(sim(REF, q) for _t, q in other)

for t, q in same:
    print(f"  {sim(REF, q):5.2f}  та сама лаба · {t}")
for t, q in other:
    print(f"  {sim(REF, q):5.2f}  інше       · {t}")

if lo_same <= hi_other:
    print(f"\nПОРЯДОК ПОРУШЕНО: найгірший «свій» {lo_same:.2f} не вищий за "
          f"найкращий «чужий» {hi_other:.2f}")
    bad = 1
elif not (hi_other < 0.5 <= lo_same):
    print(f"\nТиповий поріг 0.5 більше не між ними: свої від {lo_same:.2f}, "
          f"чужі до {hi_other:.2f}. Поріг за замовчуванням треба переглянути.")
    bad = 1

# Розбір форми: те, на чому все тримається.
if tables("select * from public.orders o join customers c on c.id = o.customer_id") \
        != {"orders", "customers"}:
    print("джерела розібрано неправильно"); bad = 1
if tables("select * from (select 1) x") != set():
    print("підзапит не є джерелом з іменем"); bad = 1
if "group_by" not in constructs("select a, count(*) from t group by a"):
    print("групування не розпізнано"); bad = 1
if constructs("select 'group by' as note"):
    print("конструкція знайдена в літералі"); bad = 1

print("\nміра схожості: усі перевірки пройдено" if not bad else "\nє розбіжності")
sys.exit(bad)
