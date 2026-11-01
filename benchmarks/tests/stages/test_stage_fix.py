"""No-model contracts for the private Codex executable-fix stage."""

from __future__ import annotations

import contextlib
import dataclasses
import importlib.util
import inspect
import json
import shlex
import sys
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace
from typing import Any

import pytest

BENCHMARKS = Path(__file__).resolve().parent.parent.parent
FIXTURE_SCOPE_SHA = "f" * 64
FIXTURE_APPROVAL_TOKEN = FIXTURE_SCOPE_SHA[:16]
sys.path.insert(0, str(BENCHMARKS))

from _bench_common.edit_patch_contracts import (  # noqa: E402
    EditExecution,
    build_edit_task_contract,
    build_fix_single_contract,
)
from _bench_common.provider_parity_contracts import load_task_suite  # noqa: E402


@pytest.fixture(name="stage_fix", scope="module")
def _stage_fix() -> Any:
    """Load the structural transport before its private fix-stage consumer.

    Example:
        >>> getfixture("stage_fix").__name__
        '_bench_codex.stage_fix'
    """
    structural_path = BENCHMARKS / "run-codex-structural.py"
    spec = importlib.util.spec_from_file_location("codex_structural_for_stage_fix", structural_path)
    assert spec is not None
    assert spec.loader is not None
    structural = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = structural
    spec.loader.exec_module(structural)
    from _bench_codex import stage_fix as module

    return module


@pytest.fixture(name="patch_runtime_identity", scope="module")
def _patch_runtime_identity(stage_fix: Any) -> dict[str, str]:
    """Resolve the real Patch pytest launcher identity once; it spawns a probe subprocess and never changes."""
    return stage_fix.patch_test_runtime_identity()


