#!/usr/bin/env python
"""setup_scan_env.py — derive scan-codebase setup state in one place.

Consolidates the per-invocation setup previously inlined in scan-codebase/SKILL.md:

1. Derive ``PROJ_SLUG`` (hostname short-name + repo basename, alphanumeric-safe).
2. Validate the ``scan-index`` binary exists at ``$CLAUDE_PLUGIN_ROOT/bin/scan-index``.
3. Run ``parse_scan_args`` against the raw ``$ARGUMENTS`` string and capture the
   resulting ``--root <quoted> [--incremental]`` token list.
4. Derive ``PROJ_NAME`` — basename of the ``--root`` value when ``--root`` is present,
   otherwise basename of the git toplevel (or the cwd when outside a repo).
5. Drop a sentinel tmpfile when ``--incremental`` was requested but no prior index
   exists, so Step 2 can report the silent full-scan fallback.
6. Write a sourceable ``KEY=VAL`` state file and the individual per-``PROJ_SLUG``
   tmpfiles consumed by the second Step 1 block + Step 2.

Usage:
    python setup_scan_env.py --arguments "$ARGUMENTS"

Exit codes:
    0  success — state file path on stdout
    1  scan-index binary missing (message on stderr)
    2  parse_scan_args failed (message on stderr)
    3  bad CLI arguments (message on stderr)

This module replaces the former ``setup_scan_env.sh``, which now survives only as a
thin delegating shim so that pre-existing bash call sites keep working. The port
exists because ``.sh`` does not execute on Windows; everything here is stdlib-only
and avoids POSIX-only tooling (``hostname -s``, ``tr``, ``mktemp``, ``stat``).
"""

from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType

_EXIT_OK = 0
_EXIT_NO_SCAN_BIN = 1
_EXIT_PARSE_FAILED = 2
_EXIT_BAD_ARGS = 3

_PROG = "setup_scan_env.py"

# Field order of the sourceable state file — pinned by tests and by the SKILL.md
# blocks that `source` it.
_STATE_FIELDS = ("PROJ_SLUG", "SCAN_BIN", "SCAN_ARGS_RAW", "PROJ_NAME")


class _BadArgs(Exception):
    """Raised for a malformed command line; carries the exact stderr message."""


def _parse_cli(argv: list[str]) -> str:
    """Extract the ``--arguments`` value from the command line.

    Hand-rolled rather than argparse: the value is a raw ``$ARGUMENTS`` blob that
    routinely starts with ``--``, which argparse would treat as a flag.

    Args:
        argv: Argument list without the program name.

    Returns:
        The raw ``$ARGUMENTS`` string; empty when the flag was never supplied.

    Raises:
        _BadArgs: On an unknown flag or a bare ``--arguments`` with no value.
    """
    arguments = ""
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--arguments":
            if index + 1 >= len(argv):
                raise _BadArgs(f"{_PROG}: --arguments needs a value")
            arguments = argv[index + 1]
            index += 2
        elif token.startswith("--arguments="):
            arguments = token[len("--arguments=") :]
            index += 1
        else:
            raise _BadArgs(f"{_PROG}: unknown argument: {token}")
    return arguments


def _load_parse_scan_args(parse_bin: Path) -> ModuleType:
    """Import ``parse_scan_args.py`` from this script's own ``bin/`` directory.

    Loading by path (rather than by ``sys.path`` manipulation) removes the former
    dependency on a ``python3`` subprocess. The path is always this script's own
    sibling — never derived from ``$CLAUDE_PLUGIN_ROOT`` — because an untrusted env
    var reaching ``exec_module`` is arbitrary code execution (ASEC3).

    Args:
        parse_bin: Path to ``parse_scan_args.py`` next to this script.

    Returns:
        The executed module object.

    Raises:
        ImportError: When the file cannot be turned into a loadable module spec.
    """
    spec = importlib.util.spec_from_file_location("_codemap_parse_scan_args", parse_bin)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {parse_bin}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sanitize_slug(value: str) -> str:
    """Keep only ASCII alphanumerics and dashes, mirroring ``tr -cd '[:alnum:]-'``.

    Args:
        value: Raw hostname or directory basename.

    Returns:
        The filtered string, possibly empty.
    """
    return "".join(char for char in value if char == "-" or (char.isascii() and char.isalnum()))


