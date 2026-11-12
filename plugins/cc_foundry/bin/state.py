#!/usr/bin/env python3
"""Persist supported workflow values across Bash tool calls.

Each Bash tool call runs in a fresh shell, so a variable assigned in one
```bash block is empty when a later block references it. Skills work around this
by writing values to a temp file and re-sourcing it. This helper standardizes
that pattern behind one tested idiom instead of ad-hoc per-skill code:

    # persist (early block)
    python "${CLAUDE_PLUGIN_ROOT}/bin/state.py" set audit-$RUN RUN_DIR="$RUN_DIR" SCOPE="$SCOPE"
    # reload (later block, fresh shell)
    eval "$(python "${CLAUDE_PLUGIN_ROOT}/bin/state.py" load audit-$RUN)"

The namespace is chosen by the caller. Session scoping is automatic: the file
name embeds the sanitized session token (``CSID`` → ``CLAUDE_CODE_SESSION_ID`` → literal
``shared``); callers without a session token must use a run-unique namespace to
separate runs belonging to the same user. Values are stored at mode 0600 under
``<tmp>/claude-shell-state-<user>/claude-state-<namespace>-<csid>.env`` in a private 0700 directory (``<tmp>``
= ``$TMPDIR`` or the platform temp dir — never a hardcoded ``/tmp``, which does
not exist on native Windows Python). Both halves of an emitted line are
constrained so ``eval`` is injection-safe: KEY must match
``^[A-Za-z_][A-Za-z0-9_]*$`` and be one of the supported workflow keys in ``_KEY_ALLOWED_NAMES`` —
other keys are rejected by ``set`` and skipped by ``load`` — and VALUE is single-quote-quoted with
embedded quotes escaped. ``set`` also rejects a VALUE holding a line break or NUL, since
the state file is line-oriented and such a value would be re-read as a second
``KEY=VALUE`` line that ``load`` then emits.

Usage:
    state.py set <namespace> KEY=VALUE [KEY=VALUE ...]   # merge (create/update)
    state.py load <namespace>                            # print `KEY='VALUE'` lines for eval
    state.py clear <namespace>                           # remove the state file

Exit code 0 on success, 2 on usage or unsafe-storage error.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
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
#: Characters ``str.splitlines`` treats as a line boundary, plus NUL.  A value carrying one
#: would be read back as a second ``KEY=VALUE`` line, so ``set_values`` refuses it.
_LINE_BREAKS = re.compile(r"[\r\n\x00\x0b\x0c\x1c-\x1e\x85\N{LINE SEPARATOR}\N{PARAGRAPH SEPARATOR}]")
#: Only documented workflow values are restored; shell, loader and tool configuration names are never emitted.
_KEY_ALLOWED_NAMES = frozenset(
    {
        "RUN_DIR",
        "SCOPE",
    }
)


def _key_denied(key: str) -> bool:
    """Report whether a key is outside the workflow values that may be restored through ``eval``.

    Args:
        key: A shell identifier that already passed ``_KEY_SAFE``.

    Returns:
        True for every name not in ``_KEY_ALLOWED_NAMES``, including shell and loader variables such as ``PATH``,
        ``IFS``, ``BASH_ENV`` and ``LD_PRELOAD``.

    Examples:
        >>> _key_denied("PATH"), _key_denied("LD_PRELOAD"), _key_denied("RUN_DIR")
        (True, True, False)
        >>> _key_denied("PATHS")
        True
    """
    return key not in _KEY_ALLOWED_NAMES


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
    token = os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"
    return _NS_SAFE.sub("_", token)


def _private_dir() -> Path:
    """Create or validate the current user's private temporary state directory."""
    tmp_fallback = tempfile.gettempdir() if os.name == "nt" else "/tmp"  # noqa: S108 - shell default of ${TMPDIR:-/tmp}
    base = Path(os.environ.get("TMPDIR") or tmp_fallback)
    user = str(os.getuid()) if hasattr(os, "getuid") else hashlib.sha256(str(Path.home()).encode()).hexdigest()[:16]
    directory = base / f"claude-shell-state-{user}"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise OSError(f"unsafe state directory: {directory}")
    if hasattr(os, "getuid") and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise OSError(f"state directory must be owned by this user with mode 0700: {directory}")
    return directory


