#!/usr/bin/env python
"""parse_deprecate_args.py — extract ``--deprecate`` flag and optional decorator value from $ARGUMENTS.

Writes DEPRECATE and DEPRECATE_DECORATOR to exclusively-created temp files and
prints the two resolved paths to stdout (flag path first, decorator path
second) so the caller reads with ``cat`` — avoids the ``eval "$(...)"``
anti-pattern. Filenames carry a random suffix from :func:`tempfile.mkstemp`
and are created ``O_CREAT|O_EXCL`` (/CWE-377): a pre-planted file or
symlink at a guessed name causes a hard failure rather than a followed write.
The caller must read the printed paths rather than a fixed name.

Usage:
    OUT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/parse_deprecate_args.py" --arguments="$ARGUMENTS")
    FLAG_FILE=$(printf '%s\\n' "$OUT" | sed -n 1p)
    DEC_FILE=$(printf '%s\\n' "$OUT" | sed -n 2p)
    DEPRECATE=$(cat "$FLAG_FILE" 2>/dev/null || echo "false")
    DEPRECATE_DECORATOR=$(cat "$DEC_FILE" 2>/dev/null || echo "")

The ``--arguments=`` form (equals sign, no space) is required so that values
beginning with ``--`` (e.g. the literal payload ``--deprecate``) survive
argparse's flag-detection.

Output files (raw values, no shell quoting — safe to read with ``cat``):
    <tmpdir>/codemap-deprecate-flag-<random>        "true" or "false"
    <tmpdir>/codemap-deprecate-decorator-<random>   raw decorator string (empty when absent)

Recognises:
    --deprecate                            bare flag → DEPRECATE=true, DEPRECATE_DECORATOR=''
    --deprecate=<value>                    value form → DEPRECATE=true, DEPRECATE_DECORATOR=<value>
    --deprecate='<value with spaces>'      single-quoted value → quotes stripped
    --deprecate="<value with spaces>"      double-quoted value → quotes stripped
    --no-deprecate                         negation → DEPRECATE=false, DEPRECATE_DECORATOR=''

Exit codes:
    0  always (never fails on input).
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import sys
import tempfile
from pathlib import Path

#: ``--deprecate=<value>`` where value is one of:
#:   - single-quoted: '...'  (no embedded single quotes)
#:   - double-quoted: "..."  (no embedded double quotes)
#:   - unquoted: any run of non-whitespace characters
#: Anchored on left so we never confuse ``--no-deprecate`` or other suffixes.
_DEPRECATE_VALUE_RE = re.compile(
    r"(?:^|\s)--deprecate=(?:'([^']*)'|\"([^\"]*)\"|(\S+))",
)
#: Bare ``--deprecate`` flag — must NOT be followed by '=' (otherwise the value form matches)
#: and must NOT be preceded by '--no-' (otherwise ``--no-deprecate`` matches).
_DEPRECATE_BARE_RE = re.compile(
    r"(?:^|\s)--deprecate(?:\s|$)",
)
#: ``--no-deprecate`` flag — explicit negation; overrides any ``--deprecate`` occurrence.
_NO_DEPRECATE_RE = re.compile(
    r"(?:^|\s)--no-deprecate(?:\s|$)",
)


def parse_deprecate_args(arguments: str) -> tuple[bool, str]:
    """Parse the $ARGUMENTS string for ``--deprecate`` / ``--no-deprecate`` flags.

    Args:
        arguments: Raw $ARGUMENTS string from the caller.

    Returns:
        Tuple ``(deprecate, decorator)`` where ``deprecate`` is ``True`` when
        ``--deprecate`` (with or without value) is present, ``False`` when
        ``--no-deprecate`` is present or no flag is present at all; ``decorator``
        is the raw value after ``--deprecate=`` with surrounding quotes stripped,
        or the empty string when the bare flag or no flag was used.

    Behaviour notes:
        * ``--no-deprecate`` always wins over ``--deprecate`` — explicit negation
          takes precedence regardless of order.
        * Only the **first** ``--deprecate=<value>`` occurrence supplies the
          decorator string; later occurrences are ignored.

    Examples:
        >>> parse_deprecate_args("--deprecate")
        (True, '')
        >>> parse_deprecate_args("--deprecate=@deprecated")
        (True, '@deprecated')
        >>> parse_deprecate_args("--deprecate='@deprecated(target=bar)'")
        (True, '@deprecated(target=bar)')
        >>> parse_deprecate_args('--deprecate="@deprecated_class(target=Bar)"')
        (True, '@deprecated_class(target=Bar)')
        >>> parse_deprecate_args("--no-deprecate")
        (False, '')
        >>> parse_deprecate_args("")
        (False, '')
        >>> parse_deprecate_args("--dry-run --since 1.0")
        (False, '')
        >>> parse_deprecate_args("--deprecate --no-deprecate")
        (False, '')
    """
    # Explicit negation wins regardless of any ``--deprecate`` presence.
    if _NO_DEPRECATE_RE.search(arguments):
        return False, ""

    value_match = _DEPRECATE_VALUE_RE.search(arguments)
    if value_match is not None:
        # Exactly one of the three capture groups matched.
        decorator = next(group for group in value_match.groups() if group is not None)
        return True, decorator

    if _DEPRECATE_BARE_RE.search(arguments):
        return True, ""

    return False, ""


def format_shell_assignments(deprecate: bool, decorator: str) -> str:
    """Render parsed values as shell assignments suitable for ``eval``.

    Retained as a pure helper for tests and external consumers.
    ``main()`` no longer calls this — it writes raw values to temp files instead.

    Args:
        deprecate: Whether ``--deprecate`` was present (or absent / negated).
        decorator: The decorator value after ``--deprecate=`` (empty when not supplied).

    Returns:
        Two-line string with ``DEPRECATE=`` and ``DEPRECATE_DECORATOR=`` assignments.
        Both values are passed through :func:`shlex.quote` so the caller can safely
        ``eval`` the output even when the decorator contains spaces, quotes, or
        shell metacharacters.

    Examples:
        >>> format_shell_assignments(True, "")
        "DEPRECATE=true\\nDEPRECATE_DECORATOR=''"
        >>> format_shell_assignments(True, "@deprecated(target=bar)")
        "DEPRECATE=true\\nDEPRECATE_DECORATOR='@deprecated(target=bar)'"
        >>> format_shell_assignments(False, "")
        "DEPRECATE=false\\nDEPRECATE_DECORATOR=''"
    """
    deprecate_str = "true" if deprecate else "false"
    return f"DEPRECATE={deprecate_str}\nDEPRECATE_DECORATOR={shlex.quote(decorator)}"


def _trusted_default_tmpdir() -> str:
    """Return the platform temp directory without re-reading ``TMPDIR``/``TEMP``/``TMP``.

    ``tempfile.gettempdir()`` is not a trustworthy fallback on its own: CPython's
    ``tempfile._candidate_tempdir_list()`` tries the environment first — the very
    ``TMPDIR``/``TEMP``/``TMP`` values :func:`_safe_tmpdir` just rejected — so calling
    it directly silently undoes the ownership check. Clearing those three variables
    for the duration of the call forces CPython's own hardcoded platform-default
    candidates instead, then restores the caller's environment unchanged.

    Returns:
        Absolute path to the platform default temp directory, ignoring env overrides.
    """
    saved = {key: os.environ.pop(key, None) for key in ("TMPDIR", "TEMP", "TMP")}
    try:
        tempfile.tempdir = None  # drop any value cached while the untrusted env was active
        return tempfile.gettempdir()
    finally:
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value
        tempfile.tempdir = None  # do not leak the forced-recompute cache either


def _safe_tmpdir() -> str:
    """Return a trustworthy temp directory, validating any ``TMPDIR`` override.

     (CWE-377): ``TMPDIR`` is attacker-influencable. A value like ``/etc``
    would redirect writes outside the temp area, and a directory not owned by the
    current user enables symlink-swap races. The override is accepted only when it
    is an absolute path to an existing directory owned by the current user;
    otherwise we fall back to :func:`_trusted_default_tmpdir`, which does not
    re-read the value just rejected.

    Returns:
        Absolute path to a validated temp directory.
    """
    override = os.environ.get("TMPDIR", "")
    if override:
        candidate = Path(override)
        try:
            if (
                candidate.is_absolute()
                and candidate.is_dir()
                and (not hasattr(os, "getuid") or candidate.stat().st_uid == os.getuid())
            ):
                return str(candidate)
        except OSError:
            pass  # stat failure (permission, race) — fall through to the trusted default
    return _trusted_default_tmpdir()


def _write_new_sentinel(tmpdir: str, prefix: str, content: str) -> Path:
    """Create a new, exclusively-owned sentinel file in ``tmpdir`` and write ``content``.

    Uses :func:`tempfile.mkstemp` — ``O_CREAT|O_EXCL`` — so a pre-planted file or
    symlink at a guessed name causes ``FileExistsError`` instead of a followed,
    truncating write (the CWE-59/CWE-377 pair the removed ``-<pid>`` suffix claimed,
    without evidence, to already defeat). Created ``0o600`` by :func:`tempfile.mkstemp`.

    Args:
        tmpdir: Validated temp directory (see :func:`_safe_tmpdir`).
        prefix: Filename prefix passed to :func:`tempfile.mkstemp`.
        content: Text to write (caller supplies any trailing newline).

    Returns:
        Path of the newly created, written file.
    """
    fd, path_str = tempfile.mkstemp(prefix=prefix, dir=tmpdir)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    return Path(path_str)


def _write_temp_vars(deprecate: bool, decorator: str) -> tuple[Path, Path]:
    """Write DEPRECATE and DEPRECATE_DECORATOR to temp files for ``cat``-based reads.

    Writes raw (unquoted) values — no shell quoting needed since callers assign
    via ``VAR=$(cat ...)`` rather than ``eval``.

    Filenames carry a random suffix from :func:`tempfile.mkstemp`, created
    exclusively (see :func:`_write_new_sentinel`) so neither name is knowable in
    advance to a co-located attacker. Because the suffix is not knowable to the
    calling shell either, :func:`main` prints the two resolved paths to stdout so
    the caller can ``cat`` exactly these files. The temp directory is validated by
    :func:`_safe_tmpdir`.

    Args:
        deprecate: Whether ``--deprecate`` flag was present.
        decorator: Raw decorator string (empty when bare flag or absent).

    Returns:
        Tuple ``(flag_path, decorator_path)`` — the two files just written.
    """
    tmpdir = _safe_tmpdir()
    flag_path = _write_new_sentinel(tmpdir, "codemap-deprecate-flag-", "true\n" if deprecate else "false\n")
    decorator_path = _write_new_sentinel(tmpdir, "codemap-deprecate-decorator-", f"{decorator}\n")
    return flag_path, decorator_path


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: parse ``--arguments`` payload, write to temp files, return 0.

    Prints the two pid-qualified temp-file paths to stdout (flag path on the first
    line, decorator path on the second) so the calling shell can ``cat`` exactly
    the files this process wrote — the pid suffix is otherwise unknowable
    to the caller.

    Args:
        argv: Optional argv override for testing. Defaults to ``sys.argv[1:]``.

    Returns:
        Exit code (always 0 — never fails on input).
    """
    parser = argparse.ArgumentParser(
        description="Extract --deprecate flag and optional decorator value from $ARGUMENTS.",
    )
    parser.add_argument(
        "--arguments",
        default="",
        help="Raw $ARGUMENTS string from the caller.",
    )
    args = parser.parse_args(argv)
    deprecate, decorator = parse_deprecate_args(args.arguments)
    flag_path, decorator_path = _write_temp_vars(deprecate, decorator)
    print(f"{flag_path}\n{decorator_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
