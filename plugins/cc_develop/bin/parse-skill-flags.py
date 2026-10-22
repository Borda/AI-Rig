#!/usr/bin/env python
"""parse-skill-flags.py — parse a skill's $ARGUMENTS blob, emit shell variable assignments and optional sentinels.

Generalizes the boolean anchored-token flag + ``--keep "<items>"`` extraction that
was independently re-implemented (regex + ``case``/``[[ ]]`` anchored-token match +
``sed``-based ``CLEAN_ARGS`` stripping) in ``resolve/SKILL.md``, ``analyse/SKILL.md``,
and ``review/SKILL.md``. Follows the eval-emit shape of ``parse-resolve-args.py``.

Usage (Claude Code plugin — ``CLAUDE_PLUGIN_ROOT`` set automatically)::

    # state mode: no eval, values read back from per-session sentinels
    python "${CLAUDE_PLUGIN_ROOT}/bin/parse-skill-flags.py" --flags reply,worktree --keep-state analyse "$ARGUMENTS"
    IFS= read -r REPLY_MODE < "${TMPDIR:-/tmp}/analyse-reply-${CSID}" 2>/dev/null || REPLY_MODE=false

    # eval mode: the original shape, still supported
    eval "$(python "${CLAUDE_PLUGIN_ROOT}/bin/parse-skill-flags.py" --flags reply,no-challenge,worktree "$ARGUMENTS")"

Emits, for a ``--flags`` list of ``N`` comma-separated bare flag names (no leading
``--``):

- ``FLAG_<NAME>`` — one per requested flag, ``true``/``false``; ``<NAME>`` is the
  flag name uppercased with ``-`` replaced by ``_`` (e.g. ``no-challenge`` →
  ``FLAG_NO_CHALLENGE``). Detection is anchored-token (padded-space match), never
  bare substring — ``--reply`` does not false-fire on ``--reply-later`` or a repo
  name containing ``--reply-bot``.
- ``VALUE_<NAME>`` — one per ``--value-flags`` entry, holding the value given as
  ``--<flag> VALUE`` or ``--<flag>=VALUE``; surrounding quotes are stripped, and the
  flag/value pair is removed from ``CLEAN_ARGS`` as a unit. Empty when absent.
  Anchored like the boolean detection, so ``--agent`` never fires on ``--agent-pool``.
- ``KEEP_ITEMS`` — value captured from ``--keep "<items>"`` (empty string if absent); ``'<items>'`` and
  quote-stripped bare words up to the next ``--`` option are accepted too, with a stderr warning for the bare form
  and for an empty value
- ``CLEAN_ARGS`` — original blob with ``--keep "<items>"`` and every requested
  ``--<flag>`` token removed (anchored, same non-substring safety as detection),
  whitespace collapsed, one leading ``#`` stripped

State mode (``--keep-state <prefix>``) prints the same assignments and additionally
initialises the skill's per-session state, which absorbs ``extract-keep-flag.py``:

- clears a stale ``.temp/state/skill-contract.md`` left by a crashed prior run, unless
  ``--inlined`` is given (a parent skill loaded this one and its contract is live) or the
  contract's owner stamp names a session other than ``CLAUDE_CODE_SESSION_ID``;
- writes one sentinel per value under ``${TMPDIR}``, every run, so a value from an
  earlier invocation in the same session never survives: ``<prefix>-<flag>-<CSID>``
  (``true``/``false``) per ``--flags`` entry, ``<prefix>-<flag>-<CSID>`` (the value,
  possibly empty) per ``--value-flags`` entry, ``<prefix>-clean-args-<CSID>``, and
  ``<prefix>-keep-items-<CSID>`` — or the ``--out-file`` path for the keep value.

Sentinels are written atomically (sibling ``mkstemp`` file, then ``os.replace``), so a
symlink planted at a predictable name in a shared temp dir is replaced, never followed.
``CSID`` (or ``CLAUDE_CODE_SESSION_ID``) must be exported by the caller: this process's
parent is the calling shell, not Claude Code, so deriving a token here would name a
different sentinel than the caller reads.

Callers keep flag-specific semantics (name mapping, polarity inversion such as
``--no-challenge`` → ``CHALLENGE_ENABLED`` defaulting ``true``) in the consuming
``SKILL.md`` — this script extracts only the duplicated detection/stripping
mechanics, not per-skill variable naming or polarity.

Exit codes:
    0 — assignments emitted (and, in state mode, every sentinel written)
    1 — state mode only: the stale contract could not be cleared, or a sentinel write failed
    2 — invalid ``--flags``/``--value-flags`` value; or, in state mode, a missing or malformed
        prefix or session ID, a flag name whose sentinel would collide with another, or an
        ``--out-file`` that is empty or outside the temp dir and working directory;
        ``--out-file``/``--inlined`` given without ``--keep-state``

Nothing is printed on a non-zero exit, so an ``eval`` caller ends with every variable unset
exactly as before state mode existed.

Note: like ``parse-resolve-args.py``, the ``$ARGUMENTS`` blob is forwarded
verbatim via ``parse_known_args`` — a blob starting with ``-`` (e.g. dash-leading
prose) is never misparsed as an option of this script. The one exception is a blob that
is exactly one of this script's own option spellings (``--inlined``, ``--keep-state=x``):
argparse consumes it as that option.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import json
import os
import re
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Final

#: Valid flag name: lowercase letters and digits in hyphen-separated groups, as listed in --flags.
_FLAG_NAME_RE: Final = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
#: Matches the --keep option with its value so the text can be captured and removed from the arguments. The value is
#: double-quoted, single-quoted, or bare words up to the next ``--`` option: the host substitutes ``$ARGUMENTS`` into
#: ``"$ARGUMENTS"`` as text, so a user's ``--keep "a b"`` reaches this script with its quotes consumed by the shell.
_KEEP_RE: Final = re.compile(r"""(?<=\s)--keep(?:=|\s+)(?:"([^"]*)"|'([^']*)'|((?!--)\S+(?:\s+(?!--)\S+)*))(?=\s)""")
#: A --keep token with no value after it (end of arguments, or another option next).
_BARE_KEEP_RE: Final = re.compile(r"(?<=\s)--keep(?:=)?(?=\s)")

