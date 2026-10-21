#!/usr/bin/env python
"""symbol_exists_at_ref.py — release truth-check probe: is a name bound at module level in a file at a git ref?

Reads ``<path>`` as it was at ``<ref>`` (``git show <ref>:<path>``), parses it, and looks for a module-level binding of
``<symbol>``: a function or class definition, an assignment or annotated assignment, an import (aliased or not), or a
literal ``parser.add_argument("<symbol>")`` call statement. Only ``tree.body`` is inspected, so a conditional
definition, a decorator-registered name, a computed or dynamic binding (``globals()[...]``), a class method, a
docstring or a comment never counts. The probe therefore proves presence of a candidate only — absence is never
established by it.

``<path>`` must be a repository-relative ``.py`` path (no leading ``/``, no ``..`` segment). The command runs ``git``
in the current directory, so call it from the repository root. Arguments are positional and read verbatim, so a
``<symbol>`` such as ``--legacy`` (a parser flag) is accepted as a name, not parsed as an option.

consumers: plugins/cc_oss/skills/release/modes/classify-truth-check.md (Truth check),
plugins/cc_oss/skills/release/templates/gather-prompt.md (truth check phase)

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/symbol_exists_at_ref.py" "$REF" "$SYMBOL" "$PUBLIC_PATH"
    python "${CLAUDE_PLUGIN_ROOT}/bin/symbol_exists_at_ref.py" --help

``--help`` (or ``-h``) as the only argument prints the usage line and exits 0; with three arguments it is a symbol name.

Exit codes:
    0 — candidate module-level binding found (or usage printed for a lone ``--help``)
    2 — inconclusive: no matching binding, an invalid path, a ``git`` failure, an unparsable file, or wrong arguments
"""

from __future__ import annotations

import ast
import subprocess
import sys
from typing import Final

#: Exit status when a candidate binding is present.
EXIT_FOUND: Final = 0
#: Exit status for everything else; the caller treats it as "inconclusive", never as proof of absence.
EXIT_INCONCLUSIVE: Final = 2
#: Number of positional arguments: ``<ref> <symbol> <path>``.
ARG_COUNT: Final = 3
#: One-line usage text, printed for a lone ``--help`` and on a wrong argument count.
USAGE: Final = "usage: symbol_exists_at_ref.py <ref> <symbol> <path>"


def is_valid_path(path: str) -> bool:
    """Return whether ``path`` is a repository-relative ``.py`` path without parent segments.

    >>> is_valid_path("pkg/api.py")
    True
    >>> is_valid_path("/etc/api.py"), is_valid_path("../api.py"), is_valid_path("pkg/api.txt"), is_valid_path("")
    (False, False, False, False)
    """
    return path.endswith(".py") and not path.startswith("/") and ".." not in path.split("/")


def declares_symbol(tree: ast.Module, symbol: str) -> bool:
    """Return whether ``symbol`` is bound by a module-level declaration in ``tree``.

    Counts definitions, assignments, imports and literal ``add_argument`` call statements (so ``--flag`` names match).

    >>> declares_symbol(ast.parse("def Legacy(): pass"), "Legacy")
    True
    >>> declares_symbol(ast.parse("Legacy = impl"), "Legacy")
    True
    >>> declares_symbol(ast.parse("import package.impl as Legacy"), "Legacy")
    True
    >>> declares_symbol(ast.parse("p.add_argument('--legacy')"), "--legacy")
    True
    >>> declares_symbol(ast.parse("'''def Legacy(): pass'''"), "Legacy")
    False
    >>> declares_symbol(ast.parse("class Hidden:\\n    def Legacy(self): pass"), "Legacy")
    False
    """
    declarations = tree.body
    named = any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == symbol
        for node in declarations
    )
    assigned = any(
        (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == symbol for target in node.targets)
        )
        or (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == symbol)
        or (
            isinstance(node, ast.Import)
            and any((alias.asname or alias.name.split(".", 1)[0]) == symbol for alias in node.names)
        )
        or (isinstance(node, ast.ImportFrom) and any((alias.asname or alias.name) == symbol for alias in node.names))
        for node in declarations
    )
    flag = any(
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "add_argument"
        and any(isinstance(arg, ast.Constant) and arg.value == symbol for arg in node.value.args)
        for node in declarations
    )
    return named or assigned or flag


def probe(ref: str, symbol: str, path: str) -> int:
    """Run the probe and return the exit status; diagnostics go to stderr.

    The file is parsed from raw bytes, never decoded first, so a PEP 263 coding cookie (for example latin-1) is honoured
    by the parser instead of failing here.

    Args:
        ref: Git ref to read the file at (tag, branch, ``HEAD`` or SHA).
        symbol: Name or literal flag to look for.
        path: Repository-relative ``.py`` path of the claimed public module.

    Returns:
        ``EXIT_FOUND`` when a candidate binding exists, otherwise ``EXIT_INCONCLUSIVE``.
    """
    if not is_valid_path(path):
        return EXIT_INCONCLUSIVE
    try:
        # argv list, no shell; git resolved via PATH on purpose, ref/path are data after `show`
        source = subprocess.run(["git", "show", f"{ref}:{path}"], capture_output=True, check=False)  # noqa: S603, S607
    except OSError as error:
        print(f"Cannot run git: {error}", file=sys.stderr)
        return EXIT_INCONCLUSIVE
    if source.returncode:
        print(source.stderr.decode("utf-8", errors="replace"), file=sys.stderr)
        return EXIT_INCONCLUSIVE
    try:
        tree = ast.parse(source.stdout)
    except (SyntaxError, UnicodeError, ValueError) as error:
        print(f"Cannot parse {ref}:{path}: {error}", file=sys.stderr)
        return EXIT_INCONCLUSIVE
    return EXIT_FOUND if declares_symbol(tree, symbol) else EXIT_INCONCLUSIVE


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``symbol_exists_at_ref.py <ref> <symbol> <path>``.

    Args:
        argv: Argument list override for testing. Defaults to ``sys.argv[1:]``.

    Returns:
        ``EXIT_FOUND`` or ``EXIT_INCONCLUSIVE``; wrong arguments are inconclusive too.

    >>> main(["only-one-argument"])
    2
    """
    args = sys.argv[1:] if argv is None else argv
    if args in (["-h"], ["--help"]):  # a lone help flag only; with three arguments `--help` is a symbol name
        print(USAGE)
        sys.exit(EXIT_FOUND)
    if len(args) != ARG_COUNT:
        print(USAGE, file=sys.stderr)
        return EXIT_INCONCLUSIVE
    ref, symbol, path = args
    return probe(ref, symbol, path)


if __name__ == "__main__":
    sys.exit(main())
