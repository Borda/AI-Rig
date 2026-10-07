"""Define provider-parity benchmark contracts without executing a benchmark.

This library loads locked task policy, preserves canonical task identity, dispatches shared evaluators, and constructs
paired effects. It does not generate tasks, invoke Claude or Codex, parse provider events, or run models.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from functools import partial
from pathlib import Path
from types import MappingProxyType
from typing import Any

Task = Mapping[str, Any]
Evaluator = Callable[[Task, str], "EvaluationResult"]


#: Seconds one paired coordinate may run; recorded as coordinate_timeout_seconds in the parity manifest.
PARITY_TIMEOUT_SECONDS = 600


#: Use-policy text for each canonical arm, with the SHA-256 of that text for tamper detection.
ARM_CONTRACTS: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "A_plain": MappingProxyType(
            {
                "contract": "Codemap is absent and inaccessible. Solve with ordinary provider tools.",
                "contract_sha256": "936a684f5b4bb6211669633a17d2a12980b24f2de43b265bbc28ef09d9a65ba7",
            }
        ),
        "B_auto": MappingProxyType(
            {
                "contract": "Codemap is installed and available. The model chooses whether to use it; no-call is valid.",
                "contract_sha256": "223aea5cbcc96ea05c9bf1e8662e74d34d2b2f8863d80ca197010bd51588d64e",
            }
        ),
        "C_strict": MappingProxyType(
            {
                "contract": "Codemap is installed and available. Use Codemap at least once for structural investigation; other tools remain allowed.",
                "contract_sha256": "06c5d7703aaa0c524889d174f962bcefd273e7b4c13e4ab53f98d7780c400ecd",
            }
        ),
    }
)

#: Every provider now runs the same three named arms. The contract text above is the use policy and
#: is shared; how a home makes Codemap reachable stays provider-native. A Codex ``C_strict`` home
#: offers only the installed Skill, so "use Codemap at least once" resolves to the Skill there
#: without needing a separate contract string, and a Claude home that also exposes the direct CLI
#: satisfies the same sentence by either route.
COMPARISON_ARMS_BY_PROVIDER: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "claude": frozenset(ARM_CONTRACTS),
        "codex": frozenset(ARM_CONTRACTS),
    }
)
#: Names of the three canonical arms, taken from ARM_CONTRACTS.
CANONICAL_ARM_NAMES = frozenset(ARM_CONTRACTS)

#: Codex structural runs before the 2026-09-06 arm alignment recorded provider-native arm names.
#: Those artifacts are immutable, so the names survive in frozen telemetry and in every published
#: table describing those runs. Reading such a row canonicalizes it for grouping and pairing; it
#: never relabels what a frozen table displays, because the frozen run's prompt is what it was.
LEGACY_ARM_ALIASES: Mapping[str, str] = MappingProxyType(
    {
        "B_direct_required": "B_auto",
        "C_skill_required": "C_strict",
    }
)
#: Every arm name a result row may carry: the canonical names plus the legacy aliases.
COMPARISON_ARM_NAMES = CANONICAL_ARM_NAMES | frozenset(LEGACY_ARM_ALIASES)

#: B is an optional-use canary on every provider: Codemap is made available and the model decides
#: whether to call it, so a zero-query B cell has followed its assigned contract (README "B is an
#: optional-use canary, so a no-query B cell is compliant"; AGENTS.md "B_auto is an optional-use
#: canary"). Treating the Codex B arm as required-use made zero-query Codex B cells non-adherent and
#: therefore pooling-ineligible, dropping exactly the cells where Codemap went unused and inflating
#: the measured B benefit. The Codex structural runner enforced a required query until the same
#: alignment removed it, which is why frozen Codex B rows measure a required-use arm.
_OPTIONAL_USE_ARMS = frozenset({"B_auto"})


@dataclass(frozen=True)
class TaskPolicy:
    """Locked headline policy for one task in an experiment revision."""

    experiment_revision: str
    task_id: str
    oracle_class: str
    headline_eligible_v1: bool
    scoreable: bool


def load_task_suite(path: Path) -> list[dict[str, Any]]:
    """Load raw task objects from a locked JSON suite without normalization.

    Args:
        path: JSON suite path, either a task list or an object with a ``tasks`` list.

    Returns:
        The original JSON task dictionaries in their declared order.

    Raises:
        ValueError: If the JSON or required task identity fields are invalid.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"task suite {path} is not valid JSON: {exc}") from exc

    if isinstance(raw, list):
        tasks = raw
    elif isinstance(raw, dict):
        if "tasks" not in raw:
            raise ValueError(f"task suite {path} object must contain a tasks list")
        tasks = raw["tasks"]
    else:
        raise ValueError(f"task suite {path} must be a JSON list or object")

    if not isinstance(tasks, list):
        raise ValueError(f"task suite {path} tasks must be a list")

    task_ids: set[str] = set()
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise ValueError(f"task suite {path} task {index} must be an object")
        for field in ("id", "prompt"):
            if field not in task:
                raise ValueError(f"task suite {path} task {index} is missing {field}")
            if not isinstance(task[field], str) or not task[field]:
                raise ValueError(f"task suite {path} task {index} {field} must be a non-empty string")
        task_id = task["id"]
        if task_id in task_ids:
            raise ValueError(f"task suite {path} contains duplicate task id {task_id!r}")
        task_ids.add(task_id)
    return tasks


