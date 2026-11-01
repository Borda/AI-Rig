"""Read and write the JSON, JSON-lines and digest fixtures that the review, remediate and loop suites share.

Every writer uses explicit UTF-8 and ``newline="\\n"`` so retained bytes and digests match on every host.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def read_json(path: Path) -> Any:
    """Parse one UTF-8 JSON file."""
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    """Write ``payload`` as compact JSON with portable newline bytes."""
    path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")


def read_jsonl(path: Path) -> list[Any]:
    """Parse every row of a UTF-8 JSON-lines file, such as retained rollout evidence."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def sha256_file(path: Path) -> str:
    """Return the hex SHA-256 digest of a file's exact bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()
