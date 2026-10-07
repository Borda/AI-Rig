"""Define the shared scientific contract for read-crop benchmark tasks.

The contract owns task identity, the strict answer shape, source-derived oracle identity, and read-cost accounting.
Provider adapters may transport a prompt and report native events, but cannot replace these fields with truth supplied
by the provider.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .provider_parity_contracts import canonical_task_hash, prompt_hash

#: Matches a lowercase 64-character hexadecimal SHA-256 digest.
_SHA256_RE = re.compile(r"[0-9a-f]{64}$")
#: Keys a read-crop answer must carry, and nothing more.
_ANSWER_FIELDS = ("signature", "parameters", "behavior")
#: Version label hashed into the read-crop scorer identity; change it when scoring semantics change.
_SCORER_VERSION = "provider-neutral-readcrop-score-v2"


@dataclass(frozen=True)
class ReadcropBehaviorFact:
    """One task-reviewed behavior concept linked to a source parameter."""

    parameter: str
    terms: tuple[str, ...]


@dataclass(frozen=True)
class ReadcropContract:
    """Immutable provider-neutral contract for one read-crop task."""

    task_id: str
    canonical_task_sha256: str
    prompt_sha256: str
    symbol: str
    source_sha256: str
    oracle_sha256: str
    answer_contract_sha256: str
    behavior_contract_sha256: str
    scorer_sha256: str
    source_parameter_names: tuple[str, ...]
    required_parameter_names: tuple[str, ...]
    required_behavior_facts: tuple[ReadcropBehaviorFact, ...]
    diagnostic_keywords: tuple[str, ...]

    def provider_binding(self) -> Mapping[str, str]:
        """Return scientific hashes that every provider adapter must preserve."""
        return MappingProxyType(
            {
                "canonical_task_sha256": self.canonical_task_sha256,
                "prompt_sha256": self.prompt_sha256,
                "source_sha256": self.source_sha256,
                "oracle_sha256": self.oracle_sha256,
                "answer_contract_sha256": self.answer_contract_sha256,
                "behavior_contract_sha256": self.behavior_contract_sha256,
                "scorer_sha256": self.scorer_sha256,
            }
        )


@dataclass(frozen=True)
class ReadcropAnswer:
    """Strict answer fields returned from a read-crop provider cell."""

    signature: str
    parameters: tuple[str, ...]
    behavior: str


@dataclass(frozen=True)
class ReadcropScore:
    """Source retrieval correctness plus task-reviewed behavior-fact coverage."""

    primary_correct: bool
    signature_correct: bool
    parameter_recall: float
    behavior_protocol_valid: bool
    behavior_fact_recall: float | None
    behavior_facts_correct: bool | None
    quality_components: Mapping[str, float]
    quality_score: float
    keyword_recall: float


@dataclass(frozen=True)
class ReadcropUsage:
    """Keep native total input cost and optional tool payload cost separate."""

    total_input_tokens: int
    tool_result_tokens: int | None

    def __post_init__(self) -> None:
        """Reject invalid measurements without inventing unavailable payload cost."""
        for field_name, value in (("total_input_tokens", self.total_input_tokens),):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        if self.tool_result_tokens is not None:
            value = self.tool_result_tokens
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("tool_result_tokens must be a non-negative integer")


def build_readcrop_contract(task: Mapping[str, Any], *, source: str) -> ReadcropContract:
    """Create a source-anchored shared contract from one committed task.

    The source text is loaded from the frozen target before execution. It is hashed into the independent oracle so an
    adapter cannot silently score a response against a different symbol body.
    """
    task_id = _required_text(task, "id")
    if _required_text(task, "type") != "read_crop":
        raise ValueError(f"task {task_id} must have type read_crop")
    symbol = _required_text(task, "symbol")
    if not isinstance(source, str) or not source.strip():
        raise ValueError(f"task {task_id} requires non-empty source")
    keywords = _string_tuple(task.get("expected_keywords"), field="expected_keywords", task_id=task_id)
    answer_contract = {"fields": _ANSWER_FIELDS, "symbol": symbol}
    source_parameter_names = _parameter_names(source)
    required_parameter_names = _required_parameter_names(
        task,
        task_id=task_id,
        source_parameter_names=source_parameter_names,
    )
    required_behavior_facts = _required_behavior_facts(
        task,
        task_id=task_id,
        required_parameter_names=required_parameter_names,
    )
    behavior_contract = [{"parameter": fact.parameter, "terms": fact.terms} for fact in required_behavior_facts]
    oracle = {
        "symbol": symbol,
        "source": source,
        "source_parameter_names": source_parameter_names,
        "required_parameter_names": required_parameter_names,
        "required_behavior_facts": behavior_contract,
    }
    return ReadcropContract(
        task_id=task_id,
        canonical_task_sha256=canonical_task_hash(task),
        prompt_sha256=prompt_hash(task),
        symbol=symbol,
        source_sha256=_sha256(source),
        oracle_sha256=_sha256_json(oracle),
        answer_contract_sha256=_sha256_json(answer_contract),
        behavior_contract_sha256=_sha256_json({"required_behavior_facts": behavior_contract}),
        scorer_sha256=_sha256_json(
            {
                "version": _SCORER_VERSION,
                "primary": "source_symbol_and_required_parameter_retrieval",
                "behavior": "task_reviewed_fact_term_coverage_when_declared",
            }
        ),
        source_parameter_names=source_parameter_names,
        required_parameter_names=required_parameter_names,
        required_behavior_facts=required_behavior_facts,
        diagnostic_keywords=keywords,
    )


def parse_readcrop_answer(text: str) -> ReadcropAnswer:
    """Parse exactly the signature, parameters, and behavior fields from JSON.

    Signature and behavior must be non-blank strings. Parameters may be an empty
    list; otherwise each item must be a non-blank string. Preserve string text
    and parameter order, returning parameters as a tuple. Raise ``ValueError``
    for invalid JSON, missing or extra keys, and invalid field values.

    Examples:
        >>> answer = parse_readcrop_answer('{"signature": "f(x)", "parameters": ["x"], "behavior": "Return x."}')
        >>> answer.parameters
        ('x',)
        >>> answer.signature
        'f(x)'
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("read-crop answer must be one JSON object") from exc
    if not isinstance(payload, dict) or set(payload) != set(_ANSWER_FIELDS):
        missing = sorted(set(_ANSWER_FIELDS) - set(payload)) if isinstance(payload, dict) else list(_ANSWER_FIELDS)
        if missing:
            raise ValueError(f"missing required answer field: {', '.join(missing)}")
        raise ValueError("read-crop answer has unexpected fields")
    signature = payload["signature"]
    parameters = payload["parameters"]
    behavior = payload["behavior"]
    if not isinstance(signature, str) or not signature.strip():
        raise ValueError("signature must be a non-empty string")
    if not isinstance(parameters, list) or not all(isinstance(item, str) and item.strip() for item in parameters):
        raise ValueError("parameters must be a string list")
    if not isinstance(behavior, str) or not behavior.strip():
        raise ValueError("behavior must be a non-empty string")
    return ReadcropAnswer(signature=signature, parameters=tuple(parameters), behavior=behavior)