def _short_hostname() -> str:
    """Return the host short-name, the portable equivalent of ``hostname -s``.

    Returns:
        Hostname with any DNS domain suffix removed; empty string when unavailable.
    """
    try:
        return socket.gethostname().split(".")[0]
    except OSError:
        return ""


def _repo_root() -> str:
    """Return the git toplevel, falling back to the current directory.

    Returns:
        Absolute path string. The cwd is used both when git exits non-zero (not a
        repository) and when git is not installed at all.
    """
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, ValueError):
        return os.getcwd()
    if completed.returncode != 0:
        return os.getcwd()
    toplevel = completed.stdout.strip()
    return toplevel or os.getcwd()


def _reject_tmpdir(reason: str) -> None:
    """Report a rejected ``TMPDIR`` and drop it from the environment.

    Removing the variable matters twice over: ``tempfile.gettempdir()`` would
    otherwise re-read the very value just rejected, and any child process would
    inherit it.

    Args:
        reason: Full stderr line explaining the rejection.
    """
    print(reason, file=sys.stderr)
    os.environ.pop("TMPDIR", None)


def _resolve_tmpdir() -> Path:
    """Resolve the base directory for state files, validating ``TMPDIR`` first.

    An attacker-controlled ``TMPDIR`` could redirect state writes outside
    the expected directories, so a supplied value must be both absolute and owned
    by the current user. A rejected value falls back to the platform temp dir.

    Returns:
        Directory in which every tmpfile and the state file are created.
    """
    raw = os.environ.get("TMPDIR") or ""
    if raw and not os.path.isabs(raw):
        _reject_tmpdir(f"{_PROG}: TMPDIR is not an absolute path — ignoring: {raw}")
        raw = ""
    if raw and hasattr(os, "getuid"):
        raw = _reject_foreign_owner(raw)
    return Path(raw) if raw else Path(tempfile.gettempdir())


def _reject_foreign_owner(raw: str) -> str:
    """Blank out ``TMPDIR`` when it is not owned by the current user.

    An unreadable path leaves the value untouched — the later write fails loudly
    on its own rather than being silently redirected here.

    Args:
        raw: Absolute ``TMPDIR`` value already validated for absoluteness.

    Returns:
        ``raw`` when the owner check passes or cannot be performed, else ``""``.
    """
    try:
        owner_uid = os.stat(raw).st_uid
    except OSError:
        return raw
    current_uid = os.getuid()
    if owner_uid == current_uid:
        return raw
    _reject_tmpdir(f"{_PROG}: TMPDIR owner UID ({owner_uid}) != current UID ({current_uid}) — ignoring: {raw}")
    return ""


def _write_atomic(target: Path, content: str) -> None:
    """Write ``content`` to ``target`` via a temp file plus an atomic rename.

    CWE-377: writing straight to the target would follow a pre-planted symlink and
    truncate whatever it points at. ``os.replace`` swaps the destination inode
    outright instead of writing through it, and the byte content is written with no
    trailing newline because the consumers compare it exactly.

    Args:
        target: Final path to create or replace.
        content: Exact bytes (as text) to store.
    """
    handle, tmp_name = tempfile.mkstemp(prefix=".codemap-setup-tmp.", dir=str(target.parent))
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(content)
    os.replace(tmp_name, target)


