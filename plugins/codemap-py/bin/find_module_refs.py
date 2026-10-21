#!/usr/bin/env python
"""find_module_refs.py — list residual references to a renamed module or symbol, ``grep -rn`` style.

``rename-refs`` runs one text search per reference kind after the structural edits (``__all__`` strings,
Sphinx roles, ``import`` / ``from ... import`` lines, relative re-exports, packaging config). Each used to
be a hand-built ``grep -rn`` with its own dot-escaping idiom (``$(printf | sed)`` capture). This helper
keeps the patterns in one tested place, escapes every user-supplied value (dots literal, never any-char),
and needs no shell, so it also runs on native Windows.

Output is one ``<path>:<line>:<text>`` per hit — the shape of ``grep -rn`` over a directory. Files are
visited in sorted order. No hits is a normal outcome: nothing is printed and the exit code stays ``0``.

Kinds (``--module`` is the dotted old module path, ``--name`` the bare old symbol name):

    all-exports     ``"NAME"`` / ``'NAME'`` in ``__init__.py``              needs --name,   default scope ``.``
    sphinx-symbol   ``:func:`` ``:class:`` ``:meth:`` ``:mod:`` ``:attr:`` roles containing NAME
                    in ``*.py`` / ``*.rst``                                   needs --name,   default scope ``.``
    import          ``import MODULE`` / ``import MODULE as`` in ``*.py``, at line start or indented
                    (function body, ``try``, ``if TYPE_CHECKING:``)          needs --module, default scope ``.``
    from-import     ``from MODULE import`` / ``from MODULE as`` in ``*.py``, at line start or indented
                                                                              needs --module, default scope ``.``
    attribute       ``MODULE.<name>`` attribute uses in ``*.py`` (``pkg.old.func()`` after ``import pkg``),
                    not preceded by a word character or dot, import lines excluded
                                                                              needs --module, default scope ``.``
    relative        ``from .BASE import`` style re-exports in ``__init__.py`` needs --module, default scope =
                    the module's package directory (see below)
    config          MODULE in ``pyproject.toml`` and ``setup.cfg`` (not recursive)
                                                                              needs --module, default scope ``.``
    sphinx-mod      ``:mod:`` roles containing MODULE in ``*.py`` / ``*.rst``; when none, retry with only
                    the last dotted component (stderr says so)               needs --module, default scope ``.``
    name            NAME as a whole word in ``*.py`` / ``*.pyi``: import lines, call sites, base classes,
                    ``isinstance`` targets, callback values, ``__all__`` strings, comments
                                                                              needs --name,   default scope ``.``

``--files-from-json FILE`` (repeatable) replaces the ``--scope`` walk with the files named by saved
``codemap-py query`` answers: ``called_by[].path`` (``fn-rdeps``), ``matches[].path`` (``find-symbol``), and a
``batch`` answer's ``rdeps`` items — each queried module plus its ``imported_by`` modules, mapped to paths through
the batch's ``list --limit 0`` item. ``rename-refs`` uses it to list every whole-word reference in the files that
call, import or re-export a renamed symbol, which ``fn-rdeps`` call edges alone miss.

``--alias-in FILE`` (``name`` only) drops the ``def NAME`` / ``class NAME`` stub line in FILE, so the
deprecated alias ``rename-refs --deprecate`` keeps there is not counted as a residual reference. Without it a
surviving definition line is a hit: the rename did not land.

``relative`` default scope mirrors the former inline shell: the module path minus its last component,
dots turned into ``/``, relative to the working directory (``.`` when that is empty). A ``src/`` layout
therefore needs an explicit ``--scope``. A missing scope directory yields no hits, as before. ``--scope``
may also name a single file, which is searched regardless of the kind's file filter.

Directories named in ``_SKIP_DIRS`` (VCS metadata, virtualenvs, tool caches) are not entered and symlinks
are not followed. ``grep -rn .`` descended into them; a virtualenv's ``site-packages`` is third-party
code that is never a rename target and made the walk slow enough to exceed the skill's block timeouts.

Exit codes:
    0 — search completed (with or without hits)
    1 — a ``--files-from-json`` answer is unreadable, carries an ``error``, or cannot map modules to paths
    2 — usage error (unknown kind, missing ``--module`` / ``--name``, ``--alias-in`` without ``--kind name``)
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass

_SCRIPT_NAME = "find_module_refs"

#: Directories never searched. A deliberate subset of ``codemap_py.scanner.SKIP_DIRS`` (the index skips
#: them too) limited to VCS metadata, virtualenvs and tool caches, so the search stays aligned with the
#: index without narrowing anything a project could legitimately own as source.
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".eggs",
    }
)
_CONFIG_FILES = ("pyproject.toml", "setup.cfg")


def _basename(module: str) -> str:
    """Last dotted component of ``module`` (the whole string when it has no dot).

    Examples:
        >>> _basename("pkg.sub.old"), _basename("old")
        ('old', 'old')
    """
    return module.rpartition(".")[2]


def default_relative_scope(module: str) -> str:
    """Package directory for the ``relative`` kind: ``module`` minus its last component, as a path.

    Matches the former ``${OLD_MODULE_PATH%.*}`` + ``tr '.' '/'``: ``%.*`` leaves a dot-free string
    unchanged, so a top-level module maps to a directory named after itself (normally absent -> no hits).

    Examples:
        >>> default_relative_scope("pkg.sub.old")
        'pkg/sub'
        >>> default_relative_scope("old")
        'old'
        >>> default_relative_scope("")
        '.'
    """
    head, sep, _tail = module.rpartition(".")
    return (head if sep else module).replace(".", "/") or "."


def _compile(pattern: str) -> re.Pattern[str]:
    """Compile ``pattern`` with ``MULTILINE`` so ``search_file`` can reject a whole file in one C-speed scan.

    A line-level match is always a whole-text match, so the pre-scan never drops a hit; ``MULTILINE`` only changes how
    ``^`` anchors in the whole text and is a no-op on the single lines checked afterwards.
    """
    return re.compile(pattern, re.MULTILINE)


def _all_exports(module: str, name: str) -> tuple[re.Pattern[str], ...]:
    quoted = re.escape(name)
    return (_compile(f"\"{quoted}\"|'{quoted}'"),)


def _sphinx_symbol(module: str, name: str) -> tuple[re.Pattern[str], ...]:
    return (_compile(rf":(?:func|class|meth|mod|attr):`[^']*{re.escape(name)}[^']*`"),)


def _import(module: str, name: str) -> tuple[re.Pattern[str], ...]:
    # Leading blanks allowed: an import inside a function, ``try`` or ``if TYPE_CHECKING:`` block is a residual too.
    mod = re.escape(module)
    return (_compile(rf"^[ \t]*import {mod}\b|^[ \t]*import {mod} as "),)


def _from_import(module: str, name: str) -> tuple[re.Pattern[str], ...]:
    mod = re.escape(module)
    return (_compile(rf"^[ \t]*from {mod} import|^[ \t]*from {mod} as "),)


def _attribute(module: str, name: str) -> tuple[re.Pattern[str], ...]:
    # ``pkg.old.func()`` after ``import pkg`` — no import line names ``pkg.old``. Import lines are left to the
    # ``import``/``from-import`` kinds; a word character or dot before MODULE (``other.pkg.old.x``) is a different
    # dotted path; the trailing ``.<name>`` makes it an attribute access.
    return (_compile(rf"^(?![ \t]*(?:import|from)\b).*?(?<![\w.]){re.escape(module)}\.[A-Za-z_]"),)


def _relative(module: str, name: str) -> tuple[re.Pattern[str], ...]:
    base = re.escape(_basename(module))
    return (_compile(rf"from \.*[^.]*\.{base} import|from \.{base} import|from \.{base} as "),)


def _config(module: str, name: str) -> tuple[re.Pattern[str], ...]:
    return (_compile(re.escape(module)),)


def _sphinx_mod(module: str, name: str) -> tuple[re.Pattern[str], ...]:
    return tuple(_compile(rf":mod:`[^']*{re.escape(text)}[^']*`") for text in (module, _basename(module)))


def _name(module: str, name: str) -> tuple[re.Pattern[str], ...]:
    # Whole word, not ``\b``: ``\b`` needs a word character at the edge, so a name ending in a non-word character
    # would never match. ``Base`` hits ``class C(Base)``, ``pkg.Base`` and ``"Base"``; never ``BaseModel``.
    return (_compile(rf"(?<!\w){re.escape(name)}(?!\w)"),)


def _alias_stub(name: str) -> re.Pattern[str]:
    """Pattern for the ``def NAME`` / ``class NAME`` line of a deprecated alias, indented for a method stub.

    Examples:
        >>> bool(_alias_stub("old").search("    def old(*args, **kwargs): ..."))
        True
        >>> bool(_alias_stub("old").search("def old_v2(): ..."))
        False
    """
    return re.compile(rf"^[ \t]*(?:async[ \t]+)?(?:def|class)[ \t]+{re.escape(name)}(?!\w)")


@dataclass(frozen=True)
class _Kind:
    """One reference kind: tiered patterns (first tier with hits wins), file filter, required inputs."""

    patterns: Callable[[str, str], tuple[re.Pattern[str], ...]]
    includes: tuple[str, ...]
    needs: str  # "module" or "name"


_KINDS: dict[str, _Kind] = {
    "all-exports": _Kind(_all_exports, ("__init__.py",), "name"),
    "sphinx-symbol": _Kind(_sphinx_symbol, ("*.py", "*.rst"), "name"),
    "import": _Kind(_import, ("*.py",), "module"),
    "from-import": _Kind(_from_import, ("*.py",), "module"),
    "attribute": _Kind(_attribute, ("*.py",), "module"),
    "relative": _Kind(_relative, ("__init__.py",), "module"),
    "config": _Kind(_config, (), "module"),
    "sphinx-mod": _Kind(_sphinx_mod, ("*.py", "*.rst"), "module"),
    "name": _Kind(_name, ("*.py", "*.pyi"), "name"),
}


def iter_files(scope: str, includes: tuple[str, ...]) -> Iterator[str]:
    """Yield searchable files under ``scope`` in sorted order, ``grep -r`` style paths.

    A file ``scope`` is yielded as-is. A directory is walked without following symlinks, pruning
    ``_SKIP_DIRS``; files are filtered by ``includes`` (basename globs, empty = all). A missing scope
    yields nothing. Like ``grep -r``, a walked path keeps ``scope`` exactly as given and joins what the
    walk adds with ``/`` on every OS, so Windows yields ``./pkg/x.py`` rather than ``.\\pkg\\x.py``.

    Examples:
        >>> import tempfile, pathlib
        >>> with tempfile.TemporaryDirectory() as d:
        ...     _ = pathlib.Path(d, "a.py").write_text(""); _ = pathlib.Path(d, "b.txt").write_text("")
        ...     [os.path.basename(p) for p in iter_files(d, ("*.py",))]
        ['a.py']
    """
    if os.path.isfile(scope):
        yield scope
        return
    for root, dirs, files in os.walk(scope):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        for filename in sorted(files):
            if includes and not any(fnmatch.fnmatchcase(filename, glob) for glob in includes):
                continue
            path = os.path.join(root, filename)
            if not os.path.islink(path):
                # os.walk roots start with scope verbatim; only the separators the walk added are native.
                yield scope + path[len(scope) :].replace(os.sep, "/")


def search_file(path: str, pattern: re.Pattern[str]) -> Iterator[tuple[int, str]]:
    """Yield ``(line_number, text)`` for each line of ``path`` matching ``pattern``; unreadable -> nothing.

    The file is scanned whole first and only split into lines when it matches: most files hold no hit, and
    one C-speed scan is several times cheaper than a Python-level pass over every line of a large tree.
    ``pattern`` must be ``MULTILINE`` (see ``_compile``) so that scan honours ``^``.
    """
    try:
        with open(path, encoding="utf-8", errors="replace", newline="\n") as handle:
            content = handle.read()
    except OSError:
        return
    if not pattern.search(content):
        return
    for number, line in enumerate(content.split("\n"), 1):
        text = line.rstrip("\r")
        if pattern.search(text):
            yield number, text


def _candidate_files(kind: str, scope: str, includes: tuple[str, ...]) -> list[str]:
    """Files to search: the two packaging files (not recursive) for ``config``, else a walk of ``scope``."""
    if kind != "config":
        return list(iter_files(scope, includes))
    prefix = "" if scope in ("", ".") else scope.rstrip("/") + "/"
    return [path for path in (prefix + name for name in _CONFIG_FILES) if os.path.isfile(path)]


def _entry_paths(entries: object) -> set[str]:
    """Collect the non-empty string ``path`` of every object in ``entries`` (anything else is ignored)."""
    if not isinstance(entries, list):
        return set()
    return {e["path"] for e in entries if isinstance(e, dict) and isinstance(e.get("path"), str) and e["path"]}


def _batch_paths(batch: list[object]) -> set[str]:
    """Paths of every module a ``batch`` answer's ``rdeps`` items name: the queried module and its importers.

    Module names map to paths through the batch's ``list`` item, which must be complete (``shown == total``).
    A failed ``rdeps`` item is skipped — an ancestor package without ``__init__.py`` is not indexed — but a
    truncated one raises, since a partial importer list would silently shrink the edit set.

    Raises:
        ValueError: No successful ``list`` item, a truncated listing, or a truncated ``rdeps`` item.

    Examples:
        >>> listing = {"modules": [{"name": "p", "path": "p/__init__.py"}, {"name": "p.d", "path": "p/d.py"}],
        ...            "total": 2, "shown": 2}
        >>> rdeps = {"module": "p", "imported_by": ["p.d"], "index": {}}
        >>> sorted(_batch_paths([{"cmd": "list", "ok": True, "result": listing},
        ...                      {"cmd": "rdeps", "ok": True, "result": rdeps}]))
        ['p/__init__.py', 'p/d.py']
    """
    items = [item for item in batch if isinstance(item, dict)]
    listing = next((i.get("result") for i in items if i.get("cmd") == "list" and i.get("ok")), None)
    if not isinstance(listing, dict):
        raise ValueError("batch answer has no successful 'list' item to map module names to paths")
    if listing.get("shown") != listing.get("total"):
        raise ValueError("batch 'list' item is truncated — query it with --limit 0")
    where = {
        m["name"]: m["path"]
        for m in listing.get("modules") or []
        if isinstance(m, dict) and isinstance(m.get("name"), str) and isinstance(m.get("path"), str)
    }
    paths: set[str] = set()
    for item in items:
        result = item.get("result")
        if item.get("cmd") != "rdeps" or not item.get("ok") or not isinstance(result, dict):
            continue
        if (result.get("index") or {}).get("truncated"):
            raise ValueError(f"rdeps answer for {result.get('module')!r} is truncated — query it without --limit")
        importers = result.get("imported_by")
        names = [result.get("module"), *(importers if isinstance(importers, list) else [])]
        paths.update(where[n] for n in names if isinstance(n, str) and n in where)
    return paths


def answer_paths(payload: object) -> set[str]:
    """Files named by one saved ``codemap-py query`` answer (see the module docstring for the accepted keys).

    Raises:
        ValueError: The answer is not an object, carries an ``error``, or is a batch that cannot be mapped.

    Examples:
        >>> sorted(answer_paths({"called_by": [{"caller": "p.d::make", "path": "p/d.py"}], "count": 1}))
        ['p/d.py']
        >>> sorted(answer_paths({"matches": [{"path": "p/a.py"}, {"path": ""}]}))
        ['p/a.py']
    """
    if not isinstance(payload, dict):
        raise ValueError("query answer is not a JSON object")
    if "error" in payload:
        raise ValueError(f"query answer carries an error: {payload['error']}")
    paths = _entry_paths(payload.get("called_by")) | _entry_paths(payload.get("matches"))
    batch = payload.get("batch")
    if isinstance(batch, list):
        paths |= _batch_paths(batch)
    return paths


def files_from_answers(sources: Iterable[str]) -> list[str]:
    """Sorted existing files named by the saved query answers in ``sources`` (JSON file paths).

    Raises:
        ValueError: A source is unreadable, is not JSON, or fails :func:`answer_paths`.
    """
    paths: set[str] = set()
    for source in sources:
        try:
            with open(source, encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read query answer {source}: {exc}") from exc
        try:
            paths |= answer_paths(payload)
        except ValueError as exc:
            raise ValueError(f"{source}: {exc}") from exc
    return sorted(p for p in paths if os.path.isfile(p) and not os.path.islink(p))


def find_refs(
    kind: str,
    module: str,
    name: str,
    scope: str,
    files: list[str] | None = None,
    alias_in: str | None = None,
) -> tuple[list[str], int]:
    """Search ``scope`` (or exactly ``files``) for ``kind`` references.

    Args:
        kind: Key of ``_KINDS``.
        module: Dotted old module path.
        name: Bare old symbol name.
        scope: Directory (or single file) to search; ignored when ``files`` is given.
        files: Explicit files to search instead of walking ``scope`` (``--files-from-json``).
        alias_in: File whose ``def NAME`` / ``class NAME`` stub line is not a hit (``--alias-in``).

    Returns:
        ``(lines, tier)``: formatted ``path:line:text`` hits and the 0-based index of the pattern tier that
        produced them (1 means ``sphinx-mod`` fell back to the basename).
    """
    spec = _KINDS[kind]
    candidates = files if files is not None else _candidate_files(kind, scope, spec.includes)
    stub = _alias_stub(name) if alias_in else None
    alias_path = os.path.normpath(alias_in) if alias_in else None
    for tier, pattern in enumerate(spec.patterns(module, name)):
        hits = [
            f"{path}:{number}:{text}"
            for path in candidates
            for number, text in search_file(path, pattern)
            if not (stub and os.path.normpath(path) == alias_path and stub.search(text))
        ]
        if hits:
            return hits, tier
    return [], 0


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser."""
    parser = argparse.ArgumentParser(prog=_SCRIPT_NAME, description="List residual references, grep -rn style.")
    parser.add_argument("--kind", required=True, choices=sorted(_KINDS))
    parser.add_argument("--module", default="", help="Dotted old module path.")
    parser.add_argument("--name", default="", help="Bare old symbol name.")
    parser.add_argument("--scope", default=None, help="Directory or file to search (default: per kind, see docstring).")
    parser.add_argument(
        "--files-from-json",
        action="append",
        default=[],
        metavar="FILE",
        help="Search only the files named by this saved query answer (repeatable; replaces the --scope walk).",
    )
    parser.add_argument("--alias-in", default=None, metavar="FILE", help="Skip the def/class NAME stub line in FILE.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — returns the process exit code (see module docstring)."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, _KINDS[args.kind].needs):
        parser.error(f"--kind {args.kind} requires a non-empty --{_KINDS[args.kind].needs}")
    if args.alias_in and args.kind != "name":
        parser.error("--alias-in applies to --kind name only")
    scope = args.scope
    if scope is None:
        scope = default_relative_scope(args.module) if args.kind == "relative" else "."
    files = None
    if args.files_from_json:
        try:
            files = files_from_answers(args.files_from_json)
        except ValueError as exc:
            sys.stderr.write(f"! {_SCRIPT_NAME}: {exc} — reference sites unknown\n")
            return 1
    hits, tier = find_refs(args.kind, args.module, args.name, scope, files, args.alias_in)
    if args.kind == "sphinx-mod" and hits and tier:
        sys.stderr.write(f"{_SCRIPT_NAME}: no full-path :mod: match; basename-only hits — verify package context\n")
    for line in hits:
        sys.stdout.write(line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
