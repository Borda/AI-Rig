#!/usr/bin/env python
"""jq_write.py — atomic ``jq`` edit of a JSON file via temp-file + rename.

Mirrors the original ``jq-write.sh`` interface: read ``<target>``, apply
``<jq-filter>`` with optional ``--arg <name> <value>`` / ``--slurpfile <name> <file>``
pairs, write to a uniquely named temp file beside the file ``<target>`` resolves to, then
``os.replace`` it over that file, keeping its permission bits. The unique name keeps two
concurrent writers from truncating each other's temp file; the atomic rename ensures
readers never see a half-written file, and a filter that prints nothing never replaces
the target with an empty file. A symlinked ``<target>`` stays a link and its destination is
updated, as in ``merge_settings_permissions.py``.

Usage:
    jq_write.py <target-file> <jq-filter> [--arg <name> <value>]... [--slurpfile <name> <file>]...

Write roots: the current working directory, ``TMPDIR``, and JSON files sitting directly
in ``~/.claude/`` (``settings.json``, ``settings.local.json``) — the user settings
``/foundry:setup`` edits. Deeper ``~/.claude/`` paths, such as the plugin cache, stay
outside. ``--slurpfile`` sources must resolve under cwd, ``TMPDIR`` or ``~/.claude/``
(a plugin's shipped ``.claude-plugin/*.json`` lives in the plugin cache there).

Exit codes:
    0 — success
    1 — target file missing
    2 — jq subprocess error (non-zero exit, jq not on PATH, or empty output)
    3 — bad args (odd number of ``--arg``/``--slurpfile`` trailing tokens, or no target/filter)
    4 — target outside allowed write roots, or ``--slurpfile`` source outside read roots
"""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
import tempfile
from contextlib import suppress
from pathlib import Path

#: The only options accepted in the jq argument list, each with its operand count, to block option injection.
#: A token that is neither one of these nor an operand of one is rejected, dash-prefixed or not: single-dash
#: options such as ``-f <file>`` (reads a program file, echoing it in the compile error) and ``-L <dir>`` (loads
#: modules) must not slip through. ``--slurpfile`` reads a file, so its source path is confined to the read roots
#: by :func:`_validate_slurp_sources`; ``--rawfile`` stays blocked (it reads any bytes, not only JSON).
#: Two operands are a variable name plus its value or source file; ``--indent`` takes one.
_OPERAND_COUNTS = {"--arg": 2, "--argjson": 2, "--indent": 1, "--slurpfile": 2}
#: Hostile jq filters can exhaust CPU/memory (CWE-400); cap wall-clock at 30s.
_JQ_TIMEOUT_SECONDS = 30
#: Hard virtual-memory ceiling for the jq subprocess (bytes). 256 MB is far
#: beyond any realistic foundry config file (typical settings.json is < 64 KB)
#: while still stopping a runaway filter that builds large in-memory data.
_JQ_MEMORY_LIMIT_BYTES = 256 * 1024 * 1024


def _jq_preexec() -> None:  # pragma: no cover — runs in subprocess fork
    """Build a subprocess initializer that caps the child address space.

    Unix-only (``resource`` module unavailable on Windows); on platforms where ``RLIMIT_AS`` is unsupported, the import
    is wrapped so the failure mode is "no limit applied" rather than "no jq spawned at all".
    """
    try:
        import resource  # imported here so non-POSIX platforms aren't penalised at module load
    except ImportError:
        return
    try:
        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        # Respect any tighter hard limit already in place.
        cap = _JQ_MEMORY_LIMIT_BYTES if hard == resource.RLIM_INFINITY else min(_JQ_MEMORY_LIMIT_BYTES, hard)
        resource.setrlimit(resource.RLIMIT_AS, (cap, hard))
    except (ValueError, OSError):
        # Setting the limit may fail on macOS for some inherited limits — fall
        # back to no cap rather than crashing the spawn.
        return


