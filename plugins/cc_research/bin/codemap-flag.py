#!/usr/bin/env python3
"""codemap-flag.py — resolve ``--codemap`` / ``--no-codemap`` out of a skill's arguments and persist
the answer to that skill's codemap-enabled sentinel.

Wraps bin/codemap_resolve.py with the flag parsing that was duplicated verbatim in run/ and
verify/. Kept as a separate wrapper rather than folded into codemap_resolve.py because
codemap_resolve.py is also called with an already-resolved mode by other callers.

Usage: CODEMAP_RAW=$(python codemap-flag.py <sentinel-slug> "$ARGUMENTS") || exit 1
  Prints the resolved mode (auto | strict | off) so the caller's prose can branch on it.
  Writes true/false to ${TMPDIR:-/tmp}/<sentinel-slug>-codemap-enabled-${CSID}.
Requires: CSID exported by the caller — a child's own parent process id is the calling shell,
  not the Claude Code process, so deriving it here would name a different sentinel.
Exit codes: 0 = resolved · 1 = ``--codemap`` (strict) but codemap unavailable · 2 = bad args
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Currency sentinel basename read back by skills/_shared/codemap-gates.md. Owned here
# because codemap_resolve.py is byte-identical across plugins and cannot name one.
#: Basename prefix of the codemap currency sentinel file that the shared codemap gate documentation reads back.
CURRENCY_PREFIX = "research-codemap-currency"


def _resolve_mode(args: str) -> str:
    """Map raw skill arguments to auto | strict | off.

    --no-codemap wins over --codemap. The patterns cannot both match anyway (" --codemap "
    needs a space before the dashes, which " --no-codemap " does not supply), but ordering the
    branches this way keeps the precedence explicit rather than incidental.
    """
    padded = f" {args} "
    if " --no-codemap " in padded:
        return "off"
    if " --codemap " in padded:
        return "strict"
    return "auto"


def _run_resolver(raw: str, csid: str) -> tuple[str, bool]:
    """Run bin/codemap_resolve.py for *raw*, returning (stripped stdout, succeeded).

    ``--currency-prefix`` is supplied here because ``codemap_resolve.py`` is byte-identical across consuming plugins
    (propagate_shared.py MANIFEST); research's sentinel name is research's own concern and must never be hard-coded in
    that shared file.
    """
    resolver = Path(__file__).resolve().parent / "codemap_resolve.py"
    # Invoked through sys.executable, not the shebang: a bare `#!` is not honoured
    # on Windows (plugins/CLAUDE.md §Installability).
    command = [sys.executable, str(resolver)]
    env = dict(os.environ, CSID=csid)
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, no shell; command is a fixed resolved script
            [*command, raw, "--currency-prefix", CURRENCY_PREFIX],
            stdout=subprocess.PIPE,
            text=True,
            env=env,
            check=False,
        )
    except OSError as exc:
        print(f"codemap-flag: cannot run codemap_resolve.py: {exc}", file=sys.stderr)
        return "", False
    return proc.stdout.rstrip("\n"), proc.returncode == 0


def main(argv: list[str]) -> int:
    """Resolve --codemap/--no-codemap and persist the skill's codemap-enabled sentinel."""
    slug = argv[1] if len(argv) > 1 else ""
    args = argv[2] if len(argv) > 2 else ""
    if not slug:
        print("codemap-flag: missing <sentinel-slug> argument", file=sys.stderr)
        return 2
    # No "shared" fallback here, unlike the other bin/ scripts: this script's contract is
    # that an unset CSID is a caller bug, and its callers gate on the exit-2.
    csid = os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or ""
    if not csid:
        print("codemap-flag: CSID not exported by caller", file=sys.stderr)
        return 2

    raw = _resolve_mode(args)
    enabled, ok = _run_resolver(raw, csid)
    if not ok:
        if raw == "strict":
            print(
                "! BLOCKED — --codemap (strict) but codemap unavailable; "
                "run /codemap-py:scan-codebase or install codemap plugin",
                file=sys.stderr,
            )
            return 1
        enabled = "false"

    tmp = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
    sentinel = tmp / f"{slug}-codemap-enabled-{csid}"
    try:
        sentinel.write_text(enabled + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"codemap-flag: cannot write {sentinel}: {exc}", file=sys.stderr)
        return 1

    print(raw)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
