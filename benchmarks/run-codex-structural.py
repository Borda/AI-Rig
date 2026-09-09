#!/usr/bin/env python3
"""Run the task-driven Codex provider-parity benchmark.

Omitting ``--tasks`` executes the complete locked catalog: 55 structural
questions, 6 ReadCrop tasks, 4 Fix-Single tasks, 3 Fix-Multi tasks, and 5
historical Patch tasks, for 73 tasks and 219 A/B/C cells. Family selectors such
as ``RC,FS,FM,PT`` and mixed exact IDs route to native stage scorers. Codex agentic exploration remains in
``run-codex-agentic.py``.

The 55 structural tasks are the former `bench`/real-codebase category, not a third benchmark type beside the
task-driven structural and agentic runners.

## What this measures

The same locked structural task, target repository, prompt, evaluator, and
600-second retry-inclusive per-coordinate wall-clock budget are used for three within-Codex arms. The
experiment asks whether Codemap availability reduces model input and elapsed
time without lowering task quality:

  A_plain    — Codemap absent; the locked index is inaccessible
  B_auto     — Codemap's compact CLI query is available; using it is the model's choice
  C_strict  — Codemap's installed query skill is required

The task series are identical to ``run-claude-structural.py``:

  SE — symbol extraction          FN — function call graph
  RV — review assistance          CQ — code quality
  BR — development blast radius   DG — debug from trace
  FT — feature scaffolding        RI — real issue
  DI — diff impact                GR — graph reasoning
  MB — module blast radius

This module owns only Codex-native process handling, isolated homes, permission
profiles, JSONL event normalization, and per-cell persistence. Task identity,
arm contracts, and scoring remain in ``_bench_common/provider_parity_contracts.py`` and the
shared Claude structural evaluator registry.

## Arms

Every arm receives the canonical task prompt unchanged plus a separately
fingerprinted arm envelope:

  A_plain
    Uses ``provider-parity-plain``. It has no Codemap plugin or writable path,
    and cannot read the locked index or copied authentication file.

  B_auto
    Uses ``provider-parity-codemap``. The direct ``$CODEMAP_BIN`` launcher and
    locked index are available and the model decides whether to query, so a
    cell that never queries has still followed this arm's contract.

  C_strict
    Uses the same treatment profile as B with the installed Codemap skill. It
    must use ``$codemap-py:query-code`` and complete one compact query;
    compliance and correctness are recorded separately.

Both treatment profiles extend ``:read-only``, disable network, and inherit no
shell environment. B/C may write only the index-local ``.index-rw`` coordination
directory. The model command cannot read the disposable home's ``auth.json``.

## Metrics

Each task × repetition × arm cell records:

  Headline inputs:
    provider, repetition, elapsed_s, input_tokens, cached_input_tokens, output_tokens,
    fresh_input_tokens, reasoning_output_tokens, quality_score, and correct

  Diagnostics:
    command_calls, Codemap calls/successes/errors, fallback calls, required-arm
    Codemap-use compliance, exact locked-query conformance, endpoint/target/option
    fitness, treatment adherence, extraction failure, contamination, retry count,
    execution index, native item counts, raw Codex events, and provider error
    classification

The runner writes raw cells; it does not declare an advantage. The manifest
analysis compares paired log input-token ratios and quality deltas, then applies
failure, adoption, and compliance guardrails.

Structural, ReadCrop, Fix-Single, Fix-Multi, and Patch keep separate telemetry,
metadata, input snapshots, scorers, and checksum ledgers. The aggregate root
records lifecycle and scope only; unlike quality metrics are never pooled.

## What is NOT measured

  - Agentic exploration from ``tasks-agentic.json``
  - Index construction cost; the locked index is prepared before model timing
  - A general Codemap advantage from one smoke task
  - Cross-provider raw token equality or pooled Claude/Codex results
  - Pooling stage-specific answer and executable quality metrics

## Quick start

Run the intended command with ``--dry-run`` first. The no-model preflight
validates target, index, permission, direct-launcher, installed-Skill, and
isolation contracts, prints the deterministic plan, then emits one aggregate
``SCOPE`` and exact ``PAID_COMMAND``. Omit ``--tasks`` for all 73 tasks; use
``--tasks RC,FS,FM,PT`` for families or mixed exact IDs for a targeted run.

The emitted execution command has no Boolean paid flag. Absence of
``--dry-run`` means model execution and requires a private ``auth.json``, a
fresh ``--run-dir``, and the aggregate ``--paid-approval`` token.

Primary options:

  --repo-path            locked target repository
  --manifest-path        active immutable benchmark manifest
  --index-path           frozen Codemap index
  --marketplace-root     local plugin marketplace used by the Skill arm
  --codemap-bin          absolute direct launcher used by the direct arm
  --model                locked Codex model identifier
  --tasks                optional family, exact-ID, or mixed selector
  --dry-run              no-model admission, plan, scope, and paid command
  --auth-source          private auth source for model execution
  --run-dir              fresh aggregate artifact directory
  --paid-approval        16-character aggregate token emitted by the dry run

## Requirements

  - Python 3.10+ and the benchmark dependency group
  - an installed Codex CLI that satisfies the exercised command and permission probes;
    its observed version is provenance, not an admission requirement
  - A clean target at PyTorch Lightning tag ``2.6.5`` and its locked index
  - A direct Codemap launcher for B and the local plugin marketplace root for C
  - For authenticated execution, a user-owned regular ``auth.json`` with mode
    0600; symlinks and group/other-readable files are rejected

## Failure conditions

The run fails closed before a model call when the manifest, target, task,
prompt, index, plugin, provided authentication, or permission-profile contract
differs from the active manifest. It also rejects dirty targets, symlinked or hard-linked
protected paths, credential/index exposure to A, missing index access for B/C,
or a broad coordination write surface.

During execution, timeouts, non-zero Codex exits, malformed/incomplete native
events, extraction failures, and target/index/coordination mutations remain
visible in the result. Only zero-token retryable transport failures may retry,
at most twice, within the original cell's timeout. A required arm without a successful compact query is recorded as
``compliance=false`` rather than rewritten as an incorrect task answer.

## Output

``--dry-run`` invokes no model and prints deterministic probe and plan rows.
Execution creates the aggregate directory exclusively, persists normalized
cells inside native stage children, and prints compact fixed-order progress.
Token counts show gross/cached/fresh input in telemetry and gross input in the
terminal. Interactive A/B/C rows are colored; redirected logs remain plain.
Each completed cell survives a later failure.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import inspect
import itertools
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, NoReturn
from uuid import uuid4

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bench_codex import plugin_registration, runtime  # noqa: E402
from _bench_common.coordination_gate import (  # noqa: E402
    COORDINATION_NAME as _COORDINATION_NAME,
    assert_coordination_root_idle as _assert_coordination_root_idle,
    assert_safe_path_components as _assert_safe_path_components,
    cleanup_coordination_root as _cleanup_coordination_root,
    prepare_coordination_root,
    validate_coordination_root as _validate_coordination_root,
)
from _bench_common.mutation_isolation import (  # noqa: E402
    ExecutableAgentWorkspace,
    load_index_relocation,
    verify_index_relocation,
    create_executable_agent_workspace,
    patch_test_runtime_identity,
    relocate_frozen_index_for_worktree,
)
from _bench_common.paid_lifecycle import paid_approval_matches, write_checksums  # noqa: E402
from _bench_common.process_group import NEW_PROCESS_GROUP, terminate_process_group  # noqa: E402
from _bench_common.provider_parity_contracts import (  # noqa: E402
    ARM_CONTRACTS,
    EvaluationResult,
    PARITY_TIMEOUT_SECONDS,
    TaskPolicy,
    capability_strata,
    canonical_task_hash,
    canonical_result_rows,
    fresh_input_tokens,
    load_task_policies,
    load_task_suite,
    materialize_task_prompt,
    prompt_hash,
    semantic_suite_hash,
    token_accounting_inconsistent,
    treatment_adherence,
)


__all__ = (
    "ExecutableAgentWorkspace",
    "create_executable_agent_workspace",
    "relocate_frozen_index_for_worktree",
)


PARITY_MANIFEST_PATH = Path(__file__).parent / "manifests" / "codex-integration.json"
CODEX_STRUCTURAL_ARMS = ("A_plain", "B_auto", "C_strict")
_COUNTERBALANCED_ARM_ORDERS = tuple(itertools.permutations(CODEX_STRUCTURAL_ARMS))
ARMS = CODEX_STRUCTURAL_ARMS
PARITY_CODEX_MODEL = "gpt-5.6-luna"
PARITY_CODEX_REASONING_EFFORT = "high"
_CODEX_BIN = "codex"
_PROVENANCE_KEY = "_codex_provenance"
_NATIVE_ITEM_TELEMETRY_CONTRACT_ID = "installed-skill-binding-locked-query-components-v3"
_PLAIN_PERMISSION_PROFILE = "provider-parity-plain"
_CODEMAP_PERMISSION_PROFILE = "provider-parity-codemap"
_FROZEN_MARKETPLACE_NAME = "borda-ai-rig-frozen"
_AUTH_MAX_BYTES = 1024 * 1024
_BENCHMARK_EVIDENCE_ROOTS_ENV = "BENCHMARK_EVIDENCE_ROOTS"


def _print_result_block(rows: Iterable[tuple[str, str]], *, printed_cells: int, planned_cells: int) -> int:
    """Print one persisted task block in stable A/B/C display order."""
    arm_rank = {arm: index for index, arm in enumerate(CODEX_STRUCTURAL_ARMS)}
    for arm, row in sorted(rows, key=lambda item: arm_rank[item[0]]):
        printed_cells += 1
        runtime.print_arm_row(f"({printed_cells}/{planned_cells}) {row}", arm)
    return printed_cells


def _is_known_codex_arm(arm: str) -> bool:
    """Return whether an arm belongs to the current Codex experiment design."""
    return arm in CODEX_STRUCTURAL_ARMS


def deterministic_arm_order(
    experiment_revision: str,
    provider: str,
    model: str,
    task_id: str,
    repetition: int,
    *,
    reasoning_effort: str = "",
    task_ordinal: int | None = None,
) -> tuple[str, ...]:
    """Return a revision-bound, position-counterbalanced Codex arm ordering.

    The coordinate remains bound to its experiment/model/effort identity, while the locked task ordinal—not a per-task
    hash—selects one of six arm permutations. Across the 55-task suite, every treatment therefore occupies each ordinal
    18 or 19 times.
    """
    if repetition < 1:
        raise ValueError("repetition must be at least 1")
    coordinates = (
        experiment_revision,
        provider,
        model,
        reasoning_effort,
        task_id,
        str(repetition),
    )
    if any(not coordinate for coordinate in coordinates):
        raise ValueError("arm-order coordinates must be non-empty")
    ordinal = _locked_task_ordinal(task_id) if task_ordinal is None else task_ordinal
    if ordinal < 0:
        raise ValueError("task ordinal must be non-negative")
    phase_payload = "|".join(coordinates[:4]).encode("utf-8")
    phase = int.from_bytes(hashlib.sha256(phase_payload).digest()[:1], "big") % len(_COUNTERBALANCED_ARM_ORDERS)
    return _COUNTERBALANCED_ARM_ORDERS[(ordinal + repetition - 1 + phase) % len(_COUNTERBALANCED_ARM_ORDERS)]


def _locked_task_ordinal(task_id: str, manifest_path: Path = PARITY_MANIFEST_PATH) -> int:
    """Return ``task_id``'s unique ordinal from the manifest's locked execution order."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        task_ids = manifest["preregistered_cells"]["structural_execution_task_ids"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("locked structural task order is unavailable") from exc
    if not isinstance(task_ids, list) or not all(isinstance(item, str) and item for item in task_ids):
        raise ValueError("locked structural task order is malformed")
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("locked structural task order contains duplicate task IDs")
    try:
        return task_ids.index(task_id)
    except ValueError as exc:
        raise ValueError(f"task {task_id!r} is absent from the locked structural task order") from exc


def _arm_contract_hash(arm: str) -> str:
    """Return the active known hash or a stable local design hash before relock."""
    if arm == "A_plain" and arm in ARM_CONTRACTS:
        return ARM_CONTRACTS[arm]["contract_sha256"]
    return hashlib.sha256(_arm_envelope(arm).encode("utf-8")).hexdigest()


def _manifest_arm_order(
    experiment_revision: str,
    model: str,
    task_id: str,
    repetition: int,
    reasoning_effort: str,
    *,
    task_ordinal: int | None = None,
) -> tuple[str, ...]:
    """Read the manifest order only when it names the current Codex arm contract exactly."""
    arms = deterministic_arm_order(
        experiment_revision,
        "codex",
        model,
        task_id,
        repetition,
        reasoning_effort=reasoning_effort,
        task_ordinal=task_ordinal,
    )
    if set(arms) != set(CODEX_STRUCTURAL_ARMS) or len(arms) != len(CODEX_STRUCTURAL_ARMS):
        raise ValueError("manifest arm ordering does not match the current Codex A/B/C contract")
    return arms


def _raw_task(task: Mapping[str, Any]) -> dict[str, Any]:
    """Return the canonical task object without adapter-added provenance."""
    raw = dict(task)
    raw.pop(_PROVENANCE_KEY, None)
    return raw


def _raw_task_hash(task: Mapping[str, Any]) -> str:
    """Hash raw task bytes, never a provider projection."""
    return canonical_task_hash(_raw_task(task))


def _repo_sha(repo_path: Path) -> str:
    """Return repository HEAD or ``unknown`` when the fixture has no Git metadata."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else "unknown"


def _index_sha(index_path: Path | None) -> str:
    """Fingerprint an index file when one is configured."""
    if index_path is None or not index_path.is_file():
        return "unknown"
    try:
        return hashlib.sha256(index_path.read_bytes()).hexdigest()
    except OSError:
        return "unknown"


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
        repo_sha=_repo_sha(repo_path),
        statuses=tuple(expected_statuses.items()),
        file_sha256=hashes,
    )


def _validate_diff_impact_stage_admission(repo_path: Path, admission: DiffImpactStageAdmission) -> None:
    """Fail closed unless the current DI state still equals its captured staged bytes."""
    if _repo_sha(repo_path) != admission.repo_sha:
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
    if _repo_sha(repo_path) != expected_repo:
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


def _manifest_revision(manifest: Mapping[str, Any]) -> str:
    """Return a non-empty experiment identity from an active manifest."""
    revision = manifest.get("experiment_revision")
    if not isinstance(revision, str) or not revision:
        raise ValueError("provider-parity execution manifest has no experiment revision")
    return revision


def _read_manifest_revision(manifest_path: Path = PARITY_MANIFEST_PATH) -> str:
    """Read the active experiment identity for planning fixture or real tasks."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return _manifest_revision(manifest)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("provider-parity execution manifest is unavailable or malformed") from exc


def _task_selection_contract(manifest_path: Path) -> dict[str, Any]:
    """Read and validate the active manifest's unified task-selection contract."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        contract = manifest["task_selection"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("task selection contract is unavailable or malformed") from exc
    if not isinstance(contract, Mapping):
        raise ValueError("task selection contract is unavailable or malformed")
    execution_ids = contract.get("allowed_task_ids")
    allowed_families = contract.get("allowed_families")
    stage_order = contract.get("stage_order")
    stages = contract.get("stages")
    coordinate_timeout = contract.get("coordinate_timeout_seconds")
    if (
        not isinstance(execution_ids, list)
        or not execution_ids
        or not all(
            isinstance(task_id, str) and re.fullmatch(r"[A-Z]{2}-[0-9]{2}", task_id) for task_id in execution_ids
        )
        or len(execution_ids) != len(set(execution_ids))
        or not isinstance(allowed_families, list)
        or not all(isinstance(family, str) and re.fullmatch(r"[A-Z]{2}", family) for family in allowed_families)
        or len(allowed_families) != len(set(allowed_families))
        or allowed_families != list(dict.fromkeys(task_id.split("-", 1)[0] for task_id in execution_ids))
        or stage_order != ["structural", "readcrop", "fix-single", "fix-multi", "patch"]
        or not isinstance(stages, Mapping)
        or set(stages) != set(stage_order)
        or type(coordinate_timeout) is not int
        or coordinate_timeout < 1
    ):
        raise ValueError("task selection contract is unavailable or malformed")
    flattened: list[str] = []
    for stage_id in stage_order:
        stage = stages.get(stage_id)
        if not isinstance(stage, Mapping):
            raise ValueError("task selection contract is unavailable or malformed")
        task_ids = stage.get("allowed_task_ids")
        families = stage.get("allowed_families")
        arms = stage.get("arms")
        default_repetitions = stage.get("default_repetitions")
        selected_repetitions = stage.get("selected_repetitions")
        if (
            not isinstance(task_ids, list)
            or not task_ids
            or not all(isinstance(task_id, str) and re.fullmatch(r"[A-Z]{2}-[0-9]{2}", task_id) for task_id in task_ids)
            or not isinstance(families, list)
            or families != list(dict.fromkeys(task_id.split("-", 1)[0] for task_id in task_ids))
            or not isinstance(arms, list)
            or len(arms) != 3
            or len(arms) != len(set(arms))
            or type(default_repetitions) is not int
            or default_repetitions < 1
            or type(selected_repetitions) is not int
            or selected_repetitions < 1
        ):
            raise ValueError("task selection contract is unavailable or malformed")
        flattened.extend(task_ids)
    if flattened != execution_ids:
        raise ValueError("task selection contract is unavailable or malformed")
    return {
        "execution_task_ids": execution_ids,
        "allowed_families": allowed_families,
        "stage_order": stage_order,
        "stages": stages,
        "coordinate_timeout_seconds": coordinate_timeout,
    }


def _selector_tokens(value: str | Sequence[str]) -> list[str]:
    """Split comma-separated selectors while rejecting empty selector tokens."""
    values = [value] if isinstance(value, str) else list(value)
    if not values or not all(isinstance(item, str) for item in values):
        raise ValueError("at least one task selector is required")
    selectors: list[str] = []
    for raw_value in values:
        for token in raw_value.split(","):
            selector = token.strip().upper()
            if not selector:
                raise ValueError("task selectors cannot contain empty tokens")
            if selector not in selectors:
                selectors.append(selector)
    return selectors


def _targeted_scope_sha256(scope: Mapping[str, Any]) -> str:
    """Return the canonical identity of a resolved targeted benchmark scope."""
    payload = {
        key: scope[key]
        for key in (
            "manifest_sha256",
            "task_ids",
            "repetitions",
            "arms",
            "coordinate_timeout_seconds",
        )
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def resolve_task_selection(manifest_path: Path, selectors: str | Sequence[str] | None = None) -> dict[str, Any]:
    """Resolve one optional mixed selector into canonical stage partitions.

    Omitting selectors selects every supported task exactly once. Explicit selectors may mix family names and exact IDs;
    overlapping selections are deduplicated in the immutable global stage order.
    """
    manifest_path = Path(manifest_path)
    contract = _task_selection_contract(manifest_path)
    normalized = _selector_tokens(selectors) if selectors is not None else []
    task_ids = contract["execution_task_ids"]
    selected: set[str] = set(task_ids) if selectors is None else set()
    known_families = set(contract["allowed_families"])
    for selector in normalized:
        if selector in task_ids:
            selected.add(selector)
            continue
        if re.fullmatch(r"[A-Z]{2}", selector) and selector in known_families:
            selected.update(task_id for task_id in task_ids if task_id.startswith(f"{selector}-"))
            continue
        raise ValueError(f"unknown task selector {selector!r}")
    resolved_ids = [task_id for task_id in task_ids if task_id in selected]
    if not resolved_ids:
        raise ValueError("task selectors resolved to no executable tasks")
    selection_mode = "all" if selectors is None else "selected"
    stages: list[dict[str, Any]] = []
    for stage_id in contract["stage_order"]:
        stage_contract = contract["stages"][stage_id]
        stage_task_ids = [task_id for task_id in stage_contract["allowed_task_ids"] if task_id in selected]
        if not stage_task_ids:
            continue
        repetitions = (
            stage_contract[f"{selection_mode}_repetitions"]
            if selection_mode == "selected"
            else stage_contract["default_repetitions"]
        )
        arms = list(stage_contract["arms"])
        stages.append(
            {
                "stage_id": stage_id,
                "task_ids": stage_task_ids,
                "repetitions": repetitions,
                "arms": arms,
                "total_cells": len(stage_task_ids) * repetitions * len(arms),
            }
        )
    resolved = {
        "selectors": normalized,
        "task_ids": resolved_ids,
        "selection_mode": selection_mode,
        "stages": stages,
        "total_tasks": len(resolved_ids),
        "total_cells": sum(stage["total_cells"] for stage in stages),
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    }
    resolved["scope_sha256"] = hashlib.sha256(
        json.dumps(resolved, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return resolved


def _resolve_structural_task_selection(manifest_path: Path, selectors: str | Sequence[str]) -> dict[str, Any]:
    """Resolve a structural-only subset for the established structural loop."""
    unified = resolve_task_selection(manifest_path, selectors)
    nonstructural = [stage["stage_id"] for stage in unified["stages"] if stage["stage_id"] != "structural"]
    if nonstructural:
        raise ValueError("the structural loop accepts only structural task selectors")
    if not unified["stages"]:
        raise ValueError("task selectors resolved to no structural tasks")
    stage = unified["stages"][0]
    scope = {
        "selectors": unified["selectors"],
        "task_ids": stage["task_ids"],
        "study_mode": "targeted",
        "nonpoolable": True,
        "pooling_eligibility": "ineligible",
        "repetitions": stage["repetitions"],
        "arms": stage["arms"],
        "coordinate_timeout_seconds": _task_selection_contract(Path(manifest_path))["coordinate_timeout_seconds"],
        "manifest_sha256": unified["manifest_sha256"],
    }
    scope["scope_sha256"] = _targeted_scope_sha256(scope)
    return scope


def _validate_targeted_scope_request(
    scope: Mapping[str, Any],
    *,
    repetitions: int,
    arm: str,
    scope_sha256: str | None,
    dry_run: bool,
) -> None:
    """Require paid targeted invocations to match their reviewed scope exactly."""
    if repetitions != scope["repetitions"]:
        raise ValueError("targeted execution requires the scope repetition count")
    if arm != "all":
        raise ValueError("targeted execution requires --arm all")
    expected_sha = scope["scope_sha256"]
    if scope_sha256 is not None and scope_sha256 != expected_sha:
        raise ValueError("targeted execution scope SHA-256 does not match the resolved scope")
    if not dry_run and scope_sha256 is None:
        raise ValueError("paid targeted execution requires --scope-sha256 from --resolve-tasks")


def _validate_unscoped_paid_task_ids(
    manifest_path: Path,
    task_ids: list[str] | None,
    *,
    targeted: bool,
    dry_run: bool,
) -> None:
    """Allow unscoped paid task IDs only for the exact confirmatory sequence."""
    if dry_run or targeted or task_ids is None:
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        confirmatory_ids = manifest["preregistered_cells"]["structural_execution_task_ids"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("confirmatory task scope is unavailable or malformed") from exc
    if task_ids != confirmatory_ids:
        raise ValueError("paid task subsets require --tasks and its resolved scope SHA-256")


def _validate_execution_manifest(manifest_path: Path = PARITY_MANIFEST_PATH) -> None:
    """Require an active manifest locked to the exact runner implementation."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        revision = _manifest_revision(manifest)
        expected_runner_sha = manifest["implementation_contract"]["artifact_sha256"]["run_codex_structural"]
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("provider-parity execution manifest is unavailable or malformed") from exc
    actual_runner_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if expected_runner_sha != actual_runner_sha:
        raise ValueError(
            f"paid execution requires a manifest locked to this runner; "
            f"revision {revision!r} records {expected_runner_sha!r}, found {actual_runner_sha!r}"
        )


@dataclass
class ArmHome:
    """Disposable Codex home and environment for one canonical arm."""

    arm: str
    path: Path
    env: dict[str, str]
    codemap_available: bool
    codemap_verified: bool = False
    auth_provisioned: bool = False
    authenticated: bool = False
    permission_profile: str = ""
    coordination_path: Path | None = None
    codemap_launcher_path: Path | None = None
    codemap_launcher_sha256: str = ""
    codemap_plugin_path: Path | None = None
    codemap_plugin_manifest_sha256: str = ""
    codemap_skill_path: Path | None = None
    codemap_skill_sha256: str = ""
    codex_rig_path: Path | None = None
    codex_rig_manifest_sha256: str = ""
    codex_rig_adapter_path: Path | None = None
    codex_rig_adapter_sha256: str = ""
    codemap_context_path: Path | None = None
    codemap_context_sha256: str = ""
    denied_read_paths: tuple[Path, ...] = ()
    evidence_probe_paths: tuple[Path, ...] = ()
    host_plugin_names: tuple[str, ...] = ()

    def cleanup(self) -> None:
        """Remove the disposable home after a run."""
        _remove_private_directory(self.path, description="disposable Codex home")

    def __enter__(self) -> "ArmHome":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.cleanup()


@contextlib.contextmanager
def bind_executable_agent_workspace(adapter: "CodexRunner", workspace: ExecutableAgentWorkspace) -> Iterable[None]:
    """Bind one existing frozen adapter to a per-cell editable worktree temporarily."""
    original_repo_path, original_index_path = adapter.repo_path, adapter.index_path
    adapter.repo_path, adapter.index_path = workspace.worktree, workspace.index_path
    try:
        yield
    finally:
        adapter.repo_path, adapter.index_path = original_repo_path, original_index_path