def _parse_jq_args(extras: list[str]) -> list[str] | None:
    """Validate and return passthrough jq flag tokens.

    Walks the list as flag-plus-operands groups (:data:`_OPERAND_COUNTS`). Operands are
    taken literally, so a value such as ``-1`` is fine; every token in flag position that
    is not an allowed flag is rejected (raises :class:`ValueError`). This blocks dangerous
    jq options such as ``--rawfile`` (reads arbitrary filesystem bytes) and the single-dash
    ``-f`` / ``-L``. ``--slurpfile`` is accepted here; its source path is confined
    separately by :func:`_validate_slurp_sources`.

    Args:
        extras: Tokens after ``<target> <filter>`` (e.g. ``["--arg", "k", "v"]``).

    Returns:
        The same list if every flag is followed by its operands, else None to signal
        malformed args.

    Raises:
        ValueError: When a token in flag position is not an allowed flag.

    Examples:
        >>> _parse_jq_args(["--slurpfile", "perms", "allow.json"])
        ['--slurpfile', 'perms', 'allow.json']
        >>> _parse_jq_args(["--slurpfile", "perms"]) is None
        True
        >>> _parse_jq_args(["--argjson", "n", "-1"])
        ['--argjson', 'n', '-1']
        >>> _parse_jq_args(["-f", "/etc/hosts"])
        Traceback (most recent call last):
        ValueError: jq_write: disallowed flag: '-f'
    """
    i = 0
    while i < len(extras):
        token = extras[i]
        operands = _OPERAND_COUNTS.get(token)
        if operands is None:
            kind = "flag" if token.startswith("-") else "argument"
            raise ValueError(f"jq_write: disallowed {kind}: {token!r}")
        # Every operand must exist; they are taken literally, whatever they start with.
        if i + operands >= len(extras):
            return None
        i += operands + 1
    return extras


def _home_dir() -> Path:
    """Return the user's home directory, preferring ``$HOME`` over :meth:`Path.home`.

    ``/foundry:setup`` creates, backs up and validates ``~/.claude/settings.json`` through ``$HOME`` in bash. On native
    Windows ``Path.home()`` reads ``USERPROFILE`` instead (Git Bash sets only ``HOME``), so using it alone could edit a
    different file than the one setup just backed up.
    """
    return Path(os.environ.get("HOME") or Path.home())


def _expand_home(path: Path) -> Path:
    """Expand a leading ``~`` through :func:`_home_dir`; ``~user`` is left to :meth:`Path.expanduser`.

    ``Path.expanduser`` reads ``USERPROFILE`` on Windows and would ignore ``$HOME`` the same way.
    """
    parts = path.parts
    if parts and parts[0] == "~":
        return _home_dir().joinpath(*parts[1:])
    return path.expanduser()


def _user_claude_dir() -> Path:
    """Return the lexical ``~/.claude`` directory; not resolved, so a symlinked home layout still matches."""
    return Path(os.path.abspath(_home_dir() / ".claude"))


def _validate_slurp_sources(extras: list[str]) -> None:
    """Assert every ``--slurpfile`` source resolves under cwd, ``TMPDIR`` or ``~/.claude``.

    Args:
        extras: Token list already accepted by :func:`_parse_jq_args`.

    Raises:
        ValueError: If a source file resolves outside the read roots.
    """
    tmpdir = Path(os.environ.get("TMPDIR") or tempfile.gettempdir()).resolve()
    roots = [Path.cwd().resolve(), tmpdir, _user_claude_dir().resolve()]
    # Walk groups exactly as _parse_jq_args does, so an `--arg` value spelled "--slurpfile" is not taken for the flag.
    sources: list[str] = []
    i = 0
    while i < len(extras):
        if extras[i] == "--slurpfile":
            sources.append(extras[i + 2])
        i += _OPERAND_COUNTS[extras[i]] + 1
    for source in sources:
        resolved = Path(source).resolve()
        if not any(resolved == r or r in resolved.parents for r in roots):
            raise ValueError(f"jq_write: --slurpfile source outside allowed roots (cwd, tmpdir, ~/.claude): {resolved}")


