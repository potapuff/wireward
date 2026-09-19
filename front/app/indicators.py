"""Показники процесу. Формули заморожені — див. docs/11-indicators.md (П1.1).

Навіщо заморожувати. Показник, підібраний після того, як побачено результат,
не доводить нічого: за достатньої кількості спроб знайдеться формула, яка
корелює з чим завгодно. Справжня преретеєстрація нам недоступна, тому її
замінник — ці формули, закомічені з датою до того, як зʼявилися дані.

**Правило, від якого не відступаємо:** ці числа описують АКТИВНІСТЬ, а не
АВТОРСТВО. Один вставлений блок SQL, що виконався з першого разу, у даних
виглядає як робота дуже сильного студента. Тому жоден показник тут не
називається «ознакою списування», нічого не блокує і нікого не ранжує. Вони
дають викладачеві питання до захисту, а не відповідь замість нього.

Порахувати на живому журналі: `make indicators`.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from statistics import median

log = logging.getLogger("observer.indicators")

# Мітка набору формул. Змінюється РАЗОМ із будь-якою сталою чи формулою вище і
# з новим розділом у docs/11-indicators.md. Кешовані значення старої версії від
# цього не зникають — вони стають видимо старими, а не тихо змішуються з
# новими в одній вибірці.
FORMULA_VERSION = "2026-09-04b"

# Вікна, які тримаються порахованими. 0 — увесь журнал: саме його читає картка
# студента, бо лабораторна не обмежена часом (ADR-019).
PERIODS = (0, 30)

# --- сталі формул. Змінювати ЛИШЕ з новою датою в docs/11-indicators.md -----

# І2: довжина, з якої запит уже не набирається за секунди.
PASTE_CHARS = 200
# І2: пауза перед запитом, коротша за яку робить набір неправдоподібним.
PASTE_SECONDS = 2.0
# І3: на скільки складність має перевищити власну базову лінію.
JUMP_OVER_BASELINE = 2
# І3: скільки днів історії формують базову лінію.
BASELINE_DAYS = 14
# І5: паузи, довші за це, вважаються перервою в роботі, а не темпом.
TEMPO_CUT_SECONDS = 600

# Класи помилок за SQLSTATE. Драбина «синтаксис → семантика → логіка» — та
# сама, що в таксономії Taipalus/Siponen/Vartiainen; логічні помилки SQLSTATE
# не має за визначенням: запит виконався, результат не той.
SYNTAX = ("42601",)                       # syntax_error
SEMANTIC = ("42P01", "42703", "42883",)   # немає таблиці / колонки / функції

INDICATORS = {
    "first_try": "частка відбитків, що спрацювали з першої спроби",
    "paste": "частка довгих запитів, надісланих без паузи на набір",
    "jump": "частка запитів, складніших за власну базову лінію",
    "syntax_share": "частка синтаксичних серед усіх помилок",
    "tempo": "медіана паузи між запитами, секунд",
    "resolved": "частка застрягань, з яких студент вибрався сам",
    "eq": "Error Quotient (Jadud) над послідовностями SQLSTATE",
    "eq_pos": "EQ з урахуванням позиції помилки (експеримент, інша шкала)",
    "slow": "частка помилок, на яких думав довше за три чверті групи",
}


def _complexity(constructs: set[str], tables: set[str]) -> int:
    """Складність запиту = скільки різних конструкцій і таблиць у ньому.

    Свідомо груба міра: вона порівнюється лише з ВЛАСНОЮ базовою лінією
    студента, тому важлива її монотонність, а не абсолютне значення.
    """
    return len(constructs) + len(tables)


def compute(rows: list[tuple], labs: dict[str, int] | None = None,
            bounds: dict[str, float] | None = None) -> dict:
    """Уся арифметика показників. Свідомо без бази й без async.

    Формула, яку не можна порахувати на вигаданих даних, не перевіряється
    ніяк — а заморожену формулу треба вміти перевіряти найбільше. Тому запит
    до журналу лишився в `for_student`, а рахунок живе тут і ганяється
    `make test-indicators` без кластера.

    Кожен рядок: (started_at, session_id, sql_fingerprint, sql_text,
                  status, sqlstate, length, error_position, duration_ms)
    — упорядковані за часом. Два останні поля потрібні лише І7 і І8.
    """
    from .sqlshape import constructs, tables

    if not rows:
        return {"queries": 0}

    # --- І1: з першої спроби ------------------------------------------------
    # За відбитком: чи був УСПІШНИМ перший запит із цим відбитком. Саме за
    # відбитком, а не за текстом: правка пробілу не робить спробу новою.
    first_status: dict[str, str] = {}
    for _at, _sess, fp, _sql, status, *_ in rows:
        first_status.setdefault(fp, status)
    first_try = (sum(1 for s in first_status.values() if s == "ok")
                 / len(first_status)) if first_status else 0.0

    # --- І2: вставка --------------------------------------------------------
    # Пауза рахується в межах ОДНІЄЇ сесії: між сесіями пауза нічого не значить.
    prev_at: dict[str, object] = {}
    long_n = paste_n = 0
    gaps: list[float] = []
    for at, sess, _fp, _sql, _status, _st, ln, *_ in rows:
        p = prev_at.get(str(sess))
        gap = (at - p).total_seconds() if p else None
        prev_at[str(sess)] = at
        if gap is not None and gap <= TEMPO_CUT_SECONDS:
            gaps.append(gap)
        if ln and ln >= PASTE_CHARS:
            long_n += 1
            if gap is not None and gap < PASTE_SECONDS:
                paste_n += 1
    paste = paste_n / long_n if long_n else 0.0

    # --- І3: стрибок складності --------------------------------------------
    cx = [(at, _complexity(constructs(sql), tables(sql)))
          for at, _s, _fp, sql, *_ in rows]
    baseline_cut = cx[0][0] + timedelta(days=BASELINE_DAYS)
    base = sorted(c for at, c in cx if at <= baseline_cut)
    if base:
        p80 = base[min(len(base) - 1, int(0.8 * len(base)))]
        jump_n = sum(1 for at, c in cx if at > baseline_cut and c > p80 + JUMP_OVER_BASELINE)
        after = sum(1 for at, _c in cx if at > baseline_cut)
        jump = jump_n / after if after else 0.0
    else:
        jump = 0.0

    # --- І4: склад помилок --------------------------------------------------
    errs = [st for _a, _s, _f, _q, status, st, *_ in rows if status == "error"]
    syntax_share = (sum(1 for e in errs if e in SYNTAX) / len(errs)) if errs else 0.0
    semantic_share = (sum(1 for e in errs if e in SEMANTIC) / len(errs)) if errs else 0.0

    # --- І5: темп -----------------------------------------------------------
    tempo = median(gaps) if gaps else 0.0

    return {
        "queries": len(rows), "fingerprints": len(first_status),
        "errors": len(errs),
        "first_try": round(first_try, 3),
        "paste": round(paste, 3), "paste_of": long_n,
        "jump": round(jump, 3),
        "syntax_share": round(syntax_share, 3),
        "semantic_share": round(semantic_share, 3),
        "tempo": round(tempo, 1),
        "eq": round(error_quotient(rows, labs), 3),
        # Експеримент, а не заміна І7: третя версія формули, /8 замість /11.
        "eq_pos": _round_or_none(error_quotient_pos(rows, labs), 3),
        # І8 без розподілів по групі не рахується — і це видно як None,
        # а не як нуль, який читався б як «швидкий».
        "slow": slow_share(rows, bounds or {}, labs),
    }


def _round_or_none(v: float | None, n: int) -> float | None:
    return None if v is None else round(v, n)


# Скільки прикладів «без спроб» показувати. Це не рейтинг і не рахунок —
# перелік для розмови, і довший за десяток його ніхто не читатиме.
NO_TRACE_LIMIT = 10

# Оператори, які створюють схему. Їх студент отримує готовими в методичці.
DDL_WORDS = frozenset("create alter drop truncate comment".split())


def without_traces(rows: list[tuple], labs: dict[str, int] | None = None) -> list[dict]:
    """Запити, які зʼявилися в журналі готовими — без видимого шляху до них.

    **Це не детектор.** Ані ШІ, ані списування, ані чого завгодно ще: рівно так
    само виглядає запит, скопійований із власної попередньої лабораторної, з
    методички, з документації або набраний людиною, яка просто знає, що робить.
    Функція нічого не блокує, нікого не ранжує і не дає числа «підозрілості».

    Вона робить одне: складає перелік конкретних запитів із часом і текстом,
    щоб викладач міг спитати про них на захисті. Відповідь дає захист.

    Ознаки, і кожна окремо нічого не варта:
      * `одразу` — перша ж спроба з цим відбитком успішна, спроб до неї немає;
      * `складніше` — складність вища за власну базову лінію студента;
      * `без набору` — довгий запит менш ніж за PASTE_SECONDS після попереднього.

    Показуються лише випадки, де збіглося **щонайменше дві**. Одна ознака —
    це звичайний робочий день: люди пишуть правильні запити з першого разу, і
    це, взагалі кажучи, мета навчання.
    """
    from .sqlshape import constructs, tables

    if not rows:
        return []

    first_seen: dict[str, tuple] = {}
    for r in rows:
        first_seen.setdefault(r[2], r)

    cx = sorted(_complexity(constructs(r[3]), tables(r[3])) for r in rows)
    p80 = cx[min(len(cx) - 1, int(0.8 * len(cx)))]

    prev_at: dict[str, object] = {}
    gap_of: dict[int, float | None] = {}
    for at, sess, _fp, _sql, _st, _sq, _ln, *_ in rows:
        p = prev_at.get(str(sess))
        gap_of[id((at, sess))] = (at - p).total_seconds() if p else None
        prev_at[str(sess)] = at

    out: list[dict] = []
    for r in rows:
        at, sess, fp, sql, status, _sq, ln = r[:7]
        if first_seen.get(fp) is not r or status != "ok":
            continue
        # Свій запобіжник проти службових запитів клієнта. `WORK_ONLY` мав би
        # їх уже відсіяти, але в журналі стенда знайшлися ті, що просочилися
        # (див. «Відкриті питання» у 07-roadmap.md). Тут ціна такої похибки
        # найвища з усіх: службовий запит DataGrip, поданий викладачеві як
        # «зʼявилося без спроб», — це хибне звинувачення на захисті. Тому
        # перелік захищається сам, не чекаючи, поки розберуться з класифікатором.
        if any(t.startswith(("pg_", "information_schema")) for t in tables(sql)):
            continue
        # DDL сюди теж не йде. Скрипт створення схеми студентам ВИДАЮТЬ —
        # питати «звідки він у вас узявся» безглуздо, а перелік, у якому
        # половина рядків очевидно безневинна, перестають читати цілком.
        if (sql.lstrip().split() or [""])[0].lower() in DDL_WORDS:
            continue
        signals = ["одразу"]
        if _complexity(constructs(sql), tables(sql)) > p80 + JUMP_OVER_BASELINE:
            signals.append("складніше")
        gap = gap_of.get(id((at, sess)))
        if ln and ln >= PASTE_CHARS and gap is not None and gap < PASTE_SECONDS:
            signals.append("без набору")
        if len(signals) < 2:
            continue
        out.append({"at": at.isoformat(timespec="seconds"), "sql": sql[:400],
                    "signals": signals, "lab_id": (labs or {}).get(fp)})
    out.sort(key=lambda d: (-len(d["signals"]), d["at"]))
    return out[:NO_TRACE_LIMIT]


def _pairs(rows: list[tuple], labs: dict[str, int] | None = None
           ) -> list[tuple[tuple, tuple, tuple]]:
    """Пари сусідніх запитів для І7 і І8 — з двома поправками Watson.

    **Пари будуються в межах (сесія, лабораторна), а не просто сесії.** Watson
    показав, що EQ рахує помилку у файлі A виправленою, якщо наступною подією
    був успішний прогін файлу B: студент просто перемкнувся, а метрика бачить
    успіх. Такі фальшиві нулі ЗАНИЖУЮТЬ EQ — студент виглядає сильнішим, ніж є.

    У SQL файлів немає, але точний аналог є і він у нас частий: студент робить
    дві лабораторні впереміш в одній сесії. Це не крайній випадок — саме його
    ми підтримуємо навмисно (`labs.where_now`, поле `also`). Запити поза будь-
    якою лабою лишаються в спільній купі: для них розділяти нема за чим.

    **Відкидаються пари «та сама невдала форма, той самий SQLSTATE» підряд.**
    І Jadud (нульова відстань редагування), і Watson (ідентичні знімки коду)
    прибирають повторний запуск без змін: він роздуває знаменник, не додаючи
    інформації. У SQL ціна цього кроку вища, ніж у Java, бо повторний запуск
    ТОГО САМОГО запиту тут буває осмисленим — «подивитися, чи змінилися дані».
    Тому відкидаємо лише коли обидві події помилкові, форма та сама і SQLSTATE
    той самий: успішний повтор лишається.

    Розбір і джерела — docs/student-performance-metrics/02-watwin.md.
    """
    labs = labs or {}
    groups: dict[tuple[str, int], list[tuple]] = {}
    for row in rows:
        groups.setdefault((str(row[1]), labs.get(row[2], 0)), []).append(row)

    out = []
    for key, events in groups.items():
        for a, b in zip(events, events[1:]):
            if (a[4] == "error" and b[4] == "error"
                    and a[2] == b[2] and a[5] == b[5]):
                continue                      # холостий повтор — не пара
            out.append((key, a, b))
    return out


def error_quotient(rows: list[tuple], labs: dict[str, int] | None = None) -> float:
    """Error Quotient (Jadud), перенесений із компіляцій на SQL. Показник І7.

    **Версія ICER 2006** — дві складові, нормування на 11:

        обидві з помилкою            → 8
        і той самий SQLSTATE         → +3
        нормування                   → /11

    Це треба називати вголос, бо «EQ» — щонайменше дві різні формули: у
    дисертації Jadud 2005 їх чотири (додаються «та сама позиція помилки» +3 і
    «те саме місце правки» +1) з нормуванням на 9. Числа в них різні.
    Реалізація звірена з еталонною на всіх дев'яти опублікованих векторах —
    `JADUD_VECTORS` у scripts/test-indicators.py.

    Далі середнє по парах у групі, потім середнє по групах. 0 — жодної пари
    підряд з помилками; 1 — студент раз за разом б'ється в ту саму помилку.

    Що змінено при перенесенні: «тип помилки» — це `SQLSTATE`, прямий аналог
    класу помилки компілятора (у Java його дістають регулярними виразами з
    тексту, у PostgreSQL він приходить у протоколі). Побудова пар — `_pairs`.

    **Чого не доводить.** Високий EQ — це не «слабкий студент», а «довго
    крутиться в одній помилці». Іноді це впертість, іноді нерозуміння
    повідомлення СУБД, а іноді клієнт, що сам перевідсилає запит.
    """
    def score(a, b):
        if a[4] != "error" or b[4] != "error":
            return 0.0
        return (11.0 if (a[5] and a[5] == b[5]) else 8.0) / 11.0

    return _mean_by_group(_pairs(rows, labs), score)


# І7-поз: наскільки далеко можуть розійтися позиції помилки, щоб вони ще
# вважалися «тією самою». Jadud дозволяв ±1 рядок; у нас позиція — зміщення в
# символах, і правка перед помилкою його зсуває. Число — ПРИПУЩЕННЯ, яке треба
# перевірити на живих даних; поки показник експериментальний, ціна помилки в
# ньому нульова.
EQ_POSITION_SLACK = 10


def error_quotient_pos(rows: list[tuple],
                       labs: dict[str, int] | None = None) -> float | None:
    """EQ із третьою складовою — позицією помилки. Експеримент, не заміна І7.

    Jadud прибрав із версії 2006 дві складові з чотирьох: «та сама позиція
    помилки» і «те саме місце правки». У Java перша коштує номера рядка з
    компілятора, друга — плагіна до IDE.

    **У нас перша безкоштовна:** PostgreSQL повертає позицію помилки в самому
    `ErrorResponse`, і ми її вже зберігаємо (`obs.queries.error_position`).
    Причому точніше за Jadud — зміщення в символах, а не рядок ± 1.

    Другу складову не реалізовано і не буде: у SQL немає редагування, кожен
    запит надсилається цілим. Тому нормування тут /8 (2+3+3), а не /9 — це
    ТРЕТЯ версія EQ, і порівнювати її з літературою напряму не можна. Вона
    відповідає на власне питання: чи додає позиція помилки пояснювальної сили
    на SQL, якщо на Java не додавала.

    Повертає None, якщо в журналі немає жодної позиції помилки, — краще
    «не рахувалося», ніж нуль, який виглядає як результат.
    """
    if not any(r[7] is not None for r in rows if r[4] == "error"):
        return None

    def score(a, b):
        if a[4] != "error" or b[4] != "error":
            return 0.0
        total = 2.0
        if a[5] and a[5] == b[5]:
            total += 3.0
            if (a[7] is not None and b[7] is not None
                    and abs(a[7] - b[7]) <= EQ_POSITION_SLACK):
                total += 3.0
        return total / 8.0

    return _mean_by_group(_pairs(rows, labs), score)


# --- І8: час розв'язання відносно однокурсників ----------------------------
#
# Ідея Watson (Watwin / Robust Relative): найінформативніше в журналі — не
# частота помилок, а ЧАС, витрачений на них, і порівнювати цей час треба не з
# абсолютним порогом, а з однокурсниками НА ТІЙ САМІЙ ПОМИЛЦІ. Приріст
# виміряний і великий: R² 43.65 проти 24.49 в EQ на когорті, якої алгоритм не
# бачив (docs/student-performance-metrics/02-watwin.md).
#
# Ваги свого алгоритму Watson підбирав генетично на трьох когортах. Ми їх НЕ
# відтворюємо і в EQ час не вмішуємо: підібрані на чужих даних ваги зроблять
# І7 неспівставним із літературою, а свої підбирати нема на чому. Тому час —
# окремий показник із зрозумілим змістом, а не поправка всередині EQ.

# Скільки спостережень має мати SQLSTATE, щоб його розподіл щось означав.
EQ_SLOW_MIN_SAMPLES = 20
# Скільки MAD відсікають викид. Watson бере 2·MAD; медіанна статистика замість
# середнього з σ — бо часи скошені, і на малих вибірках σ буває більшим за
# середнє, тобто нижня межа виходить від'ємною.
EQ_SLOW_MAD = 2.0
# Нижня межа самого квартиля. Якщо верхній квартиль виходить нульовим, це
# означає, що три чверті групи «думали» нуль секунд — тобто мітки часу
# вироджені (генератор, навантажувальний тест, пакетне надсилання клієнтом).
# Порівнювати з таким порогом не можна: повільнішим за нього виявляється
# кожен, і показник мовчки перетворюється на одиницю для всіх.
EQ_SLOW_MIN_BOUND = 1.0
# Скільки днів журналу беруться на розподіли часів для І8. Не весь журнал:
# «нормальний час на 42803» на першому занятті й у грудні — різні величини,
# бо група вчиться. Півтора місяця — компроміс між свіжістю й тим, щоб на
# SQLSTATE набралося EQ_SLOW_MIN_SAMPLES спостережень.
SLOW_WINDOW_DAYS = 45


def _resolve_seconds(a: tuple, b: tuple) -> float:
    """Скільки студент ДУМАВ між невдалою спробою і наступною.

    З паузи віднімається час виконання самого запиту: запит, що виконувався
    сорок секунд, не означає сорока секунд роздумів. `duration_ms` пише проксі.
    """
    gap = (b[0] - a[0]).total_seconds() - (float(a[8] or 0) / 1000.0)
    return max(0.0, gap)


def resolve_bounds(rows: list[tuple],
                   labs: dict[str, int] | None = None) -> dict[str, float]:
    """SQLSTATE → верхній квартиль часу роздумів. Чиста функція для перевірок;
    на живому журналі те саме рахує `group_resolve_bounds` віконним запитом."""
    times: dict[str, list[float]] = {}
    for _key, a, b in _pairs(rows, labs):
        if a[4] == "error" and a[5]:
            times.setdefault(a[5], []).append(_resolve_seconds(a, b))
    return _bounds_from_times(times)


def _bounds_from_times(times: dict[str, list[float]]) -> dict[str, float]:
    """Верхній квартиль на кожен SQLSTATE, з робастним відсівом викидів.

    Викиди прибираються 2·MAD — інакше одна пауза «пішов на пару» задирає
    квартиль так, що повільних не лишається взагалі. Медіанна статистика, а не
    середнє з σ: часи скошені, і на малих вибірках σ буває більшим за середнє,
    тобто нижня межа виходила б відʼємною (це та сама поправка, якою Watson
    замінив свою ж першу версію).

    SQLSTATE із менш ніж EQ_SLOW_MIN_SAMPLES спостережень не потрапляють
    у результат: квартиль по трьох числах — не квартиль.
    """
    out: dict[str, float] = {}
    for state, xs in times.items():
        if len(xs) < EQ_SLOW_MIN_SAMPLES:
            continue
        med = median(xs)
        mad = median([abs(x - med) for x in xs])
        kept = [x for x in xs if mad == 0 or abs(x - med) <= EQ_SLOW_MAD * mad]
        if len(kept) < EQ_SLOW_MIN_SAMPLES:
            kept = xs
        kept = sorted(kept)
        q3 = kept[(3 * len(kept)) // 4]
        if q3 >= EQ_SLOW_MIN_BOUND:
            out[state] = q3
    return out


def slow_share(rows: list[tuple], bounds: dict[str, float],
               labs: dict[str, int] | None = None) -> float | None:
    """І8 — частка помилок, на яких студент думав довше за три чверті групи.

    0 — на кожній помилці студент укладався швидше за верхній квартиль групи;
    1 — на кожній був повільнішим за три чверті однокурсників.

    **Чого не доводить.** Повільність тут — не слабкість. Це може бути пауза
    на читання підручника, розмова з викладачем або просто обід. Показник
    відповідає на питання «де варто спитати, що сталося», а не «хто гірший».

    None, якщо жодна помилка студента не має розподілу по групі: краще
    порожньо, ніж нуль, який читається як «швидкий».
    """
    if not bounds:
        return None
    seen = slow = 0
    for _key, a, b in _pairs(rows, labs):
        if a[4] != "error" or a[5] not in bounds:
            continue
        seen += 1
        if _resolve_seconds(a, b) > bounds[a[5]]:
            slow += 1
    return round(slow / seen, 3) if seen else None


def _mean_by_group(pairs: list[tuple[tuple, tuple, tuple]], score) -> float:
    """Середнє по парах у групі, далі середнє по групах.

    Одиниця усереднення — (сесія, лабораторна): у Jadud EQ є властивістю
    СЕСІЇ, і сесія з двох подій важить стільки ж, скільки сесія з двохсот.
    Еталонна реалізація Švábenського усереднює по всіх парах завдання одразу;
    за різної довжини сесій це інші числа, і в Methodology це має бути назване
    одним реченням, а не замовчане.
    """
    by_group: dict[tuple, list[float]] = {}
    for key, a, b in pairs:
        by_group.setdefault(key, []).append(score(a, b))
    if not by_group:
        return 0.0
    means = [sum(v) / len(v) for v in by_group.values()]
    return sum(means) / len(means)


async def _rows(student_id: str, days: int) -> list[tuple]:
    from .db import WORK_ONLY, fetchall

    since = ("'-infinity'::timestamptz" if days == 0
             else f"now() - interval '{int(days)} days'")
    rows = await fetchall(f"""
        SELECT q.started_at, q.session_id, q.sql_fingerprint, q.sql_text,
               q.status, q.sqlstate, length(q.sql_text),
               -- Позиція помилки й час виконання додані для І7 і І8
               -- (docs/student-performance-metrics/06-adaptation.md).
               -- Обидва поля вже пише проксі; окремої ціни в них немає.
               q.error_position, coalesce(q.duration_ms, 0)
          FROM obs.queries q
         WHERE q.student_id = %s::uuid AND {WORK_ONLY} AND q.started_at > {since}
         ORDER BY q.started_at
    """, (student_id,))
    return [tuple(r) for r in rows]


async def _with_stuck(student_id: str, out: dict) -> dict:
    """І6 — єдиний показник, який рахує не compute(): застрягання вже зведені
    в obs.sticking_points фоновим job'ом."""
    from .db import fetchall

    if not out.get("queries"):
        return out
    st_rows = await fetchall(
        "SELECT count(*), count(*) FILTER (WHERE resolved) FROM obs.sticking_points "
        " WHERE student_id = %s::uuid", (student_id,))
    stuck_total, stuck_ok = (st_rows[0] if st_rows else (0, 0))
    out["stuck"] = stuck_total
    out["resolved"] = round(stuck_ok / stuck_total, 3) if stuck_total else None
    return out