#: Shape of the --keep-state prefix and the session ID, joined into sentinel file names. No separators, so neither can
#: leave the temp dir.
_NAME_RE: Final = re.compile(r"[A-Za-z0-9._-]+")

#: CWD-relative on purpose: the contract belongs to the project the skill is running in, which is the caller's working
#: directory, not this script's location.
_CONTRACT: Final = Path(".temp/state/skill-contract.md")

#: Stamp that write_skill_contract.py writes beside the contract: {"session_id", "sha256", "written_at"}.
_OWNER: Final = _CONTRACT.with_suffix(".owner")

#: Sentinel suffixes state mode writes for itself; a requested flag with one of these names would share their path.
_RESERVED_SUFFIXES: Final = frozenset({"keep-items", "clean-args"})


class _StateError(Exception):
    """A state-mode failure, carrying the exit code ``main`` returns for it."""

    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _value_re(flag: str) -> re.Pattern[str]:
    """Build the anchored matcher for ``--<flag> VALUE`` and ``--<flag>=VALUE``.

    The value is a double-quoted string, a single-quoted string, or one bare token. Anchoring
    on leading whitespace keeps ``--agent`` from matching inside ``--agent-pool``, the same
    non-substring guarantee the boolean detection gives.

    Examples:
        >>> _value_re("agent").search(' --agent foundry:sw-engineer ').group(1)
        'foundry:sw-engineer'
        >>> _value_re("agent").search(' --agent="a b" ').group(1)
        '"a b"'
        >>> _value_re("agent").search(' --agent-pool x ') is None
        True
    """
    return re.compile(rf"(?<=\s)--{re.escape(flag)}(?:=|\s+)(\"[^\"]*\"|'[^']*'|\S+)(?=\s)")


def _keep_warning(arguments: str) -> str | None:
    """Describe a ``--keep`` that reached this script empty or with its quotes stripped, else ``None``.

    Examples:
        >>> _keep_warning('x --keep "a b"') is None
        True
        >>> "empty" in _keep_warning("x --keep")
        True
        >>> "unquoted" in _keep_warning("x --keep a b")
        True
    """
    padded = f" {arguments} "
    match = _KEEP_RE.search(padded)
    if match is None:
        if _BARE_KEEP_RE.search(padded):
            return "⚠ --keep given with an empty value; nothing will be preserved through compaction"
        return None
    if match.group(3) is not None:
        return (
            f"⚠ --keep value arrived unquoted; captured {match.group(3)!r} up to the next --option — "
            'if that is wrong, put --keep "<items>" last'
        )
    if not next(group for group in match.groups() if group is not None).strip():
        return "⚠ --keep given with an empty value; nothing will be preserved through compaction"
    return None


