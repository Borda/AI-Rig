"""Define provider-neutral contracts for mutable edit and patch benchmark stages.

Provider adapters may transport a prompt and report execution facts, but they cannot change a task's oracle, score,
exclusions, answer wire contract, or stage identity. This module does not create a worktree or execute a command; the
mutation lifecycle is owned by the runner boundary.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from types import MappingProxyType, SimpleNamespace
from typing import Any, Optional

from .provider_parity_contracts import canonical_task_hash, prompt_hash
from .subprocess_env import minimal_child_env

_SHA256_RE = re.compile(r"[0-9a-f]{64}$")
_GIT_COMMIT_RE = re.compile(r"[0-9a-f]{40}$")
_SCORER_VERSION = "provider-neutral-edit-score-v1"
_FIX_MULTI_SCORER_VERSION = MappingProxyType(
    {
        "FM-01": "provider-neutral-fix-multi-score-v4",
        "FM-02": "provider-neutral-fix-multi-score-v2",
        "FM-03": "provider-neutral-fix-multi-score-v4",
    }
)
_FIX_MULTI_BASELINE_COMMIT = "be98784a1a03581b7051a355ae1084fd352d7cea"
_FIX_SINGLE_SCORER_VERSION = "provider-neutral-fix-single-score-v1"
_FIX_SINGLE_BASELINE_COMMIT = "be98784a1a03581b7051a355ae1084fd352d7cea"
_FIX_MULTI_ROOT = "src/lightning/pytorch"
_FIX_MULTI_PATHS = {
    "FM-01": (f"{_FIX_MULTI_ROOT}/callbacks/early_stopping.py",),
    "FM-02": (f"{_FIX_MULTI_ROOT}/callbacks/model_checkpoint.py",),
    "FM-03": tuple(
        f"{_FIX_MULTI_ROOT}/strategies/{name}.py"
        for name in ("strategy", "ddp", "fsdp", "deepspeed", "model_parallel", "xla")
    ),
}
# These hashes bind each normalized method body to the frozen 2.6.5 baseline.
# Normalization removes only the requested log or forwarded keyword, so an
# otherwise deleted, reordered, or rewritten setup path cannot pass the oracle.
_SETUP_ENVIRONMENT_BODY_SHA256 = MappingProxyType(
    {
        "Strategy": "24d96c3bc5817f5d6b37604feb598f2248585db56dd266f58cebb481673ffe9a",
        "DDPStrategy": "5be57537682e822525a6a5c2b18d74d30aa1dcf13006bcf63262674eecf839eb",
        "FSDPStrategy": "301ee5b12d4016bd672bda377bd3dcff160f6f9d243f970d98a6b5da787684ea",
        "DeepSpeedStrategy": "85e23b65cbb7bd2a83dc1bbdc24cf6c67db6fde61a1073c790ac3886613d55a3",
        "ModelParallelStrategy": "8cdba6c0597af96ec59e91d3315ea389c870a8a291561916be840b91707ddc8f",
        "XLAStrategy": "c2f6d1d993218a9078b7fbaaf3df80777d72b5f675628c95c408ff1c40e3d4e7",
    }
)
_EXCLUSIONS = MappingProxyType(
    {
        "excluded_from_pooling_when": (
            "patch_not_applied",
            "targeted_test_failed",
            "regression_test_missing_or_failed",
            "changed_path_boundary_failed",
            "answer_envelope_invalid",
        ),
        "diagnostic_only": ("expected_path_recall", "keyword_recall"),
    }
)


@dataclass(frozen=True)
class StageIdentity:
    """Immutable identity of a separately reported benchmark stage."""

    stage: str
    revision: str
    task_suite_sha256: str
    contract_sha256: str

    def __post_init__(self) -> None:
        """Reject malformed identity coordinates before they enter evidence."""
        if not isinstance(self.stage, str) or not self.stage:
            raise ValueError("stage must be a non-empty string")
        if not isinstance(self.revision, str) or not self.revision:
            raise ValueError("stage revision must be a non-empty string")
        for name, value in (("task_suite_sha256", self.task_suite_sha256), ("contract_sha256", self.contract_sha256)):
            if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")

    @property
    def sha256(self) -> str:
        """Return the hash of this complete immutable stage coordinate."""
        return _sha256(
            {
                "schema": "provider-parity-stage-identity-v1",
                "stage": self.stage,
                "revision": self.revision,
                "task_suite_sha256": self.task_suite_sha256,
                "contract_sha256": self.contract_sha256,
            }
        )


@dataclass(frozen=True)
class EditTaskContract:
    """Scientific contract for one mutable task, independent of provider transport."""

    task_id: str
    task_type: str
    canonical_task_sha256: str
    prompt_sha256: str
    baseline_commit: str
    expected_paths: tuple[str, ...]
    test_fixture_patch: str
    fixture_paths: tuple[str, ...]
    test_fixture_patch_sha256: str
    targeted_test_command: str
    regression_test_commands: tuple[str, ...]
    diagnostic_keywords: tuple[str, ...]
    answer_contract_sha256: str
    oracle_sha256: str
    scorer_sha256: str
    exclusions_sha256: str

    def scientific_field_hashes(self, stage_identity: StageIdentity) -> Mapping[str, str]:
        """Return the exact provider-invariant hashes an adapter must preserve."""
        return MappingProxyType(
            {
                "canonical_task_sha256": self.canonical_task_sha256,
                "prompt_sha256": self.prompt_sha256,
                "answer_contract_sha256": self.answer_contract_sha256,
                "oracle_sha256": self.oracle_sha256,
                "scorer_sha256": self.scorer_sha256,
                "exclusions_sha256": self.exclusions_sha256,
                "stage_identity_sha256": stage_identity.sha256,
            }
        )


@dataclass(frozen=True)
class PatchAnswer:
    """A strictly parsed unified-diff answer returned by one provider."""

    diff: str
    sha256: str


@dataclass(frozen=True)
class EditExecution:
    """Observed, runner-produced facts after one patch candidate executes."""

    patch_applied: bool
    targeted_test_passed: bool
    regression_test_passed: bool | None
    changed_paths: tuple[str, ...]
    baseline_target_failed: bool = True
    baseline_regressions_passed: bool = True
    fixture_intact: bool = True
    source_integrity: bool = True
    index_integrity: bool | None = None
    cleanup_verified: bool = True
    command_evidence: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Return JSON-safe execution evidence for either provider adapter."""
        return asdict(self)


@dataclass(frozen=True)
class EditScore:
    """Primary and safety outcomes without promoting diagnostic evidence."""

    primary_correct: bool
    safety_passed: bool
    changed_path_boundary_passed: bool
    pooling_eligible: bool
    diagnostics: Mapping[str, float]


def _regression_binding(commands: tuple[str, ...]) -> dict[str, str]:
    """Return the locked regression-command binding entry, or nothing when unused.

    The key is emitted only for a contract that actually declares regression commands.
    ``provider_binding`` is persisted into result artifacts and compared byte-for-byte on
    rescore, so unconditionally adding a key would invalidate every historical row for a
    field none of those rows ever used.

    Args:
        commands: Frozen regression test commands declared by the contract.

    Returns:
        ``{"regression_test_commands_sha256": <digest>}`` when commands exist, else ``{}``.

    Examples:
        >>> _regression_binding(())
        {}
        >>> sorted(_regression_binding(("pytest tests/test_a.py",)))
        ['regression_test_commands_sha256']
    """
    if not commands:
        return {}
    payload = "\n".join(commands).encode("utf-8")
    return {"regression_test_commands_sha256": hashlib.sha256(payload).hexdigest()}


