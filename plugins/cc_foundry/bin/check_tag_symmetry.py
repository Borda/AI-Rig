#!/usr/bin/env python
"""check_tag_symmetry.py — Check structural XML tag symmetry in agent/skill .md files.

Detects four failure modes, each independently selectable via ``--check``:

  empty-block     — <tag></tag> with only whitespace between open and close.
  unbalanced      — <tag> count differs from </tag> count.
  escaped-tag     — \\<tag> in prose; should be unescaped for Claude navigation [low].
  underscore-tag  — <tag_name> on its own line; CommonMark reads it as prose, not a tag.

All four are facts about the source tree, so each drives exit code 1 on its own.
The ``escaped-tag`` mode carries [low] severity in its message; splitting it into a
separate pre-commit entry is what makes it independently skippable, not a demotion of
its exit code (bare invocation must keep reporting all three and exiting identically).

Read errors are reported unconditionally regardless of ``--check`` — a file that cannot
be opened is never silently dropped by a subset run.

Applies to structural tags: objective, workflow, inputs, notes, constants,
calibration, not-for, role, initialization, antipatterns-to-flag, core-knowledge.

``empty-block`` and ``escaped-tag`` are registry-driven. ``underscore-tag`` and
``unbalanced`` are not: the registry cannot list a block invented after it was written,
and an unregistered block breaks exactly the same way. ``underscore-tag`` flags any
block-level ``<name_with_underscore>`` line whatever the name; ``unbalanced`` checks the
registry plus every name the file itself uses as a standalone tag line (see
:func:`discover_tags`), so a new block is covered the moment it is written.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/check_tag_symmetry.py" [files...] [options]

Options:
    --check KINDS    Comma-separated modes to run: empty-block, unbalanced, escaped-tag
                     (default: all three).
    --timeout SECS   Accepted for call-site uniformity; unused (pure file I/O).

Output (stdout):
    One finding line per violation with prefix "! C14a:", or a single pass line.

Exit codes:
    0   all files pass
    1   one or more violations found
    2   argument error (unknown ``--check`` mode)
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class FindingKind(str, Enum):
    """Which subcheck produced a finding — the ``--check`` selector token.

    Subclasses ``str`` rather than ``enum.StrEnum`` because ``requires-python`` is
    ``>=3.10`` and ``StrEnum`` landed in 3.11. The ``str`` mixin keeps
    ``FindingKind.UNBALANCED == "unbalanced"`` true for comparison and f-strings.

    ``READ_ERROR`` is not a selectable mode: it is emitted by every run so that an
    unreadable file cannot be hidden by narrowing ``--check``.

    Examples:
        >>> FindingKind.EMPTY_BLOCK == "empty-block"
        True
        >>> FindingKind("escaped-tag") is FindingKind.ESCAPED_TAG
        True
    """

    EMPTY_BLOCK = "empty-block"
    UNBALANCED = "unbalanced"
    ESCAPED_TAG = "escaped-tag"
    UNDERSCORE_TAG = "underscore-tag"
    READ_ERROR = "read-error"


#: Modes a caller may name in ``--check``, in default run order.
SELECTABLE_KINDS: tuple[FindingKind, ...] = (
    FindingKind.EMPTY_BLOCK,
    FindingKind.UNBALANCED,
    FindingKind.ESCAPED_TAG,
    FindingKind.UNDERSCORE_TAG,
)


@dataclass
class Finding:
    """One tag-symmetry violation, tagged with the subcheck that found it.

    Attributes:
        kind: The subcheck responsible — used to filter by ``--check``.
        message: Human-readable violation text, file path already prepended.
    """

    kind: FindingKind
    message: str


#: Head and word-segment of a structural tag name, shared by every grammar below so the
#: underscore and discovery patterns cannot drift apart as the convention evolves.
_NAME_HEAD = r"[a-z][a-z0-9]*"
_NAME_SEGMENT = r"[a-z0-9]+"

#: A block-level tag line whose name carries an underscore. CommonMark's raw-HTML
#: tagname is a letter followed by letters, digits or hyphens — an underscore ends the
#: tag, so the line is parsed as prose and every formatter escapes the `<`.
UNDERSCORE_TAG_LINE = re.compile(rf"^[ \t]*</?({_NAME_HEAD}(?:_{_NAME_SEGMENT})+)>[ \t]*$", re.MULTILINE)

#: A tag standing alone on its own line — how every structural block is opened and closed.
#: Standing alone is the signal that separates a structural block from a `<path>`-style
#: placeholder in prose, so it is what qualifies a name for discovery. Legacy underscore
#: names are admitted deliberately: they are still blocks, and a balance defect in one
#: must be reported alongside the rename advice rather than waiting for the rename.
STRUCTURAL_TAG_LINE = re.compile(rf"^[ \t]*</?({_NAME_HEAD}(?:[_-]{_NAME_SEGMENT})*)>[ \t]*$", re.MULTILINE)

STRUCTURAL_TAGS = (
    "objective",
    "workflow",
    "inputs",
    "notes",
    "constants",
    "calibration",
    "not-for",
    "role",
    "initialization",
    "antipatterns-to-flag",
    "core-knowledge",
)

#: Guard against pathological inputs that would exhaust heap memory when read in one shot.
#: 10 MB is well above any realistic Markdown / agent file. Matches the constant used by
#: check_mode_dispatch.py, check_orphaned_bin.py, check_routing_links.py and extract_code_blocks.py.
_MAX_FILE_SIZE = 10 * 1024 * 1024


def strip_fenced_blocks(text: str) -> str:
    """Drop every fenced code block, honouring fences longer than three backticks.

    A regex over ```` ```…``` ```` mis-pairs when a block is opened with four backticks
    because it contains a literal ```` ``` ```` — it closes on the inner delimiter and
    leaves the block's body in the output. Matching the closing run's length to the
    opening one keeps such templates fully stripped.

    Args:
        text: Markdown source.

    Returns:
        The source with fenced blocks (and their delimiters) removed.

    Examples:
        >>> strip_fenced_blocks("a\\n```\\n<x>\\n```\\nb\\n")
        'a\\nb\\n'
        >>> strip_fenced_blocks("a\\n````\\n```\\n<x>\\n```\\n````\\nb\\n")
        'a\\nb\\n'
    """
    out: list[str] = []
    fence: str | None = None
    for line in text.split("\n"):
        stripped = line.lstrip()
        if fence is None:
            match = re.match(r"(`{3,}|~{3,})", stripped)
            if match:
                fence = match.group(1)
                continue
            out.append(line)
        elif re.fullmatch(re.escape(fence[0]) + f"{{{len(fence)},}}", stripped.rstrip()):
            fence = None
    return "\n".join(out)


