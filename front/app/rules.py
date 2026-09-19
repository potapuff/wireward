"""Правила над журналом: помилки, яких СУБД не помічає.

Запит виконується успішно й повертає не те, що студент думав. Саме такі
помилки найважче побачити у зданій роботі — і саме тут журнал дає те, чого
немає більше ніде (З5 у docs/07-roadmap.md).

Три речі, які визначають форму цього модуля:

* **Це не проксі.** Правила рахуються на боці front, по вже записаному
  журналу. Будь-яка робота в гарячому шляху проксі — ризик для того, заради
  чого продукт існує.
* **Попередження адресоване ВИКЛАДАЧЕВІ**, а не студентові, і нічого не
  блокує. Тому хибнопозитивне спрацювання коштує дешево, а хибнонегативне —
  дорого: пропущена помилка лишається невидимою назавжди.
* **Лише текст запиту.** Рівень «текст + схема» (`NOT IN (SELECT col …)`, де
  `col` допускає NULL) вимагає читати каталог студентської бази, куди `obs_app`
  доступу не має, і відкладений свідомо. Рівень «текст + результат» доданий
  там, де він безкоштовний: `NOT IN`, що повернув 0 рядків.

Модуль навмисно без залежностей — щоб його можна було перевірити скриптом без
бази й без psycopg.
"""
from __future__ import annotations

import re
from hashlib import sha1

# --- нормалізація -------------------------------------------------------------
# Коментарі, рядкові літерали й ідентифікатори в лапках знімаються одним
# проходом, а не трьома послідовними: якщо робити це трьома окремими
# `.sub()`, одинока лапка всередині `--`-коментаря чи рядка може випадково
# спаруватися з лапкою з наступного рядка ще ДО того, як сам коментар
# розпізнано, — і заміна знищить усе між ними, з'ївши частину запиту.
# У єдиному проході ліворуч-праворуч коментар завжди «забирає» свою
# позицію першим, і лапка всередині нього окремо вже не матчиться.
_TOKENIZE = re.compile(
    r"(?s)(?P<comment>/\*.*?\*/|--[^\n]*)"
    r"|(?P<dquoted>\"(?:[^\"]|\"\")*\")"
    r"|(?P<squoted>'(?:[^']|'')*')"
)


def _ident(quoted: str) -> str:
    """Ідентифікатор у лапках може легально збігатися зі службовим словом
    (`AS "from"`) або містити символи поза `[a-z0-9_]` (кирилиця, апостроф).
    Замінюємо на синтезоване ім'я, що завжди проходить `[a-z_][a-z0-9_]*` —
    інакше `count("колонка")` і психевдонім у лапках при JOIN стають
    невидимими для `_COUNT_COL`/`_JOIN_TARGET`. Хеш детермінований: той самий
    ідентифікатор у двох місцях запиту (наприклад, психевдонім і посилання на
    нього) дає той самий синтезований токен.
    """
    return "q" + sha1(quoted[1:-1].encode("utf-8", "surrogatepass")).hexdigest()[:8]


def _clean(sql: str) -> str:
    def repl(m: re.Match) -> str:
        if m.group("dquoted"):
            return _ident(m.group("dquoted"))
        if m.group("squoted"):
            return "?"
        return " "  # коментар

    return _TOKENIZE.sub(repl, sql or "")


# --- правила ------------------------------------------------------------------
# Кожне правило: (код, заголовок, пояснення для викладача, перевірка).
# Пояснення пишеться так, щоб його можна було показати студентові як є.

_EQ_NULL = re.compile(r"(?i)(?:=|<>|!=)\s*null\b")
_NOT_IN_SELECT = re.compile(r"(?i)\bnot\s+in\s*\(\s*select\b")
_HAVING = re.compile(r"(?i)\bhaving\b")
_GROUP_BY = re.compile(r"(?i)\bgroup\s+by\b")
_FROM_COMMA = re.compile(
    r"(?i)\bfrom\b([^()]*?)\b"
    r"(?:where|group|order|limit|having|union|intersect|except|$)")
_WHERE = re.compile(r"(?i)\bwhere\b")
_COUNT_COL = re.compile(
    r"(?i)\bcount\s*\(\s*(?!\*|distinct\b)"
    r"(?:([a-z_][a-z0-9_]*)\.)?([a-z_][a-z0-9_]*)\s*\)")
_KEY_LIKE = re.compile(r"(?i)^(?:id|.*_id)$")
_JOIN_TARGET = re.compile(
    r"(?i)\b(?:left|right|full)\s+(?:outer\s+)?join\s+"
    r"([a-z_][a-z0-9_.]*)"
    r"(?:\s+(?:as\s+)?(?!on\b|using\b)([a-z_][a-z0-9_]*))?")


