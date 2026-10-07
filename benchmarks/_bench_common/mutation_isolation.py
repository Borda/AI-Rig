"""Run one mutable benchmark cell with evidence-preserving cleanup.

The lifecycle is provider-neutral. Callers supply creation, action, and restoration mechanics; this boundary guarantees
that cleanup is attempted on every ordinary exit and that a cleanup failure cannot be mistaken for a valid measurement.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, TypeVar

from .edit_patch_contracts import (
    EditExecution,
    EditTaskContract,
    FixMultiContract,
    FixSingleContract,
    PatchAnswer,
    build_patch_answer,
    run_fix_multi_oracle,
    run_fix_single_oracle,
)

ResultT = TypeVar("ResultT")


class MutationCleanupError(RuntimeError):
    """Raised when a cell's frozen baseline cannot be restored."""


#: Maximum characters of a test command's output kept in the recorded excerpt.
_COMMAND_OUTPUT_LIMIT = 4_096
#: Seconds a targeted test command may run before it is stopped and counted as failed.
_TEST_TIMEOUT_SECONDS = 300
#: Environment variable naming the pytest command used for patch tests; the PATH pytest is the fallback.
PATCH_PYTEST_ENV = "CODEMAP_BENCH_PATCH_PYTEST"


@dataclass(frozen=True)
class PatchTaskAgentWorkspace:
    """Editable patch-task worktree with a staged immutable target-test fixture."""

    workspace: ExecutableAgentWorkspace
    contract: EditTaskContract
    fixture_sha256_by_path: Mapping[str, str]
    baseline_target_failed: bool
    baseline_regressions_passed: bool
    source_head: str
    source_porcelain: str

    def capture_answer(self) -> PatchAnswer:
        """Capture only agent edits; staged fixture changes cannot enter the answer."""
        return build_patch_answer(self.workspace.capture_diff())

    def fixture_intact(self) -> bool:
        """Return whether model edits preserved every staged fixture byte-for-byte."""
        return _paths_match_sha256(self.workspace.worktree, self.fixture_sha256_by_path)

    def source_unchanged(self) -> bool:
        """Return whether the agent preserved the exact source checkout observed before its turn."""
        return _source_state_is_unchanged(self.workspace.source, self.source_head, self.source_porcelain)


@dataclass
class MutationEvidence:
    """Lifecycle evidence retained even when action or cleanup fails."""

    worktree: Path | None = None
    action_error: str | None = None
    cleanup_error: str | None = None
    restored: bool = False


class IsolatedMutationCell:
    """Coordinate create/action/restore operations for one disposable cell."""

    def __init__(self, create: Callable[[], Path], restore: Callable[[Path], None]) -> None:
        """Store the concrete worktree lifecycle owned by a benchmark runner."""
        self._create = create
        self._restore = restore
        self.last_evidence = MutationEvidence()

    def run(self, action: Callable[[Path], ResultT]) -> ResultT:
        """Run one action and fail closed if its disposable baseline is not restored."""
        worktree = self._create()
        evidence = MutationEvidence(worktree=worktree)
        self.last_evidence = evidence
        try:
            return action(worktree)
        except Exception as exc:
            evidence.action_error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            try:
                self._restore(worktree)
            except Exception as cleanup_error:
                evidence.cleanup_error = f"{type(cleanup_error).__name__}: {cleanup_error}"
                message = "mutable cell cleanup failed"
                if evidence.action_error:
                    message = f"{message} after {evidence.action_error}"
                raise MutationCleanupError(message) from cleanup_error
            evidence.restored = True


@dataclass(frozen=True)
class FixExecution:
    """Patch-application and oracle evidence from one candidate lifecycle."""

    baseline_failed: bool
    patch_applied: bool
    changed_paths: tuple[str, ...]
    targeted_test_passed: bool
    recount_recoverable: bool
    recount_oracle_passed: bool | None
    cleanup_verified: bool
    error: str | None
    # Whether the contract's regression commands still pass after the candidate patch.
    # Vacuously ``True`` for a contract declaring no regression commands, matching the
    # patch stage's own ``all(...)`` over an empty command set, so a task that declares
    # none is neither credited nor penalized for regression safety.
    regression_test_passed: bool | None = True

    def as_dict(self) -> dict[str, object]:
        """Return JSON-safe execution evidence."""
        return asdict(self)


