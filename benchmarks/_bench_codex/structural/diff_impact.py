"""Diff-impact stage admission, capture, and the Codex command builder."""

from __future__ import annotations

import hashlib
import json
import re
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from _bench_common.mutation_isolation import (
    verify_index_relocation,
)

from _bench_codex import runtime
from _bench_codex.structural import provenance
from _bench_codex.structural.config import _CODEX_BIN, PARITY_CODEX_REASONING_EFFORT, PARITY_MANIFEST_PATH


@dataclass(frozen=True)
class DiffImpactStageAdmission:
    """Exact, temporary Git state admitted for one staged diff-impact task."""

    repo_sha: str
    statuses: tuple[tuple[str, str], ...]
    file_sha256: tuple[tuple[str, str], ...]


def _diff_impact_stage_evidence(
    repo_path: Path, task: Mapping[str, Any], admission: DiffImpactStageAdmission
) -> dict[str, Any]:
    """Return the admitted and observed DI state retained with a contaminated cell."""
    stage = task.get("stage")
    if not isinstance(stage, list):
        raise ValueError("canonical Codex DI evidence requires a declared diff-impact stage")
    return {
        "stage": stage,
        "changed_paths": [relative_path for relative_path, _ in admission.file_sha256],
        "expected_status": dict(admission.statuses),
        "observed_status": _git_porcelain_status(repo_path),
    }