@dataclass(frozen=True)
class _AuthFileIdentity:
    """Stable metadata required for one private credential file."""

    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


def _auth_identity(metadata: os.stat_result) -> _AuthFileIdentity:
    """Return the immutable metadata tuple used for credential stability checks."""
    return _AuthFileIdentity(
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _validate_auth_metadata(metadata: os.stat_result, *, description: str) -> None:
    """Reject credentials that cannot safely carry mutable OAuth state."""
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{description} must be a regular file")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise ValueError(f"{description} must be owned by the current user")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError(f"{description} permissions must be exactly 0600")
    if metadata.st_nlink != 1:
        raise ValueError(f"{description} must not be hard-linked")
    if not 0 < metadata.st_size <= _AUTH_MAX_BYTES:
        raise ValueError(f"{description} size is invalid")


def _read_auth_payload(path: Path, *, description: str) -> tuple[bytes, _AuthFileIdentity]:
    """Read one stable JSON-object credential through a no-follow descriptor."""
    path = Path(path)
    try:
        _assert_safe_path_components(path)
    except ValueError:
        raise ValueError(f"{description} path is unsafe") from None
    try:
        before = path.lstat()
    except OSError:
        raise ValueError(f"{description} is unavailable") from None
    if stat.S_ISLNK(before.st_mode):
        raise ValueError(f"{description} must not be a symlink")
    _validate_auth_metadata(before, description=description)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        _validate_auth_metadata(opened, description=description)
        if _auth_identity(opened) != _auth_identity(before):
            raise ValueError(f"{description} changed while being opened")
        chunks: list[bytes] = []
        remaining = _AUTH_MAX_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) != opened.st_size or len(payload) > _AUTH_MAX_BYTES:
            raise ValueError(f"{description} changed while being read")
        after_descriptor = os.fstat(descriptor)
    except OSError:
        raise ValueError(f"{description} could not be read securely") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        after = path.lstat()
    except OSError:
        raise ValueError(f"{description} changed while being read") from None
    identity = _auth_identity(before)
    if _auth_identity(after_descriptor) != identity or _auth_identity(after) != identity:
        raise ValueError(f"{description} changed while being read")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{description} must contain a JSON object") from exc
    if not isinstance(decoded, dict) or not decoded:
        raise ValueError(f"{description} must contain a non-empty JSON object")
    return payload, identity