@dataclass(frozen=True)
class FixMultiContract:
    """Immutable multi-caller contract shared by provider transports."""

    task_id: str
    canonical_task_sha256: str
    prompt_sha256: str
    baseline_commit: str
    expected_paths: tuple[str, ...]
    oracle_sha256: str
    scorer_sha256: str
    regression_test_commands: tuple[str, ...] = ()

    def provider_binding(self) -> Mapping[str, str]:
        """Return science-bearing fields every provider must preserve."""
        return MappingProxyType(
            {
                "canonical_task_sha256": self.canonical_task_sha256,
                "prompt_sha256": self.prompt_sha256,
                "baseline_commit": self.baseline_commit,
                "oracle_sha256": self.oracle_sha256,
                "scorer_sha256": self.scorer_sha256,
                **_regression_binding(self.regression_test_commands),
            }
        )


@dataclass(frozen=True)
class FixSingleContract:
    """Immutable single-file contract shared by provider transports.

    The contract freezes the original task, the one permitted source path, and the independent microexecution oracle.
    Provider-specific runners may only transport this coordinate and report a captured patch.
    """

    task_id: str
    canonical_task_sha256: str
    prompt_sha256: str
    baseline_commit: str
    expected_paths: tuple[str, ...]
    oracle_id: str
    oracle_sha256: str
    scorer_sha256: str
    regression_test_commands: tuple[str, ...] = ()

    def provider_binding(self) -> Mapping[str, str]:
        """Return science-bearing fields every provider transport must preserve."""
        return MappingProxyType(
            {
                "canonical_task_sha256": self.canonical_task_sha256,
                "prompt_sha256": self.prompt_sha256,
                "baseline_commit": self.baseline_commit,
                "oracle_sha256": self.oracle_sha256,
                "scorer_sha256": self.scorer_sha256,
                **_regression_binding(self.regression_test_commands),
            }
        )


_FIX_SINGLE_ORACLES: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "FS-01": {"path": "src/lightning/pytorch/callbacks/early_stopping.py", "oracle": "early_stopping_patience"},
        "FS-02": {"path": "src/lightning/pytorch/callbacks/early_stopping.py", "oracle": "early_stopping_min_delta"},
        "FS-03": {
            "path": "src/lightning/pytorch/callbacks/model_checkpoint.py",
            "oracle": "model_checkpoint_duplicate_step",
        },
        "FS-04": {
            "path": "src/lightning/pytorch/callbacks/model_checkpoint.py",
            "oracle": "model_checkpoint_save_top_k_zero_warning",
        },
    }
)


def build_fix_single_contract(task: Mapping[str, Any]) -> FixSingleContract:
    """Freeze one original scaffold task and its independently maintained oracle."""
    task_id = task.get("id")
    if not isinstance(task_id, str) or task_id not in _FIX_SINGLE_ORACLES or task.get("type") != "fix_single":
        raise ValueError("unknown fix-single task")
    expected_files = task.get("expected_files")
    if not isinstance(expected_files, list) or len(expected_files) != 1 or not isinstance(expected_files[0], str):
        raise ValueError(f"task {task_id} must name exactly one expected file")
    oracle = _FIX_SINGLE_ORACLES[task_id]
    path = oracle["path"]
    if Path(path).name != expected_files[0]:
        raise ValueError(f"task {task_id} expected file disagrees with its executable boundary")
    return FixSingleContract(
        task_id=task_id,
        canonical_task_sha256=canonical_task_hash(task),
        prompt_sha256=prompt_hash(task),
        baseline_commit=_FIX_SINGLE_BASELINE_COMMIT,
        expected_paths=(path,),
        oracle_id=oracle["oracle"],
        oracle_sha256=_sha256(
            {
                "oracle_id": oracle["oracle"],
                "path": path,
                "baseline_commit": _FIX_SINGLE_BASELINE_COMMIT,
                "semantics": "candidate-method microexecution with dependency-free fakes",
            }
        ),
        scorer_sha256=_sha256({"version": _FIX_SINGLE_SCORER_VERSION, "primary": "executable_behavior_and_exact_path"}),
    )


def validate_fix_single_binding(contract: FixSingleContract, observed: Mapping[str, object]) -> None:
    """Reject provider evidence that changes a Fix-Single scientific field."""
    if dict(observed) != dict(contract.provider_binding()):
        raise ValueError("provider adapter changed fix-single scientific fields")


def _load_class(tree: ast.Module, name: str) -> ast.ClassDef:
    """Return one candidate class definition or fail closed."""
    node = next((item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == name), None)
    if node is None:
        raise ValueError(f"candidate is missing class {name}")
    return node


def _load_method(tree: ast.Module, class_name: str, method_name: str) -> ast.FunctionDef:
    """Return one candidate method definition or fail closed."""
    node = next(
        (
            item
            for item in _load_class(tree, class_name).body
            if isinstance(item, ast.FunctionDef) and item.name == method_name
        ),
        None,
    )
    if node is None:
        raise ValueError(f"candidate is missing method {class_name}.{method_name}")
    return node


def _compile_function(node: ast.FunctionDef, namespace: dict[str, Any]) -> Callable[..., Any]:
    """Compile one candidate method with its trusted fake dependencies."""
    copied = copy.deepcopy(node)
    copied.decorator_list = []
    module = ast.Module(body=[copied], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, "<candidate-method>", "exec"), namespace)  # noqa: S102
    return namespace[copied.name]


def _load_early_stopping(tree: ast.Module) -> type[Any]:
    """Compile EarlyStopping with only its initializer's required fake runtime names."""
    class_node = _load_class(tree, "EarlyStopping")

    class Callback:
        """Minimal callback base supporting candidate initialization."""

        def __init__(self) -> None:
            pass

    namespace = {
        "Any": Any,
        "Callable": Callable,
        "Optional": Optional,
        "Tensor": object,
        "Callback": Callback,
        "EarlyStoppingReason": SimpleNamespace(NOT_STOPPED="not-stopped"),
        "MisconfigurationException": ValueError,
        "override": lambda function: function,
        "pl": SimpleNamespace(),
        "rank_prefixed_message": lambda *args: "",
        "rank_zero_warn": lambda *args, **kwargs: None,
        "torch": SimpleNamespace(
            inf=float("inf"),
            lt=lambda left, right: left < right,
            gt=lambda left, right: left > right,
            tensor=lambda value: value,
        ),
    }
    module = ast.Module(body=[copy.deepcopy(class_node)], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, "<candidate-early-stopping>", "exec"), namespace)  # noqa: S102
    return namespace["EarlyStopping"]


