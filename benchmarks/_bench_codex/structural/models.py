"""The per-cell result record for one Codex structural run."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


from _bench_common.provider_parity_contracts import (
    PARITY_TIMEOUT_SECONDS,
)

from _bench_codex.structural.config import PARITY_CODEX_REASONING_EFFORT, _NATIVE_ITEM_TELEMETRY_CONTRACT_ID


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