async def for_student(student_id: str, days: int = 0,
                      labs: dict[str, int] | None = None,
                      bounds: dict[str, float] | None = None) -> dict:
    return await _with_stuck(
        student_id, compute(await _rows(student_id, days), labs, bounds))


async def table(days: int = 0) -> list[dict]:
    from .db import fetchall
    from .labs import fingerprint_map

    people = await fetchall("""
        SELECT s.id::text, coalesce(s.display_name, s.email, s.oidc_sub)
          FROM obs.students s
         WHERE s.disabled_at IS NULL
           AND EXISTS (SELECT 1 FROM obs.queries q WHERE q.student_id = s.id)
         ORDER BY 2""")
    # Мапа лаб і межі часів — раз на всю таблицю: І8 порівнює студента з
    # групою, тож вибірка мусить бути спільною для всіх рядків.
    labs = await fingerprint_map()
    bounds = await group_resolve_bounds()
    out = []
    for sid, name in people:
        row = await for_student(sid, days, labs, bounds)
        if row.get("queries"):
            out.append({"student_id": sid, "name": name, **row})
    return out


async def _main() -> int:
    import sys

    from .db import close_pools, open_pools

    days = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    await open_pools()
    try:
        rows = await table(days)
    finally:
        await close_pools()

    if not rows:
        print("журнал порожній — рахувати нема чого")
        return 1

    print(f"Показники процесу, {'увесь час' if not days else f'{days} днів'}. "
          f"Формули заморожені: docs/11-indicators.md\n")
    head = (f"{'студент':22} {'запитів':>8} {'І1 перш':>8} {'І2 встав':>9} "
            f"{'І3 стриб':>9} {'І4 синт':>8} {'І5 темп':>8} {'І6 вибр':>8} "
            f"{'І7 EQ':>7} {'І8 повіл':>9}")
    print(head)
    print("-" * len(head))
    for r in rows:
        res = f"{r['resolved']:.2f}" if r["resolved"] is not None else "  — "
        slow = f"{r['slow']:.2f}" if r.get("slow") is not None else "  — "
        print(f"{r['name'][:22]:22} {r['queries']:8d} {r['first_try']:8.2f} "
              f"{r['paste']:9.2f} {r['jump']:9.2f} {r['syntax_share']:8.2f} "
              f"{r['tempo']:8.1f} {res:>8} {r['eq']:7.2f} {slow:>9}")
    print("\nЦі числа описують АКТИВНІСТЬ, а не АВТОРСТВО. Вони дають питання")
    print("до захисту, а не відповідь замість нього.")
    return 0


