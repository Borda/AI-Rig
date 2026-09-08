"""No-model contracts for the private Codex executable-fix stage."""

from __future__ import annotations

import contextlib
import dataclasses
import importlib.util
import inspect
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import shlex
import sys
from types import SimpleNamespace
from typing import Any

import pytest


BENCHMARKS = Path(__file__).resolve().parent.parent
FIXTURE_SCOPE_SHA = "f" * 64
FIXTURE_APPROVAL_TOKEN = FIXTURE_SCOPE_SHA[:16]
sys.path.insert(0, str(BENCHMARKS))

from _bench_common.edit_patch_contracts import EditExecution, build_edit_task_contract, build_fix_single_contract  # noqa: E402
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
    assert spec is not None and spec.loader is not None
    structural = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = structural
    spec.loader.exec_module(structural)
    from _bench_codex import stage_fix as module

    return module


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


def test_executable_result_row_uses_private_runtime_formatters(stage_fix: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Progress rendering must not depend on formatters removed from the public runner."""
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


def test_paid_stage_request_explains_missing_flags_and_stale_scope(stage_fix: Any, tmp_path: Path) -> None:
    """Paid admission errors must name the problem and give a safe recovery path."""
    common = {
        "study": "fix-single",
        "repo_path": tmp_path / "repo",
        "task_ids": ["FS-01", "FS-03"],
        "model": "gpt-5.6-luna",
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
            **common,
            auth_source=tmp_path / "auth.json",
            run_dir=tmp_path / "run",
            paid_approval="e" * 16,
        )

    message = str(error.value)
    assert f"current scope: {FIXTURE_SCOPE_SHA}" in message
    assert "--tasks FS-01,FS-03 --dry-run" in message
    assert "No model call was made." in message


def test_scope_binds_the_validated_source_and_index(stage_fix: Any) -> None:
    """A changed source input must invalidate executable paid approval."""
    task = {"contract": SimpleNamespace(task_id="FS-01", provider_binding=lambda: {"task": "one"})}
    shared = {"repo_path": "/private/tmp/repo", "repo_sha256": "repo", "manifest_sha256": "manifest"}

    first = stage_fix._resolve_scope([task], "gpt-5.6-luna", {**shared, "index_sha256": "one"})
    second = stage_fix._resolve_scope([task], "gpt-5.6-luna", {**shared, "index_sha256": "two"})

    assert first["scope_sha256"] != second["scope_sha256"]


def test_patch_scope_and_snapshot_close_over_runtime_and_implementation(stage_fix: Any, tmp_path: Path) -> None:
    """Patch approval and input archival bind every scorer/runtime byte and task index.

    Regression: the initial historical-task stage bound only a task index and
    launcher, leaving the shared scorer, mutable-worktree lifecycle, and
    designated pytest runtime outside the immutable study coordinate.
    """
    task = {"contract": SimpleNamespace(task_id="PT-01", provider_binding=lambda: {})}
    index = tmp_path / ".cache" / "codemap" / "patch" / "PT-01.json"
    index.parent.mkdir(parents=True)
    index.write_text("{}", encoding="utf-8")
    source_binding = {
        "patch_coordinates": {
            "PT-01": {
                "baseline_commit": "a" * 40,
                "raw_index_sha256": "b" * 64,
                "scan_version": "13",
            }
        }
    }

    scope = stage_fix._resolve_scope([task], "gpt-5.6-luna", source_binding)
    files = stage_fix._patch_snapshot_files(tmp_path, [task])

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
    hashes = scope["implementation_sha256"]
    assert hashes["stage_fix"] == stage_fix._file_sha256(files["stage-fix.py"])
    assert hashes["paid_lifecycle"] == stage_fix._file_sha256(files["paid-lifecycle.py"])
    assert hashes["edit_patch_contracts"] == stage_fix._file_sha256(files["edit-patch-contracts.py"])
    assert hashes["mutation_isolation"] == stage_fix._file_sha256(files["mutation-isolation.py"])
    assert hashes["patch_index_locks"] == stage_fix._file_sha256(files["patch-index-locks.json"])


def test_managed_input_recovery_preserves_the_invalid_target(stage_fix: Any) -> None:
    """Managed-target admission must recommend a recoverable reconstruction, never deletion."""
    managed_repo = stage_fix._MANAGED_PARITY_REPO

    recovery = stage_fix._stage_input_recovery(managed_repo)

    assert f"REPO_PATH={shlex.quote(str(managed_repo))}" in recovery
    assert 'mv "$REPO_PATH" "$REPO_PATH.invalid-$(date -u +%Y%m%dT%H%M%SZ)"' in recovery
    assert "run-all.sh codex --struct --tasks=FN-02 --dry-run" in recovery
    assert "rm -rf" not in recovery


def test_dry_run_emits_exact_paid_command_after_preflight(
    stage_fix: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A successful no-model admission must print a copy-paste paid command without creating a run directory."""
    contract = SimpleNamespace(task_id="FS-01", baseline_commit="baseline", provider_binding=lambda: {})
    adapter = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(stage_fix, "load_fix_single_tasks", lambda *_args: [{"task": {}, "contract": contract}])
    monkeypatch.setattr(stage_fix, "_stage_source_binding", lambda *_args: {"source": "locked"})
    monkeypatch.setattr(stage_fix, "_resolve_scope", lambda *_args: {"scope_sha256": FIXTURE_SCOPE_SHA})
    monkeypatch.setattr(
        stage_fix, "_structural", lambda: SimpleNamespace(CodexRunner=lambda *_args, **_kwargs: adapter)
    )
    monkeypatch.setattr(stage_fix, "preflight_executable_agent_workspace", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(stage_fix, "_suggested_run_dir", lambda _study: Path("benchmarks/results/fresh-run"))

    stage_fix.run_fix_stage(
        study="fix-single",
        repo_path=tmp_path / "repo",
        selected={"FS-01"},
        dry_run=True,
        resolve_scope=False,
        auth_source=None,
        run_dir=None,
        paid_approval=None,
        model="gpt-5.6-luna",
        index_path=tmp_path / "repo/.cache/codemap/repo.json",
        marketplace_root=BENCHMARKS.parent,
        codemap_bin=BENCHMARKS.parent / "plugins/codemap-py/bin/codemap-py",
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


@pytest.mark.parametrize(
    "run_dir",
    [
        pytest.param(PureWindowsPath(r"benchmarks\results\codex-fix-01"), id="windows_separator"),
        pytest.param(PurePosixPath("benchmarks/results/codex-fix-01"), id="posix_separator"),
    ],
)
def test_emitted_run_dir_argument_keeps_repository_separators(stage_fix: Any, run_dir: Any) -> None:
    """The paid command is a shell line, so its repo-relative argument stays forward-slashed.

    Regression: the suggested run directory was interpolated in native form, which handed
    a Windows operator ``--run-dir benchmarks\\results\\...`` — a value the runner reads as
    one opaque segment rather than a path under ``benchmarks/results``.
    """
    assert stage_fix._repo_relative_argument(run_dir) == "benchmarks/results/codex-fix-01"


def test_full_study_dry_run_emits_paid_command_without_task_selector(
    stage_fix: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The canonical full-study command must preserve omission of ``--tasks``."""
    contracts = [
        SimpleNamespace(task_id=task_id, baseline_commit="baseline", provider_binding=lambda: {})
        for task_id in ("FS-01", "FS-02", "FS-03", "FS-04")
    ]
    adapter = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(
        stage_fix,
        "load_fix_single_tasks",
        lambda _path, _selected: [{"task": {}, "contract": contract} for contract in contracts],
    )
    monkeypatch.setattr(
        stage_fix,
        "load_task_suite",
        lambda _path: [{"id": contract.task_id} for contract in contracts],
    )
    monkeypatch.setattr(stage_fix, "_stage_source_binding", lambda *_args: {"source": "locked"})
    monkeypatch.setattr(stage_fix, "_resolve_scope", lambda *_args: {"scope_sha256": FIXTURE_SCOPE_SHA})
    monkeypatch.setattr(
        stage_fix, "_structural", lambda: SimpleNamespace(CodexRunner=lambda *_args, **_kwargs: adapter)
    )
    monkeypatch.setattr(stage_fix, "preflight_executable_agent_workspace", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(stage_fix, "_suggested_run_dir", lambda _study: Path("benchmarks/results/full-run"))

    stage_fix.run_fix_stage(
        study="fix-single",
        repo_path=tmp_path / "repo",
        selected=None,
        dry_run=True,
        resolve_scope=False,
        auth_source=None,
        run_dir=None,
        paid_approval=None,
        model="gpt-5.6-luna",
        index_path=tmp_path / "repo/.cache/codemap/repo.json",
        marketplace_root=BENCHMARKS.parent,
        codemap_bin=BENCHMARKS.parent / "plugins/codemap-py/bin/codemap-py",
    )

    output = capsys.readouterr().out
    assert "PLAN    FS-04 C_strict" in output
    assert "--run-dir benchmarks/results/full-run" in output
    assert "--tasks" not in output


def test_patch_stage_preflights_each_distinct_task_baseline(
    stage_fix: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Patch tasks must not reuse the first task's historical baseline.

    Regression: executable preflight used only ``tasks[0]``. The real patch
    suite deliberately spans several pre-fix commits, so that shortcut can
    validate the wrong checkout and index for every later task.
    """
    contracts = [
        SimpleNamespace(task_id="PT-01", baseline_commit="baseline-one", provider_binding=lambda: {}),
        SimpleNamespace(task_id="PT-02", baseline_commit="baseline-two", provider_binding=lambda: {}),
    ]
    adapter = SimpleNamespace(close=lambda: None)
    observed: list[str] = []
    monkeypatch.setattr(
        stage_fix,
        "load_patch_tasks",
        lambda _path, _selected: [{"task": {}, "contract": contract} for contract in contracts],
    )
    monkeypatch.setattr(
        stage_fix, "load_task_suite", lambda _path: [{"id": contract.task_id} for contract in contracts]
    )
    monkeypatch.setattr(
        stage_fix,
        "_patch_stage_source_binding",
        lambda *_args: {
            "source": "locked",
            "patch_coordinates": {
                "PT-01": {"baseline_commit": "baseline-one", "raw_index_sha256": "a" * 64, "scan_version": "13"},
                "PT-02": {"baseline_commit": "baseline-two", "raw_index_sha256": "b" * 64, "scan_version": "13"},
            },
        },
    )
    monkeypatch.setattr(
        stage_fix,
        "_resolve_scope",
        lambda *_args: {
            "scope_sha256": FIXTURE_SCOPE_SHA,
            "patch_test_runtime": stage_fix.patch_test_runtime_identity(),
        },
    )
    monkeypatch.setattr(
        stage_fix, "_structural", lambda: SimpleNamespace(CodexRunner=lambda *_args, **_kwargs: adapter)
    )
    monkeypatch.setattr(
        stage_fix,
        "preflight_executable_agent_workspace",
        lambda _adapter, **kwargs: observed.append(
            f"{kwargs['baseline_commit']}:{kwargs['historical_runtime_coordinate']['raw_index_sha256']}"
            f":{kwargs['patch_contract'].task_id}"
        ),
    )

    stage_fix.run_fix_stage(
        study="patch",
        repo_path=tmp_path / "repo",
        selected=None,
        dry_run=True,
        resolve_scope=False,
        auth_source=None,
        run_dir=None,
        paid_approval=None,
        model="gpt-5.6-luna",
        index_path=tmp_path / "repo/.cache/codemap/repo.json",
        marketplace_root=BENCHMARKS.parent,
        codemap_bin=BENCHMARKS.parent / "plugins/codemap-py/bin/codemap-py",
    )

    assert observed == [f"baseline-one:{'a' * 64}:PT-01", f"baseline-two:{'b' * 64}:PT-02"]


def test_patch_preflight_reports_each_historical_baseline(
    stage_fix: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Patch dry runs expose progress while each frozen baseline test executes.

    Regression: Patch preflight ran five baseline/test fixtures before printing
    its paid scope, making a correct no-model validation look stalled.
    """
    contracts = [
        SimpleNamespace(task_id="PT-01", baseline_commit="baseline-one", provider_binding=lambda: {}),
        SimpleNamespace(task_id="PT-02", baseline_commit="baseline-two", provider_binding=lambda: {}),
    ]
    adapter = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(
        stage_fix,
        "load_patch_tasks",
        lambda _path, _selected: [{"task": {}, "contract": contract} for contract in contracts],
    )
    monkeypatch.setattr(
        stage_fix,
        "load_task_suite",
        lambda _path: [{"id": contract.task_id} for contract in contracts],
    )
    monkeypatch.setattr(
        stage_fix,
        "_patch_stage_source_binding",
        lambda *_args: {
            "source": "locked",
            "patch_coordinates": {
                "PT-01": {"baseline_commit": "baseline-one", "raw_index_sha256": "a" * 64, "scan_version": "13"},
                "PT-02": {"baseline_commit": "baseline-two", "raw_index_sha256": "b" * 64, "scan_version": "13"},
            },
        },
    )
    monkeypatch.setattr(
        stage_fix,
        "_resolve_scope",
        lambda *_args: {
            "scope_sha256": FIXTURE_SCOPE_SHA,
            "patch_test_runtime": stage_fix.patch_test_runtime_identity(),
        },
    )
    monkeypatch.setattr(
        stage_fix, "_structural", lambda: SimpleNamespace(CodexRunner=lambda *_args, **_kwargs: adapter)
    )
    monkeypatch.setattr(stage_fix, "preflight_executable_agent_workspace", lambda *_args, **_kwargs: None)

    stage_fix.run_fix_stage(
        study="patch",
        repo_path=tmp_path / "repo",
        selected=None,
        dry_run=True,
        resolve_scope=False,
        auth_source=None,
        run_dir=None,
        paid_approval=None,
        model="gpt-5.6-luna",
        index_path=tmp_path / "repo/.cache/codemap/repo.json",
        marketplace_root=BENCHMARKS.parent,
        codemap_bin=BENCHMARKS.parent / "plugins/codemap-py/bin/codemap-py",
    )

    output = capsys.readouterr().out
    assert "PREFLIGHT 1/2 PT-01 validating frozen baseline and tests..." in output
    assert "PREFLIGHT 1/2 PT-01 ✓" in output
    assert "PREFLIGHT 2/2 PT-02 validating frozen baseline and tests..." in output
    assert "PREFLIGHT 2/2 PT-02 ✓" in output


def test_patch_preflight_admits_clean_context_before_staging_fixture(
    stage_fix: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex integration probes run before the frozen fixture dirties the worktree."""
    task = next(task for task in load_task_suite(stage_fix.PATCH_TASKS_PATH) if task["id"] == "PT-01")
    contract = build_edit_task_contract(task)
    events: list[str] = []
    workspace = SimpleNamespace(
        worktree=tmp_path / "worktree",
        index_relocation={},
        cleanup=lambda: events.append("cleanup") or True,
    )
    home = SimpleNamespace(coordination_path=None, cleanup=lambda: None)

    @contextlib.contextmanager
    def _bind_workspace(*_args: Any, **_kwargs: Any) -> Any:
        """Yield through the workspace-binding boundary without altering external state."""
        yield

    class Adapter:
        """Record when each arm validates the still-clean historical worktree."""

        def prepare_verified_home(self, arm: str, **_kwargs: Any) -> Any:
            """Record preparation evidence and return the fixture-owned home."""
            events.append(f"prepare:{arm}")
            return home

    structural = stage_fix._structural()
    monkeypatch.setattr(stage_fix, "create_executable_agent_workspace", lambda *_args, **_kwargs: workspace)
    monkeypatch.setattr(
        stage_fix,
        "stage_patch_task_agent_workspace",
        lambda *_args, **_kwargs: events.append("stage-fixture"),
    )
    monkeypatch.setattr(structural, "bind_executable_agent_workspace", _bind_workspace)

    stage_fix.preflight_executable_agent_workspace(
        Adapter(),
        source_repo=tmp_path / "source",
        source_index=tmp_path / "index.json",
        baseline_commit=contract.baseline_commit,
        allow_historical_baseline=True,
        historical_runtime_coordinate={
            "baseline_commit": contract.baseline_commit,
            "raw_index_sha256": "a" * 64,
            "scan_version": "13",
        },
        patch_contract=contract,
    )

    assert events == ["prepare:A_plain", "prepare:B_auto", "prepare:C_strict", "stage-fixture", "cleanup"]


def test_patch_cell_scores_the_exact_captured_worktree_diff(stage_fix: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Historical Patch scoring must not rewrite direct-worktree diff bytes.

    Regression: the inherited Fix-Single display normalizer can insert context
    markers. A Patch result is a harness-captured Git diff, so changing it
    would make the stored digest describe different candidate bytes.
    """
    task = next(task for task in load_task_suite(stage_fix.PATCH_TASKS_PATH) if task["id"] == "PT-01")
    contract = build_edit_task_contract(task)
    captured_diff = "diff --git a/example.py b/example.py\n@@ -1 +1 @@\n-old\n+new\n"
    observed: dict[str, str] = {}
    monkeypatch.setattr(
        stage_fix.runtime,
        "parse_codex_jsonl",
        lambda *_args, **_kwargs: SimpleNamespace(
            output_text="",
            success=True,
            input_tokens=1,
            cached_input_tokens=0,
            output_tokens=1,
            reasoning_output_tokens=0,
            tool_result_tokens=0,
            command_calls=0,
            tool_elapsed_s=0.0,
            codemap_calls=0,
            codemap_observed_calls=0,
            codemap_successful_calls=0,
            codemap_direct_compact_successful_calls=0,
            codemap_skill_compact_successful_calls=0,
            codemap_errors=[],
            skill_delivery_observed=False,
            successful_query_arguments=[],
            raw_events=[],
        ),
    )

    def _execute(_repo: Path, _contract: object, diff: str) -> EditExecution:
        """Capture the candidate diff and return passing execution evidence for the fixture paths."""
        observed["diff"] = diff
        return EditExecution(
            patch_applied=True,
            targeted_test_passed=True,
            regression_test_passed=True,
            changed_paths=contract.expected_paths,
        )

    row = stage_fix._parse_patch_cell(
        "",
        arm="A_plain",
        item={"contract": contract, "provider_binding": {}},
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


def test_executable_cells_and_preflight_keep_permission_verification_enabled(
    stage_fix: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both executable paths must validate their declared workspace permissions."""
    workspace_path, source_path = tmp_path / "workspace", tmp_path / "source"
    workspace_path.mkdir()
    source_path.mkdir()
    preparation_kwargs: list[dict[str, Any]] = []
    workspace = SimpleNamespace(
        worktree=workspace_path,
        index_relocation={},
        capture_diff=lambda: "diff --git a/example.py b/example.py\n",
        changed_paths=lambda: (),
        index_unchanged=lambda: True,
        cleanup=lambda: True,
    )
    home = SimpleNamespace(coordination_path=None, cleanup=lambda: None, codemap_skill_path=None, env={})

    @contextlib.contextmanager
    def _bind_workspace(*_args: Any, **_kwargs: Any) -> Any:
        """Yield through the workspace-binding boundary without altering external state."""
        yield

    class Adapter:
        """Capture the home preparation performed by executable cells."""

        def prepare_verified_home(self, *_args: Any, **kwargs: Any) -> Any:
            """Record preparation evidence and return the fixture-owned home."""
            preparation_kwargs.append(kwargs)
            return home

        def build_command(self, *_args: Any, **_kwargs: Any) -> list[str]:
            """Return fixed Codex argv without launching a command."""
            return ["codex", "exec"]

        def run_stream(self, *_args: Any, **_kwargs: Any) -> str:
            """Return an empty transport stream without invoking Codex."""
            return ""

    structural = stage_fix._structural()
    monkeypatch.setattr(structural, "create_executable_agent_workspace", lambda *_args, **_kwargs: workspace)
    monkeypatch.setattr(structural, "bind_executable_agent_workspace", _bind_workspace)
    monkeypatch.setattr(structural, "_repo_sha", lambda _path: "baseline")
    monkeypatch.setattr(structural, "_git_porcelain_status", lambda _path: "")
    stage_fix.execute_executable_agent_cell(
        adapter=Adapter(),
        source_repo=source_path,
        source_index=tmp_path / "index.json",
        baseline_commit="baseline",
        native_arm="A_plain",
        arm="A_plain",
        prompt="fixture prompt",
        item={},
        parser=lambda *_args, **_kwargs: {
            "success": True,
            "primary_correct": True,
            "pooling_eligible": True,
            "answer_error": "",
        },
    )
    stage_fix.preflight_executable_agent_workspace(
        Adapter(), source_repo=source_path, source_index=tmp_path / "index.json", baseline_commit="baseline"
    )

    assert len(preparation_kwargs) == 1 + len(stage_fix.NATIVE_ARMS)
    assert all(kwargs["writable_workspace"] == workspace_path for kwargs in preparation_kwargs)
    assert all(kwargs["denied_workspace"] == source_path for kwargs in preparation_kwargs)


def test_patch_cell_excludes_a_clean_source_head_switch(
    stage_fix: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Patch row is not comparable when the agent changed source ``HEAD`` cleanly."""
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
            "test_fixture_patch": "diff --git a/test_fixture.py b/test_fixture.py\nnew file mode 100644\n--- /dev/null\n+++ b/test_fixture.py\n@@ -0,0 +1 @@\n+def test_fixture() -> None:\n+    pass\n",
            "test_command": "pytest test_fixture.py -q",
            "gt_files_changed": ["src/example.py"],
            "regression_test_commands": ["pytest test_regression.py -q"],
            "scoreable": True,
        }
    )
    workspace = SimpleNamespace(
        worktree=workspace_path,
        index_relocation={},
        changed_paths=lambda: ("src/example.py",),
        index_unchanged=lambda: True,
        cleanup=lambda: True,
    )
    patch_workspace = SimpleNamespace(
        workspace=workspace,
        fixture_sha256_by_path={"tests/test_fixture.py": "fixture-sha"},
        capture_answer=lambda: SimpleNamespace(diff="diff --git a/src/example.py b/src/example.py\n"),
        fixture_intact=lambda: True,
        source_unchanged=lambda: False,
    )
    home = SimpleNamespace(coordination_path=None, cleanup=lambda: None, codemap_skill_path=None, env={})

    @contextlib.contextmanager
    def _bind_workspace(*_args: Any, **_kwargs: Any) -> Any:
        """Yield through the workspace-binding boundary without altering external state."""
        yield

    structural = stage_fix._structural()
    monkeypatch.setattr(stage_fix, "create_executable_agent_workspace", lambda *_args, **_kwargs: workspace)
    monkeypatch.setattr(stage_fix, "stage_patch_task_agent_workspace", lambda *_args, **_kwargs: patch_workspace)
    monkeypatch.setattr(structural, "bind_executable_agent_workspace", _bind_workspace)
    prepared: list[dict[str, Any]] = []

    class Adapter:
        """Provide the minimum home and stream surface for one agent-cell exclusion test."""

        def prepare_verified_home(self, *_args: Any, **kwargs: Any) -> Any:
            """Record preparation evidence and return the fixture-owned home."""
            prepared.append(kwargs)
            return home

        def build_command(self, *_args: Any, **_kwargs: Any) -> list[str]:
            """Return fixed Codex argv without launching a command."""
            return ["codex", "exec"]

        def run_stream(self, *_args: Any, **_kwargs: Any) -> str:
            """Return an empty transport stream without invoking Codex."""
            return ""

    row = stage_fix.execute_executable_agent_cell(
        adapter=Adapter(),
        source_repo=source,
        source_index=tmp_path / "patch-index.json",
        baseline_commit=contract.baseline_commit,
        native_arm="A_plain",
        arm="A_plain",
        prompt="fixture",
        item={
            "contract": contract,
            "patch_test_runtime": stage_fix.patch_test_runtime_identity(),
            "historical_runtime_coordinate": {
                "baseline_commit": contract.baseline_commit,
                "raw_index_sha256": "a" * 64,
                "scan_version": "13",
            },
        },
        parser=lambda *_args, **_kwargs: {
            "success": True,
            "primary_correct": True,
            "pooling_eligible": True,
            "answer_error": "",
        },
    )

    assert row["pooling_eligible"] is False
    assert row["primary_correct"] is False
    assert row["agent_workspace"]["source_unchanged"] is False
    assert "patch_fixture_admission" not in prepared[0]


def test_patch_result_row_keeps_quality_separate_from_pooling_eligibility(stage_fix: Any) -> None:
    """Patch presentation avoids duplicating nonpoolability in quality."""
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


def test_patch_strict_query_anchors_pt02_at_existing_class(stage_fix: Any) -> None:
    """PT-02 must query its pre-fix class rather than the method the task adds."""
    assert stage_fix._PATCH_QUERY_ARGUMENTS["PT-02"] == ("symbol", "DistributedSamplerWrapper")


def test_rescore_fix_stage_reuses_captured_agent_worktree_diff(
    stage_fix: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Offline replay preserves the patch transport captured from the agent worktree."""
    source_dir, output_dir = tmp_path / "source", tmp_path / "rescored"
    source_dir.mkdir()
    source_row = _source_row(captured_diff="diff --git a/example.py b/example.py\n", tool_result_tokens=4)
    (source_dir / "run-metadata.json").write_text('{"status":"completed"}', encoding="utf-8")
    (source_dir / "telemetry.jsonl").write_text(json.dumps(source_row) + "\n", encoding="utf-8")
    (source_dir / "checksums.sha256").write_text("fixture\n", encoding="utf-8")
    observed: dict[str, object] = {}
    monkeypatch.setattr(stage_fix, "verify_checksums", lambda _path: None)
    monkeypatch.setattr(stage_fix, "_print_executable_result_row", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        stage_fix, "load_fix_single_tasks", lambda *_args: [{"contract": SimpleNamespace(task_id="fixture-task")}]
    )
    monkeypatch.setattr(stage_fix, "validate_fix_single_binding", lambda *_args: None)
    monkeypatch.setattr(
        stage_fix,
        "parse_fix_single_cell",
        lambda *_args, **kwargs: (
            observed.update(captured_diff=kwargs["captured_diff"]) or _rescored_row(tool_result_tokens=4)
        ),
    )

    stage_fix.rescore_fix_stage(source_dir, output_dir, tmp_path, study="fix-single")

    assert observed["captured_diff"] == source_row["captured_diff"]


@pytest.mark.parametrize(
    ("study", "loader", "scope"),
    (
        pytest.param("fix-single", "load_fix_single_tasks", "resolve_fix_single_scope", id="fix-single"),
        pytest.param("fix-multi", "load_fix_multi_tasks", "resolve_fix_multi_scope", id="fix-multi"),
    ),
)
def test_executable_paid_stages_route_every_arm_row_through_shared_renderer(
    stage_fix: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    study: str,
    loader: str,
    scope: str,
) -> None:
    """All executable paid stages preserve the established interactive A/B/C renderer."""
    rendered: list[tuple[str, str]] = []
    contract = SimpleNamespace(task_id="fixture-task", baseline_commit="baseline")
    adapter = SimpleNamespace(create_input_snapshot=lambda *_args, **_kwargs: None, close=lambda: None)
    monkeypatch.setattr(
        stage_fix, "_structural", lambda: SimpleNamespace(CodexRunner=lambda *_args, **_kwargs: adapter)
    )
    monkeypatch.setattr(stage_fix, loader, lambda *_args: [{"task": {}, "contract": contract}])
    monkeypatch.setattr(stage_fix, scope, lambda *_args: {"scope_sha256": FIXTURE_SCOPE_SHA})
    monkeypatch.setattr(stage_fix, "_resolve_scope", lambda *_args: {"scope_sha256": FIXTURE_SCOPE_SHA})
    monkeypatch.setattr(stage_fix, "_stage_source_binding", lambda *_args: {"source": "locked"})
    monkeypatch.setattr(stage_fix, "preflight_executable_agent_workspace", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        stage_fix,
        "validate_fix_single_binding" if study == "fix-single" else "validate_fix_multi_binding",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        stage_fix, "fix_single_prompt" if study == "fix-single" else "fix_multi_prompt", lambda *_args: "fixture prompt"
    )
    monkeypatch.setattr(stage_fix.runtime, "print_arm_row", lambda row, arm: rendered.append((row, arm)))
    monkeypatch.setattr(
        stage_fix,
        "execute_executable_agent_cell",
        lambda *_args, **kwargs: {
            **_rescored_row(tool_result_tokens=None),
            "arm": kwargs["arm"],
            "elapsed_s": 1.0,
            "primary_correct": True,
            "pooling_eligible": True,
            "execution": {
                "recount_recoverable": False,
                "recount_oracle_passed": None,
                "patch_applied": True,
                "targeted_test_passed": True,
            },
        },
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

    assert [arm for _row, arm in rendered] == list(stage_fix.ARMS)
    assert all("quality=1.000" in row and "oracle=✓" in row for row, _arm in rendered)
    output = capsys.readouterr().out
    assert f"ARTIFACTS:\n - telemetry={tmp_path / study / 'telemetry.jsonl'}" in output
    assert f" - metadata={tmp_path / study / 'run-metadata.json'}" in output


@pytest.mark.parametrize("captured_diff", (None, "", "not a diff", {"not": "a diff"}))
def test_rescore_fix_stage_rejects_missing_or_invalid_captured_diff(
    stage_fix: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, captured_diff: object
) -> None:
    """Agent-worktree replays cannot silently fall back to a response envelope."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "run-metadata.json").write_text('{"status":"completed"}', encoding="utf-8")
    (source_dir / "telemetry.jsonl").write_text(
        json.dumps(_source_row(captured_diff=captured_diff)) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(stage_fix, "verify_checksums", lambda _path: None)
    monkeypatch.setattr(
        stage_fix, "load_fix_single_tasks", lambda *_args: [{"contract": SimpleNamespace(task_id="fixture-task")}]
    )
    monkeypatch.setattr(stage_fix, "validate_fix_single_binding", lambda *_args: None)

    with pytest.raises(ValueError, match="captured.*diff"):
        stage_fix.rescore_fix_stage(source_dir, tmp_path / "rescored", tmp_path, study="fix-single")


@pytest.mark.parametrize(
    "observed_arguments",
    (
        ["symbol", "EarlyStopping._run_early_stopping_check"],
        ["fn-rdeps", "lightning.pytorch.callbacks.early_stopping::EarlyStopping._run_early_stopping_check"],
        [
            "fn-rdeps",
            "--exclude-tests",
            "lightning.pytorch.callbacks.early_stopping::EarlyStopping._run_early_stopping_check",
        ],
    ),
)
def test_strict_executable_patch_rejects_noncanonical_query_use_from_pooling(
    stage_fix: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, observed_arguments: list[str]
) -> None:
    """C compact-call compliance does not let a wrong task-fit argv enter pooling."""
    task = next(task for task in stage_fix.load_task_suite(stage_fix.FIX_MULTI_TASKS_PATH) if task["id"] == "FM-01")
    contract = stage_fix.build_fix_multi_contract(task)
    parsed = SimpleNamespace(
        success=True,
        output_text="summary",
        input_tokens=10,
        cached_input_tokens=2,
        output_tokens=3,
        reasoning_output_tokens=1,
        tool_result_tokens=None,
        command_calls=1,
        tool_elapsed_s=None,
        codemap_calls=1,
        codemap_observed_calls=1,
        codemap_successful_calls=1,
        codemap_direct_compact_successful_calls=0,
        codemap_skill_compact_successful_calls=1,
        codemap_errors=0,
        skill_delivery_observed=True,
        successful_query_arguments=[observed_arguments],
        raw_events=[],
    )
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
    monkeypatch.setattr(stage_fix.runtime, "parse_codex_jsonl", lambda *_args, **_kwargs: parsed)
    monkeypatch.setattr(
        stage_fix, "execute_fix_multi_patch", lambda *_args, **_kwargs: SimpleNamespace(as_dict=lambda: execution)
    )

    row = stage_fix.parse_fix_multi_cell(
        "{}",
        arm="C_strict",
        item={"contract": contract},
        skill_path=None,
        repo_path=tmp_path,
        captured_diff="diff --git a/a.py b/a.py\n",
    )

    assert row["success"] is True
    assert row["primary_correct"] is True
    assert row["codemap_used"] is True
    assert row["compliance"] is True
    assert row["strict_query_conformance"] is False
    assert row["pooling_eligible"] is False


@pytest.mark.parametrize(
    ("arm", "expected_compliance", "expected_pooling"),
    (pytest.param("B_auto", True, True, id="b_auto"), pytest.param("C_strict", False, False, id="c_strict")),
)
def test_fix_single_preserves_optional_and_forced_query_controls(
    stage_fix: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    arm: str,
    expected_compliance: bool,
    expected_pooling: bool,
) -> None:
    """B permits zero use while C remains an ineligible forced-query negative control."""
    task = next(iter(stage_fix.load_task_suite(stage_fix.FIX_SINGLE_TASKS_PATH)))
    contract = build_fix_single_contract(task)
    parsed = SimpleNamespace(
        success=True,
        output_text="summary",
        input_tokens=10,
        cached_input_tokens=2,
        output_tokens=3,
        reasoning_output_tokens=1,
        tool_result_tokens=None,
        command_calls=0,
        tool_elapsed_s=None,
        codemap_calls=0,
        codemap_observed_calls=0,
        codemap_successful_calls=0,
        codemap_direct_compact_successful_calls=0,
        codemap_skill_compact_successful_calls=0,
        codemap_errors=0,
        skill_delivery_observed=False,
        successful_query_arguments=[],
        raw_events=[],
    )
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
    monkeypatch.setattr(stage_fix.runtime, "parse_codex_jsonl", lambda *_args, **_kwargs: parsed)
    monkeypatch.setattr(
        stage_fix, "execute_fix_single_patch", lambda *_args, **_kwargs: SimpleNamespace(as_dict=lambda: execution)
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
    assert row["compliance"] is expected_compliance
    assert row["strict_query_conformance"] is (None if arm == "B_auto" else False)
    assert row["pooling_eligible"] is expected_pooling
    if arm == "B_auto":
        assert "use it when useful" in stage_fix.fix_single_prompt(arm, task)
    else:
        prompt = stage_fix.fix_single_prompt(arm, task)
        expected_arguments = stage_fix._FIX_SINGLE_QUERY_ARGUMENTS[contract.task_id]
        assert f'`"$CODEMAP_BIN" query --compact {shlex.join(expected_arguments)}`.' in prompt


@pytest.mark.parametrize(
    ("task_id", "expected_arguments"),
    (
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
    ),
)
def test_fix_multi_strict_prompt_and_conformance_use_task_specific_argv(
    stage_fix: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    task_id: str,
    expected_arguments: list[str],
) -> None:
    """Each Fix-Multi strict task requires and credits only its compact query argv."""
    task = next(task for task in stage_fix.load_task_suite(stage_fix.FIX_MULTI_TASKS_PATH) if task["id"] == task_id)
    contract = stage_fix.build_fix_multi_contract(task)
    parsed = SimpleNamespace(
        success=True,
        output_text="summary",
        input_tokens=10,
        cached_input_tokens=2,
        output_tokens=3,
        reasoning_output_tokens=1,
        tool_result_tokens=None,
        command_calls=1,
        tool_elapsed_s=None,
        codemap_calls=1,
        codemap_observed_calls=1,
        codemap_successful_calls=1,
        codemap_direct_compact_successful_calls=0,
        codemap_skill_compact_successful_calls=1,
        codemap_errors=0,
        skill_delivery_observed=True,
        successful_query_arguments=[expected_arguments],
        raw_events=[],
    )
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
    monkeypatch.setattr(stage_fix.runtime, "parse_codex_jsonl", lambda *_args, **_kwargs: parsed)
    monkeypatch.setattr(
        stage_fix, "execute_fix_multi_patch", lambda *_args, **_kwargs: SimpleNamespace(as_dict=lambda: execution)
    )

    prompt = stage_fix.fix_multi_prompt("C_strict", task)
    row = stage_fix.parse_fix_multi_cell(
        "{}",
        arm="C_strict",
        item={"contract": contract},
        skill_path=None,
        repo_path=tmp_path,
        captured_diff="diff --git a/a.py b/a.py\n",
    )

    assert f'`"$CODEMAP_BIN" query --compact {shlex.join(expected_arguments)}`.' in prompt
    assert row["strict_query_conformance"] is True
    assert row["pooling_eligible"] is True


def test_executable_prompt_discloses_unavailable_git_and_project_test_boundaries(stage_fix: Any) -> None:
    """All arms avoid wasting turns on intentionally inaccessible workspace facilities."""
    task = next(task for task in stage_fix.load_task_suite(stage_fix.FIX_MULTI_TASKS_PATH) if task["id"] == "FM-01")

    for arm in stage_fix.ARMS:
        prompt = stage_fix.fix_multi_prompt(arm, task)
        assert "Git metadata is intentionally inaccessible" in prompt
        assert "Do not invoke Git" in prompt
        assert "project dependencies are intentionally unavailable" in prompt


def test_fix_multi_changed_path_boundary_uses_unordered_set_semantics(
    stage_fix: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Equivalent changed-path sets remain eligible when Git returns another order."""
    task = next(task for task in stage_fix.load_task_suite(stage_fix.FIX_MULTI_TASKS_PATH) if task["id"] == "FM-03")
    contract = stage_fix.build_fix_multi_contract(task)
    parsed = SimpleNamespace(
        success=True,
        output_text="summary",
        input_tokens=10,
        cached_input_tokens=2,
        output_tokens=3,
        reasoning_output_tokens=1,
        tool_result_tokens=None,
        command_calls=0,
        tool_elapsed_s=None,
        codemap_calls=0,
        codemap_observed_calls=0,
        codemap_successful_calls=0,
        codemap_direct_compact_successful_calls=0,
        codemap_skill_compact_successful_calls=0,
        codemap_errors=0,
        skill_delivery_observed=False,
        successful_query_arguments=[],
        raw_events=[],
    )
    execution = {
        "baseline_failed": True,
        "patch_applied": True,
        "changed_paths": list(reversed(contract.expected_paths)),
        "targeted_test_passed": True,
        "recount_recoverable": False,
        "recount_oracle_passed": None,
        "cleanup_verified": True,
        "error": "",
    }
    monkeypatch.setattr(stage_fix.runtime, "parse_codex_jsonl", lambda *_args, **_kwargs: parsed)
    monkeypatch.setattr(
        stage_fix, "execute_fix_multi_patch", lambda *_args, **_kwargs: SimpleNamespace(as_dict=lambda: execution)
    )

    row = stage_fix.parse_fix_multi_cell(
        "{}",
        arm="A_plain",
        item={"contract": contract},
        skill_path=None,
        repo_path=tmp_path,
        captured_diff="diff --git a/a.py b/a.py\n",
    )

    assert row["changed_path_boundary_passed"] is True
    assert row["pooling_eligible"] is True

    execution["changed_paths"] = list(contract.expected_paths[:-1])
    missing = stage_fix.parse_fix_multi_cell(
        "{}",
        arm="A_plain",
        item={"contract": contract},
        skill_path=None,
        repo_path=tmp_path,
        captured_diff="diff --git a/a.py b/a.py\n",
    )
    assert missing["changed_path_boundary_passed"] is False
    assert missing["pooling_eligible"] is False

    execution["changed_paths"] = [*contract.expected_paths, "src/lightning/pytorch/strategies/extra.py"]
    extra = stage_fix.parse_fix_multi_cell(
        "{}",
        arm="A_plain",
        item={"contract": contract},
        skill_path=None,
        repo_path=tmp_path,
        captured_diff="diff --git a/a.py b/a.py\n",
    )
    assert extra["changed_path_boundary_passed"] is False
    assert extra["pooling_eligible"] is False


def test_fix_stage_execution_is_controlled_only_by_dry_run(stage_fix: Any) -> None:
    """The unified launcher must not leak a public ``--paid`` switch into a fix stage."""
    parameters = inspect.signature(stage_fix.run_fix_stage).parameters

    assert "dry_run" in parameters
    assert "paid" not in parameters


def _fm_parsed() -> SimpleNamespace:
    """Build successful transport evidence with no observed tools and unknown tool-result usage.

    >>> result = _fm_parsed()
    >>> result.success, result.command_calls, result.tool_result_tokens
    (True, 0, None)
    """
    return SimpleNamespace(
        success=True,
        output_text="summary",
        input_tokens=10,
        cached_input_tokens=2,
        output_tokens=3,
        reasoning_output_tokens=1,
        tool_result_tokens=None,
        command_calls=0,
        tool_elapsed_s=None,
        codemap_calls=0,
        codemap_observed_calls=0,
        codemap_successful_calls=0,
        codemap_direct_compact_successful_calls=0,
        codemap_skill_compact_successful_calls=0,
        codemap_errors=0,
        skill_delivery_observed=False,
        successful_query_arguments=[],
        raw_events=[],
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


def _fm_row(stage_fix: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, execution: dict[str, Any]) -> Any:
    """Score supplied execution evidence while replacing transport parsing and patch execution with local doubles.

    >>> from tempfile import TemporaryDirectory
    >>> stage = getfixture("stage_fix")
    >>> task = next(item for item in stage.load_task_suite(stage.FIX_MULTI_TASKS_PATH) if item["id"] == "FM-03")
    >>> execution = _fm_execution(stage.build_fix_multi_contract(task))
    >>> with TemporaryDirectory() as directory, pytest.MonkeyPatch.context() as patch:
    ...     row = _fm_row(stage, patch, Path(directory), execution)
    ...     row["primary_correct"], row["pooling_eligible"]
    (True, True)
    """
    task = next(task for task in stage_fix.load_task_suite(stage_fix.FIX_MULTI_TASKS_PATH) if task["id"] == "FM-03")
    contract = stage_fix.build_fix_multi_contract(task)
    monkeypatch.setattr(stage_fix.runtime, "parse_codex_jsonl", lambda *_args, **_kwargs: _fm_parsed())
    monkeypatch.setattr(
        stage_fix, "execute_fix_multi_patch", lambda *_args, **_kwargs: SimpleNamespace(as_dict=lambda: execution)
    )
    return stage_fix.parse_fix_multi_cell(
        "{}",
        arm="A_plain",
        item={"contract": contract},
        skill_path=None,
        repo_path=tmp_path,
        captured_diff="diff --git a/a.py b/a.py\n",
    )


def test_broken_regressions_remove_a_fix_cell_from_pooling(
    stage_fix: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A patch that fixes its target while breaking regressions is not pooled.

    Mirrors the Patch stage, where regression safety enters through pooling eligibility rather than through
    ``primary_correct``.
    """
    task = next(task for task in stage_fix.load_task_suite(stage_fix.FIX_MULTI_TASKS_PATH) if task["id"] == "FM-03")
    contract = stage_fix.build_fix_multi_contract(task)
    execution = _fm_execution(contract, regression_test_passed=False)

    row = _fm_row(stage_fix, monkeypatch, tmp_path, execution)

    assert row["primary_correct"] is True
    assert row["changed_path_boundary_passed"] is True
    assert row["pooling_eligible"] is False


def test_passing_regressions_keep_a_fix_cell_poolable(
    stage_fix: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Declared regressions that still pass leave the cell eligible."""
    task = next(task for task in stage_fix.load_task_suite(stage_fix.FIX_MULTI_TASKS_PATH) if task["id"] == "FM-03")
    contract = stage_fix.build_fix_multi_contract(task)
    execution = _fm_execution(contract, regression_test_passed=True)

    row = _fm_row(stage_fix, monkeypatch, tmp_path, execution)

    assert row["pooling_eligible"] is True


def test_a_contract_without_regression_commands_is_unaffected(
    stage_fix: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The new gate must move no score for the tasks that declare no commands.

    Every current FS/FM task declares none, so the execution dict carries no ``regression_test_passed`` key at all and
    the gate passes vacuously.
    """
    task = next(task for task in stage_fix.load_task_suite(stage_fix.FIX_MULTI_TASKS_PATH) if task["id"] == "FM-03")
    contract = stage_fix.build_fix_multi_contract(task)

    assert contract.regression_test_commands == ()
    row = _fm_row(stage_fix, monkeypatch, tmp_path, _fm_execution(contract))

    assert row["pooling_eligible"] is True


def test_regression_commands_are_absent_from_an_undeclared_provider_binding(stage_fix: Any) -> None:
    """The locked binding gains a key only for a contract that declares commands.

    ``provider_binding`` is persisted into result artifacts and compared byte-for-byte on rescore, so an unconditional
    key would invalidate every historical row.
    """
    task = next(task for task in stage_fix.load_task_suite(stage_fix.FIX_MULTI_TASKS_PATH) if task["id"] == "FM-03")
    contract = stage_fix.build_fix_multi_contract(task)

    assert "regression_test_commands_sha256" not in contract.provider_binding()


def test_declared_regression_commands_are_locked_into_the_provider_binding(stage_fix: Any) -> None:
    """Declared commands become a science-bearing coordinate."""
    task = next(task for task in stage_fix.load_task_suite(stage_fix.FIX_MULTI_TASKS_PATH) if task["id"] == "FM-03")
    contract = stage_fix.build_fix_multi_contract(task)
    declared = dataclasses.replace(contract, regression_test_commands=("pytest tests/test_a.py",))
    other = dataclasses.replace(contract, regression_test_commands=("pytest tests/test_b.py",))

    assert "regression_test_commands_sha256" in declared.provider_binding()
    assert (
        declared.provider_binding()["regression_test_commands_sha256"]
        != other.provider_binding()["regression_test_commands_sha256"]
    )