def _validate_target(target: Path) -> Path:
    """Resolve target and assert it lives under an allowed write root.

    Allowed roots are the current working directory and ``TMPDIR`` (falling back
    to the platform temp dir). Project subtrees such as ``.claude/``, ``.temp/``,
    and ``.reports/`` are covered transitively when they live under cwd; they are
    not independent roots. One narrow extra root: a ``*.json`` file whose lexical
    parent is ``~/.claude`` (the user settings files). Every other path outside
    cwd/tmpdir — including ``$CLAUDE_PLUGIN_ROOT`` deeper under ``~/.claude`` — is
    rejected.

    The user-settings case returns the lexical path, not the resolved one, so a
    ``settings.json`` symlinked into a dotfiles tree still passes the root check. The
    caller then writes through the link to the file it resolves to, keeping the link
    (``merge_settings_permissions.py`` does the same). Trade-off: such a link may lead
    outside the roots; accepted because only the user can plant a link directly in
    ``~/.claude``, and the shell ``mv`` this replaces swapped the link for a regular
    file, leaving the dotfiles copy stale.

    Args:
        target: User-supplied target file path.

    Returns:
        The resolved path, or the lexical absolute path for a ``~/.claude`` settings file.

    Raises:
        ValueError: If the path is outside allowed roots.
    """
    lexical = Path(os.path.abspath(_expand_home(target)))
    if lexical.parent == _user_claude_dir() and lexical.suffix == ".json":
        return lexical
    resolved = target.resolve()
    cwd = Path.cwd().resolve()
    tmpdir = Path(os.environ.get("TMPDIR", tempfile.gettempdir())).resolve()
    allowed = [cwd, tmpdir]
    if not any(resolved == r or r in resolved.parents for r in allowed):
        raise ValueError(
            f"jq_write: target outside allowed roots (cwd, tmpdir, ~/.claude/*.json): {resolved}",
        )
    return resolved


def _atomic_replace(temporary: Path, destination: Path) -> None:
    """Rename ``temporary`` over ``destination``, keeping the destination's permission bits.

    The temp file is created owner-only; without the chmod a 0644 settings file would silently become 0600.
    """
    os.chmod(temporary, stat.S_IMODE(destination.stat().st_mode))
    os.replace(temporary, destination)