# ---------------------------------------------------------------- кеш значень
# Рахувати показники на кожне відкриття сторінки не можна: for_student()
# витягає весь журнал студента разом із текстами запитів. Це та сама причина,
# через яку екрани читають obs.activity_buckets, а не obs.queries.

async def refresh(student_id: str, days: int, lab_id: int = 0,
                  values: dict | None = None) -> dict:
    """Покласти пораховане в obs.indicators."""
    import json

    from .db import execute

    vals = values if values is not None else await for_student(student_id, days)
    await execute("""
        INSERT INTO obs.indicators
               (student_id, period_days, lab_id, formula_version, values)
        VALUES (%s::uuid, %s, %s, %s, %s)
        ON CONFLICT (student_id, period_days, lab_id, formula_version) DO UPDATE
           SET values = EXCLUDED.values, computed_at = now()
    """, (student_id, days, lab_id, FORMULA_VERSION, json.dumps(vals)))
    return vals


async def refresh_student(student_id: str, fp_labs: dict[str, int] | None = None,
                          bounds: dict[str, float] | None = None) -> int:
    """Один прохід журналу студента — усі його показники.

    Розкладати запити по лабораторних окремими вибірками означало б читати
    журнал стільки разів, скільки лаб. Тому читаємо раз і ділимо в памʼяті.

    Розріз по лабах рахується ЛИШЕ для повного журналу (period_days=0), і це
    не економія: лабораторна не обмежена часом (ADR-019), тож «показники лаби
    за 30 днів» — величина, у якої немає змісту.
    """
    from .labs import fingerprint_map

    n = 0
    for days in PERIODS:
        rows = await _rows(student_id, days)
        labs = (fp_labs if fp_labs is not None
                else await fingerprint_map(student_id)) if days == 0 else {}
        vals = await _with_stuck(student_id, compute(rows, labs, bounds))
        if days == 0:
            vals["no_trace"] = without_traces(rows, labs)
        await refresh(student_id, days, 0, vals)
        n += 1
        if days != 0:
            continue
        by_lab: dict[int, list[tuple]] = {}
        for r in rows:
            lab = labs.get(r[2])
            if lab:
                by_lab.setdefault(lab, []).append(r)
        for lab_id, lab_rows in by_lab.items():
            # Застрягання по лабі окремо не зводяться, тож І6 тут немає:
            # краще порожньо, ніж число, яке насправді про весь журнал.
            await refresh(student_id, 0, lab_id, compute(lab_rows, labs, bounds))
            n += 1
    return n