def discover_tags(normalized: str) -> list[str]:
    """Return the structural tag names a file actually uses, in first-seen order.

    The registry cannot know about a block someone invented last week, and that gap is
    exactly where an unbalanced pair hides. A name qualifies when it appears at least once
    standing alone on its own line: that is how structural blocks are written, and it is
    what a `<output-path>` placeholder mentioned mid-sentence never does. Counting then
    uses every occurrence of the discovered name, so a block opened inline still pairs
    with its own closing line.

    Args:
        normalized: Markdown with fenced blocks, code spans and HTML comments removed.

    Returns:
        Tag names, deduplicated, in the order first encountered.

    Examples:
        >>> discover_tags("<workflow>\\nstep\\n</workflow>\\n")
        ['workflow']
        >>> discover_tags("Pass `--out <output-path>` to the script.\\n")
        []
        >>> discover_tags("<legacy_block>\\nx\\n</legacy_block>\\n")
        ['legacy_block']
    """
    return list(dict.fromkeys(STRUCTURAL_TAG_LINE.findall(normalized)))


def check_file(path: Path) -> list[Finding]:
    """Return every violation for path, empty list if clean.

    All subchecks always run; callers filter by :attr:`Finding.kind`. Findings are
    grouped by subcheck in ``SELECTABLE_KINDS`` order — escaped tags, underscore names,
    empty blocks, then unbalanced pairs — so a filtered run and a bare run agree on the
    relative order of whatever they both report.

    Args:
        path: Path to the .md file to check.

    Returns:
        List of Finding objects with file path already prepended to each message.

    Examples:
        >>> import tempfile, pathlib
        >>> p = pathlib.Path(tempfile.mktemp(suffix=".md"))
        >>> _ = p.write_text("<objective>\\ncontent\\n</objective>\\n")
        >>> check_file(p)
        []
        >>> _ = p.write_text("<notes></notes>\\n")
        >>> [f.kind.value for f in check_file(p)]
        ['empty-block']
        >>> import os; os.unlink(p)
    """
    try:
        if path.stat().st_size > _MAX_FILE_SIZE:
            return [Finding(FindingKind.READ_ERROR, f"{path}: skipped — exceeds {_MAX_FILE_SIZE} byte read guard")]
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [Finding(FindingKind.READ_ERROR, f"{path}: cannot read — {exc}")]

    violations: list[Finding] = []

    # For empty-block check: strip HTML comments but preserve code fences
    # (a block is empty only when it has no content at all, including no code blocks).
    content_for_empty = re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL)

    # For balance check: fences, then code spans, then comments. Order matters — comments
    # first deletes the innards of a span that quotes one (`<!-- policy-sibling: … -->`),
    # leaving its two backticks adjacent; every later span on the line then pairs one
    # delimiter off and real prose survives as if it were code, leaking whatever tags it
    # mentions. Stripping spans while their delimiters still bracket text avoids that.
    content_for_balance = strip_fenced_blocks(content)
    content_for_balance = re.sub(r"`[^`\n]+`", "", content_for_balance)
    content_for_balance = re.sub(r"<!--.*?-->", "", content_for_balance, flags=re.DOTALL)

    # Escaped structural tags: \<tag> in prose (after comment+fence+backtick stripping).
    # Severity: low — prevents Claude navigation; autofix unsafe (may be intentional).
    # Only flag open form \<tag>; closing \</tag> is rarer and not structural.
    escaped_tag_pattern = r"\\<(" + "|".join(re.escape(t) for t in STRUCTURAL_TAGS) + r")>"
    for match in re.finditer(escaped_tag_pattern, content_for_balance, re.IGNORECASE):
        violations.append(
            Finding(
                FindingKind.ESCAPED_TAG,
                f"{path}: escaped structural tag \\<{match.group(1)}> "
                f"— should be unescaped for Claude navigation [low]",
            )
        )

    # Underscore tag names: not registry-driven — any block-level <a_b> line qualifies,
    # including names nobody has added to STRUCTURAL_TAGS yet. Reported once per name.
    for name in dict.fromkeys(UNDERSCORE_TAG_LINE.findall(content_for_balance)):
        violations.append(
            Finding(
                FindingKind.UNDERSCORE_TAG,
                f"{path}: underscore in structural tag <{name}> — CommonMark tag names take "
                f"letters, digits and hyphens only; rename to <{name.replace('_', '-')}>",
            )
        )

    for tag in STRUCTURAL_TAGS:
        # Empty block: open + optional whitespace + close (check before fence-stripping)
        if re.search(rf"<{tag}>\s*</{tag}>", content_for_empty, re.IGNORECASE):
            violations.append(Finding(FindingKind.EMPTY_BLOCK, f"{path}: empty block <{tag}></{tag}>"))

    # Balance runs over the registry plus whatever this file actually uses. The registry
    # stays in the union so a known block still reports when its only occurrences got
    # mangled into prose and no whole-line form survives to be discovered.
    for tag in dict.fromkeys((*STRUCTURAL_TAGS, *discover_tags(content_for_balance))):
        # Unbalanced: open count != close count (check after fence+span+comment stripping).
        # The open form tolerates attributes (`<details open>`) so an attributed block
        # still pairs with its plain closing tag; the closing form never carries any.
        # Exclude backslash-escaped form \<tag> — those are flagged separately above.
        name = re.escape(tag)
        opens = len(re.findall(rf"(?<!\\)<{name}(?:\s[^>]*)?>", content_for_balance, re.IGNORECASE))
        closes = len(re.findall(rf"(?<!\\)</{name}>", content_for_balance, re.IGNORECASE))
        if opens != closes:
            violations.append(
                Finding(FindingKind.UNBALANCED, f"{path}: unbalanced <{tag}> — {opens} open, {closes} close")
            )

    return violations


