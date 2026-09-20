#!/usr/bin/env python
"""parse-resolve-args.py — parse oss:resolve $ARGUMENTS, emit shell variable assignments for eval.

Usage (Claude Code plugin — ``CLAUDE_PLUGIN_ROOT`` set automatically)::

    eval "$(python "${CLAUDE_PLUGIN_ROOT}/bin/parse-resolve-args.py" "$ARGUMENTS")"

Emits four shell-quoted variable assignments:

- ``PR_NUMBER`` — extracted PR number (bare digits, ``#N``, or the trailing digits of a
  ``.../pull/N`` URL); empty otherwise. A GitHub URL form previously left this empty, which
  routed the reject-gate check and the report-source lookup on ``n/a`` — invisible to both.
- ``PR_URL`` — full GitHub PR URL; empty otherwise
- ``MODE`` — one of ``pr``, ``pr+report``, ``report``, ``comment-dispatch``
- ``ARGUMENTS`` — original input; leading ``#`` stripped only for ``comment-dispatch``

Match order (PR/URL/report tried before any string mutation, so prompts like
``#42 looks wrong`` still route to comment-dispatch rather than misroute):

1. PR number (with optional leading ``#``; ``report`` may lead or trail — ``42 report`` == ``report 42``)
2. GitHub PR URL (``report`` may lead or trail)
3. Bare ``report``
4. Otherwise comment-dispatch (strip exactly one leading ``#``)

Exit codes:
    0 — assignments emitted

Note: the ``$ARGUMENTS`` blob is forwarded verbatim to the internal regex parser;
argparse is used only to provide ``-h``/``--help`` and never consumes blob tokens
(``parse_known_args`` lets dash-leading prose fall through untouched).
"""

from __future__ import annotations

import argparse
import re
import shlex
import sys
from enum import Enum
from typing import Final


class ResolveMode(str, Enum):
    """Routing verdict emitted as the ``MODE`` shell variable.

    Subclasses ``str`` (not ``enum.StrEnum``) because ``requires-python`` is ``>=3.10``. Members are listed in match
    order — PR, PR-with-report, bare report, then the comment-dispatch fallback.
    """

    PR = "pr"
    PR_REPORT = "pr+report"
    REPORT = "report"
    COMMENT_DISPATCH = "comment-dispatch"


# ``report`` may lead or trail the target — ``42 report`` and ``report 42`` are the same request.
# Both positions are captured so either one flips MODE to pr+report.
_PR_NUMBER_RE: Final = re.compile(r"^\s*(report\s+)?#?(\d+)(\s+report)?\s*$")
# group(3) captures the PR number from a .../pull/N path. A pasted PR link often carries a tab or
# thread tail (``/files``, ``/commits``, ``#discussion_r123``, ``?diff=split``); the tail is accepted
# and dropped so PR_URL is the canonical PR reference ``gh`` understands.
_PR_URL_RE: Final = re.compile(r"^\s*(report\s+)?(https://github\.com/\S+?/pull/(\d+))(?:[/#?]\S*)?(\s+report)?\s*$")
# Any other github.com URL (plain repo link, issue link) still routes on PR_URL alone with PR_NUMBER
# empty — downstream gates then report ``n/a`` instead of handing the URL to comment dispatch.
_GH_URL_RE: Final = re.compile(r"^\s*(report\s+)?(https://github\.com/\S+?)(\s+report)?\s*$")
_BARE_REPORT_RE: Final = re.compile(r"^\s*report\s*$")


