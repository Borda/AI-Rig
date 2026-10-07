#!/usr/bin/env python3
"""check_mode_dispatch.py — detect dangling mode-dispatch references in plugin SKILL.md files.

A SKILL.md often routes control to a named mode section, e.g.::

    **If first token equals `memory`**: ... go to "Mode: Memory Distillation" below.

That dispatch is only valid when the same file also contains a matching header::

    ## Mode: Memory Distillation

A half-done rename leaves a dispatch line pointing at a header that no longer exists
(the exact class of bug fixed in ``distill/SKILL.md`` where a line said
``go to "Mode: Lessons Distillation"`` but no ``## Mode: Lessons Distillation`` header
remained). This checker catches that: for each SKILL.md it extracts every dispatch
reference to ``Mode: <Name>`` and verifies a matching ``## Mode: <Name>`` header exists.

Recognised dispatch forms (verb case-insensitive):

  - ``go to "Mode: <Name>"``      / ``go to **Mode: <Name>**``
  - ``skip to "Mode: <Name>"``    / ``skip to **Mode: <Name>**``
  - ``see "Mode: <Name>"``        / ``see **Mode: <Name>**``

Header matching allows ``##`` or ``###`` and an optional trailing ``— <qualifier>``
after the name, e.g. ``## Mode: Memory Distillation — only when …`` matches a reference
to ``Mode: Memory Distillation``. Names are compared case-sensitively on the trimmed value.

Usage:
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_mode_dispatch.py" <file>...
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_mode_dispatch.py" --scan-dir plugins

Options:
    --scan-dir DIR   Scan every ``*/skills/*/SKILL.md`` under DIR instead of explicit files.

Output (stdout):
    One finding line per dangling reference:
        MODE-DISPATCH: <file>: references "Mode: X" with no matching header
    Or a single pass line when clean.

Exit codes:
    0   no dangling references
    1   one or more dangling references
    2   argument error
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

#: Dispatch reference: verb (go to / skip to / see) + "Mode: <Name>" or **Mode: <Name>**.
#: Name runs until the first closing quote ("), asterisk (*), em-dash (—), or newline.
_DISPATCH_RE = re.compile(
    r'(?:go to|skip to|see)\s+(?:"|\*\*)\s*Mode:\s*([^"*—\n]+)',
    re.IGNORECASE,
)

#: Header: ## Mode: <Name> or ### Mode: <Name>, optional trailing "— <qualifier>".
_HEADER_RE = re.compile(r"^#{2,3}\s+Mode:\s*(.+?)\s*$", re.MULTILINE)

#: Files larger than this many bytes (10 MiB) are skipped rather than read into memory.
_MAX_FILE_SIZE = 10 * 1024 * 1024


@dataclass
class Finding:
    """A dangling mode-dispatch reference.

    Attributes:
        source_file: Path string of the SKILL.md holding the reference.
        mode_name: Referenced mode name with no matching header.
    """

    source_file: str
    mode_name: str

    @property
    def message(self) -> str:
        """Return the canonical one-line finding message."""
        return f'MODE-DISPATCH: {self.source_file}: references "Mode: {self.mode_name}" with no matching header'


def _strip_qualifier(name: str) -> str:
    """Return a mode name with any trailing qualifier and surrounding space removed.

    A qualifier is an em-dash clause (``— only when …``) or a parenthetical
    (``(alias: --challenge)``) appended after the canonical name in a header;
    either is cut so a bare reference matches a qualified header.

    Args:
        name: Raw name captured from a reference or header.

    Returns:
        Trimmed name up to (but excluding) the first ``—`` or ``(`` qualifier.

    Examples:
        >>> _strip_qualifier("Memory Distillation — only when foo")
        'Memory Distillation'
        >>> _strip_qualifier("adversarial (alias: --challenge)")
        'adversarial'
        >>> _strip_qualifier("  Lessons Distillation  ")
        'Lessons Distillation'
    """
    return name.split("—", 1)[0].split("(", 1)[0].strip()


def extract_mode_refs(text: str) -> list[str]:
    """Extract every dispatched ``Mode: <Name>`` reference from file text.

    Args:
        text: Full SKILL.md contents.

    Returns:
        List of trimmed mode names in first-seen order (deduplicated).

    Examples:
        >>> extract_mode_refs('go to "Mode: Memory Distillation" below.')
        ['Memory Distillation']
        >>> extract_mode_refs('Skip to **Mode: Executables Extraction** below.')
        ['Executables Extraction']
        >>> extract_mode_refs('see **Mode: A** and go to "Mode: B"')
        ['A', 'B']
        >>> extract_mode_refs('nothing dispatched here')
        []
    """
    names: list[str] = []
    seen: set[str] = set()
    for match in _DISPATCH_RE.finditer(text):
        name = _strip_qualifier(match.group(1))
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def extract_mode_headers(text: str) -> set[str]:
    """Extract every ``## Mode: <Name>`` / ``### Mode: <Name>`` header name from file text.

    Args:
        text: Full SKILL.md contents.

    Returns:
        Set of trimmed header names with any ``— <qualifier>`` suffix stripped.

    Examples:
        >>> sorted(extract_mode_headers("## Mode: Memory Distillation\\n### Mode: Pruning"))
        ['Memory Distillation', 'Pruning']
        >>> sorted(extract_mode_headers("## Mode: Memory Distillation — only when x"))
        ['Memory Distillation']
        >>> extract_mode_headers("# Mode: NotAHeader")
        set()
    """
    return {_strip_qualifier(m.group(1)) for m in _HEADER_RE.finditer(text)}


def check_file(path: Path) -> list[Finding]:
    """Return dangling-dispatch findings for a single SKILL.md file.

    A finding is produced for each dispatched ``Mode: <Name>`` reference with no
    matching header in the same file.

    Args:
        path: Path to the SKILL.md file.

    Returns:
        List of Finding objects (empty when the file is clean or unreadable).
    """
    try:
        if path.stat().st_size > _MAX_FILE_SIZE:
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    headers = extract_mode_headers(text)
    return [Finding(str(path), name) for name in extract_mode_refs(text) if name not in headers]


def find_skill_files(scan_dir: Path) -> list[Path]:
    """Return every ``*/skills/*/SKILL.md`` under a directory.

    Args:
        scan_dir: Root directory to scan (e.g. ``plugins``).

    Returns:
        Sorted list of matching SKILL.md paths.
    """
    return sorted(scan_dir.glob("*/skills/*/SKILL.md"))


def _build_parser() -> argparse.ArgumentParser:
    """Return the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="check_mode_dispatch",
        description="Detect dangling Mode: dispatch references in plugin SKILL.md files.",
    )
    parser.add_argument("files", nargs="*", metavar="FILE", help="SKILL.md files to check.")
    parser.add_argument(
        "--scan-dir",
        metavar="DIR",
        help="Scan every */skills/*/SKILL.md under DIR instead of explicit files.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code: 0 clean, 1 dangling reference(s) found, 2 argument error.
    """
    args = _build_parser().parse_args(argv)

    targets: list[Path] = []
    if args.scan_dir:
        scan_dir = Path(args.scan_dir)
        if not scan_dir.is_dir():
            print(f"error: {args.scan_dir!r} is not a directory", file=sys.stderr)
            return 2
        targets.extend(find_skill_files(scan_dir))
    targets.extend(Path(f) for f in args.files)

    if not targets:
        print("error: no files to check — pass files or --scan-dir", file=sys.stderr)
        return 2

    findings: list[Finding] = []
    for path in targets:
        findings.extend(check_file(path))

    if findings:
        print("\n".join(f.message for f in findings))
        return 1

    print("✓: check_mode_dispatch — all Mode: dispatch references have matching headers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
