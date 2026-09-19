"""Міра схожості форм SQL, версія 2 — за розвідкою (docs/research/05).

`sqlshape` (v1) — це Aligon 2014 у формі Σ wᵢ·Jaccard(компонентᵢ). Він
правильний (у бенчмарку Kul 2018 найкращий на студентських запитах), але має
три вади, названі в літературі, і на реальному журналі дає 49 % хибного
прийому на лабораторних спільної схеми (див. docs/research/05 §2–3):

1. **ваги.** `tables 0.45` суперечить консенсусу «селекція+зʼєднання ≫ group-by
   > проєкція; таблиці ігнорують». У спільній схемі таблиці тотожні.
2. **немає канонізації.** Kul: препроцесинг важливіший за вибір міри;
   найбільший виграш дає резолвінг псевдонімів.
3. **згортка «максимум по зразку» крихка.** Один загальний зразок підходить
   усім лабам однієї схеми.

v2 адресує (1) і (2): форма розкладається на компоненти Aligon
(`predicates`, `joins`, `projection`, `group_by`) з резолвінгом псевдонімів,
розцукренням `BETWEEN/IN`, класами літералів і транзитивним замиканням
рівностей у зʼєднаннях. Вирішення (3) — IDF по лабах і згортка «покриття +
влучність» — живе у `shapecheck_files` / `labs`, бо потребує набору лаб, а не
пари запитів. `similarity(a, b, idf=…)` приймає ваги ознак від того шару.

Модуль без залежностей — щоб перевірятися скриптом без бази (як v1).
`SELECT *` не розгортається за схемою: тут схеми немає. У проді цей крок
додається окремо (postgres/ — схема відома).
"""
from __future__ import annotations

import re

from .sqlshape import (  # noqa: F401 — свідоме перевикористання
    _KINDS,
    constructs,
    funcs,
    tables,
)

_COMMENT = re.compile(r"(?s)/\*.*?\*/|--[^\n]*")
_STRING = re.compile(r"'(?:[^']|'')*'")
_ID = r"[a-zA-Z_Ѐ-ӿ][a-zA-Z0-9_$Ѐ-ӿ]*"
_REF = re.compile(rf'(?:"{_ID}"|{_ID})(?:\.(?:"{_ID}"|\*|{_ID}))?')

_CLAUSE_END = (r"\b(?:where|group\s+by|order\s+by|having|limit|offset|window"
               r"|union|intersect|except|returning|fetch)\b")
_JOIN_SPLIT = re.compile(r"(?i)\b(?:natural\s+)?(?:(?:left|right|full)\s+)?"
                         r"(?:outer\s+|inner\s+|cross\s+)?join\b|,")
_BOOL_SPLIT = re.compile(r"(?i)\b(?:and|or)\b")
_OP = re.compile(r"(?i)\s*(<=|>=|<>|!=|=|<|>|\bis\s+not\b|\bis\b|\bnot\s+in\b"
                 r"|\bin\b|\blike\b|\bilike\b|\bbetween\b)\s*")
_KW_STOP = frozenset("""
and or not in is null true false between like ilike exists any all some as on
using select from where group by having order limit offset asc desc distinct
""".split())


def _clean(sql: str) -> str:
    return _STRING.sub(" ? ", _COMMENT.sub(" ", sql or ""))


def _bare(name: str) -> str:
    return name.strip().strip('"').split(".")[-1].lower()


def _region(s: str, start: str, end: str = _CLAUSE_END) -> str:
    m = re.search(rf"(?i)\b{start}\b(.*?)(?={end}|$)", s, re.S)
    return m.group(1) if m else ""


def _split_top(text: str, rx: re.Pattern) -> list[str]:
    """Розбиває по rx лише на глибині 0 дужок."""
    marked, depth = [], 0
    for ch in text:
        if ch == "(":
            depth += 1
            marked.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            marked.append(ch)
        elif depth > 0 and (ch.isspace() or ch == ","):
            marked.append("\0")          # ховаємо роздільники всередині дужок
        else:
            marked.append(ch)
    parts = []
    for piece in rx.split("".join(marked)):
        p = piece.replace("\0", " ").strip()
        if p:
            parts.append(p)
    return parts


def _alias_map(from_region: str) -> dict[str, str]:
    """`section a`, `public.orders AS o` → {a: section, o: orders, orders: orders}."""
    amap: dict[str, str] = {}
    for chunk in _JOIN_SPLIT.split(from_region):
        chunk = chunk.strip()
        # відрізати умову ON/USING
        chunk = re.split(r"(?i)\b(?:on|using)\b", chunk, maxsplit=1)[0].strip()
        if not chunk or chunk.startswith("("):
            continue
        toks = re.findall(rf'"{_ID}"|{_ID}|\.', chunk)
        toks = [t for t in toks if t != "."]
        if not toks:
            continue
        m = _REF.match(chunk)
        table = _bare(m.group(0)) if m else _bare(toks[0])
        if not table or table in _KW_STOP:
            continue
        amap[table] = table
        rest = [t for t in toks if _bare(t) != table and t.lower() != "as"]
        if rest:
            alias = rest[-1].strip('"').lower()
            if alias not in _KW_STOP:
                amap[alias] = table
    return amap


