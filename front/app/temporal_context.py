"""Часовий контекст поверх per-query оцінки — regime-switching Viterbi.

Продовження розвідки `docs/article-p4-research/07-10-temporal-context-*.md`.
Постулат: `assign(q)` (05 §3, `sqlshape2.similarity` + поріг) — незалежне
випробування на кожен запит, і саме тому шумне на межі порогу (07 п. 3).
Локальна послідовність усередині епізоду роботи над лабою є (07 п. 2), а
студент повертається до попередніх лаб і перемежовує запити паузами — це
не суперечить п. 2, лише означає, що межа паузи ≠ межа епізоду (07
термінологія, 08: Peralta et al. 0.42 vs 0.25 F-measure — контент, не час).

Синтетичний бенчмарк (10) підтвердив: спрощений regime-switching HMM
(самопетля `p_stay=0.85`, Viterbi) піднімає macro-F1 з ~0.49 (per-query
baseline) до ~0.72 без шуму й ~0.68 під повним набором шуму, без потреби в
навчальних даних — рекомендований перший кандидат для живого журналу (10,
розділ «Рекомендація для Етапу 2»). CRF дав вищу стелю, але потребує
розмічених послідовностей, яких поки немає — кандидат другої черги.

Реалізація тут — перенесення `cand_hmm` з `scripts/synth-lab-timeline.py`
(писана для синтетичного експерименту) у продакшн-код, узагальнене на
довільний ключ лаби (у проді `obs.labs.id`, у синтетиці — назва лаби) і без
жорстко вшитих порогів експерименту (`T_LO=0.40` там був фіксованою
константою одного набору лаб; тут — параметр, бо в проді поріг свій на
кожну лабу, `obs.labs.threshold`, 05 §3.4 і `front/app/labs.py`).

Це ОКРЕМИЙ шар над результатом `sqlshape2`/`shapecheck`/`labs.py`, не заміна
per-query шляху: жоден із цих модулів тут не змінюється й не викликається.
Підключення до конкретного екрану чи API — окреме рішення (07 п. 10: вихід
задачі лишається `assign(q) ∈ L ∪ {⊥}`, контекст лише інформує його; коли й
де саме — за 09 «Реалізація і власники» і 07 п. 10, не тут).
"""
from __future__ import annotations

import math
from typing import Hashable, Mapping, Sequence, TypeVar

Lab = TypeVar("Lab", bound=Hashable)

# Сентинел «поза жодною лабою» — той самий символ, що ⊥ у 05 §3.4/07, але як
# питомий Python-обʼєкт (не рядок), щоб не зіткнутися з реальним id/назвою лаби.
BOT: object = object()


def _emission_logp(scores: Mapping[Lab, float], states: Sequence[Lab],
                    bot: Lab, t_lo: float, temperature: float) -> list[float]:
    """Лог-емісія кожного стану з per-query оцінки. `bot` отримує тим більший
    бал, чим далі найкраща лаба від порога `t_lo` знизу (як у референсі:
    впевнене "не дотягує" — сильний сигнал за ⊥, а не шум навколо нуля)."""
    best = max(scores.values(), default=0.0)
    raw = [scores[s] if s != bot else max(0.01, t_lo - best) for s in states]
    m = max(raw)
    ex = [math.exp(temperature * (x - m)) for x in raw]
    z = sum(ex) or 1.0
    return [math.log(x / z + 1e-12) for x in ex]


def smooth_sequence(
    score_vectors: Sequence[Mapping[Lab, float]],
    labs: Sequence[Lab],
    *,
    p_stay: float = 0.85,
    temperature: float = 8.0,
    t_lo: float = 0.0,
    bot: Lab = BOT,  # type: ignore[assignment]
) -> list[Lab]:
    """Regime-switching Viterbi над послідовністю per-query score-векторів
    одного студента, впорядкованою по `seq`/`started_at` (front/app/db.py,
    `obs.queries`).

    `score_vectors[i]` — `{lab: score(q_i, lab)}` від `sqlshape2.similarity`
    (див. `score_vector()` у `scripts/synth-lab-timeline.py` — та сама форма,
    перенесена без змін). Приховані стани — лаби з `labs` плюс `bot` («поза
    жодною лабою», ⊥ у 05/07). Персистентність — самопетля `p_stay`: лишитися
    в тому самому стані апріорі ймовірніше, ніж перейти в будь-який інший
    (перехід — рівномірно між рештою станів, як у 10 п. 4: спрощення проти
    повного Bayesian Online Changepoint Detection, той самий якісний ефект).

    `t_lo` — поріг, нижче якого найкращий per-query бал трактується як
    "жодна лаба не підходить" (05 §3.4/`obs.labs.threshold`); викликач сам
    вирішує, чи передавати єдиний поріг, чи попередньо змішати пороги різних
    лаб у `score_vectors` — тут навмисно немає жорстко вшитого числа з
    експерименту (10 використовував фіксований `T_LO=0.40` одного набору
    лаб, тут кожен виклик обирає свій).

    Повертає список міток довжини `len(score_vectors)`, той самий порядок.
    Порожній вхід → порожній вихід.
    """
    if not score_vectors:
        return []
    if not 0.0 < p_stay < 1.0:
        raise ValueError(f"p_stay має бути в (0, 1), отримано {p_stay!r}")

    states: list[Lab] = list(labs) + [bot]
    n_states = len(states)
    stay_lp = math.log(p_stay)
    move_lp = math.log((1 - p_stay) / (n_states - 1))

    steps = len(score_vectors)
    logp = [[float("-inf")] * n_states for _ in range(steps)]
    back = [[0] * n_states for _ in range(steps)]

    e0 = _emission_logp(score_vectors[0], states, bot, t_lo, temperature)
    for s in range(n_states):
        logp[0][s] = e0[s]

    for t in range(1, steps):
        et = _emission_logp(score_vectors[t], states, bot, t_lo, temperature)
        for s in range(n_states):
            best_val, best_prev = float("-inf"), 0
            for p in range(n_states):
                tr = stay_lp if p == s else move_lp
                val = logp[t - 1][p] + tr
                if val > best_val:
                    best_val, best_prev = val, p
            logp[t][s] = best_val + et[s]
            back[t][s] = best_prev

    last = max(range(n_states), key=lambda s: logp[steps - 1][s])
    path = [0] * steps
    path[steps - 1] = last
    for t in range(steps - 1, 0, -1):
        path[t - 1] = back[t][path[t]]
    return [states[s] for s in path]