def score_readcrop_answer(contract: ReadcropContract, answer: ReadcropAnswer) -> ReadcropScore:
    """Score source retrieval and declared behavior facts without an LLM judge."""
    normalized_signature = _normalize(answer.signature)
    signature_correct = _normalize(contract.symbol) in normalized_signature
    # A focused task declares its required subset and is checked against the
    # frozen source. Tasks without that declaration retain source-wide recall.
    observed_parameters = {_normalize(parameter) for parameter in answer.parameters}
    expected_parameters = {_normalize(parameter) for parameter in contract.required_parameter_names}
    parameter_recall = (
        sum(parameter in observed_parameters for parameter in expected_parameters) / len(expected_parameters)
        if expected_parameters
        else 1.0
    )
    normalized_behavior = _normalize(answer.behavior)
    if contract.required_behavior_facts:
        behavior_fact_recall = sum(
            all(_normalize(term) in normalized_behavior for term in fact.terms)
            for fact in contract.required_behavior_facts
        ) / len(contract.required_behavior_facts)
        behavior_facts_correct = behavior_fact_recall == 1.0
    else:
        behavior_fact_recall = None
        behavior_facts_correct = None
    quality_components = {
        "signature": float(signature_correct),
        "parameters": parameter_recall,
    }
    if behavior_fact_recall is not None:
        quality_components["behavior"] = behavior_fact_recall
    quality_score = sum(quality_components.values()) / len(quality_components)
    answer_text = " ".join((answer.signature, *answer.parameters, answer.behavior))
    keyword_hits = sum(_normalize(keyword) in _normalize(answer_text) for keyword in contract.diagnostic_keywords)
    keyword_recall = keyword_hits / len(contract.diagnostic_keywords) if contract.diagnostic_keywords else 1.0
    return ReadcropScore(
        primary_correct=signature_correct and parameter_recall == 1.0 and behavior_facts_correct is not False,
        signature_correct=signature_correct,
        parameter_recall=parameter_recall,
        behavior_protocol_valid=bool(answer.behavior.strip()),
        behavior_fact_recall=behavior_fact_recall,
        behavior_facts_correct=behavior_facts_correct,
        quality_components=MappingProxyType(quality_components),
        quality_score=quality_score,
        keyword_recall=keyword_recall,
    )