def _run_jq_into(target: Path, destination: Path, jq_filter: str, extras: list[str]) -> int:
    """Run jq over ``target`` into a private temp file beside ``destination``, then rename it into place.

    The temp file sits in ``destination``'s directory so the rename stays on one filesystem, and carries a random
    name so a concurrent writer never truncates it. It is removed on every path that does not install it.

    Args:
        target: File jq reads (the path the caller named).
        destination: File the result replaces (``target`` with symlinks resolved).
        jq_filter: jq program string.
        extras: Vetted jq CLI tokens.

    Returns:
        0 on success, 2 on a jq timeout, spawn failure, non-zero exit, empty output or failed rename.
    """
    # A leading space keeps a filter such as `-f` from being read as an option; jq ignores it in a program.
    program = f" {jq_filter}" if jq_filter.startswith("-") else jq_filter
    # `preexec_fn` is POSIX-only and a no-op on Windows; pass it conditionally
    # so we don't crash the spawn on platforms that don't support it.
    spawn_kwargs: dict[str, object] = {}
    if sys.platform != "win32":
        spawn_kwargs["preexec_fn"] = _jq_preexec
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp", delete=False
        ) as fh:
            temporary = Path(fh.name)
            completed = subprocess.run(  # noqa: S603 - argv list, no shell
                ["jq", *extras, program, str(target)],  # noqa: S607 - jq resolved via PATH on purpose
                stdout=fh,
                stderr=subprocess.PIPE,
                check=False,
                text=True,
                timeout=_JQ_TIMEOUT_SECONDS,
                **spawn_kwargs,
            )
        if completed.returncode != 0:
            if completed.stderr:
                sys.stderr.write(completed.stderr)
            print(f"! jq filter failed for {target}", file=sys.stderr)
            return 2
        # A filter yielding `empty` exits 0 with no output; moving that over the target would
        # truncate it. The shell form this replaces guarded the same case with `[ -n "$_jq_result" ]`.
        if not temporary.read_text(encoding="utf-8").strip():
            print(f"! jq filter produced no output for {target} — target unchanged", file=sys.stderr)
            return 2
        try:
            _atomic_replace(temporary, destination)
        except OSError as exc:
            print(f"! cannot replace {destination}: {exc} — target unchanged", file=sys.stderr)
            return 2
        temporary = None
        return 0
    except subprocess.TimeoutExpired:
        print(f"! jq filter exceeded {_JQ_TIMEOUT_SECONDS}s timeout for {target}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"! jq invocation failed for {target}: {exc}", file=sys.stderr)
        return 2
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def run_jq_write(target: Path, jq_filter: str, extras: list[str]) -> int:
    """Apply ``jq_filter`` to ``target`` atomically.

    Args:
        target: JSON file to rewrite in place.
        jq_filter: jq program string.
        extras: Additional jq CLI tokens (e.g. ``["--arg", "k", "v"]``).

    Returns:
        0 on success, 1 if target missing, 2 on jq subprocess error or empty
        output, 3 if ``extras`` holds a disallowed or malformed token, 4 if the
        target resolves outside the allowed write roots or a ``--slurpfile``
        source outside the read roots.
    """
    # Same gate as main(): this function is public, and an unvetted token would reach jq's argv.
    try:
        if _parse_jq_args(extras) is None:
            print("! malformed --arg/--argjson/--slurpfile/--indent: operand missing", file=sys.stderr)
            return 3
    except ValueError as exc:
        print(f"! {exc}", file=sys.stderr)
        return 3
    try:
        target = _validate_target(target)
        _validate_slurp_sources(extras)
    except ValueError as exc:
        print(f"! {exc}", file=sys.stderr)
        return 4
    if not target.is_file():
        print(f"! target not found: {target}", file=sys.stderr)
        return 1

    return _run_jq_into(target, Path(os.path.realpath(target)), jq_filter, extras)


def main(argv: list[str] | None = None) -> int:
    """Apply a bounded jq update and return the process status.

    Args:
        argv: Optional argv override (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` on success, ``1`` if target missing, ``2`` on jq error, ``3`` on
        bad args, ``4`` if the target is outside the allowed write roots;
        argparse exits ``2`` on malformed args (e.g. unknown flag before the
        positionals).

    No doctest — subprocess- and argv-dependent; covered by pytest.
    """
    parser = argparse.ArgumentParser(
        prog="jq_write.py",
        description="Atomically apply a jq filter to a JSON file via temp-file + rename.",
    )
    # nargs="?" keeps the legacy exit-3-on-missing-arg contract; REMAINDER hands
    # the trailing ``--arg``/``--argjson``/``--indent`` triplets straight through to jq
    # untouched (argparse must not parse them — validated by _parse_jq_args).
    parser.add_argument("target", nargs="?", help="JSON file to rewrite in place.")
    parser.add_argument("jq_filter", nargs="?", help="jq program string to apply.")
    parser.add_argument("extras", nargs=argparse.REMAINDER, help="Passthrough jq tokens (e.g. --arg <name> <value>).")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.target is None or args.jq_filter is None:
        print(
            "Usage: jq_write.py <target-file> <jq-filter> [--arg <name> <value>]... [--slurpfile <name> <file>]...",
            file=sys.stderr,
        )
        return 3

    try:
        parsed = _parse_jq_args(args.extras)
    except ValueError as exc:
        print(f"! {exc}", file=sys.stderr)
        return 3
    if parsed is None:
        print("! malformed --arg/--slurpfile: each requires <name> <value>", file=sys.stderr)
        return 3

    return run_jq_write(Path(args.target), args.jq_filter, parsed)


if __name__ == "__main__":
    sys.exit(main())