async def cached(student_id: str, days: int = 0, lab_id: int = 0) -> dict | None:
    """Значення з кешу. None означає «ще не рахувалося», і екран має сказати
    саме це, а не показати нулі: нуль тут — таке саме число, як і будь-яке
    інше, і сплутати «немає даних» із «показник дорівнює нулю» надто легко."""
    from .db import fetchone

    row = await fetchone("""
        SELECT values, computed_at FROM obs.indicators
         WHERE student_id = %s::uuid AND period_days = %s AND lab_id = %s
           AND formula_version = %s
    """, (student_id, days, lab_id, FORMULA_VERSION))
    if not row:
        return None
    out = dict(row[0])
    out["computed_at"] = row[1]
    return out


async def refresh_stale(max_age_minutes: int = 30, limit: int = 5) -> int:
    """Оновити найзастаріліші. По кілька за раз навмисно: перерахунок читає
    журнал, і робити це для сорока студентів одночасно означало б повторювати
    ту саму помилку, від якої вже страждали агрегати активності."""
    from .db import fetchall

    rows = await fetchall("""
        SELECT s.id::text
          FROM obs.students s
          LEFT JOIN obs.indicators i
                 ON i.student_id = s.id AND i.period_days = 0 AND i.lab_id = 0
                AND i.formula_version = %s
         WHERE s.disabled_at IS NULL
           AND EXISTS (SELECT 1 FROM obs.queries q WHERE q.student_id = s.id)
           AND (i.computed_at IS NULL
                OR i.computed_at < now() - make_interval(mins => %s))
         ORDER BY i.computed_at NULLS FIRST
         LIMIT %s
    """, (FORMULA_VERSION, max_age_minutes, limit))
    if not rows:
        return 0
    # Мапа відбитків будується раз на пачку, а не на студента: зіставлення
    # зразків лаб не залежить від того, чий це журнал.
    from .labs import fingerprint_map

    fp_labs = await fingerprint_map()
    bounds = await group_resolve_bounds()
    n = 0
    for (sid,) in rows:
        await refresh_student(sid, fp_labs, bounds)
        n += 1
    return n


