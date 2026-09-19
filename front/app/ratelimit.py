"""Обмеження частоти на боці веба (П0.8).

Мова тут не про підбір секрету. Токен — 256 біт, сесійна кука підписана; ані
те, ані інше перебором не береться. Обмежується ЦІНА операції:

* `/cabinet/token` — кожне натискання рахує argon2 (16 МБ), пише подію і
  ЛАМАЄ вже налаштоване підключення студента в DBeaver. Затиснута кнопка
  шкодить навіть без злого наміру;
* вхід — кожен успішний виклик тягне `ensure_database`: слот, роль, база,
  ліміти.

Ключ — завжди особа (`student_id`, `oidc_sub`), ніколи не адреса: студенти
сидять за університетським NAT, і ліміт на IP замкнув би групу цілком.

Стан — у памʼяті процесу. Front один, Redis заводити нема заради чого; ціна
такого рішення — лічильники обнуляються при перезапуску, і це прийнятно.
"""
from __future__ import annotations

import time
from collections import deque

# Скільки ключів тримати, поки не прибираємо протухлі. Без прибирання мапа
# росла б на кожен новий oidc_sub, тобто необмежено.
_SWEEP_AT = 4096


class RateLimit:
    """Ковзне вікно: не більше `limit` подій на ключ за `window` секунд."""

    def __init__(self, limit: int, window: float, name: str = "") -> None:
        self.limit = max(1, limit)
        self.window = window
        self.name = name
        self._hits: dict[str, deque[float]] = {}
        self._reported: dict[str, float] = {}

    def check(self, key: str) -> float:
        """0.0 — можна. Інакше — скільки секунд чекати."""
        now = time.monotonic()
        q = self._hits.get(key)
        if q is None:
            return 0.0
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) < self.limit:
            return 0.0
        return round(self.window - (now - q[0]), 1)

    def hit(self, key: str) -> float:
        """Зараховує спробу. Повертає те саме, що й check: 0.0 або паузу.

        Спроба зараховується ЛИШЕ коли вона дозволена — інакше той, хто
        довбиться в закриті двері, продовжував би собі паузу нескінченно.
        """
        wait = self.check(key)
        if wait:
            return wait
        self._hits.setdefault(key, deque()).append(time.monotonic())
        if len(self._hits) > _SWEEP_AT:
            self._sweep()
        return 0.0

    def report_once(self, key: str) -> bool:
        """Чи писати подію про це спрацювання.

        True не частіше разу на вікно: подія на КОЖНУ відмову залила б
        db_events рівно тим потоком, від якого ми й захищаємося.
        """
        now = time.monotonic()
        last = self._reported.get(key)
        if last is not None and now - last <= self.window:
            return False
        self._reported[key] = now
        return True

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)
        self._reported.pop(key, None)

    def _sweep(self) -> None:
        now = time.monotonic()
        for k, q in list(self._hits.items()):
            if not q or now - q[-1] > self.window:
                del self._hits[k]
                self._reported.pop(k, None)


# Ротація пароля. Ліміт свідомо не «раз на N хвилин»: студент має право
# перевипустити токен двічі поспіль, якщо перший раз не встиг скопіювати.
token_rotation = RateLimit(5, 10 * 60, "token_rotate")

# Вхід. Поріг високий: це не захист від підбору (підбирати нічого — особу
# засвідчує Mix), а стеля на повторний провізіонінг.
login = RateLimit(20, 60, "login")

# VACUUM з адмінки (З15б). Роль obs_maintenance має CONNECTION LIMIT 2: третє
# одночасне натискання впало б сирою помилкою "too many connections", а не
# зрозумілим повідомленням. Ключ — admin_id, не student_id: той самий адмін
# може тиснути кнопку на різних студентах поспіль.
vacuum = RateLimit(1, 60, "vacuum")
