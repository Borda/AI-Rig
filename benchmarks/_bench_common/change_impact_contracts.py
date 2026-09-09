"""Provide source-derived contracts for controlled change-impact prediction benchmarks.

Purpose: Define a provider-neutral, read-only answer protocol that grades whether an
agent can distinguish direct callsites broken by a declared compatibility migration
from direct callsites that remain valid. Scope: This module reads only a controlled
fixture repository, resolves its explicit direct imports with a standalone AST
visitor, parses one strict JSON envelope, and scores four location sets plus their
reason codes. Usage: Provider runners load a committed suite with
``load_change_impact_tasks``, materialize one arm-neutral prompt, build an oracle
against the locked fixture root, and pass a final response through assessment and
scoring. Outputs: Immutable oracle values, generic-reporting-compatible score fields,
source fingerprints, and bounded mismatch diagnostics; EREC/RREC/DEFF are ``None``
because this stage does not measure importer mention recall. Failure: Invalid task
shapes, stale source fingerprints, unresolved target calls, malformed envelopes, or a
disagreement between static labels and in-memory migrated-call behavior fail closed.
Used by: The opt-in benchmark provider adapters and
``benchmarks/tests/test_change_impact_contracts.py``. It deliberately does not read a
Codemap index, invoke a provider, write fixture files, or implement product heuristics.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import sys
from types import MappingProxyType, ModuleType
from typing import Any


_ANSWER_FIELDS = (
    "must_update_callsites",
    "compatible_callsites",
    "must_update_tests",
    "compatible_tests",
    "reasons",
)
_REASON_BY_CHANGE = {
    "keyword_only": "positional-after-keyword-only",
    "rename_keyword": "renamed-keyword",
    "required_argument": "missing-required-argument",
}
_COMPATIBLE_REASON = "compatible-call-shape"
_BEGIN = "BEGIN_CHANGE_IMPACT_JSON"
_END = "END_CHANGE_IMPACT_JSON"


@dataclass(frozen=True)
class ChangeImpactOracle:
    """Hold immutable source-derived expected values for one migration task."""

    task_id: str
    expected: Mapping[str, Any]
    source_sha256: str


@dataclass(frozen=True)
class ChangeImpactResponseAssessment:
    """Record strict response validity in the shape expected by generic reporting."""

    answer: Mapping[str, Any] | None
    valid: bool
    error: str | None
    strict_envelope_valid: bool
    pooling_eligible: bool
    diagnostic_only: bool = False


@dataclass(frozen=True)
class ChangeImpactScore:
    """Expose generic-reporting-compatible quality fields for one assessed answer."""

    scored: bool
    quality_score: float
    correct: bool
    components: Mapping[str, float]
    graded_score: float
    graded_components: Mapping[str, float]
    erec: float | None = None
    rrec: float | None = None
    deff: float | None = None


@dataclass(frozen=True)
class _Callsite:
    """Describe one direct target invocation discovered without index data."""

    location: str
    module: str
    scope: str
    path: Path
    lineno: int
    is_test: bool
    node: ast.Call
    reason: str


def load_change_impact_tasks(path: Path) -> list[dict[str, Any]]:
    """Load and validate a committed unlabelled change-impact task suite."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("change-impact task suite is unavailable or malformed") from exc
    tasks = payload.get("tasks") if isinstance(payload, dict) else None
    if not isinstance(tasks, list) or not tasks or not all(isinstance(task, dict) for task in tasks):
        raise ValueError("change-impact suite requires a non-empty task list")
    ids: set[str] = set()
    copied: list[dict[str, Any]] = []
    for task in tasks:
        _validate_task(task)
        if task["id"] in ids:
            raise ValueError(f"change-impact task id is duplicated: {task['id']}")
        ids.add(task["id"])
        copied.append(dict(task))
    return copied