def parse_resolve_args(arguments: str) -> dict[str, str]:
    """Parse oss:resolve arguments into a dict of shell-variable values.

    >>> parse_resolve_args("42") == {
    ...     "PR_NUMBER": "42", "PR_URL": "", "MODE": "pr", "ARGUMENTS": "42",
    ... }
    True
    >>> parse_resolve_args("#42 report") == {
    ...     "PR_NUMBER": "42", "PR_URL": "", "MODE": "pr+report", "ARGUMENTS": "#42 report",
    ... }
    True
    >>> parse_resolve_args("report 42") == {
    ...     "PR_NUMBER": "42", "PR_URL": "", "MODE": "pr+report", "ARGUMENTS": "report 42",
    ... }
    True
    >>> parse_resolve_args("https://github.com/owner/repo/pull/7") == {
    ...     "PR_NUMBER": "7", "PR_URL": "https://github.com/owner/repo/pull/7",
    ...     "MODE": "pr", "ARGUMENTS": "https://github.com/owner/repo/pull/7",
    ... }
    True
    >>> parse_resolve_args("https://github.com/owner/repo/pull/7/files#discussion_r1 report") == {
    ...     "PR_NUMBER": "7", "PR_URL": "https://github.com/owner/repo/pull/7",
    ...     "MODE": "pr+report", "ARGUMENTS": "https://github.com/owner/repo/pull/7/files#discussion_r1 report",
    ... }
    True
    >>> parse_resolve_args("https://github.com/owner/repo") == {
    ...     "PR_NUMBER": "", "PR_URL": "https://github.com/owner/repo",
    ...     "MODE": "pr", "ARGUMENTS": "https://github.com/owner/repo",
    ... }
    True
    >>> parse_resolve_args("#42 looks wrong") == {
    ...     "PR_NUMBER": "", "PR_URL": "", "MODE": "comment-dispatch",
    ...     "ARGUMENTS": "42 looks wrong",
    ... }
    True
    """
    pr_number = ""
    pr_url = ""
    mode = ResolveMode.COMMENT_DISPATCH
    out_args = arguments

    m = _PR_NUMBER_RE.match(arguments)
    if m:
        pr_number = m.group(2)
        mode = ResolveMode.PR_REPORT if (m.group(1) or m.group(3)) else ResolveMode.PR
    else:
        m = _PR_URL_RE.match(arguments)
        gh = None if m else _GH_URL_RE.match(arguments)
        if m:
            pr_url = m.group(2)
            pr_number = m.group(3)  # reject-gate check and report-source lookup are PR-scoped on this
            mode = ResolveMode.PR_REPORT if (m.group(1) or m.group(4)) else ResolveMode.PR
        elif gh:
            pr_url = gh.group(2)
            mode = ResolveMode.PR_REPORT if (gh.group(1) or gh.group(3)) else ResolveMode.PR
        elif _BARE_REPORT_RE.match(arguments):
            mode = ResolveMode.REPORT
        else:
            # Only now strip a single leading '#'; comment dispatch may carry it
            # as a Markdown header anchor.
            if out_args.startswith("#"):
                out_args = out_args[1:]

    return {
        "PR_NUMBER": pr_number,
        "PR_URL": pr_url,
        "MODE": mode.value,
        "ARGUMENTS": out_args,
    }


def _emit(parsed: dict[str, str]) -> str:
    """Render parsed dict as newline-separated ``VAR=value`` shell assignments."""
    return "\n".join(f"{key}={shlex.quote(value)}" for key, value in parsed.items())


def main(argv: list[str]) -> int:
    """Parse the ``$ARGUMENTS`` blob and emit shell assignments.

    Args:
        argv: Raw argv tokens (``sys.argv[1:]``). Space-joined into the blob and
            forwarded verbatim to :func:`parse_resolve_args`.

    Returns:
        Exit code ``0``; argparse exits ``2`` when the *sole* argument is a bad
        ``-h``/``--help`` variant.

    Examples:
        No doctest — emits to stdout; covered by pytest via subprocess.
    """
    # argparse only intercepts a lone ``-h/--help`` so users get discoverable help;
    # for any real payload the blob is forwarded untouched (dash-leading prose,
    # e.g. a comment starting with "-", must reach the regex parser intact).
    if argv in (["-h"], ["--help"]):
        argparse.ArgumentParser(
            prog="parse-resolve-args.py",
            description="Parse oss:resolve $ARGUMENTS, emit shell variable assignments for eval.",
        ).parse_args(argv)  # prints help, exits 0

    arguments = " ".join(argv)
    parsed = parse_resolve_args(arguments)
    print(_emit(parsed))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
