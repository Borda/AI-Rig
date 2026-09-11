#!/usr/bin/env python
"""parse_target_qname.py — split a ``module::function`` suspect out of a skill's arguments.

Extracted from ``skills/fix/SKILL.md`` Step 1. Persists the codemap route and the three target
fields as session sentinels, because shell variables do not survive between Bash tool calls.

The ``--`` separator at the call site is load-bearing: ``$ARGUMENTS`` routinely starts with a
flag (``--issue 42``), which argparse would otherwise read as an unknown option.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/parse_target_qname.py" [--query-kind KIND] -- "$ARGUMENTS"

Sentinels written (newline-terminated — the ``IFS= read -r`` reload contract needs it):
    ${TMPDIR}/dev-fix-codemap-query-kind-${CSID}
    ${TMPDIR}/dev-fix-target-module-${CSID}
    ${TMPDIR}/dev-fix-target-fn-${CSID}
    ${TMPDIR}/dev-fix-target-qualified-${CSID}

Exit codes:
    0 — sentinels written
    1 — sentinel write failed
    2 — argument error (argparse default)
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

#: Same shape as the inline ``grep -oE`` it replaces: dotted module, ``::``, bare function name.
_QNAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*::[A-Za-z_][A-Za-z0-9_]*")

#: Unset or unknown route falls back to the legacy batch (contract in _shared/codemap-context.md).
_DEFAULT_QUERY_KIND = "standard"


def split_qname(arguments: str) -> tuple[str, str, str]:
    """Pull the first ``module::function`` token out of an argument string.

    Args:
        arguments: Raw skill argument string, possibly carrying flags and prose.

    Returns:
        ``(module, function, qualified)``; three empty strings when no token is present.

    Examples:
        >>> split_qname("pkg.mod::do_thing --issue 42")
        ('pkg.mod', 'do_thing', 'pkg.mod::do_thing')
        >>> split_qname("--issue 42")
        ('', '', '')
        >>> split_qname("a.b::f and c.d::g")
        ('a.b', 'f', 'a.b::f')
    """
    match = _QNAME_RE.search(arguments)
    if not match:
        return "", "", ""
    qualified = match.group(0)
    module, _, function = qualified.partition("::")
    return module, function, qualified


def _sentinel_dir() -> Path:
    """Return the session temp directory (never a hardcoded ``/tmp`` — absent on native Windows)."""
    return Path(os.environ.get("TMPDIR") or tempfile.gettempdir())


def _session_token() -> str:
    """Return the sentinel session suffix; ``os.getppid()`` would name the calling shell, not the session."""
    return os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"


def main(argv: list[str] | None = None) -> int:
    """Resolve the codemap route plus target fields and persist them for later Bash calls.

    Args:
        argv: Optional argv override (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` on success, ``1`` when a sentinel cannot be written.
    """
    parser = argparse.ArgumentParser(
        prog="parse_target_qname.py",
        description="Split a module::function suspect out of a skill's arguments.",
    )
    parser.add_argument("arguments", nargs="?", default="", help="Raw $ARGUMENTS string.")
    parser.add_argument(
        "--query-kind",
        default="",
        help=f"Codemap route decided by the skill; empty fails safe to {_DEFAULT_QUERY_KIND}.",
    )
    args = parser.parse_args(argv)

    query_kind = args.query_kind.strip()
    if not query_kind:
        query_kind = _DEFAULT_QUERY_KIND
        print(f"! CODEMAP_QUERY_KIND unresolved — using {_DEFAULT_QUERY_KIND} structural context")

    module, function, qualified = split_qname(args.arguments)

    tmp = _sentinel_dir()
    csid = _session_token()
    payload = {
        "codemap-query-kind": query_kind,
        "target-module": module,
        "target-fn": function,
        "target-qualified": qualified,
    }
    for name, value in payload.items():
        sentinel = tmp / f"dev-fix-{name}-{csid}"
        try:
            sentinel.write_text(value + "\n", encoding="utf-8", newline="\n")
        except OSError as exc:
            print(f"parse_target_qname: cannot write {sentinel}: {exc}", file=sys.stderr)
            return 1

    print(f"route={query_kind} module={module or '-'} fn={function or '-'} qualified={qualified or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
