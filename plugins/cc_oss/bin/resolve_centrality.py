#!/usr/bin/env python
"""resolve_centrality.py — turn ``codemap-py query central`` output into a centrality map plus a file→canonical-module
resolver for oss:resolve Step 8 Phase 3 and for the oss:review / oss:resolve structural-context pre-flight.

Phase 3 orders whole worktree groups most-central-first (see
``merge_specialist_batch.order_plan``). That needs two things keyed the **same** way:

1. a ``{module: rdep_count}`` centrality map, and
2. each plan item's module name — and it must equal a key in (1) or the lookup
   silently scores 0.

The earlier approach derived the plan module with a hand-rolled
``sed 's|^src/||; s|/|.|g; s|\\.py$||'`` while the map was keyed by codemap's own
``name``. The two disagree — notably ``pkg/__init__.py`` becomes ``pkg.__init__``
by sed but ``pkg`` in codemap — so package-init items never matched and were
mis-ordered with no error. This helper removes the divergence: both the map and
the per-file resolution come from the **one** ``codemap-py query central`` payload, which
carries ``name``, ``path`` and ``rdep_count`` per module. Files are matched to
modules by path suffix (robust to a scan-root prefix on the index path), so the
returned module name is always a real centrality key.

Usage:
    codemap-py query central --top 100000 | resolve_centrality.py --files a.py,b/c.py
    codemap-py query central --top 100000 | resolve_centrality.py --files a.py --modules-only

Output (stdout, JSON):
    {"centrality": {name: rdep_count, ...},
     "file_module": {file: name_or_empty, ...}}

With ``--modules-only``: one canonical module name per line instead of JSON, in
input-file order, de-duplicated, unresolved files omitted. That is the form the
per-module query loops in ``review/modes/codemap-context.md`` and ``resolve/SKILL.md``
consume — they used to re-derive names with the very ``sed`` transform this module
exists to replace.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

#: Module names safe to hand to an unquoted shell word-split loop (``for m in $(... --modules-only)``): no whitespace,
#: glob or shell metacharacters, and no leading ``-`` that a downstream CLI would read as an option. A name outside this
#: shape comes from an unusual repo path and is dropped rather than risk splitting, globbing or option injection.
_SAFE_MODULE_NAME_RE = re.compile(r"[\w.][\w.-]*")
#: Upper bound on the ``codemap-py query central`` payload read from stdin, in characters.
_MAX_STDIN_CHARS = 64 * 1024 * 1024


def match_module(file: str, paths: list[tuple[str, str]]) -> str | None:
    """Resolve one repo-relative file path to its canonical codemap module name.

    Matches ``file`` against each module ``path`` by suffix so a scan-root prefix on
    the index path (e.g. ``project-master/src/pkg/mod.py`` vs ``src/pkg/mod.py``) still
    matches. When several modules match, the one with the longest path wins (most
    specific). Comparison is on ``/``-posix paths as given — callers pass repo-relative
    strings on both sides.

    Args:
        file: Repo-relative file path of the edited item (e.g. ``"src/pkg/auth.py"``).
        paths: ``(name, path)`` pairs for every indexed module.

    Returns:
        The matching module ``name``, or None when nothing matches (non-Python file,
        or a path the index does not know).

    Examples:
        >>> mods = [("pkg", "src/pkg/__init__.py"), ("pkg.auth", "src/pkg/auth.py")]
        >>> match_module("src/pkg/auth.py", mods)
        'pkg.auth'
        >>> match_module("src/pkg/__init__.py", mods)
        'pkg'
        >>> match_module("proj-main/src/pkg/auth.py", mods)  # index path has no prefix
        'pkg.auth'
        >>> match_module("src/pkg/auth.py", [("pkg", "a/b/src/pkg/auth.py")])  # prefix on index side
        'pkg'
        >>> match_module("README.md", mods) is None
        True
    """
    best_name: str | None = None
    best_len = -1
    for name, path in paths:
        if path == file or path.endswith("/" + file) or file.endswith("/" + path):
            if len(path) > best_len:
                best_name, best_len = name, len(path)
    return best_name


def _rdep_count(module: dict[str, object]) -> int:
    """Read ``rdep_count`` as an int; a missing or malformed value counts as 0 (ordered last)."""
    try:
        return int(module.get("rdep_count") or 0)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0


def build_maps(central: dict[str, object], files: list[str]) -> dict[str, object]:
    """Build the centrality map and the file→module resolution from a central payload.

    Args:
        central: Parsed ``codemap-py query central`` output containing entries with
            ``name``, ``rdep_count``, and ``path`` fields.
        files: Repo-relative file paths to resolve to module names.

    Returns:
        ``{"centrality": {name: rdep_count}, "file_module": {file: name_or_empty}}``.
        Unresolved files map to ``""`` (scored 0 downstream, ordered last).

    Examples:
        >>> payload = {"central": [{"name": "pkg.auth", "rdep_count": 9, "path": "src/pkg/auth.py"}]}
        >>> build_maps(payload, ["src/pkg/auth.py", "docs/x.md"])
        {'centrality': {'pkg.auth': 9}, 'file_module': {'src/pkg/auth.py': 'pkg.auth', 'docs/x.md': ''}}
        >>> build_maps({"central": [{"rdep_count": 3}, "junk", {"name": "pkg.a", "rdep_count": None}]}, [])
        {'centrality': {'pkg.a': 0}, 'file_module': {}}
    """
    raw = central.get("central") if isinstance(central, dict) else None
    modules = [m for m in raw if isinstance(m, dict) and "name" in m] if isinstance(raw, list) else []
    centrality = {str(m["name"]): _rdep_count(m) for m in modules}
    paths = [(str(m["name"]), str(m.get("path", ""))) for m in modules if m.get("path")]
    file_module = {f: (match_module(f, paths) or "") for f in files}
    return {"centrality": centrality, "file_module": file_module}


def ordered_modules(maps: dict[str, object]) -> list[str]:
    """Return the resolved module names in input order, de-duplicated, blanks dropped.

    Args:
        maps: A :func:`build_maps` result.

    Returns:
        Canonical module names for the files that resolved to one. A file the index
        does not know (new file, non-Python) contributes nothing rather than a
        guessed name — querying a name the index never had is a guaranteed error. A name that is not shell-word-safe
        (whitespace, glob or shell metacharacters, leading ``-``) is dropped for the same reason: callers split this
        output with an unquoted ``$(...)``.

    Examples:
        >>> ordered_modules({"file_module": {"a.py": "pkg.a", "b.py": "", "c.py": "pkg.a"}})
        ['pkg.a']
        >>> ordered_modules({"file_module": {"z.py": "pkg.z", "a.py": "pkg.a"}})
        ['pkg.z', 'pkg.a']
        >>> ordered_modules({"file_module": {"a.py": "my pkg", "b.py": "*", "c.py": "-x", "d.py": "ok-pkg.d"}})
        ['ok-pkg.d']
    """
    seen: dict[str, None] = {}
    file_module = maps.get("file_module", {})
    for name in file_module.values() if isinstance(file_module, dict) else []:
        if name and _SAFE_MODULE_NAME_RE.fullmatch(str(name)):
            seen.setdefault(str(name), None)
    return list(seen)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — read central JSON from stdin, print maps as JSON or module lines.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Exit code 0 on success; 1 when stdin is not a valid central JSON object or exceeds the size cap.

    Examples:
        No doctest — reads stdin; covered by pytest with monkeypatch/capsys.
    """
    parser = argparse.ArgumentParser(prog="resolve_centrality.py", description=__doc__)
    parser.add_argument("--files", default="", help="Comma-separated repo-relative file paths to resolve.")
    parser.add_argument(
        "--modules-only",
        action="store_true",
        help="Print one canonical module name per line instead of the JSON maps.",
    )
    args = parser.parse_args(argv)

    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    payload = sys.stdin.read(_MAX_STDIN_CHARS + 1)
    try:
        central = json.loads(payload) if len(payload) <= _MAX_STDIN_CHARS else None
    except (json.JSONDecodeError, ValueError):
        return 1
    if not isinstance(central, dict):
        return 1

    files = [f for f in args.files.split(",") if f]
    maps = build_maps(central, files)
    if args.modules_only:
        for name in ordered_modules(maps):
            print(name)
        return 0
    print(json.dumps(maps))
    return 0


if __name__ == "__main__":
    sys.exit(main())
