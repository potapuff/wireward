"""Форма запиту і схожість форм — основа привʼязки до лабораторної (З4).

Задача, поставлена викладачем: сказати, що ця робота студента стосується
лабораторної №N, при тому що
  * лабораторна **не обмежена часом** — різні студенти приходять коли завгодно;
  * запити студентів **варіативні** — точного збігу зі зразком не буде майже
    ніколи;
  * робота йде **не поспіль** — студент повертається до лаби через тиждень.

Отже привʼязати не можна ні за часом, ні за точним текстом. Лишається зміст.

## Що таке форма

`sql_fingerprint` для цього замало: він прибирає літерали, але лишає порядок
колонок, псевдоніми і форматування, тож два розвʼязки однієї задачі дають різні
відбитки. Форма — це чотири МНОЖИНИ, у яких порядок і оформлення вже не
беруть участі:

* `tables`     — джерела з `FROM`/`JOIN`, без схеми й псевдонімів;
* `constructs` — які засоби мови вжито: зʼєднання, підзапит, групування,
                 агрегат, `HAVING`, вікно, `UNION`, `DISTINCT`, `EXISTS`,
                 `IN`/`NOT IN`, `ORDER BY`, CTE, зовнішнє зʼєднання;
* `columns`    — ідентифікатори, що не є ні таблицями, ні ключовими словами;
* `funcs`      — імена викликаних функцій.

## Чому саме множини і нечіткий перетин

Викладач сформулював це точно: «нормалізація того, що писав студент, і великий
перетин з очікуваним — хоч і не точний». Зважена міра Жаккара по цих чотирьох
множинах і є той перетин. Вага таблиць найбільша: розвʼязки однієї задачі
відрізняються синтаксисом, але майже завжди читають ті самі таблиці.

Модуль без залежностей — щоб перевірятися скриптом без бази.
"""
from __future__ import annotations

import re

_COMMENT = re.compile(r"(?s)/\*.*?\*/|--[^\n]*")
_STRING = re.compile(r"'(?:[^']|'')*'")
_IDENT = re.compile(r"[a-zA-Z_Ѐ-ӿ][a-zA-Z0-9_$Ѐ-ӿ]*")
# Джерело може бути з іменем схеми: public.orders. Псевдонім, що йде далі,
# у форму не входить — саме він і відрізняє два записи одного розвʼязку.
_QUALIFIED = re.compile(r'(?:"[^"]+"|[a-zA-Z_Ѐ-ӿ][a-zA-Z0-9_$Ѐ-ӿ]*)'
                        r'(?:\.(?:"[^"]+"|[a-zA-Z_Ѐ-ӿ][a-zA-Z0-9_$Ѐ-ӿ]*))?')

# Джерела: усе між FROM/JOIN і наступним ключовим словом.
_SOURCES = re.compile(r"(?is)\b(?:from|join)\b(.*?)(?=\b(?:where|group|order|having|limit"
                      r"|offset|union|intersect|except|window|returning|on|using|join"
                      r"|left|right|full|inner|cross|natural)\b|$)")

_CONSTRUCTS: tuple[tuple[str, re.Pattern], ...] = (
    ("join",        re.compile(r"(?i)\bjoin\b")),
    ("outer_join",  re.compile(r"(?i)\b(?:left|right|full)\s+(?:outer\s+)?join\b")),
    ("cross_join",  re.compile(r"(?i)\bcross\s+join\b")),
    ("subquery",    re.compile(r"(?is)\(\s*select\b")),
    ("cte",         re.compile(r"(?i)\bwith\b\s+[a-z_Ѐ-ӿ][\w$]*\s+as\s*\(")),
    ("group_by",    re.compile(r"(?i)\bgroup\s+by\b")),
    ("having",      re.compile(r"(?i)\bhaving\b")),
    ("order_by",    re.compile(r"(?i)\border\s+by\b")),
    ("distinct",    re.compile(r"(?i)\bdistinct\b")),
    ("union",       re.compile(r"(?i)\b(?:union|intersect|except)\b")),
    ("window",      re.compile(r"(?i)\bover\s*\(")),
    ("exists",      re.compile(r"(?i)\bexists\s*\(")),
    ("in_list",     re.compile(r"(?i)\bin\s*\(")),
    ("not_in",      re.compile(r"(?i)\bnot\s+in\b")),
    ("aggregate",   re.compile(r"(?i)\b(?:count|sum|avg|min|max|string_agg|array_agg)\s*\(")),
    ("case",        re.compile(r"(?i)\bcase\b")),
    ("limit",       re.compile(r"(?i)\blimit\b")),
    ("left_semi",   re.compile(r"(?i)\bis\s+null\b")),
)