def _resolve(ref: str, amap: dict[str, str]) -> str:
    ref = ref.strip().strip('"').lower()
    if "." in ref:
        head, _, tail = ref.partition(".")
        head = head.strip('"')
        tail = tail.strip('"')
        return f"{amap.get(head, head)}.{tail}"
    return ref


def _is_ref(token: str) -> bool:
    token = token.strip()
    return bool(_REF.fullmatch(token)) and token.split(".")[0].lower() not in _KW_STOP


def _is_ref_start(expr: str) -> bool:
    """Вираз починається з ідентифікатора-посилання, а не з літерала/числа."""
    expr = expr.strip().lstrip("(").strip()
    m = _REF.match(expr)
    if not m or m.group(0).split(".")[0].lower() in _KW_STOP:
        return False
    return not re.match(r"^-?\d", expr) and not expr.startswith("?")


def _litclass(rhs: str) -> str:
    r = rhs.strip().lower()
    if r.startswith("("):
        inner = r.strip("() ")
        if "select" in inner:
            return ""            # підзапит — конструкція, не літерал
        r = inner.split(",")[0].strip()
    if r in ("?", "") or r.startswith("?"):
        return "str"
    if r in ("null",):
        return "null"
    if r in ("true", "false"):
        return "bool"
    if re.match(r"^-?\d", r):
        return "num"
    if re.match(r"^(current_date|current_timestamp|now\s*\(|date\s|timestamp\s)", r):
        return "date"
    return "expr"


def _predicates_joins(cond: str, amap: dict[str, str]):
    preds: set[str] = set()
    eq_pairs: list[frozenset[str]] = []
    other_joins: set[str] = set()
    for atom in _split_top(cond, _BOOL_SPLIT):
        atom = atom.strip().strip("()").strip()
        atom = re.sub(r"(?i)^not\s+", "", atom)
        m = _OP.search(atom)
        if not m:
            continue
        lhs, op, rhs = atom[:m.start()].strip(), m.group(1).lower().strip(), atom[m.end():].strip()
        op = re.sub(r"\s+", " ", op)
        # `100 < price` → `price > 100`: якщо зліва літерал, а справа ref — міняємо
        if not _is_ref_start(lhs) and _is_ref_start(rhs):
            lhs, rhs = rhs, lhs
            op = {"<": ">", ">": "<", "<=": ">=", ">=": "<="}.get(op, op)
        lm = _REF.search(lhs)
        if not lm:
            continue
        left = _resolve(lm.group(0), amap)
        if left.split(".")[-1] in _KW_STOP:
            continue
        if op == "between":
            preds.add(f"{left}|<=|num")
            continue
        if _is_ref(rhs) and _litclass(rhs) == "expr":
            right = _resolve(_REF.match(rhs).group(0), amap)
            if right.split(".")[-1] in _KW_STOP:
                continue
            if left == right:
                continue
            if op == "=":
                eq_pairs.append((left, right))
            else:
                other_joins.add("|".join(sorted((left, right))) + f"|{op}")
            continue
        cls = _litclass(rhs)
        if cls:
            preds.add(f"{left}|{op}|{cls}")
    # транзитивне замикання рівностей (A=B ∧ B=C ⇒ A=C)
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in eq_pairs:
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        parent[find(a)] = find(b)
    comps: dict[str, set[str]] = {}
    for node in parent:
        comps.setdefault(find(node), set()).add(node)
    joins = set(other_joins)
    for members in comps.values():
        ms = sorted(members)
        for i in range(len(ms)):
            for j in range(i + 1, len(ms)):
                joins.add(f"{ms[i]}|=|{ms[j]}")
    return preds, joins


def _columns(seg: str, amap: dict[str, str]) -> set[str]:
    seg = re.sub(r"(?i)\b(distinct|all)\b", " ", seg)
    seg = re.sub(r"(?i)\btop\s+\d+\b", " ", seg)
    out: set[str] = set()
    for item in _split_top(seg, re.compile(r",")):
        item = re.sub(r"(?i)\s+as\s+" + _ID + r"\s*$", "", item.strip())
        item = re.sub(r"\s+" + _ID + r"\s*$", "", item) if " " in item and "(" not in item else item
        if item.strip() == "*":
            out.add("*")
            continue
        for rm in _REF.finditer(item):
            ref = _resolve(rm.group(0), amap)
            tail = ref.split(".")[-1]
            if tail == "*":
                out.add(ref)
            elif tail not in _KW_STOP and not tail.isdigit():
                out.add(ref)
    return out


