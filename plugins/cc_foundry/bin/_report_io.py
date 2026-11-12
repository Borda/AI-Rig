"""Write complete reports atomically and protect their output directories from links.

Purpose:
    Share the filesystem boundary used by report-producing analyzers. Scope: UTF-8 report replacement and linked- parent
    checks only; growing ledgers retain append-only writes in their owner. Usage: Import write_report or prepare_parent
    from a sibling analyzer. Outputs: A complete report replaces its lexical destination through an exclusive sibling
    temporary file; its existing symlink target is never changed. Failure: Linked output directories and ordinary
    filesystem errors raise OSError before report replacement. Used by: cost_analyzer, timing_analyzer,
    efficiency_measured and size_importance.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def prepare_parent(path: Path) -> None:
    """Create an output directory only after refusing linked parents below trusted platform roots."""
    trusted = {Path.cwd(), Path(tempfile.gettempdir()).absolute()}
    current = path.absolute().parent
    while current not in trusted:
        if current.is_symlink():
            raise OSError(f"refusing symlinked report directory: {current}")
        parent = current.parent
        if parent == current:
            break
        current = parent
    path.parent.mkdir(parents=True, exist_ok=True)


def write_report(path: Path, text: str, *, errors: str = "strict") -> None:
    """Replace a report using an exclusive sibling file without following the destination symlink."""
    prepare_parent(path)
    fd, staging = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors=errors, newline="\n") as handle:
            handle.write(text)
        os.replace(staging, path)
    finally:
        Path(staging).unlink(missing_ok=True)
