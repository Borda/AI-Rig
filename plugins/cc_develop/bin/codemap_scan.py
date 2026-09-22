#!/usr/bin/env python
"""Structural codemap scan: derive affected modules and emit rdeps + optional coupled output.

Usage:
    codemap_scan.py --source=find --target <path> [--limit N]
    codemap_scan.py --source=diff [--limit N]

Sources:
    find — enumerate ``.py`` files under ``<path>``, derive module names (strip ``./``,
        ``src/``, ``.py``; replace ``/`` → ``.``).
    diff — derive modules from ``git diff HEAD --name-only``, including package initializers.

Output:
    Concatenated ``codemap-py query`` JSON blocks per module on stdout; ``coupled --top N`` appended
    for find mode.

Exit codes:
    0 — success (including no-results; missing ``codemap-py query`` binary; missing index file).
    1 — missing prerequisite (required CLI arg).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Iterable
from enum import Enum
from pathlib import Path


class ScanSource(str, Enum):
    """Where the affected-module list is derived from — the closed set of ``--source`` values.

    Subclasses ``str`` (not ``enum.StrEnum``) because ``requires-python`` is ``>=3.10``.
    The mixin keeps ``ScanSource.FIND == "find"`` true, so the plain string argparse
    hands back still compares equal.

    Examples:
        >>> ScanSource("diff").value
        'diff'
        >>> ScanSource.FIND == "find"
        True
    """

    FIND = "find"
    DIFF = "diff"


def derive_module_from_path(path: str) -> str:
    """Convert a Python file path to its module dotted name.

    Normalize separators, strip leading ``./`` and ``src/`` and the ``.py`` suffix,
    then map package initializers to their package. Nonstandard layouts still require
    the provider's indexed module name instead of this conventional-layout derivation.

    Args:
        path: Filesystem path to a ``.py`` file (relative or absolute-ish).

    Returns:
        The dotted module name (empty string if input collapses to nothing).

    Examples:
        >>> derive_module_from_path("./src/pkg/mod.py")
        'pkg.mod'
        >>> derive_module_from_path("src/pkg/mod.py")
        'pkg.mod'
        >>> derive_module_from_path("./pkg/mod.py")
        'pkg.mod'
        >>> derive_module_from_path("pkg/__init__.py")
        'pkg'
        >>> derive_module_from_path("mod.py")
        'mod'
    """
    s = path.replace("\\", "/")
    if s.startswith("./"):
        s = s[2:]
    if s.startswith("src/"):
        s = s[4:]
    if s.endswith(".py"):
        s = s[:-3]
    if s.endswith("/__init__"):
        s = s.removesuffix("/__init__")
    return s.replace("/", ".")


def derive_modules_from_find(files: Iterable[str]) -> list[str]:
    """Derive module dotted names from an iterable of ``.py`` file paths (find-mode rules).

    Empty entries are dropped. Order preserved.

    Args:
        files: Iterable of file path strings, typically from a ``find ... -name '*.py'`` walk.

    Returns:
        List of dotted module names, in input order, with empty entries dropped.

    Examples:
        >>> derive_modules_from_find(["./src/a.py", "./src/pkg/b.py", ""])
        ['a', 'pkg.b']
        >>> derive_modules_from_find([])
        []
    """
    out: list[str] = []
    for f in files:
        if not f:
            continue
        mod = derive_module_from_path(f)
        if mod:
            out.append(mod)
    return out


def derive_modules_from_diff(diff_files: Iterable[str], limit: int) -> list[str]:
    """Derive module dotted names from git diff output (diff-mode rules).

    Drop non-Python paths, retain package initializers as package modules, deduplicate
    in input order, and cap at ``limit`` entries. Find and diff share the same naming rules.

    Args:
        diff_files: Iterable of file path strings from ``git diff HEAD --name-only``.
        limit: Maximum number of distinct modules returned.

    Returns:
        Distinct module dotted names in input order.

    Examples:
        >>> derive_modules_from_diff(["src/pkg/a.py", "src/pkg/__init__.py", "README.md"], 10)
        ['pkg.a', 'pkg']
        >>> derive_modules_from_diff(["pkg/__init__.py"], 10)
        ['pkg']
        >>> derive_modules_from_diff(["lib/x.py", "lib/y.py", "other/z.py"], 10)
        ['lib.x', 'lib.y', 'other.z']
        >>> derive_modules_from_diff(["lib/__init__.py", "other/__init__.py"], 10)
        ['lib', 'other']
        >>> derive_modules_from_diff([], 10)
        []
    """
    modules = derive_modules_from_find(f for f in diff_files if f.endswith(".py"))
    return list(dict.fromkeys(modules))[:limit]


def _git_diff_files(timeout: int = 15) -> list[str]:
    """Return ``.py`` file paths from ``git diff HEAD --name-only`` (empty list on failure)."""
    try:
        out = subprocess.check_output(  # noqa: S603 — fixed argv, no shell.
            ["git", "diff", "HEAD", "--name-only"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return []
    return [line for line in out.splitlines() if line]


def _git_root(timeout: int = 15) -> Path:
    """Return the git toplevel, falling back to the current directory.

    Mirrors ``codemap_py.index_paths.canonical_root``: the index the scanner writes lives under the repository root, so
    a consumer that anchored on the process CWD reported a false ``no_index`` whenever a skill ran from a subdirectory.
    """
    try:
        out = subprocess.check_output(  # noqa: S603 — fixed argv, no shell.
            ["git", "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        ).strip()
        if out:
            return Path(out).resolve()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return Path.cwd().resolve()


def _index_path(root: Path) -> Path:
    """Return the index file for *root*, honouring the flat ``CODEMAP_INDEX_DIR`` override.

    The project name is the raw directory basename — the scanner writes it verbatim, so any sanitization here would seek
    a filename that is never written.
    """
    override = os.environ.get("CODEMAP_INDEX_DIR")
    index_dir = Path(override).expanduser().resolve() if override else root / ".cache" / "codemap"
    return index_dir / f"{root.name}.json"


_MAX_FIND_FILES = 2000


def _find_py_files(target: str) -> list[str]:
    """Walk ``target`` and return ``./``-prefixed paths of regular ``.py`` files.

    Security guards:
        * ``target`` must resolve to a path inside the current working directory;
          paths escaping CWD (e.g. ``/``, ``~/.ssh``) are refused.
        * Walk is capped at ``_MAX_FIND_FILES`` to prevent resource exhaustion.
    """
    root = Path(target)
    if not root.exists():
        return []
    # Restrict scans to CWD subtree — refuse arbitrary filesystem paths.
    resolved_root = root.resolve()
    cwd = Path.cwd().resolve()
    try:
        resolved_root.relative_to(cwd)
    except ValueError:
        print(
            f"codemap_scan: --target path outside project root: {resolved_root}",
            file=sys.stderr,
        )
        return []
    paths: list[str] = []
    for p in sorted(root.rglob("*.py")):
        if p.is_file():
            # Symlink-escape guard: resolve each candidate and confirm it remains under cwd.
            # Catches symlinks that physically reside inside the scan root but target paths outside.
            try:
                p.resolve(strict=True).relative_to(cwd)
            except (ValueError, OSError):
                continue
            # Match bash semantics: paths start with ./ when target starts with ./
            s = p.as_posix()
            if target.startswith("./") and not s.startswith("./"):
                s = "./" + s
            paths.append(s)
            if len(paths) >= _MAX_FIND_FILES:
                break
    return paths


def _scan_query(args: list[str], timeout: int = 15) -> None:
    """Invoke ``codemap-py query`` with given args; stream stdout; swallow non-zero exits."""
    try:
        subprocess.run(  # noqa: S603 — fixed binary name + caller-controlled args.
            ["codemap-py", "query", *args],
            check=False,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # codemap-py query missing or timed out — silent skip.
        return


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse CLI args matching the bash interface (``--source=...`` and ``--source ...`` forms)."""
    parser = argparse.ArgumentParser(
        prog="codemap_scan.py",
        description="Derive affected modules and emit codemap-py query rdeps + optional coupled output.",
        add_help=True,
    )
    parser.add_argument("--source", choices=[s.value for s in ScanSource], default=None)
    parser.add_argument("--target", default="")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument(
        "--timeout",
        type=int,
        default=15,
        help="Subprocess timeout in seconds for git and codemap-py query calls (default: 15).",
    )
    # Bash version silently ignored unrecognised positional tokens — mirror that.
    args, _unknown = parser.parse_known_args(argv)
    return args


