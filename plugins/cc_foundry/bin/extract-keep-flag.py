#!/usr/bin/env python3
"""extract-keep-flag.py — initialise a skill's compaction-contract state from its arguments.

Python rather than a shell regex on purpose: the inline twin of this parse used `[[ =~ ]]` +
${BASH_REMATCH[1]}, and zsh — the harness shell — populates $match instead, so every
``--keep "..."`` silently resolved to the empty string.

Usage: python extract-keep-flag.py <sentinel-slug> "$ARGUMENTS" [--venue-choices A,B,C]
                                   [--out-file PATH]
  <sentinel-slug> is the sentinel's own slug, not the skill name (research:run writes
  `research-run-keep-items`, so it passes `research-run`).
  --out-file writes the keep value to PATH instead of the slug-derived sentinel, creating
  parent directories as needed. For skills that keep their state in a per-session
  directory (`<skill>-state-${CSID}/keep-items`) rather than a flat sentinel name; the
  slug is still required, because it prefixes this script's own error messages.
  --venue-choices additionally parses `--venue <token>`, validates it against the given
  comma-separated list, and writes it to ${TMPDIR:-/tmp}/<sentinel-slug>-venue-${CSID}.
  An absent --venue is legal and writes an empty value; the caller's skip rule reads it.
Argv is parsed by hand, not argparse: five live call sites pass "$ARGUMENTS" positionally and
  that string routinely starts with `--`, which argparse would reject as an unknown option.
Side effects: clears a stale .temp/state/skill-contract.md left by a crashed prior run
  (compaction-contract.md §Lifecycle) and writes the keep value to
  ${TMPDIR:-/tmp}/<sentinel-slug>-keep-items-${CSID}. Also prints it (may be empty).
  Both happen before venue validation, so an invalid venue still leaves them written.
Requires: CSID exported by the caller — a child's own parent process id is the calling shell,
  not the Claude Code process, so deriving it here would name a different sentinel.
Exit codes: 0 = ok · 1 = state cleanup or sentinel write failed · 2 = missing slug, missing
  session ID, invalid --venue value, or an --out-file that is empty or outside the temp dir
  and working directory
Most call sites do not check the exit code: every one of them exports CSID on the preceding
  line, so the exit-2 identity paths are unreachable from a skill. Only those two identity
  checks return before the contract is cleared; every later non-zero exit — bad --out-file,
  failed sentinel write, invalid --venue — happens after the clear, so the caller owes no
  cleanup on them.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

# [[:space:]]+ (not a single literal space) so `--keep  "a, b"` parses too — superset of the
# inline form it replaces, never a narrower match.
_KEEP_RE = re.compile(r'--keep[ \t\r\f\v]+"([^"]+)"')

# Unquoted single token, mirroring the `--venue <VENUE>` spelling the fortify skill documents.
_VENUE_RE = re.compile(r"--venue[ \t\r\f\v]+(\S+)")

# CWD-relative, exactly as the shell original: the contract belongs to the project the skill
# is running in, which is the caller's working directory, not this script's location.
_CONTRACT = Path(".temp/state/skill-contract.md")


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
        sentinel.write_text(venue + "\n", encoding="utf-8", newline="\n")
    except OSError as exc:
        print(f"extract-keep-flag: cannot write {sentinel}: {exc}", file=sys.stderr)
        return 1
    return 0


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

    match = _KEEP_RE.search(args)
    keep_items = match.group(1) if match else ""

    try:
        _CONTRACT.unlink(missing_ok=True)
    except OSError as exc:
        print(f"extract-keep-flag: cannot clear {_CONTRACT}: {exc}", file=sys.stderr)
        return 1

    tmp = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
    if "--out-file" in argv[3:] and not out_file:
        # Falling back to the slug path here would exit 0 while writing somewhere the caller
        # does not read, losing the keep value silently. A caller that names the option owes
        # a value.
        print("extract-keep-flag: --out-file given without a value", file=sys.stderr)
        return 2
    # Callers spell the same directory `${TMPDIR:-/tmp}`, which is not what this process sees
    # when TMPDIR is unset: tempfile.gettempdir() answers TEMP/TMP on native Windows and may
    # answer a private per-session dir on macOS. Accepting the shell's spelling as a root too
    # keeps a caller-computed --out-file from being rejected as "outside the temp dir".
    shell_tmp = Path(os.environ.get("TMPDIR") or "/tmp")
    sentinel = Path(out_file) if out_file else tmp / f"{slug}-keep-items-{csid}"
    if out_file and not _within(sentinel, (tmp, shell_tmp, Path.cwd())):
        # Every real caller writes into the session temp dir or the project it runs in. The
        # value is fixed skill text today, but this script creates parent directories, so an
        # unconstrained path would let a future caller — or a blob that reached argv — build
        # a tree anywhere the process can write.
        print(
            f"extract-keep-flag: --out-file {sentinel} is outside the temp dir and the working directory",
            file=sys.stderr,
        )
        return 2
    try:
        # The per-session state directory is the caller's to name but not always its to
        # create: with --out-file this script may be the first writer into it.
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text(keep_items + "\n", encoding="utf-8", newline="\n")
    except OSError as exc:
        print(f"extract-keep-flag: cannot write {sentinel}: {exc}", file=sys.stderr)
        return 1

    print(keep_items)
    if venue_choices:
        return _write_venue(slug, args, venue_choices, tmp / f"{slug}-venue-{csid}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