def load_task_policies(path: Path) -> Mapping[str, TaskPolicy]:
    """Load immutable headline policy from a provider-parity manifest.

    Args:
        path: Locked experiment manifest containing revision and suite task rows.

    Returns:
        Immutable mapping from task ID to its revision-bound headline policy.

    Raises:
        ValueError: If required manifest policy fields are absent, malformed, or duplicated.
    """
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"experiment manifest {path} is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ValueError(f"experiment manifest {path} must be an object")

    revision = manifest.get("experiment_revision")
    suites = manifest.get("suites")
    if not isinstance(revision, str) or not revision:
        raise ValueError(f"experiment manifest {path} requires a non-empty experiment_revision")
    if not isinstance(suites, list):
        raise ValueError(f"experiment manifest {path} requires a suites list")

    policies: dict[str, TaskPolicy] = {}
    for suite_index, suite in enumerate(suites):
        if not isinstance(suite, dict) or not isinstance(suite.get("tasks"), list):
            raise ValueError(f"experiment manifest {path} suite {suite_index} requires a tasks list")
        for task_index, task in enumerate(suite["tasks"]):
            if not isinstance(task, dict):
                raise ValueError(f"experiment manifest {path} suite {suite_index} task {task_index} must be an object")
            task_id = task.get("id")
            oracle_class = task.get("oracle_class")
            headline_eligible = task.get("headline_eligible_v1")
            scoreable = task.get("effective_scoreable")
            if not isinstance(task_id, str) or not task_id:
                raise ValueError(f"experiment manifest {path} task {task_index} requires a non-empty id")
            if task_id in policies:
                raise ValueError(f"experiment manifest {path} contains duplicate task id {task_id!r}")
            if not isinstance(oracle_class, str) or not oracle_class:
                raise ValueError(f"experiment manifest {path} task {task_id!r} requires oracle_class")
            if not isinstance(headline_eligible, bool):
                raise ValueError(f"experiment manifest {path} task {task_id!r} requires boolean headline_eligible_v1")
            if not isinstance(scoreable, bool):
                raise ValueError(f"experiment manifest {path} task {task_id!r} requires boolean effective_scoreable")
            policies[task_id] = TaskPolicy(
                experiment_revision=revision,
                task_id=task_id,
                oracle_class=oracle_class,
                headline_eligible_v1=headline_eligible,
                scoreable=scoreable,
            )
    return MappingProxyType(policies)


def canonical_task_bytes(task: Task) -> bytes:
    """Serialize a raw task exactly as the B0 canonical task identity.

    Args:
        task: Raw task object with no inferred or provider-specific fields.

    Returns:
        UTF-8 JSON bytes with sorted object keys.

    Examples:
        >>> canonical_task_bytes({"b": 2, "a": 1})
        b'{"a": 1, "b": 2}'
    """
    return json.dumps(task, sort_keys=True).encode("utf-8")


def canonical_task_hash(task: Task) -> str:
    """Return the B0 SHA-256 identity for a raw task object.

    Args:
        task: Raw task object with no inferred or provider-specific fields.

    Returns:
        Hexadecimal SHA-256 digest of :func:`canonical_task_bytes`.
    """
    return hashlib.sha256(canonical_task_bytes(task)).hexdigest()


