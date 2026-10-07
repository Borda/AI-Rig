"""Run one provider-neutral paid benchmark stage with immutable artifacts.

This module deliberately owns the one generic task-by-arm lifecycle used by provider runners.  Provider-specific modules
retain transport, prompt parsing, and terminal formatting through callbacks; they do not own a competing loop.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar

Task = TypeVar("Task")
Arm = TypeVar("Arm")
#: Number of leading scope-hash characters that make up a paid-run approval token.
PAID_APPROVAL_PREFIX_LENGTH = 16
#: Matches a lowercase 64-character hexadecimal scope hash, the only form accepted for approval tokens.
_SCOPE_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def paid_approval_token(scope_sha256: str) -> str:
    """Return the canonical short token for one complete immutable scope hash.

    The token is a copy/paste guard, not a secret or cryptographic credential. Full SHA-256 remains stored in scope
    metadata; sixteen hexadecimal characters provide a 64-bit stale-scope discriminator for the single current scope.

    Raise ``ValueError`` unless the input contains exactly 64 lowercase hex digits.

    Examples:
        >>> paid_approval_token("0123456789abcdef" * 4)
        '0123456789abcdef'
    """
    if _SCOPE_SHA256_RE.fullmatch(scope_sha256) is None:
        raise ValueError("scope SHA-256 must contain 64 lowercase hexadecimal characters")
    return scope_sha256[:PAID_APPROVAL_PREFIX_LENGTH]


def paid_approval_matches(received: str | None, scope_sha256: str) -> bool:
    """Check a lowercase hash prefix against the validated current scope hash.

    Accept 16 through 64 hexadecimal characters. Missing, malformed, or
    mismatched tokens return ``False``; an invalid scope hash raises
    ``ValueError`` even when the received token is missing.

    Examples:
        >>> scope = "0123456789abcdef" * 4
        >>> paid_approval_matches(scope[:16], scope)
        True
        >>> paid_approval_matches(scope[:15], scope)
        False
        >>> paid_approval_matches(None, scope)
        False
    """
    paid_approval_token(scope_sha256)
    if received is None or re.fullmatch(r"[0-9a-f]{16,64}", received) is None:
        return False
    return scope_sha256.startswith(received)


@dataclass(frozen=True)
class PaidStageCallbacks(Generic[Task, Arm]):
    """Stage-specific work and presentation hooks for one paid lifecycle.

    ``prepare_run`` creates stage-local durable inputs after the exclusive run directory exists. ``emit_lifecycle``
    receives plain structured events that a stage may append to its run log and print. ``emit_row`` owns row formatting
    and may forward the rendered row to the shared terminal renderer.
    """

    run_cell: Callable[[Task, Arm], Mapping[str, Any]]
    validate_row: Callable[[Task, Arm, Mapping[str, Any]], None]
    prepare_run: Callable[[Path], None]
    persist_metadata: Callable[[Path, Mapping[str, Any]], None]
    emit_lifecycle: Callable[[str, Mapping[str, Any]], None]
    emit_row: Callable[[Mapping[str, Any], int, int, Arm], None]
    write_checksums: Callable[[Path], None]
    close_adapter: Callable[[], None]


def write_checksums(run_dir: Path) -> None:
    """Write SHA-256 digests for every retained artifact except the ledger itself."""
    ledger = run_dir / "checksums.sha256"
    files = [path for path in sorted(run_dir.rglob("*")) if path.is_file() and path != ledger]
    entries = "".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(run_dir).as_posix()}\n" for path in files
    )
    ledger.write_text(entries, encoding="utf-8")


def verify_checksums(run_dir: Path) -> None:
    """Raise when a retained artifact no longer matches the lifecycle ledger."""
    root = run_dir.resolve()
    ledger = root / "checksums.sha256"
    try:
        lines = ledger.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError("lifecycle checksum ledger is unavailable") from exc
    for line in lines:
        digest, separator, relative = line.partition("  ")
        if not separator or len(digest) != 64 or not relative:
            raise ValueError("lifecycle checksum ledger contains an invalid entry")
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("lifecycle checksum ledger contains an unsafe path")
        try:
            path = (root / candidate).resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"lifecycle checksum mismatch: {relative}") from exc
        if not path.is_relative_to(root):
            raise ValueError("lifecycle checksum ledger contains an unsafe path")
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError(f"lifecycle checksum mismatch: {relative}")


def run_paid_stage(
    *,
    tasks: Sequence[Task],
    arms: Sequence[Arm],
    run_dir: Path,
    metadata: Mapping[str, Any],
    callbacks: PaidStageCallbacks[Task, Arm],
) -> Path:
    """Run, persist, and finalize one exclusive paid stage in task-by-arm order.

    Every successful cell is written and flushed before its metadata progress record and presentation callback. Failures
    retain preceding cells, persist a final error status, refresh checksums, close the adapter, and then propagate the
    original exception.
    """
    run_dir = Path(run_dir)
    lifecycle_metadata = dict(metadata)
    total_cells = len(tasks) * len(arms)
    metadata_path = run_dir / "run-metadata.json"
    telemetry_path = run_dir / "telemetry.jsonl"
    directory_created = False
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
        directory_created = True
        lifecycle_metadata.update(status="running", persisted_cells=0)
        callbacks.persist_metadata(metadata_path, lifecycle_metadata)
        callbacks.prepare_run(run_dir)
        callbacks.emit_lifecycle(
            "artifacts",
            {"metadata_path": str(metadata_path), "telemetry_path": str(telemetry_path)},
        )
        with telemetry_path.open("x", encoding="utf-8") as telemetry:
            for task in tasks:
                for arm in arms:
                    row = callbacks.run_cell(task, arm)
                    callbacks.validate_row(task, arm, row)
                    telemetry.write(json.dumps(dict(row), sort_keys=True) + "\n")
                    telemetry.flush()
                    lifecycle_metadata["persisted_cells"] = int(lifecycle_metadata["persisted_cells"]) + 1
                    callbacks.persist_metadata(metadata_path, lifecycle_metadata)
                    callbacks.emit_row(row, int(lifecycle_metadata["persisted_cells"]), total_cells, arm)
        lifecycle_metadata["status"] = "completed"
        callbacks.persist_metadata(metadata_path, lifecycle_metadata)
        return run_dir
    except BaseException as exc:
        if directory_created:
            lifecycle_metadata["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
            lifecycle_metadata["error"] = {"type": type(exc).__name__, "message": str(exc)[:1000]}
            callbacks.persist_metadata(metadata_path, lifecycle_metadata)
        raise
    finally:
        if directory_created:
            callbacks.emit_lifecycle(
                "summary",
                {
                    "persisted_cells": int(lifecycle_metadata.get("persisted_cells", 0)),
                    "status": lifecycle_metadata.get("status", "failed"),
                    "total_cells": total_cells,
                },
            )
            callbacks.write_checksums(run_dir)
        callbacks.close_adapter()
