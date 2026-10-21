#!/usr/bin/env python
"""cm_json.py — read fields out of ``codemap-py query`` JSON and write them to session sentinels.

``rename-refs`` used to pull each value out of a query answer with its own
``VAR=$(printf '%s' "$JSON" | python3 -c "...")`` one-liner: one interpreter fork per value and a
command-substitution capture that trips the "Contains expansion" approval gate. This helper reads the
answer once and writes every requested value to ``<tmpdir>/<prefix>-<name>-<CSID>``; the skill reads
them back with ``IFS= read -r`` (a shell builtin), never ``$(...)``. Sentinel handling (owner-checked
``TMPDIR``, symlink-safe ``0o600`` writes, ``--output-prefix`` validation) is imported from
``resolve_index_env.py`` so the security-sensitive code has one implementation.

Usage:
    export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
    _CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
    python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/cm_json.py" first-match \\
        --input "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-find-symbol-json-${CSID}" \\
        --output-prefix "codemap-${_CM_PROJ}" \\
        --field module=sym-module --field qualified_name=sym-qname
    IFS= read -r SYM_MODULE < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-sym-module-${CSID}" 2>/dev/null || SYM_MODULE=""

Subcommands:
    first-match   Copy fields of ``matches[0]`` (``find-symbol`` / ``symbol`` answers) to sentinels.
                  ``--field KEY=NAME`` (repeatable) writes ``matches[0][KEY]`` to sentinel ``NAME``;
                  absent, null, or empty values become ``--default`` (default: empty string).
    rdeps-summary Summarise a ``fn-rdeps`` (``--kind fn``) or ``rdeps`` (``--kind module``) answer into
                  four sentinels: ``rdep-count``, ``rename-EXHAUSTIVE``, ``rename-RDEP_HINT`` and
                  ``rename-INDEX_HINT`` (the ``index.hint`` search advisory, such as hook-registered callers).
    index-path    Write the file path of module ``--module`` from an index file's ``modules[]`` to
                  sentinel ``--name`` (empty when the module is absent).
    stale         Write ``check_index_smoke.py`` freshness to sentinel ``--name``: ``true``, ``false``, or
                  ``unknown`` when ``ok`` is not true or ``stale`` is absent/null.
    class-range   Write ``"<start_line> <end_line>"`` of the ``find-symbol`` match whose ``type`` is
                  ``class`` and whose ``qualified_name`` equals ``--qualified-name`` to sentinel ``--name``
                  (empty when there is none).
    narrow        Rewrite a ``find-symbol`` answer file in place: ``--qualified-name`` keeps only the
                  exact matches when any exist (unchanged otherwise); ``--index`` keeps only that one
                  match. No sentinel and no ``--output-prefix``.

``--input`` is a file path, or ``-`` for stdin (``narrow`` needs a file). Sentinels are always written,
including on unreadable or mistyped input, so a stale value from an earlier call never survives; defaults
are fail-closed (count ``0``, ``EXHAUSTIVE=false``, empty hints, empty path, ``STALE=unknown``, empty
range) and a ``!`` warning goes to stderr.

Exit codes:
    0 — sentinels written (fail-closed defaults when the input was unusable); ``narrow`` done
    1 — a sentinel or the narrowed file could not be written (for example a pre-planted symlink)
    2 — usage error
    3 — unsafe ``--output-prefix``
    4 — ``narrow`` input unusable or ``--index`` out of range; the file is left unchanged
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

#: This ``bin/`` directory, put on ``sys.path`` so the bare ``resolve_index_env`` import also resolves when
#: ``pytest --doctest-modules`` imports this file by path (a script run already has its own directory on the path).
_BIN_DIR = str(Path(__file__).resolve().parent)
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)

from resolve_index_env import (  # noqa: E402  (needs the sys.path insert above)
    _resolve_csid,
    _resolve_tmpdir,
    _validate_output_prefix,
    _write_sentinel_file,
)

_SCRIPT_NAME = "cm_json"

#: Sentinel names are bare tokens (same character class as ``--output-prefix``) — no path separators.
_SENTINEL_NAME_RE = re.compile(r"(?!\.\.?$)[a-zA-Z0-9_.-]+")

#: Sentinel names ``rdeps-summary`` writes; ``rename-refs`` Step 2/3 reads these back.
_RDEP_COUNT = "rdep-count"
_RDEP_EXHAUSTIVE = "rename-EXHAUSTIVE"
_RDEP_HINT = "rename-RDEP_HINT"
_INDEX_HINT = "rename-INDEX_HINT"

#: Exit code for a ``narrow`` input that cannot be used; the answer file is left as it was.
_EXIT_NARROW_UNUSABLE = 4


def _load_json(source: str) -> Any:
    """Parse JSON from a file path, or from stdin when ``source`` is ``-``.

    Args:
        source: File path or ``-``.

    Returns:
        The decoded JSON value.

    Raises:
        ValueError: if the source is unreadable or not valid JSON (``OSError`` and decode errors are
            folded into one type so callers handle a single failure class).

    Examples:
        >>> import tempfile, pathlib
        >>> with tempfile.TemporaryDirectory() as d:
        ...     p = pathlib.Path(d, "x.json"); _ = p.write_text('{"a": 1}', encoding="utf-8")
        ...     _load_json(str(p))
        {'a': 1}
        >>> _load_json("/nonexistent/x.json")  # doctest: +ELLIPSIS
        Traceback (most recent call last):
        ValueError: cannot read JSON from '/nonexistent/x.json': ...
    """
    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
        return json.loads(text)
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read JSON from {source!r}: {exc}") from exc


def _as_line(value: object, default: str) -> str:
    """Render a JSON value as a single sentinel line, ``default`` when absent, null, or empty.

    Examples:
        >>> _as_line("pkg.mod", "")
        'pkg.mod'
        >>> _as_line(None, ".")
        '.'
        >>> _as_line("", "function")
        'function'
        >>> _as_line(7, "")
        '7'
        >>> _as_line("a\\nb", "")
        'a b'
    """
    if value is None or value == "":
        return default
    return " ".join(str(value).splitlines())


def first_match_values(payload: object, fields: dict[str, str], default: str) -> dict[str, str]:
    """Map sentinel name -> value for each requested field of ``payload["matches"][0]``.

    Args:
        payload: Decoded ``find-symbol`` / ``symbol`` answer.
        fields: ``{json_key: sentinel_name}``.
        default: Value for an absent, null, or empty field (also used when there is no first match).

    Returns:
        ``{sentinel_name: value}`` — every requested sentinel present, never missing.

    Examples:
        >>> answer = {"matches": [{"module": "pkg.a", "type": "class"}]}
        >>> first_match_values(answer, {"module": "m", "type": "t", "path": "p"}, "")
        {'m': 'pkg.a', 't': 'class', 'p': ''}
        >>> first_match_values({"matches": []}, {"module": "m"}, ".")
        {'m': '.'}
        >>> first_match_values([], {"module": "m"}, "")
        {'m': ''}
    """
    matches = payload.get("matches") if isinstance(payload, dict) else None
    first = matches[0] if isinstance(matches, list) and matches and isinstance(matches[0], dict) else {}
    return {name: _as_line(first.get(key), default) for key, name in fields.items()}


def rdeps_values(payload: object, kind: str) -> dict[str, str]:
    """Summarise an ``fn-rdeps`` / ``rdeps`` answer into the three rename-refs sentinels.

    ``rename-EXHAUSTIVE`` is forward-first and fail-closed: ``index.query_complete`` wins even when it is
    ``false``; only when that key is absent does the legacy ``index.exhaustive`` apply; anything that is
    not literally ``true`` reads ``false``. A ``//``-style fallback (jq) treats ``false`` as missing, so a
    false ``query_complete`` was once overridden by a true legacy ``exhaustive`` and armed the
    ``--remove-if-no-callers`` delete gate on an incomplete graph.

    ``rename-RDEP_HINT`` is the ``hint`` an ``fn-rdeps`` answer carries for a method or constructor with
    zero static callers (``obj.method()`` and dynamic dispatch are never resolved statically; the answer
    stays ``query_complete`` so the hint alone must block a delete). ``rdeps`` carries no hint, so the
    module kind writes it empty — this overwrites an earlier symbol run's hint so it never reaches the
    module gate.

    ``rename-INDEX_HINT`` is ``index.hint``, a different field: the search the coverage block suggests
    for references the static AST never sees (``fn-rdeps`` names hook-registered callers). It never
    blocks a delete on its own; the blast report prints it so the advisory is not dropped.

    Args:
        payload: Decoded query answer.
        kind: ``"fn"`` (``count`` key) or ``"module"`` (length of ``imported_by``).

    Returns:
        ``{sentinel_name: value}`` for ``rdep-count``, ``rename-EXHAUSTIVE``, ``rename-RDEP_HINT``,
        ``rename-INDEX_HINT``.

    Examples:
        >>> rdeps_values({"count": 3, "index": {"query_complete": True}}, "fn")
        {'rdep-count': '3', 'rename-EXHAUSTIVE': 'true', 'rename-RDEP_HINT': '', 'rename-INDEX_HINT': ''}
        >>> hinted = {"count": 0, "hint": "  obj.m()\\n unresolved ", "index": {"query_complete": True}}
        >>> rdeps_values(hinted, "fn")["rename-RDEP_HINT"]
        'obj.m() unresolved'
        >>> rdeps_values({"index": {"hint": 'grep -rn "f" for hooks'}}, "fn")["rename-INDEX_HINT"]
        'grep -rn "f" for hooks'
        >>> rdeps_values({"index": {"query_complete": False, "exhaustive": True}}, "fn")["rename-EXHAUSTIVE"]
        'false'
        >>> rdeps_values({"index": {"exhaustive": True}}, "fn")["rename-EXHAUSTIVE"]
        'true'
        >>> rdeps_values({"imported_by": ["a", "b"], "hint": "ignored"}, "module")
        {'rdep-count': '2', 'rename-EXHAUSTIVE': 'false', 'rename-RDEP_HINT': '', 'rename-INDEX_HINT': ''}
    """
    data = payload if isinstance(payload, dict) else {}
    if kind == "fn":
        raw_count = data.get("count", 0)
        count = raw_count if isinstance(raw_count, int) and not isinstance(raw_count, bool) else 0
        hint = " ".join(str(data.get("hint") or "").split())
    else:
        imported = data.get("imported_by")
        count = len(imported) if isinstance(imported, list) else 0
        hint = ""
    index = data.get("index")
    index = index if isinstance(index, dict) else {}
    complete = index.get("query_complete", index.get("exhaustive", False))
    index_hint = index.get("hint")
    return {
        _RDEP_COUNT: str(count),
        _RDEP_EXHAUSTIVE: "true" if complete is True else "false",
        _RDEP_HINT: hint,
        _INDEX_HINT: " ".join(index_hint.split()) if isinstance(index_hint, str) else "",
    }


def stale_values(payload: object, name: str) -> dict[str, str]:
    """Return ``{name: "true"|"false"|"unknown"}`` from a ``check_index_smoke.py`` answer.

    ``unknown`` whenever ``ok`` is not literally true (a missing or corrupt index answers
    ``{"ok": false, "stale": false}``, which must never read as fresh) or ``stale`` is absent or null.

    Examples:
        >>> stale_values({"ok": True, "stale": True}, "s"), stale_values({"ok": True, "stale": False}, "s")
        ({'s': 'true'}, {'s': 'false'})
        >>> stale_values({"ok": False, "stale": False}, "s"), stale_values({"ok": True, "stale": None}, "s")
        ({'s': 'unknown'}, {'s': 'unknown'})
        >>> stale_values([], "s")
        {'s': 'unknown'}
    """
    data = payload if isinstance(payload, dict) else {}
    stale = data.get("stale")
    if data.get("ok") is not True or not isinstance(stale, bool):
        return {name: "unknown"}
    return {name: "true" if stale else "false"}


def class_range_values(payload: object, qualified_name: str, name: str) -> dict[str, str]:
    """Return ``{name: "<start_line> <end_line>"}`` of the exact class match in a ``find-symbol`` answer.

    ``find-symbol`` is an unanchored, case-insensitive pattern, so ``matches[0]`` may be a method or a
    sibling class; only a match whose ``type`` is ``class`` and whose ``qualified_name`` equals
    ``qualified_name`` counts. Empty when there is none or its lines are not integers.

    Examples:
        >>> answer = {"matches": [{"type": "method", "qualified_name": "A.f", "start_line": 3, "end_line": 4},
        ...                       {"type": "class", "qualified_name": "A", "start_line": 1, "end_line": 9}]}
        >>> class_range_values(answer, "A", "r")
        {'r': '1 9'}
        >>> class_range_values(answer, "a", "r"), class_range_values({}, "A", "r")
        ({'r': ''}, {'r': ''})
    """
    matches = payload.get("matches") if isinstance(payload, dict) else None
    for match in matches if isinstance(matches, list) else []:
        if isinstance(match, dict) and match.get("type") == "class" and match.get("qualified_name") == qualified_name:
            start, end = match.get("start_line"), match.get("end_line")
            if type(start) is int and type(end) is int:  # bool is an int subclass; never a line number
                return {name: f"{start} {end}"}
            return {name: ""}
    return {name: ""}


def narrow_matches(payload: object, qualified_name: str | None, index: int | None) -> dict[str, Any] | None:
    """Return a narrowed ``find-symbol`` answer, or ``None`` when the input cannot be narrowed.

    ``qualified_name``: keep the matches whose ``qualified_name`` equals it, when any exist; otherwise return
    the answer unchanged (the caller then asks which candidate is meant). ``index``: keep only that match
    (0-based), so every later ``matches[0]`` read is the chosen candidate; out of range -> ``None``.

    Examples:
        >>> answer = {"pattern": "f", "matches": [{"qualified_name": "f"}, {"qualified_name": "f_v2"}], "count": 2}
        >>> narrow_matches(answer, "f", None)["matches"], narrow_matches(answer, "g", None)["count"]
        ([{'qualified_name': 'f'}], 2)
        >>> narrow_matches(answer, None, 1)
        {'pattern': 'f', 'matches': [{'qualified_name': 'f_v2'}], 'count': 1}
        >>> narrow_matches(answer, None, 2) is None, narrow_matches([], "f", None) is None
        (True, True)
    """
    matches = payload.get("matches") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or not isinstance(matches, list):
        return None
    if index is not None:
        if not 0 <= index < len(matches):
            return None
        return {"pattern": payload.get("pattern"), "matches": [matches[index]], "count": 1}
    exact = [match for match in matches if isinstance(match, dict) and match.get("qualified_name") == qualified_name]
    return {**payload, "matches": exact, "count": len(exact)} if exact else payload


def index_path_values(payload: object, module: str, name: str) -> dict[str, str]:
    """Return ``{name: path}`` for the first ``modules[]`` entry called ``module`` (empty when absent).

    Examples:
        >>> idx = {"modules": [{"name": "a.b", "path": "src/a/b.py"}, {"name": "a.c", "path": "src/a/c.py"}]}
        >>> index_path_values(idx, "a.c", "p")
        {'p': 'src/a/c.py'}
        >>> index_path_values(idx, "a.z", "p")
        {'p': ''}
        >>> index_path_values({}, "a.b", "p")
        {'p': ''}
    """
    modules = payload.get("modules") if isinstance(payload, dict) else None
    for entry in modules if isinstance(modules, list) else []:
        if isinstance(entry, dict) and entry.get("name") == module:
            return {name: _as_line(entry.get("path"), "")}
    return {name: ""}


def _field_spec(raw: str) -> tuple[str, str]:
    """Parse one ``--field KEY=NAME`` argument into ``(json_key, sentinel_name)``."""
    key, sep, name = raw.partition("=")
    if not sep or not key or not _SENTINEL_NAME_RE.fullmatch(name):
        raise argparse.ArgumentTypeError(f"--field expects KEY=NAME with NAME matching [a-zA-Z0-9_.-]+, got {raw!r}")
    return key, name


def _sentinel_name(raw: str) -> str:
    """Validate a bare sentinel name (no path separators)."""
    if not _SENTINEL_NAME_RE.fullmatch(raw):
        raise argparse.ArgumentTypeError(f"sentinel name must match [a-zA-Z0-9_.-]+, got {raw!r}")
    return raw


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser with its six subcommands."""
    parser = argparse.ArgumentParser(
        prog=_SCRIPT_NAME, description="Write fields of codemap JSON answers to sentinels."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--input", required=True, help="JSON file path, or '-' for stdin.")
        p.add_argument("--output-prefix", required=True, help="Sentinel prefix, e.g. codemap-${_CM_PROJ}.")

    first = sub.add_parser("first-match", help="Fields of matches[0] -> sentinels.")
    common(first)
    first.add_argument("--field", action="append", required=True, type=_field_spec, metavar="KEY=NAME")
    first.add_argument("--default", default="", help="Value for an absent/null/empty field (default: empty).")

    rdeps = sub.add_parser("rdeps-summary", help="fn-rdeps / rdeps answer -> count, EXHAUSTIVE, hint sentinels.")
    common(rdeps)
    rdeps.add_argument("--kind", required=True, choices=("fn", "module"))

    path = sub.add_parser("index-path", help="modules[] path of --module -> sentinel.")
    common(path)
    path.add_argument("--module", required=True, help="Dotted module name to look up.")
    path.add_argument("--name", required=True, type=_sentinel_name, help="Sentinel name to write.")

    stale = sub.add_parser("stale", help="check_index_smoke.py answer -> true/false/unknown sentinel.")
    common(stale)
    stale.add_argument("--name", required=True, type=_sentinel_name, help="Sentinel name to write.")

    cls_range = sub.add_parser("class-range", help="Exact class match of a find-symbol answer -> 'start end'.")
    common(cls_range)
    cls_range.add_argument("--qualified-name", required=True, help="Exact class qualified_name to match.")
    cls_range.add_argument("--name", required=True, type=_sentinel_name, help="Sentinel name to write.")

    narrow = sub.add_parser("narrow", help="Rewrite a find-symbol answer file to the exact or chosen match.")
    narrow.add_argument("--input", required=True, help="find-symbol answer file, rewritten in place.")
    target = narrow.add_mutually_exclusive_group(required=True)
    target.add_argument("--qualified-name", help="Keep exact qualified_name matches when any exist.")
    target.add_argument("--index", type=int, help="Keep only this 0-based match.")
    return parser


def _fallback_values(args: argparse.Namespace) -> dict[str, str]:
    """Fail-closed values written when the input cannot be used."""
    if args.command == "first-match":
        return {name: args.default for _key, name in args.field}
    if args.command == "rdeps-summary":
        return rdeps_values({}, args.kind)
    if args.command == "stale":
        return stale_values({}, args.name)
    return {args.name: ""}


def _compute_values(args: argparse.Namespace, payload: object) -> dict[str, str]:
    """Dispatch to the subcommand's pure value builder."""
    if args.command == "first-match":
        return first_match_values(payload, {key: name for key, name in args.field}, args.default)
    if args.command == "rdeps-summary":
        return rdeps_values(payload, args.kind)
    if args.command == "stale":
        return stale_values(payload, args.name)
    if args.command == "class-range":
        return class_range_values(payload, args.qualified_name, args.name)
    return index_path_values(payload, args.module, args.name)


def _narrow_file(args: argparse.Namespace) -> int:
    """Rewrite the ``narrow`` input file with the narrowed answer; leave it unchanged on any failure.

    The rewrite goes through the sentinel writer, which refuses a symlink or a file another user owns.
    """
    if args.input == "-":
        sys.stderr.write(f"! {_SCRIPT_NAME} narrow: --input must be a file, not stdin\n")
        return 2
    try:
        narrowed = narrow_matches(_load_json(args.input), args.qualified_name, args.index)
    except ValueError as exc:
        sys.stderr.write(f"! {_SCRIPT_NAME} narrow: {exc} — answer file left unchanged\n")
        return _EXIT_NARROW_UNUSABLE
    if narrowed is None:
        sys.stderr.write(
            f"! {_SCRIPT_NAME} narrow: no find-symbol matches to narrow, or --index out of range"
            " — answer file left unchanged\n"
        )
        return _EXIT_NARROW_UNUSABLE
    try:
        _write_sentinel_file(Path(args.input), json.dumps(narrowed))
    except OSError as exc:
        sys.stderr.write(f"{_SCRIPT_NAME}: {exc}\n")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — returns the process exit code (see module docstring)."""
    args = _build_parser().parse_args(argv)
    if args.command == "narrow":
        return _narrow_file(args)
    try:
        prefix = _validate_output_prefix(args.output_prefix)
    except ValueError as exc:
        sys.stderr.write(f"{_SCRIPT_NAME}: {exc}\n")
        return 3
    try:
        values = _compute_values(args, _load_json(args.input))
    except ValueError as exc:
        sys.stderr.write(f"! {_SCRIPT_NAME} {args.command}: {exc} — wrote fail-closed defaults\n")
        values = _fallback_values(args)
    tmpdir, csid = _resolve_tmpdir(), _resolve_csid()
    try:
        for name, value in values.items():
            _write_sentinel_file(Path(tmpdir, f"{prefix}-{name}-{csid}"), f"{value}\n")
    except OSError as exc:
        sys.stderr.write(f"{_SCRIPT_NAME}: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
