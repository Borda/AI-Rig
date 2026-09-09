"""Score labelled agentic answers against an independent AST oracle.

The module deliberately contains the provider-neutral answer contract only. Provider runners own transport, JSON
extraction, and legacy result rendering; they pass an already parsed answer mapping to :func:`score_answer`.
"""

from __future__ import annotations

import ast
from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass, field as dataclass_field
from functools import lru_cache
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any

from .provider_parity_contracts import materialize_task_prompt
from .python_source import extract_import_targets


AGENTIC_ARMS = ("A_plain", "B_auto", "C_strict")
DEFAULT_REPETITIONS = 1

# The oracle's single import-resolution convention, stated to the model in every scored
# prompt. Without it an honest answer under an equally defensible reading (crediting only
# the concrete submodule, or only the package) loses points for a disclosure gap rather
# than for being wrong.
IMPORT_CONVENTION_INSTRUCTION = (
    "Import convention: a `from a.b import c` statement counts as importing the package `a.b`, "
    "and additionally the submodule `a.b.c` when `a.b.c` is itself a module in this repository. "
    "`import a.b` counts as importing `a.b`. Only modules that exist in this repository count; "
    "third-party and standard-library imports are ignored."
)
ORACLE_POPULATION_INSTRUCTION = (
    "Oracle population, naming, and ranking: use only statically resolved imports between repository Python modules. "
    "A module name is its repository-relative dotted path after a leading `src` layout directory is stripped and a "
    "terminal `__init__` is removed, so `a/b/__init__.py` is `a.b`, never `a.b.__init__`. A module inside a package is "
    "instead named from the outermost directory of its `__init__.py` chain, which is the only name that resolves at "
    "runtime: where `a/b/c/` is the outermost directory carrying an `__init__.py`, `a/b/c/d.py` is `c.d`, and the "
    "`import c.d` its neighbours write is credited to it. A test module has any "
    "source-path component (including its filename stem) equal "
    "to `test` or `tests`, or beginning with `test_` or `tests_`. Production direct importers exclude test modules and "
    "the target module itself; report test importers only in the explicitly requested test-count fields. Every rdep count "
    "counts non-test direct importers only and excludes the counted module itself. Deduplicate module names. For rankings, "
    "apply the requested candidate filter, sort by rdep count descending and then full dotted module name ascending to "
    "break ties, and return the requested leading positions."
)

_ANSWER_FIELDS = frozenset(
    {
        "production_importers",
        "rdep_counts",
        "ranking",
        "buckets",
        "overlap_importers",
        "overlap_count",
        "cross_namespace_importers",
        "dependency_chain",
        "affected_module_count",
        "production_importer_count",
        "excluded_test_importer_count",
        "test_importer_count",
        "isolation_verdict",
        "risk_tier",
        "high_centrality",
    }
)
_PARAMETERIZED_FIELDS = frozenset(
    {
        "ranking",
        "buckets",
        "overlap_importers",
        "overlap_count",
        "cross_namespace_importers",
        "dependency_chain",
        "affected_module_count",
        "high_centrality",
        "risk_tier",
    }
)
_CANDIDATE_SETS = frozenset({"production_importers", "non_migrated_importers", "helper_dependent_importers"})


@dataclass(frozen=True)
class _SourceModule:
    """One successfully parsed local module used by the independent oracle."""

    name: str
    path: Path
    is_test: bool
    is_package: bool
    imports: frozenset[str]
    names: frozenset[str]


@dataclass(frozen=True)
class AgenticOracle:
    """Independent expected values for one task's declared answer fields."""

    task_id: str
    fields: tuple[str, ...]
    expected: Mapping[str, Any]


@dataclass(frozen=True)
class AnswerScore:
    """Deterministic exact and prospective graded components plus evidence diagnostics.

    ``quality_score``, ``components``, and ``correct`` retain exact historical semantics. ``graded_score`` and
    ``graded_components`` add bounded partial credit for selected count and ranking fields. ``erec`` and ``rrec`` are
    expected-importer recall in the exposure and report text, while ``deff`` is the unbounded exposure-hit count per
    command.
    """

    scored: bool
    quality_score: float
    correct: bool
    components: Mapping[str, float]
    erec: float
    rrec: float
    deff: float
    graded_score: float | None = None
    graded_components: Mapping[str, float] = dataclass_field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True)
class EvidenceMetrics:
    """Raw-text expected-importer recall and unbounded exposure per command."""

    erec: float
    rrec: float
    deff: float


@dataclass(frozen=True)
class AnswerResponseAssessment:
    """Strict-envelope status and optional diagnostic-only semantic answer.

    A recovered answer is intentionally ineligible for pooling: its semantic
    score diagnoses response content, but cannot erase a failed wire contract.
    """

    answer: Mapping[str, Any] | None
    strict_envelope_valid: bool
    diagnostic_only: bool
    pooling_eligible: bool
    error: str | None