def parse_kinds(spec: str) -> set[FindingKind]:
    """Parse a comma-separated ``--check`` spec into selectable kinds.

    Args:
        spec: Comma-separated mode tokens, e.g. ``"empty-block,unbalanced"``.

    Returns:
        Set of selected FindingKind members.

    Raises:
        ValueError: When any token is not a selectable mode. The message lists the
            unknown tokens so the CLI can surface them verbatim.

    Examples:
        >>> sorted(k.value for k in parse_kinds("unbalanced,empty-block"))
        ['empty-block', 'unbalanced']
        >>> parse_kinds("read-error")
        Traceback (most recent call last):
        ...
        ValueError: unknown check mode(s): read-error
    """
    selectable = {k.value: k for k in SELECTABLE_KINDS}
    tokens = [t.strip().lower() for t in spec.split(",") if t.strip()]
    unknown = sorted({t for t in tokens if t not in selectable})
    if unknown:
        raise ValueError(f"unknown check mode(s): {', '.join(unknown)}")
    return {selectable[t] for t in tokens}


def main(argv: list[str] | None = None) -> int:
    """Run the selected tag symmetry subchecks on provided files.

    Args:
        argv: Argument list; defaults to sys.argv[1:] when None.

    Returns:
        0 if all files pass, 1 if violations found, 2 on an unknown ``--check`` mode.
    """
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("files", nargs="*", help="Files to check")
    parser.add_argument(
        "--check",
        default=",".join(k.value for k in SELECTABLE_KINDS),
        metavar="KINDS",
        help=("Comma-separated subchecks to run: empty-block, unbalanced, escaped-tag, underscore-tag (default: all)."),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        help="Timeout in seconds (default: 10; unused — pure file I/O)",
    )
    args = parser.parse_args(argv)

    try:
        active = parse_kinds(args.check)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not args.files:
        print("✓: Check 14 — no files provided")
        return 0

    # Read errors are never filtered out — a subset run must not hide an unreadable file.
    keep = active | {FindingKind.READ_ERROR}
    label = "" if active == set(SELECTABLE_KINDS) else f" [{','.join(sorted(k.value for k in active))}]"
    all_violations: list[Finding] = []
    checked = 0

    for file_arg in args.files:
        path = Path(file_arg)
        if not path.is_file():
            continue
        checked += 1
        all_violations.extend(f for f in check_file(path) if f.kind in keep)

    if all_violations:
        for v in all_violations:
            print(f"! C14a: {v.message}")
        return 1

    print(f"✓: Check 14a{label} — no tag symmetry violations ({checked} files checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