def _workspace_git(repo_path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run one bounded Git command against an explicit benchmark repository."""
    return subprocess.run(  # noqa: S603 - argv list, no shell
        ["git", "-C", str(repo_path), *args],  # noqa: S607 - git/tool resolved via PATH on purpose
        check=check,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _apply_fixture(worktree: Path, contract: EditTaskContract) -> None:
    """Apply and stage the trusted target-test fixture before model access."""
    fixture_path = worktree / ".codemap-patch-task-fixture.diff"
    fixture_path.write_text(contract.test_fixture_patch, encoding="utf-8")
    try:
        check = _workspace_git(worktree, "apply", "--check", str(fixture_path), check=False)
        if check.returncode != 0:
            raise ValueError(f"test fixture does not apply cleanly: {check.stderr.strip()[:300]}")
        _workspace_git(worktree, "apply", "--whitespace=nowarn", str(fixture_path))
        _workspace_git(worktree, "add", "--", *contract.fixture_paths)
        staged = tuple(
            line
            for line in _workspace_git(worktree, "diff", "--cached", "--name-only").stdout.splitlines()
            if line.strip()
        )
        if staged != contract.fixture_paths:
            raise ValueError("test fixture staged paths do not match its immutable contract")
    finally:
        fixture_path.unlink(missing_ok=True)


#: The launcher reports its own runtime rather than being parsed for one. A console-script
#: launcher is a text file with a Python shebang only on POSIX; the same entry point installs
#: as a binary trampoline on Windows, where reading a shebang out of it fails outright
#: (`UnicodeDecodeError` on the first line). Asking the admitted launcher to print what it
#: actually loaded is both platform-neutral and a stricter binding: it fingerprints the
#: interpreter that will run the Patch command, not the one a header claims.
_RUNTIME_PROBE_CONFTEST = """
import importlib.metadata as metadata
import json, pathlib, pytest, sys

entry_points = metadata.entry_points()
entry_points = entry_points.select(group='pytest11') if hasattr(entry_points, 'select') else entry_points.get('pytest11', ())
plugins = sorted((item.name, item.value, item.dist.name, item.dist.version) for item in entry_points)
print({token!r} + json.dumps({{'python_executable': sys.executable, 'python_prefix': sys.prefix,
    'python_version': sys.version.split()[0], 'pytest_module': str(pathlib.Path(pytest.__file__).resolve()),
    'pytest_plugins': plugins, 'pytest_version': pytest.__version__}}, sort_keys=True))
"""
#: Seconds the runtime probe may run before it is abandoned.
_RUNTIME_PROBE_TIMEOUT_S = 120.0


def _probe_patch_test_runtime(pytest_executable: Path) -> dict[str, Any]:
    """Return the runtime facts reported by one admitted pytest launcher.

    The launcher runs against an empty throwaway root with its own empty config, so
    neither this repository's addopts nor a collected test can influence the answer.
    Collecting nothing is the expected outcome; only the tokenized line is read, because
    a loaded plugin may write its own banner to the same stream.

    Args:
        pytest_executable: Absolute launcher selected at scope admission.

    Returns:
        The decoded identity mapping printed by the launcher's own interpreter.

    Raises:
        ValueError: If the launcher produced no tokenized identity line.
    """
    token = f"patch-runtime-identity-{os.urandom(8).hex()}:"
    # An outer pytest run exports its own arguments; inheriting them would let the caller's
    # invocation, not the launcher, decide whether the probe can complete.
    environment = {name: value for name, value in os.environ.items() if name != "PYTEST_ADDOPTS"}
    with tempfile.TemporaryDirectory(prefix="codemap-patch-runtime-") as probe_root:
        root = Path(probe_root)
        (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        (root / "conftest.py").write_text(_RUNTIME_PROBE_CONFTEST.format(token=token), encoding="utf-8")
        probe = subprocess.run(  # noqa: S603 - argv list, no shell
            [str(pytest_executable), "-c", str(root / "pytest.ini"), "--rootdir", str(root), "-s", "-q", str(root)],
            cwd=probe_root,
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            env=environment,
            timeout=_RUNTIME_PROBE_TIMEOUT_S,
        )
    reported = [line for line in probe.stdout.splitlines() if line.startswith(token)]
    if not reported:
        detail = (probe.stderr or probe.stdout).strip()[:500]
        raise ValueError(f"Patch task pytest interpreter probe failed (exit {probe.returncode}): {detail}")
    return json.loads(reported[-1][len(token) :])


def patch_test_runtime_identity() -> dict[str, str]:
    """Resolve and fingerprint the pytest launcher used by Patch test commands.

    Patch tasks retain their reviewed command arguments but execute them through the exact absolute pytest selected at
    scope admission. The launcher's interpreter and pytest module are also bound so a later environment change cannot
    silently alter the accepted runtime.
    """
    try:
        pytest_command = os.environ.get(PATCH_PYTEST_ENV) or shutil.which("pytest")
        if pytest_command is None:
            raise ValueError("Patch task pytest executable is unavailable on PATH")
        pytest_executable = Path(pytest_command).absolute()
        if not pytest_executable.is_file():
            raise ValueError(f"Patch task pytest executable is unavailable: {pytest_executable}")
        identity = _probe_patch_test_runtime(pytest_executable)
        resolved_python = Path(identity["python_executable"]).resolve(strict=True)
        pytest_origin = Path(identity["pytest_module"]).resolve(strict=True)
        pytest_plugins = json.dumps(identity["pytest_plugins"], separators=(",", ":"), sort_keys=True)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, KeyError, IndexError) as exc:
        raise ValueError("Patch task pytest runtime is unavailable") from exc
    if not pytest_origin.is_file():
        raise ValueError("Patch task pytest module is unavailable")
    return {
        "python_executable": str(Path(identity["python_executable"]).absolute()),
        "python_sha256": hashlib.sha256(resolved_python.read_bytes()).hexdigest(),
        "python_prefix": str(Path(identity["python_prefix"]).resolve()),
        "python_version": str(identity["python_version"]),
        "pytest_executable": str(pytest_executable),
        "pytest_executable_sha256": hashlib.sha256(pytest_executable.read_bytes()).hexdigest(),
        "pytest_module": str(pytest_origin),
        "pytest_module_sha256": hashlib.sha256(pytest_origin.read_bytes()).hexdigest(),
        "pytest_plugins": pytest_plugins,
        "pytest_plugins_sha256": hashlib.sha256(pytest_plugins.encode("utf-8")).hexdigest(),
        "pytest_version": str(identity["pytest_version"]),
        "invocation": "absolute pytest executable",
    }


def _validated_patch_test_runtime(runtime_identity: Mapping[str, str] | None) -> dict[str, str]:
    """Return the current designated runtime or reject a scope whose bytes drifted."""
    current = patch_test_runtime_identity()
    if runtime_identity is None:
        return current
    if dict(runtime_identity) != current:
        raise ValueError("Patch task pytest runtime changed after scope admission")
    return current


def _run_test_commands(
    worktree: Path,
    commands: Sequence[str],
    prefix: str,
    *,
    runtime_identity: Mapping[str, str] | None = None,
) -> dict[str, Mapping[str, object]]:
    """Run each frozen pytest command and retain bounded, hashable evidence."""
    return {
        f"{prefix}_{index}": _run_test_command(
            worktree, command, f"{prefix}_{index}", runtime_identity=runtime_identity
        )
        for index, command in enumerate(commands, 1)
    }


def _run_test_command(
    worktree: Path, command: str, label: str, *, runtime_identity: Mapping[str, str] | None = None
) -> Mapping[str, object]:
    """Run one frozen command with worktree source precedence and the admitted pytest runtime."""
    argv = shlex.split(command)
    if not argv or Path(argv[0]).name != "pytest":
        raise ValueError(f"{label} must be a pytest command")
    runtime = _validated_patch_test_runtime(runtime_identity)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(worktree / "src")
    completed = subprocess.run(  # noqa: S603 - argv list, no shell
        [runtime["pytest_executable"], *argv[1:]],
        cwd=worktree,
        check=False,
        capture_output=True,
        env=environment,
        text=True,
        timeout=_TEST_TIMEOUT_SECONDS,
    )
    output = f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    return {
        "command": command,
        "runtime": runtime,
        "returncode": completed.returncode,
        "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
        "output_excerpt": output[:_COMMAND_OUTPUT_LIMIT],
    }


def _path_sha256es(worktree: Path, paths: Sequence[str]) -> dict[str, str]:
    """Hash exact fixture bytes so candidate edits cannot hide behind staging."""
    hashes: dict[str, str] = {}
    for path in paths:
        candidate = worktree / path
        if not candidate.is_file():
            raise ValueError(f"test fixture path is not a regular file: {path}")
        hashes[path] = hashlib.sha256(candidate.read_bytes()).hexdigest()
    return hashes


def _paths_match_sha256(worktree: Path, expected: Mapping[str, str]) -> bool:
    """Return whether a recorded fixture has not been deleted or modified."""
    try:
        return _path_sha256es(worktree, tuple(expected)) == dict(expected)
    except (OSError, ValueError):
        return False


def _file_sha256(path: Path | None) -> str | None:
    """Return one frozen file digest or fail closed for an unreadable requested file."""
    if path is None:
        return None
    try:
        return hashlib.sha256(path.resolve(strict=True).read_bytes()).hexdigest()
    except OSError:
        return None


def _source_state_is_unchanged(source: Path, head: str, status: str) -> bool:
    """Verify cleanup preserved the orchestration source's original identity."""
    try:
        return (
            _workspace_git(source, "rev-parse", "HEAD").stdout.strip() == head
            and _workspace_git(source, "status", "--porcelain").stdout == status
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def _verified_source_baseline(repo_path: Path, baseline_commit: str) -> None:
    """Reject a dirty or wrong-revision source before a mutable scoring worktree."""
    try:
        head = _workspace_git(repo_path, "rev-parse", "HEAD").stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise ValueError("source repository must be a readable Git checkout") from exc
    if head != baseline_commit:
        raise ValueError(f"source revision {head!r} does not match {baseline_commit!r}")
    if _workspace_git(repo_path, "status", "--porcelain").stdout.strip():
        raise ValueError("source repository must be clean before a patch cell")


def _verified_patch_task_source(repo_path: Path, baseline_commit: str) -> tuple[str, str]:
    """Validate a clean source while allowing each patch task's historical baseline.

    Patch tasks freeze distinct commits, so the source checkout need only contain the requested commit; it must retain
    its original clean state after scoring.
    """
    try:
        head = _workspace_git(repo_path, "rev-parse", "HEAD").stdout.strip()
        _workspace_git(repo_path, "rev-parse", f"{baseline_commit}^{{commit}}")
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"patch-task baseline commit is unavailable: {baseline_commit}") from exc
    status = _workspace_git(repo_path, "status", "--porcelain").stdout
    if status:
        raise ValueError("source repository must be clean before a patch cell")
    return head, status


def execute_executable_patch(
    repo_path: Path,
    *,
    baseline_commit: str,
    oracle: Callable[[Path], bool],
    diff: str,
    regression_test_commands: Sequence[str] = (),
) -> FixExecution:
    """Apply a candidate patch in a clean scoring worktree and retain diagnostics.

    Normal application and the independent oracle determine the primary result.
    ``--recount`` is recorded only to explain a malformed candidate; it never
    upgrades ``patch_applied`` or the primary score.

    Args:
        repo_path: Frozen benchmark repository holding the baseline commit.
        baseline_commit: Commit the scoring worktree is detached at.
        oracle: Independent behavior oracle; ``True`` when the task is satisfied.
        diff: Candidate patch text.
        regression_test_commands: Frozen commands that must still pass after the patch.
            Empty for a contract that declares none, which leaves
            ``regression_test_passed`` vacuously ``True`` exactly as the patch stage's
            ``all(...)`` over an empty command set does.
    """
    source = repo_path.resolve()
    _verified_source_baseline(source, baseline_commit)
    root = Path(tempfile.mkdtemp(prefix="codemap-executable-patch-"))
    worktree = root / "repo"
    baseline_failed = patch_applied = targeted_test_passed = recount_recoverable = cleanup_verified = created = False
    changed_paths: tuple[str, ...] = ()
    recount_oracle_passed: bool | None = None
    error: str | None = None
    # Vacuously true until a declared regression command actually fails: a contract with
    # no regression commands must not be penalized, and a patch that never applied has
    # broken nothing.
    regression_test_passed: bool | None = True
    try:
        _workspace_git(source, "worktree", "add", "--detach", str(worktree), baseline_commit)
        created = True
        baseline_failed = not oracle(worktree)
        if not baseline_failed:
            error = "baseline unexpectedly satisfies the task oracle"
        if baseline_failed:
            patch_path = root / "candidate.diff"
            patch_path.write_text(diff, encoding="utf-8")
            check = _workspace_git(worktree, "apply", "--check", str(patch_path), check=False)
            if check.returncode != 0:
                recount_recoverable = (
                    _workspace_git(worktree, "apply", "--check", "--recount", str(patch_path), check=False).returncode
                    == 0
                )
                if recount_recoverable:
                    _workspace_git(worktree, "apply", "--recount", "--whitespace=nowarn", str(patch_path))
                    recount_oracle_passed = oracle(worktree)
                error = f"patch does not apply cleanly: {check.stderr.strip()[:300]}"
            else:
                _workspace_git(worktree, "apply", "--whitespace=nowarn", str(patch_path))
                patch_applied = True
                changed_paths = tuple(
                    line for line in _workspace_git(worktree, "diff", "--name-only").stdout.splitlines() if line.strip()
                )
                targeted_test_passed = oracle(worktree)
                regressions = _run_test_commands(worktree, regression_test_commands, "candidate_regression")
                regression_test_passed = all(item["returncode"] == 0 for item in regressions.values())
    except (OSError, ValueError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        error = str(exc)[:1000]
    finally:
        try:
            if created:
                _workspace_git(worktree, "reset", "--hard", "HEAD")
                _workspace_git(worktree, "clean", "-fdx")
                _workspace_git(source, "worktree", "remove", str(worktree))
            cleanup_verified = (
                not worktree.exists()
                and str(worktree) not in _workspace_git(source, "worktree", "list", "--porcelain").stdout
            )
        except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
            error = error or f"cleanup failed: {exc}"
        shutil.rmtree(root, ignore_errors=True)
    return FixExecution(
        baseline_failed,
        patch_applied,
        changed_paths,
        targeted_test_passed,
        recount_recoverable,
        recount_oracle_passed,
        cleanup_verified,
        error,
        regression_test_passed=regression_test_passed,
    )


def execute_fix_single_patch(repo_path: Path, contract: FixSingleContract, diff: str) -> FixExecution:
    """Score one Fix-Single patch in an isolated clean worktree."""
    return execute_executable_patch(
        repo_path,
        baseline_commit=contract.baseline_commit,
        oracle=lambda worktree: run_fix_single_oracle(worktree, contract),
        diff=diff,
        regression_test_commands=contract.regression_test_commands,
    )


def execute_fix_multi_patch(repo_path: Path, contract: FixMultiContract, diff: str) -> FixExecution:
    """Score one Fix-Multi patch in an isolated clean worktree."""
    return execute_executable_patch(
        repo_path,
        baseline_commit=contract.baseline_commit,
        oracle=lambda worktree: run_fix_multi_oracle(worktree, contract),
        diff=diff,
        regression_test_commands=contract.regression_test_commands,
    )


def stage_patch_task_agent_workspace(
    source: Path,
    workspace: ExecutableAgentWorkspace,
    contract: EditTaskContract,
    *,
    runtime_identity: Mapping[str, str] | None = None,
) -> PatchTaskAgentWorkspace:
    """Stage and validate a frozen target-test fixture in an owned clean worktree.

    The fixture is staged so ``git diff`` contains only agent edits. A provider cannot satisfy the task by changing its
    generated target test, because the captured answer and later scorer both verify its frozen bytes.
    """
    source = source.resolve(strict=True)
    source_head, source_porcelain = _verified_patch_task_source(source, contract.baseline_commit)
    _apply_fixture(workspace.worktree, contract)
    fixture_sha256_by_path = _path_sha256es(workspace.worktree, contract.fixture_paths)
    baseline_regressions = _run_test_commands(
        workspace.worktree,
        contract.regression_test_commands,
        "baseline_regression",
        runtime_identity=runtime_identity,
    )
    baseline_target = _run_test_command(
        workspace.worktree,
        contract.targeted_test_command,
        "baseline_target",
        runtime_identity=runtime_identity,
    )
    baseline_regressions_passed = all(item["returncode"] == 0 for item in baseline_regressions.values())
    baseline_target_failed = baseline_target["returncode"] == 1
    if not baseline_regressions_passed or not baseline_target_failed:
        failed_commands = [
            item
            for item in (*baseline_regressions.values(), baseline_target)
            if item["returncode"] != (1 if item is baseline_target else 0)
        ]
        details = "\n\n".join(
            f"command={item['command']!r} exit={item['returncode']}\n{item['output_excerpt']}"
            for item in failed_commands
        )
        raise ValueError(
            f"Patch task {contract.task_id} baseline validation failed before paid execution; "
            f"regressions must exit 0 and target must exit 1.\n{details}"
        )
    return PatchTaskAgentWorkspace(
        workspace=workspace,
        contract=contract,
        fixture_sha256_by_path=fixture_sha256_by_path,
        baseline_target_failed=baseline_target_failed,
        baseline_regressions_passed=baseline_regressions_passed,
        source_head=source_head,
        source_porcelain=source_porcelain,
    )


def create_patch_task_agent_workspace(
    source: Path,
    index_path: Path,
    contract: EditTaskContract,
    *,
    runtime_identity: Mapping[str, str] | None = None,
) -> PatchTaskAgentWorkspace:
    """Create an isolated Patch worktree and stage its fixture after creation."""
    source = source.resolve(strict=True)
    workspace = create_executable_agent_workspace(
        source, index_path, contract.baseline_commit, require_source_baseline=False
    )
    try:
        return stage_patch_task_agent_workspace(
            source,
            workspace,
            contract,
            runtime_identity=runtime_identity,
        )
    except BaseException:
        workspace.cleanup()
        raise


def execute_patch_task_answer(
    repo_path: Path,
    contract: EditTaskContract,
    answer: PatchAnswer,
    *,
    index_path: Path | None = None,
    runtime_identity: Mapping[str, str] | None = None,
) -> EditExecution:
    """Score one direct or text patch in a second clean worktree without fallback application.

    The function accepts the captured diff from a direct agent worktree or a fenced textual answer parsed by
    :func:`assess_patch_answer`. It never uses ``git apply --recount``: an ordinary apply failure remains failed
    evidence.
    """
    if not isinstance(contract, EditTaskContract):
        raise TypeError("contract must be an EditTaskContract")
    if not isinstance(answer, PatchAnswer):
        raise TypeError("answer must be a PatchAnswer")
    source = repo_path.resolve()
    source_head, source_status = _verified_patch_task_source(source, contract.baseline_commit)
    source_index_sha256 = _file_sha256(index_path) if index_path is not None else None
    root = Path(tempfile.mkdtemp(prefix="codemap-patch-task-"))
    worktree = root / "repo"
    created = False
    baseline_target_failed = baseline_regressions_passed = fixture_intact = source_integrity = cleanup_verified = False
    patch_applied = targeted_test_passed = False
    regression_test_passed: bool | None = None
    changed_paths: tuple[str, ...] = ()
    command_evidence: dict[str, Mapping[str, object]] = {}
    error: str | None = None
    try:
        _workspace_git(source, "worktree", "add", "--detach", str(worktree), contract.baseline_commit)
        created = True
        _apply_fixture(worktree, contract)
        fixture_sha256_by_path = _path_sha256es(worktree, contract.fixture_paths)
        baseline_regressions = _run_test_commands(
            worktree,
            contract.regression_test_commands,
            "baseline_regression",
            runtime_identity=runtime_identity,
        )
        command_evidence.update(baseline_regressions)
        baseline_target = _run_test_command(
            worktree,
            contract.targeted_test_command,
            "baseline_target",
            runtime_identity=runtime_identity,
        )
        command_evidence["baseline_target"] = baseline_target
        baseline_regressions_passed = all(item["returncode"] == 0 for item in baseline_regressions.values())
        baseline_target_failed = baseline_target["returncode"] == 1
        if not baseline_regressions_passed:
            error = "baseline regression command failed"
        elif not baseline_target_failed:
            error = "baseline target command must fail with pytest exit 1"
        else:
            patch_path = root / "candidate.diff"
            patch_path.write_text(f"{answer.diff}\n", encoding="utf-8")
            check = _workspace_git(worktree, "apply", "--check", str(patch_path), check=False)
            if check.returncode != 0:
                error = f"patch does not apply cleanly: {check.stderr.strip()[:300]}"
            else:
                _workspace_git(worktree, "apply", "--whitespace=nowarn", str(patch_path))
                patch_applied = True
                fixture_intact = _paths_match_sha256(worktree, fixture_sha256_by_path)
                changed_paths = tuple(
                    path
                    for path in _workspace_git(worktree, "diff", "--name-only").stdout.splitlines()
                    if path.strip() and path not in contract.fixture_paths
                )
                target = _run_test_command(
                    worktree,
                    contract.targeted_test_command,
                    "candidate_target",
                    runtime_identity=runtime_identity,
                )
                command_evidence["candidate_target"] = target
                targeted_test_passed = target["returncode"] == 0
                regressions = _run_test_commands(
                    worktree,
                    contract.regression_test_commands,
                    "candidate_regression",
                    runtime_identity=runtime_identity,
                )
                command_evidence.update(regressions)
                regression_test_passed = all(item["returncode"] == 0 for item in regressions.values())
                if not fixture_intact:
                    error = "candidate modified a staged target-test fixture"
    except (OSError, ValueError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        error = str(exc)[:1000]
    finally:
        try:
            if created:
                _workspace_git(worktree, "reset", "--hard", "HEAD")
                _workspace_git(worktree, "clean", "-fdx")
                _workspace_git(source, "worktree", "remove", str(worktree))
            cleanup_verified = (
                not worktree.exists()
                and str(worktree) not in _workspace_git(source, "worktree", "list", "--porcelain").stdout
            )
            source_integrity = _source_state_is_unchanged(source, source_head, source_status)
        except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
            error = error or f"cleanup failed: {exc}"
        shutil.rmtree(root, ignore_errors=True)
    index_integrity = _file_sha256(index_path) == source_index_sha256 if source_index_sha256 is not None else None
    return EditExecution(
        patch_applied=patch_applied,
        targeted_test_passed=targeted_test_passed,
        regression_test_passed=regression_test_passed,
        changed_paths=changed_paths,
        baseline_target_failed=baseline_target_failed,
        baseline_regressions_passed=baseline_regressions_passed,
        fixture_intact=fixture_intact,
        source_integrity=source_integrity,
        index_integrity=index_integrity,
        cleanup_verified=cleanup_verified,
        command_evidence=command_evidence,
        error=error,
    )


@dataclass
class ExecutableAgentWorkspace:
    """One benchmark-owned editable worktree and its private relocated index."""

    source: Path
    root: Path
    worktree: Path
    index_path: Path
    baseline_commit: str
    index_relocation: Mapping[str, str]

    def index_unchanged(self) -> bool:
        """Return whether the model left the derived frozen graph untouched."""
        try:
            current_sha256 = hashlib.sha256(self.index_path.read_bytes()).hexdigest()
        except OSError:
            return False
        return current_sha256 == self.index_relocation["derived_index_sha256"]

    def capture_diff(self) -> str:
        """Return the canonical Git diff produced by direct agent edits."""
        return _workspace_git(self.worktree, "diff", "--binary", "--no-ext-diff").stdout

    def changed_paths(self) -> tuple[str, ...]:
        """Return the exact tracked paths changed by the agent."""
        return tuple(
            line for line in _workspace_git(self.worktree, "diff", "--name-only").stdout.splitlines() if line.strip()
        )

    def cleanup(self) -> bool:
        """Restore and remove only this known disposable worktree, else fail closed."""
        try:
            _workspace_git(self.worktree, "reset", "--hard", "HEAD")
            _workspace_git(self.worktree, "clean", "-fdx")
            _workspace_git(self.source, "worktree", "remove", str(self.worktree))
            cleaned = (
                not self.worktree.exists()
                and str(self.worktree) not in _workspace_git(self.source, "worktree", "list", "--porcelain").stdout
            )
        except (OSError, subprocess.SubprocessError):
            cleaned = False
        if cleaned:
            shutil.rmtree(self.root, ignore_errors=True)
        return cleaned


def _non_root_index_sha256(payload: Mapping[str, Any]) -> str:
    """Hash index content after excluding its environment-specific scan root."""
    normalized = dict(payload)
    normalized.pop("scan_root", None)
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def load_index_relocation(path: Path | str | None) -> dict[str, str] | None:
    """Load one run's index relocation provenance, or return ``None`` when the run has none.

    A canonical run at the managed clone carries no provenance: its index is admitted by byte
    identity. A run in its own worktree carries the file the relocation wrote, and the admission
    gate checks that instead, so a malformed or unreadable file has to fail here rather than
    silently downgrade the run to an unverified index.

    Args:
        path: Path to the relocation provenance JSON, if this run has one.

    Returns:
        The provenance mapping, or ``None`` when no path was given.

    Raises:
        ValueError: If the file cannot be read or does not hold a JSON object of strings.

    Examples:
        >>> load_index_relocation(None) is None
        True
    """
    if path is None:
        return None
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read index relocation provenance {path}: {exc}") from exc
    if not isinstance(payload, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in payload.items()
    ):
        raise ValueError(f"index relocation provenance must be a JSON object of strings: {path}")
    # The relocation writer adds the installed path for the operator; admission checks the digests.
    return {key: value for key, value in payload.items() if key != "derived_index_path"}


def verify_index_relocation(
    index_relocation: Mapping[str, str],
    *,
    metadata: Mapping[str, Any],
    index_sha256: str,
    repo_path: Path,
    frozen_index_sha256: str,
) -> None:
    """Prove a relocated index is the locked graph, moved and otherwise untouched.

    A run outside the canonical clone cannot reproduce the locked byte hash, because those bytes
    record that clone's own path. This is what every admission gate checks instead: the copy came
    from the frozen index, the bytes on disk are the ones the relocation produced, the graph content
    apart from its root is unchanged, and the root it now claims is the worktree being run in.

    Args:
        index_relocation: Provenance recorded when the frozen index was relocated.
        metadata: Parsed index document found at the run's index path.
        index_sha256: SHA-256 of the index bytes actually on disk.
        repo_path: Worktree the run executes in.
        frozen_index_sha256: The manifest-locked raw index hash.

    Raises:
        ValueError: If any part of the provenance fails to tie those bytes to the locked graph.

    Examples:
        >>> verify_index_relocation.__name__
        'verify_index_relocation'
    """
    required_keys = {
        "frozen_index_sha256",
        "derived_index_sha256",
        "non_root_content_sha256",
        "source_scan_root",
        "worktree_scan_root",
    }
    if set(index_relocation) != required_keys:
        raise ValueError("canonical executable index relocation provenance is malformed")
    if index_relocation["frozen_index_sha256"] != frozen_index_sha256:
        raise ValueError("canonical executable index relocation has the wrong frozen source")
    if index_relocation["derived_index_sha256"] != index_sha256:
        raise ValueError("canonical executable index changed after relocation")
    if index_relocation["worktree_scan_root"] != str(repo_path.resolve()):
        raise ValueError("canonical executable index relocation has the wrong worktree root")
    if metadata.get("scan_root") != str(repo_path.resolve()):
        raise ValueError("canonical executable index scan_root does not match its worktree")
    if _non_root_index_sha256(metadata) != index_relocation["non_root_content_sha256"]:
        raise ValueError("canonical executable index relocation changed frozen graph content")


def relocate_frozen_index_for_worktree(
    frozen_bytes: bytes,
    *,
    source_root: Path,
    worktree_root: Path,
    frozen_index_sha256: str | None = None,
) -> tuple[bytes, dict[str, str]]:
    """Relocate only ``scan_root`` in a frozen index for a byte-identical worktree.

    This derived copy keeps the static graph immutable while allowing Codemap's root-mismatch completeness guard to
    evaluate the benchmark-owned checkout. The caller records both hashes and rejects a cell that modifies the copy.

    ``frozen_index_sha256`` names the locked index this copy descends from. It is needed when the source bytes are
    themselves a relocation — an isolated run's worktree index — because admission compares the recorded origin against
    the manifest lock, which the intermediate copy no longer hashes to. Relocation moves only ``scan_root``, so the
    graph content of every link in that chain is identical and the origin remains the honest answer.
    """
    try:
        frozen_payload = json.loads(frozen_bytes)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("executable agent workspace index must be valid JSON") from exc
    if not isinstance(frozen_payload, dict):
        raise ValueError("executable agent workspace index must be a JSON object")
    expected_root = str(source_root.resolve())
    if frozen_payload.get("scan_root") != expected_root:
        raise ValueError("executable agent workspace index scan_root must match the frozen source repository")
    derived_payload = dict(frozen_payload)
    derived_payload["scan_root"] = str(worktree_root.resolve())
    frozen_content_sha256 = _non_root_index_sha256(frozen_payload)
    if _non_root_index_sha256(derived_payload) != frozen_content_sha256:
        raise RuntimeError("executable index relocation changed frozen graph content")
    derived_bytes = (json.dumps(derived_payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return derived_bytes, {
        "frozen_index_sha256": frozen_index_sha256 or hashlib.sha256(frozen_bytes).hexdigest(),
        "derived_index_sha256": hashlib.sha256(derived_bytes).hexdigest(),
        "non_root_content_sha256": frozen_content_sha256,
        "source_scan_root": expected_root,
        "worktree_scan_root": str(worktree_root.resolve()),
    }


def create_executable_agent_workspace(
    source: Path,
    index_path: Path,
    baseline_commit: str,
    *,
    require_source_baseline: bool = True,
    frozen_index_sha256: str | None = None,
) -> ExecutableAgentWorkspace:
    """Create an editable worktree with a root-relocated immutable graph copy.

    ``frozen_index_sha256`` carries the locked origin forward when ``index_path`` is itself a relocated copy, which is
    the case for every stage of a run outside the canonical clone.
    """
    source = source.resolve(strict=True)
    index_path = index_path.resolve(strict=True)
    if not index_path.is_relative_to(source):
        raise ValueError("executable agent workspace requires an index inside the frozen source repository")
    if require_source_baseline:
        _verified_source_baseline(source, baseline_commit)
    else:
        _verified_patch_task_source(source, baseline_commit)
    root = Path(tempfile.mkdtemp(prefix="codemap-executable-agent-")).resolve(strict=True)
    worktree = root / "repo"
    copied_index = worktree / ".cache" / "codemap" / f"{worktree.name}.json"
    try:
        _workspace_git(source, "worktree", "add", "--detach", str(worktree), baseline_commit)
        copied_index.parent.mkdir(parents=True, exist_ok=True)
        derived_bytes, index_relocation = relocate_frozen_index_for_worktree(
            index_path.read_bytes(),
            source_root=source,
            worktree_root=worktree,
            frozen_index_sha256=frozen_index_sha256,
        )
        copied_index.write_bytes(derived_bytes)
        if hashlib.sha256(copied_index.read_bytes()).hexdigest() != index_relocation["derived_index_sha256"]:
            raise RuntimeError("executable agent workspace derived index write is incomplete")
        return ExecutableAgentWorkspace(source, root, worktree, copied_index, baseline_commit, index_relocation)
    except BaseException:
        if worktree.exists():
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                _workspace_git(worktree, "reset", "--hard", "HEAD")
                _workspace_git(worktree, "clean", "-fdx")
                _workspace_git(source, "worktree", "remove", str(worktree))
        shutil.rmtree(root, ignore_errors=True)
        raise
