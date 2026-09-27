"""Result records for one agentic run, its quality grade, and its tool tally."""

from dataclasses import dataclass, field
from typing import Any, Optional


# Re-exported for call-site/test compatibility (tests reference it via this module's namespace).
from _bench_common.python_source import resolve_relative_base  # noqa: E402,F401
from _bench_common.agentic_contracts import (  # noqa: E402
    AgenticOracle,  # noqa: F401
    AnswerScore,  # noqa: F401
)
from _bench_common.provider_parity_contracts import (  # noqa: E402
    ARM_CONTRACTS,
)

# Stage plumbing lives in a private module so this runner stays under the suite's 250 KB maintenance limit.
# Every name it defines is re-exported here, including ones this file no longer calls itself: callers and tests
# reach these through the runner module, so pruning an apparently unused re-export breaks patch.object targets.
from _bench_common.claude_stages import (  # noqa: E402,F401
    FIX_MULTI_TASKS_PATH,
    FIX_SINGLE_ARMS,
    FIX_SINGLE_TASKS_PATH,
    FixMultiContract,
    FixSingleContract,
    PARITY_MANIFEST_PATH,
    PATCH_TASKS_PATH,
    PurePosixPath,
    READCROP_ARMS,
    READCROP_TASKS_PATH,
    ReadcropUsage,
    StageIdentity,
    _FIX_MULTI_QUERY_ARGUMENTS,
    _FIX_SINGLE_QUERY_ARGUMENTS,
    _PATCH_QUERY_ARGUMENTS,
    _READCROP_ANSWER_RE,
    _absolute_codemap_launchers,
    _claude_codemap_evidence,
    _claude_event_summary,
    _claude_message_blocks,
    _command_arguments,
    _compact_query_result_succeeded,
    _frozen_index_recovery_attempted,
    _is_compact_query,
    _is_inside_workspace,
    _load_claude_fix_tasks,
    _manifest_sha256,
    _native_tool_result_succeeded,
    _outside_workspace_path_evidence,
    _patch_index_path,
    _patch_stage_identity,
    _provider_binding,
    _query_arguments_from_bash,
    _query_command_tail,
    _readcrop_module_path,
    _resolve_claude_fix_scope,
    _study_query_arguments,
    _tool_input_strings,
    _tool_result_text,
    _workspace_containment_roots,
    build_edit_task_contract,
    build_fix_multi_contract,
    build_fix_single_contract,
    build_readcrop_contract,
    extract_readcrop_symbol_source,
    load_claude_fix_multi_tasks,
    load_claude_fix_single_tasks,
    load_claude_patch_tasks,
    load_claude_readcrop_tasks,
    parse_claude_readcrop_events,
    parse_readcrop_answer,
    prompt_hash,
    readcrop_prompt,
    resolve_claude_fix_multi_scope,
    resolve_claude_fix_single_scope,
    resolve_claude_patch_scope,
    resolve_readcrop_scope,
    score_readcrop_answer,
    stage_contract_sha256,
)