def materialize_change_impact_prompt(task: Mapping[str, Any]) -> str:
    """Append the one arm-neutral strict answer contract to a task prompt."""
    _validate_task(task)
    return "\n\n".join(
        [
            str(task["prompt"]),
            "Return exactly one JSON object between BEGIN_CHANGE_IMPACT_JSON and END_CHANGE_IMPACT_JSON.",
            "It must contain exactly must_update_callsites, compatible_callsites, must_update_tests, "
            "compatible_tests, and reasons.",
            "Each location is module::scope@line. List every statically resolved direct callsite in exactly one "
            "matching production or test partition. reasons maps every listed location to one of "
            "positional-after-keyword-only, renamed-keyword, missing-required-argument, or compatible-call-shape.",
            "Do not edit source files. This task predicts source-level compatibility only; it does not prove runtime "
            "coverage outside the supplied fixture.",
        ]
    )


def source_fingerprint(root: Path) -> str:
    """Return a portable digest over every Python source file below a fixture root."""
    root = Path(root)
    files = sorted(path for path in root.rglob("*.py") if path.is_file())
    if not files:
        raise ValueError("source fingerprint requires at least one Python file")
    digest = sha256()
    for path in files:
        relative = PurePosixPath(path.relative_to(root).as_posix()).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def verify_source_fingerprint(root: Path, expected: str) -> None:
    """Fail closed when a fixture source tree differs from its locked digest."""
    if not isinstance(expected, str) or len(expected) != 64:
        raise ValueError("expected source fingerprint must be a SHA-256 hex digest")
    if source_fingerprint(root) != expected:
        raise ValueError("source fingerprint drift")


def build_change_impact_oracle(task: Mapping[str, Any], source_root: Path) -> ChangeImpactOracle:
    """Build one independent AST oracle without consulting an index or provider output."""
    _validate_task(task)
    source_root = Path(source_root)
    source_sha256 = source_fingerprint(source_root)
    calls = _find_callsites(task, source_root)
    expected = MappingProxyType(
        {
            "must_update_callsites": tuple(
                call.location for call in calls if not call.is_test and call.reason != _COMPATIBLE_REASON
            ),
            "compatible_callsites": tuple(
                call.location for call in calls if not call.is_test and call.reason == _COMPATIBLE_REASON
            ),
            "must_update_tests": tuple(
                call.location for call in calls if call.is_test and call.reason != _COMPATIBLE_REASON
            ),
            "compatible_tests": tuple(
                call.location for call in calls if call.is_test and call.reason == _COMPATIBLE_REASON
            ),
            "reasons": MappingProxyType({call.location: call.reason for call in calls}),
        }
    )
    _validate_expected(expected)
    return ChangeImpactOracle(task_id=str(task["id"]), expected=expected, source_sha256=source_sha256)


def verify_change_impact_oracle(task: Mapping[str, Any], source_root: Path, oracle: ChangeImpactOracle) -> None:
    """Verify static labels by executing baseline and migrated target code in memory."""
    _validate_task(task)
    if oracle.task_id != task["id"]:
        raise ValueError("oracle task id differs from change-impact task")
    verify_source_fingerprint(source_root, oracle.source_sha256)
    calls = _find_callsites(task, Path(source_root))
    observed = {call.location: call.reason for call in calls}
    if observed != oracle.expected["reasons"]:
        raise ValueError("change-impact oracle source labels drifted")
    for call in calls:
        _run_call(task, source_root, call, migrated=False)
        introduced_type_error = _run_call(task, source_root, call, migrated=True)
        expected_failure = call.reason != _COMPATIBLE_REASON
        if introduced_type_error != expected_failure:
            raise ValueError(f"behavioral verification disagrees for {call.location}")


def assess_change_impact_response(task: Mapping[str, Any], text: str) -> ChangeImpactResponseAssessment:
    """Parse one strict response envelope without permissive diagnostic recovery."""
    if not isinstance(task, Mapping) or not isinstance(task.get("id"), str) or not task["id"]:
        raise ValueError("change-impact response assessment requires a task id")
    try:
        answer = _parse_answer(text)
    except (TypeError, ValueError) as exc:
        return ChangeImpactResponseAssessment(None, False, str(exc), False, False)
    return ChangeImpactResponseAssessment(MappingProxyType(answer), True, None, True, True)


