#!/usr/bin/env python3
"""Own the exhausted reverse-dependency sentinel: arm it on an exhaustive query, drop it on an edit.

Two PostToolUse roles, dispatched on ``tool_name``:

- ``Bash`` — when a ``scan-query`` or ``codemap-py query`` rdeps/fn-rdeps command's OWN result
  comes back complete, append the queried module so ``guard-redundant-scan.py`` can deny a
  redundant import grep for it.
- ``Edit`` / ``Write`` / ``MultiEdit`` / ``NotebookEdit`` / ``apply_patch`` — drop the sentinel. It asserts
  "codemap already returned the EXHAUSTIVE caller set", an authority any source edit can
  invalidate (a new import changes the caller set), so it must not outlive the tree it was
  computed from. Both roles live here because the sentinel has exactly one lifecycle owner.

Completeness is read from the queried target's OWN result, never from a substring scan of the
whole rendered response: a combined response whose unrelated sub-result carried
``query_complete: true`` used to arm the deny for a module whose own result was incomplete.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

#: Matches a Bash command that runs a codemap query (``scan-query``, ``codemap-py query`` or ``$SQ``).
_QUERY = re.compile(r"\b(?:scan-query|codemap-py\s+query)\b|\$SQ\b")
#: Captures the module or qualified name passed to a ``rdeps`` or ``fn-rdeps`` query.
_TARGET = re.compile(r"\b(?:fn-)?rdeps\s+[\"']?([A-Za-z0-9_.]+(?:::[A-Za-z0-9_.]+)?)[\"']?")
#: Tool names whose side effect can invalidate a recorded exhaustive caller set.
_EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"})
#: Suffixes whose edit can change the import graph. An edit whose path cannot be read
#: invalidates anyway — dropping a still-valid sentinel costs one re-query, while keeping
#: a stale one denies a grep the model actually needed.
_SOURCE_SUFFIXES = (".py", ".pyi")
# Claude launches this hook as `python "<plugin-root>/hooks/record-exhausted.py"`, which
# already puts hooks/ on sys.path — but the test suite loads it through
# `importlib.util.spec_from_file_location`, which does not. Inserting explicitly makes
# the shared-helper import resolve under every load mechanism.
#: Directory holding this hook, put on ``sys.path`` so the shared ``_hookutil`` helper imports under any loader.
_HOOKS_DIR = Path(__file__).resolve().parent
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

import _hookutil  # noqa: E402  (needs the sys.path insert above)

# Re-exported, not re-implemented: this hook WRITES the sentinel that
# ``guard-redundant-scan.py`` reads, so a divergence between their two copies of the key
# would not raise — it would write one file and read another, and the deny would simply
# never fire.
tmp_dir = _hookutil.tmp_dir
session_key = _hookutil.session_key


def sentinel_path(session_id: object) -> Path:
    """Return the shared per-session exhausted-query sentinel path."""
    return tmp_dir() / f"codemap-exhausted-{session_key(session_id)}"


def _loads_dict(text: object) -> dict | None:
    """Return *text* parsed as a JSON object, or ``None`` when it is not one.

    Examples:
        >>> _loads_dict('{"a": 1}')
        {'a': 1}
        >>> _loads_dict("[1, 2]") is None and _loads_dict("nope") is None
        True
    """
    if not isinstance(text, str):
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _as_payload(response: object) -> dict | None:
    """Return the scan-query JSON result carried by a PostToolUse ``tool_response``.

    Accepts every shape the host may deliver: the raw stdout string, the Bash tool's
    ``{"stdout": ...}`` envelope, or an already-parsed result. Anything else yields ``None``
    so the caller fails open instead of arming a deny on a guess.

    Examples:
        >>> _as_payload('{"module": "pkg", "index": {}}')["module"]
        'pkg'
        >>> _as_payload({"stdout": '{"module": "pkg", "index": {}}'})["module"]
        'pkg'
        >>> _as_payload({"stdout": "not json"}) is None
        True
    """
    if isinstance(response, str):
        return _loads_dict(response)
    if isinstance(response, dict):
        return response if "index" in response else _loads_dict(response.get("stdout"))
    return None


def complete_for_target(response: object, target: str) -> bool:
    """Return whether *response* is the complete result **of the queried target itself**.

    The identity check is what scopes completeness: ``rdeps`` echoes the queried module as
    ``module`` and ``fn-rdeps`` echoes the qname as ``qname``, so a response whose own
    identity does not match the command's target can never arm the guard for it.
    ``exhaustive`` is the legacy alias of ``query_complete``.

    Args:
        response: The PostToolUse ``tool_response`` in any supported shape.
        target: The module or qname the command actually queried.

    Returns:
        Whether the guard may treat *target*'s caller set as exhaustive.

    Examples:
        >>> ok = '{"module": "pkg.a", "index": {"query_complete": true}}'
        >>> complete_for_target(ok, "pkg.a")
        True
        >>> complete_for_target(ok, "pkg.other")
        False
        >>> complete_for_target('{"module": "pkg.a", "index": {"query_complete": false}}', "pkg.a")
        False
    """
    payload = _as_payload(response)
    if payload is None or target not in (payload.get("module"), payload.get("qname")):
        return False
    coverage = payload.get("index")
    if not isinstance(coverage, dict):
        return False
    if coverage.get("truncated") is True:
        return False
    return coverage.get("query_complete") is True or coverage.get("exhaustive") is True


def record(sentinel: Path, payload: dict) -> None:
    """Append the dotted and slashed module names when a query result is exhaustive."""
    command = str((payload.get("tool_input") or {}).get("command", ""))
    if not _QUERY.search(command):
        return
    target = _TARGET.search(command)
    if target is None or not complete_for_target(payload.get("tool_response"), target.group(1)):
        return
    dotted = target.group(1).split("::", 1)[0]
    with sentinel.open("a", encoding="utf-8") as stream:
        stream.write(f"{dotted}\n{dotted.replace('.', '/')}\n")


def invalidate(sentinel: Path, payload: dict) -> None:
    """Drop the sentinel after an edit that can change the import graph."""
    edited = str((payload.get("tool_input") or {}).get("file_path", ""))
    if edited and not edited.endswith(_SOURCE_SUFFIXES):
        return
    sentinel.unlink(missing_ok=True)


def main() -> int:
    """Maintain the exhausted-query sentinel for one PostToolUse event, failing open."""
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        sentinel = sentinel_path(_hookutil.runtime_session(payload))
        if str(payload.get("tool_name", "")) in _EDIT_TOOLS:
            invalidate(sentinel, payload)
        else:
            record(sentinel, payload)
    except (OSError, TypeError, ValueError):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