def prompt_hash(task: Task) -> str:
    """Return the SHA-256 digest of a task's delivered UTF-8 prompt.

    Args:
        task: Raw task object containing a string ``prompt`` field.

    Returns:
        Hexadecimal SHA-256 digest of the exact bytes delivered to a provider.

    Raises:
        ValueError: If the task does not carry a string prompt.
    """
    if "answer_contract" in task:
        from .agentic_contracts import materialize_agentic_prompt

        prompt = materialize_agentic_prompt(task)
    else:
        prompt = materialize_task_prompt(task)
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def materialize_task_prompt(task: Task) -> str:
    """Return the complete provider prompt for one structural task.

    Review tasks store independently scored follow-up questions in
    ``sub_questions``.  They must be part of the provider-visible prompt, not
    evaluator-only metadata.  Other task types retain their exact top-level
    prompt bytes.

    Args:
        task: Raw task object containing a string ``prompt`` field.

    Returns:
        The prompt string supplied to either model provider, before UTF-8 encoding.

    Raises:
        ValueError: If the task prompt or a nested question prompt is invalid.

    Examples:
        >>> materialize_task_prompt({"prompt": "Explain this function."})
        'Explain this function.'
        >>> print(materialize_task_prompt({"prompt": "Review.", "sub_questions": [{"prompt": "Any risks?"}]}))
        Review.
        <BLANKLINE>
        Answer every review question:
        1. Any risks?
    """
    prompt = task.get("prompt")
    if not isinstance(prompt, str):
        raise ValueError("task prompt must be a string")
    sub_questions = task.get("sub_questions")
    if sub_questions is None:
        return prompt
    if not isinstance(sub_questions, list):
        raise ValueError("task sub_questions must be a list when present")
    questions: list[str] = []
    for index, sub_question in enumerate(sub_questions, start=1):
        if not isinstance(sub_question, Mapping):
            raise ValueError("task sub_questions entries must be objects")
        question = sub_question.get("prompt")
        if not isinstance(question, str) or not question:
            raise ValueError(f"task sub_question {index} prompt must be a non-empty string")
        questions.append(f"{index}. {question}")
    if not questions:
        return prompt
    return f"{prompt}\n\nAnswer every review question:\n" + "\n".join(questions)