def validate_answer_contract(task: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate and return the closed answer contract embedded in one task.

    The suite controls only a bounded list of output fields and their small fixed parameter shapes. Unknown fields or
    knobs fail before a provider can execute a differently scored experiment.
    """
    contract = task.get("answer_contract")
    if not isinstance(contract, Mapping):
        raise ValueError("task answer_contract must be an object")
    fields = contract.get("fields")
    params = contract.get("params", {})
    if not isinstance(fields, list) or not fields or any(not isinstance(field, str) for field in fields):
        raise ValueError("answer_contract fields must be a non-empty string list")
    if len(set(fields)) != len(fields) or any(field not in _ANSWER_FIELDS for field in fields):
        raise ValueError("answer_contract fields contain an unknown or duplicate field")
    if not isinstance(params, Mapping) or any(not isinstance(field, str) for field in params):
        raise ValueError("answer_contract params must be an object")
    if any(field not in fields or field not in _PARAMETERIZED_FIELDS for field in params):
        raise ValueError("answer_contract params are only allowed for declared parameterized fields")
    if "risk_tier" in fields and "high_centrality" not in fields:
        raise ValueError("answer_contract risk_tier requires high_centrality")
    for field in fields:
        if field in _PARAMETERIZED_FIELDS and field not in params:
            raise ValueError(f"answer_contract field {field!r} requires params")
    for field, value in params.items():
        _validate_field_params(field, value)
    return MappingProxyType({"fields": tuple(fields), "params": MappingProxyType(dict(params))})


def answer_format_instruction(task: Mapping[str, Any]) -> str:
    """Return the exact typed JSON envelope required for one task response."""
    contract = validate_answer_contract(task)
    labels = ", ".join(contract["fields"])
    specs: list[str] = []
    example: dict[str, Any] = {}
    for field in contract["fields"]:
        description, example_value = _answer_field_spec(field, contract["params"].get(field, {}))
        specs.append(f"- {field}: {description}.")
        example[field] = example_value
    return "\n".join(
        [
            f"Return one JSON object containing exactly these labels: {labels}.",
            "Use exactly these JSON value shapes:",
            *specs,
            IMPORT_CONVENTION_INSTRUCTION,
            ORACLE_POPULATION_INSTRUCTION,
            "Run temporary analysis in memory or through interpreter stdin; do not create helper files in the "
            "repository or tool coordination/lock directories. Writable coordination storage is reserved for the "
            "tool protocol, not scratch work. Use deterministic code for counts, set filtering and ranking; "
            "check any reported count against the corresponding complete collection.",
            "Do not put objects or counts inside array fields. Values outside these shapes are invalid.",
            # The delimiters used to appear only inside the example below, which is labelled
            # synthetic — so nothing told a model to wrap its own answer. Codex read that literally
            # and returned bare JSON in 12 of 48 cells on 2026-09-06 while Claude lost 1 of 144.
            "Wrap your answer between a BEGIN_ANSWER_JSON line and an END_ANSWER_JSON line, "
            "exactly once, with the JSON object alone between them.",
            "Example using synthetic values only:",
            "BEGIN_ANSWER_JSON",
            json.dumps(example, separators=(",", ":")),
            "END_ANSWER_JSON",
        ]
    )


def materialize_agentic_prompt(task: Mapping[str, Any]) -> str:
    """Return shared task bytes plus the scored labelled-answer instruction."""
    prompt = materialize_task_prompt(task)
    if "answer_contract" not in task:
        return prompt
    return f"{prompt}\n\n{answer_format_instruction(task)}"


def parse_labeled_answer(task: Mapping[str, Any], text: str) -> dict[str, Any]:
    """Parse the one exact labelled JSON answer required by a task contract.

    The ``EMPTY`` marker is accepted only for collection fields and converted to the corresponding explicit JSON
    collection. Missing or additional labels fail closed, so a runner never scores partial prose as an answer.
    """
    contract = validate_answer_contract(task)
    if not isinstance(text, str):
        raise TypeError("labelled answer text must be a string")
    start = "BEGIN_ANSWER_JSON"
    end = "END_ANSWER_JSON"
    if text.count(start) != 1 or text.count(end) != 1:
        raise ValueError("answer requires exactly one BEGIN_ANSWER_JSON and END_ANSWER_JSON envelope")
    _, payload = text.split(start, maxsplit=1)
    payload, _ = payload.split(end, maxsplit=1)
    if start in payload or end in payload or not payload.strip():
        raise ValueError("answer JSON envelope is malformed")
    payload = payload.strip()
    # Tolerate one cosmetic markdown code fence wrapping the payload (``` or ```json).
    # Some providers fence JSON inside the envelope by habit; the BEGIN/END markers stay
    # the contract and anything beyond a plain fence still fails closed at json.loads.
    if payload.startswith("```"):
        fence_lines = payload.splitlines()
        if len(fence_lines) < 2 or fence_lines[-1].strip() != "```":
            raise ValueError("answer markdown code fence is unclosed")
        payload = "\n".join(fence_lines[1:-1])
    try:
        answer = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"answer JSON is invalid: {exc.msg}") from exc
    return _normalize_answer(contract, answer)


def assess_answer_response(task: Mapping[str, Any], text: str) -> AnswerResponseAssessment:
    """Keep strict envelope validity separate from recoverable diagnostic semantics.

    Only one complete bare JSON object can recover semantic scoring after a strict-envelope failure. Recovered answers
    are explicitly diagnostic-only and cannot enter pooled treatment comparisons.
    """
    try:
        answer = parse_labeled_answer(task, text)
    except ValueError as strict_error:
        try:
            answer = _recover_unique_bare_answer(task, text)
        except ValueError as recovery_error:
            return AnswerResponseAssessment(
                answer=None,
                strict_envelope_valid=False,
                diagnostic_only=False,
                pooling_eligible=False,
                error=f"{strict_error}; diagnostic recovery rejected: {recovery_error}",
            )
        return AnswerResponseAssessment(
            answer=MappingProxyType(answer),
            strict_envelope_valid=False,
            diagnostic_only=True,
            pooling_eligible=False,
            error=str(strict_error),
        )
    return AnswerResponseAssessment(
        answer=MappingProxyType(answer),
        strict_envelope_valid=True,
        diagnostic_only=False,
        pooling_eligible=True,
        error=None,
    )


def _recover_unique_bare_answer(task: Mapping[str, Any], text: str) -> dict[str, Any]:
    """Recover one complete bare JSON object without weakening the strict parser."""
    if not isinstance(text, str):
        raise TypeError("labelled answer text must be a string")
    payload = text.strip()
    decoder = json.JSONDecoder()
    try:
        answer, end = decoder.raw_decode(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"bare JSON is invalid: {exc.msg}") from exc
    if payload[end:].strip():
        raise ValueError("diagnostic recovery requires exactly one bare JSON object")
    return _normalize_answer(validate_answer_contract(task), answer)


def _normalize_answer(contract: Mapping[str, Any], answer: Any) -> dict[str, Any]:
    """Validate closed labels and normalize the explicit collection-empty marker."""
    if not isinstance(answer, dict):
        raise ValueError("answer JSON must be an object")
    fields = set(contract["fields"])
    if set(answer) != fields:
        missing = sorted(fields - set(answer))
        extra = sorted(set(answer) - fields)
        raise ValueError(f"answer labels differ from contract; missing={missing}, extra={extra}")
    for field, value in tuple(answer.items()):
        if value == "EMPTY":
            if field in {
                "production_importers",
                "overlap_importers",
                "cross_namespace_importers",
                "ranking",
                "dependency_chain",
            }:
                answer[field] = []
            elif field in {"rdep_counts", "buckets", "high_centrality"}:
                answer[field] = {}
            else:
                raise ValueError(f"answer field {field!r} does not accept EMPTY")
    _validate_answer_shapes(contract, answer)
    return answer


def build_oracle(task: Mapping[str, Any], source_root: Path) -> AgenticOracle:
    """Build expected answer values from a source-only, provider-independent AST scan.

    Parse failures and dynamic imports never enter the graph. Production direct importers exclude test modules and the
    target module itself, while test importers remain available for explicitly requested count fields.
    """
    contract = validate_answer_contract(task)
    task_id = task.get("id")
    primary_module = task.get("primary_module")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("task id must be a non-empty string")
    if not isinstance(primary_module, str) or not primary_module:
        raise ValueError("task primary_module must be a non-empty string")
    modules = _scan_modules(Path(source_root))
    reverse_imports = _reverse_imports(modules)
    production = tuple(
        sorted(
            name
            for name in reverse_imports.get(primary_module, set())
            if not modules[name].is_test and name != primary_module
        )
    )
    tests = tuple(
        sorted(
            name
            for name in reverse_imports.get(primary_module, set())
            if modules[name].is_test and name != primary_module
        )
    )
    production_rdeps = {
        name: len(
            [
                importer
                for importer in reverse_imports.get(name, set())
                if not modules[importer].is_test and importer != name
            ]
        )
        for name in production
    }
    expected = _expected_values(
        task,
        contract,
        modules,
        reverse_imports,
        production,
        tests,
        production_rdeps,
    )
    return AgenticOracle(task_id=task_id, fields=contract["fields"], expected=MappingProxyType(expected))


def score_answer(
    oracle: AgenticOracle,
    answer: Mapping[str, Any],
    *,
    exposure_text: str = "",
    report_text: str = "",
    tool_calls: int = 0,
) -> AnswerScore:
    """Score a parsed labelled answer with fixed component and tie rules.

    ``quality_score``, ``components``, and ``correct`` retain their exact compatibility rules. ``graded_score`` adds
    proportional count credit and ordered ranking subsequence credit, while set, categorical, and path fields retain
    their established grading rules. ``erec`` and ``rrec`` remain raw-text expected-importer recall; ``deff`` is
    unbounded exposure hits per command. Empty expected collections receive credit only from an explicit empty
    collection.
    """
    if not isinstance(answer, Mapping):
        raise TypeError("answer must be a mapping parsed from the labelled JSON envelope")
    components: dict[str, float] = {}
    graded_components: dict[str, float] = {}
    for field in oracle.fields:
        expected = oracle.expected[field]
        actual = answer.get(field)
        if field in {"production_importers", "overlap_importers", "cross_namespace_importers"}:
            components[field] = _set_f1(expected, actual)
            graded_components[field] = components[field]
        elif field in {"rdep_counts", "high_centrality"}:
            components[field] = _mapping_fraction(expected, actual)
            graded_components[field] = _graded_count_mapping(expected, actual)
        elif field == "buckets":
            components[field] = _mapping_fraction(expected, actual)
            graded_components[field] = components[field]
        elif field == "ranking":
            components[field] = _ranking_fraction(expected, actual)
            graded_components[field] = _graded_ranking_fraction(expected, actual)
        elif field in {
            "affected_module_count",
            "production_importer_count",
            "excluded_test_importer_count",
            "test_importer_count",
            "overlap_count",
        }:
            components[field] = 1.0 if _same_value(expected, actual) else 0.0
            graded_components[field] = _graded_count_fraction(expected, actual)
        else:
            components[field] = 1.0 if _same_value(expected, actual) else 0.0
            graded_components[field] = components[field]
    evidence = score_evidence_metrics(
        oracle, exposure_text=exposure_text, report_text=report_text, tool_calls=tool_calls
    )
    # Unweighted mean over declared fields. Enum and small-cardinality fields therefore
    # carry a guessable floor (a blind pick on `risk_tier` scores 0.25 of its component,
    # `isolation_verdict` 0.5), so absolute aqs levels sit above true zero-work. The floor
    # is identical in every arm, so between-arm aqs deltas are unaffected; only the
    # absolute level is inflated, and README reports it with that caveat.
    quality_score = sum(components.values()) / len(components)
    graded_score = sum(graded_components.values()) / len(graded_components)
    return AnswerScore(
        scored=True,
        quality_score=quality_score,
        correct=all(component == 1.0 for component in components.values()),
        components=MappingProxyType(components),
        erec=evidence.erec,
        rrec=evidence.rrec,
        deff=evidence.deff,
        graded_score=graded_score,
        graded_components=MappingProxyType(graded_components),
    )


def answer_failure_details(oracle: AgenticOracle, answer: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return deterministic, bounded semantic mismatches for one parsed answer.

    The details explain only answer-content differences. Envelope parsing, execution, and treatment status stay with the
    provider runner. Missing mapping keys are missing facts rather than wrong counts; count disagreements are reported
    only when both answers name the same key.
    """
    if not isinstance(answer, Mapping):
        raise TypeError("answer must be a mapping parsed from the labelled JSON envelope")
    details: list[dict[str, Any]] = []
    for field in oracle.fields:
        expected = oracle.expected[field]
        actual = answer.get(field)
        if field in {"production_importers", "overlap_importers", "cross_namespace_importers"}:
            details.extend(_collection_failure_details(field, expected, actual))
        elif field in {"rdep_counts", "high_centrality"}:
            details.extend(_mapping_failure_details(field, expected, actual))
        elif field == "buckets":
            details.extend(_bucket_failure_details(field, expected, actual))
        elif field == "ranking":
            details.extend(_ranking_failure_details(field, expected, actual))
        elif field in {
            "affected_module_count",
            "production_importer_count",
            "excluded_test_importer_count",
            "test_importer_count",
            "overlap_count",
        }:
            if not _same_value(expected, actual):
                details.append(_failure_detail("wrong_counts", field, expected, actual))
        elif not _same_value(expected, actual):
            details.append(_failure_detail("wrong_values", field, expected, actual))
    return details


def _collection_failure_details(field: str, expected: Any, actual: Any) -> list[dict[str, Any]]:
    """Describe missing and unexpected members of one set-scored answer field."""
    if not isinstance(actual, list) or not all(isinstance(item, str) for item in actual):
        return [_failure_detail("wrong_values", field, expected, actual)]
    expected_values = set(expected)
    actual_values = set(actual)
    details: list[dict[str, Any]] = []
    missing = sorted(expected_values - actual_values)
    if missing:
        details.append(_failure_detail("missing_facts", field, missing, []))
    unexpected = sorted(actual_values - expected_values)
    if unexpected:
        details.append(_failure_detail("unexpected_facts", field, [], unexpected))
    return details


def _mapping_failure_details(field: str, expected: Any, actual: Any) -> list[dict[str, Any]]:
    """Describe absent, extra, and mismatched entries of one mapping-scored field."""
    if not isinstance(expected, Mapping) or not isinstance(actual, Mapping):
        return [_failure_detail("wrong_values", field, expected, actual)]
    expected_keys = set(expected)
    actual_keys = set(actual)
    details: list[dict[str, Any]] = []
    missing = sorted(expected_keys - actual_keys)
    if missing:
        details.append(_failure_detail("missing_facts", field, _selected_mapping(expected, missing), {}))
    unexpected = sorted(actual_keys - expected_keys)
    if unexpected:
        details.append(_failure_detail("unexpected_facts", field, {}, _selected_mapping(actual, unexpected)))
    wrong = sorted(key for key in expected_keys & actual_keys if not _same_value(expected[key], actual[key]))
    if wrong:
        details.append(
            _failure_detail("wrong_counts", field, _selected_mapping(expected, wrong), _selected_mapping(actual, wrong))
        )
    return details


def _bucket_failure_details(field: str, expected: Any, actual: Any) -> list[dict[str, Any]]:
    """Describe bucket-label and bucket-member mismatches without conflating them with counts."""
    if not isinstance(expected, Mapping) or not isinstance(actual, Mapping):
        return [_failure_detail("wrong_values", field, expected, actual)]
    expected_labels = set(expected)
    actual_labels = set(actual)
    details: list[dict[str, Any]] = []
    missing_labels = sorted(expected_labels - actual_labels)
    if missing_labels:
        details.append(_failure_detail("missing_facts", field, _selected_mapping(expected, missing_labels), {}))
    unexpected_labels = sorted(actual_labels - expected_labels)
    if unexpected_labels:
        details.append(_failure_detail("unexpected_facts", field, {}, _selected_mapping(actual, unexpected_labels)))
    for label in sorted(expected_labels & actual_labels):
        details.extend(_collection_failure_details(f"{field}.{label}", expected[label], actual[label]))
    return details


def _ranking_failure_details(field: str, expected: Any, actual: Any) -> list[dict[str, Any]]:
    """Describe misplaced and extra ranking entries while preserving legacy position scores."""
    if not isinstance(actual, list) or not all(isinstance(item, str) for item in actual):
        return [_failure_detail("wrong_values", field, expected, actual)]
    details = [
        _failure_detail(
            "wrong_rankings",
            f"{field}[{index}]",
            value,
            actual[index] if index < len(actual) else None,
        )
        for index, value in enumerate(expected)
        if index >= len(actual) or actual[index] != value
    ]
    if len(actual) > len(expected):
        details.append(_failure_detail("unexpected_facts", field, [], actual[len(expected) :]))
    return details


def _selected_mapping(values: Mapping[str, Any], keys: list[str]) -> dict[str, Any]:
    """Return selected mapping values in deterministic key order."""
    return {key: values[key] for key in keys}


def _failure_detail(category: str, field: str, expected: Any, actual: Any) -> dict[str, Any]:
    """Build one JSON-safe semantic mismatch event."""
    return {
        "category": category,
        "field": field,
        "expected": _json_value(expected),
        "actual": _json_value(actual),
    }


def _json_value(value: Any) -> Any:
    """Convert oracle tuples and mappings to deterministic JSON-compatible diagnostic values."""
    if isinstance(value, Mapping):
        return {str(key): _json_value(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


def score_evidence_metrics(
    oracle: AgenticOracle,
    *,
    exposure_text: str = "",
    report_text: str = "",
    tool_calls: int = 0,
) -> EvidenceMetrics:
    """Score raw-text importer evidence independently of answer-envelope validity.

    ``deff`` is deliberately unbounded: it counts expected importer mentions in exposure text per command, so it is
    diagnostic evidence rather than a normalized efficiency or treatment-effect metric.
    """
    if not isinstance(exposure_text, str) or not isinstance(report_text, str):
        raise TypeError("exposure_text and report_text must be strings")
    if isinstance(tool_calls, bool) or not isinstance(tool_calls, int) or tool_calls < 0:
        raise ValueError("tool_calls must be a non-negative integer")
    expected_importers = oracle.expected.get("production_importers", ())
    expected_count = max(len(expected_importers), 1)
    exposure_hits = sum(_mentions_module(exposure_text, name) for name in expected_importers)
    report_hits = sum(_mentions_module(report_text, name) for name in expected_importers)
    return EvidenceMetrics(
        erec=exposure_hits / expected_count,
        rrec=report_hits / expected_count,
        deff=exposure_hits / max(tool_calls, 1),
    )


@lru_cache(maxsize=4096)
def _module_mention_re(name: str) -> re.Pattern[str]:
    """Compile a whole-name matcher for one dotted module name.

    The guards reject only characters that *continue* a dotted identifier, so a module named at the end of a sentence
    still counts. A blanket ``(?![\\w.])`` would drop ``... imports pkg.core.`` purely for its full stop.
    """
    escaped = re.escape(name)
    return re.compile(rf"(?<!\w)(?<!\w\.){escaped}(?!\w)(?!\.\w)")


def _mentions_module(text: str, name: str) -> bool:
    """Return whether ``text`` names ``name`` as a whole dotted module.

    Plain substring containment handed out free evidence credit: an expected importer
    such as ``pkg.core`` scored a hit inside the unrelated ``pkg.core_utils``, and a
    short name embedded in any longer dotted path scored before the model did any
    work.
    """
    return _module_mention_re(name).search(text) is not None


def _answer_field_spec(field: str, params: Mapping[str, Any]) -> tuple[str, Any]:
    """Describe one scored field and provide a visibly synthetic valid value."""
    if field in {"production_importers", "overlap_importers", "cross_namespace_importers"}:
        return "array of full dotted module-name strings", ["pkg.consumer"]
    if field == "ranking":
        top_k = params["top_k"]
        candidate_set = params["candidate_set"]
        qualifiers: list[str] = []
        if "exclude_overlap_targets" in params:
            qualifiers.append(f"excluding modules that also import {params['exclude_overlap_targets']}")
        if "min_rdep_count" in params:
            qualifiers.append(f"including only modules with rdep count at least {params['min_rdep_count']}")
        qualification = f", {' and '.join(qualifiers)}" if qualifiers else ""
        return (
            f"ordered array of at most {top_k} full dotted module-name strings from {candidate_set}{qualification}, sorted by rdep count descending then module name ascending",
            ["pkg.consumer"],
        )
    if field == "dependency_chain":
        return (
            "lexically tie-broken shortest static dependency path as an ordered array of full dotted module-name strings; use [] only when the target is unreachable",
            [
                "pkg.source",
                "pkg.target",
            ],
        )
    if field in {"rdep_counts", "high_centrality"}:
        qualifier = ""
        if field == "high_centrality":
            qualifier = f" for direct production importers with rdep count at least {params['min_rdep_count']}"
        return f"object mapping full dotted module names to non-negative integer rdep counts{qualifier}", {
            "pkg.consumer": 2
        }
    if field == "buckets":
        labels = params["labels"]
        example = {label: ["pkg.consumer"] if index == 0 else [] for index, label in enumerate(labels)}
        return (
            f"object with exactly keys {json.dumps(labels, separators=(',', ':'))}; each value is an array of full dotted module-name strings sorted lexically ascending",
            example,
        )
    if field == "affected_module_count":
        minimum = params["min_rdep_count"]
        return (
            f"exact non-negative integer count of the union of direct production importers and non-test direct importers of those with rdep count at least {minimum}",
            2,
        )
    if field in {"production_importer_count", "excluded_test_importer_count", "test_importer_count", "overlap_count"}:
        return "non-negative integer", 1
    if field == "isolation_verdict":
        return 'string enum "isolated" or "widely-imported"', "isolated"
    if field == "risk_tier":
        return 'string enum "low", "medium", "high", or "critical"', "high"
    raise ValueError(f"unsupported answer field {field!r}")


def _validate_answer_shapes(contract: Mapping[str, Any], answer: Mapping[str, Any]) -> None:
    """Reject values that cannot be scored under the advertised field schema."""
    list_fields = {
        "production_importers",
        "overlap_importers",
        "cross_namespace_importers",
        "ranking",
        "dependency_chain",
    }
    count_fields = {
        "affected_module_count",
        "production_importer_count",
        "excluded_test_importer_count",
        "test_importer_count",
        "overlap_count",
    }
    params = contract["params"]
    for field in contract["fields"]:
        value = answer[field]
        if field in list_fields:
            if (
                not isinstance(value, list)
                or any(not isinstance(item, str) or not item for item in value)
                or len(value) != len(set(value))
            ):
                raise ValueError(f"answer field {field!r} has invalid shape; expected an array of strings")
            if field == "ranking" and len(value) > params[field]["top_k"]:
                raise ValueError(f"answer field {field!r} has invalid shape; exceeds top_k")
        elif field in {"rdep_counts", "high_centrality"}:
            if not isinstance(value, dict) or any(
                not isinstance(name, str)
                or not name
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
                for name, count in value.items()
            ):
                raise ValueError(f"answer field {field!r} has invalid shape; expected string-to-count object")
        elif field == "buckets":
            labels = params[field]["labels"]
            if (
                not isinstance(value, dict)
                or set(value) != set(labels)
                or any(
                    not isinstance(items, list)
                    or any(not isinstance(item, str) or not item for item in items)
                    or len(items) != len(set(items))
                    for items in value.values()
                )
            ):
                raise ValueError(f"answer field {field!r} has invalid shape; expected label-to-string-array object")
        elif field in count_fields:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"answer field {field!r} has invalid shape; expected a non-negative integer")
        elif field == "isolation_verdict" and value not in {"isolated", "widely-imported"}:
            raise ValueError(f"answer field {field!r} has invalid shape; expected an isolation enum")
        elif field == "risk_tier" and value not in {"low", "medium", "high", "critical"}:
            raise ValueError(f"answer field {field!r} has invalid shape; expected a risk-tier enum")


def _validate_field_params(field: str, params: Any) -> None:
    """Reject parameter shapes outside the reviewed closed answer schema."""
    if not isinstance(params, Mapping):
        raise ValueError(f"answer_contract {field} params must be an object")
    keys = set(params)
    if field == "ranking":
        permitted = {"candidate_set", "top_k"}
        if (
            keys - {"candidate_set", "top_k", "exclude_overlap_targets", "min_rdep_count"}
            or not permitted <= keys
            or params["candidate_set"] not in _CANDIDATE_SETS
        ):
            raise ValueError("ranking params require a supported candidate_set and top_k")
        if isinstance(params["top_k"], bool) or not isinstance(params["top_k"], int) or params["top_k"] < 1:
            raise ValueError("ranking top_k must be a positive integer")
        targets = params.get("exclude_overlap_targets")
        if targets is not None and (
            params["candidate_set"] != "non_migrated_importers"
            or not isinstance(targets, list)
            or not targets
            or not all(isinstance(target, str) and target for target in targets)
        ):
            raise ValueError("ranking exclude_overlap_targets is only valid for non_migrated_importers")
        minimum = params.get("min_rdep_count")
        if minimum is not None and (isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0):
            raise ValueError("ranking min_rdep_count must be a non-negative integer")
        return
    if field == "buckets":
        labels = params.get("labels")
        if (
            keys != {"labels"}
            or not isinstance(labels, list)
            or not labels
            or not all(isinstance(label, str) and label for label in labels)
        ):
            raise ValueError("buckets params require non-empty labels")
        return
    if field in {"overlap_importers", "overlap_count", "cross_namespace_importers"}:
        if keys == {"prefix"} and isinstance(params["prefix"], str) and params["prefix"]:
            return
        if (
            field in {"overlap_importers", "overlap_count"}
            and keys == {"targets"}
            and isinstance(params["targets"], list)
            and all(isinstance(target, str) and target for target in params["targets"])
        ):
            return
        raise ValueError(f"{field} params require one non-empty prefix or targets list")
    if field == "dependency_chain":
        if keys == {"source", "target"} and all(isinstance(params[key], str) and params[key] for key in keys):
            return
        raise ValueError("dependency_chain params require source and target")
    if field in {"affected_module_count", "high_centrality"}:
        minimum = params.get("min_rdep_count")
        if keys == {"min_rdep_count"} and not isinstance(minimum, bool) and isinstance(minimum, int) and minimum >= 0:
            return
        raise ValueError(f"{field} params require non-negative min_rdep_count")
    if field == "risk_tier":
        production_minimum = params.get("critical_min_production_importer_count")
        centrality_minimum = params.get("critical_min_high_centrality_count")
        if (
            keys
            == {
                "critical_min_production_importer_count",
                "critical_min_high_centrality_count",
            }
            and not isinstance(production_minimum, bool)
            and isinstance(production_minimum, int)
            and production_minimum >= 0
            and not isinstance(centrality_minimum, bool)
            and isinstance(centrality_minimum, int)
            and centrality_minimum >= 0
        ):
            return
        raise ValueError("risk_tier params require non-negative critical thresholds")
    raise ValueError(f"unsupported answer_contract parameterized field {field!r}")


def _scan_modules(source_root: Path) -> dict[str, _SourceModule]:
    """Parse local Python files once, retaining only statically resolved imports."""
    paths = sorted(
        path
        for path in source_root.rglob("*.py")
        if not any(part.startswith(".") for part in path.relative_to(source_root).parts)
    )
    names_by_path = {path: _module_name(path, source_root) for path in paths}
    names_by_path = {path: name for path, name in names_by_path.items() if name}
    all_names = set(names_by_path.values())
    modules: dict[str, _SourceModule] = {}
    for path, name in names_by_path.items():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        imports = _import_targets(tree, name, path.name == "__init__.py", all_names)
        parts = path.relative_to(source_root).parts
        modules[name] = _SourceModule(
            name=name,
            path=path,
            is_test=any(_is_test_path_part(part) for part in parts),
            is_package=path.name == "__init__.py",
            imports=frozenset(imports - {name}),
            names=frozenset(
                [node.id for node in ast.walk(tree) if isinstance(node, ast.Name)]
                + [node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)]
                + [
                    alias.name
                    for node in ast.walk(tree)
                    if isinstance(node, (ast.Import, ast.ImportFrom))
                    for alias in node.names
                ]
            ),
        )
    return modules


def _module_name(path: Path, source_root: Path) -> str:
    """Return the dotted module name a file is importable under, matching the index the arms query.

    A file inside a package is named from the outermost directory of its ``__init__.py`` chain, which is the only name
    that resolves at runtime and the name Codemap records: ``examples/fabric/rl/agent.py`` under a chain that starts at
    ``rl`` is ``rl.agent``, not its repository-relative path. A file in no package keeps the repository-relative path
    with a leading ``src`` layout directory stripped, so a loose script cannot collapse onto a same-named sibling in
    another directory.

    The two rules coincide for a conventional package under ``src``: ``src/`` holds no ``__init__.py``, so the chain
    stops there and ``src/lightning/pytorch/core/module.py`` is ``lightning.pytorch.core.module`` either way.

    Args:
        path: absolute path to a ``.py`` file inside the scanned tree.
        source_root: repository root the name is derived relative to.

    Returns:
        Dotted module name; an ``__init__.py`` resolves to its package name.
    """
    if (path.parent / "__init__.py").exists():
        parts = [] if path.stem == "__init__" else [path.stem]
        directory = path.parent
        while (directory / "__init__.py").exists() and directory != directory.parent:
            parts.append(directory.name)
            directory = directory.parent
        return ".".join(reversed(parts))
    parts = list(path.relative_to(source_root).with_suffix("").parts)
    if parts[:1] == ["src"]:
        parts.pop(0)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _is_test_path_part(part: str) -> bool:
    """Return whether one source path component belongs to an explicit test root."""
    stem = Path(part).stem
    return stem in {"test", "tests"} or stem.startswith(("test_", "tests_"))


def _import_targets(tree: ast.Module, module: str, is_package: bool, all_names: set[str]) -> set[str]:
    """Resolve static import statements to known local module names only.

    Delegates to the shared :func:`python_source.extract_import_targets` so the agentic oracle credits imports under
    exactly the convention the MB/GR oracles use: a ``from a.b import c`` statement credits the package ``a.b`` *and*
    the submodule ``a.b.c`` whenever either is a known local module. Crediting only the concrete submodule when one
    happens to resolve made package credit all-or-nothing, so a single mixed statement (one submodule alias plus one
    plain symbol) silently dropped the package importer the task existed to find.
    """
    package = module if is_package else module.rpartition(".")[0]
    return extract_import_targets(tree, package=package, keep=all_names, credit_submodules=True)


def _reverse_imports(modules: Mapping[str, _SourceModule]) -> dict[str, set[str]]:
    """Invert statically parsed module imports into a reverse graph."""
    reverse: dict[str, set[str]] = defaultdict(set)
    for module in modules.values():
        for imported in module.imports:
            reverse[imported].add(module.name)
    return reverse


def _expected_values(
    task: Mapping[str, Any],
    contract: Mapping[str, Any],
    modules: Mapping[str, _SourceModule],
    reverse_imports: Mapping[str, set[str]],
    production: tuple[str, ...],
    tests: tuple[str, ...],
    production_rdeps: Mapping[str, int],
) -> dict[str, Any]:
    """Materialize each declared field from one fixed AST graph and contract."""
    params = contract["params"]
    expected: dict[str, Any] = {}
    for field in contract["fields"]:
        if field == "production_importers":
            expected[field] = production
        elif field == "test_importer_count":
            expected[field] = len(tests)
        elif field == "excluded_test_importer_count":
            expected[field] = len(tests)
        elif field == "production_importer_count":
            expected[field] = len(production)
        elif field == "rdep_counts":
            expected[field] = dict(production_rdeps)
        elif field == "ranking":
            candidate_set = _candidate_set(params[field], production, modules, production_rdeps)
            minimum = params[field].get("min_rdep_count", 0)
            ranked = (name for name in candidate_set if production_rdeps.get(name, 0) >= minimum)
            expected[field] = tuple(
                sorted(ranked, key=lambda name: (-production_rdeps.get(name, 0), name))[: params[field]["top_k"]]
            )
        elif field == "buckets":
            expected[field] = _bucket_values(params[field]["labels"], production, modules)
        elif field == "overlap_importers":
            expected[field] = _overlap_importers(production, modules, task["primary_module"], params[field])
        elif field == "overlap_count":
            expected[field] = len(_overlap_importers(production, modules, task["primary_module"], params[field]))
        elif field == "cross_namespace_importers":
            prefix = params[field]["prefix"]
            expected[field] = tuple(name for name in production if name.startswith(prefix))
        elif field == "dependency_chain":
            expected[field] = _shortest_chain(modules, params[field]["source"], params[field]["target"])
        elif field == "affected_module_count":
            minimum = params[field]["min_rdep_count"]
            affected = set(production)
            for importer in production:
                if production_rdeps[importer] >= minimum:
                    affected.update(name for name in reverse_imports.get(importer, set()) if not modules[name].is_test)
            expected[field] = len(affected)
        elif field == "high_centrality":
            minimum = params[field]["min_rdep_count"]
            expected[field] = {
                name: production_rdeps[name]
                for name in sorted(production, key=lambda name: (-production_rdeps[name], name))
                if production_rdeps[name] >= minimum
            }
        elif field == "isolation_verdict":
            expected[field] = (
                "isolated" if all(count <= 5 for count in production_rdeps.values()) else "widely-imported"
            )
        elif field == "risk_tier":
            thresholds = params[field]
            high_centrality_count = sum(
                count >= params["high_centrality"]["min_rdep_count"] for count in production_rdeps.values()
            )
            if (
                len(production) >= thresholds["critical_min_production_importer_count"]
                and high_centrality_count >= thresholds["critical_min_high_centrality_count"]
            ):
                expected[field] = "critical"
            elif high_centrality_count:
                expected[field] = "high"
            elif production:
                expected[field] = "medium"
            else:
                expected[field] = "low"
        else:
            raise ValueError(f"unsupported answer_contract field {field!r}")
    return expected


def _candidate_set(
    params: Mapping[str, Any],
    production: tuple[str, ...],
    modules: Mapping[str, _SourceModule],
    production_rdeps: Mapping[str, int],
) -> tuple[str, ...]:
    """Return one reviewed named ranking candidate set."""
    name = params["candidate_set"]
    if name == "production_importers":
        return production
    if name == "helper_dependent_importers":
        return tuple(module for module in production if "_lr_find" in modules[module].names)
    if name == "non_migrated_importers":
        migrated_targets = set(params["exclude_overlap_targets"])
        return tuple(module for module in production if not (modules[module].imports & migrated_targets))
    raise ValueError(f"unsupported ranking candidate_set {name!r}")


def _bucket_values(
    labels: list[str], production: tuple[str, ...], modules: Mapping[str, _SourceModule]
) -> dict[str, tuple[str, ...]]:
    """Classify only the task labels explicitly admitted by the suite schema."""
    buckets: dict[str, tuple[str, ...]] = {}
    for label in labels:
        if label == "trainer-core":
            values = tuple(name for name in production if name.startswith("lightning.pytorch.trainer."))
        elif label == "callbacks":
            values = tuple(name for name in production if name.startswith("lightning.pytorch.callbacks."))
        elif label == "everything-else":
            values = tuple(
                name
                for name in production
                if not name.startswith(("lightning.pytorch.trainer.", "lightning.pytorch.callbacks."))
            )
        elif label == "public":
            values = tuple(name for name in production if modules[name].is_package)
        elif label == "internal":
            values = tuple(name for name in production if not modules[name].is_package)
        elif label == "helper-dependent":
            values = tuple(name for name in production if "_lr_find" in modules[name].names)
        elif label == "class-only":
            values = tuple(name for name in production if "_lr_find" not in modules[name].names)
        else:
            raise ValueError(f"unsupported bucket label {label!r}")
        buckets[label] = values
    return buckets


def _overlap_importers(
    production: tuple[str, ...],
    modules: Mapping[str, _SourceModule],
    primary_module: str,
    params: Mapping[str, Any],
) -> tuple[str, ...]:
    """Return production importers that also statically import the requested target family."""
    if "targets" in params:
        targets = set(params["targets"])
        return tuple(name for name in production if modules[name].imports & targets)
    prefix = params["prefix"]
    return tuple(
        name
        for name in production
        if any(imported != primary_module and imported.startswith(prefix) for imported in modules[name].imports)
    )


def _shortest_chain(modules: Mapping[str, _SourceModule], source: str, target: str) -> tuple[str, ...]:
    """Return the lexically tie-broken shortest static dependency path, or empty."""
    if source not in modules or target not in modules:
        return ()
    queue: deque[tuple[str, ...]] = deque([(source,)])
    visited = {source}
    while queue:
        path = queue.popleft()
        current = path[-1]
        if current == target:
            return path
        for dependency in sorted(modules[current].imports):
            if dependency not in visited:
                visited.add(dependency)
                queue.append((*path, dependency))
    return ()


def _set_f1(expected: Any, actual: Any) -> float:
    """Score a string list as set F1, ignoring duplicates and element order.

    Non-list answers or lists containing non-strings score zero. An empty
    expected set receives full credit only from an explicit empty list.

    Examples:
        >>> _set_f1(("alpha", "beta"), ["beta", "gamma"])
        0.5
        >>> _set_f1(("alpha",), ["alpha", "alpha"])
        1.0
        >>> _set_f1((), [])
        1.0
        >>> _set_f1((), None)
        0.0
    """
    if not isinstance(actual, list) or not all(isinstance(item, str) for item in actual):
        return 0.0
    expected_set = set(expected)
    if not expected_set:
        return 1.0 if actual == [] else 0.0
    actual_set = set(actual)
    true_positive = len(expected_set & actual_set)
    return 2 * true_positive / (len(expected_set) + len(actual_set))


def _mapping_fraction(expected: Any, actual: Any) -> float:
    """Score matching key/value pairs with F1, penalizing missing and extra keys.

    Compare values through :func:`_same_value`. A non-mapping answer scores
    zero; an empty expected mapping receives credit only from an empty mapping.

    Examples:
        >>> _mapping_fraction({"a": 1, "b": 2}, {"a": 1, "b": 3})
        0.5
        >>> _mapping_fraction({"a": 1}, {"a": True})
        0.0
    """
    if not isinstance(actual, Mapping):
        return 0.0
    if not expected:
        return 1.0 if dict(actual) == {} else 0.0
    true_positive = sum(1 for key, value in expected.items() if key in actual and _same_value(value, actual[key]))
    return 2 * true_positive / (len(expected) + len(actual))


def _graded_count_mapping(expected: Any, actual: Any) -> float:
    """Score count mappings with soft F1, penalizing missing or extra keys once."""
    if not isinstance(expected, Mapping) or not isinstance(actual, Mapping):
        return 0.0
    if not expected:
        return 1.0 if dict(actual) == {} else 0.0
    value_credit = sum(_graded_count_fraction(value, actual.get(key)) for key, value in expected.items())
    return 2 * value_credit / (len(expected) + len(actual))


def _graded_count_fraction(expected: Any, actual: Any) -> float:
    """Return bounded proportional credit for one non-negative integer count."""
    if not all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in (expected, actual)):
        return 0.0
    if expected == actual == 0:
        return 1.0
    return min(expected, actual) / max(expected, actual)


