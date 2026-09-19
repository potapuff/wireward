"""Три різні секрети навколо одного студента. Плутати їх не можна.

1. Пароль ролі PostgreSQL — HMAC(master_secret, student_id).
   Обчислюється щоразу, НЕ зберігається ніде. Студент його не знає й не бачить:
   він потрібен лише pgproxy, щоб відкрити бекенд-зʼєднання.
2. Токен для psql/DBeaver — те, що студент вставляє як пароль. Випадковий,
   показується один раз, у базі лежить лише argon2id-хеш.
3. Сесійна кука веба — до СУБД стосунку не має, живе в security.py.

Наслідок такого поділу: навіть якщо порт 5432 колись випадково опублікують,
студент не зможе підключитися напряму й обійти журнал.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError

from .config import settings

# Параметри навмисно скромніші за типові для argon2-cffi (64 МБ, t=3, p=4).
#
# Причина не в економії, а в моделі загроз: токен — це 32 випадкові байти,
# тобто 256 біт ентропії. Перебрати його неможливо за будь-якої швидкості
# хешування, тож важка KDF тут не додає стійкості — вона лише множить памʼять
# проксі на кількість одночасних входів. При 64 МБ наплив на початку заняття
# вбивав проксі (знайдено навантажувальним тестом).
#
# 16 МБ лишають захист на випадок витоку бази і не роблять вхід вузьким місцем.
_hasher = PasswordHasher(time_cost=2, memory_cost=16 * 1024, parallelism=1)


def role_password(student_id: UUID) -> str:
    """Детермінований пароль ролі. Ротація master_secret = перевидача всіх."""
    mac = hmac.new(settings.master_secret, str(student_id).encode(), hashlib.sha256)
    return base64.urlsafe_b64encode(mac.digest()).decode().rstrip("=")


def new_db_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return _hasher.hash(token)


def verify_token(token: str, stored_hash: str) -> bool:
    try:
        return _hasher.verify(stored_hash, token)
    except (VerifyMismatchError, VerificationError):
        return False


def role_name(student_id: UUID) -> str:
    """Непрозоре імʼя. НЕ з прізвища: pg_database — спільний каталог, і студент
    бачить перелік усіх баз кластера зі своєї сесії."""
    return "u_" + str(student_id).replace("-", "")[:12]


def db_name(student_id: UUID) -> str:
    return "db_" + str(student_id).replace("-", "")[:12]
