"""Arm ordering, contract hashing, and per-arm result rendering."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from _bench_common.provider_parity_contracts import (
    ARM_CONTRACTS,
    canonical_task_hash,
)

from _bench_codex import runtime
from _bench_codex.structural.config import (
    _COUNTERBALANCED_ARM_ORDERS,
    _PROVENANCE_KEY,
    CODEX_STRUCTURAL_ARMS,
    PARITY_MANIFEST_PATH,
)


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