@pytest.fixture(name="bound_workspace")
def _bound_workspace(stage_fix: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the transport's workspace binding with a pass-through so no external state is altered."""

    @contextlib.contextmanager
    def _bind_workspace(*_args: Any, **_kwargs: Any) -> Iterator[None]:
        """Yield through the workspace-binding boundary without altering external state."""
        yield

    monkeypatch.setattr(stage_fix._structural(), "bind_executable_agent_workspace", _bind_workspace)


@pytest.fixture(name="pt01_contract")
def _pt01_contract(stage_fix: Any) -> Any:
    """Edit-task contract of the first historical Patch task, PT-01."""
    task = next(task for task in load_task_suite(stage_fix.PATCH_TASKS_PATH) if task["id"] == "PT-01")
    return build_edit_task_contract(task)


@pytest.fixture(name="fm03_task")
def _fm03_task(stage_fix: Any) -> dict[str, Any]:
    """Suite entry of Fix-Multi task FM-03."""
    return next(task for task in stage_fix.load_task_suite(stage_fix.FIX_MULTI_TASKS_PATH) if task["id"] == "FM-03")


@pytest.fixture(name="fm03_contract")
def _fm03_contract(stage_fix: Any, fm03_task: dict[str, Any]) -> Any:
    """Fix-Multi contract of FM-03, which declares no regression commands."""
    return stage_fix.build_fix_multi_contract(fm03_task)


@pytest.fixture(name="write_replay_source")
def _write_replay_source(tmp_path: Path) -> Callable[[dict[str, object]], Path]:
    """Build a factory that writes a completed run directory whose telemetry holds one supplied source row."""

    def _write(row: dict[str, object]) -> Path:
        """Write metadata, telemetry, and a checksum placeholder, then return the run directory."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        (source_dir / "run-metadata.json").write_text('{"status":"completed"}', encoding="utf-8")
        (source_dir / "telemetry.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
        (source_dir / "checksums.sha256").write_text("fixture\n", encoding="utf-8")
        return source_dir

    return _write


@pytest.fixture(name="stub_replay_inputs")
def _stub_replay_inputs(stage_fix: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace checksum, task-loading, and binding validation so a replay reads only the fixture telemetry."""
    monkeypatch.setattr(stage_fix, "verify_checksums", lambda _path: None)
    monkeypatch.setattr(
        stage_fix, "load_fix_single_tasks", lambda *_args: [{"contract": SimpleNamespace(task_id="fixture-task")}]
    )
    monkeypatch.setattr(stage_fix, "validate_fix_single_binding", lambda *_args: None)


def _source_row(*, captured_diff: object, tool_result_tokens: int | None = None) -> dict[str, object]:
    """Build replay input while preserving the supplied diff object and optional token count.

    >>> row = _source_row(captured_diff="example diff", tool_result_tokens=0)
    >>> row["captured_diff"], row["tool_result_tokens"], row["raw_events"]
    ('example diff', 0, [])
    """
    return {
        "task_id": "fixture-task",
        "arm": "A_plain",
        "raw_events": [],
        "captured_diff": captured_diff,
        "provider_binding": {},
        "input_tokens": 11,
        "cached_input_tokens": 2,
        "output_tokens": 3,
        "tool_result_tokens": tool_result_tokens,
    }


def _rescored_row(*, tool_result_tokens: int | None) -> dict[str, object]:
    """Build a compliant but unsuccessful replay result with explicit unknown or measured tool usage.

    >>> row = _rescored_row(tool_result_tokens=None)
    >>> row["tool_result_tokens"], row["primary_correct"], row["compliance"]
    (None, False, True)
    """
    return {
        "task_id": "fixture-task",
        "arm": "A_plain",
        "input_tokens": 11,
        "cached_input_tokens": 2,
        "output_tokens": 3,
        "tool_result_tokens": tool_result_tokens,
        "command_calls": 0,
        "provider_binding": {},
        "primary_correct": False,
        "codemap_used": False,
        "pooling_eligible": False,
        "compliance": True,
        "execution": {"recount_recoverable": False, "patch_applied": False, "targeted_test_passed": False},
    }


def _fm_parsed(**overrides: Any) -> SimpleNamespace:
    """Build successful transport evidence with no observed tools and unknown tool-result usage.

    Args:
        **overrides: Transport fields that replace the no-tool defaults.

    >>> result = _fm_parsed()
    >>> result.success, result.command_calls, result.tool_result_tokens
    (True, 0, None)
    >>> _fm_parsed(command_calls=1).command_calls
    1
    """
    fields = {
        "success": True,
        "output_text": "summary",
        "input_tokens": 10,
        "cached_input_tokens": 2,
        "output_tokens": 3,
        "reasoning_output_tokens": 1,
        "tool_result_tokens": None,
        "command_calls": 0,
        "tool_elapsed_s": None,
        "codemap_calls": 0,
        "codemap_observed_calls": 0,
        "codemap_successful_calls": 0,
        "codemap_direct_compact_successful_calls": 0,
        "codemap_skill_compact_successful_calls": 0,
        "codemap_errors": 0,
        "skill_delivery_observed": False,
        "successful_query_arguments": [],
        "raw_events": [],
    }
    return SimpleNamespace(**{**fields, **overrides})


def _fm_strict_query_parsed(query_arguments: list[str]) -> SimpleNamespace:
    """Build transport evidence for one successful compact Skill query with the supplied argv.

    >>> _fm_strict_query_parsed(["rdeps", "pkg.core"]).successful_query_arguments
    [['rdeps', 'pkg.core']]
    """
    return _fm_parsed(
        command_calls=1,
        codemap_calls=1,
        codemap_observed_calls=1,
        codemap_successful_calls=1,
        codemap_skill_compact_successful_calls=1,
        skill_delivery_observed=True,
        successful_query_arguments=[query_arguments],
    )


def _fm_execution(contract: Any, **overrides: Any) -> dict[str, Any]:
    """Build passing execution evidence from contract paths, then apply explicit overrides.

    >>> execution = _fm_execution(SimpleNamespace(expected_paths=("example.py",)), targeted_test_passed=False)
    >>> execution["changed_paths"], execution["targeted_test_passed"], execution["cleanup_verified"]
    (['example.py'], False, True)
    """
    execution = {
        "baseline_failed": True,
        "patch_applied": True,
        "changed_paths": list(contract.expected_paths),
        "targeted_test_passed": True,
        "recount_recoverable": False,
        "recount_oracle_passed": None,
        "cleanup_verified": True,
        "error": "",
    }
    execution.update(overrides)
    return execution


def _score_fix_multi(
    stage_fix: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    contract: Any,
    execution: dict[str, Any],
    *,
    arm: str = "A_plain",
    parsed: SimpleNamespace | None = None,
) -> dict[str, Any]:
    """Score one Fix-Multi cell from supplied transport and execution evidence, replacing both with local doubles.

    >>> from tempfile import TemporaryDirectory
    >>> stage = getfixture("stage_fix")
    >>> task = next(item for item in stage.load_task_suite(stage.FIX_MULTI_TASKS_PATH) if item["id"] == "FM-03")
    >>> contract = stage.build_fix_multi_contract(task)
    >>> with TemporaryDirectory() as directory, pytest.MonkeyPatch.context() as patch:
    ...     row = _score_fix_multi(stage, patch, Path(directory), contract, _fm_execution(contract))
    ...     row["primary_correct"], row["pooling_eligible"]
    (True, True)
    """
    transport = _fm_parsed() if parsed is None else parsed
    monkeypatch.setattr(stage_fix.runtime, "parse_codex_jsonl", lambda *_args, **_kwargs: transport)
    monkeypatch.setattr(
        stage_fix, "execute_fix_multi_patch", lambda *_args, **_kwargs: SimpleNamespace(as_dict=lambda: execution)
    )
    return stage_fix.parse_fix_multi_cell(
        "{}",
        arm=arm,
        item={"contract": contract},
        skill_path=None,
        repo_path=tmp_path,
        captured_diff="diff --git a/a.py b/a.py\n",
    )


def _home() -> SimpleNamespace:
    """Build a fixture arm home that needs no cleanup and carries no Skill file or environment."""
    return SimpleNamespace(coordination_path=None, cleanup=lambda: None, codemap_skill_path=None, env={})


def _workspace(worktree: Path, **overrides: Any) -> SimpleNamespace:
    """Build a fixture disposable workspace that reports a clean diff and unchanged index, with overrides applied."""
    fields: dict[str, Any] = {
        "worktree": worktree,
        "index_relocation": {},
        "capture_diff": lambda: "diff --git a/example.py b/example.py\n",
        "changed_paths": lambda: (),
        "index_unchanged": lambda: True,
        "cleanup": lambda: True,
    }
    return SimpleNamespace(**{**fields, **overrides})


def _passing_parser(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    """Score every cell as a successful, poolable, answer-error-free result."""
    return {"success": True, "primary_correct": True, "pooling_eligible": True, "answer_error": ""}


class _FixtureAdapter:
    """Transport double that records home preparation and never launches Codex."""

    def __init__(self, events: list[str] | None = None) -> None:
        """Initialize the shared event log and the per-call preparation keyword record."""
        self.events: list[str] = [] if events is None else events
        self.prepared: list[dict[str, Any]] = []
        self.home = _home()

    def prepare_verified_home(self, arm: str, **kwargs: Any) -> SimpleNamespace:
        """Record preparation evidence and return the fixture-owned home."""
        self.events.append(f"prepare:{arm}")
        self.prepared.append(kwargs)
        return self.home

    def build_command(self, *_args: Any, **_kwargs: Any) -> list[str]:
        """Return fixed Codex argv without launching a command."""
        return ["codex", "exec"]

    def run_stream(self, *_args: Any, **_kwargs: Any) -> str:
        """Return an empty transport stream without invoking Codex."""
        return ""


def _run_dry_run(
    stage_fix: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    study: str,
    selected: set[str] | None,
    contracts: list[SimpleNamespace],
    loader: str,
    binding: tuple[str, dict[str, Any]],
    scope: dict[str, Any],
    preflight: Callable[..., None] = lambda *_args, **_kwargs: None,
    run_dir: str = "benchmarks/results/fresh-run",
) -> None:
    """Run a no-model dry run of one fix stage with its loaders, source binding, scope, and preflight replaced.

    Args:
        stage_fix: The Fix stage module under test.
        tmp_path: Directory the fixture repository paths are rooted under.
        monkeypatch: Fixture patching the stage's loaders, scope resolver, and workspace preflight.
        study: Study name passed to the stage.
        selected: Task ids to run, or ``None`` for the full study.
        contracts: Fixture contracts the loader returns and the suite lists.
        loader: Name of the stage's task loader for the study.
        binding: Name of the source-binding function and the value it returns.
        scope: Value the scope resolver returns.
        preflight: Replacement for the executable agent workspace preflight.
        run_dir: Run directory the stage suggests in its paid command.
    """
    adapter = SimpleNamespace(close=lambda: None)
    items = [{"task": {}, "contract": contract} for contract in contracts]
    binding_name, binding_value = binding
    monkeypatch.setattr(stage_fix, loader, lambda _path, _selected: items)
    monkeypatch.setattr(
        stage_fix, "load_task_suite", lambda _path: [{"id": contract.task_id} for contract in contracts]
    )
    monkeypatch.setattr(stage_fix, binding_name, lambda *_args: binding_value)
    monkeypatch.setattr(stage_fix, "_resolve_scope", lambda *_args: scope)
    monkeypatch.setattr(
        stage_fix, "_structural", lambda: SimpleNamespace(CodexRunner=lambda *_args, **_kwargs: adapter)
    )
    monkeypatch.setattr(stage_fix, "preflight_executable_agent_workspace", preflight)
    monkeypatch.setattr(stage_fix, "_suggested_run_dir", lambda _study: Path(run_dir))

    stage_fix.run_fix_stage(
        study=study,
        repo_path=tmp_path / "repo",
        selected=selected,
        dry_run=True,
        resolve_scope=False,
        auth_source=None,
        run_dir=None,
        paid_approval=None,
        model="gpt-6.1-sol",
        index_path=tmp_path / "repo/.cache/codemap/repo.json",
        marketplace_root=BENCHMARKS.parent,
        codemap_bin=BENCHMARKS.parent / "plugins/codemap-py/bin/codemap-py",
    )


def _run_fix_single_dry_run(
    stage_fix: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    task_ids: tuple[str, ...],
    selected: set[str] | None,
    run_dir: str,
) -> None:
    """Run a no-model Fix-Single dry run over fixture contracts with the given task ids."""
    contracts = [
        SimpleNamespace(task_id=task_id, baseline_commit="baseline", provider_binding=lambda: {})
        for task_id in task_ids
    ]
    _run_dry_run(
        stage_fix,
        tmp_path,
        monkeypatch,
        study="fix-single",
        selected=selected,
        contracts=contracts,
        loader="load_fix_single_tasks",
        binding=("_stage_source_binding", {"source": "locked"}),
        scope={"scope_sha256": FIXTURE_SCOPE_SHA},
        run_dir=run_dir,
    )


def _run_two_baseline_patch_dry_run(
    stage_fix: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    preflight: Callable[..., None],
    runtime_identity: dict[str, str],
) -> None:
    """Run a no-model Patch dry run over two tasks that span distinct historical baselines."""
    contracts = [
        SimpleNamespace(task_id="PT-01", baseline_commit="baseline-one", provider_binding=lambda: {}),
        SimpleNamespace(task_id="PT-02", baseline_commit="baseline-two", provider_binding=lambda: {}),
    ]
    coordinates = {
        "PT-01": {"baseline_commit": "baseline-one", "raw_index_sha256": "a" * 64, "scan_version": "13"},
        "PT-02": {"baseline_commit": "baseline-two", "raw_index_sha256": "b" * 64, "scan_version": "13"},
    }
    _run_dry_run(
        stage_fix,
        tmp_path,
        monkeypatch,
        study="patch",
        selected=None,
        contracts=contracts,
        loader="load_patch_tasks",
        binding=("_patch_stage_source_binding", {"source": "locked", "patch_coordinates": coordinates}),
        scope={"scope_sha256": FIXTURE_SCOPE_SHA, "patch_test_runtime": runtime_identity},
        preflight=preflight,
    )


class TestFormatExecutableResultRow:
    """Progress rows rendered for executable cells."""

    def test_uses_private_runtime_formatters(self, stage_fix: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """Progress rendering must not depend on formatters removed from the public runner.

        The structural transport is replaced by an empty namespace, so any formatter still read from it would fail.
        """
        monkeypatch.setattr(stage_fix, "_structural", lambda: SimpleNamespace())
        row = {
            "task_id": "FS-01",
            "arm": "A_plain",
            "input_tokens": 12_345,
            "output_tokens": 678,
            "command_calls": 2,
            "elapsed_s": 61.0,
            "primary_correct": True,
            "pooling_eligible": True,
            "codemap_used": False,
            "compliance": True,
            "execution": {
                "recount_recoverable": False,
                "recount_oracle_passed": None,
                "patch_applied": True,
                "targeted_test_passed": True,
            },
        }

        rendered = stage_fix.format_executable_result_row(row, completed=1, total=6)

        assert "in= 12.3k" in rendered
        assert "out=  678" in rendered
        assert "time= 1m1s" in rendered
        assert "compliance=" not in rendered

    def test_keeps_patch_quality_separate_from_pooling_eligibility(self, stage_fix: Any) -> None:
        """Patch presentation avoids duplicating nonpoolability in the quality figure.

        A correct but nonpoolable row shows full quality with a failure mark and no extra nonpoolable marker.
        """
        row = {
            "task_id": "PT-01",
            "arm": "A_plain",
            "input_tokens": 1,
            "output_tokens": 1,
            "command_calls": 1,
            "elapsed_s": 1.0,
            "primary_correct": True,
            "pooling_eligible": False,
            "codemap_used": False,
            "execution": {"recount_recoverable": False, "patch_applied": True, "targeted_test_passed": True},
        }

        rendered = stage_fix.format_executable_result_row(row, completed=1, total=3)

        assert rendered.startswith("(1/3) ✗")
        assert "quality=1.000" in rendered
        assert "^" not in rendered


class TestStageAdmission:
    """Paid-stage admission, scope binding, and recovery guidance."""

    def test_paid_request_explains_missing_flags_and_stale_scope(self, stage_fix: Any, tmp_path: Path) -> None:
        """Paid admission errors name the problem and give a safe recovery path.

        A request missing its paid flags, a request with the current approval, and a request carrying a stale approval
        token are exercised in turn.
        """
        common = {
            "study": "fix-single",
            "repo_path": tmp_path / "repo",
            "task_ids": ["FS-01", "FS-03"],
            "model": "gpt-6.1-sol",
            "expected_scope": FIXTURE_SCOPE_SHA,
        }

        with pytest.raises(ValueError, match=r"missing --auth-source, --run-dir, --paid-approval"):
            stage_fix._require_paid_stage_request(**common, auth_source=None, run_dir=None, paid_approval=None)
        stage_fix._require_paid_stage_request(
            **common,
            auth_source=tmp_path / "auth.json",
            run_dir=tmp_path / "run",
            paid_approval=FIXTURE_APPROVAL_TOKEN,
        )
        with pytest.raises(ValueError, match=rf"received --paid-approval: {'e' * 16}") as error:
            stage_fix._require_paid_stage_request(
                **common, auth_source=tmp_path / "auth.json", run_dir=tmp_path / "run", paid_approval="e" * 16
            )

        message = str(error.value)
        assert f"current scope: {FIXTURE_SCOPE_SHA}" in message
        assert "--tasks FS-01,FS-03 --dry-run" in message
        assert "No model call was made." in message

    def test_scope_binds_the_validated_source_and_index(self, stage_fix: Any) -> None:
        """A changed source index invalidates executable paid approval by changing the scope digest."""
        task = {"contract": SimpleNamespace(task_id="FS-01", provider_binding=lambda: {"task": "one"})}
        shared = {"repo_path": "/private/tmp/repo", "repo_sha256": "repo", "manifest_sha256": "manifest"}

        first = stage_fix._resolve_scope([task], "gpt-6.1-sol", {**shared, "index_sha256": "one"})
        second = stage_fix._resolve_scope([task], "gpt-6.1-sol", {**shared, "index_sha256": "two"})

        assert first["scope_sha256"] != second["scope_sha256"]

    def test_patch_scope_and_snapshot_close_over_runtime_and_implementation(
        self, stage_fix: Any, tmp_path: Path
    ) -> None:
        """Patch approval and input archival bind every scorer/runtime byte and task index.

        Regression: the initial historical-task stage bound only a task index and
        launcher, leaving the shared scorer, mutable-worktree lifecycle, and
        designated pytest runtime outside the immutable study coordinate.
        """
        task = {"contract": SimpleNamespace(task_id="PT-01", provider_binding=lambda: {})}
        index = tmp_path / ".cache" / "codemap" / "patch" / "PT-01.json"
        index.parent.mkdir(parents=True)
        index.write_text("{}", encoding="utf-8")
        coordinate = {"baseline_commit": "a" * 40, "raw_index_sha256": "b" * 64, "scan_version": "13"}
        source_binding = {"patch_coordinates": {"PT-01": coordinate}}

        scope = stage_fix._resolve_scope([task], "gpt-6.1-sol", source_binding)
        files = stage_fix._patch_snapshot_files(tmp_path, [task])

        hashes = scope["implementation_sha256"]
        assert set(files) == {
            "codex-runtime.py",
            "stage-fix.py",
            "paid-lifecycle.py",
            "edit-patch-contracts.py",
            "mutation-isolation.py",
            "patch-index-locks.json",
            "patch-index-PT-01.json",
        }
        assert files["patch-index-PT-01.json"] == index
        assert scope["patch_test_runtime"]["invocation"] == "absolute pytest executable"
        assert hashes["stage_fix"] == stage_fix._file_sha256(files["stage-fix.py"])
        assert hashes["paid_lifecycle"] == stage_fix._file_sha256(files["paid-lifecycle.py"])
        assert hashes["edit_patch_contracts"] == stage_fix._file_sha256(files["edit-patch-contracts.py"])
        assert hashes["mutation_isolation"] == stage_fix._file_sha256(files["mutation-isolation.py"])
        assert hashes["patch_index_locks"] == stage_fix._file_sha256(files["patch-index-locks.json"])

    def test_managed_input_recovery_preserves_the_invalid_target(self, stage_fix: Any) -> None:
        """Managed-target admission recommends a recoverable reconstruction, never deletion."""
        managed_repo = stage_fix._MANAGED_PARITY_REPO

        recovery = stage_fix._stage_input_recovery(managed_repo)

        assert f"REPO_PATH={shlex.quote(str(managed_repo))}" in recovery
        assert 'mv "$REPO_PATH" "$REPO_PATH.invalid-$(date -u +%Y%m%dT%H%M%SZ)"' in recovery
        assert "run-all.sh codex --struct --tasks=FN-02 --dry-run" in recovery
        assert "rm -rf" not in recovery


class TestRunFixStageDryRun:
    """No-model dry runs that print the plan and the copy-paste paid command."""

    def test_emits_exact_paid_command_after_preflight(
        self, stage_fix: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A successful no-model admission prints a copy-paste paid command without creating a run directory."""
        _run_fix_single_dry_run(
            stage_fix,
            tmp_path,
            monkeypatch,
            task_ids=("FS-01",),
            selected={"FS-01"},
            run_dir="benchmarks/results/fresh-run",
        )

        output = capsys.readouterr().out
        assert f"SCOPE   {FIXTURE_SCOPE_SHA}" in output
        assert "PAID_COMMAND" in output
        assert "--run-dir benchmarks/results/fresh-run" in output
        # The command is framed, so its last flag sits above the closing rule rather than at the end.
        assert output.splitlines()[-2] == f"  --paid-approval {FIXTURE_APPROVAL_TOKEN}"
        assert output.splitlines()[-1] == "-" * 78
        assert "--tasks FS-01" in output
        assert "--study" not in output
        assert "--paid=True" not in output

    def test_full_study_command_omits_the_task_selector(
        self, stage_fix: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The canonical full-study command preserves omission of ``--tasks``."""
        _run_fix_single_dry_run(
            stage_fix,
            tmp_path,
            monkeypatch,
            task_ids=("FS-01", "FS-02", "FS-03", "FS-04"),
            selected=None,
            run_dir="benchmarks/results/full-run",
        )

        output = capsys.readouterr().out
        assert "PLAN    FS-04 C_strict" in output
        assert "--run-dir benchmarks/results/full-run" in output
        assert "--tasks" not in output

    @pytest.mark.parametrize(
        "run_dir",
        [
            pytest.param(PureWindowsPath(r"benchmarks\results\codex-fix-01"), id="windows_separator"),
            pytest.param(PurePosixPath("benchmarks/results/codex-fix-01"), id="posix_separator"),
        ],
    )
    def test_run_dir_argument_keeps_repository_separators(self, stage_fix: Any, run_dir: Any) -> None:
        """The paid command is a shell line, so its repo-relative argument stays forward-slashed.

        Regression: the suggested run directory was interpolated in native form, which handed
        a Windows operator ``--run-dir benchmarks\\results\\...`` — a value the runner reads as
        one opaque segment rather than a path under ``benchmarks/results``.
        """
        assert stage_fix._repo_relative_argument(run_dir) == "benchmarks/results/codex-fix-01"

    def test_patch_stage_preflights_each_distinct_task_baseline(
        self, stage_fix: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patch_runtime_identity: dict[str, str]
    ) -> None:
        """Patch tasks must not reuse the first task's historical baseline.

        Regression: executable preflight used only ``tasks[0]``. The real patch
        suite deliberately spans several pre-fix commits, so that shortcut can
        validate the wrong checkout and index for every later task.
        """
        observed: list[str] = []

        _run_two_baseline_patch_dry_run(
            stage_fix,
            tmp_path,
            monkeypatch,
            lambda _adapter, **kwargs: observed.append(
                f"{kwargs['baseline_commit']}:{kwargs['historical_runtime_coordinate']['raw_index_sha256']}"
                f":{kwargs['patch_contract'].task_id}"
            ),
            patch_runtime_identity,
        )

        assert observed == [f"baseline-one:{'a' * 64}:PT-01", f"baseline-two:{'b' * 64}:PT-02"]

    def test_patch_preflight_reports_each_historical_baseline(
        self,
        stage_fix: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        patch_runtime_identity: dict[str, str],
    ) -> None:
        """Patch dry runs expose progress while each frozen baseline test executes.

        Regression: Patch preflight ran five baseline/test fixtures before printing
        its paid scope, making a correct no-model validation look stalled.
        """
        _run_two_baseline_patch_dry_run(
            stage_fix, tmp_path, monkeypatch, lambda *_args, **_kwargs: None, patch_runtime_identity
        )

        output = capsys.readouterr().out
        assert "PREFLIGHT 1/2 PT-01 validating frozen baseline and tests..." in output
        assert "PREFLIGHT 1/2 PT-01 ✓" in output
        assert "PREFLIGHT 2/2 PT-02 validating frozen baseline and tests..." in output
        assert "PREFLIGHT 2/2 PT-02 ✓" in output


class TestPreflightExecutableAgentWorkspace:
    """Workspace preflight run before any paid executable cell."""

    @pytest.mark.usefixtures("bound_workspace")
    def test_admits_clean_context_before_staging_fixture(
        self, stage_fix: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pt01_contract: Any
    ) -> None:
        """Codex integration probes run before the frozen fixture dirties the worktree.

        Every arm prepares its verified home against the still-clean historical worktree; only then is the Patch fixture
        staged and the workspace cleaned up.
        """
        adapter = _FixtureAdapter()
        workspace = _workspace(tmp_path / "worktree", cleanup=lambda: adapter.events.append("cleanup") or True)
        monkeypatch.setattr(stage_fix, "create_executable_agent_workspace", lambda *_args, **_kwargs: workspace)
        monkeypatch.setattr(
            stage_fix,
            "stage_patch_task_agent_workspace",
            lambda *_args, **_kwargs: adapter.events.append("stage-fixture"),
        )

        stage_fix.preflight_executable_agent_workspace(
            adapter,
            source_repo=tmp_path / "source",
            source_index=tmp_path / "index.json",
            baseline_commit=pt01_contract.baseline_commit,
            allow_historical_baseline=True,
            historical_runtime_coordinate={
                "baseline_commit": pt01_contract.baseline_commit,
                "raw_index_sha256": "a" * 64,
                "scan_version": "13",
            },
            patch_contract=pt01_contract,
        )

        assert adapter.events == ["prepare:A_plain", "prepare:B_auto", "prepare:C_strict", "stage-fixture", "cleanup"]

    @pytest.mark.usefixtures("bound_workspace")
    def test_keeps_permission_verification_enabled(
        self, stage_fix: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Preflight validates the declared workspace permissions for every native arm."""
        workspace_path, source_path = tmp_path / "workspace", tmp_path / "source"
        workspace_path.mkdir()
        source_path.mkdir()
        adapter = _FixtureAdapter()
        workspace = _workspace(workspace_path)
        monkeypatch.setattr(stage_fix._structural(), "create_executable_agent_workspace", lambda *_a, **_k: workspace)

        stage_fix.preflight_executable_agent_workspace(
            adapter, source_repo=source_path, source_index=tmp_path / "index.json", baseline_commit="baseline"
        )

        assert len(adapter.prepared) == len(stage_fix.NATIVE_ARMS)
        assert all(kwargs["writable_workspace"] == workspace_path for kwargs in adapter.prepared)
        assert all(kwargs["denied_workspace"] == source_path for kwargs in adapter.prepared)


class TestExecuteExecutableAgentCell:
    """One paid executable cell run against a disposable workspace."""

    @pytest.mark.usefixtures("bound_workspace")
    def test_keeps_permission_verification_enabled(
        self, stage_fix: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cell prepares its verified home with the declared workspace grant and source denial."""
        workspace_path, source_path = tmp_path / "workspace", tmp_path / "source"
        workspace_path.mkdir()
        source_path.mkdir()
        adapter = _FixtureAdapter()
        workspace = _workspace(workspace_path)
        structural = stage_fix._structural()
        monkeypatch.setattr(structural, "create_executable_agent_workspace", lambda *_args, **_kwargs: workspace)
        monkeypatch.setattr(structural, "_repo_sha", lambda _path: "baseline")
        monkeypatch.setattr(structural, "_git_porcelain_status", lambda _path: "")

        stage_fix.execute_executable_agent_cell(
            adapter=adapter,
            source_repo=source_path,
            source_index=tmp_path / "index.json",
            baseline_commit="baseline",
            native_arm="A_plain",
            arm="A_plain",
            prompt="fixture prompt",
            item={},
            parser=_passing_parser,
        )

        assert len(adapter.prepared) == 1
        assert adapter.prepared[0]["writable_workspace"] == workspace_path
        assert adapter.prepared[0]["denied_workspace"] == source_path

    @pytest.mark.usefixtures("bound_workspace")
    def test_excludes_a_clean_source_head_switch(
        self, stage_fix: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patch_runtime_identity: dict[str, str]
    ) -> None:
        """A Patch row is not comparable when the agent changed source ``HEAD`` cleanly.

        The agent's diff still passes, but the source repository moved, so the row is neither correct nor poolable.
        """
        source = tmp_path / "source"
        source.mkdir()
        workspace_path = tmp_path / "workspace"
        workspace_path.mkdir()
        contract = build_edit_task_contract(
            {
                "id": "PT-fixture",
                "type": "patch_task",
                "prompt": "Fix it.",
                "pre_fix_commit": "a" * 40,
                "test_fixture_patch": (
                    "diff --git a/test_fixture.py b/test_fixture.py\nnew file mode 100644\n--- /dev/null\n"
                    "+++ b/test_fixture.py\n@@ -0,0 +1 @@\n+def test_fixture() -> None:\n+    pass\n"
                ),
                "test_command": "pytest test_fixture.py -q",
                "gt_files_changed": ["src/example.py"],
                "regression_test_commands": ["pytest test_regression.py -q"],
                "scoreable": True,
            }
        )
        workspace = _workspace(workspace_path, changed_paths=lambda: ("src/example.py",))
        patch_workspace = SimpleNamespace(
            workspace=workspace,
            fixture_sha256_by_path={"tests/test_fixture.py": "fixture-sha"},
            capture_answer=lambda: SimpleNamespace(diff="diff --git a/src/example.py b/src/example.py\n"),
            fixture_intact=lambda: True,
            source_unchanged=lambda: False,
        )
        adapter = _FixtureAdapter()
        monkeypatch.setattr(stage_fix, "create_executable_agent_workspace", lambda *_args, **_kwargs: workspace)
        monkeypatch.setattr(stage_fix, "stage_patch_task_agent_workspace", lambda *_args, **_kwargs: patch_workspace)

        row = stage_fix.execute_executable_agent_cell(
            adapter=adapter,
            source_repo=source,
            source_index=tmp_path / "patch-index.json",
            baseline_commit=contract.baseline_commit,
            native_arm="A_plain",
            arm="A_plain",
            prompt="fixture",
            item={
                "contract": contract,
                "patch_test_runtime": patch_runtime_identity,
                "historical_runtime_coordinate": {
                    "baseline_commit": contract.baseline_commit,
                    "raw_index_sha256": "a" * 64,
                    "scan_version": "13",
                },
            },
            parser=_passing_parser,
        )

        assert row["pooling_eligible"] is False
        assert row["primary_correct"] is False
        assert row["agent_workspace"]["source_unchanged"] is False
        assert "patch_fixture_admission" not in adapter.prepared[0]


class TestParsePatchCell:
    """Scoring of one historical Patch cell."""

    def test_scores_the_exact_captured_worktree_diff(
        self, stage_fix: Any, monkeypatch: pytest.MonkeyPatch, pt01_contract: Any
    ) -> None:
        """Historical Patch scoring must not rewrite direct-worktree diff bytes.

        Regression: the inherited Fix-Single display normalizer can insert context
        markers. A Patch result is a harness-captured Git diff, so changing it
        would make the stored digest describe different candidate bytes.
        """
        captured_diff = "diff --git a/example.py b/example.py\n@@ -1 +1 @@\n-old\n+new\n"
        observed: dict[str, str] = {}
        transport = _fm_parsed(
            output_text="",
            input_tokens=1,
            cached_input_tokens=0,
            output_tokens=1,
            reasoning_output_tokens=0,
            tool_result_tokens=0,
            tool_elapsed_s=0.0,
            codemap_errors=[],
        )
        monkeypatch.setattr(stage_fix.runtime, "parse_codex_jsonl", lambda *_args, **_kwargs: transport)

        def _execute(_repo: Path, _contract: object, diff: str) -> EditExecution:
            """Capture the candidate diff and return passing execution evidence for the fixture paths."""
            observed["diff"] = diff
            return EditExecution(
                patch_applied=True,
                targeted_test_passed=True,
                regression_test_passed=True,
                changed_paths=pt01_contract.expected_paths,
            )

        row = stage_fix._parse_patch_cell(
            "",
            arm="A_plain",
            item={"contract": pt01_contract, "provider_binding": {}},
            skill_path=None,
            repo_path=Path("."),
            captured_diff=captured_diff,
            answer_re=stage_fix._PATCH_ANSWER_RE,
            query_arguments=stage_fix._PATCH_QUERY_ARGUMENTS,
            execute_patch=_execute,
        )

        assert observed["diff"] == captured_diff
        assert row["patch_wire_normalized"] is False
        assert row["execution"].get("recount_recoverable") is None

    def test_strict_query_anchors_pt02_at_the_existing_class(self, stage_fix: Any) -> None:
        """PT-02 queries its pre-fix class rather than the method the task adds."""
        assert stage_fix._PATCH_QUERY_ARGUMENTS["PT-02"] == ("symbol", "DistributedSamplerWrapper")


class TestRescoreFixStage:
    """Offline replay of a completed executable run."""

    @pytest.mark.usefixtures("stub_replay_inputs")
    def test_reuses_the_captured_agent_worktree_diff(
        self,
        stage_fix: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        write_replay_source: Callable[[dict[str, object]], Path],
    ) -> None:
        """Offline replay preserves the patch transport captured from the agent worktree."""
        source_row = _source_row(captured_diff="diff --git a/example.py b/example.py\n", tool_result_tokens=4)
        source_dir = write_replay_source(source_row)
        observed: dict[str, object] = {}
        monkeypatch.setattr(stage_fix, "_print_executable_result_row", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            stage_fix,
            "parse_fix_single_cell",
            lambda *_args, **kwargs: (
                observed.update(captured_diff=kwargs["captured_diff"]) or _rescored_row(tool_result_tokens=4)
            ),
        )

        stage_fix.rescore_fix_stage(source_dir, tmp_path / "rescored", tmp_path, study="fix-single")

        assert observed["captured_diff"] == source_row["captured_diff"]

    @pytest.mark.usefixtures("stub_replay_inputs")
    @pytest.mark.parametrize("captured_diff", [None, "", "not a diff", pytest.param({"not": "a diff"}, id="mapping")])
    def test_rejects_missing_or_invalid_captured_diff(
        self,
        stage_fix: Any,
        tmp_path: Path,
        write_replay_source: Callable[[dict[str, object]], Path],
        captured_diff: object,
    ) -> None:
        """Agent-worktree replays cannot silently fall back to a response envelope."""
        source_dir = write_replay_source(_source_row(captured_diff=captured_diff))

        with pytest.raises(ValueError, match=r"captured.*diff"):
            stage_fix.rescore_fix_stage(source_dir, tmp_path / "rescored", tmp_path, study="fix-single")


class TestRunFixStagePaid:
    """Paid execution path of the executable stages."""

    @pytest.mark.parametrize(
        ("study", "loader", "scope", "binding", "prompt"),
        [
            pytest.param(
                "fix-single",
                "load_fix_single_tasks",
                "resolve_fix_single_scope",
                "validate_fix_single_binding",
                "fix_single_prompt",
                id="fix-single",
            ),
            pytest.param(
                "fix-multi",
                "load_fix_multi_tasks",
                "resolve_fix_multi_scope",
                "validate_fix_multi_binding",
                "fix_multi_prompt",
                id="fix-multi",
            ),
        ],
    )
    def test_routes_every_arm_row_through_the_shared_renderer(
        self,
        stage_fix: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        study: str,
        loader: str,
        scope: str,
        binding: str,
        prompt: str,
    ) -> None:
        """All executable paid stages preserve the established interactive A/B/C renderer.

        Every arm's result row is printed through one renderer and the artifact paths are announced after the run.
        """
        rendered: list[tuple[str, str]] = []
        contract = SimpleNamespace(task_id="fixture-task", baseline_commit="baseline")
        adapter = SimpleNamespace(create_input_snapshot=lambda *_args, **_kwargs: None, close=lambda: None)
        executed = {
            **_rescored_row(tool_result_tokens=None),
            "elapsed_s": 1.0,
            "primary_correct": True,
            "pooling_eligible": True,
            "execution": {
                "recount_recoverable": False,
                "recount_oracle_passed": None,
                "patch_applied": True,
                "targeted_test_passed": True,
            },
        }
        monkeypatch.setattr(stage_fix, "_structural", lambda: SimpleNamespace(CodexRunner=lambda *_a, **_k: adapter))
        monkeypatch.setattr(stage_fix, loader, lambda *_args: [{"task": {}, "contract": contract}])
        monkeypatch.setattr(stage_fix, scope, lambda *_args: {"scope_sha256": FIXTURE_SCOPE_SHA})
        monkeypatch.setattr(stage_fix, "_resolve_scope", lambda *_args: {"scope_sha256": FIXTURE_SCOPE_SHA})
        monkeypatch.setattr(stage_fix, "_stage_source_binding", lambda *_args: {"source": "locked"})
        monkeypatch.setattr(stage_fix, "preflight_executable_agent_workspace", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(stage_fix, binding, lambda *_args: None)
        monkeypatch.setattr(stage_fix, prompt, lambda *_args: "fixture prompt")
        monkeypatch.setattr(stage_fix.runtime, "print_arm_row", lambda row, arm: rendered.append((row, arm)))
        monkeypatch.setattr(
            stage_fix, "execute_executable_agent_cell", lambda *_args, **kwargs: {**executed, "arm": kwargs["arm"]}
        )

        stage_fix.run_fix_stage(
            study=study,
            repo_path=tmp_path,
            selected={"fixture-task"},
            dry_run=False,
            resolve_scope=False,
            auth_source=tmp_path / "auth.json",
            run_dir=tmp_path / study,
            paid_approval=FIXTURE_APPROVAL_TOKEN,
            model="fixture-model",
        )

        output = capsys.readouterr().out
        assert [arm for _row, arm in rendered] == list(stage_fix.ARMS)
        assert all("quality=1.000" in row and "oracle=✓" in row for row, _arm in rendered)
        assert f"ARTIFACTS:\n - telemetry={tmp_path / study / 'telemetry.jsonl'}" in output
        assert f" - metadata={tmp_path / study / 'run-metadata.json'}" in output

    def test_execution_is_controlled_only_by_dry_run(self, stage_fix: Any) -> None:
        """The unified launcher must not leak a public ``--paid`` switch into a fix stage."""
        parameters = inspect.signature(stage_fix.run_fix_stage).parameters

        assert "dry_run" in parameters
        assert "paid" not in parameters


class TestFixSingleScoring:
    """Scoring and prompts of the Fix-Single study."""

    @pytest.fixture(name="task")
    def _task(self, stage_fix: Any) -> dict[str, Any]:
        """First Fix-Single suite entry."""
        return next(iter(stage_fix.load_task_suite(stage_fix.FIX_SINGLE_TASKS_PATH)))

    @pytest.mark.parametrize(
        ("arm", "compliance", "strict_conformance", "pooling"),
        [
            pytest.param("B_auto", True, None, True, id="b_auto"),
            pytest.param("C_strict", False, False, False, id="c_strict"),
        ],
    )
    def test_preserves_optional_and_forced_query_controls(
        self,
        stage_fix: Any,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        task: dict[str, Any],
        arm: str,
        compliance: bool,
        strict_conformance: bool | None,
        pooling: bool,
    ) -> None:
        """B permits zero use while C remains an ineligible forced-query negative control.

        With no Codemap query observed, B stays compliant and poolable; C fails compliance and conformance.
        """
        contract = build_fix_single_contract(task)
        transport = _fm_parsed()
        execution = _fm_execution(contract)
        monkeypatch.setattr(stage_fix.runtime, "parse_codex_jsonl", lambda *_args, **_kwargs: transport)
        monkeypatch.setattr(
            stage_fix,
            "execute_fix_single_patch",
            lambda *_args, **_kwargs: SimpleNamespace(as_dict=lambda: execution),
        )
        expected_path = contract.expected_paths[0]
        captured_diff = (
            f"diff --git a/{expected_path} b/{expected_path}\n"
            f"--- a/{expected_path}\n"
            f"+++ b/{expected_path}\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )

        row = stage_fix.parse_fix_single_cell(
            "{}",
            arm=arm,
            item={"contract": contract},
            skill_path=None,
            repo_path=tmp_path,
            captured_diff=captured_diff,
        )

        assert row["codemap_used"] is False
        assert row["compliance"] is compliance
        assert row["strict_query_conformance"] is strict_conformance
        assert row["pooling_eligible"] is pooling

    def test_b_prompt_leaves_codemap_use_optional(self, stage_fix: Any, task: dict[str, Any]) -> None:
        """The auto arm's prompt tells the model to use Codemap only when useful."""
        assert "use it when useful" in stage_fix.fix_single_prompt("B_auto", task)

    def test_c_prompt_forces_the_task_specific_query(self, stage_fix: Any, task: dict[str, Any]) -> None:
        """The strict arm's prompt names the compact query argv registered for the task."""
        task_id = build_fix_single_contract(task).task_id
        expected_arguments = stage_fix._FIX_SINGLE_QUERY_ARGUMENTS[task_id]

        prompt = stage_fix.fix_single_prompt("C_strict", task)

        assert f'`"$CODEMAP_BIN" query --compact {shlex.join(expected_arguments)}`.' in prompt


class TestFixMultiScoring:
    """Scoring and prompts of the Fix-Multi study."""

    @pytest.mark.parametrize(
        "observed_arguments",
        [
            pytest.param(["symbol", "EarlyStopping._run_early_stopping_check"], id="wrong-query-command"),
            pytest.param(
                ["fn-rdeps", "lightning.pytorch.callbacks.early_stopping::EarlyStopping._run_early_stopping_check"],
                id="missing-required-flags",
            ),
            pytest.param(
                [
                    "fn-rdeps",
                    "--exclude-tests",
                    "lightning.pytorch.callbacks.early_stopping::EarlyStopping._run_early_stopping_check",
                ],
                id="partial-required-flags",
            ),
        ],
    )
    def test_strict_patch_rejects_noncanonical_query_use_from_pooling(
        self,
        stage_fix: Any,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        observed_arguments: list[str],
    ) -> None:
        """C compact-call compliance does not let a wrong task-fit argv enter pooling."""
        task = next(task for task in stage_fix.load_task_suite(stage_fix.FIX_MULTI_TASKS_PATH) if task["id"] == "FM-01")
        contract = stage_fix.build_fix_multi_contract(task)

        row = _score_fix_multi(
            stage_fix,
            monkeypatch,
            tmp_path,
            contract,
            _fm_execution(contract),
            arm="C_strict",
            parsed=_fm_strict_query_parsed(observed_arguments),
        )

        assert row["success"] is True
        assert row["primary_correct"] is True
        assert row["codemap_used"] is True
        assert row["compliance"] is True
        assert row["strict_query_conformance"] is False
        assert row["pooling_eligible"] is False

    @pytest.mark.parametrize(
        ("task_id", "expected_arguments"),
        [
            pytest.param(
                "FM-01",
                [
                    "fn-rdeps",
                    "lightning.pytorch.callbacks.early_stopping::EarlyStopping._run_early_stopping_check",
                    "--exclude-tests",
                ],
                id="fm-01",
            ),
            pytest.param(
                "FM-02",
                [
                    "fn-rdeps",
                    "lightning.pytorch.callbacks.model_checkpoint::ModelCheckpoint._save_checkpoint",
                    "--exclude-tests",
                ],
                id="fm-02",
            ),
            pytest.param(
                "FM-03", ["find-symbol", r"Strategy\.setup_environment$", "--exclude-tests", "--limit", "0"], id="fm-03"
            ),
        ],
    )
    def test_strict_prompt_and_conformance_use_task_specific_argv(
        self,
        stage_fix: Any,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        task_id: str,
        expected_arguments: list[str],
    ) -> None:
        """Each Fix-Multi strict task requires and credits only its compact query argv."""
        task = next(task for task in stage_fix.load_task_suite(stage_fix.FIX_MULTI_TASKS_PATH) if task["id"] == task_id)
        contract = stage_fix.build_fix_multi_contract(task)

        prompt = stage_fix.fix_multi_prompt("C_strict", task)
        row = _score_fix_multi(
            stage_fix,
            monkeypatch,
            tmp_path,
            contract,
            _fm_execution(contract),
            arm="C_strict",
            parsed=_fm_strict_query_parsed(expected_arguments),
        )

        assert f'`"$CODEMAP_BIN" query --compact {shlex.join(expected_arguments)}`.' in prompt
        assert row["strict_query_conformance"] is True
        assert row["pooling_eligible"] is True

    @pytest.mark.parametrize("arm", ["A_plain", "B_auto", "C_strict"])
    def test_prompt_discloses_unavailable_git_and_project_test_boundaries(self, stage_fix: Any, arm: str) -> None:
        """All arms avoid wasting turns on intentionally inaccessible workspace facilities."""
        task = next(task for task in stage_fix.load_task_suite(stage_fix.FIX_MULTI_TASKS_PATH) if task["id"] == "FM-01")

        prompt = stage_fix.fix_multi_prompt(arm, task)

        assert "Git metadata is intentionally inaccessible" in prompt
        assert "Do not invoke Git" in prompt
        assert "project dependencies are intentionally unavailable" in prompt

    @pytest.mark.parametrize(
        ("changed_paths", "boundary_passed"),
        [
            pytest.param(lambda expected: list(reversed(expected)), True, id="reordered"),
            pytest.param(lambda expected: list(expected[:-1]), False, id="missing-path"),
            pytest.param(
                lambda expected: [*expected, "src/lightning/pytorch/strategies/extra.py"], False, id="extra-path"
            ),
        ],
    )
    def test_changed_path_boundary_uses_unordered_set_semantics(
        self,
        stage_fix: Any,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fm03_contract: Any,
        changed_paths: Callable[[Any], list[str]],
        boundary_passed: bool,
    ) -> None:
        """Equivalent changed-path sets stay eligible in any order; missing or extra paths do not.

        Git may return the changed paths in a different order than the contract lists them.
        """
        execution = _fm_execution(fm03_contract, changed_paths=changed_paths(fm03_contract.expected_paths))

        row = _score_fix_multi(stage_fix, monkeypatch, tmp_path, fm03_contract, execution)

        assert row["changed_path_boundary_passed"] is boundary_passed
        assert row["pooling_eligible"] is boundary_passed

    @pytest.mark.parametrize(
        ("regression_test_passed", "pooled"),
        [
            pytest.param(False, False, id="broken-regressions"),
            pytest.param(True, True, id="passing-regressions"),
        ],
    )
    def test_regression_result_decides_pooling_not_primary_correctness(
        self,
        stage_fix: Any,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fm03_contract: Any,
        regression_test_passed: bool,
        pooled: bool,
    ) -> None:
        """A patch that fixes its target while breaking regressions is not pooled; passing regressions stay poolable.

        Mirrors the Patch stage, where regression safety enters through pooling eligibility rather than through
        ``primary_correct``.
        """
        execution = _fm_execution(fm03_contract, regression_test_passed=regression_test_passed)

        row = _score_fix_multi(stage_fix, monkeypatch, tmp_path, fm03_contract, execution)

        assert row["primary_correct"] is True
        assert row["changed_path_boundary_passed"] is True
        assert row["pooling_eligible"] is pooled

    def test_contract_without_regression_commands_moves_no_score(
        self, stage_fix: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fm03_contract: Any
    ) -> None:
        """The regression gate passes vacuously for tasks that declare no commands.

        Every current FS/FM task declares none, so the execution dict carries no ``regression_test_passed`` key at all.
        """
        row = _score_fix_multi(stage_fix, monkeypatch, tmp_path, fm03_contract, _fm_execution(fm03_contract))

        assert fm03_contract.regression_test_commands == ()
        assert row["pooling_eligible"] is True

    def test_provider_binding_omits_regression_commands_when_undeclared(self, fm03_contract: Any) -> None:
        """The locked binding gains a key only for a contract that declares commands.

        ``provider_binding`` is persisted into result artifacts and compared byte-for-byte on rescore, so an
        unconditional key would invalidate every historical row.
        """
        assert "regression_test_commands_sha256" not in fm03_contract.provider_binding()

    def test_provider_binding_locks_declared_regression_commands(self, fm03_contract: Any) -> None:
        """Declared commands become a science-bearing coordinate: different commands give different digests."""
        declared = dataclasses.replace(fm03_contract, regression_test_commands=("pytest tests/test_a.py",))
        other = dataclasses.replace(fm03_contract, regression_test_commands=("pytest tests/test_b.py",))

        assert "regression_test_commands_sha256" in declared.provider_binding()
        assert (
            declared.provider_binding()["regression_test_commands_sha256"]
            != other.provider_binding()["regression_test_commands_sha256"]
        )