def _var_name(flag: str) -> str:
    """Convert a bare flag name to its emitted shell variable suffix.

    >>> _var_name("worktree")
    'WORKTREE'
    >>> _var_name("no-challenge")
    'NO_CHALLENGE'
    """
    return flag.upper().replace("-", "_")


def parse_skill_flags(arguments: str, flags: list[str], value_flags: list[str] | None = None) -> dict[str, str]:
    """Parse a skill's ``$ARGUMENTS`` blob against a requested flag list.

    Args:
        arguments: Raw ``$ARGUMENTS`` blob (e.g. ``'42 --reply --keep "notes"'``).
        flags: Bare flag names to detect (no leading ``--``), e.g.
            ``["reply", "no-challenge", "worktree"]``.
        value_flags: Bare names of flags that carry a value, e.g. ``["agent", "repo"]``.
            Each emits ``VALUE_<NAME>``, empty when the flag is absent.

    Returns:
        Dict with one ``FLAG_<NAME>`` key per requested flag, one ``VALUE_<NAME>`` key per
        requested value flag, plus ``KEEP_ITEMS`` and ``CLEAN_ARGS``.

    Examples:
        >>> result = parse_skill_flags("42 --reply --keep \\"drop the typo fix\\"", ["reply", "worktree"])
        >>> result["FLAG_REPLY"]
        'true'
        >>> result["FLAG_WORKTREE"]
        'false'
        >>> result["KEEP_ITEMS"]
        'drop the typo fix'
        >>> result["CLEAN_ARGS"]
        '42'
        >>> parse_skill_flags("--reply-later fix the bug", ["reply"])["FLAG_REPLY"]
        'false'
        >>> parse_skill_flags("#42 --reply", ["reply"])["CLEAN_ARGS"]
        '42'
        >>> parse_skill_flags("plan.md --keep foo bar --reply", ["reply"])["KEEP_ITEMS"]
        'foo bar'
    """
    padded = f" {arguments} "
    keep_match = _KEEP_RE.search(padded)
    keep_items = next((group for group in keep_match.groups() if group is not None), "") if keep_match else ""

    result: dict[str, str] = {}
    clean = _BARE_KEEP_RE.sub(" ", _KEEP_RE.sub(" ", padded))
    # Value flags strip first: a value can look like a boolean flag (``--agent --worktree``),
    # and removing the pair as a unit keeps that value from being read as a flag of its own.
    for flag in value_flags or []:
        match = _value_re(flag).search(clean)
        raw = match.group(1) if match else ""
        result[f"VALUE_{_var_name(flag)}"] = raw[1:-1] if raw[:1] in ("'", '"') else raw
        if match:
            clean = clean[: match.start()] + " " + clean[match.end() :]
    for flag in flags:
        # Detect against `clean`, not the original blob: a value flag has already consumed
        # its value, so a malformed `--agent --worktree` cannot report --worktree as both
        # the agent name and a flag that was set.
        present = f" --{flag} " in clean
        result[f"FLAG_{_var_name(flag)}"] = "true" if present else "false"
        clean = re.sub(rf"(?<=\s)--{re.escape(flag)}(?=\s)", "", clean)

    clean = re.sub(r"\s+", " ", clean).strip()
    if clean.startswith("#"):
        clean = clean[1:].strip()

    result["KEEP_ITEMS"] = keep_items
    result["CLEAN_ARGS"] = clean
    return result


def _validate_flags(raw: str) -> list[str]:
    """Split + validate the ``--flags`` CLI value.

    Args:
        raw: Comma-separated flag names as passed to ``--flags``.

    Returns:
        Validated list of bare flag names, order preserved.

    Raises:
        ValueError: ``raw`` is empty, or any token fails
            ``^[a-z0-9]+(-[a-z0-9]+)*$`` (lowercase, digits, single internal dashes).

    Examples:
        >>> _validate_flags("reply,no-challenge")
        ['reply', 'no-challenge']
        >>> _validate_flags("")
        Traceback (most recent call last):
            ...
        ValueError: --flags must list at least one flag name
        >>> _validate_flags("Reply")
        Traceback (most recent call last):
            ...
        ValueError: invalid flag name: 'Reply'
    """
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    if not tokens:
        raise ValueError("--flags must list at least one flag name")
    for token in tokens:
        if not _FLAG_NAME_RE.match(token):
            raise ValueError(f"invalid flag name: {token!r}")
    return tokens


def _emit(parsed: dict[str, str]) -> str:
    """Render parsed dict as newline-separated ``VAR=value`` shell assignments."""
    return "\n".join(f"{key}={shlex.quote(value)}" for key, value in parsed.items())