def _fsync_directory(path: Path) -> None:
    """Durably publish a same-directory credential replacement when supported."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        os.fsync(descriptor)
    except OSError:
        # The payload has already been fsynced; directory fsync is unavailable
        # on some supported filesystems and must not erase valid state.
        return
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _apply_private_mode(path: Path, descriptor: int, mode: int) -> None:
    """Apply a private file mode where the host filesystem exposes POSIX modes."""
    if hasattr(os, "fchmod"):
        os.fchmod(descriptor, mode)
    elif os.name != "nt":
        os.chmod(path, mode)


def _atomic_write_auth_payload(destination: Path, payload: bytes, *, description: str) -> None:
    """Atomically replace one validated credential while retaining prior state on failure."""
    destination = Path(destination)
    parent = destination.parent
    try:
        _assert_safe_path_components(parent)
    except ValueError as exc:
        raise ValueError(f"{description} parent path is unsafe") from exc
    try:
        parent_metadata = parent.lstat()
    except OSError as exc:
        raise ValueError(f"{description} parent is unavailable") from exc
    if not stat.S_ISDIR(parent_metadata.st_mode) or stat.S_IMODE(parent_metadata.st_mode) != 0o700:
        raise ValueError(f"{description} parent must be a private directory")
    if hasattr(os, "getuid") and parent_metadata.st_uid != os.getuid():
        raise ValueError(f"{description} parent must be owned by the current user")
    if not 0 < len(payload) <= _AUTH_MAX_BYTES:
        raise ValueError(f"{description} payload size is invalid")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{description} payload must be a JSON object") from exc
    if not isinstance(decoded, dict) or not decoded:
        raise ValueError(f"{description} payload must be a non-empty JSON object")
    temporary = parent / f".{destination.name}.{uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        _apply_private_mode(temporary, descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("credential write returned no bytes")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, destination)
        _fsync_directory(parent)
    except OSError as exc:
        raise ValueError(f"{description} could not be updated securely") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    _read_auth_payload(destination, description=description)


def _remove_private_directory(path: Path, *, description: str, require_private: bool = True) -> None:
    """Remove a disposable private directory after validating its safe identity.

    POSIX callers retain exact owner and ``0700`` checks. Windows does not expose equivalent ACL privacy through
    ``stat`` mode bits, so cleanup keeps the symlink, directory-type, containment, and post-removal checks without
    treating its emulated mode as a POSIX security guarantee.
    """
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return
    try:
        _assert_safe_path_components(path.parent)
    except ValueError as exc:
        raise RuntimeError(f"{description} parent path is unsafe") from exc
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeError(f"{description} could not be inspected for cleanup") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError(f"{description} is not a private directory")
    if require_private and os.name != "nt":
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise RuntimeError(f"{description} is not owned by the current user")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise RuntimeError(f"{description} permissions must be exactly 0700")
    try:
        shutil.rmtree(path)
    except OSError as exc:
        raise RuntimeError(f"{description} could not be removed") from exc
    if path.exists() or path.is_symlink():
        raise RuntimeError(f"{description} remains after cleanup")


class _RunAuthState:
    """Private sequential OAuth state shared only between disposable cell homes."""

    def __init__(self, source: Path) -> None:
        self.source = Path(source)
        payload, self._source_identity = _read_auth_payload(self.source, description="auth source")
        root = Path(tempfile.gettempdir()).resolve(strict=True)
        _assert_safe_path_components(root)
        self.directory = Path(tempfile.mkdtemp(prefix="codex-benchmark-auth-", dir=root))
        self.directory.chmod(0o700)
        self.path = self.directory / "auth.json"
        try:
            _atomic_write_auth_payload(self.path, payload, description="run auth state")
        except BaseException:
            _remove_private_directory(self.directory, description="run auth state")
            raise
        self._closed = False

    def assert_source_unchanged(self) -> None:
        """Fail before a model call when the approved source metadata has drifted."""
        _payload, identity = _read_auth_payload(self.source, description="auth source")
        if identity != self._source_identity:
            raise ValueError("auth source metadata changed during benchmark run")

    def seed_home(self, home: Path) -> None:
        """Copy the current private credential state into one disposable home."""
        if self._closed:
            raise RuntimeError("run auth state is closed")
        payload, _identity = _read_auth_payload(self.path, description="run auth state")
        _atomic_write_auth_payload(Path(home) / "auth.json", payload, description="cell auth state")

    def refresh_from_home(self, home: Path) -> None:
        """Atomically retain a valid credential refresh produced by one cell."""
        if self._closed:
            raise RuntimeError("run auth state is closed")
        payload, _identity = _read_auth_payload(Path(home) / "auth.json", description="cell auth state")
        _atomic_write_auth_payload(self.path, payload, description="run auth state")

    def close(self) -> None:
        """Remove private run credential state exactly once."""
        if self._closed:
            return
        _remove_private_directory(self.directory, description="run auth state")
        self._closed = True


def _copy_auth_source(auth_source: Path, home: Path) -> None:
    """Copy one validated source credential into a disposable Codex home."""
    payload, _identity = _read_auth_payload(Path(auth_source), description="auth source")
    _atomic_write_auth_payload(Path(home) / "auth.json", payload, description="cell auth state")


def _canonical_index_path(index_path: Path) -> Path:
    """Return one regular, single-link index path with no symlink components."""
    absolute = Path(os.path.abspath(index_path))
    _assert_safe_path_components(absolute)
    try:
        metadata = absolute.lstat()
    except OSError as exc:
        raise ValueError("canonical Codemap index is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("canonical Codemap index must be a regular file")
    if metadata.st_nlink != 1:
        raise ValueError("canonical Codemap index must not be hard-linked")
    return absolute.resolve(strict=True)


def _prepare_coordination_root(index_path: Path, coordination_root: Path | None = None) -> Path:
    """Create a clean rwgate skeleton for the index, relocated when ``coordination_root`` names a directory."""
    return prepare_coordination_root(_canonical_index_path(index_path).parent, coordination_root)


# Declared stage surface: supported replacements for stage-module reach-ins into
# private names. Each delegates rather than aliases, so tests patching the private
# attribute are still observed through the public one.
def cleanup_coordination_root(coordination_root: Path) -> list[str]:
    """Discard an idle rwgate skeleton and report the non-skeleton entries removed with it."""
    return _cleanup_coordination_root(coordination_root)


def _shell_environment(home: ArmHome) -> dict[str, str]:
    """Return the explicit non-secret environment allowed in model commands."""
    allowed = {
        "PATH": home.env.get("PATH", os.defpath),
        "HOME": str(home.path),
        "CODEX_HOME": str(home.path),
    }
    for name in (
        "CODEMAP_BIN",
        "CODEMAP_COORDINATION_DIR",
        "CODEMAP_SKILL_FILE",
        "CODEMAP_PYTHON",
        "SCAN_NO_AUTOBUILD",
        "CODEMAP_LOGGING",
        "CODEX_CODEMAP_AVAILABLE",
    ):
        value = home.env.get(name)
        if value is not None:
            allowed[name] = value
    return allowed


def _untrusted_host_agent_roots(
    home: ArmHome,
    arm: str,
    marketplace_root: Path | None = None,
) -> tuple[Path, ...]:
    """Return host tooling roots that a measured model must not inspect."""
    if not _is_known_codex_arm(arm):
        raise ValueError(f"unknown benchmark arm {arm!r}")
    roots = [Path.home() / name for name in (".agents", ".claude", ".codex")]
    if marketplace_root is not None:
        roots.append(marketplace_root)

    home_root = home.path.resolve()
    denied: list[Path] = []
    for candidate in roots:
        root = candidate.expanduser().resolve()
        if home_root == root or home_root.is_relative_to(root):
            raise ValueError("disposable Codex home must be outside denied host tooling roots")
        if root not in denied:
            denied.append(root)
    return tuple(denied)


def _benchmark_evidence_roots(environment: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    """Return absolute evaluator roots that measured cells must not read."""
    raw_roots = (os.environ if environment is None else environment).get(_BENCHMARK_EVIDENCE_ROOTS_ENV)
    if raw_roots is None:
        return (Path(__file__).resolve().parent.parent,)
    try:
        serialized_roots = json.loads(raw_roots)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{_BENCHMARK_EVIDENCE_ROOTS_ENV} must be a JSON array") from exc
    if (
        not isinstance(serialized_roots, list)
        or not serialized_roots
        or not all(isinstance(root, str) and root for root in serialized_roots)
    ):
        raise ValueError(f"{_BENCHMARK_EVIDENCE_ROOTS_ENV} must contain non-empty path strings")

    roots: list[Path] = []
    for raw_root in serialized_roots:
        candidate = Path(raw_root)
        if not candidate.is_absolute():
            raise ValueError(f"{_BENCHMARK_EVIDENCE_ROOTS_ENV} paths must be absolute")
        try:
            root = candidate.resolve(strict=False)
        except OSError as exc:
            raise ValueError(f"benchmark evidence root is unavailable: {candidate}") from exc
        if root.exists() and not root.is_dir():
            raise ValueError(f"benchmark evidence root must be a directory: {root}")
        if root not in roots:
            roots.append(root)
    return tuple(roots)


def _evidence_probe_paths(evidence_roots: Iterable[Path]) -> tuple[Path, ...]:
    """Return oracle files."""
    candidates = (
        Path("benchmarks") / "run-codex-structural.py",
        Path("inputs") / "shared" / "run-codex-structural.py",
        Path("inputs") / "input-snapshot.json",
    )
    probes: list[Path] = []
    for root in evidence_roots:
        for relative_path in candidates:
            probe = root / relative_path
            if probe.is_file() and not probe.is_symlink():
                probes.append(probe)
                break
    return tuple(probes)


def _write_permission_config(
    home: ArmHome,
    arm: str,
    index_path: Path | None,
    *,
    marketplace_root: Path | None = None,
    writable_workspace: Path | None = None,
    denied_workspace: Path | None = None,
    evidence_roots: Iterable[Path] = (),
) -> Path:
    """Compose permissions ahead of any preserved Codex plugin registration."""
    if not _is_known_codex_arm(arm):
        raise ValueError(f"unknown benchmark arm {arm!r}")
    profile = _PLAIN_PERMISSION_PROFILE if arm == "A_plain" else _CODEMAP_PERMISSION_PROFILE
    auth_path = (home.path / "auth.json").resolve()
    filesystem_rules = [f'{json.dumps(str(auth_path))} = "deny"']
    denied_read_paths = list(_untrusted_host_agent_roots(home, arm, marketplace_root))
    if denied_workspace is not None:
        denied_workspace = denied_workspace.resolve()
        if denied_workspace not in denied_read_paths:
            denied_read_paths.append(denied_workspace)
    normalized_evidence_roots = tuple(Path(root).resolve(strict=False) for root in evidence_roots)
    for evidence_root in normalized_evidence_roots:
        if home.path.resolve().is_relative_to(evidence_root):
            raise ValueError("disposable Codex home must be outside benchmark evidence roots")
        if evidence_root not in denied_read_paths:
            denied_read_paths.append(evidence_root)
    filesystem_rules.extend(f'{json.dumps(str(path))} = "deny"' for path in denied_read_paths)
    if writable_workspace is not None:
        filesystem_rules.append(f'{json.dumps(str(writable_workspace.resolve()))} = "write"')
    if index_path is None:
        raise ValueError(f"{arm} permission profile requires the locked index")
    canonical_index = _canonical_index_path(index_path)
    coordination_root: Path | None = None
    if arm == "A_plain":
        filesystem_rules.append(f'{json.dumps(str(canonical_index.parent))} = "deny"')
    else:
        coordination_root = home.coordination_path or canonical_index.parent / _COORDINATION_NAME
        if coordination_root.is_symlink():
            raise ValueError("Codemap coordination root must not be a symlink")
        filesystem_rules.append(f'{json.dumps(str(coordination_root))} = "write"')

    explicit_environment = ", ".join(
        f"{name} = {json.dumps(value)}" for name, value in sorted(_shell_environment(home).items())
    )
    config_text = "\n".join(
        [
            f'default_permissions = "{profile}"',
            "",
            "[shell_environment_policy]",
            'inherit = "none"',
            "ignore_default_excludes = false",
            f"set = {{ {explicit_environment} }}",
            "",
            "[permissions]",
            "",
            f"[permissions.{profile}]",
            'description = "Read-only provider parity with isolated Codemap coordination."',
            'extends = ":read-only"',
            "",
            f"[permissions.{profile}.filesystem]",
            *filesystem_rules,
            "",
            f"[permissions.{profile}.network]",
            "enabled = false",
            "",
        ]
    )
    config_path = home.path / "config.toml"
    existing_config = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    managed_markers = (
        "default_permissions =",
        "[shell_environment_policy]",
        f"[permissions.{_PLAIN_PERMISSION_PROFILE}]",
        f"[permissions.{_CODEMAP_PERMISSION_PROFILE}]",
    )
    if any(marker in existing_config for marker in managed_markers):
        raise ValueError("disposable Codex home already contains benchmark permission configuration")
    if existing_config.strip():
        config_text = f"{config_text.rstrip()}\n\n{existing_config.lstrip()}"
    config_path.write_text(config_text, encoding="utf-8")
    config_path.chmod(0o600)
    home.permission_profile = profile
    home.coordination_path = coordination_root
    home.denied_read_paths = tuple(denied_read_paths)
    home.evidence_probe_paths = _evidence_probe_paths(normalized_evidence_roots)
    return config_path


def prepare_arm_home(
    arm: str,
    *,
    root: Path | None = None,
    auth_source: Path | None = None,
    codemap_bin: Path | None = None,
    plugin_installer: Callable[[Path], bool | None] | None = None,
) -> ArmHome:
    """Create an isolated ``CODEX_HOME`` implementing A/B/C availability."""
    if not _is_known_codex_arm(arm):
        raise ValueError(f"unknown benchmark arm {arm!r}")
    if root is None:
        try:
            temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
        except OSError as exc:
            raise ValueError("default temporary root is unavailable") from exc
    else:
        temp_root = Path(os.path.abspath(root))
        _assert_safe_path_components(temp_root)
    if not temp_root.is_dir():
        raise ValueError("temporary root must be a real directory")
    home = Path(tempfile.mkdtemp(prefix=f"codex-{arm}-", dir=str(temp_root)))
    try:
        home.chmod(0o700)
        config = home / "config.toml"
        config.touch(mode=0o600)
        config.chmod(0o600)
        if auth_source is not None:
            _copy_auth_source(auth_source, home)
        verified = False
        if arm == "B_auto":
            _validated_direct_codemap_launcher(codemap_bin)
            verified = True
        elif arm == "C_strict" and plugin_installer is not None:
            verified = bool(plugin_installer(home))
        env = os.environ.copy()
        # Batch admission values belong to the parent orchestrator, not the
        # Codex process or any measured arm environment.
        for variable in (
            "CODEX_PAID_APPROVAL",
            "CODEX_AUTH_SOURCE",
            "CODEX_RUN_DIR",
            _BENCHMARK_EVIDENCE_ROOTS_ENV,
        ):
            env.pop(variable, None)
        env.pop("CODEMAP_SKILL_FILE", None)
        env["CODEX_HOME"] = str(home)
        env["CODEX_BENCHMARK_ARM"] = arm
        env["CODEX_CODEMAP_AVAILABLE"] = "1" if verified else "0"
        arm_home = ArmHome(
            arm,
            home,
            env,
            verified,
            verified,
            auth_provisioned=auth_source is not None,
        )
        if arm == "B_auto":
            _configure_direct_codemap_launcher(arm_home, codemap_bin)
        return arm_home
    except BaseException:
        _remove_private_directory(home, description="disposable Codex home")
        raise


def probe_arm_home(home: ArmHome | Path, arm: str | None = None) -> dict[str, Any]:
    """Return deterministic isolation evidence, raising on cross-arm mismatch."""
    path = home.path if isinstance(home, ArmHome) else Path(home)
    expected = arm or (home.arm if isinstance(home, ArmHome) else None)
    config = path / "config.toml"
    available = home.codemap_available if isinstance(home, ArmHome) else False
    if expected == "A_plain" and available:
        raise ValueError("A_plain Codex home unexpectedly contains Codemap")
    if expected in {"B_auto", "C_strict"} and not (
        isinstance(home, ArmHome) and home.codemap_available and home.codemap_verified
    ):
        raise ValueError(f"{expected} Codex home requires verified Codemap delivery")
    if isinstance(home, ArmHome):
        skill_file = home.env.get("CODEMAP_SKILL_FILE")
        if expected == "C_strict":
            if home.codemap_skill_path is None or skill_file != str(home.codemap_skill_path.resolve()):
                raise ValueError("C_strict requires the exact installed Skill binding")
        elif skill_file is not None:
            raise ValueError(f"{expected} Codex home unexpectedly exposes CODEMAP_SKILL_FILE")
    return {
        "home": str(path),
        "config": str(config),
        "arm": expected,
        "codemap_available": available,
        "codemap_verified": isinstance(home, ArmHome) and home.codemap_verified,
        "auth_provisioned": isinstance(home, ArmHome) and home.auth_provisioned,
        "authenticated": isinstance(home, ArmHome) and home.authenticated,
        "permission_profile": home.permission_profile if isinstance(home, ArmHome) else "",
        "host_plugins": list(home.host_plugin_names) if isinstance(home, ArmHome) else [],
        "coordination_write_enabled": bool(isinstance(home, ArmHome) and home.coordination_path is not None),
        "codemap_python": (
            home.env.get("CODEMAP_PYTHON") if isinstance(home, ArmHome) and expected in {"B_auto", "C_strict"} else None
        ),
        "codemap_launcher_path": (
            str(home.codemap_launcher_path)
            if isinstance(home, ArmHome) and expected in {"B_auto", "C_strict"} and home.codemap_launcher_path
            else None
        ),
        "codemap_launcher_sha256": (
            home.codemap_launcher_sha256 if isinstance(home, ArmHome) and expected in {"B_auto", "C_strict"} else ""
        ),
        "codemap_context_path": (
            str(home.codemap_context_path)
            if isinstance(home, ArmHome) and expected == "C_strict" and home.codemap_context_path
            else None
        ),
        "codemap_context_sha256": (
            home.codemap_context_sha256 if isinstance(home, ArmHome) and expected == "C_strict" else ""
        ),
        "codemap_skill_path": (
            str(home.codemap_skill_path)
            if isinstance(home, ArmHome) and expected == "C_strict" and home.codemap_skill_path
            else None
        ),
        "codemap_skill_sha256": (
            home.codemap_skill_sha256 if isinstance(home, ArmHome) and expected == "C_strict" else ""
        ),
        "codemap_skill_file": (
            home.env.get("CODEMAP_SKILL_FILE") if isinstance(home, ArmHome) and expected == "C_strict" else None
        ),
        "codex_rig_path": (
            str(home.codex_rig_path)
            if isinstance(home, ArmHome) and expected == "C_strict" and home.codex_rig_path
            else None
        ),
        "codex_rig_manifest_sha256": (
            home.codex_rig_manifest_sha256 if isinstance(home, ArmHome) and expected == "C_strict" else ""
        ),
        "network_access": False,
        "config_mode": stat.S_IMODE(config.stat().st_mode),
    }


def _invoke_plugin_command(
    command: list[str],
    env: Mapping[str, str],
    command_runner: Callable[..., Any] | None = None,
    *,
    cwd: Path | None = None,
) -> tuple[int, str, str]:
    """Run a no-model Codex plugin command through an injectable seam."""
    runner = command_runner or subprocess.run
    kwargs: dict[str, Any] = {
        "env": dict(env),
        "capture_output": True,
        "text": True,
        "check": False,
    }
    if cwd is not None:
        kwargs["cwd"] = cwd
    try:
        completed = runner(command, **kwargs)
    except TypeError:
        completed = runner(command, dict(env))
    if isinstance(completed, tuple):
        code, stdout, stderr = (list(completed) + ["", ""])[:3]
        return int(code), str(stdout), str(stderr)
    return (
        int(getattr(completed, "returncode", 1)),
        str(getattr(completed, "stdout", "") or ""),
        str(getattr(completed, "stderr", "") or ""),
    )


def _verify_locked_codemap_python(
    *,
    manifest_path: Path = PARITY_MANIFEST_PATH,
    command_runner: Callable[..., Any] | None = None,
) -> str:
    """Resolve and validate a Python matching the manifest's treatment runtime."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        runtime = manifest["codex_permission_profiles"]["treatment_runtime"]
        required_major_minor = tuple(runtime["required_major_minor"])
        scope = runtime["scope"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("provider-parity treatment runtime is unavailable or malformed") from exc
    if required_major_minor != (3, 11) or scope != ["B_auto", "C_strict"]:
        raise ValueError("provider-parity treatment runtime contract does not match the active manifest")
    configured = runtime.get("environment", {}).get("CODEMAP_PYTHON")
    candidates = [
        configured,
        shutil.which("python3.11"),
        sys.executable,
        shutil.which("python3"),
        shutil.which("python"),
    ]
    checked: set[Path] = set()
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate:
            continue
        path = Path(candidate).resolve()
        if path in checked or not path.is_file() or not os.access(path, os.X_OK):
            continue
        checked.add(path)
        code, stdout, stderr = _invoke_plugin_command(
            [str(path), "--version"],
            {},
            command_runner=command_runner,
        )
        version_match = re.search(r"(\d+)\.(\d+)(?:\.\d+)?", f"{stdout}\n{stderr}")
        found_major_minor = tuple(int(part) for part in version_match.groups()) if version_match else ()
        if code == 0 and found_major_minor == required_major_minor:
            return str(path)
    required = ".".join(str(part) for part in required_major_minor)
    raise ValueError(f"Codemap treatment runtime requires an executable Python {required}")


def _verify_permission_profile(
    home: ArmHome,
    repo_path: Path,
    index_path: Path | None = None,
    command_runner: Callable[..., Any] | None = None,
    *,
    writable_workspace: Path | None = None,
) -> None:
    """Prove the selected profile denies secrets/source and permits only coordination."""
    sandbox_environment = _shell_environment(home)
    code, stdout, stderr = _invoke_plugin_command(
        [_CODEX_BIN, "--version"],
        sandbox_environment,
        command_runner=command_runner,
    )
    if code != 0 or not f"{stdout}\n{stderr}".strip():
        raise ValueError("Codex permission-profile version probe failed")

    profile = home.permission_profile or (
        _PLAIN_PERMISSION_PROFILE if home.arm == "A_plain" else _CODEMAP_PERMISSION_PROFILE
    )
    # An activated project virtualenv may expose a workspace symlink even when
    # the running interpreter itself lives outside the protected source tree.
    probe_python = str(Path(sys.executable).resolve())
    sandbox_command = [
        _CODEX_BIN,
        "sandbox",
        "-P",
        profile,
        "--include-managed-config",
        "-C",
        str(repo_path),
        "--",
    ]
    sandbox_prefix = [
        *sandbox_command,
        probe_python,
        "-c",
    ]
    code, _stdout, error = _invoke_plugin_command(
        [*sandbox_prefix, "pass"],
        sandbox_environment,
        command_runner=command_runner,
    )
    if code != 0:
        raise ValueError(f"Codex permission profile is unsupported or rejected: {error[:200]}")

    if home.arm != "A_plain" and home.codemap_available:
        codemap_bin = home.env.get("CODEMAP_BIN")
        codemap_python = home.env.get("CODEMAP_PYTHON")
        if not codemap_bin or not codemap_python:
            raise ValueError("Codemap permission profile lacks staged runtime paths")
        code, _stdout, error = _invoke_plugin_command(
            [*sandbox_command, codemap_bin, "--help"],
            sandbox_environment,
            command_runner=command_runner,
        )
        if code != 0:
            raise ValueError(f"Codex permission profile denied staged Codemap runtime: {error[:200]}")

    source_probe = repo_path / f".codex-parity-write-{uuid4().hex}"
    write_script = "from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b'probe')"
    code, _stdout, _stderr = _invoke_plugin_command(
        [*sandbox_prefix, write_script, str(source_probe)],
        sandbox_environment,
        command_runner=command_runner,
    )
    if writable_workspace is None:
        if code == 0 or source_probe.exists():
            source_probe.unlink(missing_ok=True)
            raise ValueError("Codex permission profile allowed a source-tree write")
    elif code != 0 or not source_probe.is_file():
        raise ValueError("Codex permission profile denied benchmark-workspace writes")
    source_probe.unlink(missing_ok=True)

    read_script = "from pathlib import Path; import sys; Path(sys.argv[1]).read_bytes()"
    auth_path = home.path / "auth.json"
    if auth_path.exists():
        code, probe_stdout, probe_stderr = _invoke_plugin_command(
            [*sandbox_prefix, read_script, str(auth_path)],
            sandbox_environment,
            command_runner=command_runner,
        )
        if code == 0:
            raise ValueError("Codex permission profile allowed credential reads")
        auth_bytes = auth_path.read_bytes()
        combined_output = (probe_stdout + probe_stderr).encode("utf-8", errors="replace")
        if auth_bytes and auth_bytes in combined_output:
            raise ValueError("Codex permission probe disclosed credential material")

    enumerate_script = "from pathlib import Path; import sys; next(Path(sys.argv[1]).iterdir(), None)"
    for denied_root in home.denied_read_paths:
        if not denied_root.exists():
            continue
        code, probe_stdout, _probe_stderr = _invoke_plugin_command(
            [*sandbox_prefix, enumerate_script, str(denied_root)],
            sandbox_environment,
            command_runner=command_runner,
        )
        if code == 0 or probe_stdout:
            raise ValueError("Codex permission profile allowed host tooling discovery")

    for evidence_probe in home.evidence_probe_paths:
        code, probe_stdout, _probe_stderr = _invoke_plugin_command(
            [*sandbox_prefix, read_script, str(evidence_probe)],
            sandbox_environment,
            command_runner=command_runner,
        )
        if code == 0 or probe_stdout:
            raise ValueError("Codex permission profile allowed benchmark evaluator evidence reads")

    if index_path is not None:
        code, _stdout, error = _invoke_plugin_command(
            [*sandbox_prefix, read_script, str(index_path)],
            sandbox_environment,
            command_runner=command_runner,
        )
        if home.arm == "A_plain" and code == 0:
            raise ValueError("A_plain permission profile allowed locked-index reads")
        if home.arm != "A_plain" and code != 0:
            raise ValueError(f"Codemap permission profile denied locked-index reads: {error[:200]}")

    if home.coordination_path is not None:
        coordination_probe = home.coordination_path / f".codex-parity-allow-{uuid4().hex}"
        code, _stdout, error = _invoke_plugin_command(
            [*sandbox_prefix, write_script, str(coordination_probe)],
            sandbox_environment,
            command_runner=command_runner,
        )
        if code != 0 or not coordination_probe.is_file():
            raise ValueError(f"Codex permission profile denied coordination writes: {error[:200]}")
        coordination_probe.unlink()
        _validate_coordination_root(home.coordination_path)


def _verify_authentication(
    home: ArmHome,
    command_runner: Callable[..., Any] | None = None,
) -> None:
    """Prove the disposable home is authenticated without retaining command output."""
    returncode, _stdout, _stderr = _invoke_plugin_command(
        ["codex", "login", "status"],
        home.env,
        command_runner,
    )
    if returncode != 0:
        raise RuntimeError("disposable Codex home is not authenticated")
    home.authenticated = True


_enabled_plugin_names = plugin_registration.enabled_plugin_names
_registered_plugin_tables = plugin_registration.registered_plugin_tables


def _plugin_enabled(plugin_json: str, plugin_name: str) -> bool:
    """Return whether one exact plugin appears enabled in ``codex plugin list --json``."""
    return plugin_name.lower() in _enabled_plugin_names(plugin_json)


def _plugin_listing_evidence(home: ArmHome, code: int, stdout: str, stderr: str) -> str:
    """Summarize one ``codex plugin list`` result for a fail-closed registration error."""
    return plugin_registration.plugin_listing_evidence(home.path / "config.toml", code, stdout, stderr)


def _verify_installed_plugin_pair(
    home: ArmHome,
    *,
    codex_bin: str = _CODEX_BIN,
    command_runner: Callable[..., Any] | None = None,
) -> None:
    """Require the reviewed C plugin pair among this home's own registrations."""
    code, stdout, stderr = _invoke_plugin_command(
        [codex_bin, "plugin", "list", "--json"],
        home.env,
        command_runner,
    )
    admitted, host_plugins = plugin_registration.treatment_admission(home.path / "config.toml", code, stdout)
    if not admitted:
        raise RuntimeError(
            f"final Codex plugin registration is invalid: {_plugin_listing_evidence(home, code, stdout, stderr)}"
        )
    home.host_plugin_names = host_plugins


def _configure_codemap_launcher(home: ArmHome, install_json: str) -> None:
    """Validate and expose the exact launcher reported by Codex plugin install."""
    try:
        payload = json.loads(install_json)
        raw_installed_path = payload["installedPath"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError("Codemap plugin install did not report installedPath") from exc
    if not isinstance(raw_installed_path, str) or not raw_installed_path:
        raise RuntimeError("Codemap plugin installedPath must be a non-empty string")

    installed_path = Path(os.path.abspath(raw_installed_path))
    _assert_safe_path_components(installed_path)
    try:
        installed_path = installed_path.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("Codemap plugin installedPath is unavailable") from exc
    home_root = home.path.resolve(strict=True)
    if not installed_path.is_relative_to(home_root):
        raise RuntimeError("Codemap plugin installedPath escaped the disposable CODEX_HOME")

    plugin_manifest = installed_path / ".codex-plugin" / "plugin.json"
    launcher = installed_path / "bin" / "codemap-py"
    query_skill = installed_path / "codex-skills" / "query-code" / "SKILL.md"
    _assert_safe_path_components(plugin_manifest)
    _assert_safe_path_components(launcher)
    _assert_safe_path_components(query_skill)
    try:
        manifest_metadata = plugin_manifest.lstat()
        launcher_metadata = launcher.lstat()
        skill_metadata = query_skill.lstat()
        manifest_payload = json.loads(plugin_manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Codemap plugin launcher or manifest is unavailable") from exc
    if (
        not stat.S_ISREG(manifest_metadata.st_mode)
        or plugin_manifest.is_symlink()
        or manifest_metadata.st_nlink != 1
        or manifest_payload.get("name") != "codemap-py"
    ):
        raise RuntimeError("Codemap plugin manifest identity is invalid")
    if (
        not stat.S_ISREG(launcher_metadata.st_mode)
        or launcher.is_symlink()
        or launcher_metadata.st_nlink != 1
        or not os.access(launcher, os.X_OK)
    ):
        raise RuntimeError("Codemap plugin launcher must be a regular executable")
    if not stat.S_ISREG(skill_metadata.st_mode) or query_skill.is_symlink() or skill_metadata.st_nlink != 1:
        raise RuntimeError("Codemap query skill must be a regular file")

    resolved_launcher = launcher.resolve(strict=True)
    if not resolved_launcher.is_relative_to(installed_path):
        raise RuntimeError("Codemap plugin launcher escaped installedPath")
    home.env["CODEMAP_BIN"] = str(resolved_launcher)
    home.codemap_plugin_path = installed_path
    home.codemap_plugin_manifest_sha256 = hashlib.sha256(plugin_manifest.read_bytes()).hexdigest()
    home.codemap_launcher_path = resolved_launcher
    home.codemap_launcher_sha256 = hashlib.sha256(resolved_launcher.read_bytes()).hexdigest()
    home.codemap_skill_path = query_skill.resolve(strict=True)
    home.codemap_skill_sha256 = hashlib.sha256(home.codemap_skill_path.read_bytes()).hexdigest()
    home.env["CODEMAP_SKILL_FILE"] = str(home.codemap_skill_path)


def _configure_codex_rig_plugin(home: ArmHome, install_json: str) -> None:
    """Lock the exact Codex Rig plugin installed for the skill-required arm."""
    try:
        payload = json.loads(install_json)
        raw_installed_path = payload["installedPath"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError("Codex Rig plugin install did not report installedPath") from exc
    if not isinstance(raw_installed_path, str) or not raw_installed_path:
        raise RuntimeError("Codex Rig plugin installedPath must be a non-empty string")

    installed_path = Path(os.path.abspath(raw_installed_path))
    _assert_safe_path_components(installed_path)
    try:
        installed_path = installed_path.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("Codex Rig plugin installedPath is unavailable") from exc
    home_root = home.path.resolve(strict=True)
    if not installed_path.is_relative_to(home_root):
        raise RuntimeError("Codex Rig plugin installedPath escaped the disposable CODEX_HOME")

    plugin_manifest = installed_path / ".codex-plugin" / "plugin.json"
    adapter = installed_path / "shared" / "codemap_adapter.py"
    _assert_safe_path_components(plugin_manifest)
    _assert_safe_path_components(adapter)
    try:
        manifest_metadata = plugin_manifest.lstat()
        adapter_metadata = adapter.lstat()
        manifest_payload = json.loads(plugin_manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Codex Rig plugin manifest is unavailable") from exc
    if (
        not stat.S_ISREG(manifest_metadata.st_mode)
        or plugin_manifest.is_symlink()
        or manifest_metadata.st_nlink != 1
        or manifest_payload.get("name") != "codex-rig"
    ):
        raise RuntimeError("Codex Rig plugin manifest identity is invalid")
    if not stat.S_ISREG(adapter_metadata.st_mode) or adapter.is_symlink() or adapter_metadata.st_nlink != 1:
        raise RuntimeError("Codex Rig adapter must be a regular file")
    home.codex_rig_path = installed_path
    home.codex_rig_manifest_sha256 = hashlib.sha256(plugin_manifest.read_bytes()).hexdigest()
    home.codex_rig_adapter_path = adapter.resolve(strict=True)
    home.codex_rig_adapter_sha256 = hashlib.sha256(home.codex_rig_adapter_path.read_bytes()).hexdigest()


class TreatmentArtifactLockError(ValueError):
    """Report stale local treatment bytes without obscuring the safe recovery."""


def _verify_treatment_artifact_locks(home: ArmHome, manifest_path: Path) -> None:
    """Require installed treatment files and versions to match the reviewed manifest locks."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        hashes = manifest["artifact_sha256"]
        codemap_version = str(manifest["codemap_candidate"]["version"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Codex treatment manifest is missing artifact locks") from exc
    expected_launcher = hashes.get("codemap_runtime_cli") if isinstance(hashes, Mapping) else None
    if not isinstance(expected_launcher, str) or home.codemap_launcher_sha256 != expected_launcher:
        raise ValueError("Codemap launcher does not match the locked runtime artifact")
    if home.arm == "B_auto":
        try:
            runtime_lock = manifest["direct_cli_runtime"]
            expected_files = runtime_lock["files"]
            expected_aggregate = runtime_lock["aggregate_sha256"]
            staged_root = home.codemap_launcher_path.parent.parent
        except (AttributeError, KeyError, TypeError) as exc:
            raise ValueError("direct CLI runtime closure lock is missing") from exc
        observed_files = _runtime_file_hashes(staged_root)
        if not isinstance(expected_files, Mapping) or observed_files != dict(expected_files):
            raise ValueError("staged direct CLI runtime does not match the locked file closure")
        if not isinstance(expected_aggregate, str) or _aggregate_file_hashes(observed_files) != expected_aggregate:
            raise ValueError("staged direct CLI runtime aggregate does not match the manifest")
        return
    if home.codemap_skill_path is None or home.env.get("CODEMAP_SKILL_FILE") != str(home.codemap_skill_path.resolve()):
        raise ValueError("installed Codemap Skill binding does not match the locked path")
    try:
        codex_rig_version = str(manifest["codex_rig_candidate"]["version"])
        expected = {
            "codemap_candidate_manifest": home.codemap_plugin_manifest_sha256,
            "codemap_query_skill": home.codemap_skill_sha256,
            "codex_rig_plugin_manifest": home.codex_rig_manifest_sha256,
            "codex_rig_adapter": home.codex_rig_adapter_sha256,
        }
        codemap_manifest = json.loads(
            (home.codemap_plugin_path / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        rig_manifest = json.loads((home.codex_rig_path / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    except (AttributeError, OSError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("installed Codemap/Codex Rig artifact identity is incomplete") from exc
    observed_versions = {
        "codemap-py": str(codemap_manifest.get("version")),
        "codex-rig": str(rig_manifest.get("version")),
    }
    expected_versions = {"codemap-py": codemap_version, "codex-rig": codex_rig_version}
    version_drift = {
        name: (expected_versions[name], observed_versions[name])
        for name in expected_versions
        if expected_versions[name] != observed_versions[name]
    }
    if version_drift:
        raise TreatmentArtifactLockError(_treatment_artifact_version_mismatch_message(version_drift))
    for artifact_name, observed_sha256 in expected.items():
        expected_sha256 = hashes.get(artifact_name) if isinstance(hashes, Mapping) else None
        if not isinstance(expected_sha256, str) or observed_sha256 != expected_sha256:
            raise TreatmentArtifactLockError(_treatment_artifact_lock_mismatch_message(artifact_name))


def _treatment_artifact_lock_mismatch_message(artifact_name: str) -> str:
    """Explain how to refresh a stale local treatment lock without weakening provenance."""
    return (
        f"installed treatment artifact does not match lock: {artifact_name}. "
        "The local treatment bytes changed after `benchmarks/manifests/codex-integration.json` was generated; "
        "no paid model call was started. Refresh the lock with "
        "`uv run python benchmarks/build-codex-integration-manifest.py`, then resolve a new scope for the same study, "
        "repository, model, and task IDs. Do not reuse the previous --paid-approval value. "
        "If Codex Rig edits are still in progress, regenerate only after the intended local bytes are ready."
    )


def _treatment_artifact_version_mismatch_message(version_drift: Mapping[str, tuple[str, str]]) -> str:
    """Explain how to relock reviewed local plugin versions before a paid retry."""
    observed = ", ".join(
        f"{name}: manifest={expected}, installed={installed}"
        for name, (expected, installed) in sorted(version_drift.items())
    )
    return (
        f"installed treatment version differs from the active manifest ({observed}). "
        "No paid model call was started. The local plugin changed after the treatment manifest was generated. "
        "When the intended local plugin bytes are ready, run "
        "`uv run python benchmarks/build-codex-integration-manifest.py`, then resolve a new scope for the same study, "
        "repository, model, and task IDs. Do not reuse the previous --paid-approval value."
    )


def _admit_installed_skill_pair(
    home: ArmHome,
    repo_path: Path,
    index_path: Path,
    *,
    manifest_path: Path,
    command_runner: Callable[..., Any] | None = None,
) -> None:
    """Run the installed Codex Rig adapter once and persist verified C admission context."""
    if home.arm != "C_strict" or home.codex_rig_adapter_path is None or home.codemap_plugin_path is None:
        raise ValueError("installed-skill admission requires a locked C skill home")
    if home.codemap_launcher_path is None or not home.env.get("CODEMAP_PYTHON"):
        raise ValueError("installed-skill admission requires locked Codemap runtime paths")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        admission = manifest["codex_rig_integration_admission"]
        category = admission["probe_category"]
        target = admission["probe_target"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Codex treatment manifest is missing installed-skill admission controls") from exc
    if category != "analysis" or not isinstance(target, str) or not target:
        raise ValueError("Codex treatment manifest has invalid installed-skill admission controls")
    root = repo_path.resolve(strict=True)
    locked_index = _canonical_index_path(index_path)
    context_path = home.path.resolve(strict=True) / "codemap-context.json"
    command = [
        home.env["CODEMAP_PYTHON"],
        str(home.codex_rig_adapter_path),
        "context",
        "--category",
        category,
        "--target",
        target,
        "--root",
        str(root),
        "--out",
        str(context_path),
    ]
    code, _stdout, stderr = _invoke_plugin_command(
        command,
        _shell_environment(home),
        command_runner,
        cwd=root,
    )
    if code != 0:
        raise RuntimeError(f"installed Codex Rig context admission failed: {stderr[:300]}")
    _assert_safe_path_components(context_path)
    try:
        metadata = context_path.lstat()
        payload = json.loads(context_path.read_text(encoding="utf-8"))
        probe = payload["probe"]
        doctor = probe["doctor"]
        queries = payload["queries"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("installed Codex Rig context admission produced no valid context") from exc
    if not stat.S_ISREG(metadata.st_mode) or context_path.is_symlink() or metadata.st_nlink != 1:
        raise RuntimeError("installed Codex Rig context artifact must be a regular file")
    query_evidence_valid = (
        isinstance(queries, list)
        and bool(queries)
        and all(
            isinstance(query, Mapping)
            and query.get("exit_code") == 0
            and query.get("error") is None
            and query.get("query_complete") is True
            for query in queries
        )
    )
    checks = {
        "protocol": payload.get("protocol_version") == "codemap-py.integration.v1",
        "target": payload.get("target") == target,
        "context_status": payload.get("status") in {"available", "degraded"},
        "probe_status": probe.get("status") == "available",
        "launcher": probe.get("launcher") == str(home.codemap_launcher_path),
        "plugin_root": doctor.get("plugin_root") == str(home.codemap_plugin_path),
        "index_path": doctor.get("index_path") == str(locked_index),
        "queries": query_evidence_valid,
    }
    failed_checks = [name for name, passed in checks.items() if not passed]
    if failed_checks:
        raise RuntimeError("installed Codex Rig context admission failed checks: " + ", ".join(failed_checks))
    home.codemap_context_path = context_path.resolve(strict=True)
    home.codemap_context_sha256 = hashlib.sha256(home.codemap_context_path.read_bytes()).hexdigest()


def _admit_staged_direct_cli(
    home: ArmHome,
    repo_path: Path,
    index_path: Path,
    *,
    manifest_path: Path,
    command_runner: Callable[..., Any] | None = None,
) -> None:
    """Execute one task-shaped compact query through B's staged CLI runtime."""
    if home.arm != "B_auto" or home.codemap_launcher_path is None:
        raise ValueError("direct CLI admission requires a locked B runtime")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        admission = manifest["direct_cli_admission"]
        subcommand = admission["probe_subcommand"]
        target = admission["probe_target"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("direct CLI admission contract is missing") from exc
    if subcommand != "fn-rdeps" or not isinstance(target, str) or "::" not in target:
        raise ValueError("direct CLI admission query is not task-shaped")

    index_sha256 = hashlib.sha256(index_path.read_bytes()).hexdigest()
    profile = home.permission_profile or _CODEMAP_PERMISSION_PROFILE
    command = [
        _CODEX_BIN,
        "sandbox",
        "-P",
        profile,
        "--include-managed-config",
        "-C",
        str(repo_path),
        "--",
        str(home.codemap_launcher_path),
        "query",
        "--compact",
        subcommand,
        target,
    ]
    code, stdout, stderr = _invoke_plugin_command(
        command,
        _shell_environment(home),
        command_runner=command_runner,
    )
    output_item = {"aggregated_output": stdout}
    if code != 0 or not runtime._query_output_complete(output_item):
        detail = stderr.strip() or stdout.strip()
        raise RuntimeError(f"staged direct CLI admission query failed: {detail[:300]}")
    if hashlib.sha256(index_path.read_bytes()).hexdigest() != index_sha256:
        raise RuntimeError("staged direct CLI admission mutated the locked index")


def _validated_direct_codemap_launcher(codemap_bin: Path | None) -> Path:
    """Return a directly supplied regular Codemap launcher without plugin discovery."""
    if codemap_bin is None:
        raise ValueError("B_auto requires --codemap-bin")
    launcher = Path(codemap_bin)
    if not launcher.is_absolute():
        raise ValueError("--codemap-bin must be an absolute path")
    _assert_safe_path_components(launcher)
    try:
        metadata = launcher.lstat()
        resolved_launcher = launcher.resolve(strict=True)
    except OSError as exc:
        raise ValueError("--codemap-bin is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or launcher.is_symlink()
        or metadata.st_nlink != 1
        or not os.access(resolved_launcher, os.X_OK)
    ):
        raise ValueError("--codemap-bin must be a regular executable")
    return resolved_launcher


def _direct_runtime_files(source_root: Path) -> dict[str, Path]:
    """Return the exact source files required by the isolated direct CLI."""
    relative_paths = [
        Path("bin/codemap-py"),
        Path("bin/_exclusions.py"),
        Path("scripts/codemap_py_entry.py"),
        *sorted(path.relative_to(source_root) for path in (source_root / "src" / "codemap_py").rglob("*.py")),
    ]
    files: dict[str, Path] = {}
    resolved_root = source_root.resolve(strict=True)
    for relative_path in relative_paths:
        path = source_root / relative_path
        try:
            metadata = path.lstat()
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ValueError("--codemap-bin runtime bundle is incomplete") from exc
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or not resolved.is_relative_to(resolved_root):
            raise ValueError("--codemap-bin runtime bundle contains an unsafe path")
        files[relative_path.as_posix()] = resolved
    return files


def _runtime_file_hashes(runtime_root: Path) -> dict[str, str]:
    """Hash the exact files present in a staged direct CLI runtime."""
    return {
        path.relative_to(runtime_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(runtime_root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


def _aggregate_file_hashes(hashes: Mapping[str, str]) -> str:
    """Return a stable aggregate identity for a relative-path hash mapping."""
    payload = "".join(f"{path}\0{sha256}\n" for path, sha256 in sorted(hashes.items()))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _archive_snapshot_file(
    source: Path,
    destination: Path,
    *,
    role: str,
    archive_root: Path,
    source_root: Path | None = None,
    entries: list[dict[str, Any]],
) -> None:
    """Copy one verified non-secret input and append its deterministic identity."""
    _assert_safe_path_components(source)
    metadata = source.lstat()
    resolved = source.resolve(strict=True)
    if source.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValueError(f"snapshot source must be a regular single-link file: {source}")
    if source_root is not None and not resolved.is_relative_to(source_root.resolve(strict=True)):
        raise ValueError(f"snapshot source escaped its locked root: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_fd: int | None = None
    destination_fd: int | None = None
    try:
        source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(source_fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            raise ValueError(f"snapshot source changed while being opened: {source}")
        destination_mode = 0o700 if stat.S_IMODE(opened.st_mode) & 0o111 else 0o600
        destination_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, destination_mode)
        _apply_private_mode(destination, destination_fd, destination_mode)
        with os.fdopen(source_fd, "rb") as source_handle:
            source_fd = None
            with os.fdopen(destination_fd, "wb") as destination_handle:
                destination_fd = None
                shutil.copyfileobj(source_handle, destination_handle)
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise ValueError(f"snapshot source could not be copied securely: {source}") from exc
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if destination_fd is not None:
            os.close(destination_fd)
    payload = destination.read_bytes()
    entries.append(
        {
            "role": role,
            "archived_path": destination.relative_to(archive_root).as_posix(),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
            # Windows does not expose the POSIX mode bits used by the ledger; retain
            # the logical mode selected from the verified source instead.
            "mode": destination_mode,
        }
    )


def _archive_snapshot_tree(
    source_root: Path,
    destination_root: Path,
    *,
    role: str,
    entries: list[dict[str, Any]],
) -> None:
    """Archive runtime files while excluding private evaluator, cache, plan, and test trees."""
    root = source_root.resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"snapshot package root must be a real directory: {source_root}")
    excluded_parts = {".cache", ".git", ".plans", ".reports", "__pycache__", "test", "tests"}
    for source in sorted(root.rglob("*")):
        if (
            not source.is_file()
            or source.is_symlink()
            or source.suffix == ".pyc"
            or excluded_parts.intersection(source.relative_to(root).parts)
        ):
            continue
        relative = source.relative_to(root)
        _archive_snapshot_file(
            source,
            destination_root / relative,
            role=role,
            archive_root=destination_root.parent.parent,
            source_root=root,
            entries=entries,
        )


def _write_frozen_marketplace(snapshot_root: Path, arm: str, entries: list[dict[str, Any]]) -> Path:
    """Write and ledger the fixed local marketplace for one archived C plugin pair."""
    marketplace_manifest = snapshot_root / arm / ".agents" / "plugins" / "marketplace.json"
    marketplace_manifest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": _FROZEN_MARKETPLACE_NAME,
        "plugins": [
            {"name": "codemap-py", "source": {"source": "local", "path": "./codemap-py"}},
            {"name": "codex-rig", "source": {"source": "local", "path": "./codex-rig"}},
        ],
    }
    serialized = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    marketplace_manifest.write_bytes(serialized)
    marketplace_manifest.chmod(0o600)
    entries.append(
        {
            "role": f"{arm}:marketplace",
            "archived_path": marketplace_manifest.relative_to(snapshot_root).as_posix(),
            "sha256": hashlib.sha256(serialized).hexdigest(),
            "bytes": len(serialized),
            "mode": 0o600,
        }
    )
    return marketplace_manifest


def _write_input_snapshot(
    snapshot_root: Path,
    *,
    manifest_path: Path,
    tasks_path: Path,
    runner_path: Path,
    invocation_launcher_path: Path | None = None,
    index_path: Path | None,
    auth_source: Path | None,
    arm_archives: Mapping[str, Mapping[str, Path]],
    arm_files: Mapping[str, Mapping[str, Path]] | None = None,
    additional_shared_files: Mapping[str, Path] | None = None,
) -> dict[str, Any]:
    """Write immutable launch inputs without copying credential bytes."""
    if snapshot_root.exists():
        raise FileExistsError(snapshot_root)
    snapshot_root.mkdir(parents=True, mode=0o700)
    entries: list[dict[str, Any]] = []
    shared = snapshot_root / "shared"
    for role, source, relative in (
        ("manifest", manifest_path, Path("manifest.json")),
        ("task_suite", tasks_path, Path(tasks_path.name)),
        ("runner", runner_path, Path(runner_path.name)),
    ):
        _archive_snapshot_file(source, shared / relative, role=role, archive_root=snapshot_root, entries=entries)
    for relative, source in sorted((additional_shared_files or {}).items()):
        _archive_snapshot_file(
            source,
            shared / relative,
            role=f"shared:{relative}",
            archive_root=snapshot_root,
            entries=entries,
        )
    if invocation_launcher_path is not None and invocation_launcher_path.resolve() != runner_path.resolve():
        _archive_snapshot_file(
            invocation_launcher_path,
            shared / invocation_launcher_path.name,
            role="invocation_launcher",
            archive_root=snapshot_root,
            entries=entries,
        )
    if index_path is not None:
        _archive_snapshot_file(
            index_path,
            shared / "locked-index.json",
            role="locked_index",
            archive_root=snapshot_root,
            entries=entries,
        )
    files_by_arm = arm_files or {}
    for arm in sorted(set(arm_archives) | set(files_by_arm)):
        for relative, source in sorted(files_by_arm.get(arm, {}).items()):
            _archive_snapshot_file(
                source,
                snapshot_root / arm / relative,
                role=f"{arm}:{relative}",
                archive_root=snapshot_root,
                entries=entries,
            )
        for package_role, root in sorted(arm_archives.get(arm, {}).items()):
            _archive_snapshot_tree(
                root, snapshot_root / arm / package_role, role=f"{arm}:{package_role}", entries=entries
            )
        if arm == "C_strict":
            _write_frozen_marketplace(snapshot_root, arm, entries)

    auth_metadata: dict[str, Any] | None = {"supplied": True, "archived": False} if auth_source is not None else None

    entries.sort(key=lambda item: (str(item["role"]), str(item["archived_path"])))
    payload = {
        "schema_version": "codex-structural-input-snapshot-v1",
        "files": entries,
        "auth_source": auth_metadata,
    }
    snapshot_path = snapshot_root / "input-snapshot.json"
    serialized = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    snapshot_path.write_bytes(serialized)
    snapshot_path.chmod(0o600)
    payload["path"] = str(snapshot_path.resolve())
    payload["sha256"] = hashlib.sha256(serialized).hexdigest()
    payload["bytes"] = len(serialized)
    return payload


def _validate_invocation_launcher(path: Path, expected_sha256: str) -> None:
    """Require the executing paid launcher to remain the locked regular file."""
    try:
        metadata = path.lstat()
        observed_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError(f"invocation launcher is unavailable: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink() or metadata.st_nlink != 1:
        raise ValueError(f"invocation launcher is not a private regular file: {path}")
    if observed_sha256 != expected_sha256:
        raise ValueError(f"invocation launcher changed: expected {expected_sha256}, observed {observed_sha256}")


def _configure_direct_codemap_launcher(home: ArmHome, codemap_bin: Path | None) -> None:
    """Stage the direct CLI runtime inside B's disposable home and expose it."""
    source_launcher = _validated_direct_codemap_launcher(codemap_bin)
    source_root = source_launcher.parent.parent
    if source_launcher.parent.name != "bin" or source_launcher.name != "codemap-py":
        raise ValueError("--codemap-bin must use the Codemap runtime layout")
    source_files = _direct_runtime_files(source_root)

    # Only the CLI closure is staged: no plugin manifest, skill, marketplace,
    # or Codex Rig bytes enter B's model-visible home.
    staged_root = home.path / "direct-cli"
    staged_launcher = staged_root / "bin" / "codemap-py"
    for relative_path, source in source_files.items():
        destination = staged_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    staged_launcher.chmod(source_launcher.stat().st_mode & 0o777)
    source_hashes = {
        relative_path: hashlib.sha256(source.read_bytes()).hexdigest() for relative_path, source in source_files.items()
    }
    if _runtime_file_hashes(staged_root) != source_hashes:
        raise RuntimeError("staged Codemap runtime differs from its locked source closure")
    home.env["CODEMAP_BIN"] = str(staged_launcher)
    home.codemap_launcher_path = staged_launcher
    home.codemap_launcher_sha256 = hashlib.sha256(staged_launcher.read_bytes()).hexdigest()


def _install_codemap_plugin(
    home: ArmHome,
    marketplace_root: Path | None,
    *,
    plugin_sources: Mapping[str, Path] | None = None,
    codex_bin: str = _CODEX_BIN,
    command_runner: Callable[..., Any] | None = None,
) -> bool:
    """Install Codemap and Codex Rig through an admitted local marketplace."""
    if plugin_sources is None:
        if marketplace_root is None:
            return False
        marketplace_root = marketplace_root.resolve()
        marketplace_manifest = marketplace_root / ".agents" / "plugins" / "marketplace.json"
        if not marketplace_manifest.is_file():
            raise RuntimeError(
                "Codemap plugin source must be a marketplace root containing .agents/plugins/marketplace.json"
            )
        setup_commands = [[codex_bin, "plugin", "marketplace", "add", str(marketplace_root)]]
        add_plugin = [codex_bin, "plugin", "add", "codemap-py@borda-ai-rig", "--json"]
        add_codex_rig = [codex_bin, "plugin", "add", "codex-rig@borda-ai-rig", "--json"]
    else:
        expected_sources = {"codemap-py", "codex-rig"}
        if set(plugin_sources) != expected_sources:
            raise ValueError(f"runtime plugin source set must be {sorted(expected_sources)}")
        if marketplace_root is None:
            raise ValueError("runtime plugin sources require a frozen local marketplace")
        marketplace_root = marketplace_root.resolve(strict=True)
        marketplace_manifest = marketplace_root / ".agents" / "plugins" / "marketplace.json"
        try:
            manifest = json.loads(marketplace_manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("frozen runtime marketplace is unavailable or malformed") from exc
        expected_plugins = [
            {"name": "codemap-py", "source": {"source": "local", "path": "./codemap-py"}},
            {"name": "codex-rig", "source": {"source": "local", "path": "./codex-rig"}},
        ]
        if not isinstance(manifest, Mapping) or manifest.get("name") != _FROZEN_MARKETPLACE_NAME:
            raise ValueError("frozen runtime marketplace name drifted")
        if manifest.get("plugins") != expected_plugins:
            raise ValueError("frozen runtime marketplace schema drifted")
        for name, source in plugin_sources.items():
            if Path(source).resolve(strict=True) != (marketplace_root / name).resolve(strict=True):
                raise ValueError(f"frozen runtime marketplace source drifted for {name}")
        setup_commands = [[codex_bin, "plugin", "marketplace", "add", str(marketplace_root)]]
        add_plugin = [codex_bin, "plugin", "add", f"codemap-py@{_FROZEN_MARKETPLACE_NAME}", "--json"]
        add_codex_rig = [codex_bin, "plugin", "add", f"codex-rig@{_FROZEN_MARKETPLACE_NAME}", "--json"]
    list_plugins = [codex_bin, "plugin", "list", "--json"]
    codex_rig_install_json = ""
    install_json = ""
    for command in (*setup_commands, add_plugin, add_codex_rig):
        code, stdout, stderr = _invoke_plugin_command(command, home.env, command_runner)
        if code != 0:
            raise RuntimeError(f"Codemap plugin setup failed ({' '.join(command[1:4])}): {stderr[:300]}")
        if command is add_plugin:
            install_json = stdout
        elif command is add_codex_rig:
            codex_rig_install_json = stdout
    _configure_codex_rig_plugin(home, codex_rig_install_json)
    _configure_codemap_launcher(home, install_json)
    code, stdout, stderr = _invoke_plugin_command(list_plugins, home.env, command_runner)
    if code != 0 or not _plugin_enabled(stdout, "codex-rig") or not _plugin_enabled(stdout, "codemap-py"):
        raise RuntimeError(
            f"Codemap plugin verification failed: {_plugin_listing_evidence(home, code, stdout, stderr)}"
        )
    return True


def _verify_plain_plugin_absent(
    home: ArmHome,
    *,
    codex_bin: str = _CODEX_BIN,
    command_runner: Callable[..., Any] | None = None,
) -> None:
    """Prove A has no Codemap plugin or Codemap binary exposed on PATH."""
    code, stdout, stderr = _invoke_plugin_command([codex_bin, "plugin", "list", "--json"], home.env, command_runner)
    if code != 0:
        raise RuntimeError(
            f"A_plain plugin absence probe failed: {_plugin_listing_evidence(home, code, stdout, stderr)}"
        )
    admitted, host_plugins = plugin_registration.control_admission(home.path / "config.toml", code, stdout)
    if not admitted:
        raise RuntimeError(
            f"A_plain Codex home carries a treatment plugin: {_plugin_listing_evidence(home, code, stdout, stderr)}"
        )
    home.host_plugin_names = host_plugins
    path_dirs = home.env.get("PATH", "").split(os.pathsep)
    if any(
        (Path(directory) / candidate).exists() for directory in path_dirs for candidate in ("codemap-py", "scan-query")
    ):
        raise RuntimeError("A_plain Codemap binary is exposed on PATH")


def load_tasks_with_provenance(tasks_path: Path, manifest_path: Path = PARITY_MANIFEST_PATH) -> list[dict[str, Any]]:
    """Load raw tasks and fail closed on any locked hash or ordering mismatch."""
    raw_tasks = load_task_suite(tasks_path)
    policies = load_task_policies(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    experiment_revision = _manifest_revision(manifest)
    manifest_rows = {task["id"]: task for suite in manifest.get("suites", []) for task in suite.get("tasks", [])}
    task_ids = [task["id"] for task in raw_tasks]
    matching_suites = [
        suite for suite in manifest.get("suites", []) if [row.get("id") for row in suite.get("tasks", [])] == task_ids
    ]
    if len(matching_suites) != 1:
        raise ValueError("ordered task IDs do not match exactly one locked manifest suite")
    for task_ordinal, task in enumerate(raw_tasks):
        row = manifest_rows.get(task["id"])
        if row is None or task["id"] not in policies:
            raise ValueError(f"no locked task policy for {task['id']!r}")
        if canonical_task_hash(task) != row.get("canonical_task_sha256"):
            raise ValueError(f"task hash mismatch for {task['id']!r}")
        if prompt_hash(task) != row.get("prompt_sha256"):
            raise ValueError(f"prompt hash mismatch for {task['id']!r}")
    suite_hash = semantic_suite_hash(raw_tasks)
    raw_hash = hashlib.sha256(tasks_path.read_bytes()).hexdigest()
    loaded: list[dict[str, Any]] = []
    for task_ordinal, task in enumerate(raw_tasks):
        policy: TaskPolicy = policies[task["id"]]
        if policy.experiment_revision != experiment_revision:
            raise ValueError(f"task policy revision mismatch for {task['id']!r}")
        item = dict(task)
        item[_PROVENANCE_KEY] = {
            "experiment_revision": experiment_revision,
            "task_hash": canonical_task_hash(task),
            "prompt_hash": prompt_hash(task),
            "suite_hash": suite_hash,
            "suite_raw_hash": raw_hash,
            "task_ordinal": task_ordinal,
            "oracle_class": policy.oracle_class,
            "headline_eligible_v1": policy.headline_eligible_v1,
            "scoreable": policy.scoreable,
        }
        loaded.append(item)
    return loaded


@dataclass
class CodexRun:
    """Normalized provider result carrying shared provenance and native telemetry."""

    arm: str
    task_id: str
    task_type: str
    model: str
    reasoning_effort: str = PARITY_CODEX_REASONING_EFFORT
    provider: str = "codex"
    capability_strata: tuple[str, ...] = ()
    quality_components: dict[str, float] = field(default_factory=dict)
    repetition: int = 1
    success: bool = False
    experiment_revision: str = ""
    parity_arm: str = ""
    task_hash: str = ""
    prompt_hash: str = ""
    suite_hash: str = ""
    suite_raw_hash: str = ""
    evaluator_id: str = ""
    evaluator_hash: str = ""
    envelope_hash: str = ""
    arm_contract_hash: str = ""
    repo_sha: str = "unknown"
    index_sha: str = "unknown"
    oracle_class: str = "unknown"
    headline_eligible_v1: bool = False
    scoreable: bool = True
    targeted: bool = False
    diagnostic_only: bool = False
    study_mode: str = "confirmatory"
    quality_score: float | None = None
    correct: bool = False
    input_tokens: int = 0
    cached_input_tokens: int = 0
    fresh_input_tokens: int | None = None
    token_accounting_inconsistent: bool = False
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    command_calls: int = 0
    codemap_observed_calls: int = 0
    codemap_calls: int = 0
    codemap_successful_calls: int = 0
    codemap_compact_successful_calls: int = 0
    codemap_direct_calls: int = 0
    codemap_direct_successful_calls: int = 0
    codemap_direct_compact_successful_calls: int = 0
    codemap_skill_calls: int = 0
    codemap_skill_successful_calls: int = 0
    codemap_skill_compact_successful_calls: int = 0
    successful_query_arguments: list[list[str]] = field(default_factory=list)
    locked_query_conformance: bool | None = None
    locked_query_fitness: float | None = None
    locked_query_endpoint_fitness: float | None = None
    locked_query_target_fitness: float | None = None
    locked_query_option_fitness: float | None = None
    skill_delivery_observed: bool = False
    codemap_errors: int = 0
    fallback_calls: int = 0
    # Nonzero means the provider reported usage this parser could not read as a
    # token count, so this row's cost is an undercount rather than a cheap cell.
    malformed_usage: int = 0
    compliance: bool | None = None
    treatment_adherence: bool = False
    codemap_delivery: str = "none"
    incomplete: bool = False
    extraction_failed: bool = False
    contaminated: bool = False
    stage_evidence: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    error_type: str = ""
    output_text: str = ""
    thread_id: str = ""
    raw_events: list[dict[str, Any]] = field(default_factory=list)
    telemetry_contract_id: str = _NATIVE_ITEM_TELEMETRY_CONTRACT_ID
    native_item_counts: dict[str, int] = field(default_factory=dict)
    elapsed_s: float = 0.0
    tool_elapsed_s: float | None = None
    tool_result_tokens: int | None = None
    native_attempt_events: list[list[dict[str, Any]]] = field(default_factory=list)
    retry_count: int = 0
    execution_index: int = -1
    cell_wall_clock_limit_s: float = PARITY_TIMEOUT_SECONDS
    turn_budget_enforced: bool = False


@dataclass(frozen=True)
class LockedQueryFitness:
    """Continuous exact-query similarity with independently visible components."""

    overall: float
    endpoint: float
    target: float
    options: float


def _arm_compliance(arm: str, evidence: runtime.CodexParseResult | CodexRun) -> bool | None:
    """Evaluate the transport-specific required-use contract for one arm.

    Scope of the claim: the ``_skill_`` / ``_direct_`` prefixes are configured by
    construction, not observed — the parser labels a query "skill" only because
    the C home supplied a ``skill_path`` (see ``runtime.parse_codex_jsonl``). Both
    branches prove "a successful canonical compact query ran in a verified home
    for this arm"; the stream cannot tell B and C apart, since both end in the
    same ``$CODEMAP_BIN`` command. Never read C compliance as proof the Skill was
    read — ``skill_delivery_observed`` is the separate observational signal.
    """
    if arm == "B_auto":
        return evidence.codemap_direct_compact_successful_calls > 0
    if arm == "C_strict":
        return evidence.codemap_skill_compact_successful_calls > 0
    if arm == "A_plain":
        return None
    raise ValueError(f"unknown benchmark arm {arm!r}")


def _locked_query_conformance(
    task: Mapping[str, Any], arm: str, run: runtime.CodexParseResult | CodexRun
) -> bool | None:
    """Report whether successful compact queries exactly match the locked contract."""
    if arm == "A_plain":
        return None
    locked = _locked_expected_queries(task)
    if not locked:
        return None
    observed = {
        normalized
        for arguments in run.successful_query_arguments
        if arguments and (normalized := _normalize_locked_query(arguments[0], arguments[1:])) is not None
    }
    if _expected_query_policy(task) == "all_required":
        return all(query in observed for query in locked)
    return any(query in observed for query in locked)


def _locked_query_fitness(
    task: Mapping[str, Any],
    arm: str,
    run: runtime.CodexParseResult | CodexRun,
) -> LockedQueryFitness | None:
    """Score exact-query similarity and expose endpoint, target, and option contributions."""
    if arm == "A_plain":
        return None
    locked = _locked_expected_queries(task)
    if not locked:
        return None
    observed = [
        normalized
        for arguments in run.successful_query_arguments
        if arguments and (normalized := _normalize_locked_query(arguments[0], arguments[1:])) is not None
    ]
    if not observed:
        return LockedQueryFitness(0.0, 0.0, 0.0, 0.0)
    best_matches = [_best_locked_query_match(expected_query, observed) for expected_query in locked]
    if _expected_query_policy(task) == "all_required":
        return _mean_locked_query_fitness(best_matches)
    return max(
        best_matches,
        key=lambda match: (match.overall, match.endpoint, match.target, match.options),
    )


def _best_locked_query_match(
    expected_query: tuple[str, ...],
    observed_queries: list[tuple[str, ...]],
) -> LockedQueryFitness:
    """Return the single observed query most similar to one locked query."""
    matches = [_locked_query_pair_fitness(expected_query, actual_query) for actual_query in observed_queries]
    return max(matches, key=lambda match: (match.overall, match.endpoint, match.target, match.options))


def _mean_locked_query_fitness(matches: list[LockedQueryFitness]) -> LockedQueryFitness:
    """Average required-query fitness without mixing independently matched components."""
    count = len(matches)
    return LockedQueryFitness(
        overall=sum(match.overall for match in matches) / count,
        endpoint=sum(match.endpoint for match in matches) / count,
        target=sum(match.target for match in matches) / count,
        options=sum(match.options for match in matches) / count,
    )


_EXPECTED_QUERY_POLICIES = frozenset({"any_match", "all_required"})


def _expected_query_policy(task: Mapping[str, Any]) -> str:
    """Return the task query-match policy, defaulting legacy tasks to any-match."""
    policy = task.get("expected_query_policy", "any_match")
    if not isinstance(policy, str) or policy not in _EXPECTED_QUERY_POLICIES:
        choices = ", ".join(sorted(_EXPECTED_QUERY_POLICIES))
        raise ValueError(f"expected_query_policy must be one of {choices}")
    return policy


def _locked_expected_queries(task: Mapping[str, Any]) -> list[tuple[str, ...]]:
    """Normalize every valid expected query declared by one task."""
    expected = task.get("expected_queries")
    if not isinstance(expected, list) or not expected:
        return []
    locked: list[tuple[str, ...]] = []
    for query in expected:
        if not isinstance(query, Mapping) or not isinstance(query.get("cmd"), str):
            continue
        arguments = query.get("args", [])
        if isinstance(arguments, list) and all(isinstance(value, str) for value in arguments):
            normalized = _normalize_locked_query(str(query["cmd"]), arguments)
            if normalized is not None:
                locked.append(normalized)
    return locked


def _locked_query_pair_fitness(
    expected_query: tuple[str, ...],
    actual_query: tuple[str, ...],
) -> LockedQueryFitness:
    """Measure overall and component similarity for one locked/observed pair."""
    expected_endpoint, expected_targets, expected_options = _split_normalized_query(expected_query)
    actual_endpoint, actual_targets, actual_options = _split_normalized_query(actual_query)
    return LockedQueryFitness(
        overall=_token_set_similarity(expected_query, actual_query),
        endpoint=float(expected_endpoint == actual_endpoint),
        target=_token_set_similarity(expected_targets, actual_targets),
        options=_token_set_similarity(expected_options, actual_options),
    )


def _token_set_similarity(expected: tuple[str, ...], actual: tuple[str, ...]) -> float:
    """Return Jaccard similarity, treating two empty query components as equal."""
    expected_tokens = set(expected)
    actual_tokens = set(actual)
    if not expected_tokens and not actual_tokens:
        return 1.0
    return len(expected_tokens & actual_tokens) / len(expected_tokens | actual_tokens)


_LOCKED_QUERY_BOOLEAN_OPTIONS = frozenset({"--broken", "--exclude-tests", "--with-imports"})
_LOCKED_QUERY_VALUE_OPTIONS = frozenset({"--limit", "--top"})


def _split_normalized_query(query: tuple[str, ...]) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Separate a normalized query into endpoint, positional targets, and option groups."""
    endpoint = query[0]
    targets: list[str] = []
    options: list[str] = []
    index = 1
    while index < len(query):
        token = query[index]
        if token in _LOCKED_QUERY_BOOLEAN_OPTIONS:
            options.append(token)
        elif token in _LOCKED_QUERY_VALUE_OPTIONS:
            options.append(f"{token}={query[index + 1]}")
            index += 1
        else:
            targets.append(token)
        index += 1
    return endpoint, tuple(targets), tuple(options)


def _normalize_locked_query(command: str, arguments: list[str]) -> tuple[str, ...] | None:
    """Canonicalize the locked query grammar without weakening task semantics.

    Positional arguments retain their order.  Only the registered boolean options may move, and ``--limit``/``--top``
    accept one non-negative decimal value. Unknown, duplicate, missing-value, and extra option tokens are rejected.
    """
    if not command or not isinstance(command, str):
        return None
    positionals: list[str] = []
    booleans: set[str] = set()
    values: dict[str, str] = {}
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if not isinstance(argument, str) or not argument:
            return None
        if argument in _LOCKED_QUERY_BOOLEAN_OPTIONS:
            if argument in booleans:
                return None
            booleans.add(argument)
        elif argument in _LOCKED_QUERY_VALUE_OPTIONS:
            if argument in values or index + 1 >= len(arguments):
                return None
            value = arguments[index + 1]
            if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value):
                return None
            values[argument] = str(int(value))
            index += 1
        elif argument.startswith("-"):
            return None
        else:
            positionals.append(argument)
        index += 1
    normalized = [command, *positionals, *sorted(booleans)]
    for option, value in sorted(values.items()):
        normalized.extend((option, value))
    return tuple(normalized)


def _pooling_ineligibility_reasons(run: CodexRun) -> tuple[str, ...]:
    """Return run-level admission failures that forbid canonical pooling.

    Unscoreable diagnostic cells are intentionally absent: they are planned
    exclusions, unlike incomplete, contaminated, malformed-token, and
    required-use-invalid results.
    """
    reasons: list[str] = []
    if not run.success:
        reasons.append("unsuccessful")
    if run.incomplete:
        reasons.append("incomplete")
    if run.extraction_failed:
        reasons.append("extraction_failed")
    if run.contaminated:
        reasons.append("contaminated")
    if run.token_accounting_inconsistent:
        reasons.append("token_accounting_inconsistent")
    if run.targeted:
        reasons.append("targeted")
    if run.diagnostic_only:
        reasons.append("diagnostic_only")
    # Only the strict arm carries a required-use contract. B is an optional-use canary on
    # both providers, so a zero-query B cell is compliant and stays poolable; excluding it
    # here dropped exactly the cells where the model declined to query, which biased the
    # pooled B result toward the runs that happened to use Codemap.
    if run.arm == "C_strict" and run.compliance is not True:
        reasons.append("required_use_missing")
    return tuple(reasons)


def _infrastructure_failure_signature(run: CodexRun) -> str | None:
    """Return a recurrence key only for pre-response runner/provider failures."""
    if run.success or not run.incomplete or run.input_tokens or run.output_tokens or run.output_text.strip():
        return None
    if run.error_type == "authentication_failed":
        return "authentication_failed"
    if run.error_type not in {
        "authentication_state_failed",
        "launch_os_error",
        "missing_terminal",
        "non_zero_exit",
        "response_failed",
        "transport_error",
        "turn_failed",
    }:
        return None
    normalized = run.error.casefold()
    http_class = re.search(r"\b([45])\d\d\b", normalized)
    return f"{run.error_type}:http_{http_class.group(1)}xx" if http_class else run.error_type


@lru_cache(maxsize=1)
def _reference_bench_module() -> Any:
    """Load the Claude reference module once to reuse its exact evaluator registry."""
    module_path = Path(__file__).with_name("run-claude-structural.py")
    spec = importlib.util.spec_from_file_location("_codex_shared_bench_reference", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load shared evaluator")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _diff_impact_stager(repo_path: Path, task: Mapping[str, Any]) -> Any | None:
    """Return the shared Claude stager for one DI task, or ``None``."""
    if task.get("type") != "diff_impact":
        return None
    stage_spec = task.get("stage")
    if not isinstance(stage_spec, list) or not stage_spec:
        return None
    return _reference_bench_module().DiffImpactStager(repo_path, stage_spec)


def _validate_diff_impact_stage(repo_path: Path, task: Mapping[str, Any]) -> None:
    """Validate every DI anchor without mutating the target tree."""
    stager = _diff_impact_stager(repo_path, task)
    if stager is None:
        return
    stager._assert_clean()
    for edit in stager.stage_spec:
        if not isinstance(edit, Mapping) or not isinstance(edit.get("file"), str):
            raise ValueError(f"invalid DI stage edit for {task.get('id', '<unknown>')}")
        path = repo_path / str(edit["file"])
        if not path.is_file():
            raise ValueError(f"DI stage anchor file is unavailable: {edit['file']}")
        text = path.read_text(encoding="utf-8")
        if "append" in edit:
            continue
        if "find" not in edit or "replace" not in edit or str(edit["find"]) not in text:
            raise ValueError(f"DI stage anchor is stale: {edit['file']}")


def _default_evaluator(task: Mapping[str, Any], output_text: str) -> EvaluationResult:
    """Invoke the exact evaluator registry used by the Claude reference adapter."""
    return _reference_bench_module()._SHARED_EVALUATORS.evaluate(task, output_text)


def _evaluator_identity(task: Mapping[str, Any], evaluator: Callable[..., Any]) -> tuple[str, str]:
    """Return the shared evaluator ID/hash, or deterministic fixture provenance."""
    if evaluator is _default_evaluator:
        return _reference_bench_module()._evaluator_provenance(task)
    identifier = getattr(evaluator, "__name__", evaluator.__class__.__name__)
    try:
        source = inspect.getsource(evaluator)
    except (OSError, TypeError):
        source = identifier
    return identifier, hashlib.sha256((identifier + "\n" + source).encode("utf-8")).hexdigest()


class CodexRunner:
    """Run one canonical Codex cell with injectable process/evaluator seams."""

    def __init__(
        self,
        model: str,
        repo_path: Path,
        *,
        reasoning_effort: str = PARITY_CODEX_REASONING_EFFORT,
        index_path: Path | None = None,
        timeout: float = PARITY_TIMEOUT_SECONDS,
        marketplace_root: Path | None = None,
        codemap_bin: Path | None = None,
        manifest_path: Path = PARITY_MANIFEST_PATH,
        index_relocation: Mapping[str, str] | None = None,
        auth_source: Path | None = None,
        plugin_installer: Callable[[Path], bool | None] | None = None,
        plugin_probe: Callable[[Path], bool] | None = None,
        targeted: bool = False,
        command_runner: Callable[..., Any] | None = None,
        transport: Callable[..., str | bytes | Iterable[str | bytes]] | None = None,
        evaluator: Callable[[Mapping[str, Any], str], EvaluationResult] | None = None,
        evidence_roots: Iterable[Path] = (),
    ) -> None:
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.repo_path = Path(repo_path).resolve()
        self.index_path = _canonical_index_path(Path(index_path)) if index_path else None
        self.timeout = timeout
        self.marketplace_root = marketplace_root.resolve() if marketplace_root else None
        self.codemap_bin = Path(codemap_bin) if codemap_bin else None
        self.manifest_path = Path(manifest_path)
        # A relocated index carries run-owned provenance.
        self.index_relocation = dict(index_relocation) if index_relocation is not None else None
        # Preserve this path so auth-copy rejects symlinks.
        self.auth_source = Path(auth_source) if auth_source else None
        self.plugin_installer = plugin_installer
        self.plugin_probe = plugin_probe
        self.targeted = targeted
        self.command_runner = command_runner
        self.transport = transport
        self.evaluator = evaluator or _default_evaluator
        supplied_evidence_roots = tuple(evidence_roots)
        self._evidence_roots = (
            tuple(Path(root).resolve(strict=False) for root in supplied_evidence_roots)
            if supplied_evidence_roots
            else _benchmark_evidence_roots()
        )
        self._auth_state: _RunAuthState | None = None
        self._auth_state_dir: Path | None = None
        self._runtime_snapshot_sources: dict[str, dict[str, Path]] = {}
        self._runtime_snapshot_marketplaces: dict[str, Path] = {}
        self._runtime_snapshot_hashes: dict[Path, str] = {}
        self._runtime_snapshot_modes: dict[Path, int] = {}
        self._runtime_evidence_path: Path | None = None
        # Record every coordination-root cleanup failure.
        self.coordination_cleanup_errors: list[str] = []

    @property
    def evidence_roots(self) -> tuple[Path, ...]:
        """Return denied roots."""
        return self._evidence_roots

    def _cleanup_coordination(self, coordination_path: Path | None) -> str | None:
        """Remove one coordination root, recording rather than discarding a failure.

        Never raises: every call site is inside a ``finally`` block, where raising would
        mask the exception that carries the real cause. The returned message lets a
        cell-scoped caller additionally promote the failure to contamination.
        """
        if coordination_path is None:
            return None
        try:
            _cleanup_coordination_root(coordination_path)
        except ValueError as exc:
            message = f"coordination cleanup failed for {coordination_path}: {exc}"
            self.coordination_cleanup_errors.append(message)
            return message
        return None

    def _bind_runtime_snapshot(
        self,
        snapshot_root: Path,
        arm_archives: Mapping[str, Mapping[str, Path]],
    ) -> None:
        """Bind later B/C cells to the exact package bytes archived for this run."""
        snapshot_root = Path(snapshot_root).resolve(strict=True)
        snapshot_path = snapshot_root / "input-snapshot.json"
        try:
            payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
            entries = payload["files"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("runtime input snapshot is unavailable or malformed") from exc
        if not isinstance(entries, list):
            raise ValueError("runtime input snapshot does not contain file identities")
        expected_hashes: dict[Path, str] = {}
        expected_modes: dict[Path, int] = {}
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise ValueError("runtime input snapshot contains malformed file identities")
            archived_path = entry.get("archived_path")
            sha256 = entry.get("sha256")
            mode = entry.get("mode")
            if (
                not isinstance(archived_path, str)
                or not isinstance(sha256, str)
                or not isinstance(mode, int)
                or mode not in {0o600, 0o700}
            ):
                raise ValueError("runtime input snapshot contains malformed file identities")
            relative_path = Path(archived_path)
            if relative_path.is_absolute():
                raise ValueError("runtime input snapshot contains an unsafe archived path")
            path = snapshot_root / relative_path
            if not path.resolve(strict=False).is_relative_to(snapshot_root):
                raise ValueError("runtime input snapshot contains an unsafe archived path")
            expected_hashes[path] = sha256
            expected_modes[path] = mode
        bound_sources: dict[str, dict[str, Path]] = {}
        bound_marketplaces: dict[str, Path] = {}
        for arm, archives in arm_archives.items():
            if arm not in {"B_auto", "C_strict"}:
                raise ValueError(f"runtime snapshot cannot bind unsupported arm {arm!r}")
            bound_sources[arm] = {}
            for role, source in archives.items():
                if role == "marketplace":
                    continue
                source_path = Path(source).resolve(strict=True)
                if not source_path.is_relative_to(snapshot_root):
                    raise ValueError("runtime snapshot source escaped the run-owned inputs directory")
                if not any(path.is_relative_to(source_path) for path in expected_hashes):
                    raise ValueError(f"runtime snapshot lacks identities for {arm}:{role}")
                bound_sources[arm][role] = source_path
            marketplace = archives.get("marketplace")
            if marketplace is None and arm == "C_strict":
                plugin_root = bound_sources[arm].get("codemap-py")
                if (
                    plugin_root is not None
                    and (plugin_root.parent / ".agents" / "plugins" / "marketplace.json").is_file()
                ):
                    marketplace = plugin_root.parent
            if marketplace is not None:
                if arm != "C_strict":
                    raise ValueError("only the C runtime snapshot can bind a frozen marketplace")
                marketplace_path = Path(marketplace).resolve(strict=True)
                if not marketplace_path.is_relative_to(snapshot_root):
                    raise ValueError("runtime marketplace escaped the run-owned inputs directory")
                self._validate_runtime_snapshot_tree(marketplace_path, expected_hashes, expected_modes)
                manifest_path = marketplace_path / ".agents" / "plugins" / "marketplace.json"
                expected_manifest = expected_hashes.get(manifest_path)
                if expected_manifest is None or expected_modes.get(manifest_path) != 0o600:
                    raise ValueError("runtime marketplace lacks a locked private manifest")
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise ValueError("runtime marketplace manifest is unavailable or malformed") from exc
                expected_plugins = [
                    {"name": "codemap-py", "source": {"source": "local", "path": "./codemap-py"}},
                    {"name": "codex-rig", "source": {"source": "local", "path": "./codex-rig"}},
                ]
                if not isinstance(manifest, Mapping) or manifest.get("name") != _FROZEN_MARKETPLACE_NAME:
                    raise ValueError("runtime marketplace name drifted")
                if manifest.get("plugins") != expected_plugins:
                    raise ValueError("runtime marketplace schema drifted")
                bound_marketplaces[arm] = marketplace_path
        self._runtime_snapshot_sources = bound_sources
        self._runtime_snapshot_marketplaces = bound_marketplaces
        self._runtime_snapshot_hashes = expected_hashes
        self._runtime_snapshot_modes = expected_modes
        self._runtime_evidence_path = snapshot_root.parent / "runtime-isolation.jsonl"
        runtime_history_root = snapshot_root.parent
        if runtime_history_root not in self._evidence_roots:
            self._evidence_roots = (*self._evidence_roots, runtime_history_root)

    def _validate_runtime_snapshot_tree(
        self,
        source: Path,
        expected_hashes: Mapping[Path, str] | None = None,
        expected_modes: Mapping[Path, int] | None = None,
    ) -> None:
        """Fail closed when a run-owned runtime tree no longer matches its input ledger."""
        source = Path(source).resolve(strict=True)
        expected_hashes = self._runtime_snapshot_hashes if expected_hashes is None else expected_hashes
        expected_modes = self._runtime_snapshot_modes if expected_modes is None else expected_modes
        expected = {path: sha256 for path, sha256 in expected_hashes.items() if path.is_relative_to(source)}
        if not expected:
            raise ValueError(f"runtime snapshot has no locked identities for {source}")
        observed: dict[Path, str] = {}
        for path in source.rglob("*"):
            if path.is_symlink():
                raise ValueError(f"runtime snapshot source contains symlink: {source}")
            if path.is_file():
                observed[path] = hashlib.sha256(path.read_bytes()).hexdigest()
        if observed != expected:
            expected_identity = _aggregate_file_hashes(
                {path.relative_to(source).as_posix(): sha256 for path, sha256 in expected.items()}
            )
            observed_identity = _aggregate_file_hashes(
                {path.relative_to(source).as_posix(): sha256 for path, sha256 in observed.items()}
            )
            raise ValueError(
                f"runtime snapshot byte drift for {source.name}: expected={expected_identity} observed={observed_identity}"
            )
        expected_modes = {path: mode for path, mode in expected_modes.items() if path.is_relative_to(source)}
        if os.name != "nt":
            observed_modes = {path: stat.S_IMODE(path.lstat().st_mode) for path in observed}
            if observed_modes != expected_modes:
                raise ValueError(f"runtime snapshot mode drift for {source.name}")

    def _runtime_plugin_sources(self, arm: str) -> dict[str, Path] | None:
        """Return validated C plugin sources when this run already owns a snapshot."""
        sources = self._runtime_snapshot_sources.get(arm)
        if sources is None:
            return None
        if set(sources) != {"codemap-py", "codex-rig"}:
            raise ValueError("C runtime snapshot lacks the locked plugin pair")
        marketplace = self._runtime_snapshot_marketplaces.get(arm)
        if marketplace is not None:
            self._validate_runtime_snapshot_tree(marketplace)
        for source in sources.values():
            self._validate_runtime_snapshot_tree(source)
        return dict(sources)

    def _runtime_direct_launcher(self, arm: str) -> Path | None:
        """Return the validated B launcher from this run's snapshot when available."""
        sources = self._runtime_snapshot_sources.get(arm)
        if sources is None:
            return self.codemap_bin
        direct_root = sources.get("direct-cli")
        if direct_root is None or set(sources) != {"direct-cli"}:
            raise ValueError("B runtime snapshot lacks the locked direct CLI")
        self._validate_runtime_snapshot_tree(direct_root)
        return direct_root / "bin" / "codemap-py"

    def _record_runtime_failure(
        self,
        arm: str,
        error: BaseException,
        *,
        home: ArmHome | None = None,
        source_paths: Iterable[Path] = (),
    ) -> None:
        """Persist non-secret expected and observed runtime identities before home cleanup."""
        if self._runtime_evidence_path is None:
            return
        expected, expected_plugins = self._expected_runtime_identities()
        payload = {
            "arm": arm,
            "error": str(error),
            "expected_artifact_sha256": expected,
            "expected_plugin_identities": expected_plugins,
            "observed_plugin_identities": self._observed_plugin_identities(home, source_paths),
            "status": "failed",
        }
        self._append_runtime_evidence(payload)

    def _record_runtime_success(self, arm: str, home: ArmHome) -> None:
        """Persist the verified plugin identities before the disposable home is removed."""
        if self._runtime_evidence_path is None:
            return
        expected, expected_plugins = self._expected_runtime_identities()
        observed = self._observed_plugin_identities(home, ())
        if observed != expected_plugins:
            raise ValueError("verified runtime plugin identities differ from the locked manifest")
        self._append_runtime_evidence(
            {
                "arm": arm,
                "error": None,
                "expected_artifact_sha256": expected,
                "expected_plugin_identities": expected_plugins,
                "observed_plugin_identities": observed,
                "status": "verified",
            }
        )

    def _observed_plugin_identities(
        self,
        home: ArmHome | None,
        source_paths: Iterable[Path],
    ) -> dict[str, dict[str, str]]:
        """Return public plugin identity fields from sources that still exist."""
        observed: dict[str, dict[str, str]] = {}
        for source in source_paths:
            manifest_path = Path(source) / ".codex-plugin" / "plugin.json"
            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, TypeError, json.JSONDecodeError):
                continue
            if isinstance(payload, Mapping) and isinstance(payload.get("name"), str):
                observed[payload["name"]] = {
                    "version": str(payload.get("version", "")),
                    "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                }
        if home is not None:
            for name, path in (("codemap-py", home.codemap_plugin_path), ("codex-rig", home.codex_rig_path)):
                if path is not None:
                    manifest_path = path / ".codex-plugin" / "plugin.json"
                    try:
                        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                    except (OSError, TypeError, json.JSONDecodeError):
                        continue
                    observed[name] = {
                        "version": str(payload.get("version", "")) if isinstance(payload, Mapping) else "",
                        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                    }
        return observed

    def _expected_runtime_identities(self) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
        """Return locked public artifact and plugin identities from the manifest."""
        expected: Mapping[str, Any] = {}
        expected_plugins: dict[str, dict[str, str]] = {}
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            candidate = manifest.get("artifact_sha256", {})
            if isinstance(candidate, Mapping):
                expected = {str(name): str(value) for name, value in candidate.items()}
            for name, candidate_key, manifest_key in (
                ("codemap-py", "codemap_candidate", "codemap_candidate_manifest"),
                ("codex-rig", "codex_rig_candidate", "codex_rig_plugin_manifest"),
            ):
                version = manifest.get(candidate_key, {})
                expected_plugins[name] = {
                    "version": str(version.get("version", "")) if isinstance(version, Mapping) else "",
                    "manifest_sha256": expected.get(manifest_key, ""),
                }
        except (OSError, TypeError, json.JSONDecodeError):
            pass
        return dict(expected), expected_plugins

    def _append_runtime_evidence(self, payload: Mapping[str, Any]) -> None:
        """Append one private runtime-identity and evidence-boundary record."""
        if self._runtime_evidence_path is None:
            return
        record = {**payload, "evidence_roots": [str(root) for root in self._evidence_roots]}
        self._runtime_evidence_path.parent.mkdir(parents=True, exist_ok=True)
        with self._runtime_evidence_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        self._runtime_evidence_path.chmod(0o600)

    def _ensure_auth_state(self) -> _RunAuthState | None:
        """Lazily seed the private credential chain for this runner."""
        if self.auth_source is None:
            return None
        if self._auth_state is None:
            self._auth_state = _RunAuthState(self.auth_source)
            self._auth_state_dir = self._auth_state.directory
        return self._auth_state

    def close(self) -> None:
        """Remove the runner-owned private credential chain."""
        if self._auth_state is not None:
            self._auth_state.close()

    def build_command(self, prompt: str, *, working_directory: Path | None = None) -> list[str]:
        """Build this runner's canonical Codex command."""
        return build_codex_command(
            working_directory or self.repo_path,
            self.model,
            prompt,
            reasoning_effort=self.reasoning_effort,
        )

    # Declared stage surface (see module-level note): delegating replacements for
    # stage-module reach-ins. `run_stream` avoids the name `subprocess` so it
    # cannot be confused with the stdlib module.
    def prepare_verified_home(self, arm: str, **kwargs: Any) -> ArmHome:
        """Create and verify one arm home."""
        return self._prepare_verified_home(arm, **kwargs)

    def run_stream(self, command: list[str], env: Mapping[str, str], **kwargs: Any) -> str:
        """Run one Codex attempt and return its raw stream."""
        return self._subprocess(command, env, **kwargs)

    def _admit_runtime(self, arm: str, diff_impact_stage: DiffImpactStageAdmission | None = None) -> None:
        """Admit this run's target and index for one arm, carrying any relocation provenance.

        Every admission routes through here, so a relocated index is proven the same way at each; reaching the module-
        level check directly would demand byte identity a worktree cannot have.
        """
        _validate_locked_runtime(
            self.repo_path,
            self.index_path,
            arm,
            self.manifest_path,
            diff_impact_stage,
            self.index_relocation,
        )

    def _prepare_verified_home(
        self,
        arm: str,
        *,
        diff_impact_stage: DiffImpactStageAdmission | None = None,
        writable_workspace: Path | None = None,
        denied_workspace: Path | None = None,
        index_relocation: Mapping[str, str] | None = None,
        historical_runtime_coordinate: Mapping[str, str] | None = None,
        fixture_runtime_coordinate: Mapping[str, Any] | None = None,
    ) -> ArmHome:
        """Create and verify one arm home without invoking a model.

        Historical Patch and copied-fixture coordinates are explicit opt-ins; other callers use the active manifest.
        """
        # Historical coordinates do not inherit the active-run relocation.
        run_relocation = None if historical_runtime_coordinate is not None else self.index_relocation
        _validate_locked_runtime(
            self.repo_path,
            self.index_path,
            arm,
            self.manifest_path,
            diff_impact_stage,
            index_relocation if index_relocation is not None else run_relocation,
            historical_runtime_coordinate,
            fixture_runtime_coordinate,
        )
        auth_state = self._ensure_auth_state()
        if auth_state is not None:
            auth_state.assert_source_unchanged()
        runtime_plugin_sources: dict[str, Path] | None = None
        runtime_marketplace: Path | None = None
        bound_sources = self._runtime_snapshot_sources.get(arm, {})
        home: ArmHome | None = None
        try:
            if arm == "C_strict":
                runtime_plugin_sources = self._runtime_plugin_sources(arm)
                runtime_marketplace = self._runtime_snapshot_marketplaces.get(arm)
            runtime_codemap_bin = self._runtime_direct_launcher(arm) if arm == "B_auto" else self.codemap_bin
            home = prepare_arm_home(
                arm,
                auth_source=None,
                codemap_bin=runtime_codemap_bin,
                plugin_installer=None if runtime_plugin_sources is not None else self.plugin_installer,
            )
            if auth_state is not None:
                auth_state.seed_home(home.path)
                home.auth_provisioned = True
            if arm != "A_plain" and self.index_path is not None:
                home.env["CODEMAP_PYTHON"] = _verify_locked_codemap_python(
                    manifest_path=self.manifest_path,
                    command_runner=self.command_runner,
                )
                home.env["SCAN_NO_AUTOBUILD"] = "1"
                home.env["CODEMAP_LOGGING"] = "false"
                home.env["CODEX_CODEMAP_AVAILABLE"] = "1"
                # Sole writable path of a measured cell: outside the workspace, discarded with this cell's home.
                gate = home.path.parent / f"{home.path.name}-gate"
                if gate.resolve().is_relative_to(self.repo_path.resolve()):
                    raise ValueError("Codemap coordination root must be outside the measured workspace")
                home.coordination_path = _prepare_coordination_root(self.index_path, gate)
                home.env["CODEMAP_COORDINATION_DIR"] = str(home.coordination_path)
            else:
                for variable in (
                    "CODEMAP_BIN",
                    "CODEMAP_COORDINATION_DIR",
                    "CODEMAP_INDEX",
                    "CODEMAP_INDEX_DIR",
                    "CODEMAP_PYTHON",
                    "CODEMAP_SKILL_FILE",
                    "SCAN_NO_AUTOBUILD",
                    "CODEMAP_LOGGING",
                ):
                    home.env.pop(variable, None)
            if arm == "C_strict":
                if runtime_plugin_sources is not None:
                    home.codemap_verified = _install_codemap_plugin(
                        home,
                        runtime_marketplace,
                        plugin_sources=runtime_plugin_sources,
                        command_runner=self.command_runner,
                    )
                elif self.plugin_probe is not None:
                    home.codemap_verified = bool(self.plugin_probe(home.path))
                elif not home.codemap_verified:
                    home.codemap_verified = _install_codemap_plugin(
                        home,
                        self.marketplace_root,
                        command_runner=self.command_runner,
                    )
            if arm != "A_plain":
                if not home.codemap_verified:
                    raise RuntimeError("Codemap delivery is not verified")
                home.codemap_available = True
                _verify_treatment_artifact_locks(home, self.manifest_path)
            if arm == "C_strict" and (
                home.codemap_skill_path is None
                or not home.codemap_skill_sha256
                or home.codex_rig_path is None
                or not home.codex_rig_manifest_sha256
            ):
                raise RuntimeError("installed Codemap skill and Codex Rig are not verified")
            if arm == "C_strict" and fixture_runtime_coordinate is None:
                if self.index_path is None:
                    raise ValueError("C_strict admission requires the locked index")
                _admit_installed_skill_pair(
                    home,
                    self.repo_path,
                    self.index_path,
                    manifest_path=self.manifest_path,
                    command_runner=self.command_runner,
                )
            _write_permission_config(
                home,
                arm,
                self.index_path,
                marketplace_root=self.marketplace_root,
                writable_workspace=writable_workspace,
                denied_workspace=denied_workspace,
                evidence_roots=self._evidence_roots,
            )
            if arm == "C_strict":
                _verify_installed_plugin_pair(home, command_runner=self.command_runner)
            _verify_permission_profile(
                home,
                self.repo_path,
                self.index_path,
                command_runner=self.command_runner,
                writable_workspace=writable_workspace,
            )
            if arm == "B_auto" and fixture_runtime_coordinate is None:
                if self.index_path is None:
                    raise ValueError("B_auto admission requires the locked index")
                _admit_staged_direct_cli(
                    home,
                    self.repo_path,
                    self.index_path,
                    manifest_path=self.manifest_path,
                    command_runner=self.command_runner,
                )
            if home.auth_provisioned:
                _verify_authentication(home, command_runner=self.command_runner)
                if auth_state is not None:
                    auth_state.refresh_from_home(home.path)
            if arm == "A_plain":
                _verify_plain_plugin_absent(home, command_runner=self.command_runner)
        except BaseException as exc:
            self._record_runtime_failure(
                arm,
                exc,
                home=home,
                source_paths=(runtime_plugin_sources or bound_sources).values(),
            )
            if home is not None:
                self._cleanup_coordination(home.coordination_path)
                home.cleanup()
            raise
        assert home is not None
        return home

    def preflight_expected_queries(self, tasks: Iterable[Mapping[str, Any]], arms: Iterable[str]) -> None:
        """Validate each unique locked query through B once before study setup."""
        if not {"B_auto", "C_strict"}.intersection(arms):
            return
        if self.index_path is None:
            raise RuntimeError("B_auto query preflight lacks a locked index")
        home = self._prepare_verified_home("B_auto")
        try:
            if home.codemap_launcher_path is None:
                raise RuntimeError("B_auto query preflight lacks a locked launcher")
            index_sha256 = hashlib.sha256(self.index_path.read_bytes()).hexdigest()
            profile = home.permission_profile or _CODEMAP_PERMISSION_PROFILE
            seen_queries: set[tuple[str, ...]] = set()
            for task in tasks:
                task_id = str(task.get("id", "unknown"))
                expected = task.get("expected_queries")
                if not isinstance(expected, list) or not expected:
                    raise RuntimeError(f"B_auto task {task_id} has no structured expected_queries")
                for query in expected:
                    if not isinstance(query, Mapping) or not isinstance(query.get("cmd"), str):
                        raise RuntimeError(f"B_auto task {task_id} has malformed expected query")
                    arguments = query.get("args", [])
                    if not isinstance(arguments, list) or not all(isinstance(value, str) for value in arguments):
                        raise RuntimeError(f"B_auto task {task_id} has malformed expected query args")
                    normalized = _normalize_locked_query(str(query["cmd"]), arguments)
                    if normalized is None:
                        raise RuntimeError(f"B_auto task {task_id} has malformed expected query")
                    if normalized in seen_queries:
                        continue
                    seen_queries.add(normalized)
                    command = [
                        _CODEX_BIN,
                        "sandbox",
                        "-P",
                        profile,
                        "--include-managed-config",
                        "-C",
                        str(self.repo_path),
                        "--",
                        str(home.codemap_launcher_path),
                        "query",
                        "--compact",
                        *normalized,
                    ]
                    code, stdout, stderr = _invoke_plugin_command(
                        command,
                        home.env,
                        self.command_runner,
                        cwd=self.repo_path,
                    )
                    if code != 0 or not runtime._canonical_query_output({"aggregated_output": stdout}):
                        detail = stderr.strip() or stdout.strip()
                        raise RuntimeError(f"B_auto expected query failed for {task_id}: {detail[:300]}")
                    if hashlib.sha256(self.index_path.read_bytes()).hexdigest() != index_sha256:
                        raise RuntimeError(f"B_auto expected query mutated the locked index for {task_id}")
        finally:
            try:
                self._cleanup_coordination(home.coordination_path)
            finally:
                home.cleanup()

    def create_input_snapshot(
        self,
        run_dir: Path,
        *,
        tasks_path: Path,
        manifest_path: Path,
        invocation_launcher_path: Path | None = None,
        tasks: Iterable[Mapping[str, Any]],
        arms: Iterable[str],
        runner_path: Path | None = None,
        additional_shared_files: Mapping[str, Path] | None = None,
    ) -> dict[str, Any]:
        """Archive launch inputs and verified B/C package bytes before paid calls."""
        snapshot_root = Path(run_dir) / "inputs"
        if snapshot_root.exists():
            raise FileExistsError(snapshot_root)
        # The first admission home is created before the immutable input snapshot
        # exists.  Reserve this non-secret evidence file now so an install failure
        # can persist expected/observed identities before the disposable home is
        # cleaned up.
        self._runtime_evidence_path = Path(run_dir) / "runtime-isolation.jsonl"
        self._runtime_evidence_path.parent.mkdir(parents=True, exist_ok=True)
        self._runtime_evidence_path.touch(exist_ok=False)
        self._runtime_evidence_path.chmod(0o600)
        homes: list[ArmHome] = []
        arm_archives: dict[str, dict[str, Path]] = {}
        arm_files: dict[str, dict[str, Path]] = {}
        try:
            for arm in arms:
                home = self._prepare_verified_home(arm)
                homes.append(home)
                arm_files[arm] = {"config.toml": home.path / "config.toml"}
                if arm == "B_auto":
                    arm_archives[arm] = {"direct-cli": home.path / "direct-cli"}
                elif arm == "C_strict":
                    if home.codemap_plugin_path is None or home.codex_rig_path is None:
                        raise RuntimeError("C_strict package roots are not verified")
                    arm_archives[arm] = {
                        "codemap-py": home.codemap_plugin_path,
                        "codex-rig": home.codex_rig_path,
                    }
                    if home.codemap_context_path is not None:
                        arm_files[arm]["codemap-context.json"] = home.codemap_context_path
            snapshot = _write_input_snapshot(
                snapshot_root,
                manifest_path=manifest_path,
                tasks_path=tasks_path,
                runner_path=runner_path or Path(__file__),
                invocation_launcher_path=invocation_launcher_path,
                index_path=self.index_path,
                auth_source=self.auth_source,
                arm_archives=arm_archives,
                arm_files=arm_files,
                additional_shared_files=additional_shared_files,
            )
            if isinstance(snapshot.get("path"), str):
                runtime_archives = {
                    arm: {
                        **{role: snapshot_root / arm / role for role in archives},
                        **({"marketplace": snapshot_root / arm} if arm == "C_strict" else {}),
                    }
                    for arm, archives in arm_archives.items()
                }
                self._bind_runtime_snapshot(snapshot_root, runtime_archives)
            return snapshot
        finally:
            cleaned_coordination_paths: set[Path] = set()
            for home in homes:
                coordination_path = home.coordination_path
                if coordination_path is not None and coordination_path not in cleaned_coordination_paths:
                    cleaned_coordination_paths.add(coordination_path)
                    self._cleanup_coordination(coordination_path)
                home.cleanup()

    def probe_arm(
        self,
        arm: str,
        *,
        diff_impact_stage: DiffImpactStageAdmission | None = None,
    ) -> dict[str, Any]:
        """Return no-model runtime and plugin-isolation evidence for one arm."""
        if not _is_known_codex_arm(arm):
            raise ValueError(f"unknown benchmark arm {arm!r}")
        if diff_impact_stage is None:
            home = self._prepare_verified_home(arm)
        else:
            home = self._prepare_verified_home(arm, diff_impact_stage=diff_impact_stage)
        try:
            return probe_arm_home(home)
        finally:
            try:
                self._cleanup_coordination(home.coordination_path)
            finally:
                home.cleanup()

    def preflight_diff_impact_stages(self, tasks: Iterable[Mapping[str, Any]], arms: Iterable[str]) -> None:
        """Exercise DI staging, exact admission, and strict restoration without a model."""
        selected_arms = tuple(arms)
        for task in tasks:
            stager = _diff_impact_stager(self.repo_path, task)
            if stager is None:
                continue
            self._admit_runtime("A_plain")
            entered = False
            try:
                stager.__enter__()
                entered = True
                admission = _capture_diff_impact_stage(self.repo_path, task)
                for arm in selected_arms:
                    home = self._prepare_verified_home(arm, diff_impact_stage=admission)
                    try:
                        self._admit_runtime(arm, admission)
                    finally:
                        try:
                            self._cleanup_coordination(home.coordination_path)
                        finally:
                            home.cleanup()
            finally:
                try:
                    if entered:
                        stager.__exit__(*sys.exc_info())
                finally:
                    self._admit_runtime("A_plain")

    def run(
        self,
        task: Mapping[str, Any],
        arm: str,
        *,
        repetition: int = 1,
        diff_impact_stage: DiffImpactStageAdmission | None = None,
    ) -> CodexRun:
        """Execute one task cell within a retry-inclusive coordinate deadline."""
        if not _is_known_codex_arm(arm):
            raise ValueError(f"unknown benchmark arm {arm!r}")
        if repetition < 1:
            raise ValueError("repetition must be a positive integer")
        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task requires a non-empty id")
        is_diff_impact = task.get("type") == "diff_impact"
        if is_diff_impact and diff_impact_stage is None:
            raise ValueError("canonical Codex DI run requires an admitted staged worktree")
        if not is_diff_impact and diff_impact_stage is not None:
            raise ValueError("canonical Codex non-DI run cannot admit a staged worktree")
        prompt = materialize_task_prompt(_raw_task(task))
        envelope = _arm_envelope(arm)
        command_prompt = envelope + "\n\n" + prompt
        metadata = task.get(_PROVENANCE_KEY, {})
        if not isinstance(metadata, Mapping):
            metadata = {}
        run = CodexRun(
            arm,
            task_id,
            str(task.get("type", "unknown")),
            self.model,
            reasoning_effort=self.reasoning_effort,
            repetition=repetition,
            targeted=self.targeted,
            study_mode="targeted" if self.targeted else "confirmatory",
        )
        run.parity_arm = arm
        run.cell_wall_clock_limit_s = self.timeout
        if diff_impact_stage is not None:
            run.stage_evidence = _diff_impact_stage_evidence(self.repo_path, task, diff_impact_stage)
        run.capability_strata = capability_strata(_raw_task(task))
        run.arm_contract_hash = _arm_contract_hash(arm)
        raw_hash = _raw_task_hash(task)
        expected_hash = metadata.get("task_hash", task.get("task_hash"))
        raw_prompt_hash = prompt_hash(_raw_task(task))
        expected_prompt_hash = metadata.get("prompt_hash", task.get("prompt_hash"))
        if metadata and metadata.get("task_hash") != raw_hash:
            raise ValueError(f"task hash mismatch for {task_id!r}")
        if metadata and metadata.get("prompt_hash") != raw_prompt_hash:
            raise ValueError(f"prompt hash mismatch for {task_id!r}")
        # ``load_tasks_with_provenance`` already verifies these values against
        # the locked manifest.  Trusting the precomputed fields here avoids
        # hashing an enriched projection (which is not canonical task bytes).
        run.task_hash = str(expected_hash or raw_hash)
        run.prompt_hash = str(expected_prompt_hash or raw_prompt_hash)
        run.suite_hash = str(metadata.get("suite_hash", task.get("suite_hash", "")))
        run.suite_raw_hash = str(metadata.get("suite_raw_hash", task.get("suite_raw_hash", "")))
        run.experiment_revision = str(metadata.get("experiment_revision", task.get("experiment_revision", "")))
        run.oracle_class = str(metadata.get("oracle_class", task.get("oracle_class", "unknown")))
        run.headline_eligible_v1 = bool(metadata.get("headline_eligible_v1", task.get("headline_eligible_v1", False)))
        run.repo_sha = _repo_sha(self.repo_path)
        run.index_sha = _index_sha(self.index_path)
        explicit_evaluator_id = metadata.get("evaluator_id", task.get("evaluator_id"))
        explicit_evaluator_hash = metadata.get("evaluator_hash", task.get("evaluator_hash"))
        if explicit_evaluator_id and explicit_evaluator_hash:
            run.evaluator_id = str(explicit_evaluator_id)
            run.evaluator_hash = str(explicit_evaluator_hash)
        else:
            run.evaluator_id, run.evaluator_hash = _evaluator_identity(_raw_task(task), self.evaluator)
        run.envelope_hash = hashlib.sha256(envelope.encode()).hexdigest()
        run.scoreable = metadata.get("scoreable", task.get("scoreable", True)) is not False
        home: ArmHome | None = None
        if diff_impact_stage is not None:
            self._admit_runtime(arm, diff_impact_stage)
        if self.transport is None:
            if diff_impact_stage is None:
                home = self._prepare_verified_home(arm)
            else:
                home = self._prepare_verified_home(arm, diff_impact_stage=diff_impact_stage)
        started_at = time.monotonic()
        coordinate_deadline = started_at + self.timeout
        attempt_events: list[list[dict[str, Any]]] = []
        parsed = runtime.CodexParseResult()
        postflight_error = ""
        auth_state_error = ""
        command = self.build_command(command_prompt)
        try:
            for attempt in range(3):
                remaining_s = coordinate_deadline - time.monotonic()
                if remaining_s <= 0:
                    parsed = runtime.CodexParseResult(
                        incomplete=True,
                        error=f"cell wall-clock budget exhausted ({self.timeout}s total)",
                        error_type="cell_timeout",
                    )
                    break
                run.retry_count = attempt
                if self.transport is None:
                    assert home is not None
                    stream = self._subprocess(command, home.env, timeout=remaining_s)
                else:
                    stream = self.transport(command, arm=arm)
                parsed = runtime.parse_codex_jsonl(
                    stream,
                    launcher_path=home.codemap_launcher_path if home is not None else None,
                    skill_path=home.codemap_skill_path if home is not None else None,
                    skill_sha256=home.codemap_skill_sha256 if home is not None else "",
                )
                attempt_events.append(parsed.raw_events)
                if home is not None or diff_impact_stage is not None:
                    try:
                        self._admit_runtime(arm, diff_impact_stage)
                        if home is not None and home.coordination_path is not None:
                            # Liveness and path safety only; gate scratch is permitted output.
                            _assert_coordination_root_idle(home.coordination_path)
                    except ValueError as exc:
                        postflight_error = str(exc)
                        if diff_impact_stage is not None:
                            run.stage_evidence = _diff_impact_stage_evidence(self.repo_path, task, diff_impact_stage)
                        parsed.completed = False
                        parsed.incomplete = True
                        parsed.error = f"runtime contamination: {postflight_error}"
                        parsed.error_type = "runtime_contamination"
                        parsed.retryable = False
                        break
                zero_token_transport_failure = (
                    parsed.input_tokens == 0
                    and parsed.output_tokens == 0
                    and not parsed.output_text.strip()
                    and parsed.retryable
                )
                if not zero_token_transport_failure or attempt == 2:
                    break
        finally:
            run.elapsed_s = time.monotonic() - started_at
            if home is not None:
                if self._auth_state is not None and home.auth_provisioned:
                    try:
                        self._auth_state.refresh_from_home(home.path)
                    except (RuntimeError, ValueError) as exc:
                        auth_state_error = str(exc)
                # Cell-scoped escalation on top of the shared recording policy: a leak
                # here contaminates this cell's measurement, not just the run's hygiene.
                cleanup_error = self._cleanup_coordination(home.coordination_path)
                postflight_error = postflight_error or cleanup_error
                home.cleanup()
        run.thread_id = parsed.thread_id
        run.output_text = parsed.output_text
        run.raw_events = parsed.raw_events
        run.native_attempt_events = attempt_events
        run.native_item_counts = parsed.item_counts
        run.tool_elapsed_s = parsed.tool_elapsed_s
        run.tool_result_tokens = parsed.tool_result_tokens
        for field_name in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "command_calls",
            "codemap_observed_calls",
            "codemap_calls",
            "codemap_successful_calls",
            "codemap_compact_successful_calls",
            "codemap_direct_calls",
            "codemap_direct_successful_calls",
            "codemap_direct_compact_successful_calls",
            "codemap_skill_calls",
            "codemap_skill_successful_calls",
            "codemap_skill_compact_successful_calls",
            "skill_delivery_observed",
            "codemap_errors",
            "fallback_calls",
            "malformed_usage",
            "successful_query_arguments",
        ):
            setattr(run, field_name, getattr(parsed, field_name))
        run.success = parsed.success
        run.incomplete = parsed.incomplete
        run.error = parsed.error
        run.error_type = parsed.error_type
        run.compliance = _arm_compliance(arm, run)
        run.locked_query_conformance = _locked_query_conformance(task, arm, run)
        locked_query_fitness = _locked_query_fitness(task, arm, run)
        if locked_query_fitness is not None:
            run.locked_query_fitness = locked_query_fitness.overall
            run.locked_query_endpoint_fitness = locked_query_fitness.endpoint
            run.locked_query_target_fitness = locked_query_fitness.target
            run.locked_query_option_fitness = locked_query_fitness.options
        if arm == "B_auto" and run.compliance:
            run.codemap_delivery = "direct_cli"
        elif arm == "C_strict" and run.compliance:
            run.codemap_delivery = "installed_skill"
        run.contaminated = bool(postflight_error) or (arm == "A_plain" and run.codemap_observed_calls > 0)
        run.treatment_adherence = treatment_adherence(
            arm,
            codemap_use_compliance=run.compliance,
            contaminated=run.contaminated,
        )
        run.token_accounting_inconsistent = token_accounting_inconsistent(run.input_tokens, run.cached_input_tokens)
        run.fresh_input_tokens = fresh_input_tokens(run.input_tokens, run.cached_input_tokens)
        if postflight_error:
            run.incomplete = True
            run.error = f"runtime contamination: {postflight_error}"
            run.error_type = "runtime_contamination"
            run.success = False
        if auth_state_error:
            run.incomplete = True
            run.error = "run auth state could not be refreshed"
            run.error_type = "authentication_state_failed"
            run.success = False
        if run.contaminated and not run.error:
            run.error = "contaminated"
            run.success = False
        if run.scoreable and not run.incomplete:
            evaluation = self.evaluator(_raw_task(task), run.output_text)
            run.quality_score = evaluation.quality_score
            run.quality_components = evaluation.components
            run.correct = evaluation.correct
            run.extraction_failed = evaluation.extraction_failed
        return run

    def _subprocess(
        self,
        command: list[str],
        env: Mapping[str, str],
        *,
        timeout: float | None = None,
        working_directory: Path | None = None,
    ) -> str:
        """Run one Codex attempt within the coordinate's remaining budget.

        The child runs in its own process group so a timeout kills the descendants too. Killing only the direct child
        left grandchildren alive, still consuming paid budget outside the measured window.
        """
        attempt_timeout = self.timeout if timeout is None else timeout
        try:
            process = subprocess.Popen(  # noqa: S603 - benchmark-owned argv, no shell
                command,
                cwd=working_directory or self.repo_path,
                env=dict(env),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                **NEW_PROCESS_GROUP,
            )
        except OSError as exc:
            return json.dumps(
                {
                    "type": "error",
                    "error": f"Codex launch failed: {exc.strerror or type(exc).__name__}",
                    "error_type": "launch_os_error",
                }
            )
        try:
            stdout, stderr = process.communicate(timeout=attempt_timeout)
        except subprocess.TimeoutExpired:
            terminate_process_group(process)
            # Whatever the agent streamed before the kill is real evidence: the usage
            # events it already emitted are what it already billed. Discarding them
            # persisted a timed-out cell as 0 tokens despite genuine spend.
            stdout, stderr = process.communicate()
            terminal = json.dumps(
                {
                    "type": "error",
                    "error": f"timeout ({attempt_timeout}s)",
                    "error_type": "timeout",
                }
            )
            return ((stdout or "") + "\n" + terminal).lstrip()
        if process.returncode != 0:
            terminal = json.dumps(
                {
                    "type": "error",
                    "error": (stderr or "").strip()[:300] or f"non-zero exit {process.returncode}",
                    "error_type": "non_zero_exit",
                }
            )
            return ((stdout or "") + "\n" + terminal).lstrip()
        return stdout or ""


def _arm_envelope(arm: str) -> str:
    """Return arm-only tool availability instructions."""
    if arm == "A_plain":
        return "Codemap is absent and inaccessible. Use ordinary provider tools only; do not invoke Codemap."
    if arm == "B_auto":
        return (
            "Codemap is available only through the direct CLI. Whether to use it is your choice, and "
            "answering without it is valid. A query counts only as a dedicated native "
            'command item of the exact form "$CODEMAP_BIN" query --compact <subcommand> <arguments>. '
            "It must exit 0 and emit one JSON document whose index.query_complete and index.compact are true. "
            "The credited item contains only that query: do not prefix, assign, wrap, or combine it with shell work. "
            "Do not use batch; repeat the dedicated command for each independently required fact. "
            'If discovery is needed, use "$CODEMAP_BIN" query --help; help is not a subcommand. '
            "Additional reads and shell work are allowed only as separate native items and are ignored for credit."
        )
    if arm == "C_strict":
        return (
            "Codemap's installed $codemap-py:query-code Skill is available through a runner-owned immutable binding. "
            "Use its smallest complete query guidance, then complete a dedicated native command item of the exact form "
            '"$CODEMAP_BIN" query --compact <subcommand> <arguments> with '
            "exit 0 and one JSON document whose index.query_complete and index.compact are true. "
            "The credited item contains only that query: do not prefix, assign, wrap, or combine it with shell work. "
            "Do not use batch; repeat the dedicated command for each independently required fact. "
            'If discovery is needed, use "$CODEMAP_BIN" query --help; help is not a subcommand. '
            "Additional reads and shell work are allowed only as separate native items and are ignored for credit."
        )
    raise ValueError(f"unknown benchmark arm {arm!r}")


def arm_envelope(arm: str) -> str:
    """Return arm-only tool availability instructions (declared stage surface)."""
    return _arm_envelope(arm)


def _append_run(output_path: Path, run: CodexRun, *, execution_index: int) -> None:
    """Append one completed cell so later failures cannot erase smoke evidence."""
    if execution_index < 0:
        raise ValueError("execution_index must be non-negative")
    run.execution_index = execution_index
    with output_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(run), sort_keys=True) + "\n")


def _canonical_telemetry_path(output_path: Path) -> Path:
    """Return the derived canonical-order sidecar path for raw telemetry."""
    return output_path.with_name("telemetry-canonical.jsonl")


def _write_canonical_telemetry(
    output_path: Path,
    canonical_path: Path,
    *,
    task_order: tuple[str, ...],
) -> str:
    """Atomically publish a canonical sidecar without rewriting raw execution evidence."""
    rows = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines() if line]
    canonical_rows = canonical_result_rows(
        rows,
        task_order=task_order,
        arm_order=CODEX_STRUCTURAL_ARMS,
    )
    serialized = "".join(json.dumps(row, sort_keys=True) + "\n" for row in canonical_rows).encode("utf-8")
    with tempfile.NamedTemporaryFile(
        dir=canonical_path.parent, prefix=f".{canonical_path.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(serialized)
    try:
        os.replace(temporary, canonical_path)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(serialized).hexdigest()


def _utc_now() -> str:
    """Return one stable UTC timestamp for run-level evidence."""
    return datetime.now(timezone.utc).isoformat()


def _write_run_metadata(metadata_path: Path, payload: Mapping[str, Any]) -> None:
    """Atomically persist run provenance so interruptions retain the last completed cell."""
    serialized = (json.dumps(dict(payload), indent=2, sort_keys=True) + "\n").encode("utf-8")
    with tempfile.NamedTemporaryFile(
        dir=metadata_path.parent, prefix=f".{metadata_path.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(serialized)
    try:
        os.replace(temporary, metadata_path)
    finally:
        temporary.unlink(missing_ok=True)


def _close_runner(runner: Any) -> None:
    """Close optional private runner state without constraining fixture runners."""
    close = getattr(runner, "close", None)
    if callable(close):
        close()


def _regular_file_within(path: Path, root: Path, *, description: str) -> Path:
    """Resolve one immutable rescore input while rejecting links and scope escapes."""
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"offline rescore {description} is unavailable") from exc
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or not resolved.is_relative_to(root):
        raise ValueError(f"offline rescore {description} escaped the run directory")
    return resolved


def _load_frozen_rescore_inputs(
    run_dir: Path,
) -> tuple[dict[str, Any], Path, list[dict[str, Any]], Path, Path, str]:
    """Load all hash-verified inputs needed to replay one completed run."""
    root = run_dir.resolve(strict=True)
    metadata_candidates = sorted(root.glob("*metadata.json"))
    if len(metadata_candidates) != 1:
        raise ValueError("offline rescore requires exactly one run metadata JSON file")
    metadata_path = _regular_file_within(metadata_candidates[0], root, description="metadata")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("offline rescore metadata is not valid JSON") from exc
    if not isinstance(metadata, dict) or metadata.get("schema_version") not in {
        "codex-structural-run-metadata-v1",
        "codex-structural-run-metadata-v2",
    }:
        raise ValueError("offline rescore metadata schema is unsupported")
    if metadata.get("status") != "completed":
        raise ValueError("offline rescore requires completed run metadata")
    artifacts = metadata.get("artifacts")
    inputs = metadata.get("inputs")
    if not isinstance(artifacts, Mapping) or not isinstance(inputs, Mapping):
        raise ValueError("offline rescore metadata lacks artifact or input provenance")
    telemetry_raw = artifacts.get("telemetry_jsonl")
    snapshot = inputs.get("snapshot")
    if not isinstance(telemetry_raw, str) or not isinstance(snapshot, Mapping):
        raise ValueError("offline rescore metadata lacks frozen telemetry or snapshot")
    telemetry_recorded = Path(telemetry_raw)
    if telemetry_recorded.name != "telemetry.jsonl":
        raise ValueError("offline rescore telemetry provenance has an unexpected name")
    telemetry_path = _regular_file_within(root / telemetry_recorded.name, root, description="telemetry")
    telemetry_bytes = telemetry_path.read_bytes()
    expected_telemetry_hash = artifacts.get("telemetry_sha256")
    if (
        not isinstance(expected_telemetry_hash, str)
        or hashlib.sha256(telemetry_bytes).hexdigest() != expected_telemetry_hash
    ):
        raise ValueError("offline rescore telemetry hash mismatch")
    snapshot_raw = snapshot.get("path")
    snapshot_hash = snapshot.get("sha256")
    if not isinstance(snapshot_raw, str) or not isinstance(snapshot_hash, str):
        raise ValueError("offline rescore snapshot provenance is incomplete")
    snapshot_recorded = Path(snapshot_raw)
    if snapshot_recorded.name != "input-snapshot.json":
        raise ValueError("offline rescore snapshot provenance has an unexpected name")
    snapshot_path = _regular_file_within(
        root / "inputs" / snapshot_recorded.name,
        root,
        description="input snapshot",
    )
    snapshot_bytes = snapshot_path.read_bytes()
    if hashlib.sha256(snapshot_bytes).hexdigest() != snapshot_hash:
        raise ValueError("offline rescore input snapshot hash mismatch")
    try:
        snapshot_payload = json.loads(snapshot_bytes)
    except json.JSONDecodeError as exc:
        raise ValueError("offline rescore input snapshot is not valid JSON") from exc
    if (
        not isinstance(snapshot_payload, Mapping)
        or snapshot_payload.get("schema_version") != "codex-structural-input-snapshot-v1"
    ):
        raise ValueError("offline rescore input snapshot schema is unsupported")
    files = snapshot_payload.get("files")
    if not isinstance(files, list):
        raise ValueError("offline rescore input snapshot lacks files")
    task_entry = next(
        (entry for entry in files if isinstance(entry, Mapping) and entry.get("role") == "task_suite"), None
    )
    if task_entry is None:
        raise ValueError("offline rescore input snapshot lacks frozen task suite")
    archived_path = task_entry.get("archived_path")
    expected_task_hash = task_entry.get("sha256")
    if not isinstance(archived_path, str) or not isinstance(expected_task_hash, str):
        raise ValueError("offline rescore frozen task suite provenance is incomplete")
    tasks_path = _regular_file_within(root / "inputs" / archived_path, root, description="frozen task suite")
    if hashlib.sha256(tasks_path.read_bytes()).hexdigest() != expected_task_hash:
        raise ValueError("offline rescore frozen task suite hash mismatch")
    treatments = metadata.get("treatments")
    artifact_hashes = treatments.get("artifact_sha256") if isinstance(treatments, Mapping) else None
    skill_hash = artifact_hashes.get("codemap_query_skill") if isinstance(artifact_hashes, Mapping) else None
    if not isinstance(skill_hash, str) or not skill_hash:
        raise ValueError("offline rescore metadata lacks the frozen Codemap Skill hash")
    skill_entries = [
        entry
        for entry in files
        if isinstance(entry, Mapping)
        and entry.get("role") == "C_strict:codemap-py"
        and entry.get("sha256") == skill_hash
        and str(entry.get("archived_path", "")).endswith("/codex-skills/query-code/SKILL.md")
    ]
    if len(skill_entries) != 1:
        raise ValueError("offline rescore snapshot lacks one exact frozen Codemap Skill")
    skill_archived_path = skill_entries[0].get("archived_path")
    if not isinstance(skill_archived_path, str):
        raise ValueError("offline rescore frozen Codemap Skill provenance is incomplete")
    skill_path = _regular_file_within(
        root / "inputs" / skill_archived_path,
        root,
        description="frozen Codemap Skill",
    )
    if hashlib.sha256(skill_path.read_bytes()).hexdigest() != skill_hash:
        raise ValueError("offline rescore frozen Codemap Skill hash mismatch")
    return metadata, telemetry_path, load_task_suite(tasks_path), metadata_path, skill_path, skill_hash


def rescore_results(run_dir: Path) -> Path:
    """Replay frozen raw telemetry and current evaluators into an immutable offline artifact.

    The function accepts only a completed run directory. It never invokes a provider, reads credentials, or rewrites raw
    telemetry, canonical telemetry, metadata, or frozen input snapshots.
    """
    root = Path(run_dir).resolve(strict=True)
    metadata, telemetry_path, tasks, metadata_path, skill_path, skill_sha256 = _load_frozen_rescore_inputs(root)
    task_by_id = {str(task["id"]): task for task in tasks}
    execution = metadata.get("execution")
    if not isinstance(execution, Mapping):
        raise ValueError("offline rescore metadata lacks execution scope")
    selected_ids = execution.get("selected_task_ids")
    coordinates = execution.get("coordinates")
    if not isinstance(selected_ids, list) or not all(isinstance(task_id, str) for task_id in selected_ids):
        raise ValueError("offline rescore selected task scope is invalid")
    if set(selected_ids) - task_by_id.keys() or not isinstance(coordinates, list):
        raise ValueError("offline rescore task scope disagrees with frozen suite")
    allowed_coordinates: set[tuple[str, int, str]] = set()
    for coordinate in coordinates:
        if not isinstance(coordinate, Mapping):
            raise ValueError("offline rescore coordinate scope is invalid")
        task_id = coordinate.get("task_id")
        repetition = coordinate.get("repetition")
        arm = coordinate.get("arm")
        if (
            not isinstance(task_id, str)
            or task_id not in selected_ids
            or isinstance(repetition, bool)
            or not isinstance(repetition, int)
            or repetition < 1
            or arm not in CODEX_STRUCTURAL_ARMS
        ):
            raise ValueError("offline rescore coordinate scope is invalid")
        allowed_coordinates.add((task_id, repetition, arm))
    if len(allowed_coordinates) != len(coordinates):
        raise ValueError("offline rescore coordinate scope is invalid")
    telemetry_bytes = telemetry_path.read_bytes()
    source = {
        "metadata_sha256": hashlib.sha256(metadata_path.read_bytes()).hexdigest(),
        "telemetry_sha256": hashlib.sha256(telemetry_bytes).hexdigest(),
        "frozen_suite_semantic_sha256": semantic_suite_hash(tasks),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    rows: list[dict[str, Any]] = []
    seen_coordinates: set[tuple[Any, Any, Any]] = set()
    for line in telemetry_bytes.decode("utf-8").splitlines():
        try:
            raw_row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("offline rescore telemetry is not valid JSONL") from exc
        if not isinstance(raw_row, Mapping):
            raise ValueError("offline rescore telemetry row is not an object")
        task_id = raw_row.get("task_id")
        arm = raw_row.get("arm")
        repetition = raw_row.get("repetition", 1)
        coordinate = (task_id, repetition, arm)
        if (
            not isinstance(task_id, str)
            or task_id not in task_by_id
            or coordinate not in allowed_coordinates
            or coordinate in seen_coordinates
        ):
            raise ValueError("offline rescore telemetry row is outside frozen execution scope")
        raw_events = raw_row.get("raw_events")
        if not isinstance(raw_events, list) or not all(isinstance(event, Mapping) for event in raw_events):
            raise ValueError("offline rescore telemetry row lacks replayable raw events")
        seen_coordinates.add(coordinate)
        parsed = runtime.parse_codex_jsonl(
            (json.dumps(event, sort_keys=True) for event in raw_events),
            skill_path=skill_path if arm == "C_strict" else None,
            skill_sha256=skill_sha256 if arm == "C_strict" else "",
        )
        evaluation = _default_evaluator(task_by_id[task_id], parsed.output_text)
        compliance = _arm_compliance(str(arm), parsed)
        contaminated = arm == "A_plain" and parsed.codemap_observed_calls > 0
        row = {
            "task_id": task_id,
            "repetition": repetition,
            "arm": arm,
            "output_text": parsed.output_text,
            "success": parsed.success,
            "incomplete": parsed.incomplete,
            "error": parsed.error,
            "error_type": parsed.error_type,
            "quality_score": evaluation.quality_score if evaluation.scored else None,
            "quality_components": evaluation.components,
            "correct": evaluation.correct,
            "extraction_failed": evaluation.extraction_failed,
            "compliance": compliance,
            "locked_query_conformance": _locked_query_conformance(task_by_id[task_id], str(arm), parsed),
            "contaminated": contaminated,
            "treatment_adherence": treatment_adherence(
                str(arm),
                codemap_use_compliance=compliance,
                contaminated=contaminated,
            ),
            "codemap_calls": parsed.codemap_calls,
            "codemap_observed_calls": parsed.codemap_observed_calls,
            "codemap_successful_calls": parsed.codemap_successful_calls,
            "codemap_direct_compact_successful_calls": parsed.codemap_direct_compact_successful_calls,
            "codemap_skill_compact_successful_calls": parsed.codemap_skill_compact_successful_calls,
            "skill_delivery_observed": parsed.skill_delivery_observed,
            "successful_query_arguments": parsed.successful_query_arguments,
            "raw_events_sha256": hashlib.sha256(
                json.dumps(raw_events, separators=(",", ":"), sort_keys=True).encode("utf-8")
            ).hexdigest(),
        }
        locked_query_fitness = _locked_query_fitness(task_by_id[task_id], str(arm), parsed)
        row.update(
            {
                "locked_query_fitness": locked_query_fitness.overall if locked_query_fitness is not None else None,
                "locked_query_endpoint_fitness": (
                    locked_query_fitness.endpoint if locked_query_fitness is not None else None
                ),
                "locked_query_target_fitness": locked_query_fitness.target
                if locked_query_fitness is not None
                else None,
                "locked_query_option_fitness": locked_query_fitness.options
                if locked_query_fitness is not None
                else None,
            }
        )
        rows.append(row)
    if seen_coordinates != allowed_coordinates:
        raise ValueError("offline rescore telemetry is incomplete for the frozen execution scope")
    payload = {
        "schema_version": "codex-structural-offline-rescore-v2",
        "source": source,
        "rows": rows,
    }
    serialized_without_hash = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    payload["derived_sha256"] = hashlib.sha256(serialized_without_hash).hexdigest()
    serialized = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    artifact_path = root / (f"offline-rescore-v2-{source['telemetry_sha256'][:16]}-{source['runner_sha256'][:16]}.json")
    if artifact_path.exists():
        if artifact_path.read_bytes() != serialized:
            raise ValueError("offline rescore artifact already exists with different derived content")
        return artifact_path
    artifact_path.write_bytes(serialized)
    return artifact_path


def _initial_run_metadata(
    *,
    manifest_path: Path,
    repo_path: Path,
    index_path: Path | None,
    output_path: Path,
    metadata_path: Path,
    model: str,
    reasoning_effort: str,
    repetitions: int,
    task_arms: Mapping[tuple[str, int], tuple[str, ...]],
    cell_wall_clock_seconds: float,
    auth_provisioned: bool,
    input_snapshot: Mapping[str, Any] | None = None,
    study_mode: str = "confirmatory",
    targeted_scope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build complete non-secret provenance for one paid structural run."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    scope = dict(targeted_scope) if targeted_scope is not None else None
    coordinates = [
        {"task_id": task_id, "repetition": repetition, "arm": arm}
        for (task_id, repetition), arms in task_arms.items()
        for arm in arms
    ]
    return {
        "schema_version": "codex-structural-run-metadata-v2",
        "status": "running",
        "started_at": _utc_now(),
        "completed_at": None,
        "persisted_cells": 0,
        "cell_outcomes": {
            "successful": 0,
            "unsuccessful": 0,
            "unscoreable": 0,
            "incomplete": 0,
            "extraction_failed": 0,
            "contaminated": 0,
            "compliance_failed": 0,
            "locked_query_nonconforming": 0,
            "targeted": 0,
            "token_accounting_inconsistent": 0,
        },
        "last_persisted_coordinate": None,
        "error": None,
        "auth_provisioned": auth_provisioned,
        "auth_source_recorded": False,
        "manifest": {
            "path": str(manifest_path.resolve()),
            "sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "experiment_id": manifest["experiment_id"],
            "experiment_revision": manifest["experiment_revision"],
        },
        "execution": {
            "study_mode": study_mode,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "repetitions": repetitions,
            "arms": list(CODEX_STRUCTURAL_ARMS),
            "planned_cells": len(coordinates),
            "selected_task_ids": list(dict.fromkeys(task_id for task_id, _ in task_arms)),
            "targeted_scope": scope,
            "targeted_scope_sha256": scope["scope_sha256"] if scope is not None else None,
            "coordinates": coordinates,
            "cell_wall_clock_seconds": cell_wall_clock_seconds,
            "python": sys.version,
            "codex_cli": {
                "reviewed": manifest["codex_cli"],
                "observed_version": os.environ.get("CODEX_CLI_OBSERVED_VERSION"),
            },
        },
        "inputs": {
            "target_path": str(repo_path.resolve()),
            "target": manifest["target_source"],
            "index_path": str(index_path.resolve()) if index_path is not None else None,
            "index_sha256": _index_sha(index_path),
            "index": manifest["index"],
            "suite_integrity": manifest["suite_integrity"],
            "snapshot": dict(input_snapshot) if input_snapshot is not None else None,
        },
        "treatments": {
            "arms": manifest["arms"],
            "package_roster": manifest["package_roster"],
            "codemap_candidate": manifest["codemap_candidate"],
            "codex_rig_candidate": manifest["codex_rig_candidate"],
            "artifact_sha256": manifest["artifact_sha256"],
            "permission_profiles": manifest["codex_permission_profiles"],
            "telemetry_admission": manifest["telemetry_admission"],
        },
        "artifacts": {
            "telemetry_jsonl": str(output_path.resolve()),
            "telemetry_canonical_jsonl": str(_canonical_telemetry_path(output_path).resolve()),
            "canonical_telemetry_status": "not_written",
            "canonical_telemetry_pooling_eligible": False,
            "canonical_telemetry_pooling_ineligibility_reasons": [],
            "run_metadata": str(metadata_path.resolve()),
        },
    }


def main(
    *,
    repo_path: Path,
    model: str,
    reasoning_effort: str = PARITY_CODEX_REASONING_EFFORT,
    tasks_path: Path,
    manifest_path: Path = PARITY_MANIFEST_PATH,
    index_path: Path | None = None,
    marketplace_root: Path | None = None,
    codemap_bin: Path | None = None,
    auth_source: Path | None = None,
    invocation_launcher_path: Path | None = None,
    output_path: Path | None = None,
    metadata_path: Path | None = None,
    task_ids: list[str] | None = None,
    task_selectors: str | Sequence[str] | None = None,
    scope_sha256: str | None = None,
    repetitions: int | None = None,
    arm: str = "all",
    dry_run: bool = False,
    show_legend: bool = True,
    index_relocation_path: Path | None = None,
) -> None:
    """Validate, plan, and execute cells under the manifest's per-cell timeout."""
    manifest_path = Path(manifest_path)
    _validate_codex_stratum(model, reasoning_effort, manifest_path)
    try:
        active_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        confirmatory_repetitions = active_manifest["preregistered_cells"]["confirmatory_repetitions"]
        cell_wall_clock_seconds = active_manifest["execution_controls"]["parity_timeout_seconds"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("provider-parity execution controls are unavailable or malformed") from exc
    if type(confirmatory_repetitions) is not int or confirmatory_repetitions < 1:
        raise ValueError("confirmatory repetitions must be a positive integer")
    if type(cell_wall_clock_seconds) not in {int, float} or cell_wall_clock_seconds <= 0:
        raise ValueError("per-cell timeout must be positive")
    if task_ids and task_selectors is not None:
        raise ValueError("--task-id and --tasks cannot be combined")
    targeted_scope = (
        _resolve_structural_task_selection(Path(manifest_path), task_selectors) if task_selectors is not None else None
    )
    if targeted_scope is not None:
        task_ids = list(targeted_scope["task_ids"])
        repetitions = targeted_scope["repetitions"] if repetitions is None else repetitions
    repetitions = confirmatory_repetitions if repetitions is None else repetitions
    if repetitions < 1:
        raise ValueError("--repetitions must be a positive integer")
    if targeted_scope is not None:
        _validate_targeted_scope_request(
            targeted_scope,
            repetitions=repetitions,
            arm=arm,
            scope_sha256=scope_sha256,
            dry_run=dry_run,
        )
    _validate_unscoped_paid_task_ids(
        manifest_path,
        task_ids,
        targeted=targeted_scope is not None,
        dry_run=dry_run,
    )
    tasks = load_tasks_with_provenance(tasks_path, manifest_path)
    if not tasks:
        raise ValueError("locked task suite must contain at least one task")
    provenance = tasks[0].get(_PROVENANCE_KEY, {})
    experiment_revision = (
        str(provenance.get("experiment_revision", "")) if isinstance(provenance, Mapping) else ""
    ) or _read_manifest_revision(manifest_path)
    if task_ids:
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("--task-id values must be unique")
        missing = set(task_ids) - {task["id"] for task in tasks}
        if missing:
            raise ValueError(f"unknown locked task IDs: {sorted(missing)}")
        selected_ids = set(task_ids)
        tasks = [task for task in tasks if task["id"] in selected_ids]
    explicit_selection = targeted_scope is not None
    for task in tasks:
        _validate_diff_impact_stage(Path(repo_path), task)
    if not dry_run:
        if output_path is None:
            raise ValueError("non-dry Codex runs require --output-path")
        metadata_path = metadata_path or output_path.with_name(f"{output_path.stem}-metadata.json")
        if output_path.exists():
            raise FileExistsError(output_path)
        if metadata_path.exists():
            raise FileExistsError(metadata_path)
        canonical_path = _canonical_telemetry_path(output_path)
        if canonical_path.exists():
            raise FileExistsError(canonical_path)
        _validate_execution_manifest(manifest_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("x", encoding="utf-8"):
            pass
    invocation_launcher_sha256: str | None = None
    if invocation_launcher_path is not None:
        invocation_launcher_path = Path(invocation_launcher_path)
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            invocation_launcher_sha256 = str(manifest["artifact_sha256"]["run_all"])
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("paid manifest lacks the invocation-launcher lock") from exc
        _validate_invocation_launcher(invocation_launcher_path, invocation_launcher_sha256)
    runner = CodexRunner(
        model,
        repo_path,
        reasoning_effort=reasoning_effort,
        index_path=index_path,
        marketplace_root=marketplace_root,
        codemap_bin=codemap_bin,
        manifest_path=manifest_path,
        index_relocation=load_index_relocation(index_relocation_path),
        auth_source=auth_source,
        targeted=explicit_selection,
        timeout=(
            float(targeted_scope["coordinate_timeout_seconds"])
            if targeted_scope is not None
            else float(cell_wall_clock_seconds)
        ),
    )
    if show_legend:
        runtime.print_structural_legend()
    if dry_run:
        try:
            for selected in ARMS if arm == "all" else (arm,):
                evidence = runner.probe_arm(selected)
                codemap_python = evidence.get("codemap_python") or "absent"
                runtime.print_plan_row(
                    runtime.format_probe_row(
                        selected,
                        {
                            "codemap": bool(evidence["codemap_available"]),
                            "use": runtime.probe_use(selected),
                            "codemap_python": codemap_python,
                        },
                    )
                )
            selected_arms = ARMS if arm == "all" else (arm,)
            preflight = getattr(runner, "preflight_expected_queries", None)
            if callable(preflight):
                preflight(tasks, selected_arms)
            staged_preflight = getattr(runner, "preflight_diff_impact_stages", None)
            if callable(staged_preflight):
                staged_preflight(tasks, selected_arms)
        finally:
            _close_runner(runner)
    print(f"CONTROL\tcell_wall_clock_seconds={runner.timeout:g}")
    if not dry_run:
        assert output_path is not None
        assert metadata_path is not None
        print(runtime.presentation.format_artifact_block(telemetry=output_path, metadata=metadata_path))
    task_arms = {
        (task["id"], repetition): (
            _manifest_arm_order(
                experiment_revision,
                model,
                task["id"],
                repetition,
                reasoning_effort,
                task_ordinal=(
                    int(task[_PROVENANCE_KEY]["task_ordinal"])
                    if isinstance(task.get(_PROVENANCE_KEY), Mapping)
                    and type(task[_PROVENANCE_KEY].get("task_ordinal")) is int
                    else None
                ),
            )
            if arm == "all"
            else (arm,)
        )
        for task in tasks
        for repetition in range(1, repetitions + 1)
    }
    if dry_run:
        for task in tasks:
            for repetition in range(1, repetitions + 1):
                for selected in task_arms[(task["id"], repetition)]:
                    runtime.print_plan_row(runtime.format_plan_row(task["id"], repetition, selected))
        return
    assert output_path is not None
    assert metadata_path is not None
    snapshot_builder = getattr(runner, "create_input_snapshot", None)
    preflight = getattr(runner, "preflight_expected_queries", None)
    try:
        if callable(preflight):
            preflight(tasks, tuple(dict.fromkeys(selected for arms in task_arms.values() for selected in arms)))
        input_snapshot = (
            snapshot_builder(
                output_path.parent,
                tasks_path=tasks_path,
                manifest_path=manifest_path,
                invocation_launcher_path=invocation_launcher_path,
                tasks=tasks,
                arms=tuple(dict.fromkeys(selected for arms in task_arms.values() for selected in arms)),
            )
            if callable(snapshot_builder)
            else None
        )
        metadata = _initial_run_metadata(
            manifest_path=manifest_path,
            repo_path=repo_path,
            index_path=index_path,
            output_path=output_path,
            metadata_path=metadata_path,
            model=model,
            reasoning_effort=reasoning_effort,
            repetitions=repetitions,
            task_arms=task_arms,
            cell_wall_clock_seconds=runner.timeout,
            auth_provisioned=auth_source is not None,
            input_snapshot=input_snapshot,
            study_mode="targeted" if explicit_selection else "confirmatory",
            targeted_scope=targeted_scope,
        )
        canonical_path = _canonical_telemetry_path(output_path)
        task_order = tuple(str(task["id"]) for task in tasks)
        _write_run_metadata(metadata_path, metadata)
    except BaseException:
        _close_runner(runner)
        raise
    planned_cells = sum(len(arms) for arms in task_arms.values())
    printed_cells = 0
    pending_result_rows: list[tuple[str, str]] = []
    active_stager: Any | None = None
    active_diff_impact_stage: DiffImpactStageAdmission | None = None
    consecutive_infrastructure_signature = ""
    consecutive_infrastructure_failures = 0
    try:
        for task in tasks:
            for repetition in range(1, repetitions + 1):
                active_stager = _diff_impact_stager(Path(repo_path), task)
                if active_stager is not None:
                    runner._admit_runtime("A_plain")
                    try:
                        active_stager.__enter__()
                        active_diff_impact_stage = _capture_diff_impact_stage(Path(repo_path), task)
                    except BaseException:
                        try:
                            active_stager.__exit__(*sys.exc_info())
                        finally:
                            active_stager = None
                            runner._admit_runtime("A_plain")
                        raise
                pending_result_rows = []
                for selected in task_arms[(task["id"], repetition)]:
                    if invocation_launcher_path is not None and invocation_launcher_sha256 is not None:
                        _validate_invocation_launcher(invocation_launcher_path, invocation_launcher_sha256)
                    run_kwargs: dict[str, Any] = {"repetition": repetition}
                    if active_diff_impact_stage is not None:
                        run_kwargs["diff_impact_stage"] = active_diff_impact_stage
                    run = runner.run(task, selected, **run_kwargs)
                    if invocation_launcher_path is not None and invocation_launcher_sha256 is not None:
                        _validate_invocation_launcher(invocation_launcher_path, invocation_launcher_sha256)
                    _append_run(output_path, run, execution_index=int(metadata["persisted_cells"]))
                    metadata["persisted_cells"] = int(metadata["persisted_cells"]) + 1
                    outcomes = metadata["cell_outcomes"]
                    outcomes["successful" if run.success else "unsuccessful"] += 1
                    for outcome, failed in (
                        ("unscoreable", not run.scoreable),
                        ("incomplete", run.incomplete),
                        ("extraction_failed", run.extraction_failed),
                        ("contaminated", run.contaminated),
                        (
                            "compliance_failed",
                            run.arm in {"B_auto", "C_strict"} and not run.compliance,
                        ),
                        (
                            "locked_query_nonconforming",
                            run.arm in {"B_auto", "C_strict"} and run.locked_query_conformance is False,
                        ),
                        ("targeted", run.targeted),
                        ("token_accounting_inconsistent", run.token_accounting_inconsistent),
                    ):
                        if failed:
                            outcomes[outcome] += 1
                    pooling_reasons = metadata["artifacts"]["canonical_telemetry_pooling_ineligibility_reasons"]
                    for reason in _pooling_ineligibility_reasons(run):
                        if reason not in pooling_reasons:
                            pooling_reasons.append(reason)
                    metadata["last_persisted_coordinate"] = {
                        "task_id": task["id"],
                        "repetition": repetition,
                        "arm": selected,
                    }
                    metadata["artifacts"]["canonical_telemetry_sha256"] = _write_canonical_telemetry(
                        output_path,
                        canonical_path,
                        task_order=task_order,
                    )
                    metadata["artifacts"]["canonical_telemetry_status"] = "partial"
                    _write_run_metadata(metadata_path, metadata)
                    status = "✓" if run.success else "✗"
                    quality = f"{run.quality_score:.3f}" if run.quality_score is not None else "?"
                    pending_result_rows.append(
                        (
                            selected,
                            runtime.format_structural_result_row(
                                status=status,
                                task_id=task["id"],
                                repetition=repetition,
                                arm=selected,
                                input_tokens=run.input_tokens,
                                cached_input_tokens=run.cached_input_tokens,
                                fresh_tokens=run.fresh_input_tokens,
                                output_tokens=run.output_tokens,
                                elapsed_s=run.elapsed_s,
                                quality=quality,
                                adherence=run.treatment_adherence,
                                codemap_used=run.codemap_observed_calls > 0,
                                query_conformance=run.locked_query_conformance,
                                headline_eligible=run.headline_eligible_v1,
                            ),
                        )
                    )
                    infrastructure_signature = _infrastructure_failure_signature(run)
                    if infrastructure_signature is None:
                        consecutive_infrastructure_signature = ""
                        consecutive_infrastructure_failures = 0
                    elif run.error_type == "authentication_failed":
                        raise RuntimeError(
                            "infrastructure failure: authentication failed; reauthenticate before resuming the benchmark"
                        )
                    elif infrastructure_signature == consecutive_infrastructure_signature:
                        consecutive_infrastructure_failures += 1
                    else:
                        consecutive_infrastructure_signature = infrastructure_signature
                        consecutive_infrastructure_failures = 1
                    if consecutive_infrastructure_failures >= 3:
                        raise RuntimeError(
                            "infrastructure failure recurred three times before a model response; "
                            "preserved partial artifacts and stopped scheduling"
                        )
                printed_cells = _print_result_block(
                    pending_result_rows, printed_cells=printed_cells, planned_cells=planned_cells
                )
                pending_result_rows = []
                if active_stager is not None:
                    try:
                        active_stager.__exit__(None, None, None)
                    finally:
                        active_stager = None
                        active_diff_impact_stage = None
                        runner._admit_runtime("A_plain")
    except BaseException as exc:
        if active_stager is not None:
            try:
                active_stager.__exit__(*sys.exc_info())
            finally:
                active_stager = None
                active_diff_impact_stage = None
                runner._admit_runtime("A_plain")
        if pending_result_rows:
            printed_cells = _print_result_block(
                pending_result_rows, printed_cells=printed_cells, planned_cells=planned_cells
            )
            pending_result_rows = []
        metadata["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        metadata["completed_at"] = _utc_now()
        metadata["error"] = {"type": type(exc).__name__, "message": str(exc)[:1000]}
        metadata["artifacts"]["telemetry_sha256"] = hashlib.sha256(output_path.read_bytes()).hexdigest()
        pooling_reasons = metadata["artifacts"]["canonical_telemetry_pooling_ineligibility_reasons"]
        if "run_not_completed" not in pooling_reasons:
            pooling_reasons.append("run_not_completed")
        if canonical_path.exists():
            metadata["artifacts"]["canonical_telemetry_status"] = "partial"
            metadata["artifacts"]["canonical_telemetry_pooling_eligible"] = False
        _write_run_metadata(metadata_path, metadata)
        print(f"SUMMARY\tstatus={metadata['status']}\tpersisted_cells={metadata['persisted_cells']}")
        raise
    finally:
        try:
            _close_runner(runner)
        except BaseException as cleanup_exc:
            prior_error = metadata.get("error")
            metadata["status"] = "failed"
            metadata["completed_at"] = _utc_now()
            metadata["error"] = {
                "type": type(cleanup_exc).__name__,
                "message": f"runner credential cleanup failed: {cleanup_exc}"[:1000],
                "prior_error": prior_error,
            }
            metadata["artifacts"]["telemetry_sha256"] = hashlib.sha256(output_path.read_bytes()).hexdigest()
            pooling_reasons = metadata["artifacts"]["canonical_telemetry_pooling_ineligibility_reasons"]
            if "run_not_completed" not in pooling_reasons:
                pooling_reasons.append("run_not_completed")
            if canonical_path.exists():
                metadata["artifacts"]["canonical_telemetry_status"] = "partial"
                metadata["artifacts"]["canonical_telemetry_pooling_eligible"] = False
            _write_run_metadata(metadata_path, metadata)
            print(f"SUMMARY\tstatus=failed\tpersisted_cells={metadata['persisted_cells']}")
            raise
    if invocation_launcher_path is not None and invocation_launcher_sha256 is not None:
        _validate_invocation_launcher(invocation_launcher_path, invocation_launcher_sha256)
    metadata["status"] = "completed"
    metadata["completed_at"] = _utc_now()
    metadata["artifacts"]["telemetry_sha256"] = hashlib.sha256(output_path.read_bytes()).hexdigest()
    metadata["artifacts"]["canonical_telemetry_status"] = "complete"
    metadata["artifacts"]["canonical_telemetry_pooling_eligible"] = not metadata["artifacts"][
        "canonical_telemetry_pooling_ineligibility_reasons"
    ]
    _write_run_metadata(metadata_path, metadata)
    print(
        f"SUMMARY\tstatus=completed\tpersisted_cells={metadata['persisted_cells']}"
        f"\toutcomes={json.dumps(metadata['cell_outcomes'], sort_keys=True)}"
    )


def _cli_error(message: str) -> NoReturn:
    """Fail a CLI invocation with the usage status the previous parser reported.

    Args:
        message: Operator-facing reason, written to standard error verbatim.

    Raises:
        SystemExit: Always, with status ``2`` for usage errors.

    Examples:
        >>> import contextlib, io
        >>> stderr = io.StringIO()
        >>> with contextlib.redirect_stderr(stderr):
        ...     try:
        ...         _cli_error("boom")
        ...     except SystemExit as exc:
        ...         print(exc.code)
        2
        >>> stderr.getvalue().strip()
        'ERROR: boom'
    """
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(2)


def _optional_path(value: str | Path | None) -> Path | None:
    """Coerce an optional CLI string into a path, because fire never coerces.

    Args:
        value: Raw flag value, or ``None`` when the flag was not supplied.

    Returns:
        The coerced path, or ``None`` when nothing was supplied.

    Examples:
        >>> _optional_path(None) is None
        True
        >>> _optional_path("benchmarks/suites/tasks-bench.json").name
        'tasks-bench.json'
    """
    return None if value is None else Path(value)


def _print_rescore(run_dir: Path) -> None:
    """Print the offline rescore artifact path; fire must never see a return value.

    Args:
        run_dir: Completed run directory holding frozen telemetry and tasks.

    Examples:
        >>> _print_rescore.__name__
        '_print_rescore'
    """
    print(rescore_results(run_dir))


def _print_task_selection(manifest_path: Path, selectors: str | Sequence[str]) -> None:
    """Print the resolved targeted scope as canonical JSON on standard output.

    Args:
        manifest_path: Active benchmark manifest defining the locked task suite.
        selectors: Comma-separated exact task IDs or task families.

    Examples:
        >>> _print_task_selection.__name__
        '_print_task_selection'
    """
    print(json.dumps(resolve_task_selection(manifest_path, selectors), sort_keys=True))


def _validate_cli_modes(
    *,
    render_results: bool,
    rescore_results_dir: str | None,
    resolve_tasks: str | Sequence[str] | None,
    force_color: bool,
    hide_plan: bool,
) -> None:
    """Reject the mutually exclusive CLI mode combinations fire cannot express.

    Args:
        render_results: Whether the stream-rendering mode was requested.
        rescore_results_dir: Run directory for the offline rescore mode, if any.
        resolve_tasks: Selectors for the task-resolution mode, if any.
        force_color: Whether renderer coloring was forced.
        hide_plan: Whether renderer PLAN filtering was requested.

    Raises:
        SystemExit: When two exclusive modes or a renderer-only flag are combined.

    Examples:
        >>> _validate_cli_modes(
        ...     render_results=True,
        ...     rescore_results_dir=None,
        ...     resolve_tasks=None,
        ...     force_color=True,
        ...     hide_plan=True,
        ... ) is None
        True
    """
    if force_color and not render_results:
        _cli_error("--force-color requires --render-results")
    if hide_plan and not render_results:
        _cli_error("--hide-plan requires --render-results")
    if rescore_results_dir is not None and (render_results or resolve_tasks is not None):
        _cli_error("--rescore-results cannot be combined with rendering or task resolution")
    if resolve_tasks is not None and render_results:
        _cli_error("--resolve-tasks cannot be combined with --render-results")


def _require_execution_options(**options: object) -> None:
    """Require every execution-only option, reporting the missing flag by name.

    Args:
        **options: Parameter name to supplied value, in the order to report.

    Raises:
        SystemExit: When any supplied value is ``None``.

    Examples:
        >>> _require_execution_options(repo_path="repo", model="gpt", tasks_path="tasks.json") is None
        True
    """
    for option, value in options.items():
        if value is None:
            _cli_error(f"--{option.replace('_', '-')} is required unless --render-results is used")


def _resolve_execution_scope(
    *,
    selection: Mapping[str, Any],
    repo_path: Path,
    model: str,
    reasoning_effort: str,
    manifest_path: Path,
    index_path: Path,
    index_relocation: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Bind the selected stage partitions into one immutable execution scope.

    ``index_relocation`` is present only for a run outside the canonical clone; each executable stage admits the
    relocated graph on that provenance rather than on the byte hash the lock recorded.
    """
    from _bench_codex.stage_fix import resolve_fix_stage_scope
    from _bench_codex.stage_readcrop import resolve_readcrop_stage_scope

    scoped_stages: list[dict[str, Any]] = []
    for stage in selection["stages"]:
        stage_id = str(stage["stage_id"])
        task_ids = list(stage["task_ids"])
        if stage_id == "structural":
            child_scope = {
                "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                "task_ids": task_ids,
                "repetitions": stage["repetitions"],
                "arms": stage["arms"],
                "coordinate_timeout_seconds": _task_selection_contract(manifest_path)["coordinate_timeout_seconds"],
            }
            child_scope["scope_sha256"] = _targeted_scope_sha256(child_scope)
        elif stage_id == "readcrop":
            child_scope = resolve_readcrop_stage_scope(
                repo_path=repo_path,
                model=model,
                tasks_selector=",".join(task_ids),
                structural_manifest_path=manifest_path,
            )
        else:
            child_scope = resolve_fix_stage_scope(
                study=stage_id,
                repo_path=repo_path,
                selected=set(task_ids),
                model=model,
                index_path=index_path,
                index_relocation=index_relocation,
            )
        scoped_stages.append({**stage, "scope_sha256": child_scope["scope_sha256"]})
    aggregate = {
        "schema_version": "codex-unified-scope-v1",
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "model": model,
        "reasoning_effort": reasoning_effort,
        "selection_mode": selection["selection_mode"],
        "task_ids": selection["task_ids"],
        "stages": scoped_stages,
        "total_tasks": selection["total_tasks"],
        "total_cells": selection["total_cells"],
    }
    aggregate["scope_sha256"] = hashlib.sha256(
        json.dumps(aggregate, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return aggregate


def _write_unified_metadata(path: Path, payload: Mapping[str, Any]) -> None:
    """Persist aggregate lifecycle state without stage-specific quality fields."""
    path.write_text(json.dumps(dict(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _annotate_stage_metadata(stage_dir: Path, stage_id: str) -> None:
    """Ensure every child artifact identifies its native scorer stage."""
    path = stage_dir / "run-metadata.json"
    if not path.is_file():
        return
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["stage_id"] = stage_id
    _write_unified_metadata(path, payload)


def _run_unified_execution(
    *,
    repo_path: Path,
    model: str,
    reasoning_effort: str,
    tasks: str | Sequence[str] | None,
    manifest_path: Path,
    index_path: Path,
    marketplace_root: Path,
    codemap_bin: Path,
    auth_source: Path | None,
    invocation_launcher_path: Path | None,
    run_dir: Path | None,
    paid_approval: str | None,
    dry_run: bool,
    show_legend: bool,
    index_relocation_path: Path | None = None,
    show_paid_command: bool = True,
) -> None:
    """Plan or execute the selected stage partitions under one authorization.

    ``index_relocation_path`` is present only for a run outside the canonical clone, whose index is the locked graph
    with its scan root moved; admission then checks that provenance rather than the byte hash, which reproduces at the
    canonical path alone.
    """
    from _bench_codex.stage_fix import run_fix_stage
    from _bench_codex.stage_readcrop import run_stage as run_readcrop_stage

    paid_approval = None if paid_approval is None else str(paid_approval)
    relocation = load_index_relocation(index_relocation_path)
    selection = resolve_task_selection(manifest_path, tasks)
    if not dry_run:
        missing = [
            flag
            for flag, value in (
                ("--auth-source", auth_source),
                ("--run-dir", run_dir),
                ("--paid-approval", paid_approval),
            )
            if value is None
        ]
        if missing:
            raise ValueError(
                f"cannot start model execution; missing {', '.join(missing)}. "
                "Run the same command with --dry-run and copy its PAID_COMMAND exactly."
            )
        if not isinstance(paid_approval, str) or re.fullmatch(r"[0-9a-f]{16,64}", paid_approval) is None:
            raise ValueError(
                "paid approval must be the 16-character token printed by --dry-run "
                "or a longer matching lowercase SHA-256 prefix. No model call was made."
            )
    scope = _resolve_execution_scope(
        selection=selection,
        repo_path=repo_path,
        model=model,
        reasoning_effort=reasoning_effort,
        manifest_path=manifest_path,
        index_path=index_path,
        index_relocation=relocation,
    )
    if not dry_run:
        if not paid_approval_matches(paid_approval, str(scope["scope_sha256"])):
            raise ValueError(
                "paid approval does not match the current aggregate scope. No model call was made. "
                "Run the same command with --dry-run and copy its PAID_COMMAND exactly."
            )
        assert run_dir is not None
        if run_dir.exists():
            raise FileExistsError(
                f"run directory already exists: {run_dir}. Use the fresh --run-dir printed by --dry-run."
            )

    def run_stage(stage: Mapping[str, Any], *, child_dir: Path | None) -> None:
        """Dispatch one already-resolved partition to its native stage engine."""
        stage_id = str(stage["stage_id"])
        task_ids = list(stage["task_ids"])
        if stage_id == "structural":
            selected = selection["selection_mode"] == "selected"
            main(
                repo_path=repo_path,
                model=model,
                reasoning_effort=reasoning_effort,
                tasks_path=Path(__file__).with_name("suites") / "tasks-bench.json",
                manifest_path=manifest_path,
                index_path=index_path,
                marketplace_root=marketplace_root,
                codemap_bin=codemap_bin,
                auth_source=auth_source,
                invocation_launcher_path=invocation_launcher_path,
                output_path=None if child_dir is None else child_dir / "telemetry.jsonl",
                metadata_path=None if child_dir is None else child_dir / "run-metadata.json",
                task_ids=None if selected else task_ids,
                task_selectors=",".join(task_ids) if selected else None,
                scope_sha256=stage["scope_sha256"] if selected else None,
                repetitions=int(stage["repetitions"]),
                index_relocation_path=index_relocation_path,
                dry_run=dry_run,
                show_legend=show_legend,
            )
        elif stage_id == "readcrop":
            run_readcrop_stage(
                repo_path=repo_path,
                model=model,
                tasks_selector=",".join(task_ids),
                dry_run_requested=dry_run,
                resolve_scope_requested=False,
                auth_source=auth_source,
                run_dir=child_dir,
                paid_approval=stage["scope_sha256"],
                index_path=index_path,
                marketplace_root=marketplace_root,
                codemap_bin=codemap_bin,
                structural_manifest_path=manifest_path,
                emit_authorization=False,
                index_relocation=relocation,
            )
        else:
            run_fix_stage(
                study=stage_id,
                repo_path=repo_path,
                selected=set(task_ids),
                dry_run=dry_run,
                resolve_scope=False,
                auth_source=auth_source,
                run_dir=child_dir,
                paid_approval=stage["scope_sha256"],
                model=model,
                index_path=index_path,
                marketplace_root=marketplace_root,
                codemap_bin=codemap_bin,
                emit_authorization=False,
                index_relocation=relocation,
            )

    if dry_run:
        for stage in scope["stages"]:
            run_stage(stage, child_dir=None)
        print(f"DESIGN   {scope['total_tasks']} tasks × A/B/C = {scope['total_cells']} cells")
        print(f"SCOPE   {scope['scope_sha256']}")
        if show_paid_command:
            runtime.print_unified_paid_command(
                repo_path=repo_path,
                manifest_path=manifest_path,
                index_path=index_path,
                marketplace_root=marketplace_root,
                codemap_bin=codemap_bin,
                model=model,
                selectors=selection["selectors"],
                scope_sha256=scope["scope_sha256"],
                patch_pytest=(
                    str(patch_test_runtime_identity()["pytest_executable"])
                    if any(stage["stage_id"] == "patch" for stage in scope["stages"])
                    else None
                ),
                index_relocation_path=index_relocation_path,
            )
        return

    assert run_dir is not None
    metadata_path = run_dir / "run-metadata.json"
    metadata: dict[str, Any] = {
        "schema_version": "codex-unified-run-v1",
        "status": "running",
        "scope": scope,
        "stages": [],
        "started_at": _utc_now(),
    }
    stage_design = ", ".join(
        f"{stage['stage_id']}={len(stage['task_ids'])} tasks/{stage['total_cells']} cells" for stage in scope["stages"]
    )
    runtime.print_section_rule("CODEX UNIFIED A/B/C STUDY")
    print(f"→ aggregate: {scope['total_tasks']} tasks, {scope['total_cells']} cells")
    print(f"→ sequential stages: {stage_design}")
    run_dir.mkdir(parents=True, exist_ok=False)
    _write_unified_metadata(metadata_path, metadata)
    aggregate_completed = 0
    try:
        for stage_number, stage in enumerate(scope["stages"], start=1):
            stage_id = str(stage["stage_id"])
            child_dir = run_dir / stage_id
            stage_record = {"stage_id": stage_id, "status": "running", "path": stage_id}
            metadata["stages"].append(stage_record)
            _write_unified_metadata(metadata_path, metadata)
            runtime.print_section_rule(
                f"STAGE {stage_number}/{len(scope['stages'])}: {stage_id} "
                f"({len(stage['task_ids'])} tasks, {stage['total_cells']} cells)"
            )
            try:
                with runtime.progress_scope(
                    completed_offset=aggregate_completed,
                    total_cells=int(scope["total_cells"]),
                ):
                    run_stage(stage, child_dir=child_dir)
            finally:
                _annotate_stage_metadata(child_dir, stage_id)
                if child_dir.is_dir():
                    write_checksums(child_dir)
            stage_record["status"] = "completed"
            aggregate_completed += int(stage["total_cells"])
            _write_unified_metadata(metadata_path, metadata)
        metadata["status"] = "completed"
    except BaseException as exc:
        metadata["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        metadata["error"] = {"type": type(exc).__name__, "message": str(exc)[:1000]}
        if metadata["stages"] and metadata["stages"][-1]["status"] == "running":
            metadata["stages"][-1]["status"] = metadata["status"]
        raise
    finally:
        metadata["completed_at"] = _utc_now()
        _write_unified_metadata(metadata_path, metadata)
        write_checksums(run_dir)
        print(f"SUMMARY  status={metadata['status']}  stages={len(metadata['stages'])}/{len(scope['stages'])}")
    print(f"done: {run_dir}")


def cli(  # noqa: PLR0913 — fire CLI adapter: every param is a keyword flag with a default (0 required)
    render_results: bool = False,
    rescore_results: str | None = None,
    resolve_tasks: str | Sequence[str] | None = None,
    force_color: bool = False,
    hide_plan: bool = False,
    repo_path: str | None = None,
    model: str | None = None,
    reasoning_effort: str = PARITY_CODEX_REASONING_EFFORT,
    manifest_path: str | Path = PARITY_MANIFEST_PATH,
    index_path: str | None = None,
    marketplace_root: str | None = None,
    codemap_bin: str | None = None,
    auth_source: str | None = None,
    invocation_launcher_path: str | None = None,
    tasks: str | Sequence[str] | None = None,
    dry_run: bool = False,
    no_legend: bool = False,
    no_paid_command: bool = False,
    run_dir: str | None = None,
    paid_approval: str | None = None,
    rescore_fix_run_dir: str | None = None,
    rescore_fix_output_dir: str | None = None,
    rescore_readcrop_run_dir: str | None = None,
    rescore_readcrop_output_dir: str | None = None,
    index_relocation_path: str | None = None,
) -> None:
    """Dispatch one unified Codex benchmark, rendering, resolution, or rescore mode.

    Benchmark execution is task-driven. Omitting ``tasks`` executes all 55
    structural, 6 ReadCrop, 4 Fix-Single, 3 Fix-Multi, and 5 Patch tasks: 73
    tasks and 219 A/B/C cells. Family selectors such as ``RC,FS,FM,PT`` and
    mixed exact IDs such as ``RC-01,FS-03,FM-02,PT-04`` route to their native stage scorers while
    retaining separate child artifacts. Absence of ``dry_run`` means model
    execution and therefore requires authentication, a fresh run directory,
    and the aggregate approval printed by the matching dry run.

    Exactly one non-execution mode may run per invocation: stream rendering,
    offline rescoring, or selector resolution. Every branch prints its own
    output and returns ``None`` because Fire echoes returned values and would
    corrupt machine-parsed standard output.

    Args:
        render_results: Render progress rows read from standard input.
        rescore_results: Completed run directory to replay into an immutable
            offline rescore artifact; prints the artifact path.
        resolve_tasks: Comma-separated exact task IDs or task families to resolve
            into a targeted scope; prints the scope as canonical JSON.
        force_color: Force terminal coloring in the renderer; requires
            ``--render-results``. Test-only.
        hide_plan: Drop human ``PLAN`` rows in the renderer; requires
            ``--render-results``. Test-only.
        repo_path: Target repository clone; required for execution.
        model: Codex model identifier; required for execution.
        reasoning_effort: Locked Codex reasoning stratum; only
            ``PARITY_CODEX_REASONING_EFFORT`` is accepted.
        manifest_path: Active benchmark manifest defining the locked contract.
        index_path: Locked Codemap index consumed by the B and C arms.
        marketplace_root: Local plugin marketplace root for the C arm.
        codemap_bin: Direct Codemap launcher for the B arm.
        auth_source: User-owned ``auth.json`` copied into the disposable home.
        invocation_launcher_path: Recorded launcher whose digest is revalidated.
        tasks: Optional comma-separated task families or exact IDs. Omit it for
            the complete 73-task suite.
        dry_run: Validate locked inputs and print the cell plan without a model call.
        no_legend: Suppress the output legend block.
        no_paid_command: Suppress the dry run's PAID_COMMAND block; SCOPE still prints.
        run_dir: Fresh aggregate artifact directory required for model execution.
        paid_approval: Aggregate approval token printed by the matching dry run.

    Raises:
        SystemExit: With status ``2`` when flags are combined illegally, a
            required execution option is missing, or the reasoning stratum
            differs from the locked value.

    Examples:
        >>> cli.__name__
        'cli'
    """
    if render_results:
        # Fire has already identified renderer mode, but Windows subprocess tests
        # can lose the second bare Boolean. Preserve the explicit test-only flag.
        force_color = force_color or "--force-color" in sys.argv[1:]
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
    _validate_cli_modes(
        render_results=render_results,
        rescore_results_dir=rescore_results,
        resolve_tasks=resolve_tasks,
        force_color=force_color,
        hide_plan=hide_plan,
    )
    if rescore_results is not None:
        _print_rescore(Path(rescore_results))
        return
    if resolve_tasks is not None:
        _print_task_selection(Path(manifest_path), resolve_tasks)
        return
    if render_results:
        runtime.render_result_rows(sys.stdin, sys.stdout, force_color=force_color, hide_plan=hide_plan)
        return
    if (rescore_readcrop_run_dir is None) != (rescore_readcrop_output_dir is None):
        _cli_error("--rescore-readcrop-run-dir requires --rescore-readcrop-output-dir")
    if rescore_readcrop_run_dir is not None:
        from _bench_codex.stage_readcrop import run_stage

        _require_execution_options(repo_path=repo_path)
        run_stage(
            repo_path=Path(str(repo_path)),
            model=str(model) if model is not None else PARITY_CODEX_MODEL,
            tasks_selector=None,
            dry_run_requested=False,
            resolve_scope_requested=False,
            auth_source=None,
            run_dir=None,
            paid_approval=None,
            structural_manifest_path=Path(manifest_path),
            rescore_run_dir=Path(str(rescore_readcrop_run_dir)),
            rescore_output_dir=Path(str(rescore_readcrop_output_dir)),
        )
        return
    if (rescore_fix_run_dir is None) != (rescore_fix_output_dir is None):
        _cli_error("--rescore-fix-run-dir requires --rescore-fix-output-dir")
    if rescore_fix_run_dir is not None:
        from _bench_codex.stage_fix import rescore_fix_stage

        _require_execution_options(repo_path=repo_path)
        output_dir = rescore_fix_stage(
            Path(str(rescore_fix_run_dir)),
            Path(str(rescore_fix_output_dir)),
            Path(str(repo_path)),
        )
        print(f"rescored: {output_dir}")
        return
    _require_execution_options(repo_path=repo_path, model=model)
    if reasoning_effort != PARITY_CODEX_REASONING_EFFORT:
        _cli_error(f"--reasoning-effort must be {PARITY_CODEX_REASONING_EFFORT!r}")
    resolved_repo = Path(str(repo_path))
    resolved_index = _optional_path(index_path) or resolved_repo / ".cache" / "codemap" / f"{resolved_repo.name}.json"
    _run_unified_execution(
        repo_path=resolved_repo,
        model=str(model),
        reasoning_effort=reasoning_effort,
        tasks=tasks,
        manifest_path=Path(manifest_path),
        index_path=resolved_index,
        marketplace_root=_optional_path(marketplace_root) or Path(__file__).resolve().parents[1],
        codemap_bin=_optional_path(codemap_bin)
        or Path(__file__).resolve().parents[1] / "plugins" / "codemap-py" / "bin" / "codemap-py",
        auth_source=_optional_path(auth_source),
        invocation_launcher_path=_optional_path(invocation_launcher_path),
        run_dir=_optional_path(run_dir),
        paid_approval=paid_approval,
        index_relocation_path=_optional_path(index_relocation_path),
        dry_run=dry_run,
        show_legend=not no_legend,
        show_paid_command=not no_paid_command,
    )


if __name__ == "__main__":
    from fire import Fire

    # Fire invokes the callable before reporting surplus flags. Fail first so a
    # removed legacy option can never widen or start an execution accidentally.
    removed_options = {
        "--arm",
        "--metadata-path",
        "--output-path",
        "--paid",
        "--repetitions",
        "--scope-sha256",
        "--study",
        "--task-id",
        "--tasks-path",
    }
    supplied_removed = sorted({argument.partition("=")[0] for argument in sys.argv[1:]} & removed_options)
    if supplied_removed:
        print(
            f"ERROR: removed option(s): {', '.join(supplied_removed)}. Use only --tasks for family or exact-task "
            "selection; omit --tasks for all supported tasks.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    try:
        Fire(cli)
    except (TreatmentArtifactLockError, FileExistsError, ValueError) as exc:
        raise SystemExit(f"ERROR: {exc}") from None