async def group_resolve_bounds() -> dict[str, float]:
    """Розподіли часів роздумів по ВСІЙ групі — один прохід на цикл.

    І8 порівнює студента з однокурсниками, тож вибірка мусить бути спільною:
    рахувати її по разу на студента означало б сорок проходів заради того
    самого числа.

    Пари будуються **в SQL**, віконною функцією: сюди потрапляють лише
    помилкові рядки й час до наступної дії, а не весь журнал у память. Холості
    повтори (та сама форма, той самий SQLSTATE підряд) відсіюються тут же, як
    і в `_pairs`.

    Лабораторна тут НЕ розділяє пари, і це не спрощення. Розділення в `_pairs`
    існує, щоб не зарахувати помилку виправленою через перемикання на іншу
    задачу; для розподілу часів питання інше — «скільки минуло до наступної
    дії студента», і перемикання на іншу лабу такою дією є.
    """
    from .db import WORK_ONLY, fetchall

    rows = await fetchall(f"""
        WITH steps AS (
            SELECT q.sqlstate,
                   q.sql_fingerprint,
                   coalesce(q.duration_ms, 0)                    AS ms,
                   q.started_at,
                   lead(q.started_at)      OVER w                AS next_at,
                   lead(q.sqlstate)        OVER w                AS next_state,
                   lead(q.sql_fingerprint) OVER w                AS next_fp,
                   lead(q.status)          OVER w                AS next_status
              FROM obs.queries q
             WHERE {WORK_ONLY}
               AND q.started_at > now() - interval '{SLOW_WINDOW_DAYS} days'
            WINDOW w AS (PARTITION BY q.session_id ORDER BY q.started_at)
        )
        SELECT sqlstate,
               greatest(0, extract(epoch FROM next_at - started_at) - ms / 1000.0)
          FROM steps
         WHERE sqlstate IS NOT NULL AND next_at IS NOT NULL
           AND NOT (next_status = 'error' AND next_fp = sql_fingerprint
                    AND next_state = sqlstate)
    """)

    times: dict[str, list[float]] = {}
    for state, secs in rows:
        times.setdefault(state, []).append(float(secs))
    return _bounds_from_times(times)


