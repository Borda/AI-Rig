"""Hashing, canonical JSON, timestamps and report paths shared by every mode."""

from __future__ import annotations
import hashlib
import json
import re
import time
from pathlib import Path
from .types import ConsumerTarget


_GIT_TIMEOUT_S = 5


_MAX_JSON_BYTES = 1_048_576


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _sha256_bytes(data: bytes) -> str:
    """Return the lowercase SHA-256 digest of exact *data* bytes."""
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str | None:
    """Return the sha256 of *path*, or ``None`` when it is absent (first-install case)."""
    if not path.is_file() or path.is_symlink():
        return None
    return _sha256_bytes(path.read_bytes())


def _canonical_json(obj: object) -> bytes:
    """Serialize *obj* deterministically for digest binding."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_plan_sha256(plan: dict) -> str:
    """Return the plan's binding SHA-256, computed over every field except the digest itself.

    Examples:
        >>> p = {"a": 1, "plan_sha256": "stale"}
        >>> compute_plan_sha256(p) == compute_plan_sha256({"a": 1})
        True
    """
    body = {k: v for k, v in plan.items() if k != "plan_sha256"}
    return _sha256_bytes(_canonical_json(body))


def _utc_now_iso() -> str:
    """Return the current UTC time in the second-precision ISO form used in artifacts."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _utc_stamp() -> str:
    """Return the current UTC time in the filesystem-safe artifact-directory form."""
    return time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime())


def _report_dir(root: Path) -> Path:
    """Return (and create) a fresh task-specific integration report directory.

    Never inside a plugin cache.
    """
    path = root / ".reports" / "integrate" / _utc_stamp()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_json_file(path: Path) -> dict | None:
    """Return the JSON object at *path*, or ``None`` if absent, symlinked, or unparsable."""
    if path.is_symlink() or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _manifest_for(target: ConsumerTarget, root: Path) -> dict | None:
    """Return *target*'s own plugin manifest (``.claude-plugin`` or ``.codex-plugin``)."""
    dirname = ".claude-plugin" if target.runtime == "claude" else ".codex-plugin"
    return _read_json_file(root / target.plugin_dir / dirname / "plugin.json")
