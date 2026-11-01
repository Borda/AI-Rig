#!/usr/bin/env python3
"""Check that Codex Rig's pure agent-shim kernels import and call nothing that touches files or processes.

The approval, journal, plan and lifecycle kernels only derive and validate values from evidence handed to them; the
sibling modules that observe the host and write files own all I/O. A kernel that gained an ``os`` import, an ``open``
call or a ``.write_text`` attribute would still pass its behaviour tests while losing the property that makes those
tests sufficient, so the property is checked here by reading the parsed source, as a hook and not as a pytest test.
Ruff's banned-api rule cannot replace it: it bans an API everywhere, while only these modules must stay pure.

A kernel is flagged for any of:

- importing ``os``, ``pathlib``, ``shutil``, ``subprocess`` or ``tempfile``
- calling ``open``, ``exec``, ``eval``, ``compile``, ``input`` or ``__import__``
- touching a write-like attribute: ``open``, ``write``, ``write_bytes``, ``write_text``, ``replace``, ``rename`` or
  ``unlink``
- containing a ``del`` statement

Usage: ``check_pure_kernels.py [--root DIR] [FILE...]``. With no ``FILE`` the declared kernel modules under ``--root``
(default: the repository root) are checked. Exit 1 on any finding; each line is ``path:line: message``.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

#: Pure kernel modules, relative to the repository root. Add a module here to put it under the check.
KERNELS = (
    "plugins/codex-rig/scripts/_agent_shim_approval.py",
    "plugins/codex-rig/scripts/_agent_shim_journal.py",
    "plugins/codex-rig/scripts/_agent_shim_plan.py",
    "plugins/codex-rig/scripts/_agent_shim_lifecycle.py",
)
BANNED_IMPORTS = frozenset({"os", "pathlib", "shutil", "subprocess", "tempfile"})
BANNED_CALLS = frozenset({"open", "exec", "eval", "compile", "input", "__import__"})
BANNED_ATTRIBUTES = frozenset({"open", "write", "write_bytes", "write_text", "replace", "rename", "unlink"})


def _imported_modules(node: ast.AST) -> list[str]:
    """Return the top-level module names an import statement brings in; other nodes bring in none."""
    if isinstance(node, ast.Import):
        return [alias.name.split(".")[0] for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        return [(node.module or "").split(".")[0]]
    return []


def check_source(source: str, filename: str) -> list[str]:
    """Return one ``file:line: message`` per impure construct in a kernel's source.

    Examples:
        >>> check_source("import os\\n", "k.py")
        ['k.py:1: imports `os`; a pure kernel must not touch files or processes']
        >>> check_source("def f(x):\\n    return x + 1\\n", "k.py")
        []
    """
    findings = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        where = f"{filename}:{getattr(node, 'lineno', 0)}"
        findings.extend(
            f"{where}: imports `{name}`; a pure kernel must not touch files or processes"
            for name in _imported_modules(node)
            if name in BANNED_IMPORTS
        )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in BANNED_CALLS:
            findings.append(
                f"{where}: calls `{node.func.id}()`; move the I/O or dynamic execution to a non-kernel module"
            )
        if isinstance(node, ast.Attribute) and node.attr in BANNED_ATTRIBUTES:
            findings.append(f"{where}: uses write-like attribute `.{node.attr}`; a pure kernel must not write")
        if isinstance(node, ast.Delete):
            findings.append(f"{where}: `del` statement; a pure kernel must not delete or mutate state")
    return findings


def main(argv: list[str] | None = None) -> int:
    """Check the given files, or the declared kernels under ``--root``, and print every finding."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1], help="repository root")
    parser.add_argument("files", nargs="*", type=Path, help="kernel files to check instead of the declared list")
    args = parser.parse_args(argv)
    findings = []
    for path in args.files or [args.root / kernel for kernel in KERNELS]:
        if not path.is_file():
            findings.append(f"{path}: kernel file is missing; fix the path in KERNELS in check_pure_kernels.py")
            continue
        findings.extend(check_source(path.read_text(encoding="utf-8"), path.as_posix()))
    for finding in findings:
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