def main(argv: list[str] | None = None) -> int:
    """Entry point — mirrors ``codemap-scan.sh`` behaviour exactly.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Exit code (0 success or soft-skip; 1 missing prerequisite arg).
    """
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))

    if not args.source:
        print("codemap_scan.py: --source=find|diff required", file=sys.stderr)
        return 1
    # Boundary conversion: argparse hands back a plain string; everything below is the enum.
    source = ScanSource(args.source)

    # codemap-py query missing → silent skip (caller decides whether to warn).
    if shutil.which("codemap-py") is None:
        return 0

    # Index file missing → silent exit 0.
    index_path = _index_path(_git_root(timeout=args.timeout))
    if not index_path.is_file():
        return 0

    if source == ScanSource.FIND:
        if not args.target:
            print("codemap_scan.py: --target required for --source=find", file=sys.stderr)
            return 1
        modules = derive_modules_from_find(_find_py_files(args.target))
    else:  # diff
        modules = derive_modules_from_diff(_git_diff_files(timeout=args.timeout), args.limit)

    if not modules:
        return 0

    for mod in modules:
        if mod:
            _scan_query(["rdeps", mod], timeout=args.timeout)

    if source == ScanSource.FIND:
        _scan_query(["coupled", "--top", str(args.limit)], timeout=args.timeout)

    return 0


if __name__ == "__main__":
    sys.exit(main())