def score_change_impact_answer(oracle: ChangeImpactOracle, answer: Mapping[str, Any] | None) -> ChangeImpactScore:
    """Score four affected/compatible sets and exact reasons without importer diagnostics."""
    if answer is None:
        components = MappingProxyType({field: 0.0 for field in _ANSWER_FIELDS})
        return ChangeImpactScore(False, 0.0, False, components, 0.0, components)
    try:
        normalized = _normalize_answer(answer)
    except (TypeError, ValueError):
        components = MappingProxyType({field: 0.0 for field in _ANSWER_FIELDS})
        return ChangeImpactScore(False, 0.0, False, components, 0.0, components)
    components = {
        field: _set_f1(oracle.expected[field], normalized[field]) for field in _ANSWER_FIELDS if field != "reasons"
    }
    components["reasons"] = _mapping_fraction(oracle.expected["reasons"], normalized["reasons"])
    immutable = MappingProxyType(components)
    quality = sum(components.values()) / len(components)
    return ChangeImpactScore(
        True, quality, all(value == 1.0 for value in components.values()), immutable, quality, immutable
    )


def change_impact_failure_details(oracle: ChangeImpactOracle, answer: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return bounded missing, unexpected, and wrong-reason diagnostics for one answer."""
    normalized = _normalize_answer(answer)
    details: list[dict[str, Any]] = []
    for field in _ANSWER_FIELDS:
        expected = oracle.expected[field]
        actual = normalized[field]
        if field == "reasons":
            missing = sorted(set(expected) - set(actual))
            unexpected = sorted(set(actual) - set(expected))
            wrong = sorted(key for key in set(expected) & set(actual) if expected[key] != actual[key])
            details.extend(_detail("missing_facts", field, missing) for _ in [0] if missing)
            details.extend(_detail("unexpected_facts", field, unexpected) for _ in [0] if unexpected)
            details.extend(_detail("wrong_reasons", key, {key: expected[key], "actual": actual[key]}) for key in wrong)
            continue
        missing = sorted(set(expected) - set(actual))
        unexpected = sorted(set(actual) - set(expected))
        details.extend(_detail("missing_facts", field, missing) for _ in [0] if missing)
        details.extend(_detail("unexpected_facts", field, unexpected) for _ in [0] if unexpected)
    return details


def _validate_task(task: Mapping[str, Any]) -> None:
    """Reject task shapes that could change the semantic meaning after review."""
    if not isinstance(task, Mapping):
        raise ValueError("change-impact task must be an object")
    if set(task) - {"id", "type", "primary_fn", "change", "prompt"}:
        raise ValueError("change-impact task has unknown fields")
    if task.get("type") != "change_impact_prediction":
        raise ValueError("change-impact task type is invalid")
    if not all(isinstance(task.get(field), str) and task[field] for field in ("id", "primary_fn", "prompt")):
        raise ValueError("change-impact task requires non-empty id, primary_fn, and prompt")
    primary = str(task["primary_fn"])
    if primary.count("::") != 1 or not all(primary.split("::")):
        raise ValueError("change-impact primary_fn must be module::function")
    change = task.get("change")
    if not isinstance(change, Mapping) or not isinstance(change.get("kind"), str):
        raise ValueError("change-impact task requires a change object")
    kind = change["kind"]
    if kind == "keyword_only":
        if set(change) != {"kind", "parameter", "position"} or not isinstance(change.get("parameter"), str):
            raise ValueError("keyword-only change is malformed")
        if type(change.get("position")) is not int or change["position"] < 0:
            raise ValueError("keyword-only position is malformed")
    elif kind == "rename_keyword":
        if set(change) != {"kind", "old_parameter", "new_parameter"}:
            raise ValueError("rename-keyword change is malformed")
        if not all(
            isinstance(change.get(field), str) and change[field] for field in ("old_parameter", "new_parameter")
        ):
            raise ValueError("rename-keyword parameter is malformed")
    elif kind == "required_argument":
        if set(change) != {"kind", "parameter", "position"} or not isinstance(change.get("parameter"), str):
            raise ValueError("required-argument change is malformed")
        if type(change.get("position")) is not int or change["position"] < 0:
            raise ValueError("required-argument position is malformed")
    else:
        raise ValueError("change-impact change kind is unsupported")


def _find_callsites(task: Mapping[str, Any], root: Path) -> list[_Callsite]:
    """Resolve and classify every direct call to the declared fixture target."""
    target_module, target_function = str(task["primary_fn"]).split("::")
    calls: list[_Callsite] = []
    for path in sorted(candidate for candidate in root.rglob("*.py") if candidate.is_file()):
        module = _module_name(root, path)
        if module == target_module:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        bindings = _import_bindings(tree, target_module, target_function)
        if not bindings:
            continue
        visitor = _CallVisitor(task, path, module, bindings)
        visitor.visit(tree)
        calls.extend(visitor.calls)
    calls.sort(key=lambda call: call.location)
    if not calls:
        raise ValueError("change-impact task has no statically resolved direct calls")
    return calls


def _module_name(root: Path, path: Path) -> str:
    """Return a dotted fixture module name for a Python file."""
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _import_bindings(tree: ast.Module, target_module: str, target_function: str) -> dict[str, str]:
    """Return direct-name and module-alias bindings pointing at one target function."""
    bindings: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == target_module and node.level == 0:
            for alias in node.names:
                if alias.name == target_function:
                    bindings[alias.asname or alias.name] = "function"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == target_module:
                    bindings[alias.asname or alias.name.split(".")[0]] = "module"
    return bindings


class _CallVisitor(ast.NodeVisitor):
    """Collect qualified direct calls while respecting local function-name shadows."""

    def __init__(self, task: Mapping[str, Any], path: Path, module: str, bindings: Mapping[str, str]) -> None:
        """Bind one task’s compatibility rule and module-level import bindings."""
        self._task = task
        self._path = path
        self._module = module
        self._bindings = bindings
        self._scopes: list[str] = []
        self._shadows: list[set[str]] = []
        self.calls: list[_Callsite] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Visit a callable scope after recording local definitions that shadow imports."""
        self._scopes.append(node.name)
        self._shadows.append({child.name for child in node.body if isinstance(child, (ast.FunctionDef, ast.ClassDef))})
        self.generic_visit(node)
        self._shadows.pop()
        self._scopes.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        """Record a direct target call with an exact source location and reason."""
        if self._scopes and self._is_target_call(node):
            reason = _classify_call(self._task["change"], node)
            if reason is None:
                raise ValueError(f"unresolved static call at {self._module}:{node.lineno}")
            scope = ".".join(self._scopes)
            self.calls.append(
                _Callsite(
                    f"{self._module}::{scope}@{node.lineno}",
                    self._module,
                    scope,
                    self._path,
                    node.lineno,
                    self._module == "tests" or self._module.startswith("tests."),
                    node,
                    reason,
                )
            )
        self.generic_visit(node)

    def _is_target_call(self, node: ast.Call) -> bool:
        """Identify direct-name or module-alias calls without accepting same-name decoys."""
        if isinstance(node.func, ast.Name):
            return (
                node.func.id in self._bindings
                and self._bindings[node.func.id] == "function"
                and not any(node.func.id in shadows for shadows in self._shadows)
            )
        if not isinstance(node.func, ast.Attribute) or node.func.attr != str(self._task["primary_fn"]).split("::")[1]:
            return False
        return isinstance(node.func.value, ast.Name) and self._bindings.get(node.func.value.id) == "module"


def _classify_call(change: Mapping[str, Any], node: ast.Call) -> str | None:
    """Classify an explicit direct call under one supported compatibility migration."""
    if any(isinstance(argument, ast.Starred) for argument in node.args) or any(
        keyword.arg is None for keyword in node.keywords
    ):
        return None
    kind = change["kind"]
    if kind == "keyword_only":
        return _REASON_BY_CHANGE[kind] if len(node.args) > change["position"] else _COMPATIBLE_REASON
    if kind == "rename_keyword":
        return (
            _REASON_BY_CHANGE[kind]
            if any(keyword.arg == change["old_parameter"] for keyword in node.keywords)
            else _COMPATIBLE_REASON
        )
    if kind == "required_argument":
        supplied = len(node.args) > change["position"] or any(
            keyword.arg == change["parameter"] for keyword in node.keywords
        )
        return _COMPATIBLE_REASON if supplied else _REASON_BY_CHANGE[kind]
    raise ValueError("unsupported change kind")


def _validate_expected(expected: Mapping[str, Any]) -> None:
    """Require a balanced four-set oracle before provider execution can use it."""
    for field in _ANSWER_FIELDS[:-1]:
        values = expected[field]
        if not isinstance(values, tuple) or len(values) < 2:
            raise ValueError(f"change-impact oracle requires at least two values for {field}")
    locations = [location for field in _ANSWER_FIELDS[:-1] for location in expected[field]]
    if len(locations) != len(set(locations)) or set(locations) != set(expected["reasons"]):
        raise ValueError("change-impact oracle locations and reasons must match exactly")


def _parse_answer(text: str) -> dict[str, Any]:
    """Parse one labelled JSON answer and reject every ambiguous envelope shape."""
    if not isinstance(text, str) or text.count(_BEGIN) != 1 or text.count(_END) != 1:
        raise ValueError("answer requires exactly one strict change-impact JSON envelope")
    _, payload = text.split(_BEGIN, maxsplit=1)
    payload, _ = payload.split(_END, maxsplit=1)
    try:
        answer = json.loads(payload.strip())
    except json.JSONDecodeError as exc:
        raise ValueError("change-impact answer JSON is invalid") from exc
    return _normalize_answer(answer)


def _normalize_answer(answer: Mapping[str, Any]) -> dict[str, Any]:
    """Validate exact labels, unique location sets, and closed reason values."""
    if not isinstance(answer, Mapping) or set(answer) != set(_ANSWER_FIELDS):
        raise ValueError("change-impact answer labels differ from the required contract")
    normalized: dict[str, Any] = {}
    locations: list[str] = []
    for field in _ANSWER_FIELDS[:-1]:
        values = answer[field]
        if not isinstance(values, list) or not all(isinstance(value, str) and value for value in values):
            raise ValueError(f"change-impact answer {field} must be a string list")
        normalized[field] = list(values)
        locations.extend(values)
    reasons = answer["reasons"]
    allowed = {*_REASON_BY_CHANGE.values(), _COMPATIBLE_REASON}
    if not isinstance(reasons, Mapping) or not all(
        isinstance(location, str) and isinstance(reason, str) and reason in allowed
        for location, reason in reasons.items()
    ):
        raise ValueError("change-impact answer reasons are invalid")
    if len(locations) != len(set(locations)) or set(locations) != set(reasons):
        raise ValueError("change-impact answer locations and reason keys must match exactly")
    normalized["reasons"] = dict(reasons)
    return normalized


def _set_f1(expected: Sequence[str], actual: Sequence[str]) -> float:
    """Return exact-set F1 with an explicit empty-set convention."""
    expected_set, actual_set = set(expected), set(actual)
    if not expected_set and not actual_set:
        return 1.0
    overlap = len(expected_set & actual_set)
    return 2 * overlap / (len(expected_set) + len(actual_set)) if expected_set or actual_set else 1.0


def _mapping_fraction(expected: Mapping[str, str], actual: Mapping[str, str]) -> float:
    """Return full-key denominator accuracy for reason assignments."""
    if not expected:
        return 1.0 if not actual else 0.0
    return sum(actual.get(key) == value for key, value in expected.items()) / len(expected)


def _detail(category: str, field: str, values: Any) -> dict[str, Any]:
    """Build one JSON-safe answer mismatch detail."""
    return {"category": category, "field": field, "values": values}


def _run_call(task: Mapping[str, Any], source_root: Path, call: _Callsite, *, migrated: bool) -> bool:
    """Execute one baseline or migrated caller and return whether migration raised TypeError."""
    target_module, target_function = str(task["primary_fn"]).split("::")
    target_path = Path(source_root).joinpath(*target_module.split(".")).with_suffix(".py")
    target_tree = ast.parse(target_path.read_text(encoding="utf-8"), filename=str(target_path))
    if migrated:
        target_tree = _migrate_target_tree(target_tree, target_function, task["change"])
    module_names = {"impactlib", target_module, call.module}
    previous = {name: sys.modules.get(name) for name in module_names}
    try:
        package = ModuleType("impactlib")
        package.__path__ = []  # type: ignore[attr-defined]
        sys.modules["impactlib"] = package
        target = ModuleType(target_module)
        sys.modules[target_module] = target
        setattr(package, target_module.rsplit(".", maxsplit=1)[1], target)
        exec(compile(ast.fix_missing_locations(target_tree), str(target_path), "exec"), target.__dict__)
        caller = ModuleType(call.module)
        sys.modules[call.module] = caller
        exec(compile(call.path.read_text(encoding="utf-8"), str(call.path), "exec"), caller.__dict__)
        getattr(caller, call.scope)()
        return False
    except TypeError as exc:
        if not migrated:
            raise ValueError(f"baseline scenario failed at {call.location}") from exc
        return True
    finally:
        for name, original in previous.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


def _migrate_target_tree(tree: ast.Module, target_function: str, change: Mapping[str, Any]) -> ast.Module:
    """Apply the reviewed compatibility migration to an AST without writing source files."""
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == target_function:
            if change["kind"] == "keyword_only":
                _make_keyword_only(node.args, change["parameter"], change["position"])
            elif change["kind"] == "rename_keyword":
                _rename_parameter(node, change["old_parameter"], change["new_parameter"])
            elif change["kind"] == "required_argument":
                _remove_default(node.args, change["parameter"], change["position"])
            return tree
    raise ValueError("change-impact target function was not found")


def _make_keyword_only(arguments: ast.arguments, parameter: str, position: int) -> None:
    """Move one positional parameter and its default into keyword-only arguments."""
    if position >= len(arguments.args) or arguments.args[position].arg != parameter:
        raise ValueError("keyword-only migration parameter does not match target signature")
    default_index = position - (len(arguments.args) - len(arguments.defaults))
    default = arguments.defaults.pop(default_index) if default_index >= 0 else None
    arguments.kwonlyargs.append(arguments.args.pop(position))
    arguments.kw_defaults.append(default)


def _rename_parameter(node: ast.FunctionDef, old: str, new: str) -> None:
    """Rename one parameter and its local references in a controlled target function."""
    candidates = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
    matched = [argument for argument in candidates if argument.arg == old]
    if len(matched) != 1:
        raise ValueError("rename migration parameter does not match target signature")
    matched[0].arg = new
    for descendant in ast.walk(node):
        if isinstance(descendant, ast.Name) and descendant.id == old:
            descendant.id = new


def _remove_default(arguments: ast.arguments, parameter: str, position: int) -> None:
    """Remove one positional parameter default so omitted calls fail binding."""
    if position >= len(arguments.args) or arguments.args[position].arg != parameter:
        raise ValueError("required-argument migration parameter does not match target signature")
    default_index = position - (len(arguments.args) - len(arguments.defaults))
    if default_index < 0:
        raise ValueError("required-argument migration needs an existing default")
    arguments.defaults.pop(default_index)