def _check_patience(tree: ast.Module) -> bool:
    """Require invalid patience rejection while preserving the positive case."""
    candidate = _load_early_stopping(tree)
    try:
        candidate("metric", patience=0)
    except ValueError as exc:
        invalid_rejected = str(exc) == "patience must be >= 1, got 0"
    else:
        invalid_rejected = False
    try:
        candidate("metric", patience=1)
    except Exception:
        valid_accepted = False
    else:
        valid_accepted = True
    return invalid_rejected and valid_accepted


def _check_min_delta(tree: ast.Module) -> bool:
    """Require invalid min_delta rejection while preserving zero."""
    candidate = _load_early_stopping(tree)
    try:
        candidate("metric", min_delta=-0.1)
    except ValueError as exc:
        invalid_rejected = str(exc) == "min_delta must be >= 0, got -0.1"
    else:
        invalid_rejected = False
    try:
        candidate("metric", min_delta=0.0)
    except Exception:
        valid_accepted = False
    else:
        valid_accepted = True
    return invalid_rejected and valid_accepted


def _check_duplicate_checkpoint(tree: ast.Module) -> bool:
    """Require a duplicate global-step save to skip the second trainer save."""
    save = _load_method(tree, "ModelCheckpoint", "_save_checkpoint")
    saved: list[tuple[str, bool]] = []
    notices: list[str] = []
    state = SimpleNamespace(_last_global_step_saved=0, save_weights_only=False, _last_checkpoint_saved="")
    trainer = SimpleNamespace(
        global_step=7,
        is_global_zero=False,
        loggers=[],
        save_checkpoint=lambda path, weights_only: saved.append((path, weights_only)),
    )
    function = _compile_function(save, {"proxy": lambda value: value, "rank_zero_info": notices.append})
    function(state, trainer, "first.ckpt")
    function(state, trainer, "second.ckpt")
    return saved == [("first.ckpt", False)] and bool(notices)


def _check_save_top_k_warning(tree: ast.Module) -> bool:
    """Require the no-checkpoint warning while retaining legal positive setup."""
    validator = _load_method(tree, "ModelCheckpoint", "__validate_init_configuration")
    warnings: list[str] = []
    function = _compile_function(
        validator, {"MisconfigurationException": ValueError, "rank_zero_warn": warnings.append}
    )
    zero = SimpleNamespace(
        save_top_k=0, _every_n_train_steps=0, _every_n_epochs=0, _train_time_interval=None, monitor="metric"
    )
    positive = SimpleNamespace(
        save_top_k=1, _every_n_train_steps=0, _every_n_epochs=0, _train_time_interval=None, monitor="metric"
    )
    function(zero)
    zero_warned = warnings == [
        "ModelCheckpoint(save_top_k=0) is set: no checkpoints will be saved. Pass save_top_k=-1 to save all checkpoints."
    ]
    warnings.clear()
    function(positive)
    return zero_warned and not warnings


_FIX_SINGLE_CHECKS: Mapping[str, Callable[[ast.Module], bool]] = MappingProxyType(
    {
        "early_stopping_patience": _check_patience,
        "early_stopping_min_delta": _check_min_delta,
        "model_checkpoint_duplicate_step": _check_duplicate_checkpoint,
        "model_checkpoint_save_top_k_zero_warning": _check_save_top_k_warning,
    }
)


def run_fix_single_check(oracle_id: str, source: str, filename: str) -> bool:
    """Evaluate one Fix-Single oracle against candidate source in the current process.

    This is the in-process core of the oracle and the entry point the sandbox worker
    imports. Production scoring never calls it directly: model-authored source is
    micro-executed, so :func:`run_fix_single_oracle` runs it in a separate process
    instead of inside the scorer.

    Args:
        oracle_id: Selected executable oracle from :data:`_FIX_SINGLE_CHECKS`.
        source: Candidate module source text.
        filename: Reporting filename used for the parsed candidate.

    Returns:
        Whether the candidate satisfies the selected oracle.

    Examples:
        >>> candidate = '''
        ... class EarlyStopping(Callback):
        ...     def __init__(self, monitor, patience=3):
        ...         if patience < 1:
        ...             raise MisconfigurationException(f"patience must be >= 1, got {patience}")
        ...         self.patience = patience
        ... '''
        >>> run_fix_single_check("early_stopping_patience", candidate, "<candidate>")
        True
    """
    if oracle_id not in _FIX_SINGLE_CHECKS:
        raise ValueError(f"unknown fix-single oracle {oracle_id!r}")
    return _FIX_SINGLE_CHECKS[oracle_id](ast.parse(source, filename=filename))


# The worker reaches its own package through argv rather than an inherited PYTHONPATH so
# the child can stay in isolated mode. The verdict carries a parent-generated token because
# candidate code may print to stdout, and an untokenized last line would be ambiguous.
_FIX_SINGLE_WORKER_SOURCE = """
import json
import sys

sys.path.insert(0, sys.argv[1])
request = json.loads(sys.stdin.read())
from _bench_common.edit_patch_contracts import run_fix_single_check

try:
    verdict = {"passed": bool(run_fix_single_check(request["oracle_id"], request["source"], request["filename"]))}
except BaseException as exc:
    verdict = {"error": f"{type(exc).__name__}: {exc}"}
print(request["token"] + json.dumps(verdict))
"""
# A candidate that neither returns nor terminates is a failed candidate, not an unbounded
# scorer stall; the deadline is generous next to the microexecution it bounds.
_FIX_SINGLE_ORACLE_TIMEOUT_S = 120.0


