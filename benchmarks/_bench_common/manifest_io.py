"""Publish generated benchmark records without exposing partial bytes to concurrent readers.

Builders and pytest snapshot restoration share this boundary. Each file is replaced independently; this is not a
transaction across a manifest set. The temporary file lives beside its destination so replacement stays on one
filesystem, and its handle is closed before replacement for native Windows compatibility.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


def write_manifest_bytes(path: Path, payload: bytes) -> None:
    """Publish complete bytes, retaining existing permissions and leaving the old file intact on failure."""
    # Preserve write-through behavior for existing symlinks while replacing the actual destination atomically.
    path = path.resolve()
    try:
        if path.read_bytes() == payload:
            return
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = None
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
        if mode is not None:
            temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