async def background_loop(interval: int = 120) -> None:
    """Помилка тут не має валити застосунок: значення похідні, і найгірше, що
    станеться, — екран покаже «ще не рахувалося»."""
    while True:
        try:
            await refresh_stale()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("оновлення показників не вдалося")
        await asyncio.sleep(interval)


async def for_lab(lab_id: int) -> dict[str, dict]:
    """Показники всіх студентів у розрізі однієї лабораторної.

    Читається з кешу: розрахунок на 40 студентів по відкриттю екрана — це
    сорок проходів журналу.
    """
    from .db import fetchall

    rows = await fetchall("""
        SELECT student_id::text, values, computed_at
          FROM obs.indicators
         WHERE lab_id = %s AND period_days = 0 AND formula_version = %s
    """, (lab_id, FORMULA_VERSION))
    out = {}
    for sid, vals, at in rows:
        v = dict(vals)
        v["computed_at"] = at
        out[sid] = v
    return out


# Запуск модуля стоїть НАПРИКІНЦІ файлу навмисно. Раніше він був посередині, і
# все, оголошене нижче, для `python -m app.indicators` не існувало: інтерпретатор
# доходив до цього рядка й виходив. Помітилося лише тоді, коли _main() уперше
# знадобилася функція, оголошена нижче за нього.
if __name__ == "__main__":
    import asyncio
    import sys
    sys.exit(asyncio.run(_main()))
