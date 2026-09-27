"""Result records for one benchmark run and its quality grading."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


from _bench_common.provider_parity_contracts import (
    EvaluationResult,
)

from _bench_claude.structural.config import LEGACY_EXPERIMENT_REVISION


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class BenchQuality:
    """Quality score for one benchmark run.

    Attributes:
        scored: True when evaluation ran against a matching task type.
        correct: True when the primary metric matches ground truth within tolerance.
        metric_expected: Ground-truth value of the primary metric.
        metric_got: Value extracted from model output; None when extraction failed.
        recall: Optional evaluator-specific recall metric; set by develop_br, rv (symbol tasks), debug, feature, and
            real_issue evaluators. None for count-based evaluators (symbol_extraction, code_quality).
        caller_count_gt: Ground-truth unique caller count; used by caller-list evaluators for both fn_call_graph and
            develop_blast_radius.
        extraction_degraded: True when structured-block scoring could not find a
            labeled answer block (e.g. ``## Files`` / ``## Callers``) and fell back to matching
            against the full output text. Diagnostic only — a degraded match still counts, but the
            flag surfaces that the stricter block-scoped match was unavailable for the run.
        evaluator_used: Name of the evaluator function that produced this score
            (diagnostic; None when no evaluator ran).
        extracted_metric: Raw value pulled from output_text before comparison — an
            integer count, a matches/recall numerator, or a found-name list depending
            on the evaluator (diagnostic; distinct from the final ``correct`` score).
        scoring_detail: Diagnostic breakdown of the comparison the evaluator computed:
            keys ``metric_expected``, ``metric_got``, ``threshold``, ``method``. Lets a
            failed run be diagnosed without re-reading output_text.
    """

    scored: bool = False
    correct: bool = False
    metric_expected: Any = None
    metric_got: Any = None
    recall: float | None = None
    caller_count_gt: int | None = None
    extraction_failed: bool = False
    extraction_degraded: bool = False
    evaluator_used: str | None = None
    evaluator_version: str | None = None
    extracted_metric: Any = None
    scoring_detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _BenchEvaluationResult(EvaluationResult):
    """Shared evaluation result that retains the Claude-specific score diagnostics."""

    bench_quality: BenchQuality = field(default_factory=BenchQuality)


@dataclass
class BenchRun:
    """Result of a single benchmark run (one task × arm × model).

    Attributes:
        arm: "plain" or "codemap".
        task_id: Task ID from tasks-bench.json (e.g. "SE-01").
        task_type: Task type string (e.g. "symbol_extraction").
        model: Short model tier name.
        success: True when the claude subprocess returned a successful result.
        workflow_type: Coarse workflow grouping (e.g. "query", "debug", "feature").
            Falls back to ``task_type`` when the task carries no ``workflow_type`` field.
        input_tokens: Total input token count (all cache partitions summed).
        output_tokens: Total output token count.
        elapsed_s: Wall-clock seconds.
        error: Error string on failure; empty on success.
        tool_log: Short log of tool calls in order.
        output_text: Full agent response text.
        quality: Quality evaluation result.
        skill_calls: Number of Skill tool invocations.
        grep_calls: Number of Grep tool invocations.
        bash_calls: Number of Bash tool invocations.
        patch_pass: Patch tasks only — True when the failing test passed after the
            agent's diff was applied in a sandbox; None for non-patch tasks or when
            no diff could be extracted from the agent output.
        mutation_evidence: Patch-task lifecycle evidence, including an action or
            cleanup failure, retained separately from the semantic test result.
        self_consistency: True when the task's ground truth is derived from the same
            scan-query index the codemap arm queries (uncovered / broken-xref counts).
            Such runs are still scored but excluded from headline accuracy aggregates
            and reported in a separate self-consistency row.
        repo_sha: Provenance — repo HEAD SHA when the run executed; "unknown" on failure.
        index_sha: Provenance — fingerprint of the index head-meta (see ``_index_sha``).
        task_hash: Provenance — sha256 of the canonical task JSON (see ``_task_hash``).
        suite_hash: Provenance — versioned semantic hash of ordered task contracts.
        suite_raw_hash: Audit-only SHA-256 of the source suite file bytes.
        contaminated: True when the existing contamination or answer-file-read guard excluded the row.
        treatment_adherence: Canonical A/B/C assigned-treatment observation; None for legacy arms.
        resumed: True when this line was reused from a prior results file via ``--resume``
            (the claude subprocess was not re-executed for this tuple).
    """

    arm: str
    task_id: str
    task_type: str
    model: str
    success: bool
    workflow_type: str = ""
    capability_strata: tuple[str, ...] = ()
    quality_components: dict[str, float] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0  # Anthropic's total_cost_usd for this run (current prices); 0.0 if absent
    elapsed_s: float = 0.0
    error: str = ""
    tool_log: list[str] = field(default_factory=list)
    output_text: str = ""
    quality: BenchQuality = field(default_factory=BenchQuality)
    skill_calls: int = 0
    skill_counts: dict[str, int] = field(default_factory=dict)
    grep_calls: int = 0
    bash_calls: int = 0
    read_calls: int = 0
    scan_query_calls: int = 0
    contamination_hits: int = 0  # plain arm only: full-string reads/execs touching the codemap index or binary
    scan_query_subcommands: dict[str, int] = field(default_factory=dict)
    used_batch: bool = False  # codemap arm invoked scan-query `batch` (JSON-array multi-query form) at least once
    turn_count: int = 0
    incomplete: bool = False  # budget exhausted before final answer; excluded from accuracy
    codemap_methods: list[str] = field(default_factory=list)
    codemap_not_covered: list[str] = field(default_factory=list)
    patch_pass: bool | None = None  # patch tasks only: True if failing test passed after applying the agent diff
    mutation_evidence: dict[str, Any] = field(default_factory=dict)
    self_consistency: bool = False  # ground truth derived from the queried index; excluded from headline accuracy
    repo_sha: str = "unknown"  # provenance: repo HEAD when the run executed (git rev-parse; "unknown" on failure)
    index_sha: str = "unknown"  # provenance: fingerprint of the index head-meta (see _index_sha)
    task_hash: str | None = None  # provenance: sha256 of the canonical task JSON (see _task_hash)
    prompt_hash: str | None = None
    prompt_sha256: str | None = None  # compatibility alias for prompt_hash
    suite_hash: str | None = None
    suite_raw_hash: str | None = None
    evaluator_id: str | None = None
    evaluator_hash: str | None = None
    envelope_hash: str | None = None
    arm_contract_hash: str = ""
    experiment_revision: str = LEGACY_EXPERIMENT_REVISION
    parity_arm: str = ""
    compliance: bool | None = None  # C_strict usage evidence, separate from task quality
    contaminated: bool = False
    treatment_adherence: bool | None = None
    oracle_class: str = "unknown"
    headline_eligible_v1: bool = False
    scoreable: bool = False
    resumed: bool = False  # True when this line was reused from a prior results file via ``--resume`` (not re-executed)
