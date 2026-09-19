"""OIDC-клієнт до Mix.

Ключова особливість — ADR-012: підпис `id_token` МИ НЕ ПЕРЕВІРЯЄМО і ключа Mix
не зберігаємо. Mix підписує HS256, тобто ключ перевірки водночас є ключем
підпису; тримати його в навчальному сервісі означало б дати йому змогу підробити
токен будь-якого користувача LMS. Довіру дає TLS-канал до token endpoint
(OIDC Core §3.1.3.7), особу читаємо з /oauth/userinfo.

Мапа claims Mix (ADR-004), важлива і неочевидна:
    sub    <- scope profile      email <- scope openid
    name   <- scope openid       roles <- scope openid
Тому scopes рівно `openid profile`; `email` і `roles` як scope нічого не дають.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets

import httpx

from .config import settings

log = logging.getLogger("observer.oidc")

_discovery_cache: dict | None = None


async def discover() -> dict:
    """Читає .well-known. Явні *_ENDPOINT у оточенні мають пріоритет —
    щоб можна було підняти застосунок, поки Mix недоступний."""
    global _discovery_cache
    if settings.oidc_authorize and settings.oidc_token and settings.oidc_userinfo:
        return {
            "authorization_endpoint": settings.oidc_authorize,
            "token_endpoint": settings.oidc_token,
            "userinfo_endpoint": settings.oidc_userinfo,
            "issuer": settings.oidc_issuer,
        }
    if _discovery_cache is None:
        url = settings.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            _discovery_cache = resp.json()
    return _discovery_cache


def new_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    return verifier, challenge


async def authorization_url(state: str, nonce: str, challenge: str) -> str:
    """`nonce` надсилаємо, бо цього вимагає специфікація, але покладатися на нього
    не можна: за ADR-012 ми не споживаємо `id_token` взагалі, а перевіряти nonce
    у непідписаному тілі — самообман. Від підміни й повтору тут захищають
    `state` (привʼязаний до сесії) і PKCE (привʼязаний до цього ж браузера)."""
    meta = await discover()
    params = {
        "response_type": "code",
        "client_id": settings.oidc_client_id,
        "redirect_uri": settings.oidc_redirect_uri,
        "scope": settings.oidc_scopes,
        "state": state,
        "nonce": nonce,
    }
    if settings.oidc_pkce:
        params["code_challenge"] = challenge
        params["code_challenge_method"] = "S256"
    return meta["authorization_endpoint"] + "?" + str(httpx.QueryParams(params))


async def exchange_code(code: str, verifier: str, client_secret: str) -> dict:
    meta = await discover()
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.oidc_redirect_uri,
        "client_id": settings.oidc_client_id,
    }
    if client_secret:
        data["client_secret"] = client_secret
    if settings.oidc_pkce:
        data["code_verifier"] = verifier
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(meta["token_endpoint"], data=data)
        if resp.status_code >= 400:
            safe = {k: v for k, v in data.items() if k not in ("client_secret", "code")}
            safe["code_len"] = len(data["code"])
            safe["has_secret"] = bool(client_secret)
            log.error("token endpoint %s: %s | sent: %s", resp.status_code, resp.text[:500], safe)
        resp.raise_for_status()
        return resp.json()


async def userinfo(access_token: str) -> dict:
    meta = await discover()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(meta["userinfo_endpoint"],
                                headers={"Authorization": f"Bearer {access_token}"})
        resp.raise_for_status()
        return resp.json()


def normalize_identity(claims: dict) -> dict:
    """Мапінг claims Mix у поля obs.students.

    `display_name` береться з claim `name` — НЕ з `full_name`: рішення від
    2026-09-02, claim `name` достатній.

    `groups` (scope `groups`, дозволений для нашого client_id 05.09.2026) —
    список обʼєктів `{name, code}`, не рядок. Мапимо на `name` напряму: за
    підтвердженням викладача назви стабільні в межах навчального року, а
    кілька груп одночасно — норма (вибіркові дисципліни на пів року йдуть
    окремими групами поруч з основною). `code` не зберігаємо — не потрібен,
    поки group_name лишається стабільним ключем.

    `lms_groups` тут `None`, якщо claim відсутній узагалі (scope не
    запитаний, чи дев-вхід) — це відрізняється від порожнього списку: `None`
    лишає типову групу (рік реєстрації) від students.py, порожній список
    закрив би всі членства студента.
    """
    roles = claims.get("roles") or []
    if isinstance(roles, str):
        roles = [r.strip() for r in roles.split(",") if r.strip()]
    groups = claims.get("groups")
    if groups is not None:
        groups = [g["name"] if isinstance(g, dict) else str(g) for g in groups]
    return {
        "oidc_sub": str(claims["sub"]),
        "email": claims.get("email"),
        "display_name": claims.get("name"),
        "lms_roles": list(roles),
        "lms_groups": groups,
    }