def _ranking_fraction(expected: Any, actual: Any) -> float:
    """Return the fraction of expected ranking positions matched by a string list.

    Missing or misplaced entries lose credit; trailing actual entries do not
    change the denominator. Invalid answer types score zero. An empty expected
    ranking requires an explicit empty list for credit.

    Examples:
        >>> _ranking_fraction(("a", "b"), ["a"])
        0.5
        >>> _ranking_fraction(("a",), ["a", "extra"])
        1.0
    """
    if not isinstance(actual, list) or not all(isinstance(item, str) for item in actual):
        return 0.0
    if not expected:
        return 1.0 if actual == [] else 0.0
    return sum(index < len(actual) and actual[index] == value for index, value in enumerate(expected)) / len(expected)


def _graded_ranking_fraction(expected: Any, actual: Any) -> float:
    """Return longest-common-subsequence credit for an ordered ranking answer."""
    if not isinstance(actual, list) or not all(isinstance(item, str) for item in actual):
        return 0.0
    if not expected:
        return 1.0 if actual == [] else 0.0
    previous = [0] * (len(actual) + 1)
    for expected_item in expected:
        current = [0]
        for index, actual_item in enumerate(actual, start=1):
            # Extend an ordered match; otherwise retain the best subsequence seen on either prefix.
            current.append(
                previous[index - 1] + 1 if expected_item == actual_item else max(previous[index], current[index - 1])
            )
        previous = current
    return previous[-1] / max(len(expected), len(actual))


def _same_value(expected: Any, actual: Any) -> bool:
    """Compare scalars by exact type and value, or tuple paths against JSON lists.

    Scalar booleans do not alias integers. Tuple expectations require a list
    answer and compare its converted tuple using ordinary sequence equality.

    Examples:
        >>> _same_value(1, True)
        False
        >>> _same_value(("a", "b"), ["a", "b"])
        True
    """
    if isinstance(expected, tuple):
        return isinstance(actual, list) and tuple(actual) == expected
    return type(actual) is type(expected) and actual == expected
