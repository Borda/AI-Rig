#!/usr/bin/env python
"""Purpose: List unique commit contributors within a Git range.

Scope: This deterministic helper runs ``git log`` for commit authors and
``Co-authored-by`` trailers, then deduplicates contributor lines by email. Its
default output remains bot-free for existing callers; ``--include-bots``
preserves bot identities for the release skill to aggregate separately. It
does not resolve GitHub handles, real names, contribution summaries, or
LinkedIn links.

Usage:
    extract_contributors.py --range <git-range>
    extract_contributors.py --from <ref> --to <ref>
    extract_contributors.py --range <git-range> --include-bots

Outputs: One sorted ``Name <email>`` line per deduplicated contributor is
written to stdout. The normal exit code is zero, including an empty range.

Failure: Exit 1 reports invalid arguments; exit 2 reports a failed Git log.
Missing Git raises ``FileNotFoundError`` so a calling workflow cannot mistake
an unavailable executable for an empty contributor list.

Used by: ``skills/release/SKILL.md`` for inline notes mode and
``skills/release/modes/changelog-audit-prompt.md`` for delegated release
preparation. Tests in ``tests/test_extract_contributors.py`` cover the pure
classification and subprocess boundary.

Arguments:
    --range:  Full git range string, e.g. ``v1.2.0..HEAD`` or ``v1..v2``.
    --from:   Range lower bound (used with ``--to``); ``..`` joins them.
    --to:     Range upper bound (defaults to ``HEAD`` when ``--from`` given).
    --repo:   Optional repo root passed to ``git -C`` (default: cwd).
    --include-bots: Preserve bot identities for caller-side credit grouping.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from shutil import which

_BOT_LOGIN_RE = re.compile(r"\[bot\]", re.IGNORECASE)
_NOREPLY_RE = re.compile(r"noreply", re.IGNORECASE)
_LINE_RE = re.compile(r"^(?P<name>.*?)\s*<(?P<email>[^>]+)>\s*$")

_GIT_FORMAT = "%aN <%aE>%n%(trailers:key=Co-authored-by,valueonly)"


def is_bot(line: str) -> bool:
    """Return True when a ``Name <email>`` line denotes a bot account.

    Args:
        line: A ``Name <email>`` contributor line.

    Returns:
        True if the line contains ``[bot]`` or a generic ``noreply`` address.
        ``users.noreply.github.com`` is **not** treated as a bot signal — GitHub
        assigns that domain to all humans who commit via the web UI or who enable
        the privacy-email setting, so it is a human indicator, not a bot one.

    Examples:
        >>> is_bot("dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>")
        True
        >>> is_bot("Jirka Borovec <6035284+Borda@users.noreply.github.com>")
        False
        >>> is_bot("Jane Doe <jane@example.com>")
        False
        >>> is_bot("CI <ci@noreply.example.com>")
        True
    """
    if _BOT_LOGIN_RE.search(line):
        return True
    # GitHub privacy / web-UI email — assigned to every human who commits via
    # GitHub web editor or enables privacy email; [bot] check above already
    # catches bots that happen to use this domain.
    if "users.noreply.github.com" in line:
        return False
    return bool(_NOREPLY_RE.search(line))


def dedupe_by_email(lines: list[str], include_bots: bool = False) -> list[str]:
    """Deduplicate contributor lines by email, optionally retaining bots.

    First occurrence of each email wins (preserves its display name). Lines
    without a parseable ``<email>`` are kept and keyed on the whole line.

    Args:
        lines: Raw ``Name <email>`` lines (blank lines and bots may be present).
        include_bots: Preserve bot identities for release credit aggregation.

    Returns:
        Sorted, de-duplicated list of ``Name <email>`` lines.

    Examples:
        >>> dedupe_by_email([
        ...     "Jane Doe <jane@example.com>",
        ...     "J. Doe <jane@example.com>",
        ...     "bot[bot] <bot@noreply.github.com>",
        ...     "Al Pace <al@example.com>",
        ... ])
        ['Al Pace <al@example.com>', 'Jane Doe <jane@example.com>']
    """
    seen: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or (not include_bots and is_bot(line)):
            continue
        match = _LINE_RE.match(line)
        key = match.group("email").lower() if match else line
        seen.setdefault(key, line)
    return sorted(seen.values(), key=str.casefold)


def _build_range(range_arg: str, from_ref: str, to_ref: str) -> str:
    """Resolve the effective git range from CLI args.

    Args:
        range_arg: Value of ``--range`` (empty when unset).
        from_ref: Value of ``--from`` (empty when unset).
        to_ref: Value of ``--to`` (empty when unset).

    Returns:
        The git range string, or empty string when none was provided.

    Examples:
        >>> _build_range("v1..v2", "", "")
        'v1..v2'
        >>> _build_range("", "v1", "v2")
        'v1..v2'
        >>> _build_range("", "v1", "")
        'v1..HEAD'
        >>> _build_range("", "", "")
        ''
    """
    if range_arg:
        return range_arg
    if from_ref:
        return f"{from_ref}..{to_ref or 'HEAD'}"
    return ""


def main(argv: list[str] | None = None) -> int:
    """Entry point — parse args, run ``git log``, print contributor list.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Exit code: 1 on bad args, 2 on git failure, 0 otherwise.

    Examples:
        No doctest — subprocess-dependent; covered by pytest with monkeypatch.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    # Honour only ``-h/--help`` via argparse; all other flags keep the manual reject
    # loop below (exit 1 on unknown arg) — argparse's native exit-2 would break
    # the legacy contract that a bad flag exits 1.
    if args in (["-h"], ["--help"]):
        argparse.ArgumentParser(
            prog="extract_contributors.py",
            description="List unique contributors in a git range.",
        ).parse_args(["-h"])

    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]

    range_arg = from_ref = to_ref = repo = ""
    include_bots = False
    i = 0
    while i < len(args):
        flag = args[i]
        if flag == "--include-bots":
            include_bots = True
            i += 1
            continue
        value = args[i + 1] if i + 1 < len(args) else ""
        if flag == "--range":
            range_arg = value
        elif flag == "--from":
            from_ref = value
        elif flag == "--to":
            to_ref = value
        elif flag == "--repo":
            repo = value
        else:
            print(f"extract_contributors: unknown arg '{flag}'", file=sys.stderr)
            return 1
        i += 2

    if range_arg and (from_ref or to_ref):
        print("extract_contributors: pass either --range or --from/--to, not both", file=sys.stderr)
        return 1

    git_range = _build_range(range_arg, from_ref, to_ref)
    if not git_range:
        print("extract_contributors: --range or --from required", file=sys.stderr)
        return 1

    git = which("git")
    if git is None:
        raise FileNotFoundError("executable not found on PATH: git")

    cmd = [git]
    if repo:
        cmd += ["-C", repo]
    cmd += ["log", git_range, "--no-merges", f"--format={_GIT_FORMAT}"]

    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
    if proc.returncode != 0:
        print(f"extract_contributors: git log failed: {proc.stderr.strip()}", file=sys.stderr)
        return 2

    contributors = dedupe_by_email(proc.stdout.splitlines(), include_bots=include_bots)
    if contributors:
        print("\n".join(contributors))
    return 0


if __name__ == "__main__":
    sys.exit(main())
