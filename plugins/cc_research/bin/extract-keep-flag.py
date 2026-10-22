#!/usr/bin/env python3
"""extract-keep-flag.py — initialise a skill's compaction-contract state from its arguments.

Python rather than a shell regex on purpose: the inline twin of this parse used `[[ =~ ]]` +
${BASH_REMATCH[1]}, and zsh — the harness shell — populates $match instead, so every
``--keep "..."`` silently resolved to the empty string.

Usage: python extract-keep-flag.py <sentinel-slug> "$ARGUMENTS" [--venue-choices A,B,C]
                                   [--out-file PATH] [--inlined]
  <sentinel-slug> is the sentinel's own slug, not the skill name (research:run writes
  `research-run-keep-items`, so it passes `research-run`).
  --out-file writes the keep value to PATH instead of the slug-derived sentinel, creating
  parent directories as needed. For skills that keep their state in a per-session
  directory (`<skill>-state-${CSID}/keep-items`) rather than a flat sentinel name; the
  slug is still required, because it prefixes this script's own error messages.
  --venue-choices additionally parses `--venue <token>`, validates it against the given
  comma-separated list, and writes it to ${TMPDIR:-/tmp}/<sentinel-slug>-venue-${CSID}.
  An absent --venue is legal and writes an empty value; the caller's skip rule reads it.
  --inlined marks a call made from a skill that a parent skill loaded inline (sweep running
  judge or run): the parent's live contract must survive, so the stale-contract clear is skipped.
Argv is parsed by hand, not argparse: five live call sites pass "$ARGUMENTS" positionally and
  that string routinely starts with `--`, which argparse would reject as an unknown option.
Side effects: clears a stale .temp/state/skill-contract.md left by a crashed prior run
  (compaction-contract.md §Lifecycle) and writes the keep value to
  ${TMPDIR:-/tmp}/<sentinel-slug>-keep-items-${CSID}. Also prints it (may be empty).
  Both happen before venue validation, so an invalid venue still leaves them written.
  Sentinels are written atomically (sibling mkstemp file, then os.replace), so a symlink planted at a
  predictable sentinel name in a shared temp dir is replaced, never followed.
  The clear is skipped, leaving contract and owner stamp untouched, under --inlined and when the
  owner stamp names a different session than CLAUDE_CODE_SESSION_ID: concurrent sessions in one
  working directory share that path, and the hooks already ignore a foreign-stamped contract.
  No stamp, an unreadable stamp or an unset CLAUDE_CODE_SESSION_ID still clears.
Requires: CSID exported by the caller — a child's own parent process id is the calling shell,
  not the Claude Code process, so deriving it here would name a different sentinel.
Exit codes: 0 = ok · 1 = state cleanup or sentinel write failed · 2 = missing or malformed slug,
  missing or malformed session ID, invalid --venue value, or an --out-file that is empty or outside the temp dir
  and working directory
Most call sites do not check the exit code: every one of them exports CSID on the preceding
  line, so the exit-2 identity paths are unreachable from a skill. Only those two identity
  checks return before the contract is cleared; every later non-zero exit — bad --out-file,
  failed sentinel write, invalid --venue — happens after the clear, so the caller owes no
  cleanup on them.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import sys
import tempfile
from pathlib import Path

#: [[:space:]]+ (not a single literal space) so `--keep  "a, b"` parses too — superset of the
#: inline form it replaces, never a narrower match.
_KEEP_RE = re.compile(r'--keep[ \t\r\f\v]+"([^"]+)"')

#: Unquoted single token, mirroring the `--venue <VENUE>` spelling the fortify skill documents.
_VENUE_RE = re.compile(r"--venue[ \t\r\f\v]+(\S+)")

#: CWD-relative, exactly as the shell original: the contract belongs to the project the skill
#: is running in, which is the caller's working directory, not this script's location.
_CONTRACT = Path(".temp/state/skill-contract.md")

#: Stamp that write_skill_contract.py writes beside the contract: {"session_id", "sha256", "written_at"}.
_OWNER = _CONTRACT.with_suffix(".owner")

#: Shape of the slug and session ID, joined into sentinel file names. No separators, so neither can leave the temp
#: dir.
_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")


def option_value(argv: list[str], name: str) -> str:
    """Return the value of ``--name`` in ``argv``, accepting both the spaced and attached spellings.

    Args:
        argv: Option tokens to scan (never the caller's ``"$ARGUMENTS"`` payload).
        name: Option name including its leading dashes.

    Returns:
        The option's value, or an empty string when the option is absent.

    Examples:
        >>> option_value(["--venue-choices", "CVPR,ICML"], "--venue-choices")
        'CVPR,ICML'
        >>> option_value(["--venue-choices=CVPR"], "--venue-choices")
        'CVPR'
        >>> option_value(["--other", "x"], "--venue-choices")
        ''
    """
    for i, token in enumerate(argv):
        if token == name and i + 1 < len(argv):
            return argv[i + 1]
        if token.startswith(f"{name}="):
            return token[len(name) + 1 :]
    return ""


def _within(candidate: Path, roots: tuple[Path, ...]) -> bool:
    """Report whether ``candidate`` resolves inside any of ``roots``.

    Resolution happens before the comparison so ``..`` segments and symlinks cannot walk out
    of a root that the unresolved string appears to sit under. Resolution is non-strict on
    both sides, so a root that does not exist yet still matches paths under it — deliberate:
    the shell root ``/tmp`` may be absent on the host when the caller has not created it,
    and rejecting the caller's own directory for not existing yet is the defect this guard
    caused once already.

    Examples:
        >>> import tempfile
        >>> root = Path(tempfile.gettempdir()).resolve()
        >>> _within(root / "a" / "b", (root,))
        True
        >>> _within(root / ".." / "elsewhere", (root,))
        False
    """
    try:
        resolved = candidate.resolve()
    except OSError:
        return False
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
        except (OSError, ValueError):
            continue
        return True
    return False


def _make_private_dirs(path: Path) -> None:
    """Create ``path`` and any missing parents, giving each directory this call creates mode 0700.

    ``Path.mkdir(parents=True)`` creates parents with the default mode, which leaves a new per-session state directory
    listable by other users. Existing directories keep their mode: the temp dir itself (``/tmp`` on Linux) belongs to
    root. The ``chmod`` is best-effort, since native Windows ignores POSIX mode bits.

    Raises:
        OSError: When a missing directory cannot be created, or a non-directory sits in the way.
    """
    missing = [directory for directory in (path, *path.parents) if not directory.exists()]
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            continue  # created concurrently by someone else; not this call's to re-mode
        with contextlib.suppress(OSError):
            os.chmod(directory, 0o700)  # mkdir's mode is masked by the umask; restate it exactly
    if not path.is_dir():
        raise NotADirectoryError(errno.ENOTDIR, "expected a directory", str(path))


def _write_atomic(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` through a sibling temp file, never writing through an existing entry at ``path``.

    A plain ``write_text`` follows a symlink planted at a predictable name in a shared temp dir and overwrites its
    target. ``mkstemp`` opens a fresh name ``O_EXCL`` (so it also refuses a symlink) with mode 0600, and ``os.replace``
    swaps the directory entry itself, leaving any symlink's target untouched. An entry another user already owns at
    ``path`` is refused first (``lstat``, so a foreign symlink counts too); native Windows has no ``os.getuid``, so the
    check is skipped there, where the per-user temp dir already keeps other users out.

    Args:
        path: Destination file; its parent directory must exist.
        text: Content written verbatim, with LF newlines.

    Raises:
        PermissionError: When ``path`` exists and belongs to another user; nothing is written.
        OSError: When the temp file cannot be created, written or moved into place; the temp file is removed first.
    """
    getuid = getattr(os, "getuid", None)
    try:
        owner = os.lstat(path).st_uid
    except FileNotFoundError:
        owner = None
    if getuid is not None and owner is not None and owner != getuid():
        raise PermissionError(
            errno.EPERM,
            "sentinel is owned by another user; remove it, or point TMPDIR at a directory only you can write",
            str(path),
        )
    descriptor, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise


def _stamped_by_other_session() -> bool:
    """Report whether the contract's owner stamp names a session other than the running one.

    Missing, unreadable or malformed stamps, and an unset ``CLAUDE_CODE_SESSION_ID``, answer ``False``: the caller then
    clears as it always did, so a crashed prior run is still cleaned up.
    """
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not session_id:
        return False
    try:
        stamp = json.loads(_OWNER.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    stamped = stamp.get("session_id") if isinstance(stamp, dict) else None
    return isinstance(stamped, str) and bool(stamped) and stamped != session_id


def _write_venue(slug: str, args: str, choices: str, sentinel: Path) -> int:
    """Validate ``--venue`` against ``choices`` and persist it.

    Args:
        slug: Sentinel slug, reused as the message prefix so the wording stays the skill's own.
        args: Raw ``$ARGUMENTS`` string.
        choices: Comma-separated list of legal venues; an empty venue is always legal.
        sentinel: Destination sentinel path.

    Returns:
        ``0`` when written, ``2`` on an invalid venue, ``1`` when the write failed.
    """
    match = _VENUE_RE.search(args)
    venue = match.group(1) if match else ""
    valid = [c.strip() for c in choices.split(",") if c.strip()]
    if venue and venue not in valid:
        print(f"{slug}: invalid --venue '{venue}' — valid: {', '.join(valid)}", file=sys.stderr)
        return 2
    try:
        _write_atomic(sentinel, venue + "\n")
    except OSError as exc:
        print(f"extract-keep-flag: cannot write {sentinel}: {exc}", file=sys.stderr)
        return 1
    return 0


def _resolve_sentinel_path(slug: str, csid: str, out_file: str, out_file_named: bool, tmp: Path) -> Path | None:
    """Choose where the keep value is written, or ``None`` when ``--out-file`` is unusable.

    Without ``--out-file`` the path is the per-session slug sentinel. With it, the caller's path must stay inside the
    temp dir or the working directory; the rejection is printed here.
    """
    if out_file_named and not out_file:
        # Falling back to the slug path here would exit 0 while writing somewhere the caller
        # does not read, losing the keep value silently. A caller that names the option owes
        # a value.
        print("extract-keep-flag: --out-file given without a value", file=sys.stderr)
        return None
    if not out_file:
        return tmp / f"{slug}-keep-items-{csid}"
    # `tmp` is the shell's `${TMPDIR:-/tmp}`. With TMPDIR unset, tempfile.gettempdir() answers
    # TEMP/TMP instead (on native Windows, and on POSIX when either is set); accepting that
    # platform dir as a root too keeps an --out-file computed there, such as one under
    # pytest's tmp_path, from being rejected as "outside the temp dir".
    platform_tmp = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
    sentinel = Path(out_file)
    if not _within(sentinel, (tmp, platform_tmp, Path.cwd())):
        # Every real caller writes into the session temp dir or the project it runs in. The
        # value is fixed skill text today, but this script creates parent directories, so an
        # unconstrained path would let a future caller — or a blob that reached argv — build
        # a tree anywhere the process can write.
        print(
            f"extract-keep-flag: --out-file {sentinel} is outside the temp dir and the working directory",
            file=sys.stderr,
        )
        return None
    return sentinel


def _write_keep_sentinel(sentinel: Path, keep_items: str) -> int | None:
    """Write the keep value to its sentinel, returning ``1`` if the write failed."""
    try:
        # The per-session state directory is the caller's to name but not always its to
        # create: with --out-file this script may be the first writer into it.
        _make_private_dirs(sentinel.parent)
        _write_atomic(sentinel, keep_items + "\n")
    except OSError as exc:
        print(f"extract-keep-flag: cannot write {sentinel}: {exc}", file=sys.stderr)
        return 1
    return None


def main(argv: list[str]) -> int:
    """Persist the first quoted keep value after clearing stale compaction state.

    ``argv`` includes the executable name, sentinel slug, and optional argument text. Resolve the session ID from
    ``CSID`` or ``CLAUDE_CODE_SESSION_ID``. Return 2 for missing identity, 1 for filesystem errors, or 0 after printing
    the retained value. A missing keep flag writes and prints an empty value. With ``--venue-choices`` present in the
    trailing options, also parse, validate, and persist ``--venue``.
    """
    slug = argv[1] if len(argv) > 1 else ""
    args = argv[2] if len(argv) > 2 else ""
    venue_choices = option_value(argv[3:], "--venue-choices")
    out_file = option_value(argv[3:], "--out-file")
    if not slug:
        print("extract-keep-flag: missing <sentinel-slug> argument", file=sys.stderr)
        return 2
    # No "shared" fallback here, unlike the other bin/ scripts: this script's contract is
    # that an unset CSID is a caller bug, and its callers gate on the exit-2.
    csid = os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or ""
    if not csid:
        print("extract-keep-flag: CSID not exported by caller", file=sys.stderr)
        return 2
    if any(not _NAME_RE.fullmatch(value) or ".." in value for value in (slug, csid)):
        print(
            "extract-keep-flag: slug and CSID must use letters, digits, '.', '_' or '-' and must not contain '..'",
            file=sys.stderr,
        )
        return 2

    match = _KEEP_RE.search(args)
    keep_items = match.group(1) if match else ""

    if "--inlined" not in argv[3:] and not _stamped_by_other_session():
        try:
            _CONTRACT.unlink(missing_ok=True)
        except OSError as exc:
            print(f"extract-keep-flag: cannot clear {_CONTRACT}: {exc}", file=sys.stderr)
            return 1

    tmp_fallback = tempfile.gettempdir() if os.name == "nt" else "/tmp"  # noqa: S108 - shell default of ${TMPDIR:-/tmp}
    tmp = Path(os.environ.get("TMPDIR") or tmp_fallback)
    sentinel = _resolve_sentinel_path(slug, csid, out_file, "--out-file" in argv[3:], tmp)
    if sentinel is None:
        return 2

    write_failure = _write_keep_sentinel(sentinel, keep_items)
    if write_failure is not None:
        return write_failure

    print(keep_items)
    venue_sentinel = tmp / f"{slug}-venue-{csid}"
    return _write_venue(slug, args, venue_choices, venue_sentinel) if venue_choices else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