# Назви конструкцій для екрана. Викладач бачить їх у формі заборон.
CONSTRUCT_TITLES = {
    "join": "зʼєднання (JOIN)", "outer_join": "зовнішнє зʼєднання",
    "cross_join": "зʼєднання без умови", "subquery": "підзапит",
    "cte": "CTE (WITH)", "group_by": "групування", "having": "HAVING",
    "order_by": "сортування", "distinct": "DISTINCT", "union": "UNION/INTERSECT/EXCEPT",
    "window": "віконна функція", "exists": "EXISTS", "in_list": "IN (…)",
    "not_in": "NOT IN", "aggregate": "агрегатна функція", "case": "CASE",
    "limit": "LIMIT", "left_semi": "перевірка на NULL",
}

# Слова, які не є ні таблицею, ні колонкою. Перелік свідомо неповний: зайве
# слово в множині колонок лише трохи розмиває міру, а не ламає її.
_KEYWORDS = frozenset("""
select from where group by having order limit offset join left right full inner
outer cross natural on using union intersect except with as and or not in exists
is null true false case when then else end distinct all any some between like
ilike similar to asc desc insert into values update set delete returning count
sum avg min max coalesce nullif cast text int integer numeric date timestamp
boolean varchar char now current_date current_timestamp interval extract lateral
over partition row rows range unbounded preceding following current window
""".split())


def _clean(sql: str) -> str:
    return _STRING.sub(" ? ", _COMMENT.sub(" ", sql or ""))


def _bare(name: str) -> str:
    """`public.orders o` → `orders`; лапки й схема прибираються."""
    name = name.strip().strip('"')
    return name.split(".")[-1].lower()


def tables(sql: str) -> set[str]:
    found: set[str] = set()
    for seg in _SOURCES.findall(_clean(sql)):
        for part in seg.split(","):
            part = part.strip()
            if not part or part.startswith("("):
                continue          # підзапит замість джерела — імені немає
            m = _QUALIFIED.match(part)
            if m:
                found.add(_bare(m.group(0)))
    return {t for t in found if t and t not in _KEYWORDS}


def constructs(sql: str) -> set[str]:
    s = _clean(sql)
    return {name for name, rx in _CONSTRUCTS if rx.search(s)}


def funcs(sql: str) -> set[str]:
    s = _clean(sql)
    return {m.group(1).lower() for m in re.finditer(r"([a-zA-Z_][\w$]*)\s*\(", s)
            if m.group(1).lower() not in ("if",)}


def shape(sql: str) -> dict:
    s = _clean(sql)
    t = tables(sql)
    f = funcs(sql)
    cols = {w.lower() for w in _IDENT.findall(s)}
    cols -= _KEYWORDS | t | f
    first = (s.strip().split() or [""])[0].lower()
    return {"kind": first, "tables": t, "constructs": constructs(sql),
            "columns": cols, "funcs": f}


def _jaccard(a: set, b: set) -> float | None:
    """None, якщо обидві множини порожні — тоді ця складова не голосує."""
    if not a and not b:
        return None
    return len(a & b) / len(a | b)


# Ваги. Таблиці важать найбільше: розвʼязки однієї задачі відрізняються
# синтаксисом, але майже завжди читають ті самі таблиці. Колонки — слабкий
# сигнал (псевдоніми, різні набори полів), функції — ще слабший.
_KINDS = frozenset("select insert update delete with table values create alter "
                   "drop truncate copy begin commit rollback grant revoke".split())

_WEIGHTS = (("tables", 0.45), ("constructs", 0.30), ("columns", 0.15), ("funcs", 0.10))


def similarity(a: dict, b: dict) -> float:
    """0..1. Порожні складові виключаються, ваги перенормовуються."""
    total = 0.0
    weight = 0.0
    for key, w in _WEIGHTS:
        j = _jaccard(a[key], b[key])
        if j is None:
            continue
        total += w * j
        weight += w
    if weight == 0:
        return 0.0
    score = total / weight
    # Різний рід оператора — та сама задача навряд чи. Не нуль, бо студент
    # міг створити тимчасову таблицю замість вибірки.
    #
    # Знижка НЕ застосовується, якщо перше слово взагалі не є оператором:
    # `SELCT * FROM orders` — це та сама робота над лабою, лише з опечаткою,
    # і саме такі запити викладачеві потрібні найбільше. Карати їх удвічі за
    # те, що студент помилився, означало б ховати найцікавіше.
    if a["kind"] != b["kind"] and a["kind"] in _KINDS and b["kind"] in _KINDS:
        score *= 0.6
    return round(score, 4)


def best_match(student_sql: str, expectations: list[tuple[int, str]]) -> tuple[int | None, float]:
    """Найкраща з очікуваних форм для одного запиту студента."""
    s = shape(student_sql)
    best_id, best = None, 0.0
    for eid, sample in expectations:
        sc = similarity(s, shape(sample))
        if sc > best:
            best_id, best = eid, sc
    return best_id, best