def _write_handoff_tmpfiles(tmpdir: Path, csid: str, state: dict[str, str]) -> None:
    """Write the per-``PROJ_SLUG`` tmpfiles consumed by later skill steps.

    Both a PID-qualified set (so concurrent same-project scans cannot race on shared
    state) and a non-PID set (for callers predating that change) are produced.

    Args:
        tmpdir: Validated base temp directory.
        csid: Session-scope suffix.
        state: Mapping with the four ``_STATE_FIELDS`` keys.
    """
    slug = state["PROJ_SLUG"]
    for qualifier in (f"-{os.getpid()}", ""):
        _write_atomic(tmpdir / f"codemap-proj-slug{qualifier}-{csid}", slug)
        _write_atomic(tmpdir / f"codemap-scan-bin-{slug}{qualifier}-{csid}", state["SCAN_BIN"])
        _write_atomic(tmpdir / f"codemap-scan-args-{slug}{qualifier}-{csid}", state["SCAN_ARGS_RAW"])
        _write_atomic(tmpdir / f"codemap-proj-name-{slug}{qualifier}-{csid}", state["PROJ_NAME"])


def _mark_incremental_noop(arguments: str, tmpdir: Path, slug: str, proj_name: str, csid: str) -> None:
    """Drop the full-scan-fallback sentinel when ``--incremental`` cannot apply.

    The surrounding spaces make this a whole-token match, so ``--incremental-foo``
    does not trigger it.

    Args:
        arguments: Raw ``$ARGUMENTS`` string.
        tmpdir: Validated base temp directory.
        slug: Derived ``PROJ_SLUG``.
        proj_name: Derived ``PROJ_NAME``, naming the expected index file.
        csid: Session-scope suffix.
    """
    if f" {arguments} ".find(" --incremental ") < 0:
        return
    # Anchored at the git root, matching resolve_proj_index.py's own
    # <git-root-or-cwd>/.cache/codemap/<proj>.json layout — a bare relative path here
    # would report "no prior index" whenever this script runs from a subdirectory.
    index_dir = os.environ.get("CODEMAP_INDEX_DIR") or os.path.join(_repo_root(), ".cache/codemap")
    if Path(index_dir, f"{proj_name}.json").is_file():
        return
    # stderr — keeps stdout reserved for the state file path so the caller can
    # capture only that path.
    print("[codemap] No prior index: falling back to full scan", file=sys.stderr)
    (tmpdir / f"codemap-incremental-noop-{slug}-{csid}").touch()


def _escape_single_quotes(value: str) -> str:
    r"""Escape a value for embedding inside single quotes in a sourced shell file.

    Args:
        value: Raw value.

    Returns:
        Value with every ``'`` replaced by the standard ``'\''`` shell idiom.

    Examples:
        >>> _escape_single_quotes("plain")
        'plain'
        >>> _escape_single_quotes("it's")
        "it'\\''s"
    """
    return value.replace("'", "'\\''")


def _write_state_file(tmpdir: Path, state: dict[str, str]) -> Path:
    """Write the sourceable ``KEY='value'`` state file and return its path.

    The caller ``source``s this file, which makes any integrity gap a code-execution
    hole rather than a data-corruption one. ``mkstemp`` creates it atomically
    (``O_CREAT|O_EXCL``, mode 0600) under an unguessable name, so a co-located
    attacker in a shared temp dir cannot pre-plant a symlink at the exact target;
    the caller additionally asserts ownership and non-symlink-ness before sourcing.

    Args:
        tmpdir: Validated base temp directory.
        state: Mapping with the four ``_STATE_FIELDS`` keys.

    Returns:
        Path of the created state file.
    """
    handle, name = tempfile.mkstemp(prefix="codemap-scan-state-", dir=str(tmpdir))
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        for field in _STATE_FIELDS:
            stream.write(f"{field}='{_escape_single_quotes(state[field])}'\n")
    return Path(name)