# fmt_tok comes from presentation (shared with run-claude-structural).


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class QualityScore:
    """Quality score for a single benchmark run.

    Primary metrics (v2 — multi-form matching with 2+ component surface forms):
        ``erec``  — exposure recall: rdeps found in agent output_text (tool outputs excluded)
        ``rrec``  — report recall: rdeps found in agent's final answer after last tool call
        ``delta`` — erec - rrec: information gap (agent saw but did not report)
        ``deff``  — discovery efficiency: erec_tp / max(tool_calls, 1)

    Supplementary (codemap arm only):
        ``skill_coverage``, ``skill_returned``

    Semble-native lens (semble / combined arms only):
        ``chunk_hit_rate`` — expected rdeps whose module/file appears in any retrieved semble chunk

    Legacy fields (``leaf_recall``, ``precision``, ``recall``, ``f1``, ``tp``, ``fp``, ``fn``)
    are retained for backward compatibility; computed via leaf-name matching on output_text.
    """

    scored: bool = False  # False when no ground truth is available

    # ── Primary metrics (v2 — multi-form matching) ──
    erec: float = 0.0  # exposure recall: rdeps found in output_text + codemap results
    erec_tp: int = 0
    erec_fn: int = 0
    rrec: float = 0.0  # report recall: rdeps found in final answer text (after last tool call)
    rrec_tp: int = 0
    rrec_fn: int = 0
    delta: float = 0.0  # erec - rrec: information the agent saw but did not report
    deff: float = 0.0  # discovery efficiency: erec_tp / max(tool_calls, 1)
    erec_top10: float = 0.0  # erec restricted to top-10 rdeps by in-degree (reverse-dep) centrality
    erec_top10_k: int = 0  # actual k used (min(10, |expected|)); equals |expected| when ≤10

    # ── Skill result coverage (codemap arm only; None when not applicable) ──
    skill_coverage: Optional[float] = None
    skill_returned: Optional[int] = None

    # ── Semble-native lens (semble / combined arms only; None when not applicable) ──
    # chunk_hit_rate: fraction of expected rdep modules whose module/file appears in ANY semble
    # search chunk the arm retrieved. A fair semantic-search axis that does not require semble to
    # emit an exhaustive dotted rdep list; erec/rrec stay the codemap-native lens.
    chunk_hit_rate: Optional[float] = None

    # ── Targeted-test correctness signal (fix tasks that declare a test_target; None otherwise) ──
    # test_passed: outcome of running the task's declared pytest node on the post-edit sandbox.
    # A stronger correctness signal than keyword recall — recorded alongside erec, never replacing
    # it. None when the task declares no test or the test could not be launched.
    test_passed: Optional[bool] = None

    # ── Legacy fields (backward compat — leaf-name matching on output_text) ──
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    tp: int = 0
    fp: int = 0
    fn: int = 0
    leaf_recall: float = 0.0
    leaf_tp: int = 0
    leaf_fn: int = 0
    ambiguous_leaves: int = 0


@dataclass
class ToolCounts:
    grep: int = 0
    glob: int = 0
    bash: int = 0
    skill: int = 0  # /codemap:query and other skill invocations via the Skill tool
    semble: int = 0  # mcp__semble__search and mcp__semble__find_related calls
    blocked: int = 0  # tool_use events that returned <tool_use_error> (permission-denied or disallowed)
    bash_for_imports: int = 0  # bash calls matching import-discovery patterns (grep/rg for import)
    index_reads: int = 0  # bash calls that read .cache/codemap/ or .cache/scan/ index files directly
    scan_query: int = 0  # Bash calls that invoke scan-query; diagnostic subset of bash
    codemap: int = 0  # Codemap Skill calls; diagnostic subset of skill

    @property
    def total(self) -> int:
        """Sum of all tool call counts across all arms.

        >>> ToolCounts(grep=3, bash=1, semble=2).total
        6
        >>> ToolCounts().total
        0
        """
        return self.grep + self.glob + self.bash + self.skill + self.semble


