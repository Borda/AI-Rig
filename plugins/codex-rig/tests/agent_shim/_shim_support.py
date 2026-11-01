"""Fixtures shared by the agent-shim kernel tests: canonical JSON, root identities, private files, the role roster."""

from __future__ import annotations

import functools
import hashlib
import json
import stat
from pathlib import Path
from types import ModuleType

from _modules import load_module_registered

GENERATOR_PATH = Path(__file__).resolve().parents[2] / "scripts" / "generate_roles.py"


def canonical_json(value: object) -> bytes:
    """Encode the public canonical JSON representation of one fixture.

    Example:
        >>> canonical_json({"b": 2, "a": 1})
        b'{"a":1,"b":2}'
    """
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def root_identity(path: str) -> dict[str, object]:
    """Build one exact persisted root identity for a path that need not exist.

    Example:
        >>> root_identity("/fixture")["canonical_path"]
        '/fixture'
    """
    return {"canonical_path": path, "device": 1, "inode": 2, "owner": 3, "group": 4, "mode": "0700"}


def stat_root_identity(path: Path) -> dict[str, object]:
    """Return the persisted root identity fields of one existing fixture directory.

    Example:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as directory:
        ...     stat_root_identity(Path(directory))["canonical_path"] == directory
        True
    """
    metadata = path.stat()
    return {
        "canonical_path": str(path),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "owner": metadata.st_uid,
        "group": metadata.st_gid,
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
    }


def write_private(path: Path, payload: bytes) -> None:
    """Write one exact owner-only fixture file.

    Example:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as directory:
        ...     path = Path(directory) / "state.json"
        ...     write_private(path, b"state")
        ...     path.read_bytes()
        b'state'
    """
    path.write_bytes(payload)
    path.chmod(0o600)


def sha256_hex(payload: bytes) -> str:
    """Return the SHA-256 hex digest of one payload."""
    return hashlib.sha256(payload).hexdigest()


@functools.cache
def role_ids() -> tuple[str, ...]:
    """Read the exact generator-owned role roster."""
    return load_module_registered("codex_rig_generator_roster", GENERATOR_PATH).ROLE_IDS


def absent_roster(lifecycle: ModuleType) -> dict[str, object]:
    """Observe every roster target as absent through the lifecycle kernel under test."""
    return {f"codex-rig-{role_id}.toml": lifecycle.TargetObservation("absent") for role_id in role_ids()}
