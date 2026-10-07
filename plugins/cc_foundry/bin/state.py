#!/usr/bin/env python3
"""state.py — persist small shell values across Bash tool calls.

Each Bash tool call runs in a fresh shell, so a variable assigned in one
```bash block is empty when a later block references it. Skills work around this
by writing values to a temp file and re-sourcing it. This helper standardizes
that pattern behind one tested idiom instead of ad-hoc per-skill code:

    # persist (early block)
    python "${CLAUDE_PLUGIN_ROOT}/bin/state.py" set audit-$RUN RUN_DIR="$RUN_DIR" SCOPE="$SCOPE"
    # reload (later block, fresh shell)
    eval "$(python "${CLAUDE_PLUGIN_ROOT}/bin/state.py" load audit-$RUN)"

The namespace is chosen by the caller. Session scoping is automatic: the file
name embeds the session token (``CSID`` → ``CLAUDE_CODE_SESSION_ID`` → literal
``shared``), so concurrent sessions never collide on the same namespace — a
run-unique component (timestamp / run-id) is still useful to separate multiple
runs *within* one session, but no longer required for cross-session safety.
Values are stored under ``<tmp>/claude-state-<namespace>-<csid>.env`` (``<tmp>``
= ``$TMPDIR`` or the platform temp dir — never a hardcoded ``/tmp``, which does
not exist on native Windows Python). Both halves of an emitted line are
constrained so ``eval`` is injection-safe: KEY must match
``^[A-Za-z_][A-Za-z0-9_]*$`` (rejected by ``set``, skipped by ``load``) and
VALUE is single-quote-quoted with embedded quotes escaped.

Usage:
    state.py set <namespace> KEY=VALUE [KEY=VALUE ...]   # merge (create/update)
    state.py load <namespace>                            # print `KEY='VALUE'` lines for eval
    state.py clear <namespace>                           # remove the state file

Exit code 0 on success, 2 on usage error.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

#: Matches characters outside the file-name-safe set; they are replaced with ``_`` in the namespace.
_NS_SAFE = re.compile(r"[^A-Za-z0-9._-]")
#: ``load_values`` emits ``KEY='VALUE'`` for the caller's ``eval``.  KEY sits outside
#: the quoting that protects VALUE, so a metacharacter-bearing key would run as a
#: separate shell statement (CWE-78).  Restricted to shell identifiers on both the
#: write path (``set_values``) and the emit path (``load_values``).
_KEY_SAFE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _csid() -> str:
    """Return the session token for sentinel scoping (resolved at call time).

    Resolution chain: ``CSID`` (exported by the calling bash block) →
    ``CLAUDE_CODE_SESSION_ID`` (set by Claude Code) → literal ``"shared"``.
    Never derives from ``os.getppid()`` — that is the transient bash shell PID,
    not the Claude Code process, and would diverge from the bash-side token.

    Examples:
        >>> import os
        >>> _prev = os.environ.pop("CSID", None)
        >>> os.environ["CSID"] = "abc"
        >>> _csid()
        'abc'
        >>> _ = os.environ.pop("CSID")
        >>> _ = os.environ.setdefault("CSID", _prev) if _prev else None
    """
    return os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"


def state_path(namespace: str) -> Path:
    """Return the session-scoped state file path for a namespace.

    Args:
        namespace: Caller-chosen namespace (sanitized to a safe filename).

    Returns:
        Path under the platform temp dir: ``claude-state-<namespace>-<csid>.env``.

    Examples:
        >>> import os
        >>> _prev = os.environ.pop("CSID", None)
        >>> os.environ["CSID"] = "abc"
        >>> state_path("audit-123").name
        'claude-state-audit-123-abc.env'
        >>> _ = os.environ.pop("CSID")
        >>> _ = os.environ.setdefault("CSID", _prev) if _prev else None
    """
    safe = _NS_SAFE.sub("_", namespace)
    base = os.environ.get("TMPDIR") or tempfile.gettempdir()
    return Path(base) / f"claude-state-{safe}-{_csid()}.env"


def _read(path: Path) -> dict[str, str]:
    """Return existing KEY=VALUE pairs from a state file (empty if absent)."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, _, val = line.partition("=")
            values[key.strip()] = val
    return values


def set_values(namespace: str, assignments: list[str]) -> int:
    """Merge KEY=VALUE assignments into a namespace's state file.

    Args:
        namespace: Target namespace.
        assignments: Strings of the form ``KEY=VALUE`` (VALUE may be empty or contain ``=``).
            KEY must be a shell identifier — ``^[A-Za-z_][A-Za-z0-9_]*$``.

    Returns:
        0 on success, 2 if any assignment is malformed or carries an unsafe key.
    """
    path = state_path(namespace)
    values = _read(path)
    for item in assignments:
        if "=" not in item:
            sys.stderr.write(f"state.py: malformed assignment (want KEY=VALUE): {item!r}\n")
            return 2
        key, _, val = item.partition("=")
        key = key.strip()
        if not key:
            sys.stderr.write(f"state.py: empty key in {item!r}\n")
            return 2
        if not _KEY_SAFE.fullmatch(key):
            sys.stderr.write(f"state.py: unsafe key {key!r} in {item!r} — must match [A-Za-z_][A-Za-z0-9_]*\n")
            return 2
        values[key] = val
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    return 0


def load_values(namespace: str) -> int:
    """Print ``KEY='VALUE'`` lines for a namespace, safe to ``eval`` in bash.

    Single quotes inside values are escaped as ``'\\''`` so the emitted lines are
    injection-safe. The state file is not trusted to have been written by this
    module — any key failing ``^[A-Za-z_][A-Za-z0-9_]*$`` (legacy file, concurrent
    writer, hand edit) is skipped with a stderr warning rather than emitted, since
    KEY precedes the quoting that protects VALUE.

    Args:
        namespace: Namespace to load.

    Returns:
        0 always (absent file → no output).
    """
    for key, val in _read(state_path(namespace)).items():
        if not _KEY_SAFE.fullmatch(key):
            sys.stderr.write(f"state.py: skipping unsafe key {key!r} from state file\n")
            continue
        escaped = val.replace("'", "'\\''")
        print(f"{key}='{escaped}'")
    return 0


def clear(namespace: str) -> int:
    """Remove a namespace's state file (no-op if absent).

    Returns 0.
    """
    state_path(namespace).unlink(missing_ok=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Persist small shell values across Bash tool calls")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_set = sub.add_parser("set", help="merge KEY=VALUE assignments")
    p_set.add_argument("namespace")
    p_set.add_argument("assignments", nargs="+")
    p_load = sub.add_parser("load", help="print KEY='VALUE' lines for eval")
    p_load.add_argument("namespace")
    p_clear = sub.add_parser("clear", help="remove the state file")
    p_clear.add_argument("namespace")
    args = parser.parse_args(argv)

    if args.cmd == "set":
        return set_values(args.namespace, args.assignments)
    if args.cmd == "load":
        return load_values(args.namespace)
    return clear(args.namespace)


if __name__ == "__main__":
    sys.exit(main())
