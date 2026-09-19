"""Конфігурація з оточення. Секрети читаються з файлів (docker secrets),
а не зі змінних: змінні видно в `docker inspect`."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _secret(name: str) -> str:
    path = Path("/run/secrets") / name
    if not path.is_file():
        raise RuntimeError(f"немає секрету /run/secrets/{name}")
    return path.read_text().strip()


def optional_secret(name: str) -> str:
    """Для секретів, яких може не бути. Зокрема oidc_client_secret — не потрібен
    лише коли OIDC_PKCE=1 (публічний клієнт із PKCE); інакше обмін коду
    лишається без жодного підтвердження клієнта, і config.__post_init__
    це зупиняє.

    `is_file()`, а не `exists()`: коли джерела секрету немає на хості, Docker
    Desktop (WSL2) мовчки монтує на його місце порожню директорію — `exists()`
    це не бачить і падає незрозумілим `IsADirectoryError` замість чіткої
    відмови."""
    path = Path("/run/secrets") / name
    return path.read_text().strip() if path.is_file() else ""


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _bool(name: str, default: bool = False) -> bool:
    return _env(name, "1" if default else "0").lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # --- база ---------------------------------------------------------------
    pg_host: str = field(default_factory=lambda: _env("PGHOST", "postgres"))
    pg_port: int = field(default_factory=lambda: int(_env("PGPORT", "5432")))
    app_dsn: str = ""
    provisioner_dsn: str = ""
    _maintenance_base_dsn: str = ""

    # --- OIDC ---------------------------------------------------------------
    oidc_issuer: str = field(default_factory=lambda: _env("OIDC_ISSUER"))
    oidc_client_id: str = field(default_factory=lambda: _env("OIDC_CLIENT_ID"))
    oidc_scopes: str = field(default_factory=lambda: _env("OIDC_SCOPES", "openid profile"))
    oidc_redirect_uri: str = field(default_factory=lambda: _env("OIDC_REDIRECT_URI"))
    oidc_userinfo: str = field(default_factory=lambda: _env("OIDC_USERINFO_ENDPOINT"))
    oidc_authorize: str = field(default_factory=lambda: _env("OIDC_AUTHORIZE_ENDPOINT"))
    oidc_token: str = field(default_factory=lambda: _env("OIDC_TOKEN_ENDPOINT"))
    # Mix (Doorkeeper) не зберігає code_challenge для нашого конфіденційного
    # клієнта: presence code_verifier на /oauth/token дає invalid_request, а
    # не PKCE-невідповідність. Вимкнено, доки адміністратор Mix не увімкне
    # PKCE для цього client_id (docs/07-roadmap.md, З9). Це НЕ «зайва
    # обережність»: без PKCE і без client_secret обмін коду не підтверджується
    # нічим — __post_init__ це й перевіряє.
    oidc_pkce: bool = field(default_factory=lambda: _bool("OIDC_PKCE", False))

    # --- підключення студента ----------------------------------------------
    public_host: str = field(default_factory=lambda: _env("PUBLIC_DB_HOST", "localhost"))
    port_primary: int = field(default_factory=lambda: int(_env("PGPROXY_PORT_PRIMARY", "55432")))
    port_fallback: int = field(default_factory=lambda: int(_env("PGPROXY_PORT_FALLBACK", "8080")))

    # --- політика -----------------------------------------------------------
    token_ttl_days: int = field(default_factory=lambda: int(_env("DB_TOKEN_TTL_DAYS", "180")))

    # Прапорець Secure на сесійній куці. За замовчуванням увімкнений: без TLS
    # кука ходила б відкритим текстом. Вимикати ЛИШЕ для локальної роботи по
    # http, поки немає caddy — інакше сесію просто не видно (браузер і curl
    # не зберігають Secure-куку з http).
    cookie_secure: bool = field(default_factory=lambda: _bool("SESSION_COOKIE_SECURE", True))

    # --- режим розробки -----------------------------------------------------
    # Вхід в обхід Mix. Потрібен, поки LMS недоступна. НЕ вмикати на сервері:
    # будь-хто зможе увійти як будь-хто. Кожен такий вхід пише db_event
    # і показує банер у кабінеті.
    dev_login: bool = field(default_factory=lambda: _bool("DEV_LOGIN_ENABLED"))

    # --- версія збірки (З16) --------------------------------------------------
    # Cache-busting статики й footer «версія … від …». Значення приходять
    # build ARG-ами з Dockerfile — .git усередині образу немає.
    build_commit: str = field(default_factory=lambda: _env("BUILD_COMMIT", "dev"))
    build_date: str = field(default_factory=lambda: _env("BUILD_DATE", ""))

    def __post_init__(self) -> None:
        base = f"host={self.pg_host} port={self.pg_port} sslmode=disable"
        object.__setattr__(self, "app_dsn",
                           f"{base} dbname=observer user=obs_app password={_secret('pg_app_password')}")
        object.__setattr__(self, "provisioner_dsn",
                           f"{base} dbname=postgres user=obs_provisioner "
                           f"password={_secret('pg_provisioner_password')}")
        # obs_maintenance (член pg_maintain, З15б) — DSN без dbname: конкретна
        # база студента підставляється в maintenance.py, бо там її не знати
        # заздалегідь (одна роль на всі бази студентів, а не пул на кожну).
        object.__setattr__(self, "_maintenance_base_dsn",
                           f"{base} user=obs_maintenance "
                           f"password={_secret('pg_maintenance_password')}")
        if self.oidc_client_id and not self.oidc_pkce and not optional_secret("oidc_client_secret"):
            raise RuntimeError(
                "OIDC: немає ні client_secret (secrets/oidc_client_secret), ні "
                "PKCE (OIDC_PKCE=1) — авторизаційний код можна було б обміняти "
                "без жодного підтвердження клієнта. Вхід зупинено навмисно.")

    @property
    def master_secret(self) -> bytes:
        return _secret("master_secret").encode()

    @property
    def session_secret(self) -> str:
        return _secret("session_secret")

    def maintenance_dsn(self, db_name: str) -> str:
        """DSN для obs_maintenance у конкретну базу студента (З15б).

        Пряме підключення повз pgproxy — так само, як `app_dsn`/
        `provisioner_dsn`: це внутрішній адмінський канал, не студентський
        трафік, тож проксі тут ні до чого.
        """
        return f"{self._maintenance_base_dsn} dbname={db_name} connect_timeout=5"


settings = Settings()
