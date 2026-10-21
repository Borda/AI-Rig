#!/usr/bin/env python
"""classify_breaking.py — label changed public symbols Breaking vs internal.

Reads ``codemap-py query batch`` output (a JSON object with a ``batch`` array of
``fn-rdeps`` results) on stdin and classifies each queried symbol:

- **Breaking**: at least one caller lives outside the symbol's own top-level
  package — changing or removing the symbol breaks a downstream consumer.
- **internal**: every caller (if any) lives inside the same package — the
  change is contained; no external migration needed.

Package boundary = first dotted segment of the module name (``a.b.c`` -> ``a``).
The symbol's package comes from its ``qname`` (``module::symbol``); each caller's
package comes from the caller ``module`` field. A caller whose package differs
is an external call site and is emitted as migration evidence.

A symbol whose ``fn-rdeps`` errored (removed public name — itself a Breaking
signal for a previously-public symbol) is labelled Breaking with a ``removed``
reason. When the shared coverage block reports ``query_complete: false`` the
whole result set is marked ``query_complete: false`` so callers can flag the
evidence as possibly-incomplete rather than trusting it as exhaustive.

Usage:
    codemap-py query batch queries.json | classify_breaking.py
    classify_breaking.py < batch-output.json

Exit codes:
    0 — classification emitted
    2 — stdin is not valid JSON

Output (stdout, JSON):
    {
      "breaking": [{"symbol", "package", "external_callers": [...]}],
      "internal": [{"symbol", "package", "caller_count"}],
      "query_complete": bool,
      "migration_lines": ["- `symbol` — called by `caller` (`path`)", ...]
    }

Caller pattern (classify-truth-check.md Breaking classification phase):
    BATCH_JSON=$(codemap-py query batch "$QUERIES_FILE" 2>/dev/null)
    echo "$BATCH_JSON" | classify_breaking.py > "$BREAKING_FILE"
"""

from __future__ import annotations

import argparse
import json
import sys


def _package_of(module: str) -> str:
    """Return the top-level package of a dotted module name.

    Args:
        module: Dotted module name (e.g. ``"a.b.c"``) or empty string.

    Returns:
        The first dotted segment (e.g. ``"a"``); empty string when ``module``
        is empty.

    Examples:
        >>> _package_of("mypkg.sub.mod")
        'mypkg'
        >>> _package_of("solo")
        'solo'
        >>> _package_of("")
        ''
    """
    return module.split(".", 1)[0] if module else ""


def _symbol_package(qname: str) -> str:
    """Return the owning package of a ``module::symbol`` qname.

    Args:
        qname: Fully qualified symbol name (``module::symbol``); a bare name
            with no ``::`` is treated as having no module.

    Returns:
        Top-level package of the symbol's module, or empty string.

    Examples:
        >>> _symbol_package("mypkg.sub::Thing")
        'mypkg'
        >>> _symbol_package("bare_name")
        ''
    """
    module = qname.split("::", 1)[0] if "::" in qname else ""
    return _package_of(module)


def _external_callers(callers: list[dict], own_package: str) -> list[dict]:
    """Filter callers to those outside ``own_package``.

    Args:
        callers: ``called_by`` entries, each a dict with ``caller``, ``module``,
            ``path`` keys.
        own_package: Top-level package of the changed symbol.

    Returns:
        Caller entries whose module resolves to a different top-level package.
        A caller with an empty module is treated as external (cannot be proven
        same-package).

    Examples:
        >>> _external_callers(
        ...     [{"caller": "other.m::f", "module": "other.m", "path": "o.py"}],
        ...     "mypkg",
        ... )
        [{'caller': 'other.m::f', 'module': 'other.m', 'path': 'o.py'}]
        >>> _external_callers(
        ...     [{"caller": "mypkg.m::f", "module": "mypkg.m", "path": "m.py"}],
        ...     "mypkg",
        ... )
        []
    """
    return [c for c in callers if _package_of(c.get("module", "")) != own_package]