def _fix_single_worker_verdict(oracle_id: str, source: str, filename: str, *, timeout_s: float) -> bool:
    """Run one Fix-Single oracle in a contained child process and return its verdict."""
    token = f"fix-single-verdict-{os.urandom(8).hex()}:"
    request = json.dumps({"oracle_id": oracle_id, "source": source, "filename": filename, "token": token})
    package_root = str(Path(__file__).resolve().parent.parent)
    with tempfile.TemporaryDirectory(prefix="codemap-fix-single-oracle-") as sandbox:
        try:
            completed = subprocess.run(
                [sys.executable, "-I", "-B", "-c", _FIX_SINGLE_WORKER_SOURCE, package_root],
                input=request,
                capture_output=True,
                text=True,
                errors="replace",
                cwd=sandbox,
                env=minimal_child_env(),
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False
    verdicts = [line for line in completed.stdout.splitlines() if line.startswith(token)]
    if not verdicts:
        detail = (completed.stderr or completed.stdout).strip()[:300]
        raise ValueError(f"fix-single oracle worker returned no verdict (exit {completed.returncode}): {detail}")
    verdict = json.loads(verdicts[-1][len(token) :])
    if "error" in verdict:
        raise ValueError(f"fix-single candidate execution failed: {verdict['error']}")
    return bool(verdict["passed"])


def run_fix_single_oracle(
    repo_path: Path, contract: FixSingleContract, *, timeout_s: float = _FIX_SINGLE_ORACLE_TIMEOUT_S
) -> bool:
    """Return whether the candidate source satisfies the selected Fix-Single oracle.

    The oracle micro-executes model-authored source, so it runs in a separate isolated
    interpreter with an empty working directory, a minimal environment, and a deadline
    rather than inside the scoring process. Only the candidate text crosses the boundary;
    the verdict is unchanged, so oracle and scorer identity hashes are untouched.

    Args:
        repo_path: Worktree holding the candidate source.
        contract: Fix-Single contract naming the expected path and executable oracle.
        timeout_s: Deadline for the candidate microexecution.

    Returns:
        Whether the candidate satisfies its oracle; a candidate that exceeds the deadline
        has not satisfied it.
    """
    source_path = repo_path / contract.expected_paths[0]
    if not source_path.is_file():
        raise ValueError(f"candidate source is missing: {contract.expected_paths[0]}")
    source = source_path.read_text(encoding="utf-8")
    return _fix_single_worker_verdict(contract.oracle_id, source, str(source_path), timeout_s=timeout_s)


def build_fix_multi_contract(task: Mapping[str, Any]) -> FixMultiContract:
    """Freeze one canonical multi-file task and its complete-caller oracle."""
    task_id = task.get("id")
    if not isinstance(task_id, str) or task_id not in _FIX_MULTI_PATHS or task.get("type") != "fix_multicaller":
        raise ValueError("unknown fix-multi task")
    if not isinstance(task.get("expected_files"), list):
        raise ValueError(f"task {task_id} must retain its canonical expected-files field")
    oracle = {"task_id": task_id, "paths": _FIX_MULTI_PATHS[task_id], "semantics": "complete-caller AST oracle"}
    return FixMultiContract(
        task_id=task_id,
        canonical_task_sha256=canonical_task_hash(task),
        prompt_sha256=prompt_hash(task),
        baseline_commit=_FIX_MULTI_BASELINE_COMMIT,
        expected_paths=_FIX_MULTI_PATHS[task_id],
        oracle_sha256=_sha256(oracle),
        scorer_sha256=_sha256(
            {"version": _FIX_MULTI_SCORER_VERSION[task_id], "primary": "complete_callers_and_ast_contract"}
        ),
    )


def validate_fix_multi_binding(contract: FixMultiContract, observed: Mapping[str, object]) -> None:
    """Reject provider evidence that changes a shared task, oracle, or score field."""
    if dict(observed) != dict(contract.provider_binding()):
        raise ValueError("provider adapter changed fix-multi scientific fields")


def run_fix_multi_oracle(repo_path: Path, contract: FixMultiContract) -> bool:
    """Return whether every required caller and method contract is satisfied."""
    trees = {path: ast.parse((repo_path / path).read_text(encoding="utf-8")) for path in contract.expected_paths}
    if contract.task_id == "FM-01":
        return _check_early_stopping(next(iter(trees.values())))
    if contract.task_id == "FM-02":
        return _check_model_checkpoint(next(iter(trees.values())))
    return _check_strategy_callers(trees)


def _check_early_stopping(tree: ast.Module) -> bool:
    """Require an observe-only parameter, branch, and explicit false at each caller."""
    cls = _class(tree, "EarlyStopping")
    method = _method(cls, "_run_early_stopping_check")
    callers = _self_calls(cls, "_run_early_stopping_check")
    return (
        _parameter_false(method, "dry_run")
        and len(callers) == 2
        and all(_keyword_false(call, "dry_run") for call in callers)
        and _observe_only_dry_run_branch(method)
    )


def _check_model_checkpoint(tree: ast.Module) -> bool:
    """Require explicit checkpoint provenance and a log at the persistence boundary."""
    cls = _class(tree, "ModelCheckpoint")
    method = _method(cls, "_save_checkpoint")
    callers = _self_calls_by_method(cls, "_save_checkpoint")
    expected_reasons = {
        "on_exception": "exception",
        "_save_last_checkpoint": "last",
        "_save_none_monitor_checkpoint": "none",
        "_update_best_and_save": "top_k",
    }
    return (
        _parameter_empty_string(method, "reason")
        and set(callers) == set(expected_reasons)
        and all(
            len(calls) == 1 and _keyword_exact_string(calls[0], "reason", reason)
            for caller, reason in expected_reasons.items()
            for calls in (callers.get(caller, []),)
        )
        and _reason_log_precedes_save(method)
    )


def _check_strategy_callers(trees: Mapping[str, ast.Module]) -> bool:
    """Require cooperative environment setup propagation without changing existing behavior."""
    names = {
        f"{_FIX_MULTI_ROOT}/strategies/strategy.py": "Strategy",
        f"{_FIX_MULTI_ROOT}/strategies/ddp.py": "DDPStrategy",
        f"{_FIX_MULTI_ROOT}/strategies/fsdp.py": "FSDPStrategy",
        f"{_FIX_MULTI_ROOT}/strategies/deepspeed.py": "DeepSpeedStrategy",
        f"{_FIX_MULTI_ROOT}/strategies/model_parallel.py": "ModelParallelStrategy",
        f"{_FIX_MULTI_ROOT}/strategies/xla.py": "XLAStrategy",
    }
    methods = {path: _method(_class(trees[path], name), "setup_environment") for path, name in names.items()}
    base_path = f"{_FIX_MULTI_ROOT}/strategies/strategy.py"
    return (
        _parameter_false(methods[base_path], "verbose")
        and _verbose_environment_log_precedes_device_setup(methods[base_path])
        and all(
            _parameter_false(method, "verbose")
            and _super_verbose_environment_call_present(method)
            and not _super_setup_call_present(method)
            for path, method in methods.items()
            if path != base_path
        )
        and all(
            _normalized_setup_environment_body_sha256(method, is_base=path == base_path)
            == _SETUP_ENVIRONMENT_BODY_SHA256[names[path]]
            for path, method in methods.items()
        )
    )


def _class(tree: ast.Module, name: str) -> ast.ClassDef:
    """Return a named top-level class or reject malformed candidate source."""
    node = next((item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == name), None)
    if node is None:
        raise ValueError(f"candidate lacks class {name}")
    return node


def _method(cls: ast.ClassDef, name: str) -> ast.FunctionDef:
    """Return a named direct method or reject malformed candidate source."""
    node = next((item for item in cls.body if isinstance(item, ast.FunctionDef) and item.name == name), None)
    if node is None:
        raise ValueError(f"candidate lacks method {cls.name}.{name}")
    return node


def _parameter_false(method: ast.FunctionDef, name: str) -> bool:
    """Require one parameter with the exact false default."""
    pairs = list(zip(method.args.args[-len(method.args.defaults) :], method.args.defaults))
    return any(
        arg.arg == name and isinstance(default, ast.Constant) and default.value is False for arg, default in pairs
    )


def _parameter_empty_string(method: ast.FunctionDef, name: str) -> bool:
    """Require one parameter with the exact empty-string default."""
    pairs = list(zip(method.args.args[-len(method.args.defaults) :], method.args.defaults))
    return any(arg.arg == name and isinstance(default, ast.Constant) and default.value == "" for arg, default in pairs)


def _self_calls(cls: ast.ClassDef, method_name: str) -> list[ast.Call]:
    """Return every internal self call excluding the target method body."""
    calls = []
    for method in (item for item in cls.body if isinstance(item, ast.FunctionDef) and item.name != method_name):
        calls.extend(_calls_to_self_method(method, method_name))
    return calls


def _self_calls_by_method(cls: ast.ClassDef, method_name: str) -> dict[str, list[ast.Call]]:
    """Return each direct method's internal calls to the target method."""
    calls_by_method: dict[str, list[ast.Call]] = {}
    for method in cls.body:
        if not isinstance(method, ast.FunctionDef) or method.name == method_name:
            continue
        calls = _calls_to_self_method(method, method_name)
        if calls:
            calls_by_method[method.name] = calls
    return calls_by_method


def _calls_to_self_method(method: ast.FunctionDef, method_name: str) -> list[ast.Call]:
    """Return all direct self calls to one named method within a method body."""
    return [
        call
        for call in ast.walk(method)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "self"
        and call.func.attr == method_name
    ]


def _keyword_false(call: ast.Call, name: str) -> bool:
    """Return whether a call explicitly supplies false for one keyword."""
    return any(
        keyword.arg == name and isinstance(keyword.value, ast.Constant) and keyword.value.value is False
        for keyword in call.keywords
    )


def _keyword_exact_string(call: ast.Call, name: str, expected: str) -> bool:
    """Return whether a call supplies the one contractually required string."""
    return any(
        keyword.arg == name and isinstance(keyword.value, ast.Constant) and keyword.value.value == expected
        for keyword in call.keywords
    )


def _observe_only_dry_run_branch(method: ast.FunctionDef) -> bool:
    """Require a decision-bearing dry-run path isolated from persistent mutation."""
    branches = [
        node
        for node in method.body
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "dry_run"
    ]
    if not _has_unconditionally_logged_dry_run_decision(method):
        return False

    if len(branches) == 1:
        branch = branches[0]
        branch_index = method.body.index(branch)
        prior_text = "\n".join(ast.unparse(node) for node in method.body[:branch_index])
        dry_run_text = "\n".join(ast.unparse(node) for node in branch.body)
        following_text = "\n".join(ast.unparse(node) for node in method.body[branch_index + 1 :])
        returns_from_dry_run = any(isinstance(node, ast.Return) for node in branch.body)
        if returns_from_dry_run:
            normal_path_text = "\n".join(ast.unparse(node) for node in branch.orelse) + following_text
        elif branch.orelse:
            normal_path_text = "\n".join(ast.unparse(node) for node in branch.orelse)
        else:
            return _normal_mutations_stay_under_negative_guard(method)
        writes_state = "trainer.should_stop" in dry_run_text or "self.stopped_epoch" in dry_run_text
        mutated_before_branch = "trainer.should_stop" in prior_text or "self.stopped_epoch" in prior_text
        dry_run_falls_through_to_mutation = not returns_from_dry_run and (
            "trainer.should_stop" in following_text or "self.stopped_epoch" in following_text
        )
        preserves_normal_mutations = (
            "trainer.should_stop" in normal_path_text and "self.stopped_epoch" in normal_path_text
        )
        return (
            not writes_state
            and not mutated_before_branch
            and not dry_run_falls_through_to_mutation
            and preserves_normal_mutations
        )

    return _normal_mutations_stay_under_negative_guard(method)


def _normal_mutations_stay_under_negative_guard(method: ast.FunctionDef) -> bool:
    """Require all persistent state writes to stay below one ``if not dry_run`` guard."""
    negative_guards = [
        node
        for node in method.body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.UnaryOp)
        and isinstance(node.test.op, ast.Not)
        and isinstance(node.test.operand, ast.Name)
        and node.test.operand.id == "dry_run"
    ]
    if len(negative_guards) != 1 or negative_guards[0].orelse:
        return False
    normal_path_text = "\n".join(ast.unparse(node) for node in negative_guards[0].body)
    outside_guard_text = "\n".join(ast.unparse(node) for node in method.body if node is not negative_guards[0])
    return (
        "trainer.should_stop" in normal_path_text
        and "self.stopped_epoch" in normal_path_text
        and "trainer.should_stop" not in outside_guard_text
        and "self.stopped_epoch" not in outside_guard_text
    )


