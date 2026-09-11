#!/usr/bin/env python3
"""extract-keep-flag.py — initialise a skill's compaction-contract state from its arguments.

Python rather than a shell regex on purpose: the inline twin of this parse used `[[ =~ ]]` +
${BASH_REMATCH[1]}, and zsh — the harness shell — populates $match instead, so every
``--keep "..."`` silently resolved to the empty string.

Usage: python extract-keep-flag.py <sentinel-slug> "$ARGUMENTS" [--venue-choices A,B,C]
  <sentinel-slug> is the sentinel's own slug, not the skill name (research:run writes
  `research-run-keep-items`, so it passes `research-run`).
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
  session ID, or invalid --venue value
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
    sentinel = tmp / f"{slug}-keep-items-{csid}"
    try:
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
