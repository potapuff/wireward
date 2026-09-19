# Приклад: вибірка з однієї таблиці
групи: 2026
поріг: 0.5
заборонено: join, subquery
нотатка: приклад формату; справжні лабораторні кладіть поруч окремими файлами

## Товари, дорожчі за 100
select name, price from products where price > 100;

## Найдорожчий товар
select name, price from products order by price desc limit 1;

## Скільки товарів у кожній категорії
select category, count(*) from products group by category;

# Приклад: зʼєднання двох таблиць
групи: 2026
поріг: 0.5

## Клієнти та їхні замовлення
select c.name, o.id, o.total
  from customers c
  join orders o on o.customer_id = c.id;

## Клієнти без жодного замовлення
select c.name from customers c
  left join orders o on o.customer_id = c.id
 where o.id is null;