def state_path(namespace: str) -> Path:
    """Return the session-scoped state file path for a namespace.

    Args:
        namespace: Caller-chosen namespace (sanitized to a safe filename).

    Returns:
        Path inside the private per-user temporary directory, created and checked here.

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
    return _private_dir() / f"claude-state-{safe}-{_csid()}.env"


def _check_file(info: os.stat_result) -> None:
    """Refuse non-regular, multiply-linked or foreign-owned state files before use."""
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise OSError("state file must be a regular file with one link")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise OSError("state file must be owned by this user")


def _open_state(path: Path, *, create: bool = False) -> int:
    """Open a regular state file without following links or truncating unvalidated data."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        if not create:
            raise
        before = None
    if before is not None:
        _check_file(before)
    flags = os.O_RDWR if create else os.O_RDONLY
    if before is None:
        flags |= os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), 0o600)
    try:
        opened = os.fstat(descriptor)
        _check_file(opened)
        if before is not None and (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError("state file changed while opening")
    except OSError:
        os.close(descriptor)
        raise
    return descriptor


def _read(path: Path) -> dict[str, str]:
    """Return existing KEY=VALUE pairs from a state file (empty if absent)."""
    values: dict[str, str] = {}
    try:
        descriptor = _open_state(path)
    except FileNotFoundError:
        return values
    with os.fdopen(descriptor, encoding="utf-8") as handle:
        text = handle.read()
    for line in text.splitlines():
        if "=" in line:
            key, _, val = line.partition("=")
            values[key.strip()] = val
    return values


def _assignment_problem(item: str) -> str | None:
    """Describe why a ``KEY=VALUE`` argument cannot be stored, or return None when it can.

    Args:
        item: One command-line assignment.

    Returns:
        A stderr-ready message for a malformed assignment, empty or unsafe or reserved key, or a
        value carrying a line break or NUL; None when the assignment is acceptable.

    Examples:
        >>> _assignment_problem("RUN_DIR=.reports/x") is None
        True
        >>> _assignment_problem("NOEQUALS")
        "malformed assignment (want KEY=VALUE): 'NOEQUALS'"
        >>> _assignment_problem("PATH=/evil")
        "reserved key 'PATH' in 'PATH=/evil' — shell and loader variables cannot be restored through eval"
        >>> _assignment_problem("RUN_DIR=x\\nB=1")
        "value of 'RUN_DIR' contains a line break or NUL"
    """
    if "=" not in item:
        return f"malformed assignment (want KEY=VALUE): {item!r}"
    key, _, val = item.partition("=")
    key = key.strip()
    if not key:
        return f"empty key in {item!r}"
    if not _KEY_SAFE.fullmatch(key):
        return f"unsafe key {key!r} in {item!r} — must match [A-Za-z_][A-Za-z0-9_]*"
    if _key_denied(key):
        return f"reserved key {key!r} in {item!r} — shell and loader variables cannot be restored through eval"
    if _LINE_BREAKS.search(val):
        return f"value of {key!r} contains a line break or NUL"
    return None


def set_values(namespace: str, assignments: list[str]) -> int:
    """Merge KEY=VALUE assignments into a namespace's state file.

    Args:
        namespace: Target namespace.
        assignments: Strings of the form ``KEY=VALUE`` (VALUE may be empty or contain ``=``).
            KEY must be a shell identifier — ``^[A-Za-z_][A-Za-z0-9_]*$``.

    Returns:
        0 on success, 2 if any assignment is malformed, carries an unsafe or reserved key, or holds
        a value with a line break or NUL.
    """
    for item in assignments:
        problem = _assignment_problem(item)
        if problem is not None:
            sys.stderr.write(f"state.py: {problem}\n")
            return 2
    try:
        path = state_path(namespace)
        values = _read(path)
        for item in assignments:
            key, _, val = item.partition("=")
            values[key.strip()] = val
        with os.fdopen(_open_state(path, create=True), "w", encoding="utf-8") as handle:
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), 0o600)
            else:
                os.chmod(path, 0o600)
            handle.write("".join(f"{k}={v}\n" for k, v in values.items()))
            handle.truncate()
    except (OSError, UnicodeError) as exc:
        sys.stderr.write(f"state.py: cannot store state: {exc}\n")
        return 2
    return 0


def load_values(namespace: str) -> int:
    """Print ``KEY='VALUE'`` lines for a namespace, safe to ``eval`` in bash.

    Single quotes inside values are escaped as ``'\\''`` so the emitted lines are
    injection-safe. The state file is not trusted to have been written by this
    module — any key failing ``^[A-Za-z_][A-Za-z0-9_]*$`` (legacy file, concurrent
    writer, hand edit) is skipped with a stderr warning rather than emitted, since
    KEY precedes the quoting that protects VALUE. Keys naming shell or loader
    variables or names outside ``_KEY_ALLOWED_NAMES`` are skipped the same way.

    Args:
        namespace: Namespace to load.

    Returns:
        0 on success (absent file → no output), 2 for unsafe or unreadable storage.
    """
    try:
        values = _read(state_path(namespace))
    except (OSError, UnicodeError) as exc:
        sys.stderr.write(f"state.py: cannot load state: {exc}\n")
        return 2
    for key, val in values.items():
        if not _KEY_SAFE.fullmatch(key):
            sys.stderr.write(f"state.py: skipping unsafe key {key!r} from state file\n")
            continue
        if _key_denied(key):
            sys.stderr.write(f"state.py: skipping reserved key {key!r} from state file\n")
            continue
        escaped = val.replace("'", "'\\''")
        print(f"{key}='{escaped}'")
    return 0


def clear(namespace: str) -> int:
    """Remove a namespace's state file (no-op if absent).

    Returns 0 on success or 2 for unsafe or unwritable storage.
    """
    try:
        state_path(namespace).unlink(missing_ok=True)
    except OSError as exc:
        sys.stderr.write(f"state.py: cannot clear state: {exc}\n")
        return 2
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