def _sentinel_values(parsed: dict[str, str], flags: list[str], value_flags: list[str]) -> dict[str, str]:
    """Map each state-mode sentinel suffix to the value written under it, keep value excluded.

    Raises:
        _StateError: A requested name is reserved, or listed as both a boolean and a value flag; either would make
            two values share one sentinel path, the later silently overwriting the earlier (exit 2).

    Examples:
        >>> _sentinel_values(parse_skill_flags("42 --reply", ["reply"], ["agent"]), ["reply"], ["agent"])
        {'reply': 'true', 'agent': '', 'clean-args': '42'}
    """
    names = [*flags, *value_flags]
    clashes = sorted({name for name in names if name in _RESERVED_SUFFIXES or names.count(name) > 1})
    if clashes:
        raise _StateError(f"--keep-state cannot write a sentinel for {', '.join(clashes)}: the name is taken", 2)
    values = {flag: parsed[f"FLAG_{_var_name(flag)}"] for flag in flags}
    values.update({flag: parsed[f"VALUE_{_var_name(flag)}"] for flag in value_flags})
    values["clean-args"] = parsed["CLEAN_ARGS"]
    return values


def _within(candidate: Path, roots: tuple[Path, ...]) -> bool:
    """Report whether ``candidate`` resolves inside any of ``roots``.

    Resolution happens before the comparison so ``..`` segments and symlinks cannot walk out
    of a root that the unresolved string appears to sit under. Resolution is non-strict on
    both sides, so a root that does not exist yet still matches paths under it — deliberate:
    the shell root ``/tmp`` may be absent on the host when the caller has not created it,
    and rejecting the caller's own directory for not existing yet is the defect this guard
    caused once already in ``extract-keep-flag.py``.

    Examples:
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


def _keep_path(prefix: str, csid: str, out_file: str | None, tmp: Path) -> Path:
    """Choose where the keep value is written: the per-session prefix sentinel, or a confined ``--out-file``.

    Raises:
        _StateError: ``--out-file`` is empty, or resolves outside the temp dir and the working directory (exit 2).
    """
    if out_file is None:
        return tmp / f"{prefix}-keep-items-{csid}"
    if not out_file:
        # Falling back to the prefix path here would exit 0 while writing somewhere the caller does not read, losing
        # the keep value silently. A caller that names the option owes a value.
        raise _StateError("--out-file given without a value", 2)
    # `tmp` is the shell's `${TMPDIR:-/tmp}`. With TMPDIR unset, tempfile.gettempdir() answers TEMP/TMP instead (on
    # native Windows, and on POSIX when either is set); accepting that platform dir as a root too keeps an --out-file
    # computed there, such as one under pytest's tmp_path, from being rejected.
    platform_tmp = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
    sentinel = Path(out_file)
    if not _within(sentinel, (tmp, platform_tmp, Path.cwd())):
        # This script creates parent directories, so an unconstrained path would let a caller — or a blob that
        # reached argv — build a tree anywhere the process can write.
        raise _StateError(f"--out-file {sentinel} is outside the temp dir and the working directory", 2)
    return sentinel


def _persist_state(prefix: str, values: dict[str, str], keep_items: str, out_file: str | None, inlined: bool) -> None:
    """Clear stale compaction state and write every state-mode sentinel for one skill invocation.

    Order matters: both identity checks run before anything is touched; the flag sentinels are written before the
    ``--out-file`` check, so a rejected ``--out-file`` still never leaves a previous run's flag values in place.

    Args:
        prefix: Sentinel prefix, the skill's existing keep-items slug (``analyse`` → ``analyse-keep-items-<CSID>``).
        values: Sentinel suffix → value, from ``_sentinel_values``.
        keep_items: The ``--keep`` value, possibly empty.
        out_file: ``--out-file`` value, or ``None`` when the option was not given.
        inlined: Skip the stale-contract clear, because a parent skill's contract is live.

    Raises:
        _StateError: Identity, ``--out-file`` (exit 2), contract-clear or write (exit 1) failures.
    """
    # No "shared" fallback, unlike other bin/ scripts: an unset CSID is a caller bug, and a guessed token would write
    # sentinels the caller never reads.
    csid = os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or ""
    if not csid:
        raise _StateError("CSID not exported by caller", 2)
    if any(not _NAME_RE.fullmatch(value) or ".." in value for value in (prefix, csid)):
        raise _StateError(
            "--keep-state prefix and CSID must use letters, digits, '.', '_' or '-' and must not contain '..'", 2
        )
    if not inlined and not _stamped_by_other_session():
        try:
            _CONTRACT.unlink(missing_ok=True)
        except OSError as exc:
            raise _StateError(f"cannot clear {_CONTRACT}: {exc}", 1) from exc
    tmp_fallback = tempfile.gettempdir() if os.name == "nt" else "/tmp"  # noqa: S108 - shell default of ${TMPDIR:-/tmp}
    tmp = Path(os.environ.get("TMPDIR") or tmp_fallback)
    targets = {tmp / f"{prefix}-{suffix}-{csid}": value for suffix, value in values.items()}
    for path, value in targets.items():
        _write_sentinel(path, value)
    keep_target = _keep_path(prefix, csid, out_file, tmp)
    # The per-session state directory is the caller's to name but not always its to create: with --out-file this
    # script may be the first writer into it.
    _write_sentinel(keep_target, keep_items, make_parents=out_file is not None)


def _write_sentinel(path: Path, value: str, make_parents: bool = False) -> None:
    """Write one single-line sentinel atomically, converting filesystem errors into a ``_StateError`` (exit 1)."""
    try:
        if make_parents:
            _make_private_dirs(path.parent)
        _write_atomic(path, value + "\n")
    except OSError as exc:
        raise _StateError(f"cannot write {path}: {exc}", 1) from exc


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser; ``$ARGUMENTS`` itself is never an option of this script."""
    parser = argparse.ArgumentParser(
        prog="parse-skill-flags.py",
        description="Parse a skill's $ARGUMENTS blob for anchored-token flags + --keep, emit shell assignments.",
        # No -h/--help: callers run this inside eval "$(...)", so argparse's help would be
        # printed to stdout and then executed as shell source, leaving every parsed variable
        # unset while the skill continued. A blob that is exactly `--help` is argument text
        # like any other and belongs in CLEAN_ARGS.
        add_help=False,
        # Same reason one step further: with abbreviation on, a blob of `--fl` is consumed as
        # this script's own `--flags`, argparse exits 2, and every emitted variable ends unset.
        allow_abbrev=False,
    )
    parser.add_argument(
        "--flags",
        required=True,
        help="Comma-separated bare flag names, no leading --, e.g. reply,no-challenge,worktree",
    )
    parser.add_argument(
        "--value-flags",
        default="",
        help="Comma-separated bare names of flags carrying a value, e.g. agent,repo",
    )
    parser.add_argument(
        "--keep-state",
        default=None,
        metavar="PREFIX",
        help="Also clear a stale compaction contract and write <PREFIX>-<flag>-<CSID> sentinels under TMPDIR",
    )
    parser.add_argument(
        "--out-file",
        default=None,
        help="State mode: write the keep value here instead of <PREFIX>-keep-items-<CSID>",
    )
    parser.add_argument(
        "--inlined",
        action="store_true",
        help="State mode: skip the stale-contract clear (a parent skill loaded this one; its contract is live)",
    )
    return parser


