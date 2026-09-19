"""Два пули з різними правами.

`app_pool` — обмежений: читає й пише метадані, журнал лише читає.
`prov_pool` — привілейований, autocommit (CREATE/DROP DATABASE не виконуються
              всередині транзакції). Ним ходимо тільки в provisioning.py.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from psycopg_pool import AsyncConnectionPool

from .config import settings

app_pool = AsyncConnectionPool(settings.app_dsn, min_size=1, max_size=8, open=False)
prov_pool = AsyncConnectionPool(settings.provisioner_dsn, min_size=0, max_size=2, open=False)


async def open_pools() -> None:
    await app_pool.open(wait=True, timeout=30)
    await prov_pool.open(wait=True, timeout=30)


async def close_pools() -> None:
    await app_pool.close()
    await prov_pool.close()


@asynccontextmanager
async def provisioner():
    """Зʼєднання для DDL. autocommit обовʼязковий."""
    async with prov_pool.connection() as conn:
        await conn.set_autocommit(True)
        yield conn


async def fetchone(sql: str, params: tuple = ()) -> tuple | None:
    async with app_pool.connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchone()


async def fetchall(sql: str, params: tuple = ()) -> list[tuple]:
    async with app_pool.connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall()


async def execute(sql: str, params: tuple = ()) -> None:
    async with app_pool.connection() as conn:
        await conn.execute(sql, params)


# Службовий трафік клієнта (звернення до каталогу і розмова із сервером)
# зберігається в журналі, але за замовчуванням не показується і не потрапляє
# в жодні підрахунки «роботи студента» — ADR-018. Один вираз на весь застосунок,
# щоб екрани не розійшлися між собою в тому, що вважати роботою.
#
# Помилка лишається видимою завжди: заборонений SET чи невдалий службовий запит
# — це сигнал, а не шум, і ховати його було б гірше, ніж показати зайвий рядок.
WORK_ONLY = "NOT (is_catalog OR is_service)"
VISIBLE = "(status = 'error' OR " + WORK_ONLY + ")"