def token_accounting_inconsistent(input_tokens: int, cached_input_tokens: int) -> bool:
    """Return whether native cached input exceeds gross input.

    Gross and cached counts are retained as provider-native evidence. A cache count above gross is internally
    contradictory, so derived token metrics must remain unscoreable rather than being coerced into a plausible value.

    Raise ``ValueError`` for negative counts, booleans, or non-integer values.

    Examples:
        >>> token_accounting_inconsistent(10, 10)
        False
        >>> token_accounting_inconsistent(10, 11)
        True
        >>> token_accounting_inconsistent(True, 0)
        Traceback (most recent call last):
        ...
        ValueError: input_tokens must be a non-negative integer token count
    """
    for name, value in (("input_tokens", input_tokens), ("cached_input_tokens", cached_input_tokens)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer token count")
    return cached_input_tokens > input_tokens


def fresh_input_tokens(input_tokens: int, cached_input_tokens: int) -> int | None:
    """Return fresh input tokens, or ``None`` for contradictory native usage.

    Native providers report gross input and a cached subset. The raw values remain available for diagnosis; this derived
    metric is deliberately absent when the cache exceeds gross so it cannot enter token comparisons.

    Both counts must be non-negative integers, excluding booleans; invalid
    counts raise ``ValueError`` through :func:`token_accounting_inconsistent`.

    Examples:
        >>> fresh_input_tokens(100, 40)
        60
        >>> fresh_input_tokens(100, 100)
        0
        >>> fresh_input_tokens(10, 11) is None
        True
    """
    if token_accounting_inconsistent(input_tokens, cached_input_tokens):
        return None
    return input_tokens - cached_input_tokens


def canonical_arm(arm: str) -> str:
    """Return the canonical name of a benchmark arm, resolving pre-alignment Codex names.

    Args:
        arm: An arm name as recorded in a result row, canonical or legacy.

    Returns:
        The canonical arm name shared by every provider.

    Raises:
        ValueError: If the name is neither canonical nor a known legacy name.

    Examples:
        >>> canonical_arm("B_direct_required")
        'B_auto'
        >>> canonical_arm("C_strict")
        'C_strict'
    """
    if arm in CANONICAL_ARM_NAMES:
        return arm
    try:
        return LEGACY_ARM_ALIASES[arm]
    except KeyError as exc:
        raise ValueError(f"unknown benchmark arm {arm!r}") from exc


def treatment_adherence(
    arm: str,
    *,
    codemap_use_compliance: bool | None,
    contaminated: bool,
) -> bool:
    """Return whether a treatment arm followed its assigned availability rule."""
    arm = canonical_arm(arm)
    if contaminated:
        return False
    if arm == "A_plain":
        return codemap_use_compliance is None
    if arm in _OPTIONAL_USE_ARMS:
        return True
    return codemap_use_compliance is True


def canonical_result_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    task_order: Sequence[str],
    arm_order: Sequence[str],
) -> list[dict[str, Any]]:
    """Return a derived task/repetition/arm ordered view of raw result rows.

    The caller retains its raw append-only stream in execution order.  This function only constructs a canonical
    analysis sidecar and rejects ambiguous coordinates instead of silently selecting evidence.
    """
    task_rank = {task_id: index for index, task_id in enumerate(task_order)}
    arm_rank = {arm: index for index, arm in enumerate(arm_order)}
    if len(task_rank) != len(task_order) or len(arm_rank) != len(arm_order):
        raise ValueError("canonical result task and arm order values must be unique")
    coordinates: set[tuple[str, int, str]] = set()
    normalized: list[dict[str, Any]] = []
    for row in rows:
        task_id = row.get("task_id")
        arm = row.get("arm")
        repetition = row.get("repetition")
        if task_id not in task_rank:
            raise ValueError(f"unknown task coordinate {task_id!r}")
        if arm not in arm_rank:
            raise ValueError(f"unknown arm coordinate {arm!r}")
        if isinstance(repetition, bool) or not isinstance(repetition, int) or repetition < 1:
            raise ValueError("result repetition must be a positive integer")
        coordinate = (task_id, repetition, arm)
        if coordinate in coordinates:
            raise ValueError(f"duplicate canonical result coordinate {coordinate!r}")
        coordinates.add(coordinate)
        normalized.append(dict(row))
    return sorted(
        normalized,
        key=lambda row: (task_rank[row["task_id"]], row["repetition"], arm_rank[row["arm"]]),
    )


def semantic_suite_hash(tasks: Sequence[Task]) -> str:
    """Hash ordered task contracts while excluding suite wrapper metadata.

    The versioned payload contains each task ID, canonical task hash, and exact
    prompt hash. Repository path aliases and other root-wrapper fields are
    intentionally excluded because target and index identity are validated
    separately.

    Args:
        tasks: Raw task objects in their declared execution order.

    Returns:
        Hexadecimal SHA-256 digest of the versioned ordered contract rows.

    Raises:
        ValueError: If a task ID is missing, empty, or duplicated.
    """
    rows: list[dict[str, str]] = []
    task_ids: set[str] = set()
    for task in tasks:
        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("semantic suite tasks require a non-empty id")
        if task_id in task_ids:
            raise ValueError(f"semantic suite contains duplicate task id {task_id!r}")
        task_ids.add(task_id)
        rows.append(
            {
                "id": task_id,
                "canonical_task_sha256": canonical_task_hash(task),
                "prompt_sha256": prompt_hash(task),
            }
        )
    payload = {"schema": "provider-parity-semantic-suite-v1", "tasks": rows}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _arm_order_digest(coordinates: tuple[str, ...], arm: str) -> bytes:
    """Return the sort digest that places one arm within a paired experiment block.

    Args:
        coordinates: Revision-bound block coordinates shared by every arm in the block.
        arm: Arm label being ranked.

    Returns:
        The SHA-256 digest of the pipe-joined coordinates plus the arm label.

    Examples:
        >>> _arm_order_digest(("rev1", "claude"), "A_plain").hex()[:8]
        'b5e864b5'
        >>> _arm_order_digest(("rev1", "claude"), "A_plain") == _arm_order_digest(("rev1", "claude"), "B_auto")
        False
    """
    payload = "|".join((*coordinates, arm)).encode("utf-8")
    return hashlib.sha256(payload).digest()


