"""SUS і коротка рефлексія (П2.6).

Навіщо саме SUS. У статті про засіб мусить бути число **про засіб**.
«Викладачам сподобалося» — не результат: його не можна ні перевірити, ні з
чимось порівняти. System Usability Scale (Brooke, 1996) — десять тверджень і
одна цифра від 0 до 100, за якою стоїть тридцятирічний корпус вимірів. 68 у ній
означає те саме, що й у будь-кого іншого, і рецензент це знає.

Формулювання тверджень — канонічні, лише перекладені: змінювати їх не можна,
інакше число перестає бути SUS і порівнювати його стає нема з чим.

Рефлексія — три відкриті питання, і вони не про засіб, а про роботу. Саме вони
дають те, чого не дасть жодна метрика: чи бачить студент у власному журналі
себе.
"""
from __future__ import annotations

# Канонічні десять тверджень. Непарні сформульовані позитивно, парні —
# негативно; чергування навмисне і є частиною методики.
SUS_ITEMS = [
    "Я хотів би користуватися цим засобом регулярно.",
    "Засіб видався мені невиправдано складним.",
    "Засобом легко користуватися.",
    "Мені потрібна була б допомога фахівця, щоб користуватися цим засобом.",
    "Різні частини засобу добре узгоджені між собою.",
    "У засобі забагато непослідовностей.",
    "Гадаю, більшість людей навчиться користуватися ним швидко.",
    "Користуватися засобом незручно.",
    "Я почувався впевнено, працюючи із засобом.",
    "Мені довелося багато чого вивчити, перш ніж я зміг почати.",
]

SUS_SCALE = ["зовсім не згоден", "радше не згоден", "важко сказати",
             "радше згоден", "цілком згоден"]

REFLECTION_ITEMS = {
    "learned": "Що ви побачили у власному журналі такого, чого не помічали за собою?",
    "stuck": "Пригадайте, де ви застрягли найдовше. Що зрештою допомогло?",
    "fair": "Чи здається вам справедливим, що викладач бачить ваші помилки? Чому?",
}


def sus_score(answers: dict) -> float:
    """Канонічний підрахунок: непарні — (x−1), парні — (5−x), сума × 2.5.

    Діапазон 0—100, але це НЕ відсотки і не оцінка: 68 — середнє по галузі, і
    саме з ним порівнюють. Подавати SUS як «68 %» — типова помилка, після якої
    число втрачає будь-який сенс.
    """
    total = 0
    for i in range(1, 11):
        v = int(answers[f"q{i}"])
        if not 1 <= v <= 5:
            raise ValueError(f"q{i}={v}: шкала від 1 до 5")
        total += (v - 1) if i % 2 else (5 - v)
    return round(total * 2.5, 1)


async def save(student_id, kind: str, answers: dict) -> float | None:
    import json

    from .db import execute

    if kind not in ("sus", "reflection"):
        raise ValueError(f"невідома анкета: {kind!r}")
    score = sus_score(answers) if kind == "sus" else None
    await execute("""
        INSERT INTO obs.surveys (student_id, kind, answers, score)
        VALUES (%s::uuid, %s, %s, %s)
        ON CONFLICT (student_id, kind) DO UPDATE
           SET answers = EXCLUDED.answers, score = EXCLUDED.score, at = now()
    """, (str(student_id), kind, json.dumps(answers, ensure_ascii=False), score))
    return score


async def mine(student_id) -> dict[str, dict]:
    from .db import fetchall

    rows = await fetchall(
        "SELECT kind, answers, score, at FROM obs.surveys WHERE student_id = %s::uuid",
        (str(student_id),))
    return {r[0]: {"answers": r[1], "score": r[2], "at": r[3]} for r in rows}


async def summary() -> dict:
    """Зведення для викладача. Окремі відповіді на рефлексію тут НЕ
    показуються: студент писав їх, знаючи, що це анкета, а не публічний допис,
    і десять рядків із іменами поруч перетворили б анкету на щось інше."""
    from .db import fetchall, fetchone

    row = await fetchone("""
        SELECT count(*), round(avg(score), 1), min(score), max(score)
          FROM obs.surveys WHERE kind = 'sus'""")
    per_item = await fetchall("""
        SELECT i, round(avg((answers->>('q' || i))::numeric), 2)
          FROM obs.surveys, generate_series(1, 10) AS i
         WHERE kind = 'sus'
         GROUP BY 1 ORDER BY 1""")
    refl = await fetchone("SELECT count(*) FROM obs.surveys WHERE kind = 'reflection'")
    return {
        "n": row[0] if row else 0,
        "avg": row[1] if row else None,
        "min": row[2] if row else None,
        "max": row[3] if row else None,
        # Ключ навмисно НЕ "items": у Jinja `sus.items` розвʼязується в метод
        # словника, а не в це поле, і шаблон падає з невиразним TypeError.
        "per_item": [{"i": r[0], "text": SUS_ITEMS[r[0] - 1], "avg": r[1]} for r in per_item],
        "reflections": refl[0] if refl else 0,
    }