def _classify_one(entry: dict) -> dict:
    """Classify a single ``fn-rdeps`` batch entry as breaking or internal.

    Args:
        entry: One element of the ``batch`` array — ``{ok, cmd, result, ...}``.
            ``result`` holds either an ``fn-rdeps`` payload (``qname``,
            ``called_by``) or an ``error`` string (symbol not found).

    Returns:
        A dict ``{label, record, migration}`` where ``label`` is ``"breaking"``
        or ``"internal"``, ``record`` is the classified entry, and
        ``migration`` is a list of evidence lines (empty for internal).

    Examples:
        >>> out = _classify_one(
        ...     {
        ...         "ok": True,
        ...         "result": {
        ...             "qname": "mypkg.m::Thing",
        ...             "called_by": [
        ...                 {"caller": "app.x::run", "module": "app.x", "path": "x.py"}
        ...             ],
        ...         },
        ...     }
        ... )
        >>> out["label"]
        'breaking'
        >>> out["record"]["symbol"]
        'mypkg.m::Thing'
    """
    result = entry.get("result", {})
    if not entry.get("ok", False) or "error" in result:
        qname = result.get("qname", entry.get("args", [""])[0] if entry.get("args") else "")
        record = {"symbol": qname, "package": _symbol_package(qname), "reason": "removed"}
        line = f"- `{qname}` — public symbol removed (was present, now absent from index)"
        return {"label": "breaking", "record": record, "migration": [line]}

    qname = result.get("qname", "")
    own_pkg = _symbol_package(qname)
    callers = result.get("called_by", [])
    external = _external_callers(callers, own_pkg)

    if external:
        record = {"symbol": qname, "package": own_pkg, "external_callers": external}
        lines = [f"- `{qname}` — called by `{c['caller']}` (`{c.get('path', '?')}`)" for c in external]
        return {"label": "breaking", "record": record, "migration": lines}

    record = {"symbol": qname, "package": own_pkg, "caller_count": len(callers)}
    return {"label": "internal", "record": record, "migration": []}


def classify(batch_output: dict) -> dict:
    """Classify every symbol in a ``codemap-py query batch`` output.

    Args:
        batch_output: Parsed ``codemap-py query batch`` JSON with ``batch`` and
            ``index`` fields.

    Returns:
        A dict with ``breaking``, ``internal``, ``migration_lines`` lists and a
        ``query_complete`` bool (False when the shared coverage block reports an
        incomplete traversal — evidence is then possibly-incomplete).

    Examples:
        >>> classify({"batch": [], "index": {"query_complete": True}})
        {'breaking': [], 'internal': [], 'query_complete': True, 'migration_lines': []}
    """
    breaking: list[dict] = []
    internal: list[dict] = []
    migration_lines: list[str] = []
    for entry in batch_output.get("batch", []):
        classified = _classify_one(entry)
        if classified["label"] == "breaking":
            breaking.append(classified["record"])
        else:
            internal.append(classified["record"])
        migration_lines.extend(classified["migration"])
    query_complete = bool(batch_output.get("index", {}).get("query_complete", True))
    return {
        "breaking": breaking,
        "internal": internal,
        "query_complete": query_complete,
        "migration_lines": migration_lines,
    }


def main(argv: list[str] | None = None) -> int:
    """Entry point — read batch JSON from stdin, print classification to stdout.

    Args:
        argv: Optional argv override. Ignored except for an explicit ``-h``/``--help``
            (preserves the legacy stdin-only, argv-ignored contract).

    Returns:
        ``0`` on success; ``2`` when stdin is not valid JSON.

    Examples:
        No doctest — reads stdin; covered by pytest.
    """
    # Honour only -h/--help; any other argv is ignored (legacy stdin-only contract).
    effective_argv = sys.argv[1:] if argv is None else argv
    if effective_argv in (["-h"], ["--help"]):
        argparse.ArgumentParser(
            prog="classify_breaking.py",
            description="Label changed public symbols Breaking vs internal (reads codemap-py query batch JSON on stdin).",
        ).parse_args(["-h"])  # prints help, exits 0
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    raw = sys.stdin.read()
    try:
        batch_output = json.loads(raw)
    except (json.JSONDecodeError, ValueError, RecursionError):  # deeply nested input recurses past the limit
        print(json.dumps({"error": "stdin is not valid JSON"}))
        return 2
    print(json.dumps(classify(batch_output)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