def _has_unconditionally_logged_dry_run_decision(method: ast.FunctionDef) -> bool:
    """Require dry-run logging that always exposes the computed decision values."""
    parents = {child: parent for parent in ast.walk(method) for child in ast.iter_child_nodes(parent)}
    for branch in ast.walk(method):
        if not isinstance(branch, ast.If) or not isinstance(branch.test, ast.Name) or branch.test.id != "dry_run":
            continue
        for node in ast.walk(branch):
            if not isinstance(node, ast.Call) or not _is_dry_run_body_node(node, branch, parents):
                continue
            if _is_decision_gated(node, parents) or not _is_log_call(node):
                continue
            if _decision_log_references_computed_values(node, method, parents):
                return True
    return False


def _is_dry_run_body_node(node: ast.AST, branch: ast.If, parents: Mapping[ast.AST, ast.AST]) -> bool:
    """Return whether a node occurs on the true path of one ``if dry_run`` branch."""
    current = node
    while parents.get(current) is not branch:
        parent = parents.get(current)
        if parent is None:
            return False
        current = parent
    return current in branch.body


def _is_decision_gated(node: ast.AST, parents: Mapping[ast.AST, ast.AST]) -> bool:
    """Return whether a log is conditional on ``reason`` or ``verbose``."""
    current = node
    while (parent := parents.get(current)) is not None:
        if isinstance(parent, ast.If):
            gate_names = {name.id for name in ast.walk(parent.test) if isinstance(name, ast.Name)}
            gate_attributes = {
                attribute.attr for attribute in ast.walk(parent.test) if isinstance(attribute, ast.Attribute)
            }
            if gate_names & {"reason", "verbose"} or gate_attributes & {"reason", "verbose"}:
                return True
        current = parent
    return False


def _is_log_call(node: ast.Call) -> bool:
    """Return whether one call uses the callback's normal log boundary."""
    return "log." in ast.unparse(node.func) or "_log_info" in ast.unparse(node.func)


def _decision_log_references_computed_values(
    node: ast.Call, method: ast.FunctionDef, parents: Mapping[ast.AST, ast.AST]
) -> bool:
    """Require a log argument to interpolate both values directly or through one local message."""
    values = [*node.args, *(keyword.value for keyword in node.keywords)]
    if any(_interpolates_decision_values(value) for value in values):
        return True
    for value in values:
        for name in ast.walk(value):
            if not isinstance(name, ast.Name):
                continue
            assigned_value = _nearest_ungated_assignment_value(method, node, name.id, parents)
            if assigned_value is not None and _interpolates_decision_values(assigned_value):
                return True
    return False


def _nearest_ungated_assignment_value(
    method: ast.FunctionDef, call: ast.Call, name: str, parents: Mapping[ast.AST, ast.AST]
) -> ast.expr | None:
    """Return the nearest prior local assignment that can reach an unconditional log."""
    assignments: list[tuple[int, ast.expr]] = []
    for node in ast.walk(method):
        if _is_decision_gated(node, parents) or getattr(node, "lineno", 0) >= call.lineno:
            continue
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
            or (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == name
                and node.value
            )
        ):
            assignments.append((node.lineno, node.value))
    return max(assignments, default=(-1, None), key=lambda assignment: assignment[0])[1]


