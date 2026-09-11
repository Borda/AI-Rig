#!/usr/bin/env python
"""scan_efficiency_signals.py — deterministic cost signals for /foundry:audit --efficiency.

Replaces the inline Phase B scan block. Four sections, printed in the order the
efficiency consolidator expects:

``Unbounded spawn patterns``
    An ``Agent(`` call inside a ``for``/``while`` with no batch guard nearby.
``Missing model declarations``
    A skill or agent whose frontmatter has no ``model:`` line, so it inherits the
    session model. There is no ``disable-model-invocation`` exemption: such a
    skill is still user-invocable and still inherits.
``Boilerplate duplication``
    File counts for three recurring inline patterns.
``Bin/ extraction candidates``
    File counts for two resolution patterns that belong in ``bin/``.

Frontmatter is scoped to the block between the first two ``---`` lines; a
whole-file search would match these field names in prose discussing other skills.

Usage:
    scan_efficiency_signals.py [--scan-dir plugins/]

Exit codes:
    0   always — this is a reporting scan, not a gate
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

#: Lines before an `Agent(` call that are inspected for an enclosing loop.
LOOP_LOOKBEHIND = 5

_LOOP = re.compile(r"^\s*(for|while)\b")
_BATCH_GUARD = re.compile(r"BATCH_SIZE|EFFECTIVE_BATCH|head -n? ?\d+")
_MODEL_FIELD = re.compile(r"^model:", re.MULTILINE)

#: Section label → regex counted across files, for the two count-only sections.
BOILERPLATE: dict[str, str] = {
    "agent-resolution boilerplate": r"=\$\(ls -td.*plugins/cache",
    "unsupported-flag-check boilerplate": r"Unknown flag",
    # Keyed on HARD_CUTOFF, not the retired MONITOR_INTERVAL: spawns are background and the
    # orchestrator ends its turn, so no skill declares a poll interval any more. Matching the
    # old token would count zero files and quietly retire the section.
    "health-monitoring constants": r"HARD_CUTOFF=",
}

EXTRACTION: dict[str, str] = {
    "mode-dispatch pattern": r"find.*plugins/cache.*-path.*modes/",
    "_shared resolution pattern": r"=\$\(find.*plugins/cache.*_shared|=\$\(ls -td.*plugins/cache",
}


def frontmatter(text: str) -> str:
    """Return the text between the first two `---` lines, or '' when absent."""
    parts = text.split("\n---", 2)
    if not text.startswith("---") or len(parts) < 2:
        return ""
    return parts[0][3:]


def unbounded_spawns(path: Path, text: str) -> str | None:
    """Return a finding line when the file dispatches agents inside an unguarded loop."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if "Agent(" not in line:
            continue
        window = lines[max(0, index - LOOP_LOOKBEHIND) : index]
        if not any(_LOOP.match(w) for w in window):
            continue
        if _BATCH_GUARD.search(text):
            continue
        return f"UNBOUNDED_SPAWN: {path.as_posix()} — Agent() inside for/while without BATCH_SIZE guard"
    return None


def missing_model(path: Path, text: str) -> str | None:
    """Return a finding line when the file's frontmatter declares no model."""
    if _MODEL_FIELD.search(frontmatter(text)):
        return None
    return f"NO_MODEL: {path.as_posix()}"


def _declares_model(path: Path) -> bool:
    """Report whether a file is one that must declare a model tier."""
    posix = path.as_posix()
    if "/agents/" in posix:
        return True
    return path.name == "SKILL.md" and path.parent.parent.name == "skills"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _count_matching(files: dict[Path, str], pattern: str) -> int:
    compiled = re.compile(pattern)
    return sum(1 for text in files.values() if compiled.search(text))


def scan(scan_dir: Path) -> int:
    """Print every section for one scope directory."""
    markdown = sorted(scan_dir.rglob("*.md"))
    texts = {path: _read(path) for path in markdown}

    print("=== Unbounded spawn patterns ===")
    for path, text in texts.items():
        finding = unbounded_spawns(path, text)
        if finding:
            print(finding)

    print("=== Missing model declarations ===")
    declaring = [path for path in markdown if _declares_model(path)]
    for path in declaring:
        finding = missing_model(path, texts[path])
        if finding:
            print(finding)

    print("=== Boilerplate duplication ===")
    for label, pattern in BOILERPLATE.items():
        print(f"{label}: {_count_matching(texts, pattern)} files")

    print("=== Bin/ extraction candidates ===")
    for label, pattern in EXTRACTION.items():
        print(f"{label}: {_count_matching(texts, pattern)} files")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scan-dir", default="plugins/")
    args = parser.parse_args()
    return scan(Path(args.scan_dir))


if __name__ == "__main__":
    sys.exit(main())
