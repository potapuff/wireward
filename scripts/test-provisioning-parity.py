#!/usr/bin/env python3
"""Звіряє два описи провізіонінгу, які мусять лишатися однаковими.

`postgres/sql/provision_student.sql` — канонічна специфікація для DBA і для
`scripts/test-provisioning.sh`. `front/app/provisioning.py` — та сама
послідовність на psycopg, бо psql-плейсхолдери (:"role") з Python не виконати.

Дублювання тут навмисне і незворотне: CREATE DATABASE не виконується всередині
транзакції, а отже і всередині функції PL/pgSQL, тож звести обидва описи в один
неможливо. Лишається єдине — не дати їм розійтися мовчки.

Перевірка СТАТИЧНА: кластер не потрібен, psycopg не імпортується. Python
розбирається через ast, а не регулярками, тому перейменування змінної тест не
ламає, а зміна порядку чи параметра — ламає, і саме цього ми хочемо.

Відомі й свідомі розбіжності перелічені в ALLOWED_EXTRA нижче з поясненням.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SQL_FILE = ROOT / "postgres" / "sql" / "provision_student.sql"
PY_FILE = ROOT / "front" / "app" / "provisioning.py"

# Кроки, яких у SQL-шаблоні немає навмисно. Кожен — із причиною: мовчазного
# «ну тут просто інакше» тут бути не може.
ALLOWED_EXTRA = {
    # Пароль виводиться з master_secret. Після його ротації роль треба
    # перепідписати; шаблон для DBA описує створення з нуля, де цієї гілки
    # не існує.
    "alter role <role> password <pw>",
}


def norm(stmt: str) -> str:
    """Канонічний вигляд оператора: без коментарів, регістру і зайвих пробілів.

    Плейсхолдери обох мов зводяться до спільних <role>/<db>/<ts>/<pw>, а лапки
    навколо значень знімаються: :'ts' у psql і sql.Literal(tablespace) у psycopg
    дають той самий оператор, і тест не має бачити тут різниці.
    """
    s = re.sub(r"--[^\n]*", " ", stmt)
    s = s.replace('"', "").replace("'", "")
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def from_sql() -> list[str]:
    text = SQL_FILE.read_text(encoding="utf-8")
    text = re.sub(r"--[^\n]*", "", text)
    # Мета-команди psql (\if / \else / \endif / \warn) і рядки-зонди, що
    # закінчуються на \gset, — це КЕРУВАННЯ ПОТОКОМ, а не крок провізіонінгу.
    # У provisioning.py їм відповідає звичайний `if`, тіло якого extractor уже
    # розгортає як канонічну гілку. Якби ми лишили їх тут, умовний GRANT
    # CONNECT для obs_maintenance читався б як зайвий крок, і два однакові за
    # змістом описи розійшлися б на порожньому місці.
    text = "\n".join(ln for ln in text.splitlines()
                     if not ln.lstrip().startswith("\\")
                     and not ln.rstrip().endswith("\\gset"))
    text = (text.replace(':"role"', "<role>").replace(':"db"', "<db>")
                .replace(':"ts"', "<ts>").replace(":'ts'", "<ts>")
                .replace(":'pw'", "<pw>"))
    return [norm(s) for s in text.split(";") if norm(s)]


class PyExtractor(ast.NodeVisitor):
    """Витягає послідовність операторів із тіла create_database."""

    def __init__(self) -> None:
        self.steps: list[str] = []

    # --- дістати текст оператора з одного виразу await conn.execute(...) ----
    @staticmethod
    def _template(call: ast.Call) -> str | None:
        arg = call.args[0] if call.args else None
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
        # sql.SQL("...").format(...)
        if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Attribute) \
                and arg.func.attr == "format":
            inner = arg.func.value
            if isinstance(inner, ast.Call) and inner.args \
                    and isinstance(inner.args[0], ast.Constant):
                return inner.args[0].value
        return None

    @staticmethod
    def _subst(tpl: str, extra: dict[str, str] | None = None) -> str:
        names = {"r": "<role>", "d": "<db>", "t": "<ts>", "pw": "<pw>"}
        names.update(extra or {})
        for k, v in names.items():
            tpl = tpl.replace("{" + k + "}", v)
        return tpl

    @staticmethod
    def _value(node: ast.expr) -> str:
        """Літерал — сам по собі; змінна `tablespace` — це <ts>."""
        if isinstance(node, ast.Constant):
            return str(node.value)
        if isinstance(node, ast.Name) and node.id == "tablespace":
            return "<ts>"
        raise SystemExit(f"невідоме значення параметра в циклі ALTER ROLE: {ast.dump(node)}")

    def _execute_call(self, node: ast.AST) -> ast.Call | None:
        if isinstance(node, ast.Expr):
            node = node.value
        if isinstance(node, ast.Await):
            node = node.value
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "execute":
            return node
        return None

    def walk(self, body: list[ast.stmt]) -> None:
        for st in body:
            if isinstance(st, ast.If):
                # Канонічний шлях — створення з нуля, тобто гілка «ролі немає».
                self.walk(st.body)
                for sub in st.orelse:
                    call = self._execute_call(sub)
                    if call and (tpl := self._template(call)):
                        self.steps.append("EXTRA:" + norm(self._subst(tpl)))
                continue
            if isinstance(st, ast.For):
                # Цикл по літеральному переліку параметрів розгортається.
                call = None
                for sub in ast.walk(st):
                    call = self._execute_call(sub) or call
                tpl = self._template(call) if call else None
                if tpl and isinstance(st.iter, (ast.Tuple, ast.List)):
                    for item in st.iter.elts:
                        p, v = (self._value(e) for e in item.elts)
                        self.steps.append(norm(self._subst(tpl, {"p": p, "v": v})))
                continue
            if isinstance(st, (ast.AsyncWith, ast.With, ast.Try)):
                self.walk(st.body)
                continue
            call = self._execute_call(st)
            if call and (tpl := self._template(call)):
                self.steps.append(norm(self._subst(tpl)))


def from_py() -> tuple[list[str], list[str]]:
    tree = ast.parse(PY_FILE.read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
               and n.name == "create_database"), None)
    if fn is None:
        sys.exit("у provisioning.py немає create_database — перевірка застаріла")
    ex = PyExtractor()
    ex.walk(fn.body)
    steps = [s for s in ex.steps if not s.startswith("EXTRA:")]
    extra = [s[len("EXTRA:"):] for s in ex.steps if s.startswith("EXTRA:")]
    return steps, extra


def main() -> int:
    # У циклі ALTER ROLE значення параметра в Python — змінна `tablespace`,
    # у SQL — :'ts'; norm() уже звів обидва до <ts>.
    sql_steps = from_sql()
    py_steps, py_extra = from_py()

    bad = False
    for s in py_extra:
        if s not in ALLOWED_EXTRA:
            print(f"provisioning.py має крок, якого немає в SQL і в переліку винятків:\n  {s}")
            bad = True

    if sql_steps != py_steps:
        bad = True
        print("послідовності розійшлися:\n")
        n = max(len(sql_steps), len(py_steps))
        for i in range(n):
            a = sql_steps[i] if i < len(sql_steps) else "—"
            b = py_steps[i] if i < len(py_steps) else "—"
            mark = "  " if a == b else "≠ "
            print(f"{mark}{i + 1}. sql: {a}")
            print(f"{mark}   py:  {b}")
        print("\nОбидва описи мусять збігатися крок у крок. Якщо розбіжність")
        print("свідома — додайте її в ALLOWED_EXTRA з поясненням ЧОМУ.")

    if bad:
        return 1
    print(f"провізіонінг збігається: {len(sql_steps)} кроків, "
          f"винятків {len(py_extra)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