def _interpolates_decision_values(value: ast.expr) -> bool:
    """Return whether one f-string exposes both decision values in the required message form."""
    if not isinstance(value, ast.JoinedStr):
        return False
    names = {name.id for name in ast.walk(value) if isinstance(name, ast.Name)}
    literals = "".join(
        part.value for part in value.values if isinstance(part, ast.Constant) and isinstance(part.value, str)
    )
    return {"should_stop", "reason"}.issubset(names) and "should_stop=" in literals


def _reason_log_precedes_save(method: ast.FunctionDef) -> bool:
    """Require a reason-and-path log immediately before checkpoint persistence."""
    body = method.body
    for index, node in enumerate(body[:-1]):
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Name) or node.test.id != "reason":
            continue
        if not any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "rank_zero_info"
            and "reason" in ast.unparse(child)
            and "filepath" in ast.unparse(child)
            for child in ast.walk(node)
        ):
            return False
        following = body[index + 1]
        return (
            isinstance(following, ast.Expr)
            and isinstance(following.value, ast.Call)
            and isinstance(following.value.func, ast.Attribute)
            and isinstance(following.value.func.value, ast.Name)
            and following.value.func.value.id == "trainer"
            and following.value.func.attr == "save_checkpoint"
        )
    return False


def _verbose_environment_log_precedes_device_setup(method: ast.FunctionDef) -> bool:
    """Require one verbose-gated debug log before the existing device setup call."""
    verbose_logs = [
        index
        for index, node in enumerate(method.body)
        if isinstance(node, ast.If) and "verbose" in ast.unparse(node.test) and "log.debug" in ast.unparse(node)
    ]
    device_setup = [index for index, node in enumerate(method.body) if "accelerator.setup_device" in ast.unparse(node)]
    return len(verbose_logs) == 1 and len(device_setup) == 1 and verbose_logs[0] < device_setup[0]


def _super_verbose_environment_call_present(method: ast.FunctionDef) -> bool:
    """Require exactly one cooperative environment setup call carrying verbose."""
    calls = [
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "setup_environment"
        and isinstance(node.func.value, ast.Call)
        and isinstance(node.func.value.func, ast.Name)
        and node.func.value.func.id == "super"
    ]
    return len(calls) == 1 and any(
        keyword.arg == "verbose" and isinstance(keyword.value, ast.Name) and keyword.value.id == "verbose"
        for keyword in calls[0].keywords
    )


def _super_setup_call_present(method: ast.FunctionDef) -> bool:
    """Reject the behavior-breaking full setup call from environment overrides."""
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "setup"
        and isinstance(node.func.value, ast.Call)
        and isinstance(node.func.value.func, ast.Name)
        and node.func.value.func.id == "super"
        for node in ast.walk(method)
    )