def _outer_join_aliases(s: str) -> set[str]:
    """Псевдоніми (або, якщо його нема, самі назви) таблиць, доданих через
    LEFT/RIGHT/FULL JOIN — той бік, де рядки можуть бути відсутні."""
    out = set()
    for table, alias in _JOIN_TARGET.findall(s):
        out.add((alias or table.rsplit(".", 1)[-1]).lower())
    return out


_SETOP = re.compile(r"(?i)\b(?:union|intersect|except)\b(?:\s+all\b)?")
_IS_NULL = re.compile(r"(?i)\bis\s+(?:not\s+)?null\b")
# Токени, що змінюють контекст: WHERE/ON/HAVING роблять вираз умовою; SET і
# VALUES — присвоєнням чи значенням. CASE й будь-яка відкривна дужка
# відкладають поточний контекст на стек, не змінюючи його самі по собі —
# `VALUES (null, a = null)` лишається в контексті VALUES усередині дужки,
# а не скидається в «немає клаузи». Контекст усередині міняє лише явний
# токен клаузи (WHEN/WHERE/…), і саме це відрізняє `VALUES (a = null)`
# (значення, не помилка) від `SET x = (SELECT … WHERE y = null)` (де WHERE
# явно перевстановлює контекст). END і закривна дужка повертають контекст,
# що був до CASE/дужки — щоб друге присвоєння після неї не лишалося в
# контексті вкладеного виразу.
_TOKEN = re.compile(r"(?i)\b(where|on|having|case|when|end|set|values)\b|([()])")
_COMPARING = {"where", "on", "having", "when"}


def _null_comparison(s: str, rows: int | None) -> bool:
    """`x = NULL` ніколи не TRUE — має бути `IS NULL`.

    Той самий текст поза умовою — не помилка: `UPDATE … SET x = NULL`
    присвоює NULL, і це єдиний спосіб це зробити. Тому дивимося, у якому
    контексті стоїть порівняння: немає жодної клаузи (список вибірки й
    подібне) або WHERE/ON/HAVING/WHEN — умова; SET/VALUES — ні.

    Один прохід замість повторного сканування префікса на кожне входження:
    останнє важило б O(n·k) на запит із k входженнями `= NULL`, а рядок
    журналу — до 64 КБ (межа проксі), і на такому вході повторне сканування
    коштувало секунди в синхронному циклі адмін-екрана.
    """
    positions = [(m.start(), None) for m in _EQ_NULL.finditer(s)]
    if not positions:
        return False
    events = [(m.start(), m.group(1).lower() if m.group(1) else m.group(2))
              for m in _TOKEN.finditer(s)]
    events.sort(key=lambda e: e[0])
    stack: list[str | None] = []
    ctx: str | None = None
    ei = 0
    for pos, _ in positions:
        while ei < len(events) and events[ei][0] < pos:
            w = events[ei][1]
            if w in ("case", "("):
                stack.append(ctx)
            elif w == "when":
                ctx = "when"
            elif w in ("end", ")"):
                if stack:
                    ctx = stack.pop()
            elif w in ("set", "values"):
                ctx = "assign"
            else:  # where, on, having
                ctx = w
            ei += 1
        if ctx is None or ctx in _COMPARING:
            return True
    return False


def _spans(s: str, opener: re.Pattern) -> list[str]:
    """Вміст дужок після кожного збігу з урахуванням вкладення.

    Обірваний фрагмент (дужку так і не закрито — типово для тексту,
    витягнутого зі звіту) пропускається: довіряти недописаному вмісту
    небезпечніше, ніж не побачити його взагалі.
    """
    out = []
    for m in opener.finditer(s):
        depth, i = 1, m.end()
        while i < len(s) and depth:
            depth += (s[i] == "(") - (s[i] == ")")
            i += 1
        if depth == 0:
            out.append(s[m.end():i - 1])
    return out


_NOT_IN_OPEN = re.compile(r"(?i)\bnot\s+in\s*\(")
_SELECT_START = re.compile(r"(?i)\s*select\b")


def _is_subquery(inner: str) -> bool:
    """Вміст дужки — підзапит (`NOT IN (SELECT …)`), а не список значень
    (`NOT IN (1, 2, (SELECT …))`, де підзапит — лише один з елементів)."""
    return bool(_SELECT_START.match(inner))