def validate_provider_binding(contract: ReadcropContract, binding: Mapping[str, object]) -> None:
    """Reject provider evidence that changes a shared scientific hash."""
    expected = dict(contract.provider_binding())
    observed = {name: binding.get(name) for name in expected}
    if observed != expected:
        raise ValueError("provider binding changes scientific fields")


def _required_text(task: Mapping[str, Any], field: str) -> str:
    """Return one required non-empty task string."""
    value = task.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"task {field} must be a non-empty string")
    return value


def _string_tuple(value: object, *, field: str, task_id: str) -> tuple[str, ...]:
    """Validate a stable non-empty string list from one raw task field."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"task {task_id} {field} must be a string list")
    items = tuple(value)
    if not items or not all(isinstance(item, str) and item.strip() for item in items):
        raise ValueError(f"task {task_id} {field} must contain non-empty strings")
    if len(set(items)) != len(items):
        raise ValueError(f"task {task_id} {field} must not contain duplicates")
    return items


def _required_parameter_names(
    task: Mapping[str, Any],
    *,
    task_id: str,
    source_parameter_names: tuple[str, ...],
) -> tuple[str, ...]:
    """Return a source-validated task subset or the full-source fallback."""
    configured = task.get("required_parameters")
    if configured is None:
        return source_parameter_names
    required = _string_tuple(configured, field="required_parameters", task_id=task_id)
    unavailable = sorted(set(required) - set(source_parameter_names))
    if unavailable:
        raise ValueError(f"task {task_id} required_parameters are absent from source: {', '.join(unavailable)}")
    return required


def _required_behavior_facts(
    task: Mapping[str, Any],
    *,
    task_id: str,
    required_parameter_names: tuple[str, ...],
) -> tuple[ReadcropBehaviorFact, ...]:
    """Validate optional task-reviewed behavior facts against required parameters."""
    configured = task.get("required_behavior_facts")
    if configured is None:
        return ()
    if not isinstance(configured, Sequence) or isinstance(configured, (str, bytes)) or not configured:
        raise ValueError(f"task {task_id} required_behavior_facts must be a non-empty object list")
    facts: list[ReadcropBehaviorFact] = []
    for item in configured:
        if not isinstance(item, Mapping) or set(item) != {"parameter", "terms"}:
            raise ValueError(f"task {task_id} required_behavior_facts entries require parameter and terms")
        parameter = item["parameter"]
        if not isinstance(parameter, str) or not parameter.strip():
            raise ValueError(f"task {task_id} required_behavior_facts parameter must be a non-empty string")
        if parameter not in required_parameter_names:
            raise ValueError(f"task {task_id} behavior fact parameter is not required: {parameter}")
        terms = _string_tuple(item["terms"], field="required_behavior_facts terms", task_id=task_id)
        facts.append(ReadcropBehaviorFact(parameter=parameter, terms=terms))
    parameters = [fact.parameter for fact in facts]
    if len(parameters) != len(set(parameters)):
        raise ValueError(f"task {task_id} required_behavior_facts must not repeat parameters")
    return tuple(facts)


def _normalize(value: str) -> str:
    """Lowercase text and collapse whitespace while retaining identifier punctuation.

    Examples:
        >>> _normalize("  Use   input_value  ")
        'use input_value'
    """
    return re.sub(r"\s+", " ", value).strip().lower()


def _parameter_names(source: str) -> tuple[str, ...]:
    """Extract parameters from the first function yielded by the parsed source's AST walk.

    Collect positional-only, ordinary, then keyword-only names; append variadic
    positional and keyword names last. Drop a leading ``self`` or ``cls``.
    Raise ``ValueError`` for unparsable source or source without a function.

    Examples:
        >>> _parameter_names("def f(self, x, *, limit=1): pass")
        ('x', 'limit')
        >>> _parameter_names("def f(x, *args, flag=False, **kwargs): pass")
        ('x', 'flag', 'args', 'kwargs')
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError("read-crop source must parse as Python") from exc
    definition = next(
        (node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))),
        None,
    )
    if definition is None:
        raise ValueError("read-crop source must contain a function or method definition")
    names = [
        argument.arg for argument in (*definition.args.posonlyargs, *definition.args.args, *definition.args.kwonlyargs)
    ]
    if names and names[0] in {"self", "cls"}:
        names.pop(0)
    if definition.args.vararg is not None:
        names.append(definition.args.vararg.arg)
    if definition.args.kwarg is not None:
        names.append(definition.args.kwarg.arg)
    return tuple(names)


def _sha256(value: str) -> str:
    """Return the SHA-256 digest of one UTF-8 string."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: object) -> str:
    """Return the stable SHA-256 digest of one JSON-compatible value."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