def deterministic_arm_order(
    experiment_revision: str,
    provider: str,
    model: str,
    task_id: str,
    repetition: int,
    *,
    reasoning_effort: str = "",
) -> tuple[str, ...]:
    """Return the revision-bound arm order for one paired experiment block.

    The returned arm names are the ones the named provider actually runs. Returning Claude's arm names for every
    provider left the Codex lane with no shared ordering to reuse, which is why it carries its own reimplementation.
    """
    if repetition < 1:
        raise ValueError("repetition must be at least 1")
    base_coordinates = (experiment_revision, provider, model, task_id, str(repetition))
    if any(not coordinate for coordinate in base_coordinates):
        raise ValueError("arm-order coordinates must be non-empty")
    arms = COMPARISON_ARMS_BY_PROVIDER.get(provider)
    if arms is None:
        raise ValueError(f"unknown provider {provider!r}")
    coordinates = (
        (experiment_revision, provider, model, reasoning_effort, task_id, str(repetition))
        if reasoning_effort
        else base_coordinates
    )

    return tuple(sorted(sorted(arms), key=partial(_arm_order_digest, coordinates)))


#: Codemap capability stratum labels assigned to a task by its type.
_CAPABILITY_BY_TASK_TYPE = {
    "develop_blast_radius": ("direct_reverse_call",),
    "fn_call_graph": ("direct_reverse_call",),
    "module_blast_radius": ("direct_reverse_import",),
    "graph_fn_blast": ("transitive_reverse_call",),
    "graph_path": ("dependency_path",),
    "graph_central": ("graph_centrality",),
    "diff_impact": ("diff_impact", "test_selection"),
}
#: Caller or importer count in a task's ground truth at which it also gets the high_fan_in stratum.
_HIGH_FAN_IN_MINIMUM = 16


def capability_strata(task: Task) -> tuple[str, ...]:
    """Return named structural capabilities exercised by one shared task."""
    explicit = task.get("capability_strata")
    if explicit is not None:
        if not isinstance(explicit, list) or not all(isinstance(item, str) and item for item in explicit):
            raise ValueError("task capability_strata must be a list of non-empty strings")
        return tuple(dict.fromkeys(explicit))

    task_type = task.get("type")
    strata = list(_CAPABILITY_BY_TASK_TYPE.get(task_type, ()))
    ground_truth = task.get("ground_truth")
    if isinstance(ground_truth, Mapping):
        fan_in = max(
            (
                len(value)
                for key, value in ground_truth.items()
                if key in {"fn_callers", "blast_callers", "importers"} and isinstance(value, list)
            ),
            default=0,
        )
        if fan_in >= _HIGH_FAN_IN_MINIMUM:
            strata.append("high_fan_in")
    return tuple(strata)


@dataclass(frozen=True)
class EvaluationResult:
    """Provider-neutral evaluation outcome for one task response."""

    scored: bool
    correct: bool
    quality_score: float | None
    extraction_failed: bool = False
    components: dict[str, float] = dataclass_field(default_factory=dict)


class EvaluatorRegistry:
    """Dispatch approved scoreable task types to shared evaluators."""

    def __init__(self, evaluators: Mapping[str, Evaluator]) -> None:
        """Store the explicitly approved evaluator mapping.

        Args:
            evaluators: Task-type keys and their provider-neutral evaluators.

        Raises:
            ValueError: If a task-type key is invalid or an evaluator is not callable.
        """
        self._evaluators: dict[str, Evaluator] = {}
        for task_type, evaluator in evaluators.items():
            if not isinstance(task_type, str) or not task_type:
                raise ValueError("evaluator task types must be non-empty strings")
            if not callable(evaluator):
                raise ValueError(f"evaluator for {task_type!r} must be callable")
            self._evaluators[task_type] = evaluator

    def evaluate(self, task: Task, output_text: str) -> EvaluationResult:
        """Evaluate one response without provider-specific dispatch.

        Args:
            task: Raw task definition including scoreability and type metadata.
            output_text: Provider response text to score.

        Returns:
            The registered evaluator's provider-neutral result, or an unscored result.

        Raises:
            ValueError: If a scoreable task type is absent from the registry.
            TypeError: If a registered evaluator returns the wrong result type.
        """
        if task.get("scoreable", True) is False:
            return EvaluationResult(scored=False, correct=False, quality_score=None)

        task_type = task.get("type")
        evaluator = self._evaluators.get(task_type) if isinstance(task_type, str) else None
        if evaluator is None:
            raise ValueError(f"unknown evaluator for scoreable task type {task_type!r}")
        result = evaluator(task, output_text)
        if not isinstance(result, EvaluationResult):
            raise TypeError(f"evaluator for {task_type!r} must return EvaluationResult")
        return result