def _normalized_setup_environment_body_sha256(method: ast.FunctionDef, *, is_base: bool) -> str:
    """Hash pre-existing behavior after removing only requested changes and the docstring."""
    body = copy.deepcopy(method.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body.pop(0)
    if is_base:
        body = [
            node
            for node in body
            if not (
                isinstance(node, ast.If) and "verbose" in ast.unparse(node.test) and "log.debug" in ast.unparse(node)
            )
        ]
    else:
        for node in ast.walk(ast.Module(body=body, type_ignores=[])):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "setup_environment"
                and isinstance(node.func.value, ast.Call)
                and isinstance(node.func.value.func, ast.Name)
                and node.func.value.func.id == "super"
            ):
                node.keywords = [keyword for keyword in node.keywords if keyword.arg != "verbose"]
    payload = ast.dump(ast.Module(body=body, type_ignores=[]), include_attributes=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_edit_task_contract(task: Mapping[str, Any]) -> EditTaskContract:
    """Validate one mutable task and freeze its shared scientific fields.

    A task must name its exact baseline commit, expected changed paths, one targeted executable test, and any relevant
    safety-test commands. Keyword and path recall remain diagnostics and are never the primary oracle.
    """
    task_id = _required_string(task, "id")
    task_type = _required_string(task, "type")
    baseline_commit = _required_string(task, "pre_fix_commit")
    if _GIT_COMMIT_RE.fullmatch(baseline_commit) is None:
        raise ValueError("pre_fix_commit must be a lowercase 40-character Git commit")
    targeted_test_command = _required_string(task, "test_command")
    expected_paths = _required_string_sequence(task, "gt_files_changed")
    regression_test_commands = _required_string_sequence(task, "regression_test_commands")
    test_fixture_patch = _required_string(task, "test_fixture_patch")
    fixture_paths = _unified_diff_paths(test_fixture_patch)
    if set(expected_paths) & set(fixture_paths):
        raise ValueError("test_fixture_patch paths must not overlap gt_files_changed")
    diagnostic_keywords = _optional_string_sequence(task, "expected_patch_keywords")
    answer_contract = {
        "format": "harness_captured_git_diff",
        "capture": "git_diff_binary_no_ext_diff",
        "requires_apply": True,
    }
    oracle = {
        "primary": ("patch_applied", "targeted_test_passed"),
        "safety": "regression_test_passed",
        "expected_paths": expected_paths,
        "test_fixture_patch_sha256": hashlib.sha256(test_fixture_patch.encode("utf-8")).hexdigest(),
        "fixture_paths": fixture_paths,
        "targeted_test_command": targeted_test_command,
        "regression_test_commands": regression_test_commands,
    }
    return EditTaskContract(
        task_id=task_id,
        task_type=task_type,
        canonical_task_sha256=canonical_task_hash(task),
        prompt_sha256=prompt_hash(task),
        baseline_commit=baseline_commit,
        expected_paths=expected_paths,
        test_fixture_patch=test_fixture_patch,
        fixture_paths=fixture_paths,
        test_fixture_patch_sha256=hashlib.sha256(test_fixture_patch.encode("utf-8")).hexdigest(),
        targeted_test_command=targeted_test_command,
        regression_test_commands=regression_test_commands,
        diagnostic_keywords=diagnostic_keywords,
        answer_contract_sha256=_sha256(answer_contract),
        oracle_sha256=_sha256(oracle),
        scorer_sha256=_sha256({"version": _SCORER_VERSION}),
        exclusions_sha256=_sha256(dict(_EXCLUSIONS)),
    )


def assess_patch_answer(text: str) -> PatchAnswer:
    """Extract one fenced diff while preserving unified-diff context whitespace.

    Require one ``diff`` fence with a newline after its opening marker and exactly one closing fence in the remaining
    text. Remove only the fence boundary newlines, then delegate the prefix check and digest construction to
    :func:`build_patch_answer`. Raise ``TypeError`` for non-text input and ``ValueError`` for an invalid envelope. Patch
    applicability is checked later.
    """
    if not isinstance(text, str):
        raise TypeError("patch answer text must be a string")
    sections = text.split("```diff")
    if len(sections) != 2:
        raise ValueError("patch answer requires exactly one fenced diff block")
    _, fenced = sections
    if not fenced.startswith("\n") or fenced.count("```") != 1:
        raise ValueError("patch answer requires exactly one fenced diff block")
    diff, _ = fenced.split("```", maxsplit=1)
    # Preserve unified-diff context markers. In particular, a trailing blank
    # source line is encoded as ``" \n"`` and ``strip()`` would silently turn
    # an otherwise valid hunk into a corrupt patch.
    diff = diff[1:]
    diff = diff.removesuffix("\n")
    return build_patch_answer(diff)


def build_patch_answer(diff: str) -> PatchAnswer:
    """Wrap non-empty Git diff text with the SHA-256 digest of its exact UTF-8 bytes.

    Require the ``diff --git `` prefix; raise ``ValueError`` for any other
    input. This checks the answer envelope, not hunk syntax or applicability.
    Preserve trailing whitespace and newlines for the subsequent patch executor.

    Examples:
        >>> build_patch_answer("")
        Traceback (most recent call last):
        ...
        ValueError: patch answer must contain a unified diff
    """
    if not isinstance(diff, str) or not diff or not diff.startswith("diff --git "):
        raise ValueError("patch answer must contain a unified diff")
    return PatchAnswer(diff=diff, sha256=hashlib.sha256(diff.encode("utf-8")).hexdigest())


def score_edit_execution(contract: EditTaskContract, answer: PatchAnswer, execution: EditExecution) -> EditScore:
    """Score executable patch evidence while retaining heuristic recall as diagnostics."""
    if not isinstance(contract, EditTaskContract):
        raise TypeError("contract must be an EditTaskContract")
    if not isinstance(answer, PatchAnswer):
        raise TypeError("answer must be a PatchAnswer")
    if not isinstance(execution, EditExecution):
        raise TypeError("execution must be an EditExecution")
    changed_paths = _normalized_paths(execution.changed_paths)
    expected_paths = set(contract.expected_paths)
    changed_path_boundary_passed = set(changed_paths) == expected_paths
    lifecycle_intact = (
        execution.baseline_target_failed
        and execution.baseline_regressions_passed
        and execution.fixture_intact
        and execution.source_integrity
        and execution.index_integrity is not False
        and execution.cleanup_verified
    )
    primary_correct = lifecycle_intact and execution.patch_applied and execution.targeted_test_passed
    safety_passed = lifecycle_intact and execution.regression_test_passed is True
    diagnostics = MappingProxyType(
        {
            "expected_path_recall": _recall(expected_paths, set(changed_paths)),
            "keyword_recall": _keyword_recall(contract.diagnostic_keywords, answer.diff),
        }
    )
    return EditScore(
        primary_correct=primary_correct,
        safety_passed=safety_passed,
        changed_path_boundary_passed=changed_path_boundary_passed,
        pooling_eligible=primary_correct and safety_passed and changed_path_boundary_passed,
        diagnostics=diagnostics,
    )


def validate_provider_binding(
    contract: EditTaskContract,
    stage_identity: StageIdentity,
    reported_hashes: Mapping[str, str],
) -> None:
    """Reject a provider adapter that changes any scientific contract field."""
    expected = contract.scientific_field_hashes(stage_identity)
    if dict(reported_hashes) != dict(expected):
        raise ValueError("provider adapter changed provider-neutral scientific fields")


def compare_stage_identities(expected: Mapping[str, StageIdentity], observed: Mapping[str, StageIdentity]) -> None:
    """Fail closed when a new stage changes immutable earlier-stage identity."""
    if set(expected) != set(observed):
        raise ValueError("prior-stage identity set changed")
    for stage, prior in expected.items():
        if observed[stage] != prior:
            raise ValueError(f"prior-stage identity changed for {stage!r}")


def stage_contract_sha256(contracts: Sequence[EditTaskContract]) -> str:
    """Return the ordered provider-neutral scientific-contract digest for a stage."""
    task_ids = [contract.task_id for contract in contracts]
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("stage contracts contain duplicate task ids")
    return _sha256(
        {
            "schema": "provider-neutral-edit-stage-v1",
            "contracts": [
                {
                    "task_id": contract.task_id,
                    "canonical_task_sha256": contract.canonical_task_sha256,
                    "prompt_sha256": contract.prompt_sha256,
                    "baseline_commit": contract.baseline_commit,
                    "expected_paths": contract.expected_paths,
                    "test_fixture_patch_sha256": contract.test_fixture_patch_sha256,
                    "fixture_paths": contract.fixture_paths,
                    "targeted_test_command": contract.targeted_test_command,
                    "regression_test_commands": contract.regression_test_commands,
                    "answer_contract_sha256": contract.answer_contract_sha256,
                    "oracle_sha256": contract.oracle_sha256,
                    "scorer_sha256": contract.scorer_sha256,
                    "exclusions_sha256": contract.exclusions_sha256,
                }
                for contract in contracts
            ],
        }
    )


def _is_absolute_anywhere(value: str) -> bool:
    """Return whether a recorded path is absolute under POSIX or Windows rules.

    A lock document is portable data: the host reading one does not get to decide whether
    the path another host recorded was absolute. ``os.path.isabs`` cannot stand in here —
    ``ntpath.isabs("/x")`` changed its answer in Python 3.13, so a host-flavoured check
    would begin rejecting valid locks on an interpreter upgrade with no other signal.

    Args:
        value: Recorded path string from a lock document.

    Returns:
        Whether either path flavour considers the value absolute.

    Examples:
        >>> _is_absolute_anywhere("/canonical/checkout")
        True
        >>> _is_absolute_anywhere(r"C:\\canonical\\checkout")
        True
        >>> _is_absolute_anywhere("relative/checkout")
        False
    """
    return PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()


def _rerooted_posix(value: str, runtime_root: str) -> str:
    """Replace runtime-root prefixes and render each surviving tail separator-free.

    Stripping the root is not enough to make a digest portable: the path that remains
    still carries the recording host's separator, so the same graph hashes differently
    on Windows than on POSIX and a lock recorded on either can never verify on the other.
    Only a tail that followed a matched root is renormalized — a backslash elsewhere in a
    payload is content, not a separator, and rewriting it would corrupt the digest.

    Args:
        value: One string leaf from an index payload.
        runtime_root: Resolved scan root to strip, in the recording host's own form.

    Returns:
        The string with each root occurrence replaced and its tail in POSIX form.

    Examples:
        >>> _rerooted_posix("/repo/src/app.py", "/repo")
        '<runtime-root>/src/app.py'
        >>> _rerooted_posix(r"D:\\repo\\src\\app.py", r"D:\\repo")
        '<runtime-root>/src/app.py'
        >>> _rerooted_posix("unrelated content", "/repo")
        'unrelated content'
    """
    if runtime_root not in value:
        return value
    head, *tails = value.split(runtime_root)
    rendered = [head]
    for tail in tails:
        rendered.append("<runtime-root>")
        if tail:
            rendered.append(PureWindowsPath(tail).as_posix())
    return "".join(rendered)


def semantic_index_sha256(payload: Mapping[str, Any], source_root: PurePath) -> str:
    """Hash graph content after removing only runtime-root and scan-time metadata.

    The digest is graph identity, so it must not depend on which host recorded the scan:
    the runtime root is stripped and every surviving path tail is rendered in POSIX form.
    A pure path is accepted and taken as already resolved, which is how a scan recorded on
    the other platform is re-hashed without a matching filesystem present.

    Args:
        payload: Parsed index document.
        source_root: Scan root to strip, as a concrete or pure path.

    Returns:
        The hex digest of the root-stripped, separator-normalized payload.

    Examples:
        >>> from pathlib import PurePosixPath, PureWindowsPath
        >>> posix = {"modules": [{"file": "/repo/src/app.py"}]}
        >>> windows = {"modules": [{"file": r"D:\\repo\\src\\app.py"}]}
        >>> digest = semantic_index_sha256(posix, PurePosixPath("/repo"))
        >>> digest == semantic_index_sha256(windows, PureWindowsPath(r"D:\\repo"))
        True
    """
    resolve = getattr(source_root, "resolve", None)
    runtime_root = str(source_root if resolve is None else resolve())

    def replace_root(value: Any) -> Any:
        """Replace embedded runtime-root prefixes without changing structure."""
        if isinstance(value, str):
            return _rerooted_posix(value, runtime_root)
        if isinstance(value, list):
            return [replace_root(item) for item in value]
        if isinstance(value, dict):
            return {key: replace_root(item) for key, item in value.items()}
        return value

    semantic_payload = {key: value for key, value in payload.items() if key not in {"scan_root", "scanned_at"}}
    normalized = replace_root(semantic_payload)
    return hashlib.sha256(json.dumps(normalized, separators=(",", ":"), sort_keys=True).encode("utf-8")).hexdigest()


def validate_patch_index_bundle(
    source_root: Path, locks_path: Path, contracts: Sequence[EditTaskContract]
) -> dict[str, dict[str, str]]:
    """Validate selected historical indexes against shared task and graph locks.

    The preparation utility and both provider adapters call this same boundary.
    It rejects a post-build byte change, a source-root mismatch, a task/baseline
    mismatch, or semantic graph drift before a model is started.

    Args:
        source_root: Clean orchestration checkout containing the task indexes.
        locks_path: Reviewed patch-index lock document.
        contracts: Selected provider-neutral Patch contracts.

    Returns:
        Per-task immutable source/index coordinates for scope hashing.

    Raises:
        ValueError: If a lock or installed historical index is missing or drifts.
    """
    source_root = source_root.resolve(strict=True)
    locks_path = locks_path.resolve(strict=True)
    try:
        document = json.loads(locks_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"patch index locks are unreadable: {locks_path}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != "provider-parity-patch-index-locks-v1":
        raise ValueError("patch index locks use an unsupported schema")
    canonical_root = document.get("canonical_scan_root")
    locks = document.get("tasks")
    if not isinstance(canonical_root, str) or not _is_absolute_anywhere(canonical_root) or not isinstance(locks, dict):
        raise ValueError("patch index locks require an absolute canonical root and tasks object")
    task_ids = [contract.task_id for contract in contracts]
    if not task_ids or len(task_ids) != len(set(task_ids)):
        raise ValueError("patch index validation requires unique selected contracts")
    coordinates: dict[str, dict[str, str]] = {}
    for contract in contracts:
        lock = locks.get(contract.task_id)
        if not isinstance(lock, dict) or lock.get("baseline_commit") != contract.baseline_commit:
            raise ValueError(f"patch index lock baseline does not match {contract.task_id}")
        index_path = source_root / ".cache" / "codemap" / "patch" / f"{contract.task_id}.json"
        try:
            index_bytes = index_path.read_bytes()
            payload = json.loads(index_bytes)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"patch index is unreadable for {contract.task_id}: {index_path}") from exc
        if not isinstance(payload, dict) or payload.get("scan_root") != str(source_root):
            raise ValueError(f"patch index scan_root does not match source for {contract.task_id}")
        expected_fields = {
            "project": f"provider-parity-{contract.task_id}",
            "scanned_at": lock.get("scanned_at"),
            "scan_version": lock.get("scan_version"),
        }
        if any(payload.get(field) != value for field, value in expected_fields.items()):
            raise ValueError(f"patch index metadata drifted for {contract.task_id}")
        modules = payload.get("modules")
        if not isinstance(modules, list) or len(modules) != lock.get("module_count"):
            raise ValueError(f"patch index module count drifted for {contract.task_id}")
        semantic_sha256 = semantic_index_sha256(payload, source_root)
        if semantic_sha256 != lock.get("semantic_sha256"):
            raise ValueError(f"patch index semantic SHA-256 drifted for {contract.task_id}")
        raw_sha256 = hashlib.sha256(index_bytes).hexdigest()
        if str(source_root) == canonical_root and raw_sha256 != lock.get("raw_sha256_at_canonical_root"):
            raise ValueError(f"patch index raw SHA-256 drifted for {contract.task_id}")
        coordinates[contract.task_id] = {
            "baseline_commit": contract.baseline_commit,
            "index_path": str(index_path),
            "index_sha256": raw_sha256,
            "raw_index_sha256": raw_sha256,
            "semantic_index_sha256": semantic_sha256,
            "scan_version": str(lock["scan_version"]),
        }
    return coordinates


def _required_string(task: Mapping[str, Any], field: str) -> str:
    """Return one non-empty task string field."""
    value = task.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"task requires non-empty {field}")
    return value