def main(argv: list[str]) -> int:
    """Parse ``--flags`` + the ``$ARGUMENTS`` blob, emit shell assignments, and in state mode persist them.

    Args:
        argv: Raw argv tokens (``sys.argv[1:]``).

    Returns:
        ``0`` on success; ``1`` or ``2`` as listed in the module docstring. Argparse itself
        exits ``2`` when ``--flags`` is missing entirely.

    Examples:
        No doctest — emits to stdout and writes sentinels; covered by pytest via subprocess.
    """
    ns, extra = _build_parser().parse_known_args(argv)

    try:
        flags = _validate_flags(ns.flags)
        value_flags = _validate_flags(ns.value_flags) if ns.value_flags else []
    except ValueError as exc:
        print(f"parse-skill-flags: {exc}", file=sys.stderr)
        return 2

    arguments = " ".join(extra)
    parsed = parse_skill_flags(arguments, flags, value_flags)
    warning = _keep_warning(arguments)
    try:
        if ns.keep_state is not None:
            values = _sentinel_values(parsed, flags, value_flags)
            _persist_state(ns.keep_state, values, parsed["KEEP_ITEMS"], ns.out_file, ns.inlined)
        elif ns.out_file is not None or ns.inlined:
            raise _StateError("--out-file and --inlined are only valid with --keep-state", 2)
    except _StateError as exc:
        print(f"parse-skill-flags: {exc}", file=sys.stderr)
        return exc.exit_code
    if warning:
        print(f"parse-skill-flags: {warning}", file=sys.stderr)
    print(_emit(parsed))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