@dataclass(frozen=True)
class ResultRecord:
    """One provider-arm result with the coordinates required for pairing."""

    revision: str
    provider: str
    model: str
    task_id: str
    repetition: int
    arm: str
    input_tokens: int
    quality_score: float | None
    treatment_adherence: bool
    cached_input_tokens: int = 0
    fresh_input_tokens: int | None = None
    token_accounting_inconsistent: bool = False
    scoreable: bool = True
    self_consistency: bool = False
    approximate: bool = False
    static_reference: bool = False
    incomplete: bool = False
    extraction_failed: bool = False
    contaminated: bool = False
    diagnostic_only: bool = False


@dataclass(frozen=True)
class PairEffect:
    """One within-provider/model treatment effect for a task repetition."""

    revision: str
    provider: str
    model: str
    task_id: str
    repetition: int
    baseline_arm: str
    treatment_arm: str
    log_input_token_ratio: float
    quality_delta: float


def result_eligibility(record: ResultRecord, policies: Mapping[str, TaskPolicy]) -> bool:
    """Return whether a result may enter headline quality pairing.

    Args:
        record: One provider-arm benchmark result.
        policies: Revision-bound task policy loaded from the locked manifest.

    Returns:
        True only for a complete independent score with a quality value.

    Raises:
        ValueError: If the task or experiment revision has no matching policy.
    """
    policy = _task_policy(record, policies)
    return (
        policy.scoreable
        and policy.headline_eligible_v1
        and record.scoreable
        and record.quality_score is not None
        and not record.self_consistency
        and not record.approximate
        and not record.static_reference
        and not record.incomplete
        and not record.extraction_failed
        and not record.contaminated
        and not record.diagnostic_only
        and record.treatment_adherence is True
        and not record.token_accounting_inconsistent
        and not token_accounting_inconsistent(record.input_tokens, record.cached_input_tokens)
    )


def pair_effects(
    records: Sequence[ResultRecord],
    *,
    baseline_arm: str,
    treatment_arm: str,
    policies: Mapping[str, TaskPolicy],
) -> list[PairEffect]:
    """Construct complete, repetition-preserving paired treatment effects.

    Args:
        records: Provider-arm records from one or more provider/model blocks.
        baseline_arm: Registered arm used as the within-block baseline.
        treatment_arm: Registered arm compared with the baseline.
        policies: Revision-bound task policy loaded from the locked manifest.

    Returns:
        One effect for each complete revision/provider/model/task/repetition block.

    Raises:
        ValueError: If arms, records, cells, token counts, or quality scores are invalid.
    """
    _validate_comparison_arms(baseline_arm, treatment_arm)
    cells: dict[tuple[str, str, str, str, int], dict[str, ResultRecord]] = {}
    for record in records:
        _validate_record_arm(record)
        if record.arm not in (baseline_arm, treatment_arm):
            continue
        _validate_pair_record(record, policies)
        coordinates = (record.revision, record.provider, record.model, record.task_id, record.repetition)
        arm_cells = cells.setdefault(coordinates, {})
        if record.arm in arm_cells:
            raise ValueError(f"duplicate {record.arm} cell for pair coordinates {coordinates!r}")
        arm_cells[record.arm] = record

    effects: list[PairEffect] = []
    for coordinates in sorted(cells):
        arm_cells = cells[coordinates]
        if set(arm_cells) != {baseline_arm, treatment_arm}:
            raise ValueError(f"missing paired cell for coordinates {coordinates!r}")
        baseline = arm_cells[baseline_arm]
        treatment = arm_cells[treatment_arm]
        effects.append(
            PairEffect(
                revision=baseline.revision,
                provider=baseline.provider,
                model=baseline.model,
                task_id=baseline.task_id,
                repetition=baseline.repetition,
                baseline_arm=baseline_arm,
                treatment_arm=treatment_arm,
                log_input_token_ratio=math.log(treatment.input_tokens / baseline.input_tokens),
                quality_delta=float(treatment.quality_score) - float(baseline.quality_score),
            )
        )
    return effects


