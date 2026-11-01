"""Text and file helpers shared by the cc_oss tests.

The shipped skills are Markdown with fenced Bash blocks; their contract tests read the text, flatten its whitespace, and
cut a block out to run it. This module holds the one copy of each of those steps, plus the small fixture-file and
sentinel readers several test modules also repeated.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

#: Step 8 dispatch files of the resolve skill, in load order.
DISPATCH_PARTS = ("prelude", "phase1", "phase2", "phase3")


def flat(text: str) -> str:
    """Collapse every whitespace run to one space, so a phrase matches wherever the source wraps it."""
    return " ".join(text.split())


def write_lf(path: Path, text: str) -> None:
    """Write UTF-8 text with LF endings untouched, so byte assertions hold on every OS."""
    path.write_text(text, encoding="utf-8", newline="\n")


def bash_path(path: str | Path) -> str:
    """Return the fixture path in Git Bash syntax on native Windows, unchanged elsewhere."""
    if sys.platform != "win32":
        return str(path)
    return subprocess.check_output(["cygpath", "-u", str(path)], text=True).strip()


def bash_blocks(text: str) -> list[str]:
    """Return the body of every fenced ``bash`` block in ``text``, found line by line, in file order."""
    blocks: list[str] = []
    current: list[str] | None = None
    for line in text.splitlines(keepends=True):
        if current is None:
            if line.strip() == "```bash":
                current = []
        elif line.strip().startswith("```"):
            blocks.append("".join(current))
            current = None
        else:
            current.append(line)
    return blocks


def bash_block_after(text: str, heading: str) -> str:
    """Return the first Bash block after a unique heading or phrase."""
    start = text.index("```bash", text.index(heading)) + len("```bash\n")
    return text[start : text.index("```", start)]


def dispatch_text(resolve_dir: Path) -> str:
    """Join Step 8's dispatch files of the resolve skill in ``resolve_dir`` in load order."""
    return "\n".join((resolve_dir / f"modes/dispatch-{part}.md").read_text(encoding="utf-8") for part in DISPATCH_PARTS)


def read_shared_sentinel(root: Path, name: str) -> str:
    """Read one ``<name>-shared`` sentinel a script wrote under ``root``, without its trailing newline."""
    return (root / f"{name}-shared").read_text(encoding="utf-8").rstrip("\n")