def _git_porcelain_status(repo_path: Path) -> dict[str, str]:
    """Return exact short Git statuses, rejecting malformed or rename records."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("canonical Codex run could not verify worktree cleanliness") from exc
    if proc.returncode != 0:
        raise ValueError("canonical Codex run could not verify worktree cleanliness")
    records = [record for record in proc.stdout.split("\0") if record]
    statuses: dict[str, str] = {}
    for record in records:
        if len(record) < 4 or record[2] != " ":
            raise ValueError("canonical Codex run received malformed Git worktree status")
        status, relative_path = record[:2], record[3:]
        if not relative_path or status[0] in "RC" or status[1] in "RC":
            raise ValueError("canonical Codex run rejects renamed or copied worktree paths")
        if relative_path in statuses:
            raise ValueError("canonical Codex run received duplicate Git worktree status")
        statuses[relative_path] = status
    return statuses


def _stage_relative_path(repo_path: Path, relative_path: str) -> Path:
    """Return one tracked regular stage file, rejecting escaping or linked paths."""
    relative = Path(relative_path)
    if not relative_path or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("canonical Codex DI stage contains an unsafe path")
    path = repo_path / relative
    current = repo_path
    for part in relative.parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except OSError as exc:
            raise ValueError("canonical Codex DI stage file is unavailable") from exc
        if stat.S_ISLNK(mode):
            raise ValueError("canonical Codex DI stage rejects symlink paths")
    path_stat = path.lstat()
    if not stat.S_ISREG(path_stat.st_mode) or path_stat.st_nlink != 1:
        raise ValueError("canonical Codex DI stage requires unlinked regular tracked files")
    try:
        tracked = subprocess.run(
            ["git", "-C", str(repo_path), "ls-files", "--error-unmatch", "--", relative_path],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("canonical Codex DI stage could not verify tracked files") from exc
    if tracked.returncode != 0:
        raise ValueError("canonical Codex DI stage requires tracked files")
    return path


def _capture_diff_impact_stage(repo_path: Path, task: Mapping[str, Any]) -> DiffImpactStageAdmission:
    """Capture the sole staged state that may temporarily replace clean-tree admission."""
    stage = task.get("stage")
    if task.get("type") != "diff_impact" or not isinstance(stage, list) or not stage:
        raise ValueError("canonical Codex DI admission requires a declared diff-impact stage")
    declared_paths: list[str] = []
    for edit in stage:
        if not isinstance(edit, Mapping) or not isinstance(edit.get("file"), str) or not edit["file"]:
            raise ValueError("canonical Codex DI stage contains an invalid file declaration")
        declared_paths.append(edit["file"])
    paths = tuple(dict.fromkeys(declared_paths))
    hashes = tuple(
        (relative_path, hashlib.sha256(_stage_relative_path(repo_path, relative_path).read_bytes()).hexdigest())
        for relative_path in paths
    )
    statuses = _git_porcelain_status(repo_path)
    expected_statuses = {relative_path: " M" for relative_path in paths}
    if statuses != expected_statuses:
        raise ValueError("canonical Codex DI stage does not match its exact staged worktree status")
    return DiffImpactStageAdmission(
        repo_sha=provenance._repo_sha(repo_path),
        statuses=tuple(expected_statuses.items()),
        file_sha256=hashes,
    )


def _validate_diff_impact_stage_admission(repo_path: Path, admission: DiffImpactStageAdmission) -> None:
    """Fail closed unless the current DI state still equals its captured staged bytes."""
    if provenance._repo_sha(repo_path) != admission.repo_sha:
        raise ValueError("canonical Codex DI stage changed the target commit")
    expected_statuses = dict(admission.statuses)
    if _git_porcelain_status(repo_path) != expected_statuses:
        raise ValueError("canonical Codex DI stage has unexpected worktree status")
    for relative_path, expected_hash in admission.file_sha256:
        observed_hash = hashlib.sha256(_stage_relative_path(repo_path, relative_path).read_bytes()).hexdigest()
        if observed_hash != expected_hash:
            raise ValueError("canonical Codex DI stage bytes changed after admission")


def _validate_locked_runtime(
    repo_path: Path,
    index_path: Path | None,
    arm: str,
    manifest_path: Path = PARITY_MANIFEST_PATH,
    diff_impact_stage: DiffImpactStageAdmission | None = None,
    index_relocation: Mapping[str, str] | None = None,
    historical_runtime_coordinate: Mapping[str, str] | None = None,
    fixture_runtime_coordinate: Mapping[str, Any] | None = None,
) -> None:
    """Fail closed unless the target repository and index match one explicit frozen coordinate."""
    if fixture_runtime_coordinate is not None:
        if diff_impact_stage is not None or historical_runtime_coordinate is not None or index_relocation is not None:
            raise ValueError("fixture runtime coordinate cannot combine with graph runtime admission")
        if index_path is None:
            raise ValueError("fixture runtime coordinate requires a frozen index")
        from _bench_codex.fixture_runtime import validate_fixture_runtime

        validate_fixture_runtime(repo_path, index_path, fixture_runtime_coordinate)
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_index = manifest["index"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("provider-parity manifest is unavailable or malformed") from exc
    if historical_runtime_coordinate is None:
        expected_repo = manifest["target_source"]["commit"]
        expected_index = manifest_index
    else:
        expected_repo = historical_runtime_coordinate.get("baseline_commit")
        raw_index_sha256 = historical_runtime_coordinate.get("raw_index_sha256")
        scan_version = historical_runtime_coordinate.get("scan_version")
        if (
            not isinstance(expected_repo, str)
            or not re.fullmatch(r"[0-9a-f]{40}", expected_repo)
            or not isinstance(raw_index_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", raw_index_sha256)
            or not isinstance(scan_version, str)
            or not scan_version.isdigit()
        ):
            raise ValueError("historical executable runtime coordinate is malformed")
        expected_index = {
            "raw_sha256": raw_index_sha256,
            "git_sha": expected_repo,
            "scan_version": int(scan_version),
        }
    if provenance._repo_sha(repo_path) != expected_repo:
        scope = "historical Patch" if historical_runtime_coordinate is not None else "canonical Codex"
        raise ValueError(f"{scope} run requires target commit {expected_repo}")
    if diff_impact_stage is None:
        if _git_porcelain_status(repo_path):
            raise ValueError("canonical Codex run requires a clean target worktree")
    else:
        _validate_diff_impact_stage_admission(repo_path, diff_impact_stage)
    if index_path is None or not index_path.is_file():
        raise ValueError("canonical Codex arm requires the locked index")
    if not index_path.is_relative_to(repo_path):
        raise ValueError("canonical Codemap index must be readable inside the target sandbox")
    expected_index_path = repo_path / ".cache" / "codemap" / f"{repo_path.name}.json"
    if index_path != expected_index_path:
        raise ValueError(f"canonical Codemap index must use the product resolver path {expected_index_path}")
    index_bytes = index_path.read_bytes()
    index_sha256 = hashlib.sha256(index_bytes).hexdigest()
    metadata = json.loads(index_bytes)
    if index_relocation is None:
        if index_sha256 != expected_index["raw_sha256"]:
            raise ValueError("canonical Codex run requires the locked index bytes")
    else:
        verify_index_relocation(
            index_relocation,
            metadata=metadata,
            index_sha256=index_sha256,
            repo_path=repo_path,
            frozen_index_sha256=expected_index["raw_sha256"],
        )
    if (
        metadata.get("git_sha") != expected_index["git_sha"]
        or metadata.get("scan_version") != expected_index["scan_version"]
    ):
        raise ValueError("canonical Codex index metadata does not match the locked manifest")


def build_codex_command(
    repo_path: Path | str,
    model: str,
    prompt: str,
    *,
    reasoning_effort: str = PARITY_CODEX_REASONING_EFFORT,
    codex_bin: str = _CODEX_BIN,
) -> list[str]:
    """Build an ephemeral, JSONL Codex command preserving *prompt* as-is.

    The isolated ``CODEX_HOME`` supplied by :class:`CodexRunner` prevents a user's global config from changing an arm's
    tool surface.
    """
    if not isinstance(prompt, str):
        raise TypeError("prompt must be a string")
    if not isinstance(model, str) or not model:
        raise ValueError("model must be a non-empty string")
    if model not in runtime.SUPPORTED_CODEX_MODELS:
        raise ValueError(f"supported Codex benchmark model required: {', '.join(runtime.SUPPORTED_CODEX_MODELS)}")
    if not isinstance(reasoning_effort, str) or not reasoning_effort:
        raise ValueError("reasoning_effort must be a non-empty string")
    path = str(Path(repo_path).resolve())
    return [
        codex_bin,
        "exec",
        "--json",
        "--ephemeral",
        "--config",
        f'model_reasoning_effort="{reasoning_effort}"',
        "--strict-config",
        "--cd",
        path,
        "--model",
        model,
        prompt,
    ]


#: Manifest-bound model/effort gate; defined in the shared Codex runtime.
_validate_codex_stratum = runtime.validate_codex_stratum