def _validate_comparison_arms(baseline_arm: str, treatment_arm: str) -> None:
    """Reject invalid, degenerate, or cross-experiment treatment comparisons."""
    for arm in (baseline_arm, treatment_arm):
        if arm not in COMPARISON_ARM_NAMES:
            raise ValueError(f"unknown benchmark arm {arm!r}")
    if baseline_arm == treatment_arm:
        raise ValueError("baseline and treatment arms must differ")
    requested_arms = frozenset({baseline_arm, treatment_arm})
    if not any(requested_arms <= provider_arms for provider_arms in COMPARISON_ARMS_BY_PROVIDER.values()):
        raise ValueError(f"benchmark arms {baseline_arm!r} and {treatment_arm!r} do not coexist in one provider")


def _validate_record_arm(record: ResultRecord) -> None:
    """Reject a result whose arm belongs to another provider's experiment."""
    provider_arms = COMPARISON_ARMS_BY_PROVIDER.get(record.provider)
    if provider_arms is None:
        raise ValueError(f"unknown benchmark provider {record.provider!r}")
    if record.arm not in provider_arms:
        raise ValueError(f"benchmark arm {record.arm!r} is not valid for provider {record.provider!r}")


def _task_policy(record: ResultRecord, policies: Mapping[str, TaskPolicy]) -> TaskPolicy:
    """Return the exact revision-bound policy for a result or fail closed."""
    policy = policies.get(record.task_id)
    if not isinstance(policy, TaskPolicy) or policy.task_id != record.task_id:
        raise ValueError(f"no locked task policy for {record.task_id!r}")
    if record.revision != policy.experiment_revision:
        raise ValueError(
            f"result revision {record.revision!r} does not match task policy {policy.experiment_revision!r}"
        )
    return policy


def _validate_pair_record(record: ResultRecord, policies: Mapping[str, TaskPolicy]) -> None:
    """Reject an ineligible or mathematically invalid pair cell."""
    _validate_pair_token_accounting(record)
    if not isinstance(record.treatment_adherence, bool):
        raise ValueError("result treatment_adherence must be a boolean")
    if not result_eligibility(record, policies):
        raise ValueError(f"ineligible result for task {record.task_id!r}")
    for field in ("revision", "provider", "model", "task_id"):
        if not isinstance(getattr(record, field), str) or not getattr(record, field):
            raise ValueError(f"result {field} must be a non-empty string")
    if isinstance(record.repetition, bool) or not isinstance(record.repetition, int) or record.repetition < 1:
        raise ValueError("result repetition must be a positive integer")
    if isinstance(record.input_tokens, bool) or not isinstance(record.input_tokens, int) or record.input_tokens < 1:
        raise ValueError("result input_tokens must be a positive integer")
    quality_score = record.quality_score
    if (
        isinstance(quality_score, bool)
        or not isinstance(quality_score, (int, float))
        or not math.isfinite(quality_score)
        or not 0.0 <= quality_score <= 1.0
    ):
        raise ValueError("result quality_score must be a finite value in [0, 1]")


def _validate_pair_token_accounting(record: ResultRecord) -> None:
    """Reject stale flags or derived values that disagree with native token counts."""
    inconsistent = token_accounting_inconsistent(record.input_tokens, record.cached_input_tokens)
    if not isinstance(record.token_accounting_inconsistent, bool):
        raise ValueError("result token_accounting_inconsistent must be a boolean")
    if record.token_accounting_inconsistent is not inconsistent:
        raise ValueError("result token_accounting_inconsistent flag disagrees with native token counts")
    if record.fresh_input_tokens is None:
        if inconsistent:
            raise ValueError("result token accounting is inconsistent: cached input exceeds gross input")
        return
    if isinstance(record.fresh_input_tokens, bool) or not isinstance(record.fresh_input_tokens, int):
        raise ValueError("result fresh_input_tokens must be an integer or None")
    expected_fresh = record.input_tokens - record.cached_input_tokens
    if inconsistent or record.fresh_input_tokens != expected_fresh:
        raise ValueError("result fresh_input_tokens disagrees with native token counts")