def shape(sql: str) -> dict:
    s = _clean(sql)
    kind = (s.strip().split() or [""])[0].lower()
    from_region = _region(s, "from")
    amap = _alias_map(from_region)

    on_conds = " and ".join(re.findall(r"(?i)\bon\b(.*?)(?=\bjoin\b|\bwhere\b|$)", from_region, re.S))
    where = _region(s, "where")
    having = _region(s, "having")
    p1, j1 = _predicates_joins(f"{where} and {having}", amap)
    p2, j2 = _predicates_joins(on_conds, amap)

    # Проєкція: усе до першого FROM, без першого слова (SELECT або його
    # опечатка — `SELCT`, `SLECT`; на такі запити перевірка й розрахована).
    fm = re.search(r"(?i)\bfrom\b", s)
    proj_seg = re.sub(r"^\s*\S+", "", s[:fm.start()]) if fm else ""
    proj = _columns(proj_seg, amap)
    gb = _columns(_region(s, "group\\s+by"), amap)

    # Канонізація конструкцій у дусі Kul (EXISTS/IN-стандартизація): підзапит,
    # звʼязаний з таблицею через IN/EXISTS, — це напівзʼєднання; і воно, і явний
    # JOIN означають «дві таблиці повʼязані» → спільний токен `table_link`, щоб
    # два еквівалентні переформулювання не розходилися до нуля.
    c = constructs(sql)
    if "subquery" in c and (c & {"in_list", "not_in", "exists"}):
        c.add("semijoin")
    if {"join", "outer_join", "cross_join", "semijoin"} & c:
        c.add("table_link")

    return {
        "kind": kind,
        "tables": tables(sql),
        "predicates": p1 | p2,
        "joins": j1 | j2,
        "projection": proj,
        "group_by": gb,
        "constructs": c,
        "funcs": funcs(sql),
    }


# Ваги за розвідкою (docs/research/05 §3.1). Порядок важливіший за значення
# (Aligon: Silhouette майже не залежить від ваг).
WEIGHTS_V2 = (
    ("predicates", 0.30), ("joins", 0.15), ("projection", 0.12),
    ("group_by", 0.08), ("constructs", 0.20), ("tables", 0.10), ("funcs", 0.05),
)
# Ваги v1 у тій самій формі — щоб shapecheck_files ганяв обидві через один код.
WEIGHTS_V1 = (("tables", 0.45), ("constructs", 0.30), ("columns", 0.15), ("funcs", 0.10))


def wjaccard(a: set, b: set, idf: dict[str, float] | None = None) -> float | None:
    if not a and not b:
        return None
    if idf is None:
        return len(a & b) / len(a | b)
    inter = sum(idf.get(f, 1.0) for f in a & b)
    union = sum(idf.get(f, 1.0) for f in a | b)
    return inter / union if union else 0.0


def similarity(a: dict, b: dict, weights=WEIGHTS_V2,
               idf: dict[str, dict[str, float]] | None = None) -> float:
    """0..1. Порожні складові не голосують, ваги перенормовуються.

    `idf` — {компонент: {ознака: вага}} від шару, що знає набір лаб
    (docs/research/05 §3.3). None ⇒ звичайний Жаккар, як v1.
    """
    total = weight = 0.0
    for key, w in weights:
        j = wjaccard(a[key], b[key], (idf or {}).get(key))
        if j is None:
            continue
        total += w * j
        weight += w
    if weight == 0:
        return 0.0
    score = total / weight
    if a["kind"] != b["kind"] and a["kind"] in _KINDS and b["kind"] in _KINDS:
        score *= 0.6
    return round(score, 4)


def collect_df(pools: dict[int, list[dict]], weights=WEIGHTS_V2) -> dict[str, dict[str, float]]:
    """df(f) = # лаб, чий хоч один зразок містить ознаку f  →  idf по компонентах.

    idf(f) = ln((m+1)/(1+df(f))) + 1   (docs/research/05 §3.3)
    """
    import math

    m = len(pools)
    idf: dict[str, dict[str, float]] = {}
    for key, _w in weights:
        df: dict[str, int] = {}
        for shs in pools.values():
            seen: set[str] = set()
            for sh in shs:
                seen |= sh.get(key, set())
            for f in seen:
                df[f] = df.get(f, 0) + 1
        idf[key] = {f: math.log((m + 1) / (1 + c)) + 1 for f, c in df.items()}
    return idf
