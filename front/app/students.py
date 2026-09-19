"""Пошук і створення студента за OIDC-ідентифікатором."""
from __future__ import annotations

from uuid import UUID

from .config import settings
from .db import execute, fetchone

FIELDS = ("id, oidc_sub, email, display_name, lms_roles, is_admin, is_teacher, "
          "slot, db_name, role_name, db_token_hash, db_token_set_at, "
          "db_token_expires_at, consented_at, research_consent, research_consent_at, "
          "created_at, last_seen_at, disabled_at")


async def get_by_id(student_id: UUID) -> dict | None:
    row = await fetchone(f"SELECT {FIELDS} FROM obs.students WHERE id = %s", (student_id,))
    return _row(row)


async def upsert_from_claims(identity: dict) -> dict:
    """Знаходить студента за sub або створює.

    Профільні поля оновлюються при кожному вході: у LMS могли змінити прізвище
    чи пошту. `is_admin` НЕ чіпаємо — вона проставляється руками в базі і жоден
    код застосунку її не змінює (ADR-006), крім першого рядка `obs.students`,
    де це робить тригер бази, а не цей код (ADR-023). `is_teacher` так само не
    чіпаємо — її роздає лише адмінка.
    """
    await execute(
        """
        INSERT INTO obs.students (oidc_sub, email, display_name,
                                  lms_roles, last_seen_at)
        VALUES (%s, %s, %s, %s, now())
        ON CONFLICT (oidc_sub) DO UPDATE
           SET email        = EXCLUDED.email,
               display_name = EXCLUDED.display_name,
               lms_roles    = EXCLUDED.lms_roles,
               last_seen_at = now()
        """,
        (identity["oidc_sub"], identity.get("email"), identity.get("display_name"),
         identity.get("lms_roles") or []),
    )
    groups = identity.get("lms_groups")
    if groups is not None:
        await _sync_groups_from_lms(identity["oidc_sub"], groups)
    # Типова група — рік реєстрації, а не константа з .env: константу треба
    # памʼятати міняти щовересня, і рік-два вона тихо бреше. Ставиться лише
    # тим, у кого груп немає взагалі: якщо Mix щойно віддав claim groups (гілка
    # вище), цей INSERT нічого не додає — NOT EXISTS уже неправда.
    await execute(
        """
        INSERT INTO obs.student_groups (student_id, group_name, since)
        SELECT s.id, to_char(s.created_at, 'YYYY'), s.created_at::date
          FROM obs.students s
         WHERE s.oidc_sub = %s
           AND NOT EXISTS (SELECT 1 FROM obs.student_groups g
                            WHERE g.student_id = s.id)
        ON CONFLICT DO NOTHING
        """, (identity["oidc_sub"],))
    row = await fetchone(f"SELECT {FIELDS} FROM obs.students WHERE oidc_sub = %s",
                         (identity["oidc_sub"],))
    return _row(row)


async def _sync_groups_from_lms(oidc_sub: str, groups: list[str]) -> None:
    """Mix стає джерелом правди для груп цього студента: відкриває нові
    членства, закриває ті, яких більше нема в claim. Кілька одночасних груп —
    норма (вибіркові дисципліни на пів року йдуть окремою групою поруч з
    основною), а не помилка мапінгу.

    `sg.since < current_date` у закритті — щоб не впертися в CHECK (until >
    since): групу, яку Mix відкрив і забрав в один день, наступний вхід
    закриє наступного дня, а не впаде.
    """
    if not groups:
        groups = []
    await execute(
        """
        INSERT INTO obs.student_groups (student_id, group_name, since)
        SELECT s.id, g.name, current_date
          FROM obs.students s, unnest(%s::text[]) AS g(name)
         WHERE s.oidc_sub = %s
           AND NOT EXISTS (SELECT 1 FROM obs.student_groups sg
                            WHERE sg.student_id = s.id AND sg.group_name = g.name
                              AND sg.until IS NULL)
        ON CONFLICT DO NOTHING
        """, (groups, oidc_sub))
    closed = await fetchone(
        """
        WITH upd AS (
            UPDATE obs.student_groups sg
               SET until = current_date
              FROM obs.students s
             WHERE sg.student_id = s.id
               AND s.oidc_sub = %s
               AND sg.until IS NULL
               AND sg.since < current_date
               AND NOT (sg.group_name = ANY(%s::text[]))
         RETURNING sg.student_id
        )
        SELECT DISTINCT student_id FROM upd
        """, (oidc_sub, groups))
    if closed:
        # Групу змінили — старі записи в кеші прив'язки лаб (obs.fingerprint_labs)
        # порахували lab_id за вже неактуальною групою і без інвалідації лишилися б
        # неправильними назавжди (_labs_for_groups звужує кандидатів саме за
        # поточними групами). Фоновий refresh_fingerprint_labs() сам перерахує
        # цього студента наступним проходом (NOT EXISTS — "лише вперед").
        await execute(
            "DELETE FROM obs.fingerprint_labs WHERE student_id = %s", (closed[0],))


async def set_token(student_id: UUID, token_hash: str, ttl_days: int) -> None:
    await execute(
        """
        UPDATE obs.students
           SET db_token_hash       = %s,
               db_token_set_at     = now(),
               db_token_expires_at = now() + make_interval(days => %s)
         WHERE id = %s
        """,
        (token_hash, ttl_days, student_id),
    )


async def give_consent(student_id: UUID) -> None:
    await execute("UPDATE obs.students SET consented_at = now() WHERE id = %s", (student_id,))


async def set_research_consent(student_id: UUID, decision: str) -> None:
    """Рішення щодо участі в дослідженні. Змінюється в обидва боки будь-коли.

    До доступу в базу і до оцінки стосунку не має — і це не декларація, а
    властивість коду: `research_consent` не читає ні провізіонінг, ні проксі,
    ні жоден екран з оцінками.
    """
    if decision not in ("given", "declined"):
        raise ValueError(f"невідоме рішення: {decision!r}")
    await execute(
        "UPDATE obs.students SET research_consent = %s, research_consent_at = now() "
        "WHERE id = %s", (decision, student_id))


def _row(row) -> dict | None:
    if row is None:
        return None
    return dict(zip([f.strip() for f in FIELDS.split(",")], row))