@dataclass
class BenchmarkRun:
    """Result of a single benchmark run (one task x arm x model).

    Renamed from RunResult; field names are unchanged, so ``asdict()`` output and serialised JSON remain identical.
    """

    arm: str
    task_id: str
    task_type: str
    model: str  # short tier name: haiku / sonnet / opus
    success: bool
    repetition: int = 1
    experiment_revision: str = ""
    parity_arm: str | None = None
    codemap_compliant: bool | None = None
    codemap_query_attempted: int = 0
    codemap_query_succeeded: int = 0
    codemap_compact_success: bool = False
    index_relocations: list[dict[str, str]] = field(default_factory=list)
    treatment_adherence: bool | None = None
    contaminated: bool = False
    incomplete: bool = False
    task_hash: str = ""
    prompt_hash: str = ""
    suite_hash: str = ""
    suite_raw_hash: str = ""
    evaluator_id: str = ""
    evaluator_hash: str = ""
    envelope_hash: str = ""
    arm_contract_hash: str = ""
    repo_sha: str = ""
    index_sha: str = ""
    oracle_class: str = ""
    headline_eligible_v1: bool = False
    scoreable: bool = True
    answer_scored: bool = False
    answer_quality_score: float | None = None
    answer_correct: bool | None = None
    answer_components: dict[str, float] = field(default_factory=dict)
    answer_graded_score: float | None = None
    answer_graded_components: dict[str, float] = field(default_factory=dict)
    answer_error: str = ""
    answer_failure_details: list[dict[str, Any]] = field(default_factory=list)
    answer_contract_valid: bool | None = None
    answer_diagnostic_only: bool = False
    answer_pooling_eligible: bool = False
    tools: ToolCounts = field(default_factory=ToolCounts)
    # Token metrics
    input_tokens: int = 0
    output_tokens: int = 0
    tool_result_tokens: int = 0  # tiktoken estimate of tool result content
    cache_read_tokens: int = 0  # cache-hit input tokens (billed ~0.1x) — for cache-aware cost
    cache_creation_tokens: int = 0  # cache-write input tokens (billed ~1.25x)
    cost_usd: float = 0.0  # Anthropic's total_cost_usd for this run (current prices); 0.0 if absent
    usage_complete: bool = False
    # Timing metrics (stored in seconds)
    elapsed_s: float = 0.0
    tool_elapsed_s: float = 0.0  # time inside tool execution only
    error: str = ""
    error_type: str = ""  # subtype from result event: error_max_turns | error_non_zero_exit | error_timeout | ""
    # Per-call log for post-run investigation: ["Bash: grep -r 'import'", "Skill: /codemap:query rdeps ..."]
    tool_log: list[str] = field(default_factory=list)
    # Raw tool_use_error payloads (first 2k chars each) for diagnosing skill failures
    tool_errors: list[str] = field(default_factory=list)
    # Full agent output text — captured for quality scoring
    output_text: str = ""
    quality: QualityScore = field(default_factory=QualityScore)
    # Internal — excluded from JSON (see _save_snapshot); populated for fix_single/fix_multicaller tasks
    agent_diff: str = field(default="", repr=False)  # unified diff of agent's edits vs original codebase
    # Transient carrier for the declared targeted-test outcome (run in the sandbox, before cleanup);
    # excluded from JSON — the persisted signal lives in quality.test_passed.
    targeted_test_passed: Optional[bool] = field(default=None, repr=False)
    # Internal fields excluded from JSON serialisation (see _save_snapshot)
    skill_result_text: str = field(default="", repr=False)  # all codemap:query rdeps results joined (for sc)
    codemap_results: list[str] = field(default_factory=list, repr=False)  # ALL codemap skill results (for erec)
    semble_results: list[str] = field(default_factory=list, repr=False)  # ALL semble MCP tool results (for erec)
    last_tool_text_offset: int = field(default=0, repr=False)  # output_text offset after last tool event
    raw_events: list[dict[str, Any]] = field(default_factory=list, repr=False)


@dataclass
class Task:
    id: str
    type: str
    prompt: str
    primary_module: str = ""
    difficulty: str = "unknown"
    skill: str = ""
    symbol: str = ""  # read_crop tasks: target symbol (e.g. "Trainer.fit")
    expected_keywords: list[str] = field(default_factory=list)  # read_crop tasks: keyword-recall ground truth
    requires_reset: bool = False  # fix tasks: snapshot/restore codebase around each arm run
    codebase_module: str = ""  # fix tasks: top-level module to snapshot (e.g. "lightning")
    expected_patch_keywords: list[str] = field(default_factory=list)  # fix tasks: strings expected in diff +lines
    expected_files: list[str] = field(default_factory=list)  # fix tasks: file-path fragments expected in diff
    test_target: str = ""  # fix tasks: pytest node id/path run on the post-edit sandbox for a correctness signal
    experiment_revision: str = ""
    task_hash: str = ""
    prompt_hash: str = ""
    suite_hash: str = ""
    suite_raw_hash: str = ""
    oracle_class: str = ""
    headline_eligible_v1: bool = False
    scoreable: bool = True
    answer_task: dict[str, object] = field(default_factory=dict, repr=False)


def parity_arm_identity(arm: str) -> str | None:
    """Return a canonical A/B/C arm only when that arm was explicitly executed.

    Legacy agentic labels have different historical no-call semantics, so they intentionally remain unlabelled rather
    than being retroactively mapped.
    """
    return arm if arm in ARM_CONTRACTS else None
