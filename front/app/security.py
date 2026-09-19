"""Сесія у підписаній куці + CSRF."""
from __future__ import annotations

from typing import Any

from fastapi import Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import settings

COOKIE = "observer_session"
MAX_AGE = 12 * 3600

_serializer = URLSafeTimedSerializer(settings.session_secret, salt="observer.session")


def read_session(request: Request) -> dict[str, Any]:
    raw = request.cookies.get(COOKIE)
    if not raw:
        return {}
    try:
        return _serializer.loads(raw, max_age=MAX_AGE)
    except (BadSignature, SignatureExpired):
        return {}


def write_session(response: Response, data: dict[str, Any]) -> None:
    response.set_cookie(
        COOKIE,
        _serializer.dumps(data),
        max_age=MAX_AGE,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(COOKIE, path="/")


def csrf_ok(session: dict[str, Any], submitted: str | None) -> bool:
    expected = session.get("csrf")
    return bool(expected and submitted and consteq(expected, submitted))


def consteq(a: str, b: str) -> bool:
    if len(a) != len(b):
        return False
    diff = 0
    for x, y in zip(a.encode(), b.encode()):
        diff |= x ^ y
    return diff == 0