def _required_string_sequence(task: Mapping[str, Any], field: str) -> tuple[str, ...]:
    """Return a unique non-empty task string-list field."""
    value = _optional_string_sequence(task, field)
    if not value:
        raise ValueError(f"task requires non-empty {field}")
    return value


def _optional_string_sequence(task: Mapping[str, Any], field: str) -> tuple[str, ...]:
    """Return an optional unique string-list task field."""
    value = task.get(field)
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"task {field} must be a string list")
    if len(set(value)) != len(value):
        raise ValueError(f"task {field} must not contain duplicates")
    return tuple(value)


def _unified_diff_paths(diff: str) -> tuple[str, ...]:
    """Return the unique paths added or changed by one trusted fixture diff."""
    paths: list[str] = []
    for line in diff.splitlines():
        if not line.startswith("+++ "):
            continue
        path = line.removeprefix("+++ ")
        if path == "/dev/null":
            continue
        if not path.startswith("b/"):
            raise ValueError("test_fixture_patch must contain Git-style b/ paths")
        paths.append(path[2:])
    normalized = _normalized_paths(paths)
    if not normalized:
        raise ValueError("test_fixture_patch must change at least one path")
    return normalized


def _normalized_paths(paths: Sequence[str]) -> tuple[str, ...]:
    """Validate and normalize one observed changed-path sequence."""
    if any(not isinstance(path, str) or not path or path.startswith("/") or ".." in path.split("/") for path in paths):
        raise ValueError("changed paths must be non-empty relative paths")
    return tuple(dict.fromkeys(paths))


def _recall(expected: set[str], observed: set[str]) -> float:
    """Return expected-set recall, with no empty oracle allowed upstream."""
    return len(expected & observed) / len(expected)


def _keyword_recall(keywords: Sequence[str], diff: str) -> float:
    """Return the fraction of listed keywords present as case-sensitive substrings.

    An empty keyword list scores one. Count each list entry, including
    duplicates; this diagnostic does not determine patch correctness.

    Examples:
        >>> _keyword_recall(["cache", "flush"], "clear cache")
        0.5
        >>> _keyword_recall([], "")
        1.0
    """
    if not keywords:
        return 1.0
    return sum(keyword in diff for keyword in keywords) / len(keywords)


def _sha256(payload: Mapping[str, Any]) -> str:
    """Serialize one immutable contract payload into a canonical digest."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