def _derive_scan_args(plugin_root: Path, arguments: str) -> tuple[str, str | None]:
    """Parse ``$ARGUMENTS`` into the quoted token line plus the raw ``--root`` value.

    Args:
        plugin_root: Resolved ``$CLAUDE_PLUGIN_ROOT`` — unused here (ASEC3). Left in
            the signature to keep this fix a one-line change; ``main()`` still needs
            its own ``plugin_root`` for the unrelated ``scan-index`` existence check.
        arguments: Raw ``$ARGUMENTS`` string.

    Returns:
        Tuple of ``(SCAN_ARGS_RAW, root_value_or_None)``.
    """
    # CLAUDE_PLUGIN_ROOT is untrusted input, used only for bookkeeping (the scan-index
    # existence check in main()) — the module actually executed via exec_module must
    # always come from this script's own installation, never from caller-supplied
    # plugin_root, or an attacker-controlled env var becomes RCE (ASEC3).
    module = _load_parse_scan_args(Path(__file__).resolve().parent / "parse_scan_args.py")
    tokens: list[str] = module.parse_scan_args(arguments)
    root = tokens[1] if tokens[:1] == ["--root"] else None
    return module.format_scan_args(tokens), root


def _derive_identity(arguments: str, plugin_root: Path) -> tuple[str, str, str] | None:
    """Derive ``SCAN_ARGS_RAW``, ``PROJ_SLUG`` and ``PROJ_NAME``.

    Args:
        arguments: Raw ``$ARGUMENTS`` string.
        plugin_root: Resolved ``$CLAUDE_PLUGIN_ROOT`` — forwarded to
            ``_derive_scan_args`` but no longer read there (ASEC3); see that
            function's docstring.

    Returns:
        Tuple of ``(scan_args_raw, proj_slug, proj_name)``, or ``None`` when the
        argument parser could not be loaded or run.
    """
    try:
        scan_args_raw, root = _derive_scan_args(plugin_root, arguments)
    except Exception:  # noqa: BLE001 — any parser failure maps to the single exit-2 contract
        return None
    repo_root = _repo_root()
    proj_slug = f"{_sanitize_slug(_short_hostname())}-{_sanitize_slug(Path(repo_root).name)}"
    # "." is what an absent ``--root`` resolves to, and ``--root .`` is deliberately treated
    # the same way: basename(".") is empty, which would produce a nameless index.
    proj_name = Path(repo_root).name if root in (None, ".") else Path(root).name
    return scan_args_raw, proj_slug, proj_name


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: derive setup state, write the handoff files, print the path.

    Args:
        argv: Optional argv override for testing. Defaults to ``sys.argv[1:]``.

    Returns:
        Exit code — 0 success, 1 missing ``scan-index``, 2 parse failure, 3 bad CLI.

    Examples:
        Invoked from a shell, the only stdout is the state-file path::

            $ python setup_scan_env.py --arguments "--root . --incremental"
            /tmp/codemap-scan-state-8f3a1c
    """
    try:
        arguments = _parse_cli(sys.argv[1:] if argv is None else argv)
    except _BadArgs as exc:
        print(str(exc), file=sys.stderr)
        return _EXIT_BAD_ARGS

    plugin_root = Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or "plugins/codemap-py")
    scan_bin = plugin_root / "bin" / "scan-index"
    if not os.access(scan_bin, os.X_OK):
        print(
            f"! scan-index binary not found at {scan_bin} — reinstall: claude plugin install codemap-py@borda-ai-rig",
            file=sys.stderr,
        )
        return _EXIT_NO_SCAN_BIN

    identity = _derive_identity(arguments, plugin_root)
    if identity is None:
        print("! parse_scan_args.py failed — check Python availability and plugin installation", file=sys.stderr)
        return _EXIT_PARSE_FAILED
    scan_args_raw, proj_slug, proj_name = identity

    tmpdir = _resolve_tmpdir()
    # Session-scope suffix — the caller exports CSID before invoking; the "shared"
    # fallback covers a caller that forgot. Deliberately NOT the repo-wide
    # CLAUDE_CODE_SESSION_ID chain: this script reads only what its caller sets.
    csid = os.environ.get("CSID") or "shared"

    state = {
        "PROJ_SLUG": proj_slug,
        "SCAN_BIN": str(scan_bin),
        "SCAN_ARGS_RAW": scan_args_raw,
        "PROJ_NAME": proj_name,
    }
    _write_handoff_tmpfiles(tmpdir, csid, state)
    _mark_incremental_noop(arguments, tmpdir, proj_slug, proj_name, csid)
    print(_write_state_file(tmpdir, state))
    return _EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
