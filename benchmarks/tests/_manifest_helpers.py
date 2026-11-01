"""Readers for the generated benchmark manifests and the artifacts they lock."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def load_manifest(path: Path) -> dict[str, Any]:
    """Read a UTF-8 JSON manifest and reject non-object roots.

    >>> from tempfile import TemporaryDirectory
    >>> with TemporaryDirectory() as directory:
    ...     path = Path(directory) / "manifest.json"
    ...     _ = path.write_text('{"schema": 1}', encoding="utf-8")
    ...     load_manifest(path)
    {'schema': 1}
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def sha256_file(path: Path) -> str:
    """Hash exact artifact bytes without newline or text normalization.

    >>> from tempfile import TemporaryDirectory
    >>> with TemporaryDirectory() as directory:
    ...     path = Path(directory) / "artifact"
    ...     _ = path.write_bytes(b"abc")
    ...     sha256_file(path)
    'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()