def _not_in_null_literal(s: str, rows: int | None) -> bool:
    """`NOT IN (1, 2, NULL)` завжди порожній результат.

    Пастка саме в списку значень. `NOT IN (SELECT … WHERE c IS NOT NULL)` —
    протилежне: так пишуть ті, хто цю пастку знає й від неї захистився.
    """
    for inner in _spans(s, _NOT_IN_OPEN):
        if _is_subquery(inner):
            continue
        if _IS_NULL.search(inner):
            continue
        if re.search(r"(?i)\bnull\b", inner):
            return True
    return False


def _not_in_empty(s: str, rows: int | None) -> bool:
    """`NOT IN (SELECT …)`, що повернув 0 рядків, — підозра, а не діагноз."""
    return rows == 0 and bool(_NOT_IN_SELECT.search(s))


def _having_without_group(s: str, rows: int | None) -> bool:
    return bool(_HAVING.search(s)) and not _GROUP_BY.search(s)


def _cross_join(s: str, rows: int | None) -> bool:
    """Кілька таблиць через кому і жодного WHERE — декартів добуток.

    Кожна гілка операції над множинами перевіряється окремо: кома в списку
    вибірки сусідньої гілки — не з'єднання. Гілку обрізаємо від пробілів і
    крапки з комою — інакше межа `\\b…$` під кінець гілки не збігається й
    правило мовчить рівно там, де мало б спрацювати.
    """
    for branch in _SETOP.split(s):
        branch = branch.strip().rstrip(";").rstrip()
        if _WHERE.search(branch):
            continue
        m = _FROM_COMMA.search(branch)
        if m and "," in m.group(1):
            return True
    return False


def _count_column(s: str, rows: int | None) -> bool:
    """`count(колонка)` замість `count(*)`.

    Різниця виникає лише тоді, коли колонка допускає NULL, а це властивість
    схеми, не тексту — тому правило лишається підказкою. Один вузький
    випадок усе ж виключено: `count(псевдонім.ключ)`, де псевдонім належить
    таблиці, доданій через LEFT/RIGHT/FULL JOIN, а колонка має ім'я `id`
    чи `..._id` — це рахунок збігів за ключем, стандартна ідіома. Колонка з
    іншим іменем при тому самому з'єднанні (`count(e.comm)`) і далі
    спрацьовує: те, що вона теж може бути технічно NOT NULL, текст не
    підтверджує, а ідіома підрахунку стосується саме ключа зв'язку.
    """
    aliases = _outer_join_aliases(s)
    for m in _COUNT_COL.finditer(s):
        alias, col = m.group(1), m.group(2)
        if alias and alias.lower() in aliases and _KEY_LIKE.match(col):
            continue
        return True
    return False


RULES: tuple[tuple[str, str, str, object], ...] = (
    ("eq_null", "Порівняння з NULL через =",
     "x = NULL ніколи не буває TRUE — навіть якщо значення справді NULL. "
     "Перевірка на порожнє значення пишеться як IS NULL / IS NOT NULL.",
     _null_comparison),
    ("not_in_null", "NULL у списку NOT IN",
     "x NOT IN (1, 2, NULL) розкривається в x <> 1 AND x <> 2 AND x <> NULL. "
     "Останній кон'юнкт ніколи не TRUE, тому WHERE не пропускає ЖОДНОГО рядка — "
     "навіть коли відповідь очевидно є. З IN такої пастки немає, бо там OR.",
     _not_in_null_literal),
    ("not_in_empty", "NOT IN (SELECT …) повернув 0 рядків",
     "Порожній результат при NOT IN із підзапитом — типова ознака того, що "
     "підзапит повернув хоча б один NULL. Це підозра, а не діагноз: "
     "порожня відповідь може бути й правильною.",
     _not_in_empty),
    ("having_no_group", "HAVING без GROUP BY",
     "HAVING без GROUP BY застосовується до всієї вибірки як до однієї групи. "
     "Найчастіше малося на увазі WHERE.",
     _having_without_group),
    ("cross_join", "З'єднання без умови",
     "Кілька таблиць перелічені через кому, а WHERE немає — це декартів "
     "добуток: кожен рядок з кожним.",
     _cross_join),
    ("count_column", "count(колонка) замість count(*)",
     "count(колонка) не рахує рядки, де ця колонка NULL. Якщо потрібна "
     "кількість рядків — count(*). Різниця виникає лише для колонки, що "
     "допускає NULL, тож це привід перепитати, а не готовий вирок.",
     _count_column),
)


def check(sql: str, rows: int | None = None) -> list[str]:
    """Коди правил, що спрацювали на одному запиті."""
    s = _clean(sql)
    return [code for code, _t, _e, fn in RULES if fn(s, rows)]


TITLES = {code: (title, expl) for code, title, expl, _ in RULES}
