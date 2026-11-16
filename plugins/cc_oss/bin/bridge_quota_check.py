#!/usr/bin/env python3
"""Tell whether a persisted bridge reply is a Codex quota stop classified by the bridge harness.

The bridge harness links an incident record through a reply's ``incident`` field (a path relative to the bridge
workspace, inside ``.temp/bridge/incidents/``); the record's ``fault`` names the cause it classified. A reply counts as
a quota stop only when ``incident`` is non-null and that record's ``fault`` is ``quota-exhausted``. Verdict and blockers
text is prose a peer model can write itself, with ``incident: null``, so it is never read here. A missing or unreadable
incident, a path outside the incident store, or any other fault (``rate-limited``, ``capacity``, ``blocked`` from an
older bridge) is not classified, and the caller keeps its normal handling.

The bridge workspace is the launch directory of the calling skill. The incident is looked up relative to the current
directory first, then each ancestor up to the Git repository root, for a shell that moved below the launch directory.
A path with a backslash, or a record that resolves outside ``<workspace>/.temp/bridge/incidents`` (a symlinked store or
record), is not an incident.

A ``foundry:challenger`` runs its own ``bridge:review`` cross-check, whose reply the calling skill never sees. When that
call stops on a harness ``quota-exhausted`` incident, the challenger reports it through two carriers: a top-level
``"codex_blocked": "quota"`` in every JSON envelope it returns (absent otherwise), and a ``## Codex Cross-Check``
section whose first non-blank line opens with ``⚠ codex-blocked:quota``. ``--challenger DIR PATTERN...`` reads the
challenger replies and reports the calling skill persisted directly in ``DIR`` under the file-name globs and reports a
quota stop when any of them carries a signal:

- A reply carrying a JSON envelope decides by that top-level field alone: the whole file is the envelope, or its last
  non-blank line is (a demanded envelope may close a reply as one JSON line).
- Only a reply with no envelope at all counts by its report line: the first non-blank line under the first
  ``## Codex Cross-Check`` heading outside fenced code blocks opens with the quota line.

The challenger reserves that opening line for itself: when Codex ran, it reads ``Codex cross-check completed.`` and the
verbatim Codex output follows. A peer model writes that output, so it may open with the quota line or even carry its own
``## Codex Cross-Check`` heading; both come after the challenger's heading, and only the first heading decides. A report
that quotes the contract does so inside a fenced block (backtick or tilde, nested fences included, an unclosed fence
running to the end; a fence opens on any run of three or more backticks or tildes indented at most three spaces and
closes on the same character, at least as long, with nothing after it), and lines inside a fence are never a heading. A
fence opening the section is not the quota line. These rules match foundry's calibrate reader of the same line.

Rationale and other report prose are never read; any other field value, a nested key, the quota token anywhere else, or
no matching file at all (a challenger that timed out) is not classified.

Usage:
    bridge_quota_check.py REPLY_JSON
    bridge_quota_check.py --challenger DIR PATTERN [PATTERN ...]

Prints ``quota-exhausted`` or ``none`` and exits 0. Exits 2 when the reply file is missing or not a JSON object, or when
the call names both modes or neither.

Consumers: ``skills/resolve/modes/dispatch-prelude.md`` (C1 quota check), ``skills/resolve/modes/comment-dispatch.md``
(Step 12a/12b), ``skills/resolve/modes/dispatch-phase1.md`` (challenger quota check),
``skills/analyse/modes/vitality-codex-review.md`` (Step 5), ``skills/analyse/modes/vitality-adversarial-rework.md``
(Step 6a, bridge reply and challenger envelope) and ``skills/review/modes/post-agent.md`` (Step 4, co-review reply).

Examples:
    >>> incident_path({"incident": ".temp/bridge/incidents/1-ab-quota-exhausted.json"}).name
    '1-ab-quota-exhausted.json'
    >>> incident_path({"incident": None}) is None
    True
    >>> incident_path({"incident": "../outside.json"}) is None
    True
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

#: Incident fault that disables the peer for the rest of a run.
QUOTA_FAULT = "quota-exhausted"
#: Printed for every reply that is not a harness-classified quota stop.
NOT_CLASSIFIED = "none"
#: Directory, relative to the bridge workspace, where the harness writes incident records.
INCIDENT_DIR = PurePosixPath(".temp/bridge/incidents")
#: Top-level challenger envelope key naming why its own Codex cross-check stopped.
CHALLENGER_FIELD = "codex_blocked"
#: ``CHALLENGER_FIELD`` value for a cross-check stopped on a harness ``quota-exhausted`` incident.
CHALLENGER_QUOTA = "quota"
#: Heading of the challenger report section whose first non-blank line carries the no-envelope quota signal; the whole
#: heading line, trailing whitespace aside (a longer heading such as ``## Codex Cross-Check notes`` is another section).
CROSS_CHECK_HEADING = "## Codex Cross-Check"
#: Opening of the line the challenger starts that section with after a quota stop.
CROSS_CHECK_QUOTA_LINE = "⚠ codex-blocked:quota"
#: Characters a fenced code block opens with (CommonMark backtick and tilde fences).
_FENCE_CHARS = "`~"
#: Shortest run of fence characters that opens or closes a fenced block.
_FENCE_MIN_RUN = 3
#: Most leading spaces a fence line may carry; four or more make an indented code line instead.
_FENCE_MAX_INDENT = 3


def incident_path(reply: dict) -> PurePosixPath | None:
    """Return the reply's incident path when it names a record directly inside the bridge incident store.

    Args:
        reply: Parsed bridge public object.

    Returns:
        The workspace-relative incident path, or None when ``incident`` is null, not a string, absolute, climbs with
        ``..``, or points anywhere but a ``.json`` file directly in ``.temp/bridge/incidents/``.

    Examples:
        >>> incident_path({"incident": ".temp/bridge/incidents/sub/x.json"}) is None
        True
    """
    value = reply.get("incident")
    if not isinstance(value, str) or "\\" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.parent != INCIDENT_DIR or path.suffix != ".json":
        return None
    return path


def _git_root() -> Path | None:
    """Return the resolved Git repository root of the current directory, or None outside a repository."""
    git = shutil.which("git")
    if git is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell; git resolved through shutil.which
            [git, "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    top = result.stdout.strip()
    return Path(top).resolve() if result.returncode == 0 and top else None


def workspace_roots() -> list[Path]:
    """Return the directories an incident path may be relative to, nearest first.

    The bridge workspace is the launch directory, which may sit anywhere between the current directory and the Git root.

    Returns:
        The resolved current directory and each ancestor up to and including the Git root; only the current directory
        outside a repository or when it is not below the reported root.

    Examples:
        >>> workspace_roots()[0] == Path.cwd().resolve()
        True
    """
    cwd = Path.cwd().resolve()
    top = _git_root()
    if top is None or (cwd != top and top not in cwd.parents):
        return [cwd]
    roots = [cwd]
    for parent in cwd.parents:
        if top not in parent.parents and parent != top:
            break
        roots.append(parent)
    return roots


def _contained_record(root: Path, relative: PurePosixPath) -> Path | None:
    """Return the incident file when it resolves directly inside ``root``'s incident store, else None.

    Mirrors the bridge's own containment: the root is resolved, the store is not, so a symlinked store or a symlinked
    record pointing elsewhere is rejected.

    Examples:
        >>> _contained_record(Path("/nonexistent"), PurePosixPath(".temp/bridge/incidents/x.json")) is None
        True
    """
    base = root.resolve()
    candidate = base / relative
    if not candidate.is_file():
        return None
    resolved = candidate.resolve()
    return resolved if resolved.parent == base / INCIDENT_DIR else None


def incident_fault(relative: PurePosixPath, roots: list[Path]) -> str | None:
    """Return the ``fault`` of the first readable incident record found under one of ``roots``.

    Args:
        relative: Workspace-relative incident path.
        roots: Candidate workspace directories, in lookup order.

    Returns:
        The record's ``fault`` string, or None when no root holds a contained, readable JSON object with a string
        ``fault``.

    Examples:
        >>> incident_fault(PurePosixPath(".temp/bridge/incidents/absent.json"), [Path("/nonexistent")]) is None
        True
    """
    for root in roots:
        record_file = _contained_record(root, relative)
        if record_file is None:
            continue
        try:
            record = json.loads(record_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        fault = record.get("fault") if isinstance(record, dict) else None
        return fault if isinstance(fault, str) else None
    return None


def classify(reply_file: Path) -> str:
    """Return ``quota-exhausted`` for a harness-classified quota stop, otherwise ``none``.

    Args:
        reply_file: Persisted bridge public object.

    Returns:
        ``QUOTA_FAULT`` or ``NOT_CLASSIFIED``.

    Raises:
        OSError: The reply file cannot be read.
        ValueError: The reply file is not a JSON object.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as tmp:
        ...     reply = Path(tmp, "reply.json")
        ...     _ = reply.write_text('{"status": "blocked", "incident": null}', encoding="utf-8")
        ...     classify(reply)
        'none'
    """
    reply = json.loads(reply_file.read_text(encoding="utf-8"))
    if not isinstance(reply, dict):
        raise ValueError("reply is not a JSON object")
    relative = incident_path(reply)
    if relative is None:
        return NOT_CLASSIFIED
    return QUOTA_FAULT if incident_fault(relative, workspace_roots()) == QUOTA_FAULT else NOT_CLASSIFIED


def _envelope_stopped(envelope: object) -> bool:
    """Return whether a parsed challenger envelope carries the top-level quota field.

    Examples:
        >>> _envelope_stopped({"items": [], "codex_blocked": "quota"}), _envelope_stopped(["codex_blocked", "quota"])
        (True, False)
    """
    return isinstance(envelope, dict) and envelope.get(CHALLENGER_FIELD) == CHALLENGER_QUOTA


def _closing_envelope(lines: list[str]) -> object:
    """Return the JSON value on the last non-blank line, or None when that line is not JSON.

    Examples:
        >>> _closing_envelope(["report", '{"codex_blocked": "quota"}', ""])
        {'codex_blocked': 'quota'}
    """
    last = next((line for line in reversed(lines) if line.strip()), "")
    try:
        return json.loads(last)
    except ValueError:
        return None


def _fence_run(line: str) -> tuple[str, int, str] | None:
    """Return the fence character, run length and trailing text when ``line`` is a fence line, else None.

    A fence line carries at most three leading spaces, then at least three backticks or tildes. Any trailing text is
    allowed, a backtick included: the rule matches the challenger quota reader of foundry's calibrate pipeline, so both
    readers classify the same reply alike.

    Examples:
        >>> _fence_run("````markdown"), _fence_run("   ~~~"), _fence_run("``"), _fence_run("    ```")
        (('`', 4, 'markdown'), ('~', 3, ''), None, None)
        >>> _fence_run("``` a`b")
        ('`', 3, 'a`b')
    """
    stripped = line.lstrip(" ")
    if len(line) - len(stripped) > _FENCE_MAX_INDENT or not stripped or stripped[0] not in _FENCE_CHARS:
        return None
    char = stripped[0]
    run = len(stripped) - len(stripped.lstrip(char))
    return (char, run, stripped[run:].strip()) if run >= _FENCE_MIN_RUN else None


def _closes(fence: tuple[str, int], line: str) -> bool:
    """Return whether ``line`` closes the open ``fence``: same character, at least as long, nothing after it.

    Examples:
        >>> _closes(("`", 4), "```"), _closes(("`", 3), "````")
        (False, True)
        >>> _closes(("`", 3), "```python"), _closes(("`", 3), "~~~")
        (False, False)
    """
    run = _fence_run(line)
    return run is not None and run[0] == fence[0] and run[1] >= fence[1] and not run[2]


def _cross_check_opens_with_quota(lines: list[str]) -> bool:
    """Return whether the first non-blank line under the first unfenced Codex Cross-Check heading is the quota line.

    Scanning stops at that heading: a later heading belongs to verbatim Codex output or to quoted text. Lines inside a
    fenced block are never a heading, and a fence opening the section is not the quota line.

    Examples:
        >>> _cross_check_opens_with_quota(["## Codex Cross-Check", "", "⚠ codex-blocked:quota — out of credits"])
        True
        >>> _cross_check_opens_with_quota(["## Codex Cross-Check", "Codex agrees.", "⚠ codex-blocked:quota"])
        False
        >>> _cross_check_opens_with_quota(["```", "## Codex Cross-Check", "⚠ codex-blocked:quota", "```"])
        False
    """
    fence: tuple[str, int] | None = None
    under_heading = False
    for line in lines:
        if fence is not None:
            fence = None if _closes(fence, line) else fence
        elif under_heading and line.strip():
            return line.startswith(CROSS_CHECK_QUOTA_LINE)
        elif (run := _fence_run(line)) is not None:
            fence = (run[0], run[1])
        elif line.rstrip() == CROSS_CHECK_HEADING:
            under_heading = True
    return False


def _challenger_stopped(reply_file: Path) -> bool:
    """Return whether one persisted challenger reply or report says its own Codex cross-check stopped on quota.

    Examples:
        >>> _challenger_stopped(Path("/nonexistent/challenger.json"))
        False
    """
    try:
        text = reply_file.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return False
    try:
        return _envelope_stopped(json.loads(text))
    except ValueError:
        lines = text.splitlines()
    closing = _closing_envelope(lines)
    if isinstance(closing, dict):
        return _envelope_stopped(closing)
    return _cross_check_opens_with_quota(lines)


def classify_challengers(directory: Path, patterns: list[str]) -> str:
    """Return ``quota-exhausted`` when any persisted challenger reply or report signals a quota stop, else ``none``.

    Args:
        directory: Run directory holding the persisted challenger files; its own path is never a pattern.
        patterns: File-name globs of those files, matched only directly inside ``directory``.

    Returns:
        ``QUOTA_FAULT`` or ``NOT_CLASSIFIED``; a missing directory or no matching file is ``NOT_CLASSIFIED``.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as tmp:
        ...     _ = Path(tmp, "challenger-iter0.json").write_text('{"codex_blocked": "quota"}', encoding="utf-8")
        ...     classify_challengers(Path(tmp), ["challenger-iter*.json", "challenger-iter*.md"])
        'quota-exhausted'
    """
    if not directory.is_dir():
        return NOT_CLASSIFIED
    files = {path for pattern in patterns for path in directory.glob(pattern)}
    replies = sorted(path for path in files if path.parent == directory and path.is_file())
    return QUOTA_FAULT if any(_challenger_stopped(reply) for reply in replies) else NOT_CLASSIFIED


def main(argv: list[str] | None = None) -> int:
    """Print the classification of one persisted bridge reply, or of the persisted challenger replies of a run.

    Args:
        argv: Command-line arguments without the program name; ``sys.argv[1:]`` when None.

    Returns:
        0 after printing the classification, 2 when the reply cannot be read.

    Examples:
        >>> main(["/nonexistent/reply.json"])
        2
    """
    parser = argparse.ArgumentParser(description="Tell whether a persisted bridge reply is a Codex quota stop.")
    parser.add_argument("reply", type=Path, nargs="?", help="persisted bridge public object (JSON)")
    parser.add_argument(
        "--challenger",
        nargs="+",
        metavar="DIR PATTERN",
        help="DIR, then file-name globs: read the challenger quota signal of the matching files directly in DIR",
    )
    args = parser.parse_args(argv)
    if (args.reply is None) == (args.challenger is None) or (args.challenger is not None and len(args.challenger) < 2):
        parser.error("name exactly one of REPLY or --challenger DIR PATTERN [PATTERN ...]")
    if args.challenger is not None:
        directory, *patterns = args.challenger
        print(classify_challengers(Path(directory), patterns))
        return 0
    try:
        result = classify(args.reply)
    except (OSError, ValueError) as error:
        print(f"bridge_quota_check: cannot read reply {args.reply}: {error}", file=sys.stderr)
        return 2
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
