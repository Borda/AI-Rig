#!/usr/bin/env python3
"""Flag test assertions that mix a native-separator path with a ``/``-joined one.

A path rendered into text with ``f"{tmp_path / 'm.py'}"``, ``str(tmp_path / "m.py")``, ``os.path.join(...)`` or
``os.sep`` is ``/``-joined on Linux and macOS but ``\\``-joined on Windows. When the same assertion also holds a
``/``-joined path (a literal such as ``./pkg/a.py`` or a ``.as_posix()`` call), the two forms agree on every POSIX
runner and disagree only on Windows CI, so the assertion passes locally and fails there.

An assertion that renders only native paths is consistent on every OS and is left alone, as is any comparison of
``Path`` objects. When the mixture is deliberate, add ``# path-sep: native`` on the ``assert`` line.

Usage: ``check_test_path_separators.py FILE...`` (pre-commit passes the staged test files). Exit 1 on any finding.
"""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Iterator
from pathlib import Path

#: Comment that marks an assertion whose mixed separators are deliberate.
OPT_OUT = "# path-sep: native"
#: A relative path joined with ``/`` and ending in a file name (``./pkg/x.py``, ``pkg/a.py``), never a URL.
_POSIX_PATH = re.compile(r"(?<![:/\w])(?:\.{1,2}/|[\w.-]+/)(?:[\w.-]+/)*[\w.-]+\.\w+")


def _is_path_join(node: ast.AST) -> bool:
    """Return whether ``node`` builds a path with a native separator: ``a / b``, ``os.path.join(...)``, ``os.sep``.

    Examples:
        >>> _is_path_join(ast.parse("tmp_path / 'x'", mode="eval").body)
        True
        >>> _is_path_join(ast.parse("a + b", mode="eval").body)
        False
    """
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return True
    return ast.unparse(node) == "os.sep" or (isinstance(node, ast.Call) and ast.unparse(node.func) == "os.path.join")


def _rendered_paths(node: ast.AST) -> Iterator[ast.AST]:
    """Yield every native-separator path expression that ``node`` renders into a string.

    Covers f-string placeholders, ``str(...)`` calls and ``os.sep`` / ``os.path.join`` anywhere inside ``node``;
    a bare ``Path`` operand is not a string and is skipped.

    Examples:
        >>> [ast.unparse(n) for n in _rendered_paths(ast.parse('f"{tmp_path / \\'m\\'}:1"', mode="eval").body)]
        ["tmp_path / 'm'"]
        >>> list(_rendered_paths(ast.parse("tmp_path / 'm'", mode="eval").body))
        []
    """
    for child in ast.walk(node):
        if isinstance(child, ast.FormattedValue) and _is_path_join(child.value):
            yield child.value
        elif isinstance(child, ast.Call) and ast.unparse(child.func) == "str" and child.args:
            if _is_path_join(child.args[0]):
                yield child.args[0]
        elif _is_path_join(child) and not isinstance(child, ast.BinOp):
            yield child


def _has_posix_form(node: ast.AST, ignored: set[int]) -> bool:
    """Return whether ``node`` holds a ``/``-joined path outside ``ignored`` nodes: a literal or ``.as_posix()``.

    ``ignored`` holds the ids of nodes inside the rendered paths themselves, so the ``'bin/x.py'`` operand of
    ``str(out / 'bin/x.py')`` never counts as the other side of a mixture.

    Examples:
        >>> _has_posix_form(ast.parse("['./pkg/a.py', x]", mode="eval").body, set())
        True
        >>> _has_posix_form(ast.parse("'no slash here'", mode="eval").body, set())
        False
    """
    for child in ast.walk(node):
        if id(child) in ignored:
            continue
        if isinstance(child, ast.Constant) and isinstance(child.value, str) and _POSIX_PATH.search(child.value):
            return True
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and child.func.attr == "as_posix":
            return True
    return False


def check_source(source: str, filename: str) -> list[str]:
    """Return one ``file:line: message`` per native-rendered path in an assertion that also holds a ``/`` path.

    Examples:
        >>> check_source('assert out == [f"{tmp_path / \\'m.py\\'}", "./pkg/a.py"]\\n', "t.py")[0].split(": ")[0]
        't.py:1'
        >>> check_source('assert out == f"{tmp_path / \\'m.py\\'}:1:Base"\\n', "t.py")
        []
        >>> check_source("assert run(str(out / 'bin/x.py')) == 0\\n", "t.py")
        []
        >>> check_source('assert out == [f"{t / \\'m\\'}", "./a.py"]  # path-sep: native\\n', "t.py")
        []
    """
    lines = source.splitlines()
    findings = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if not isinstance(node, ast.Assert) or OPT_OUT in lines[node.lineno - 1]:
            continue
        rendered = list(_rendered_paths(node.test))
        ignored = {id(part) for path_expr in rendered for part in ast.walk(path_expr)}
        if not rendered or not _has_posix_form(node.test, ignored):
            continue
        findings.extend(
            f"{filename}:{path_expr.lineno}: `{ast.unparse(path_expr)}` renders native separators (\\ on Windows) "
            f"beside a /-joined path in the same assertion; use .as_posix(), or mark `{OPT_OUT}`"
            for path_expr in rendered
        )
    return findings


def main(argv: list[str] | None = None) -> int:
    """Check every given file and print findings; exit 1 when any exist."""
    findings = []
    for name in sys.argv[1:] if argv is None else argv:
        findings.extend(check_source(Path(name).read_text(encoding="utf-8"), name))
    for finding in findings:
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
