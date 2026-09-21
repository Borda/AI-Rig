#!/usr/bin/env python
"""resolve_index_env.py — resolve codemap PROJ + INDEX and write to temp files.

Calls ``bin/resolve_proj_index.py``, reads PROJ (line 1) and INDEX (line 2),
and writes each to ``<tmpdir>/${prefix}-resolve-{proj,index}`` for the
caller to read back with ``cat`` — avoids the ``eval "$(...)"`` anti-pattern.

``CLAUDE_PLUGIN_ROOT`` is validated before use — it must resolve to the exact directory
this script itself runs from — to prevent arbitrary subprocess execution. ``TMPDIR`` is
only honoured when absolute and owned by the current user.

Usage:
    export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
    _CM_PROJ=$(git rev-parse --show-toplevel | xargs basename)
    python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/resolve_index_env.py" \\
        --output-prefix "codemap-${_CM_PROJ}"
    IFS= read -r PROJ < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-resolve-proj-${CSID}" 2>/dev/null || PROJ=""
    IFS= read -r INDEX < "${TMPDIR:-/tmp}/codemap-${_CM_PROJ}-resolve-index-${CSID}" 2>/dev/null || INDEX=""

    python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/resolve_index_env.py" --check-exists
    # exit 1 when INDEX file missing; temp files still written for diagnostics
    # (uses default prefix "codemap"; prefer ``--output-prefix`` for concurrent safety)

Flags:
    --check-exists       verify INDEX file exists; exit 1 with stderr message if missing.
    --output-prefix STR  prefix for temp file names (default: "codemap"); must match
                         [a-zA-Z0-9_.-]+, not "."/".." (no path separators). Use
                         "codemap-${_CM_PROJ}" to scope per-project and avoid concurrent
                         collisions.

Exit codes:
    0 — success (PROJ + INDEX written to temp files)
    1 — resolver produced no output, or (with ``--check-exists``) INDEX file missing
        (temp files still written so caller can read PROJ for diagnostics)
    2 — unknown flag
    3 — unsafe CLAUDE_PLUGIN_ROOT or ``--output-prefix`` (validation failure)
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path


_SCRIPT_NAME = "resolve_index_env"

# ``--output-prefix`` must be a single bare token — no path separators — blocking traversal
# out of TMPDIR (CWE-22). A leading/trailing dot-run is still rejected via the
# fullmatch below (no bare "." or ".." token), but an embedded "." is legitimate (project
# basenames like "Borda.local", "site.com" are common).
_VALID_OUTPUT_PREFIX_RE = re.compile(r"(?!\.\.?$)[a-zA-Z0-9_.-]+")


def _own_plugin_root() -> Path:
    """Return the resolved plugin root this script is itself running from (``bin/..``).

    Used as the sole trust anchor for :func:`_validate_plugin_root` — a caller-supplied
    ``CLAUDE_PLUGIN_ROOT`` is safe only if it names the very directory tree this script
    already lives in, whether that is the installed cache path or the source tree.

    Returns:
        Resolved absolute path two levels above this file (``<plugin_root>/bin/<this file>``).
    """
    return Path(__file__).resolve().parent.parent


def _validate_plugin_root(plugin_root: str) -> str:
    """Validate ``CLAUDE_PLUGIN_ROOT`` before it is used to build a subprocess path.

    An attacker-controlled ``CLAUDE_PLUGIN_ROOT`` would otherwise let
    :func:`_run_resolver` execute an arbitrary ``resolve_proj_index.py``. A regex
    on the raw, unnormalized string cannot express containment (``..`` segments are never
    collapsed) and previously both rejected the real installed path (after the
    ``codemap`` -> ``codemap-py`` rename) and accepted attacker-chosen directories whose
    path merely *looked* right. Containment is instead checked directly: the value must
    resolve to the exact directory this script itself runs from (see :func:`_own_plugin_root`).

    Args:
        plugin_root: Raw value read from ``$CLAUDE_PLUGIN_ROOT``.

    Returns:
        The validated ``plugin_root`` unchanged.

    Raises:
        ValueError: if ``plugin_root`` is empty, relative, unresolvable, or does not
            resolve to this script's own plugin root.

    Examples:
        >>> str(_validate_plugin_root(str(_own_plugin_root()))) == str(_own_plugin_root())
        True
        >>> _validate_plugin_root("/opt/evil")
        Traceback (most recent call last):
        ValueError: CLAUDE_PLUGIN_ROOT is not a safe path: '/opt/evil'
        >>> _validate_plugin_root("relative/path")
        Traceback (most recent call last):
        ValueError: CLAUDE_PLUGIN_ROOT is not a safe path: 'relative/path'
    """
    if not plugin_root or not os.path.isabs(plugin_root):
        raise ValueError(f"CLAUDE_PLUGIN_ROOT is not a safe path: {plugin_root!r}")
    try:
        resolved = Path(plugin_root).resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"CLAUDE_PLUGIN_ROOT is not a safe path: {plugin_root!r}") from exc
    if resolved != _own_plugin_root():
        raise ValueError(f"CLAUDE_PLUGIN_ROOT is not a safe path: {plugin_root!r}")
    return plugin_root


def _resolve_plugin_root() -> str:
    """Read and validate ``CLAUDE_PLUGIN_ROOT``, falling back to the in-tree default.

    The unset/empty default ``plugins/codemap-py`` is only valid when running from the
    source tree where the path is relative-trusted; any explicitly set value must pass
    :func:`_validate_plugin_root`.

    Returns:
        A validated, safe plugin-root path string.

    Raises:
        ValueError: if ``CLAUDE_PLUGIN_ROOT`` is set to an unsafe value.

    Examples:
        >>> import os
        >>> _ = os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
        >>> _resolve_plugin_root()
        'plugins/codemap-py'
    """
    raw = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if raw is None or raw == "":
        return "plugins/codemap-py"
    return _validate_plugin_root(raw)


def _resolve_csid() -> str:
    """Return the session-scope suffix for written temp filenames.

    Caller exports ``CSID`` (bash ``export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"``);
    read fresh on every call (not cached at import time) so tests can monkeypatch
    ``os.environ`` per-case. Degrades to ``"shared"`` only when the caller forgot the
    export (accepted residual collision scope, WS3 lint flags the caller).

    Returns:
        ``CSID`` env value, else ``CLAUDE_CODE_SESSION_ID``, else ``"shared"``.

    Examples:
        >>> import os
        >>> _ = os.environ.pop("CSID", None); _ = os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        >>> _resolve_csid()
        'shared'
    """
    return os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"


def _resolve_tmpdir() -> str:
    """Return a safe temp directory: ``TMPDIR`` only when absolute and owned by this user.

    An untrusted ``TMPDIR`` is a write-anywhere primitive; a directory owned by
    another user can be a symlink-swap target. When ``TMPDIR`` fails either check, fall back
    to :func:`tempfile.gettempdir`.

    Returns:
        Absolute path to a temp directory safe for this process to write into.

    Examples:
        >>> import os, tempfile
        >>> from unittest.mock import patch
        >>> with patch.dict(os.environ), patch.object(tempfile, "tempdir", tempfile.tempdir):
        ...     _ = os.environ.pop("TMPDIR", None)
        ...     _resolve_tmpdir() == tempfile.gettempdir()
        True
    """
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir and os.path.isabs(tmpdir):
        try:
            if not hasattr(os, "getuid") or Path(tmpdir).stat().st_uid == os.getuid():
                return tmpdir
        except OSError:
            pass
    return tempfile.gettempdir()


def _validate_output_prefix(prefix: str) -> str:
    """Validate the ``--output-prefix`` value against path traversal.

    A prefix containing ``/`` would escape ``TMPDIR``, and a bare ``.``/``..`` token would
    resolve to a directory rather than a filename prefix (CWE-22) — both rejected.
    An embedded ``.`` is otherwise accepted: the documented recipe builds the prefix from a
    project's git-root basename (``codemap-$(basename ...)``), and basenames containing a
    dot are common (this very repository's is ``Borda.local``).

    Args:
        prefix: Raw ``--output-prefix`` argument.

    Returns:
        The validated prefix unchanged.

    Raises:
        ValueError: if ``prefix`` is empty, ``.``, ``..``, or contains anything outside
            ``[a-zA-Z0-9_.-]``.

    Examples:
        >>> _validate_output_prefix("codemap-myproj")
        'codemap-myproj'
        >>> _validate_output_prefix("codemap-Borda.local")
        'codemap-Borda.local'
        >>> _validate_output_prefix("../escape")
        Traceback (most recent call last):
        ValueError: --output-prefix must match [a-zA-Z0-9_.-]+ (not '.'/'..', no path separators): '../escape'
        >>> _validate_output_prefix("..")
        Traceback (most recent call last):
        ValueError: --output-prefix must match [a-zA-Z0-9_.-]+ (not '.'/'..', no path separators): '..'
    """
    if not _VALID_OUTPUT_PREFIX_RE.fullmatch(prefix):
        raise ValueError(f"--output-prefix must match [a-zA-Z0-9_.-]+ (not '.'/'..', no path separators): {prefix!r}")
    return prefix


def parse_resolver_output(stdout: str) -> tuple[str, str]:
    """Extract PROJ (line 1) and INDEX (line 2) from resolver stdout.

    Trailing newlines on each line are stripped; lines beyond the second are ignored.
    Missing lines return empty strings — the caller treats either empty value as failure.

    Args:
        stdout: Raw stdout text from ``resolve_proj_index.py``.

    Returns:
        Tuple of ``(proj, index)`` strings; empty when the corresponding line is absent.

    Examples:
        >>> parse_resolver_output("myproj\\n/path/to/index.json\\n")
        ('myproj', '/path/to/index.json')
        >>> parse_resolver_output("only-one-line\\n")
        ('only-one-line', '')
        >>> parse_resolver_output("")
        ('', '')
        >>> parse_resolver_output("a\\nb\\nc\\nd\\n")
        ('a', 'b')
    """
    lines = stdout.splitlines()
    proj = lines[0] if len(lines) >= 1 else ""
    index = lines[1] if len(lines) >= 2 else ""
    return proj, index


def format_eval_line(proj: str, index: str) -> str:
    """Return a single eval-safe assignment line for ``PROJ`` and ``INDEX``.

    Uses :func:`shlex.quote` so any embedded single quotes, spaces, or shell
    metacharacters survive the round-trip through ``eval``.

    Args:
        proj: Project name string.
        index: Index file path string.

    Returns:
        Single line of the form ``PROJ=<quoted> INDEX=<quoted>`` (no trailing newline).

    Examples:
        >>> format_eval_line("myproj", "/opt/index.json")
        'PROJ=myproj INDEX=/opt/index.json'
        >>> format_eval_line("proj with space", "/opt/index.json")
        "PROJ='proj with space' INDEX=/opt/index.json"
        >>> "PROJ='proj'" in format_eval_line("proj'q", "/opt/x.json")
        True
    """
    return f"PROJ={shlex.quote(proj)} INDEX={shlex.quote(index)}"


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser for this script."""
    parser = argparse.ArgumentParser(
        prog=_SCRIPT_NAME,
        description="Resolve codemap PROJ + INDEX and emit eval-safe assignments.",
        add_help=True,
    )
    parser.add_argument(
        "--check-exists",
        action="store_true",
        help="Verify INDEX file exists; exit 1 with stderr message if missing.",
    )
    parser.add_argument(
        "--output-prefix",
        default="codemap",
        help=(
            "Prefix for temp file names (default: 'codemap'); must match [a-zA-Z0-9_.-]+, "
            "not '.'/'..' (no path separators). Use 'codemap-<proj>' to scope per-project "
            "and avoid concurrent collisions."
        ),
    )
    return parser


def _write_sentinel_file(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` without following an existing symlink.

    A predictable sentinel filename in a shared ``TMPDIR`` is a symlink-plant target —
    ``Path.write_text`` follows an existing symlink and truncates whatever it points at.
    ``O_NOFOLLOW`` makes the open fail (``ELOOP``) instead of following, and the file is
    created ``0o600`` regardless of umask (mirrors the pattern already used by
    ``anonymize.py``'s ``_load_salt``). ``O_EXCL`` is deliberately not used here — unlike a
    write-once salt file, this sentinel is legitimately overwritten on every resolver
    invocation within the same session.

    Args:
        path: Destination file path.
        content: Text to write (caller supplies any trailing newline).
    """
    if path.is_symlink():
        raise OSError(f"refusing to write through symlink: {path}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        if hasattr(os, "fchmod"):
            os.fchmod(fh.fileno(), 0o600)
        fh.write(content)


def _write_temp_vars(proj: str, index: str, prefix: str = "codemap") -> None:
    """Write PROJ and INDEX to ``<tmpdir>/${prefix}-resolve-{proj,index}-${CSID}`` temp files.

    Callers read back with ``cat`` — avoids the ``eval "$(...)"`` anti-pattern. The
    ``-{_CSID}`` terminal suffix scopes the filename to the calling session, so concurrent
    sessions in the same project never collide (see module-level ``_CSID``). Temp files are
    always written (even on resolver failure) so downstream ``cat`` calls can supply their
    own ``|| echo ""`` fallback without extra conditionals. The temp directory is resolved
    via :func:`_resolve_tmpdir` (owner-checked ``TMPDIR``); each file is written via
    :func:`_write_sentinel_file` (symlink-safe, ``0o600``).

    Args:
        proj: Project name string (may be empty on resolver failure or to clear stale state).
        index: Index file path string (may be empty on resolver failure or to clear stale state).
        prefix: Validated temp file name prefix (default: ``"codemap"``). Pass
            ``"codemap-<proj>"`` to scope per-project and avoid concurrent collisions.
    """
    tmpdir = _resolve_tmpdir()
    csid = _resolve_csid()
    for key, val in (("proj", proj), ("index", index)):
        _write_sentinel_file(Path(tmpdir, f"{prefix}-resolve-{key}-{csid}"), f"{val}\n")


def _run_resolver(plugin_root: str) -> str:
    """Invoke ``resolve_proj_index.py`` via subprocess and return its stdout.

    Args:
        plugin_root: Validated plugin root directory (see :func:`_validate_plugin_root`).

    Returns:
        Captured stdout text. Empty string on subprocess failure.
    """
    resolver = str(Path(plugin_root) / "bin" / "resolve_proj_index.py")
    try:
        result = subprocess.run(
            [sys.executable, resolver],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout


def main(argv: list[str] | None = None) -> int:
    """CLI entry point - returns the process exit code.

    Always writes PROJ/INDEX to temp files before any failure exit so callers
    can read partial results for diagnostics even when the script exits non-zero.

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Exit code — 0 success, 1 resolver/check failure, 2 unknown flag, 3 unsafe input.
    """
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse exits 2 on unknown flags with its own stderr message.
        # Re-emit a stable, prefixed error line to match the legacy bash contract.
        if exc.code == 2:
            unknown = argv or sys.argv[1:]
            offending = next((a for a in unknown if a.startswith("-")), "")
            sys.stderr.write(f"{_SCRIPT_NAME}: unknown flag: {offending}\n")
            return 2
        return int(exc.code) if exc.code is not None else 0

    # ``--output-prefix`` determines the sentinel filenames, so it must validate first.
    try:
        prefix = _validate_output_prefix(args.output_prefix)
    except ValueError as exc:
        sys.stderr.write(f"{_SCRIPT_NAME}: {exc}\n")
        return 3

    # Clear any prior run's PROJ/INDEX for this prefix before validating the rest of the
    # untrusted input. Without this, a plugin-root validation failure below used to leave a
    # stale, unrelated-project's sentinel readable — the exit code is discarded by the
    # documented shell consumer, whose only liveness check is `[ -n "$PROJ" ]`.
    _write_temp_vars("", "", prefix=prefix)

    try:
        plugin_root = _resolve_plugin_root()
    except ValueError as exc:
        sys.stderr.write(f"{_SCRIPT_NAME}: {exc}\n")
        return 3

    stdout = _run_resolver(plugin_root)
    proj, index = parse_resolver_output(stdout)

    # Always write to temp files before any failure exit — callers read with cat.
    _write_temp_vars(proj, index, prefix=prefix)

    if not proj or not index:
        sys.stderr.write(f"{_SCRIPT_NAME}: resolve_proj_index.py produced no output (PROJ/INDEX empty)\n")
        return 1

    if args.check_exists and not Path(index).is_file():
        sys.stderr.write(f"{_SCRIPT_NAME}: INDEX file not found: {index}\n")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
