"""Tests for benchmarks/run-claude-structural.py.

Public API surface covered:
  - BenchQuality dataclass: field defaults and types
  - BenchRun dataclass: field defaults and types
  - _extract_int(): regex extraction helper
  - _int_close(): tolerance comparison helper
  - _count_tol_detail(): scoring-detail dict builder
  - _parse_scan_query_subcommand(): Bash-command parser
  - _normalize_external_task(): external task schema normalizer
  - _load_tasks_file(): JSON task loader (error paths and normalization)
  - _evaluate_symbol(): symbol_extraction evaluator
  - _evaluate_fn(): fn_call_graph count evaluator
  - _evaluate_rv(): review_assistance evaluator (count and symbol-recall paths)
  - _evaluate_oss(): code_quality evaluator (coupled / xrefs_broken / undocumented / uncovered)
  - _evaluate_debug(): debug_from_trace evaluator
  - _evaluate_feature(): feature_scaffolding evaluator
  - _evaluate_real_issue(): real_issue evaluator
  - _evaluate_develop_br(): develop_blast_radius recall evaluator
  - _extract_diff(): unified-diff extractor
  - _safe_ratio(): safe division helper
  - _workflow_type_of(): workflow key accessor
  - _effective_recall(): recall accessor with fallback logic

Tests do NOT invoke the claude CLI or write files outside /tmp.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

import pytest

BENCHMARKS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BENCHMARKS))

from _bench_common.presentation import (  # noqa: E402
    BENCHMARK_OUTPUT_WIDTH,
    LEGEND_CLOSE_RULE,
    LEGEND_OPEN_RULE,
)

# ---------------------------------------------------------------------------
# Helpers shared across test classes
# ---------------------------------------------------------------------------


def _make_run(script_bench: Any, **overrides: Any) -> Any:
    """Build a successful synthetic run, applying overrides before constructing the runner's record.

    >>> run = _make_run(getfixture("script_run_bench"), success=False, task_id="example")
    >>> run.task_id, run.success, run.arm
    ('example', False, 'plain')
    """
    defaults = dict(
        arm="plain",
        task_id="SE-01",
        task_type="symbol_extraction",
        model="haiku",
        success=True,
    )
    defaults.update(overrides)
    return script_bench.BenchRun(**defaults)


def _se_task(start_line: int = 100, qname: str = "Trainer.fit") -> dict:
    """Build symbol-extraction ground truth with a ten-line span from the supplied start.

    >>> truth = _se_task(12, "Example.run")["ground_truth"]
    >>> truth["start_line"], truth["end_line"], truth["qualified_name"]
    (12, 22, 'Example.run')
    """
    return {
        "id": "SE-01",
        "type": "symbol_extraction",
        "ground_truth": {
            "start_line": start_line,
            "qualified_name": qname,
            "end_line": start_line + 10,
            "module": "lightning.pytorch.trainer",
        },
    }


def _is_pytest_argv(command: list[str]) -> bool:
    """Recognize a pytest module invocation using this process's interpreter, allowing trailing arguments.

    >>> _is_pytest_argv([sys.executable, "-m", "pytest", "-q"])
    True
    >>> _is_pytest_argv(["pytest", "-q"])
    False
    """
    return command[:3] == [sys.executable, "-m", "pytest"]


def test_patch_sandbox_recreates_a_unique_worktree_for_each_retry(
    script_run_bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The existing Claude patch boundary consumes the shared isolated-cell lifecycle."""
    roots: list[Path] = []
    test_calls = 0

    def _mkdtemp(*_args: Any, **_kwargs: Any) -> str:
        """Create a fixture-owned attempt directory for worktree lifecycle assertions."""
        root = tmp_path / f"attempt-{len(roots)}"
        root.mkdir()
        roots.append(root)
        return str(root)

    def _run(command: list[str], **_kwargs: Any) -> SimpleNamespace:
        """Supply scenario-specific subprocess outcomes at the worktree and test-command boundary."""
        nonlocal test_calls
        if "worktree" in command and "add" in command:
            Path(command[-2]).mkdir()
        elif _is_pytest_argv(command):
            test_calls += 1
            return SimpleNamespace(returncode=1 if test_calls % 2 else 0, stderr="")
        elif "worktree" in command and "remove" in command:
            worktree = Path(command[-1])
            for path in worktree.iterdir():
                path.unlink()
            worktree.rmdir()
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(script_run_bench.tempfile, "mkdtemp", _mkdtemp)
    monkeypatch.setattr(script_run_bench.subprocess, "run", _run)
    sandbox = script_run_bench.PatchSandbox(
        tmp_path,
        {
            "id": "PT-fixture",
            "pre_fix_commit": "a" * 40,
            "test_command": "pytest tests/test_fixture.py::test_fix -x",
        },
    )

    assert sandbox.run("diff --git a/a.py b/a.py\n") is True
    assert sandbox.run("diff --git a/a.py b/a.py\n") is True
    assert len(roots) == 2
    assert all(not root.exists() for root in roots)
    assert sandbox.last_mutation_evidence == {
        "worktree": str(roots[-1] / "repo"),
        "action_error": None,
        "cleanup_error": None,
        "restored": True,
    }


class TestProviderParityIntegration:
    """The Claude structural adapter consumes the locked shared contract."""

    def test_provider_parity_arm_orders_use_the_shared_coordinate_policy(
        self, script_run_bench: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each task delegates its canonical arm order to the shared Claude coordinate policy."""
        tasks = [{"id": "FN-02"}, {"id": "GR-01"}]
        calls: list[tuple[str, str, str, str, int, str]] = []
        expected = {
            "FN-02": ("C_strict", "A_plain", "B_auto"),
            "GR-01": ("B_auto", "C_strict", "A_plain"),
        }

        def _order(
            revision: str,
            provider: str,
            model: str,
            task_id: str,
            repetition: int,
            *,
            reasoning_effort: str = "",
        ) -> tuple[str, ...]:
            """Record treatment coordinates and return the expected task-specific order."""
            calls.append((revision, provider, model, task_id, repetition, reasoning_effort))
            return expected[task_id]

        monkeypatch.setattr(script_run_bench, "deterministic_arm_order", _order)

        observed = script_run_bench._arm_orders_by_task(
            tasks,
            script_run_bench.PARITY_ARMS,
            model="sonnet",
            provider_parity=True,
        )

        assert observed == expected
        assert calls == [
            (script_run_bench.PARITY_EXPERIMENT_REVISION, "claude", "sonnet", "FN-02", 1, ""),
            (script_run_bench.PARITY_EXPERIMENT_REVISION, "claude", "sonnet", "GR-01", 1, ""),
        ]

    def test_run_loop_executes_each_task_in_its_provider_parity_arm_order(
        self, script_run_bench: Any, tmp_path: Path
    ) -> None:
        """Paid execution consumes the same per-task arm order exposed by the plan."""
        calls: list[tuple[str, str]] = []

        class Runner:
            """Record each scheduled cell without starting Claude."""

            def run(self, task: dict[str, Any], arm: str, update_fn: Any = None) -> Any:
                """Return the scenario-specific benchmark result without invoking a provider."""
                calls.append((task["id"], arm))
                return _make_run(script_run_bench, task_id=task["id"], arm=arm)

        class Console:
            """Accept result rows emitted by the run loop."""

            def print(self, _message: str, **_options: Any) -> None:
                """Handle console output through the test double instead of a live terminal."""
                return None

        class Progress:
            """Provide the small Rich progress surface used by the run loop."""

            console = Console()

            def add_task(self, *_args: Any, **_kwargs: Any) -> int:
                """Return a stable progress-task identifier without creating terminal output."""
                return 1

            def update(self, *_args: Any, **_kwargs: Any) -> None:
                """Accept progress updates without rendering a terminal display."""
                return None

            def remove_task(self, *_args: Any, **_kwargs: Any) -> None:
                """Accept task removal without maintaining a live progress display."""
                return None

            def advance(self, *_args: Any, **_kwargs: Any) -> None:
                """Accept task advancement without rendering a terminal display."""
                return None

        task = {"id": "FN-02", "type": "fixture", "scoreable": False}
        loop = script_run_bench._StructuralRunLoop(
            runner=Runner(),
            repo_path=tmp_path,
            arm_orders={"FN-02": ("C_strict", "A_plain", "B_auto")},
            patch_ids=set(),
        )

        loop.run_task_arms(task, Progress(), 0)

        assert calls == [("FN-02", "C_strict"), ("FN-02", "A_plain"), ("FN-02", "B_auto")]

    def test_base_command_disables_session_persistence(self, script_run_bench: Any) -> None:
        """Every structural cell starts a non-resumable Claude process session."""
        assert "--no-session-persistence" in script_run_bench._CMD
        assert "--continue" not in script_run_bench._CMD
        assert "--resume" not in script_run_bench._CMD
        assert "--session-id" not in script_run_bench._CMD

    def test_run_loop_aligns_result_metrics_after_the_longest_arm(self, script_run_bench: Any, tmp_path: Path) -> None:
        """Every result row reserves the longest arm label plus one separating space."""
        lines: list[str] = []

        class Runner:
            """Return a minimal successful run for each requested arm."""

            def run(self, task: dict[str, Any], arm: str, update_fn: Any = None) -> Any:
                """Return the scenario-specific benchmark result without invoking a provider."""
                elapsed_s = 8.0 if arm == "plain" else 95.0
                return _make_run(script_run_bench, task_id=task["id"], arm=arm, elapsed_s=elapsed_s)

        loop = script_run_bench._StructuralRunLoop(
            runner=Runner(),
            repo_path=tmp_path,
            arm_orders={"SE-01": ("plain", "C_strict")},
            patch_ids=set(),
        )
        task = {"id": "SE-01", "type": "fixture", "scoreable": False}

        loop.run_combo(task, "plain", lines.append)
        loop.run_combo(task, "C_strict", lines.append)

        expected_width = max(map(len, (*script_run_bench.ARMS, *script_run_bench.PARITY_ARMS))) + 1
        assert lines[0].split("\t", 1)[0] == f"  ✓? SE-01 {'plain':<{expected_width}}"
        assert lines[1].split("\t", 1)[0] == f"  ✓? SE-01 {'C_strict':<{expected_width}}"
        assert lines[0].index("\ttok:") == lines[1].index("\ttok:")
        assert lines[0].index("recall=") == lines[1].index("recall=")

    def test_run_loop_labels_and_aligns_unscored_recall(self, script_run_bench: Any, tmp_path: Path) -> None:
        """Unscored recall is explicit and does not shift the following total column."""
        lines: list[str] = []

        class Runner:
            """Return one unscored and one scored result for output comparison."""

            def run(self, task: dict[str, Any], arm: str, update_fn: Any = None) -> Any:
                """Return the scenario-specific benchmark result without invoking a provider."""
                run = _make_run(script_run_bench, task_id=task["id"], arm=arm)
                if arm == "C_strict":
                    run.quality = script_run_bench.BenchQuality(scored=True, correct=True, recall=0.857)
                return run

        loop = script_run_bench._StructuralRunLoop(
            runner=Runner(),
            repo_path=tmp_path,
            arm_orders={"CQ-01": ("A_plain", "C_strict")},
            patch_ids=set(),
        )
        task = {"id": "CQ-01", "type": "code_quality"}

        loop.run_combo(task, "A_plain", lines.append)
        loop.run_combo(task, "C_strict", lines.append)

        assert "recall=?unscored" in lines[0]
        assert lines[0].index("total=") == lines[1].index("total=")

    def test_run_loop_summarizes_list_valued_expected_metrics(self, script_run_bench: Any, tmp_path: Path) -> None:
        """CQ-03 result rendering reports ranking size without formatting the list as a scalar."""
        lines: list[str] = []

        class Runner:
            """Return the list-valued quality contract produced by the CQ-03 evaluator."""

            def run(self, task: dict[str, Any], arm: str, update_fn: Any = None) -> Any:
                """Return the scenario-specific benchmark result without invoking a provider."""
                run = _make_run(script_run_bench, task_id=task["id"], arm=arm)
                run.quality = script_run_bench.BenchQuality(
                    scored=True,
                    correct=True,
                    metric_expected=[{"name": "pkg.alpha", "dep_count": 49}, {"name": "pkg.bravo", "dep_count": 42}],
                )
                return run

        loop = script_run_bench._StructuralRunLoop(
            runner=Runner(),
            repo_path=tmp_path,
            arm_orders={"CQ-03": ("B_auto",)},
            patch_ids=set(),
        )

        loop.run_combo({"id": "CQ-03", "type": "code_quality"}, "B_auto", lines.append)

        assert "\ttotal=   2\t" in lines[0]

    def test_primary_contract_preserves_raw_task_identity_and_manifest_policy(self, script_run_bench: Any) -> None:
        """The primary suite uses shared raw loading, hashes, and revision-bound policy without a model call."""
        tasks, policies = script_run_bench._load_primary_parity_contract()
        task = next(task for task in tasks if task["id"] == "FN-02")
        manifest = json.loads(script_run_bench.PARITY_MANIFEST_FILE.read_text(encoding="utf-8"))
        expected_task = next(row for suite in manifest["suites"] for row in suite["tasks"] if row["id"] == task["id"])

        assert script_run_bench._task_hash(task) == expected_task["canonical_task_sha256"]
        assert script_run_bench._prompt_hash(task) == expected_task["prompt_sha256"]
        assert policies[task["id"]].experiment_revision == script_run_bench.PARITY_EXPERIMENT_REVISION

    def test_legacy_arms_keep_their_labels_and_have_no_parity_identity(self, script_run_bench: Any) -> None:
        """Existing transport labels stay available but cannot inherit A/B/C identity."""
        assert script_run_bench.ARMS == ("plain", "codemap")
        assert script_run_bench.PARITY_ARMS == ("A_plain", "B_auto", "C_strict")
        assert script_run_bench.PARITY_ARM_BY_LEGACY_ARM == {}
        assert script_run_bench._arm_orders_by_task(
            [{"id": "FN-02"}],
            script_run_bench.ARMS,
            model="haiku",
            provider_parity=False,
        ) == {"FN-02": script_run_bench.ARMS}

    def test_runner_stamps_legacy_identity_without_relabelling_legacy_arm(
        self, script_run_bench: Any, tmp_path: Path
    ) -> None:
        """A legacy transport run retains only the explicit legacy experiment identity."""
        task = {"id": "legacy-fixture", "prompt": "answer", "type": "fixture", "scoreable": False}
        runner = script_run_bench.BenchRunner(
            "haiku",
            "fixture-model",
            tmp_path,
            tmp_path / "index.json",
        )
        runner._execute = lambda *_args, **_kwargs: script_run_bench.BenchRun(
            arm="codemap",
            task_id=task["id"],
            task_type=task["type"],
            model="haiku",
            success=True,
        )

        result = runner.run(task, "codemap")

        assert result.arm == "codemap"
        assert result.parity_arm == ""
        assert result.compliance is None
        assert result.treatment_adherence is None
        assert result.contaminated is False
        assert result.experiment_revision == script_run_bench.LEGACY_EXPERIMENT_REVISION
        assert result.arm_contract_hash == ""
        assert result.task_hash == script_run_bench._task_hash(task)
        assert result.prompt_hash == script_run_bench._prompt_hash(task)

    def test_canonical_result_stamps_the_locked_policy_and_provenance(
        self, script_run_bench: Any, tmp_path: Path
    ) -> None:
        """Canonical arms receive provider-parity task and contract identity."""
        tasks, policies = script_run_bench._load_primary_parity_contract()
        task = next(task for task in tasks if task["id"] == "FN-02")
        runner = script_run_bench.BenchRunner(
            "haiku",
            "fixture-model",
            tmp_path,
            tmp_path / "index.json",
            task_policies=policies,
            suite_hash=script_run_bench.PRIMARY_SUITE_HASH,
            suite_raw_hash=script_run_bench.PRIMARY_SUITE_RAW_HASH,
        )
        runner._execute = lambda *_args, **_kwargs: script_run_bench.BenchRun(
            arm="B_auto", task_id=task["id"], task_type=task["type"], model="haiku", success=True
        )

        result = runner.run(task, "B_auto")

        assert result.parity_arm == "B_auto"
        assert result.experiment_revision == script_run_bench.PARITY_EXPERIMENT_REVISION
        assert result.suite_hash == script_run_bench.PRIMARY_SUITE_HASH
        assert result.suite_raw_hash == script_run_bench.PRIMARY_SUITE_RAW_HASH
        assert result.arm_contract_hash == script_run_bench.ARM_CONTRACTS["B_auto"]["contract_sha256"]
        assert result.evaluator_id == "_evaluate_develop_br"
        assert len(result.evaluator_hash) == 64
        assert len(result.envelope_hash) == 64
        assert result.scoreable is True

    def test_canonical_task_hash_mismatch_fails_before_execution(self, script_run_bench: Any, tmp_path: Path) -> None:
        """Changed task bytes cannot enter a canonical provider-parity run."""
        tasks, policies = script_run_bench._load_primary_parity_contract()
        task = next(task for task in tasks if task["id"] == "FN-02").copy()
        task["prompt"] = f"{task['prompt']} tampered"
        runner = script_run_bench.BenchRunner(
            "haiku",
            "fixture-model",
            tmp_path,
            tmp_path / "index.json",
            task_policies=policies,
            suite_hash=script_run_bench.PRIMARY_SUITE_HASH,
        )
        runner._execute = lambda *_args, **_kwargs: pytest.fail("canonical mismatch reached execution")

        with pytest.raises(ValueError, match="task.*hash|hash.*task"):
            runner.run(task, "A_plain")

    def test_c_strict_tracks_no_call_as_noncompliance_without_changing_quality(
        self, script_run_bench: Any, tmp_path: Path
    ) -> None:
        """C records a missing Codemap call separately from the unscored task result."""
        runner = script_run_bench.BenchRunner("haiku", "fixture-model", tmp_path, tmp_path / "index.json")
        task = {"id": "fixture", "prompt": "answer", "type": "fixture", "scoreable": False}
        runner._stream = lambda _cmd, result, _arm, update_fn=None: setattr(result, "success", True)

        result = runner._execute(task, "C_strict")

        assert result.compliance is False
        assert result.quality == script_run_bench.BenchQuality(scored=False)

    @pytest.mark.parametrize(
        ("arm", "scan_query_calls", "expected_compliance", "expected_adherence"),
        [
            pytest.param("A_plain", 0, None, True, id="plain-clean"),
            pytest.param("B_auto", 0, None, True, id="auto-optional-no-call"),
            pytest.param("C_strict", 0, False, False, id="required-no-call"),
            pytest.param("C_strict", 1, True, True, id="required-call"),
        ],
    )
    def test_canonical_arm_records_treatment_adherence(
        self,
        script_run_bench: Any,
        tmp_path: Path,
        arm: str,
        scan_query_calls: int,
        expected_compliance: bool | None,
        expected_adherence: bool,
    ) -> None:
        """Canonical A/B/C rows record assigned availability separately from answer quality."""
        runner = script_run_bench.BenchRunner("haiku", "fixture-model", tmp_path, tmp_path / "index.json")
        task = {"id": "fixture", "prompt": "answer", "type": "fixture", "scoreable": False}

        def _stream(_cmd: list[str], result: Any, _arm: str, update_fn: Any = None) -> None:
            """Populate a successful streamed result with the scenario's tool evidence."""
            result.success = True
            result.input_tokens = 1
            result.scan_query_calls = scan_query_calls

        runner._stream = _stream

        result = runner._execute(task, arm)

        assert result.compliance is expected_compliance
        assert result.contaminated is False
        assert result.treatment_adherence is expected_adherence

    def test_canonical_contamination_makes_treatment_adherence_false(
        self, script_run_bench: Any, tmp_path: Path
    ) -> None:
        """An answer-file-read is contamination and invalidates a canonical arm."""
        runner = script_run_bench.BenchRunner("haiku", "fixture-model", tmp_path, tmp_path / "index.json")
        task = {**_se_task(), "prompt": "answer"}

        def _stream(_cmd: list[str], result: Any, _arm: str, update_fn: Any = None) -> None:
            """Populate a successful streamed result with the scenario's tool evidence."""
            result.success = True
            result.input_tokens = 1
            result.tool_log.append("Read: benchmarks/results/answer.json")

        runner._stream = _stream

        result = runner._execute(task, "A_plain")

        assert result.error == "answer_file_read"
        assert result.contaminated is True
        assert result.treatment_adherence is False

    def test_run_serialization_includes_treatment_provenance(
        self, script_run_bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """JSONL telemetry preserves adherence and contamination for later pairing ingestion."""
        run = _make_run(
            script_run_bench,
            arm="A_plain",
            contaminated=False,
            treatment_adherence=True,
        )
        serialized = script_run_bench.asdict(run)
        monkeypatch.setattr(script_run_bench, "RESULTS_DIR", tmp_path)
        saved = script_run_bench._save_results([run], "haiku")

        persisted = json.loads(saved.read_text(encoding="utf-8"))
        assert serialized["contaminated"] is False
        assert serialized["treatment_adherence"] is True
        assert persisted["contaminated"] is False
        assert persisted["treatment_adherence"] is True

    @pytest.mark.parametrize("arm", ["A_plain", "B_auto", "C_strict"])
    def test_canonical_commands_omit_turn_caps_while_legacy_keeps_its_task_cap(
        self, script_run_bench: Any, tmp_path: Path, arm: str
    ) -> None:
        """Canonical structural cells use wall-clock control; legacy cells retain their task cap."""
        task = {
            "id": "fixture",
            "prompt": "answer",
            "type": "develop_blast_radius",
            "ground_truth": {"unique_caller_count": 12},
            "scoreable": False,
        }
        runner = script_run_bench.BenchRunner("haiku", "fixture-model", tmp_path, tmp_path / "index.json")
        commands: dict[str, list[str]] = {}

        def _capture_command(cmd: list[str], result: Any, selected_arm: str, update_fn: Any = None) -> None:
            """Record argv by treatment and supply minimal input-token usage."""
            commands[selected_arm] = cmd
            result.input_tokens = 1

        runner._stream = _capture_command
        runner._execute(task, arm)
        runner._execute(task, "plain")

        assert "--max-turns" not in commands[arm]
        legacy = commands["plain"]
        assert legacy[legacy.index("--max-turns") + 1] == "80"

    def test_dry_run_plans_canonical_cells_without_executing_claude(
        self,
        script_run_bench: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The no-model dry run validates inputs then emits deterministic A/B/C cells only."""
        index_path = tmp_path / "index.json"
        index_path.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(
            script_run_bench.BenchRunner,
            "_execute",
            lambda *_args, **_kwargs: pytest.fail("dry run executed Claude"),
        )
        runtime_inputs: list[tuple[Path, Path]] = []
        monkeypatch.setattr(
            script_run_bench,
            "_validate_primary_runtime",
            lambda repo_path, index, *_relocation: runtime_inputs.append((repo_path, index)),
        )
        expected_order = ("C_strict", "A_plain", "B_auto")
        monkeypatch.setattr(script_run_bench, "deterministic_arm_order", lambda *_args, **_kwargs: expected_order)

        script_run_bench.main(
            repo_path=tmp_path,
            index_path=index_path,
            tasks=["FN-02"],
            arm="all",
            dry_run=True,
            provider_parity=True,
        )

        planned = [
            line.removeprefix("PLAN\t") for line in capsys.readouterr().out.splitlines() if line.startswith("PLAN\t")
        ]
        assert planned == [f"FN-02\t{arm_name}" for arm_name in expected_order]
        assert runtime_inputs == [(tmp_path, index_path)]

    def test_run_header_and_legend_render_through_the_shared_framed_surfaces(
        self,
        script_run_bench: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A redirected run frames its legend in titled rules and its banner as a plain section rule.

        Scenario: an operator pipes a run into an archived log. The Claude and Codex lanes used to
        disagree here — Codex framed its legend, this runner printed a bare ``Task series legend:``
        header and hand-drawn 64-column banners. The redirected forms are what the log keeps and
        what a replay upgrades to panels, so they are asserted rather than the terminal rendering.
        """
        index_path = tmp_path / "index.json"
        index_path.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(script_run_bench, "_validate_primary_runtime", lambda *_args: None)
        monkeypatch.setattr(script_run_bench, "BenchRunner", lambda **_kwargs: None)

        class ProgressReached(RuntimeError):
            """Stop the no-model test once the header and legend have been emitted."""

            pass

        def _stop_before_the_first_cell(_console: Any) -> None:
            """Fail the run at progress construction, after the header block is printed."""
            raise ProgressReached

        monkeypatch.setattr(script_run_bench, "make_progress", _stop_before_the_first_cell)

        with pytest.raises(ProgressReached):
            script_run_bench.main(repo_path=tmp_path, index_path=index_path, tasks=["FN-02"], arm="all")

        lines = capsys.readouterr().out.splitlines()
        assert "== ▶ RUN START — model=haiku ==" in lines
        assert sum(line == LEGEND_OPEN_RULE for line in lines) == 1
        assert sum(line == LEGEND_CLOSE_RULE for line in lines) == 1
        assert "  task series:" in lines

    def test_summary_banner_renders_as_a_plain_section_rule_when_redirected(
        self, script_run_bench: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The end-of-run summary announces itself with the same rule every other phase uses.

        Scenario: the summary header was a hand-drawn 64-column ``=`` box, so it neither matched
        the run-start banner above it nor the 78-column width every framed block now shares.
        """
        script_run_bench._print_summary(_parity_runs(script_run_bench), "haiku")

        assert "== Codemap benchmark — model=haiku ==" in capsys.readouterr().out.splitlines()

    def test_console_is_the_shared_benchmark_console(self, script_run_bench: Any) -> None:
        """Framed output is fixed at the shared benchmark width instead of the window's own width.

        Scenario: this runner built a bare ``rich.Console``, so its panels and rules stretched to
        whatever the terminal happened to be while the paid-command block stayed at 78 columns.
        """
        assert script_run_bench._console.width == BENCHMARK_OUTPUT_WIDTH

    @pytest.mark.parametrize(
        ("arm", "timeout", "expected"),
        [
            pytest.param("A_plain", None, 600, id="canonical-default"),
            pytest.param("A_plain", 123, 123, id="canonical-explicit-override"),
            pytest.param("plain", None, 210, id="legacy-model-default"),
        ],
    )
    def test_main_selects_shared_parity_timeout_without_changing_legacy_runs(
        self,
        script_run_bench: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        arm: str,
        timeout: int | None,
        expected: int,
    ) -> None:
        """Canonical cells use 600 seconds unless explicitly overridden; legacy policy is unchanged."""
        index_path = tmp_path / "index.json"
        index_path.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(script_run_bench, "_validate_primary_runtime", lambda *_args: None)
        observed: dict[str, int] = {}

        class RunnerReached(RuntimeError):
            """Stop the no-model test after runner construction."""

            pass

        def _capture_runner(*_args: Any, **kwargs: Any) -> None:
            """Record the timeout then stop execution at runner construction."""
            observed["timeout"] = kwargs["timeout"]
            raise RunnerReached

        monkeypatch.setattr(script_run_bench, "BenchRunner", _capture_runner)

        with pytest.raises(RunnerReached):
            script_run_bench.main(
                repo_path=tmp_path,
                index_path=index_path,
                tasks=["FN-02"],
                arm=arm,
                timeout=timeout,
            )

        assert observed == {"timeout": expected}

    def test_resume_rejects_cached_canonical_result_with_another_contract(
        self, script_run_bench: Any, tmp_path: Path
    ) -> None:
        """A cached result cannot satisfy a canonical arm after its contract identity changes."""
        tasks, policies = script_run_bench._load_primary_parity_contract()
        task = next(task for task in tasks if task["id"] == "FN-02")
        runner = script_run_bench.BenchRunner(
            "haiku",
            "fixture-model",
            tmp_path,
            tmp_path / "index.json",
            task_policies=policies,
            suite_hash=script_run_bench.PRIMARY_SUITE_HASH,
        )
        task_hash = script_run_bench._task_hash(task)
        runner.resume_cache = {
            (task["id"], "B_auto", "haiku", runner.repo_sha, runner.index_sha, task_hash): {
                "experiment_revision": script_run_bench.PARITY_EXPERIMENT_REVISION,
                "arm_contract_hash": "different-contract",
            }
        }
        calls: list[str] = []

        def _execute(_task: dict, arm: str, **_kwargs: Any) -> Any:
            """Record the selected treatment and return a successful synthetic run."""
            calls.append(arm)
            return script_run_bench.BenchRun(
                arm=arm,
                task_id=task["id"],
                task_type=task["type"],
                model="haiku",
                success=True,
            )

        runner._execute = _execute

        runner.run(task, "B_auto")

        assert calls == ["B_auto"]

    def test_shared_evaluator_runs_once_and_preserves_diagnostics(self, script_run_bench: Any) -> None:
        """The shared registry retains the one detailed Claude score rather than evaluating twice."""
        calls: list[tuple[dict[str, Any], str]] = []
        detailed = script_run_bench.BenchQuality(
            scored=True,
            correct=True,
            recall=0.75,
            evaluator_used="fixture",
            scoring_detail={"method": "fixture"},
        )

        def _evaluate_demo(task: dict[str, Any], output_text: str) -> Any:
            """Record evaluator inputs and return the detailed fixture result."""
            calls.append((task, output_text))
            return detailed

        registry = script_run_bench.EvaluatorRegistry({"demo": script_run_bench._wrap_bench_evaluator(_evaluate_demo)})
        task = {"id": "EV-01", "prompt": "score", "type": "demo", "scoreable": True}

        result = script_run_bench._evaluate_shared_task(task, "answer", registry=registry)

        assert calls == [(task, "answer")]
        assert result is detailed
        assert result.recall == pytest.approx(0.75)
        assert result.evaluator_used == "fixture"
        assert result.scoring_detail == {"method": "fixture"}

    def test_shared_evaluator_uses_recall_as_normalized_quality_score(self, script_run_bench: Any) -> None:
        """Recall-based task families retain their continuous score for paired quality effects."""
        detailed = script_run_bench.BenchQuality(
            scored=True,
            correct=False,
            recall=0.65,
            evaluator_used="fixture",
        )
        registry = script_run_bench.EvaluatorRegistry(
            {"demo": script_run_bench._wrap_bench_evaluator(lambda _task, _output: detailed)}
        )

        result = registry.evaluate(
            {"id": "EV-02", "prompt": "score", "type": "demo", "scoreable": True},
            "answer",
        )

        assert result.quality_score == pytest.approx(0.65)


# ===========================================================================
# BenchQuality dataclass
# ===========================================================================


class TestBenchQuality:
    """Field defaults and type constraints for BenchQuality."""

    def test_defaults_scored_false(self, script_run_bench: Any) -> None:
        """Newly constructed BenchQuality must default to unscored state."""
        q = script_run_bench.BenchQuality()
        assert q.scored is False

    def test_defaults_correct_false(self, script_run_bench: Any) -> None:
        """Newly constructed BenchQuality must default correct=False."""
        q = script_run_bench.BenchQuality()
        assert q.correct is False

    def test_defaults_optional_fields_none(self, script_run_bench: Any) -> None:
        """Optional numeric fields default to None when no score was computed."""
        q = script_run_bench.BenchQuality()
        assert q.recall is None
        assert q.metric_expected is None
        assert q.metric_got is None
        assert q.caller_count_gt is None
        assert q.evaluator_used is None
        assert q.evaluator_version is None
        assert q.extracted_metric is None

    def test_defaults_extraction_failed_false(self, script_run_bench: Any) -> None:
        """extraction_failed must default to False on an empty quality object."""
        q = script_run_bench.BenchQuality()
        assert q.extraction_failed is False

    def test_defaults_scoring_detail_empty_dict(self, script_run_bench: Any) -> None:
        """scoring_detail must be an empty dict (not shared mutable default)."""
        q1 = script_run_bench.BenchQuality()
        q2 = script_run_bench.BenchQuality()
        assert q1.scoring_detail == {}
        # mutable default isolation — distinct objects
        q1.scoring_detail["x"] = 1
        assert q2.scoring_detail == {}

    def test_explicit_fields_round_trip(self, script_run_bench: Any) -> None:
        """Explicitly set fields are stored and returned verbatim."""
        q = script_run_bench.BenchQuality(
            scored=True,
            correct=True,
            metric_expected=10,
            metric_got=9,
            recall=0.9,
            evaluator_used="_evaluate_fn",
        )
        assert q.scored is True
        assert q.correct is True
        assert q.metric_expected == 10
        assert q.metric_got == 9
        assert q.recall == pytest.approx(0.9)
        assert q.evaluator_used == "_evaluate_fn"


# ===========================================================================
# BenchRun dataclass
# ===========================================================================


class TestBenchRun:
    """Field defaults and type constraints for BenchRun."""

    def test_required_fields_stored(self, script_run_bench: Any) -> None:
        """Required constructor arguments are stored in matching fields."""
        r = _make_run(script_run_bench)
        assert r.arm == "plain"
        assert r.task_id == "SE-01"
        assert r.task_type == "symbol_extraction"
        assert r.model == "haiku"
        assert r.success is True

    def test_token_defaults_zero(self, script_run_bench: Any) -> None:
        """Token counters default to zero on a fresh run."""
        r = _make_run(script_run_bench)
        assert r.input_tokens == 0
        assert r.output_tokens == 0

    def test_tool_call_counters_default_zero(self, script_run_bench: Any) -> None:
        """All tool-call counters default to zero."""
        r = _make_run(script_run_bench)
        assert r.skill_calls == 0
        assert r.grep_calls == 0
        assert r.bash_calls == 0
        assert r.read_calls == 0
        assert r.scan_query_calls == 0

    def test_tool_log_independent_per_instance(self, script_run_bench: Any) -> None:
        """tool_log must be an independent list per BenchRun, not shared state."""
        first_run = _make_run(script_run_bench)
        second_run = _make_run(script_run_bench)
        first_run.tool_log.append("Bash: foo")
        assert second_run.tool_log == []

    def test_skill_counts_independent_per_instance(self, script_run_bench: Any) -> None:
        """skill_counts dict must be independent per instance (no shared mutable default)."""
        first_run = _make_run(script_run_bench)
        second_run = _make_run(script_run_bench)
        first_run.skill_counts["fix"] = 1
        assert "fix" not in second_run.skill_counts

    def test_quality_defaults_to_unscored(self, script_run_bench: Any) -> None:
        """Quality field must default to an unscored BenchQuality object."""
        r = _make_run(script_run_bench)
        assert isinstance(r.quality, script_run_bench.BenchQuality)
        assert r.quality.scored is False

    def test_patch_pass_defaults_none(self, script_run_bench: Any) -> None:
        """patch_pass must default to None (no signal) rather than False."""
        r = _make_run(script_run_bench)
        assert r.patch_pass is None

    def test_incomplete_defaults_false(self, script_run_bench: Any) -> None:
        """Incomplete flag must default to False."""
        r = _make_run(script_run_bench)
        assert r.incomplete is False

    def test_workflow_type_defaults_empty_string(self, script_run_bench: Any) -> None:
        """workflow_type defaults to empty string when not provided."""
        r = _make_run(script_run_bench)
        assert r.workflow_type == ""

    def test_elapsed_s_defaults_zero(self, script_run_bench: Any) -> None:
        """elapsed_s defaults to 0.0."""
        r = _make_run(script_run_bench)
        assert r.elapsed_s == pytest.approx(0.0)


# ===========================================================================
# _extract_int
# ===========================================================================


class TestExtractInt:
    """Regex-based integer extraction from free-form model output."""

    @pytest.mark.parametrize(
        "text,patterns,expected",
        [
            pytest.param("found 42 callers in total", [r"(\d+) caller"], 42, id="found-42-callers-in-total"),
            pytest.param("there are 7 unique callers", [r"(\d+) unique"], 7, id="there-are-7-unique-callers"),
            pytest.param(
                "UNIQUE CALLERS: 15", [r"(\d+) caller", r"unique callers:\s*(\d+)"], 15, id="unique-callers-15"
            ),
            # Bold markers are replaced by spaces; use whitespace-tolerant pattern.
            pytest.param("**42** callers", [r"(\d+)\s+caller"], 42, id="42-callers"),
            # Backticked value — inline-code markers replaced by spaces (same as bold).
            pytest.param("`25` undocumented functions", [r"(\d+)\s+undocumented"], 25, id="25-undocumented-functions"),
            pytest.param("total: 100", [r"total[:\s]+(\d+)"], 100, id="total-100"),
        ],
    )
    def test_extracts_integer_from_matching_pattern(
        self, script_run_bench: Any, text: str, patterns: list[str], expected: int
    ) -> None:
        """_extract_int returns the first integer matching any supplied pattern."""
        assert script_run_bench._extract_int(text, patterns) == expected

    @pytest.mark.parametrize(
        "text,patterns",
        [
            pytest.param("no numbers here", [r"(\d+) caller"], id="no-numbers-here"),
            pytest.param("", [r"(\d+) caller"], id="empty"),
            pytest.param("123", [], id="123"),  # no patterns → nothing matches
        ],
    )
    def test_returns_none_when_no_match(self, script_run_bench: Any, text: str, patterns: list[str]) -> None:
        """_extract_int returns None when no pattern matches the text."""
        assert script_run_bench._extract_int(text, patterns) is None

    def test_first_pattern_wins_over_later_match(self, script_run_bench: Any) -> None:
        """When multiple patterns match, the one listed first wins."""
        text = "12 unique callers, total: 99"
        result = script_run_bench._extract_int(text, [r"(\d+) unique", r"total[:\s]+(\d+)"])
        assert result == 12

    def test_bold_markers_stripped_before_matching(self, script_run_bench: Any) -> None:
        """Bold markers replaced by spaces; digit survives and can be matched."""
        # After stripping, "**7** unique" becomes " 7  unique" — use whitespace-tolerant pattern.
        text = "**7** unique callers"
        assert script_run_bench._extract_int(text, [r"(\d+)\s+unique"]) == 7


# ===========================================================================
# cost accounting — captured total_cost_usd
# ===========================================================================


class TestCostAccounting:
    """The $ metric comes from each run's captured total_cost_usd, not a local price table."""

    def test_cost_usd_defaults_to_zero(self, script_run_bench: Any) -> None:
        """A run with no captured cost carries cost_usd 0.0 (so the $ column is omitted)."""
        assert _make_run(script_run_bench).cost_usd == 0.0

    def test_cost_ratio_is_codemap_over_plain(self, script_run_bench: Any) -> None:
        """The ratio table's cost_ratio is the price-accurate codemap $ / plain $."""
        plain = _make_run(script_run_bench, arm="plain", task_id="SE-01", cost_usd=0.10)
        codemap = _make_run(script_run_bench, arm="codemap", task_id="SE-01", cost_usd=0.04)
        df = script_run_bench._token_ratio_table([plain, codemap])
        row = df[df["task_id"] == "SE-01"].iloc[0]
        assert row["cost_ratio"] == pytest.approx(0.4)

    def test_cost_ratio_nan_when_a_cost_is_missing(self, script_run_bench: Any) -> None:
        """A missing cost (0.0) yields NaN cost_ratio — never a bogus number from a zero denominator."""
        import math

        plain = _make_run(script_run_bench, arm="plain", task_id="SE-02", cost_usd=0.0)
        codemap = _make_run(script_run_bench, arm="codemap", task_id="SE-02", cost_usd=0.04)
        df = script_run_bench._token_ratio_table([plain, codemap])
        row = df[df["task_id"] == "SE-02"].iloc[0]
        assert math.isnan(row["cost_ratio"])


# ===========================================================================
# parity arm reporting — A_plain / B_auto / C_strict
# ===========================================================================


def _parity_runs(script_bench: Any) -> list[Any]:
    """Build one task's three canonical parity runs with distinct token counts."""
    return [
        _make_run(script_bench, arm="A_plain", task_id="FN-02", input_tokens=3_000_000, cost_usd=0.50),
        _make_run(script_bench, arm="B_auto", task_id="FN-02", input_tokens=1_200_000, cost_usd=0.25),
        _make_run(script_bench, arm="C_strict", task_id="FN-02", input_tokens=41_000, cost_usd=0.03),
    ]


class TestParityArmReporting:
    """Summaries must read the canonical parity arm names, not only the legacy plain/codemap pair."""

    def test_arm_pairs_compares_each_treatment_against_the_control(self, script_run_bench: Any) -> None:
        """Both treatment arms are paired with the single control arm.

        Collapsing B_auto and C_strict onto one key would drop whichever arm was recorded second, discarding a third of
        a paid parity run.
        """
        assert script_run_bench._arm_pairs(_parity_runs(script_run_bench)) == [
            ("A_plain", "C_strict"),
            ("A_plain", "B_auto"),
        ]

    def test_ratio_table_reads_parity_arm_names(self, script_run_bench: Any) -> None:
        """A parity run yields real token figures instead of the zeros a legacy-name lookup produced."""
        df = script_run_bench._token_ratio_table(_parity_runs(script_run_bench), "A_plain", "C_strict")
        row = df[df["task_id"] == "FN-02"].iloc[0]
        assert row["plain_tok"] == 3_000_000
        assert row["codemap_tok"] == 41_000
        assert row["ratio"] == pytest.approx(41_000 / 3_000_000)

    def test_paired_accuracy_reads_parity_arm_names(self, script_run_bench: Any) -> None:
        """The paired accuracy view pairs the named control and treatment arms."""
        runs = _parity_runs(script_run_bench)
        for run in runs:
            run.quality.scored = True
            run.quality.correct = run.arm != "A_plain"
        paired = script_run_bench._paired_accuracy(runs, "A_plain", "B_auto")
        assert paired == {"n": 1, "plain_correct": 0, "codemap_correct": 1}

    def test_summary_renders_a_section_per_treatment_arm(self, script_run_bench: Any, capsys: Any) -> None:
        """Every treatment arm gets its own labelled comparison against the control."""
        script_run_bench._print_summary(_parity_runs(script_run_bench), "haiku")
        out = capsys.readouterr().out
        assert "A_plain (control) vs C_strict (treatment)" in out
        assert "A_plain (control) vs B_auto (treatment)" in out
        assert "Token ratio (C_strict/A_plain)" in out
        assert "3,000,000" in out

    def test_summary_still_reads_the_legacy_arm_pair(self, script_run_bench: Any, capsys: Any) -> None:
        """Legacy plain/codemap results keep rendering, so older result files stay replayable."""
        runs = [
            _make_run(script_run_bench, arm="plain", task_id="SE-01", input_tokens=200_000),
            _make_run(script_run_bench, arm="codemap", task_id="SE-01", input_tokens=50_000),
        ]
        script_run_bench._print_summary(runs, "haiku")
        out = capsys.readouterr().out
        assert "plain (control) vs codemap (treatment)" in out
        assert "200,000" in out


# ===========================================================================
# DiffImpactStager — resilience
# ===========================================================================


class TestDiffImpactStagerResilience:
    """A failed stage must leave no residue: __enter__ reverts partial edits before raising."""

    @staticmethod
    def _git_repo(tmp_path: Any) -> Any:
        """Create a minimal committed git repo with two source files."""
        import subprocess

        repo = tmp_path / "repo"
        (repo / "pkg").mkdir(parents=True)
        (repo / "pkg" / "a.py").write_text("def foo():\n    return 1\n")
        (repo / "pkg" / "b.py").write_text("def bar():\n    return 2\n")
        for args in (
            ["init", "-q"],
            ["add", "-A"],
            ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        ):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        return repo

    def test_enter_reverts_partial_apply_on_missing_anchor(self, script_run_bench: Any, tmp_path: Any) -> None:
        """First edit applies, second edit's anchor is absent → raise AND revert the first (no residue)."""
        repo = self._git_repo(tmp_path)
        stage = [
            {"file": "pkg/a.py", "append": "\n# staged change\n"},
            {"file": "pkg/b.py", "find": "def does_not_exist(", "replace": "def x("},
        ]
        stager = script_run_bench.DiffImpactStager(repo, stage)
        with pytest.raises(script_run_bench.DirtyTreeError):
            stager.__enter__()
        # a.py was written by the first edit; __enter__ must have reverted it before propagating.
        assert (repo / "pkg" / "a.py").read_text() == "def foo():\n    return 1\n"

    def test_successful_revert_records_no_error(self, script_run_bench: Any, tmp_path: Any) -> None:
        """The ordinary path leaves the tree clean and the error slot empty."""
        repo = self._git_repo(tmp_path)
        stage = [{"file": "pkg/a.py", "append": "\n# staged change\n"}]

        with script_run_bench.DiffImpactStager(repo, stage) as stager:
            assert (repo / "pkg" / "a.py").read_text().endswith("# staged change\n")

        assert stager.revert_error is None
        assert (repo / "pkg" / "a.py").read_text() == "def foo():\n    return 1\n"

    def test_failed_revert_escalates_instead_of_silently_leaking(
        self, script_run_bench: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed revert leaves the shared tree mutated, so it must not pass silently.

        Before the fix the git result was discarded entirely: the staged synthetic change
        survived into every later task, which then saw an unexplained dirty tree far from
        the task that caused it.
        """
        import subprocess as real_subprocess

        repo = self._git_repo(tmp_path)
        stage = [{"file": "pkg/a.py", "append": "\n# staged change\n"}]
        real_run = real_subprocess.run

        def _run(command: list[str], **kwargs: Any) -> Any:
            """Supply scenario-specific subprocess outcomes at the worktree and test-command boundary."""
            if "checkout" in command:
                return SimpleNamespace(returncode=1, stderr="checkout refused", stdout="")
            return real_run(command, **kwargs)

        monkeypatch.setattr(script_run_bench.subprocess, "run", _run)

        with pytest.raises(script_run_bench.DirtyTreeError, match="still mutated"):
            with script_run_bench.DiffImpactStager(repo, stage):
                pass

    def test_failed_revert_does_not_mask_an_in_flight_exception(
        self, script_run_bench: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Escalation must never replace the exception carrying the real cause."""
        import subprocess as real_subprocess

        repo = self._git_repo(tmp_path)
        stage = [{"file": "pkg/a.py", "append": "\n# staged change\n"}]
        real_run = real_subprocess.run

        def _run(command: list[str], **kwargs: Any) -> Any:
            """Supply scenario-specific subprocess outcomes at the worktree and test-command boundary."""
            if "checkout" in command:
                return SimpleNamespace(returncode=1, stderr="checkout refused", stdout="")
            return real_run(command, **kwargs)

        monkeypatch.setattr(script_run_bench.subprocess, "run", _run)

        with pytest.raises(RuntimeError, match="original cause"):
            with script_run_bench.DiffImpactStager(repo, stage):
                raise RuntimeError("original cause")


# ===========================================================================
# PatchSandbox — baseline semantics, apply isolation, interpreter pinning
# ===========================================================================


class TestPatchSandboxExitCodes:
    """Pytest exit codes 2-5 carry no test result and must not score as a failed patch."""

    @staticmethod
    def _sandbox(script_run_bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, codes: list[int]) -> Any:
        """Build a PatchSandbox whose pytest invocations return ``codes`` in order."""
        remaining = list(codes)

        def _mkdtemp(*_args: Any, **_kwargs: Any) -> str:
            """Create a fixture-owned attempt directory for worktree lifecycle assertions."""
            root = tmp_path / f"attempt-{len(list(tmp_path.iterdir()))}"
            root.mkdir()
            return str(root)

        def _run(command: list[str], **_kwargs: Any) -> SimpleNamespace:
            """Supply scenario-specific subprocess outcomes at the worktree and test-command boundary."""
            if "worktree" in command and "add" in command:
                Path(command[-2]).mkdir()
            elif _is_pytest_argv(command):
                return SimpleNamespace(returncode=remaining.pop(0), stderr="boom", stdout="")
            elif "worktree" in command and "remove" in command:
                worktree = Path(command[-1])
                for path in worktree.iterdir():
                    path.unlink()
                worktree.rmdir()
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        monkeypatch.setattr(script_run_bench.tempfile, "mkdtemp", _mkdtemp)
        monkeypatch.setattr(script_run_bench.subprocess, "run", _run)
        return script_run_bench.PatchSandbox(
            tmp_path,
            {
                "id": "PT-fixture",
                "pre_fix_commit": "a" * 40,
                "test_command": "pytest tests/test_fixture.py::test_fix -x",
            },
        )

    @pytest.mark.parametrize("code", [2, 3, 4, 5])
    def test_baseline_non_result_exit_raises_sandbox_error(
        self, script_run_bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
    ) -> None:
        """A baseline that produced no test result is a sandbox error, not a failed patch.

        Exit 4 is the in-tree trigger: ``--timeout=60`` requires pytest-timeout, and
        without the plugin every patch task scored a silent zero.
        """
        sandbox = self._sandbox(script_run_bench, tmp_path, monkeypatch, [code])

        with pytest.raises(script_run_bench.SandboxError, match="baseline pytest exited"):
            sandbox.run("diff --git a/a.py b/a.py\n")

    def test_baseline_exit_one_is_a_valid_baseline_failure(
        self, script_run_bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exit 1 means the target test genuinely fails, so scoring proceeds."""
        sandbox = self._sandbox(script_run_bench, tmp_path, monkeypatch, [1, 0])

        assert sandbox.run("diff --git a/a.py b/a.py\n") is True

    def test_baseline_exit_zero_reports_an_already_passing_test(
        self, script_run_bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A test that already passes cannot validate a fix."""
        sandbox = self._sandbox(script_run_bench, tmp_path, monkeypatch, [0])

        assert sandbox.run("diff --git a/a.py b/a.py\n") is False

    def test_post_patch_non_result_exit_raises_sandbox_error(
        self, script_run_bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same rule applies after the patch — no result means no evidence."""
        sandbox = self._sandbox(script_run_bench, tmp_path, monkeypatch, [1, 4])

        with pytest.raises(script_run_bench.SandboxError, match="post-patch pytest exited"):
            sandbox.run("diff --git a/a.py b/a.py\n")

    def test_post_patch_exit_one_scores_a_failed_patch(
        self, script_run_bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exit 1 after patching is a genuine failed fix."""
        sandbox = self._sandbox(script_run_bench, tmp_path, monkeypatch, [1, 1])

        assert sandbox.run("diff --git a/a.py b/a.py\n") is False


class TestPatchSandboxApply:
    """The apply path must not leave a half-patched tree or harness residue."""

    def test_git_apply_runs_without_reject(
        self, script_run_bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Preserve a nonzero exit when patch rejection follows partial application.

        The fallback then re-applied the same file onto the already-mutated tree.
        """
        commands: list[list[str]] = []

        def _mkdtemp(*_args: Any, **_kwargs: Any) -> str:
            """Create a fixture-owned attempt directory for worktree lifecycle assertions."""
            root = tmp_path / "attempt"
            root.mkdir()
            return str(root)

        def _run(command: list[str], **_kwargs: Any) -> SimpleNamespace:
            """Supply scenario-specific subprocess outcomes at the worktree and test-command boundary."""
            commands.append(command)
            if "worktree" in command and "add" in command:
                Path(command[-2]).mkdir()
            elif _is_pytest_argv(command):
                return SimpleNamespace(returncode=1 if len(commands) < 3 else 0, stderr="", stdout="")
            elif "worktree" in command and "remove" in command:
                worktree = Path(command[-1])
                for path in worktree.iterdir():
                    path.unlink()
                worktree.rmdir()
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        monkeypatch.setattr(script_run_bench.tempfile, "mkdtemp", _mkdtemp)
        monkeypatch.setattr(script_run_bench.subprocess, "run", _run)
        sandbox = script_run_bench.PatchSandbox(
            tmp_path,
            {"id": "PT-fixture", "pre_fix_commit": "a" * 40, "failing_test": "tests/t.py::t"},
        )

        sandbox.run("diff --git a/a.py b/a.py\n")

        apply_commands = [command for command in commands if "apply" in command]
        assert apply_commands, "expected a git apply invocation"
        assert all("--reject" not in command for command in apply_commands)

    def test_tree_is_reset_before_the_fallback_apply(
        self, script_run_bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed ``git apply`` must not leave hunks behind for ``patch -p1``."""
        commands: list[list[str]] = []

        def _mkdtemp(*_args: Any, **_kwargs: Any) -> str:
            """Create a fixture-owned attempt directory for worktree lifecycle assertions."""
            root = tmp_path / "attempt"
            root.mkdir()
            return str(root)

        def _run(command: list[str], **_kwargs: Any) -> SimpleNamespace:
            """Supply scenario-specific subprocess outcomes at the worktree and test-command boundary."""
            commands.append(command)
            if "worktree" in command and "add" in command:
                Path(command[-2]).mkdir()
                return SimpleNamespace(returncode=0, stderr="", stdout="")
            if _is_pytest_argv(command):
                return SimpleNamespace(returncode=1 if len(commands) < 3 else 0, stderr="", stdout="")
            if "apply" in command:
                return SimpleNamespace(returncode=1, stderr="hunk rejected", stdout="")
            if "worktree" in command and "remove" in command:
                worktree = Path(command[-1])
                for path in worktree.iterdir():
                    path.unlink()
                worktree.rmdir()
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        monkeypatch.setattr(script_run_bench.tempfile, "mkdtemp", _mkdtemp)
        monkeypatch.setattr(script_run_bench.subprocess, "run", _run)
        sandbox = script_run_bench.PatchSandbox(
            tmp_path,
            {"id": "PT-fixture", "pre_fix_commit": "a" * 40, "failing_test": "tests/t.py::t"},
        )

        sandbox.run("diff --git a/a.py b/a.py\n")

        checkout_index = next(
            index for index, command in enumerate(commands) if "checkout" in command and "--" in command
        )
        fallback_index = next(index for index, command in enumerate(commands) if command[:1] == ["patch"])
        assert checkout_index < fallback_index

    def test_patch_artifacts_are_removed_before_the_scored_run(
        self, script_run_bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The diff file and any ``.rej`` residue must not survive into the scored test."""
        seen_during_test: list[list[str]] = []

        def _mkdtemp(*_args: Any, **_kwargs: Any) -> str:
            """Create a fixture-owned attempt directory for worktree lifecycle assertions."""
            root = tmp_path / "attempt"
            root.mkdir()
            return str(root)

        def _run(command: list[str], **_kwargs: Any) -> SimpleNamespace:
            """Supply scenario-specific subprocess outcomes at the worktree and test-command boundary."""
            if "worktree" in command and "add" in command:
                worktree = Path(command[-2])
                worktree.mkdir()
                return SimpleNamespace(returncode=0, stderr="", stdout="")
            if _is_pytest_argv(command):
                worktree = tmp_path / "attempt" / "repo"
                seen_during_test.append(sorted(path.name for path in worktree.iterdir()))
                return SimpleNamespace(returncode=1 if len(seen_during_test) == 1 else 0, stderr="", stdout="")
            if "apply" in command:
                worktree = tmp_path / "attempt" / "repo"
                (worktree / "a.py.rej").write_text("rejected hunk")
                return SimpleNamespace(returncode=0, stderr="", stdout="")
            if "worktree" in command and "remove" in command:
                worktree = Path(command[-1])
                for path in worktree.iterdir():
                    path.unlink()
                worktree.rmdir()
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        monkeypatch.setattr(script_run_bench.tempfile, "mkdtemp", _mkdtemp)
        monkeypatch.setattr(script_run_bench.subprocess, "run", _run)
        sandbox = script_run_bench.PatchSandbox(
            tmp_path,
            {"id": "PT-fixture", "pre_fix_commit": "a" * 40, "failing_test": "tests/t.py::t"},
        )

        sandbox.run("diff --git a/a.py b/a.py\n")

        assert ".patch-bench.diff" not in seen_during_test[-1]
        assert "a.py.rej" not in seen_during_test[-1]


class TestPytestInterpreterPinning:
    """All lanes must resolve pytest through one interpreter."""

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["pytest", "tests/t.py", "-x"], id="bare-pytest"),
            pytest.param(["py.test", "tests/t.py"], id="legacy-alias"),
            pytest.param(["/usr/local/bin/pytest", "tests/t.py"], id="absolute-path"),
        ],
    )
    def test_bare_pytest_is_pinned_to_the_running_interpreter(self, script_run_bench: Any, argv: list[str]) -> None:
        """A PATH-resolved pytest can select a different interpreter than the harness."""
        assert script_run_bench._pin_pytest_interpreter(argv)[:3] == [sys.executable, "-m", "pytest"]

    def test_a_non_pytest_command_is_left_untouched(self, script_run_bench: Any) -> None:
        """Only pytest invocations are rewritten."""
        argv = ["tox", "-e", "py311"]

        assert script_run_bench._pin_pytest_interpreter(argv) == argv

    def test_failing_test_argv_uses_the_pinned_interpreter(self, script_run_bench: Any) -> None:
        """The ``failing_test`` path builds its argv from the running interpreter too."""
        sandbox = script_run_bench.PatchSandbox(
            Path("/repo"),
            {"id": "PT-fixture", "pre_fix_commit": "a" * 40, "failing_test": "tests/t.py::t"},
        )

        assert sandbox._test_argv() == [sys.executable, "-m", "pytest", "tests/t.py::t", "-x"]


# ===========================================================================
# _int_close
# ===========================================================================


class TestIntClose:
    """Tolerance-based integer comparison helper."""

    @pytest.mark.parametrize(
        "got,expected,tolerance,result",
        [
            pytest.param(42, 40, 0.10, True, id="42-40"),  # within 10%
            pytest.param(44, 40, 0.10, True, id="44"),  # exactly at boundary: 4/40 = 0.10
            pytest.param(45, 40, 0.10, False, id="45"),  # just over boundary: 5/40 = 0.125
            pytest.param(40, 40, 0.10, True, id="40"),  # exact match
            pytest.param(0, 40, 0.10, False, id="0"),  # far below
            pytest.param(42, 30, 0.10, False, id="42-30"),  # 12/30 = 0.40
            pytest.param(1, 1, 0.0, True, id="1"),  # zero tolerance exact
            pytest.param(2, 1, 0.0, False, id="2"),  # zero tolerance mismatch
        ],
    )
    def test_within_tolerance(
        self, script_run_bench: Any, got: int, expected: int, tolerance: float, result: bool
    ) -> None:
        """_int_close returns True iff abs deviation is within fractional tolerance."""
        assert script_run_bench._int_close(got, expected, tolerance=tolerance) is result

    def test_none_always_returns_false(self, script_run_bench: Any) -> None:
        """_int_close returns False when got is None regardless of expected."""
        assert script_run_bench._int_close(None, 40) is False
        assert script_run_bench._int_close(None, 0) is False

    def test_denominator_floor_at_1(self, script_run_bench: Any) -> None:
        """When expected=0, denominator is clamped to 1 to avoid ZeroDivisionError."""
        # abs(1 - 0) / max(0, 1) = 1.0 → outside 10% → False
        assert script_run_bench._int_close(1, 0, tolerance=0.10) is False
        # got == expected == 0 → diff = 0 → True
        assert script_run_bench._int_close(0, 0, tolerance=0.10) is True


# ===========================================================================
# _count_tol_detail
# ===========================================================================


class TestCountTolDetail:
    """Scoring-detail dict builder for count-tolerance evaluators."""

    def test_required_fields_present(self, script_run_bench: Any) -> None:
        """Result contains metric_expected, metric_got, threshold, and method."""
        d = script_run_bench._count_tol_detail(10, 9)
        assert d["metric_expected"] == 10
        assert d["metric_got"] == 9
        assert d["threshold"] == pytest.approx(0.10)
        assert d["method"] == "count_tolerance"

    def test_extra_kwargs_merged(self, script_run_bench: Any) -> None:
        """Extra keyword arguments are merged into the result dict."""
        d = script_run_bench._count_tol_detail(10, 9, check="coupled")
        assert d["check"] == "coupled"

    def test_extra_kwargs_do_not_overwrite_fixed_keys(self, script_run_bench: Any) -> None:
        """Core keys always reflect the positional arguments, not extras."""
        d = script_run_bench._count_tol_detail(5, 3)
        assert d["metric_expected"] == 5
        assert d["metric_got"] == 3

    def test_none_values_accepted(self, script_run_bench: Any) -> None:
        """None values for got are stored without error (extraction failed case)."""
        d = script_run_bench._count_tol_detail(10, None)
        assert d["metric_got"] is None


# ===========================================================================
# _safe_ratio
# ===========================================================================


class TestSafeRatio:
    """Division helper that returns NaN for undefined denominators."""

    @pytest.mark.parametrize(
        "num,den,expected",
        [
            pytest.param(10, 4, 2.5, id="10"),
            pytest.param(0, 5, 0.0, id="0"),
            pytest.param(1, 1, 1.0, id="1"),
            pytest.param(100, 100, 1.0, id="100"),
        ],
    )
    def test_normal_division(self, script_run_bench: Any, num: float, den: float, expected: float) -> None:
        """_safe_ratio performs ordinary division when denominator is non-zero."""
        assert script_run_bench._safe_ratio(num, den) == pytest.approx(expected)

    @pytest.mark.parametrize(
        "num,den",
        [
            pytest.param(10, 0, id="10-0"),
            pytest.param(0, 0, id="0"),
            pytest.param(10, None, id="10-none"),
            pytest.param(None, 4, id="none-4"),
            pytest.param(None, None, id="none-none"),
        ],
    )
    def test_returns_nan_for_undefined(self, script_run_bench: Any, num: Any, den: Any) -> None:
        """_safe_ratio returns NaN when denominator is zero or either operand is None."""
        assert math.isnan(script_run_bench._safe_ratio(num, den))


# ===========================================================================
# _parse_scan_query_subcommand
# ===========================================================================


class TestParseScanQuerySubcommand:
    """Extract scan-query subcommand from Bash command strings."""

    @pytest.mark.parametrize(
        "command,expected",
        [
            pytest.param(
                "scan-query --index /x.json fn-rdeps a.b --exclude-tests",
                "fn-rdeps",
                id="scan-query---index-x.json-fn-rdeps-a.b---exclude-tests",
            ),
            pytest.param("scan-query symbol Trainer", "symbol", id="scan-query-symbol-trainer"),
            pytest.param("scan-query find-symbol Trainer", "find-symbol", id="scan-query-find-symbol-trainer"),
            pytest.param(
                "scan-query symbols lightning.pytorch.trainer",
                "symbols",
                id="scan-query-symbols-lightning.pytorch.trainer",
            ),
            pytest.param(
                "scan-query rdeps lightning.pytorch.loops", "rdeps", id="scan-query-rdeps-lightning.pytorch.loops"
            ),
            pytest.param(
                "scan-query undocumented lightning.pytorch.trainer",
                "undocumented",
                id="scan-query-undocumented-lightning.pytorch.trainer",
            ),
            pytest.param(
                "scan-query uncovered lightning.pytorch.trainer --top 10",
                "uncovered",
                id="scan-query-uncovered-lightning.pytorch.trainer---top-10",
            ),
            pytest.param("scan-query coupled --top 5", "coupled", id="scan-query-coupled---top-5"),
            pytest.param(
                "scan-query xrefs lightning.pytorch.trainer", "xrefs", id="scan-query-xrefs-lightning.pytorch.trainer"
            ),
            pytest.param(
                "/path/to/bin/scan-query symbol Trainer", "symbol", id="path-to-bin-scan-query-symbol-trainer"
            ),  # full path form
        ],
    )
    def test_known_subcommands_extracted(self, script_run_bench: Any, command: str, expected: str) -> None:
        """_parse_scan_query_subcommand returns the recognised subcommand token."""
        assert script_run_bench._parse_scan_query_subcommand(command) == expected

    @pytest.mark.parametrize("command", ["grep -r foo .", "ls -la", "python3 -m pytest", ""])
    def test_non_scan_query_command_returns_none(self, script_run_bench: Any, command: str) -> None:
        """_parse_scan_query_subcommand returns None for commands without scan-query."""
        assert script_run_bench._parse_scan_query_subcommand(command) is None

    def test_unknown_subcommand_returns_none(self, script_run_bench: Any) -> None:
        """Unrecognised subcommands (not in the known set) return None."""
        assert script_run_bench._parse_scan_query_subcommand("scan-query unknown-subcmd foo") is None

    def test_index_flag_value_skipped(self, script_run_bench: Any) -> None:
        """Verify command-line option behavior.

        ``--index <path>`` flag-value pair is skipped before finding the subcommand.
        """
        cmd = "scan-query --index /some/path/index.json fn-rdeps lightning.pytorch.trainer"
        assert script_run_bench._parse_scan_query_subcommand(cmd) == "fn-rdeps"

    def test_equals_form_flag_skipped(self, script_run_bench: Any) -> None:
        """Verify command-line option behavior.

        ``--index=/path`` form (= present) is treated as a single token and skipped.
        """
        cmd = "scan-query --index=/some/path.json symbol Trainer"
        assert script_run_bench._parse_scan_query_subcommand(cmd) == "symbol"

    @pytest.mark.parametrize(
        "command,expected",
        [
            pytest.param(
                'scan-query --index "/tmp/index with spaces.json" symbol Trainer',
                "symbol",
                id="scan-query---index-tmp-index-with-spaces.json-symbol-trainer",
            ),
            pytest.param(
                "scan-query --index /tmp/one.json --index /tmp/two.json rdeps pkg.mod",
                "rdeps",
                id="scan-query---index-tmp-one.json---index-tmp-two.json-rdeps-pkg.mod",
            ),
            pytest.param(
                "env CODEMAP=1 scan-query --index /tmp/index.json xrefs pkg.mod",
                "xrefs",
                id="env-codemap-1-scan-query---index-tmp-index.json-xrefs-pkg.mod",
            ),
        ],
    )
    def test_shell_token_edge_cases(self, script_run_bench: Any, command: str, expected: str) -> None:
        """Quoted paths, repeated index flags, and env prefixes still expose the subcommand."""
        assert script_run_bench._parse_scan_query_subcommand(command) == expected

    @pytest.mark.parametrize(
        "command",
        [
            "echo scan-query symbol Trainer",
            "python -c 'print(\"scan-query symbol Trainer\")'",
            "scan-query --unknown value symbol Trainer",
            "scan-query --unknown=value symbol Trainer",
        ],
    )
    def test_non_invocations_and_unknown_global_flags_return_none(self, script_run_bench: Any, command: str) -> None:
        """Scan-query mentioned as data or with unknown global flags is not credited as a query."""
        assert script_run_bench._parse_scan_query_subcommand(command) is None


# ===========================================================================
# _normalize_external_task
# ===========================================================================


class TestNormalizeExternalTask:
    """Schema normalizer for tasks loaded via ``--tasks-file``."""

    def test_queries_renamed_to_expected_queries(self, script_run_bench: Any) -> None:
        """'queries' key is renamed to 'expected_queries' and original key removed."""
        task = {
            "id": "B-01",
            "prompt": "p",
            "skill": "fix",
            "queries": [{"cmd": "rdeps", "args": ["m"]}],
        }
        result = script_run_bench._normalize_external_task(task)
        assert "expected_queries" in result
        assert "queries" not in result
        assert result["expected_queries"] == [{"cmd": "rdeps", "args": ["m"]}]

    def test_type_defaults_to_develop_skill_when_missing(self, script_run_bench: Any) -> None:
        """Tasks without a 'type' field get type=develop_skill."""
        task = {"id": "B-01", "prompt": "p", "skill": "fix"}
        result = script_run_bench._normalize_external_task(task)
        assert result["type"] == script_run_bench._EXTERNAL_TASK_TYPE

    def test_scoreable_false_when_no_ground_truth(self, script_run_bench: Any) -> None:
        """Tasks without ground_truth are forced scoreable=False."""
        task = {"id": "B-01", "prompt": "p", "skill": "fix"}
        result = script_run_bench._normalize_external_task(task)
        assert result["scoreable"] is False

    def test_scoreable_true_preserved_when_ground_truth_present(self, script_run_bench: Any) -> None:
        """Tasks with ground_truth and explicit scoreable=True keep that flag."""
        task = {
            "id": "DBG-01",
            "type": "debug_from_trace",
            "scoreable": True,
            "prompt": "p",
            "ground_truth": {"function": "f", "file": "a.py", "start_line": 1},
        }
        result = script_run_bench._normalize_external_task(task)
        assert result["scoreable"] is True

    def test_scoreable_defaults_true_when_ground_truth_present_but_no_flag(self, script_run_bench: Any) -> None:
        """ground_truth present without explicit scoreable → defaults to True."""
        task = {
            "id": "DBG-02",
            "type": "debug_from_trace",
            "prompt": "p",
            "ground_truth": {"function": "g", "file": "b.py", "start_line": 5},
        }
        result = script_run_bench._normalize_external_task(task)
        assert result["scoreable"] is True

    def test_existing_type_preserved(self, script_run_bench: Any) -> None:
        """Tasks with an existing 'type' field keep that type unchanged."""
        task = {
            "id": "DBG-01",
            "type": "debug_from_trace",
            "prompt": "p",
            "ground_truth": {"function": "f", "file": "a.py", "start_line": 1},
        }
        result = script_run_bench._normalize_external_task(task)
        assert result["type"] == "debug_from_trace"

    def test_original_task_dict_not_mutated(self, script_run_bench: Any) -> None:
        """_normalize_external_task must not mutate the input dict."""
        task = {"id": "B-01", "prompt": "p", "queries": [{"cmd": "rdeps"}]}
        original = dict(task)
        script_run_bench._normalize_external_task(task)
        assert task == original


# ===========================================================================
# _load_tasks_file
# ===========================================================================


class TestLoadTasksFile:
    """JSON task file loader — parsing, normalization, and error paths."""

    def _write_json(self, tmp_path: Path, data: Any) -> Path:
        """Write data as JSON to a temp file and return its path."""
        p = tmp_path / "tasks.json"
        p.write_text(json.dumps(data))
        return p

    def test_bare_list_loaded_and_normalized(self, script_run_bench: Any, tmp_path: Path) -> None:
        """A bare JSON list of tasks is loaded and each task normalized."""
        data = [{"id": "X-01", "prompt": "p", "skill": "fix", "queries": [{"cmd": "rdeps"}]}]
        p = self._write_json(tmp_path, data)
        result = script_run_bench._load_tasks_file(p)
        assert len(result) == 1
        assert result[0]["id"] == "X-01"
        assert "expected_queries" in result[0]

    def test_dict_with_tasks_key_loaded(self, script_run_bench: Any, tmp_path: Path) -> None:
        """A {'repo': ..., 'tasks': [...]} shaped file extracts the 'tasks' list."""
        data = {
            "repo": {"name": "myrepo"},
            "tasks": [{"id": "Y-01", "prompt": "p", "skill": "fix"}],
        }
        p = self._write_json(tmp_path, data)
        result = script_run_bench._load_tasks_file(p)
        assert len(result) == 1
        assert result[0]["id"] == "Y-01"

    def test_missing_file_raises_file_not_found(self, script_run_bench: Any, tmp_path: Path) -> None:
        """FileNotFoundError raised when the file does not exist."""
        with pytest.raises(FileNotFoundError, match="not found"):
            script_run_bench._load_tasks_file(tmp_path / "nonexistent.json")

    def test_malformed_json_raises_value_error(self, script_run_bench: Any, tmp_path: Path) -> None:
        """ValueError raised when the file contains invalid JSON."""
        p = tmp_path / "bad.json"
        p.write_text("{not valid json")
        with pytest.raises(ValueError, match="not valid JSON"):
            script_run_bench._load_tasks_file(p)

    def test_unexpected_shape_raises_value_error(self, script_run_bench: Any, tmp_path: Path) -> None:
        """ValueError raised when JSON is neither a list nor a dict."""
        p = tmp_path / "bad.json"
        p.write_text("42")
        with pytest.raises(ValueError, match="must be a JSON list or object"):
            script_run_bench._load_tasks_file(p)

    def test_empty_list_returns_empty(self, script_run_bench: Any, tmp_path: Path) -> None:
        """Load an empty task list without error."""
        p = self._write_json(tmp_path, [])
        result = script_run_bench._load_tasks_file(p)
        assert result == []

    def test_multiple_tasks_all_normalized(self, script_run_bench: Any, tmp_path: Path) -> None:
        """All tasks in the list are normalized, not just the first."""
        data = [
            {"id": "A-01", "prompt": "p", "queries": [{"cmd": "rdeps"}]},
            {"id": "A-02", "prompt": "p", "queries": [{"cmd": "symbol"}]},
        ]
        p = self._write_json(tmp_path, data)
        result = script_run_bench._load_tasks_file(p)
        assert all("expected_queries" in t for t in result)
        assert all("queries" not in t for t in result)


# ===========================================================================
# _evaluate_symbol
# ===========================================================================


class TestEvaluateSymbol:
    """symbol_extraction evaluator: start_line within ±5 lines of ground truth."""

    @pytest.mark.parametrize(
        "output_text,start_line,expected_correct",
        [
            pytest.param(
                "file_path: x.py  start_line: 100  end_line: 110",
                100,
                True,
                id="file_path-x.py-start_line-100-end_line-110",
            ),  # exact
            pytest.param("start_line: 104", 100, True, id="start_line-104"),  # within +4 lines
            pytest.param("start_line: 96", 100, True, id="start_line-96"),  # within -4 lines
            pytest.param("start_line: 105", 100, True, id="start_line-105"),  # exactly ±5 boundary
            pytest.param("start_line: 95", 100, True, id="start_line-95"),  # exactly -5 boundary
            pytest.param("start_line: 106", 100, False, id="start_line-106"),  # just outside +5
            pytest.param("start_line: 94", 100, False, id="start_line-94"),  # just outside -5
            pytest.param(
                "Lines 100-110 of the file", 100, True, id="lines-100-110-of-the-file"
            ),  # range pattern fallback
            pytest.param(
                "src/lightning/trainer/trainer.py:100-110", 100, True, id="src-lightning-trainer-trainer.py-100-110"
            ),  # source-location range
            pytest.param(
                "starts at line 100 in the file", 100, True, id="starts-at-line-100-in-the-file"
            ),  # "starts at line N"
            pytest.param(
                "start_line: `100`", 100, True, id="start_line-100"
            ),  # backticked value (markdown inline code)
            pytest.param(
                "file_path: `x.py`  start_line: `104`  end_line: `110`",
                100,
                True,
                id="file_path-x.py-start_line-104-end_line-110",
            ),  # full backticked format
        ],
    )
    def test_correct_when_start_line_within_tolerance(
        self, script_run_bench: Any, output_text: str, start_line: int, expected_correct: bool
    ) -> None:
        """_evaluate_symbol marks correct when extracted start_line is within ±5 of GT."""
        task = _se_task(start_line=start_line)
        result = script_run_bench._evaluate_symbol(task, output_text)
        assert result.scored is True
        assert result.correct is expected_correct

    def test_extraction_failed_when_no_line_in_output(self, script_run_bench: Any) -> None:
        """Mark extraction as failed and incorrect when no start line can be parsed."""
        task = _se_task(start_line=100)
        result = script_run_bench._evaluate_symbol(task, "The function does something useful.")
        assert result.scored is True
        assert result.extraction_failed is True
        assert result.correct is False

    def test_bold_markers_stripped_before_parsing(self, script_run_bench: Any) -> None:
        """Markdown bold markers around start_line are ignored by the parser."""
        task = _se_task(start_line=200)
        result = script_run_bench._evaluate_symbol(task, "**start_line**: 200")
        assert result.correct is True

    def test_backtick_wrapped_value_parsed_not_failed(self, script_run_bench: Any) -> None:
        """Regression: a backtick between the colon and the digit must not defeat extraction.

        Real observed output (SE-05) was ``start_line: `213` `` — the backtick left the digit one char past ``[:\\s]+``
        and every pattern missed it, scoring !parse. Inline-code markers are now stripped alongside bold, so the value
        parses.
        """
        task = _se_task(start_line=213)
        result = script_run_bench._evaluate_symbol(
            task, "file_path: `src/x.py`  \nstart_line: `213`  \nend_line: `222`"
        )
        assert result.extraction_failed is False
        assert result.correct is True

    def test_metric_expected_set_to_ground_truth_start(self, script_run_bench: Any) -> None:
        """metric_expected is always the ground-truth start_line value."""
        task = _se_task(start_line=42)
        result = script_run_bench._evaluate_symbol(task, "start_line: 42")
        assert result.metric_expected == 42

    def test_evaluator_name_recorded(self, script_run_bench: Any) -> None:
        """evaluator_used field identifies the function that ran."""
        task = _se_task()
        result = script_run_bench._evaluate_symbol(task, "start_line: 100")
        assert result.evaluator_used == "_evaluate_symbol"

    def test_scoring_detail_contains_threshold(self, script_run_bench: Any) -> None:
        """scoring_detail carries threshold and method for diagnosis."""
        task = _se_task(start_line=50)
        result = script_run_bench._evaluate_symbol(task, "start_line: 50")
        assert result.scoring_detail["threshold"] == 5
        assert result.scoring_detail["method"] == "line_tolerance"

    def test_source_location_range_does_not_need_generic_line_prose(self, script_run_bench: Any) -> None:
        """A conventional ``path.py:start-end`` answer remains scoreable.

        Prevents a valid compact symbol answer from being marked wrong merely because it omits the redundant word
        ``Lines``.
        """
        task = _se_task(start_line=1110)
        result = script_run_bench._evaluate_symbol(
            task, "file_path: src/lightning/trainer/trainer.py:1110\N{EN DASH}1125"
        )
        assert result.correct is True


# ===========================================================================
# ===========================================================================
# _evaluate_rv (review_assistance)
# ===========================================================================


class TestEvaluateRv:
    """review_assistance evaluator: count-tolerance or symbol-recall path."""

    def _rv_task_count(self, count: int) -> dict:
        """Return a review_assistance task graded by count extraction."""
        return {
            "id": "RV-01",
            "type": "review_assistance",
            "sub_questions": [{"id": "q1", "match": "integer_extract", "ground_truth": {"count": count}}],
        }

    def _rv_task_symbols(self, symbols: list[str]) -> dict:
        """Return a review_assistance task graded by symbol recall."""
        return {
            "id": "RV-05",
            "type": "review_assistance",
            "sub_questions": [{"id": "q1", "match": "symbol_name_set", "ground_truth": {"symbols": symbols}}],
        }

    def test_no_sub_questions_returns_unscored(self, script_run_bench: Any) -> None:
        """A task with no sub_questions cannot be scored."""
        task = {"id": "RV-01", "type": "review_assistance", "sub_questions": []}
        result = script_run_bench._evaluate_rv(task, "something")
        assert result.scored is False

    def test_count_path_correct_within_tolerance(self, script_run_bench: Any) -> None:
        """Count path: correct when extracted count is within 10% of expected."""
        task = self._rv_task_count(20)
        result = script_run_bench._evaluate_rv(task, "found 20 undocumented symbols")
        assert result.scored is True
        assert result.correct is True

    def test_count_path_incorrect_outside_tolerance(self, script_run_bench: Any) -> None:
        """Count path: incorrect when extracted count is outside 10%."""
        task = self._rv_task_count(20)
        result = script_run_bench._evaluate_rv(task, "found 30 undocumented symbols")
        assert result.correct is False

    def test_count_path_prefers_answer_region_over_stray_prose(self, script_run_bench: Any) -> None:
        """Count path: a stray count in exploration must not outrank the answer-region count.

        Regression for the RV-02 false-fail: exploration said "0 symbols of its own" while the
        conclusion said "65 importers" (expected 64). First-match-anywhere extraction grabbed the
        0 (recall 0.000); answer-region scoping must instead score the conclusion's 65 as correct.
        """
        task = self._rv_task_count(64)
        text = (
            "Exploring the module — it has 0 symbols of its own but is widely imported.\n\n"
            "## Answer\n"
            "65 importers depend on this module.\n"
        )
        result = script_run_bench._evaluate_rv(task, text)
        assert result.metric_got == 65
        assert result.correct is True
        assert result.extraction_failed is False

    @pytest.mark.parametrize(
        "answer",
        [
            "I’ll inspect the repository with ordinary file-search tools and count module files "
            "containing direct imports of `lightning.pytorch.utilities.rank_zero`.64 modules "
            "directly import `lightning.pytorch.utilities.rank_zero` (60 source modules and 4 "
            "test modules).",
            "I’ll scan the repository with ordinary text search only, identify files that import "
            "`lightning.pytorch.utilities.rank_zero` directly, and count distinct modules.60 production "
            "modules directly import `lightning.pytorch.utilities.rank_zero` (64 modules including 4 test modules).",
            "I’ll verify this through Codemap’s native CLI, then cross-check the resulting import "
            "count in the repository.The relevant Codemap query is reverse dependencies (`rdeps`) "
            "for `lightning.pytorch.utilities.rank_zero`; I’m running the required compact query "
            "now.64 modules directly import `lightning.pytorch.utilities.rank_zero`.",
            "I’m using the installed `codemap-py:query-code` skill as required. I’ll activate it in "
            "one dedicated shell item, then run the single compact reverse-dependency query.1. "
            "**64 modules** directly import `lightning.pytorch.utilities.rank_zero`.",
            "64 modules directly depend on `lightning.pytorch.utilities.rank_zero`.",
            "64 modules import `lightning.pytorch.utilities.rank_zero`.",
        ],
    )
    def test_rv02_exact_provider_answers_extract_direct_import_count(self, script_run_bench: Any, answer: str) -> None:
        """Immutable RV-02 A/B/C answers must parse their directly-imported count."""
        result = script_run_bench._evaluate_rv(self._rv_task_count(64), answer)

        assert result.metric_got == 64
        assert result.correct is True
        assert result.extraction_failed is False

    def test_rv05_exact_partial_answer_scores_count_and_symbol_components_separately(
        self, script_run_bench: Any
    ) -> None:
        """RV-05 keeps correct count credit while retaining its 2/5 symbol recall loss."""
        task = {
            "id": "RV-05",
            "type": "review_assistance",
            "sub_questions": [
                {"id": "q1", "match": "integer_extract", "ground_truth": {"count": 11}},
                {
                    "id": "q2",
                    "match": "symbol_name_set",
                    "ground_truth": {
                        "symbols": [
                            "LayerSummary",
                            "LayerSummary.detach_hook",
                            "LayerSummary.in_size",
                            "LayerSummary.layer_type",
                            "LayerSummary.num_parameters",
                        ]
                    },
                },
            ],
        }
        answer = (
            "I’m checking the module’s actual test references against the benchmark’s AST-based "
            "definition, while also completing the required compact native Codemap query.The required "
            "compact query completed successfully (`query_complete: true`, `compact: true`). Its "
            "25-item static list is not the benchmark oracle, so I’m now inspecting declarations and "
            "all test AST identifiers/string patch targets directly.1. **11** unique public symbols "
            "are uncovered under the independent AST oracle.\n\n2. The lexicographically first five "
            "are:\n\n   1. `LayerSummary`\n   2. `detach_hook`\n   3. `flop_counts`\n   4. "
            "`get_formatted_model_size`\n   5. `get_human_readable_count`"
        )

        result = script_run_bench._evaluate_rv(task, answer)

        assert result.metric_got == 11
        assert result.extracted_metric["q1.count"] == 11
        assert result.extracted_metric["q2.symbols"] == 2
        assert result.scoring_detail["components"]["q1.count"]["extraction_failed"] is False
        assert result.scoring_detail["components"]["q2.symbols"]["extraction_failed"] is False
        assert result.correct is False
        assert result.recall == pytest.approx(0.7, abs=1e-9)
        assert result.evaluator_version == "v8"

    def test_rv04_counts_production_functions_with_a_trailing_qualifier(self, script_run_bench: Any) -> None:
        """A correct count is credited when the qualifier follows the noun instead of preceding it."""
        answer = "1. **24** production functions uniquely call `lightning.pytorch.trainer.call::_call_callback_hooks`."

        result = script_run_bench._evaluate_rv(self._rv_task_count(24), answer)

        assert result.metric_got == 24
        assert result.extraction_failed is False
        assert result.correct is True

    def test_bare_numbered_sub_answer_supplies_the_count(self, script_run_bench: Any) -> None:
        """An enumerated sub-answer carrying the number alone is still an extractable count."""
        answer = "Checked the oracle directly.\n1. **11**\n\n2. Lexicographically first five uncovered symbols:\n"

        result = script_run_bench._evaluate_rv(self._rv_task_count(11), answer)

        assert result.metric_got == 11
        assert result.extraction_failed is False

    def test_phrased_count_outranks_an_enumerated_step_number(self, script_run_bench: Any) -> None:
        """A noun-anchored count wins over a leading integer on an enumerated exploration line."""
        answer = "1. 3 candidate files inspected.\nThe module has 20 undocumented symbols."

        result = script_run_bench._evaluate_rv(self._rv_task_count(20), answer)

        assert result.metric_got == 20
        assert result.correct is True

    def test_symbol_recall_path_correct_at_threshold(self, script_run_bench: Any) -> None:
        """Symbol-recall path: correct when ≥70% of expected symbols appear in output."""
        syms = [f"lightning.pytorch.mod::Cls.method{i}" for i in range(10)]
        task = self._rv_task_symbols(syms)
        # Include 7 of 10 symbols by their short name (last component after split)
        text = " ".join(s.split(".")[-1] for s in syms[:7])
        result = script_run_bench._evaluate_rv(task, text)
        assert result.scored is True
        assert result.correct is True
        assert result.recall is not None
        assert result.recall >= 0.70

    def test_symbol_recall_path_incorrect_below_threshold(self, script_run_bench: Any) -> None:
        """Symbol-recall path: incorrect when fewer than 70% of symbols found."""
        syms = [f"lightning.pytorch.mod::Cls.method{i}" for i in range(10)]
        task = self._rv_task_symbols(syms)
        # Only 5 of 10 symbols present → recall = 0.50 < 0.70 → incorrect
        text = " ".join(f"method{i}" for i in range(5))
        result = script_run_bench._evaluate_rv(task, text)
        assert result.correct is False
        assert result.recall is not None and result.recall < 0.70

    def test_evaluator_name_recorded(self, script_run_bench: Any) -> None:
        """evaluator_used is always _evaluate_rv."""
        task = self._rv_task_count(5)
        result = script_run_bench._evaluate_rv(task, "found 5 undocumented")
        assert result.evaluator_used == "_evaluate_rv"

    def test_symbol_path_extraction_failed_when_no_symbol_found(self, script_run_bench: Any) -> None:
        """Symbol path: no extractable symbol → extraction_failed=True (excluded from accuracy)."""
        syms = [f"lightning.pytorch.mod::Cls.method{i}" for i in range(5)]
        task = self._rv_task_symbols(syms)
        result = script_run_bench._evaluate_rv(task, "## Symbols\nnothing recognisable here at all\n")
        assert result.scored is True
        assert result.metric_got == 0
        assert result.extraction_failed is True
        assert result.correct is False

    def test_symbol_path_extraction_not_failed_when_symbol_found(self, script_run_bench: Any) -> None:
        """Symbol path: at least one symbol found → extraction_failed stays False."""
        syms = [f"lightning.pytorch.mod::Cls.method{i}" for i in range(5)]
        task = self._rv_task_symbols(syms)
        text = " ".join(s.split(".")[-1] for s in syms[:4])
        result = script_run_bench._evaluate_rv(task, text)
        assert result.extraction_failed is False


# ===========================================================================
# _extract_codemap_meta (scan-query index telemetry)
# ===========================================================================


class TestExtractCodemapMeta:
    """BenchRunner._extract_codemap_meta: recover scan-query index metadata from tool_result text."""

    def test_index_metadata_recovered_from_prose_wrapped_json(self, script_run_bench: Any) -> None:
        """Method and not_covered are recovered when the scan-query JSON is embedded in prose.

        Regression: json.loads over the whole tool_result raised on any prose preamble/trailer,
        silently dropping index.method (~16/17 codemap runs recorded no method despite rdeps).
        """
        result = _make_run(script_run_bench, arm="codemap")
        block = {"content": 'some prose\n{"index": {"method": "rdeps", "not_covered": ["x"]}}\ntrailing'}
        script_run_bench.BenchRunner._extract_codemap_meta(block, result)
        assert result.codemap_methods == ["rdeps"]
        assert result.codemap_not_covered == ["x"]


# ===========================================================================
# _evaluate_oss (code_quality)
# ===========================================================================


class TestEvaluateOss:
    """code_quality evaluator: coupled / xrefs_broken / undocumented / uncovered."""

    def _oss_task(self, check: str, **gt_fields: Any) -> dict:
        """Return a code_quality task dict for the given check type."""
        return {
            "id": "CQ-01",
            "type": "code_quality",
            "ground_truth": {"check": check, **gt_fields},
        }

    def test_coupled_correct_when_dep_count_within_tolerance(self, script_run_bench: Any) -> None:
        """Coupled check: correct when dep_count is within 10% of GT."""
        task = self._oss_task("coupled", top_dep_count=100)
        result = script_run_bench._evaluate_oss(task, "dep_count: 100")
        assert result.scored is True
        assert result.correct is True

    def test_coupled_incorrect_outside_tolerance(self, script_run_bench: Any) -> None:
        """Coupled check: incorrect when extracted count is outside 10%."""
        task = self._oss_task("coupled", top_dep_count=100)
        result = script_run_bench._evaluate_oss(task, "120 dependencies found")
        assert result.correct is False

    @staticmethod
    def _cq03_ranking() -> list[dict[str, int | str]]:
        """Return an ordered five-module CQ-03 oracle with distinct names and counts."""
        return [
            {"name": "pkg.alpha", "dep_count": 49},
            {"name": "pkg.bravo", "dep_count": 42},
            {"name": "pkg.charlie", "dep_count": 37},
            {"name": "pkg.delta", "dep_count": 31},
            {"name": "pkg.echo", "dep_count": 29},
        ]

    def _cq03_task(self) -> dict:
        """Return the full ranking contract requested by the CQ-03 prompt."""
        ranking = self._cq03_ranking()
        return self._oss_task(
            "coupled",
            top_dep_count=ranking[0]["dep_count"],
            top_modules=ranking,
        )

    @pytest.mark.parametrize(
        "answer",
        [
            "1. `pkg.alpha` — dep_count: 49\n"
            "2. `pkg.bravo` — dep_count: 42\n"
            "3. `pkg.charlie` — dep_count: 37\n"
            "4. `pkg.delta` — dep_count: 31\n"
            "5. `pkg.echo` — dep_count: 29",
            "| Rank | Module | dep_count |\n"
            "| ---: | --- | ---: |\n"
            "| 1 | `pkg.alpha` | 49 |\n"
            "| 2 | `pkg.bravo` | 42 |\n"
            "| 3 | `pkg.charlie` | 37 |\n"
            "| 4 | `pkg.delta` | 31 |\n"
            "| 5 | `pkg.echo` | 29 |",
        ],
    )
    def test_cq03_requires_the_complete_ordered_module_dependency_ranking(
        self, script_run_bench: Any, answer: str
    ) -> None:
        """CQ-03 accepts each requested module/count pair in rank order, not only rank one."""
        expected = self._cq03_ranking()

        result = script_run_bench._evaluate_oss(self._cq03_task(), answer)

        assert result.correct is True
        assert result.extracted_metric == expected

    @pytest.mark.parametrize(
        "answer",
        [
            "1. `pkg.alpha` — dep_count: 49\n"
            "2. `pkg.bravo` — dep_count: 42\n"
            "3. `pkg.charlie` — dep_count: 37\n"
            "4. `pkg.delta` — dep_count: 31",
            "1. `pkg.alpha` — dep_count: 49\n"
            "2. `pkg.bravo` — dep_count: 42\n"
            "3. `pkg.unexpected` — dep_count: 37\n"
            "4. `pkg.delta` — dep_count: 31\n"
            "5. `pkg.echo` — dep_count: 29",
            "1. `pkg.alpha` — dep_count: 49\n"
            "2. `pkg.charlie` — dep_count: 37\n"
            "3. `pkg.bravo` — dep_count: 42\n"
            "4. `pkg.delta` — dep_count: 31\n"
            "5. `pkg.echo` — dep_count: 29",
            "1. `pkg.alpha` — dep_count: 49\n"
            "2. `pkg.bravo` — dep_count: 42\n"
            "3. `pkg.charlie` — dep_count: 37\n"
            "4. `pkg.delta` — dep_count: 31\n"
            "5. `pkg.echo` — dep_count: 28",
        ],
    )
    def test_cq03_rejects_incomplete_or_incorrect_ranking_members(self, script_run_bench: Any, answer: str) -> None:
        """CQ-03 rejects missing, substituted, reordered, and mismatched requested rows."""
        result = script_run_bench._evaluate_oss(self._cq03_task(), answer)

        assert result.extraction_failed is False
        assert result.correct is False

    def test_xrefs_broken_exact_match_correct(self, script_run_bench: Any) -> None:
        """xrefs_broken check: correct when exact broken count found in output."""
        task = self._oss_task("xrefs_broken", broken_count=3, broken_targets=[])
        result = script_run_bench._evaluate_oss(task, "3 broken xrefs detected")
        assert result.scored is True
        assert result.correct is True

    def test_xrefs_broken_wrong_count_incorrect(self, script_run_bench: Any) -> None:
        """xrefs_broken check: incorrect when count does not exactly match GT."""
        task = self._oss_task("xrefs_broken", broken_count=3, broken_targets=[])
        result = script_run_bench._evaluate_oss(task, "4 broken xrefs")
        assert result.correct is False

    def test_xrefs_broken_uses_target_names_when_available(self, script_run_bench: Any) -> None:
        """xrefs_broken check: count found symbols from broken_targets list."""
        broken_targets = [{"target": "mod::foo"}, {"target": "mod::bar"}]
        task = self._oss_task("xrefs_broken", broken_count=2, broken_targets=broken_targets)
        # Both short names present in output → got=2 == expected=2 → correct
        result = script_run_bench._evaluate_oss(task, "References to foo and bar are broken.")
        assert result.correct is True

    def test_undocumented_correct_within_tolerance(self, script_run_bench: Any) -> None:
        """combined_health / undocumented: correct within 10%."""
        task = self._oss_task("undocumented", undocumented_count=10)
        result = script_run_bench._evaluate_oss(task, "10 undocumented symbols found")
        assert result.scored is True
        assert result.correct is True

    def test_combined_health_accepts_both_explicit_counts(self, script_run_bench: Any) -> None:
        """combined_health requires and accepts its two labelled count fields."""
        task = self._oss_task("combined_health", undocumented_count=5, uncovered_count=8)
        result = script_run_bench._evaluate_oss(task, "## Answer\nundocumented_count: 5\nuncovered_count: 8\n")
        assert result.scored is True
        assert result.correct is True

    def test_independent_ast_count_wins_over_codemap_static_count(self, script_run_bench: Any) -> None:
        """Multi-view answers must score the explicitly labelled independent AST count.

        Prevents the first Codemap static count in an otherwise correct answer from being mistaken for the independently
        scored benchmark oracle.
        """
        task = self._oss_task(
            "undocumented",
            undocumented_count=7,
            undocumented_symbols=["Thing.one"],
            required_answer_components=["independent_ast_count", "independent_ast_symbols"],
            oracle_views={"independent_ast": {"count": 7, "symbols": ["Thing.one"]}},
        )
        output = "## Answer\ncodemap_static_count: 11\nindependent_ast_count: 7\n## Symbols\nThing.one\n"

        result = script_run_bench._evaluate_oss(task, output)

        assert result.correct is True
        assert result.extracted_metric["independent_ast_count"] == 7

    @pytest.mark.parametrize(
        ("check", "count_field", "count", "answer"),
        [
            pytest.param(
                "undocumented",
                "undocumented_count",
                7,
                "Independent AST: 7 unique names.",
                id="independent-ast-label",
            ),
            pytest.param(
                "undocumented",
                "undocumented_count",
                7,
                "Independent AST view: 7 unique names.",
                id="independent-ast-view-label",
            ),
            pytest.param("uncovered", "uncovered_count", 11, "11 uncovered symbols.", id="count-first-uncovered-label"),
            pytest.param(
                "uncovered",
                "uncovered_count",
                11,
                "Uncovered public symbols: 11.",
                id="label-first-uncovered-count",
            ),
        ],
    )
    def test_required_count_label_preserves_count_fitness_without_symbol_list(
        self,
        script_run_bench: Any,
        check: str,
        count_field: str,
        count: int,
        answer: str,
    ) -> None:
        """Explicit count labels retain count credit while required symbols remain missing."""
        task = self._oss_task(
            check,
            **{
                count_field: count,
                "required_answer_components": ["independent_ast_count", "independent_ast_symbols"],
                "oracle_views": {"independent_ast": {"count": count, "symbols": ["Thing.one"]}},
                "undocumented_symbols": ["Thing.one"],
                "uncovered_symbols": ["Thing.one"],
            },
        )

        result = script_run_bench._evaluate_oss(task, answer)

        assert result.correct is False
        assert result.recall == pytest.approx(0.5)
        assert result.extraction_failed is True

    @pytest.mark.parametrize(
        ("check", "count_field", "count", "answer"),
        [
            pytest.param(
                "undocumented",
                "undocumented_count",
                7,
                "Independent AST found names after 7 passes.",
                id="ast-not-a-label",
            ),
            pytest.param(
                "uncovered",
                "uncovered_count",
                11,
                "After 11 checks, uncovered symbols remain.",
                id="uncovered-not-a-count-label",
            ),
            pytest.param(
                "uncovered",
                "uncovered_count",
                11,
                "Uncovered symbols in 11 files.",
                id="uncovered-count-describes-files",
            ),
        ],
    )
    def test_required_count_rejects_incidental_numbers(
        self,
        script_run_bench: Any,
        check: str,
        count_field: str,
        count: int,
        answer: str,
    ) -> None:
        """Incidental numbers cannot satisfy a required count component."""
        task = self._oss_task(
            check,
            **{
                count_field: count,
                "required_answer_components": ["independent_ast_count"],
                "oracle_views": {"independent_ast": {"count": count}},
            },
        )

        result = script_run_bench._evaluate_oss(task, answer)

        assert result.correct is False
        assert result.recall == pytest.approx(0.0)
        assert result.extraction_failed is True

    def test_uncovered_parenthesized_label_count_is_parsed(self, script_run_bench: Any) -> None:
        """A label-first parenthesized uncovered count is a valid explicit answer."""
        task = self._oss_task("uncovered", uncovered_count=11)

        result = script_run_bench._evaluate_oss(task, "## Answer\nUncovered symbols (11)\n")

        assert result.correct is True
        assert result.metric_got == 11

    def test_combined_health_requires_both_explicit_count_components(self, script_run_bench: Any) -> None:
        """A correct docstring count cannot hide a wrong uncovered count."""
        task = self._oss_task("combined_health", undocumented_count=3, uncovered_count=7)
        output = "## Answer\nundocumented_count: 3\nuncovered_count: 6\n"

        result = script_run_bench._evaluate_oss(task, output)

        assert result.correct is False
        assert result.scoring_detail["components"]["uncovered_count"]["correct"] is False

    def test_uncovered_correct_within_tolerance(self, script_run_bench: Any) -> None:
        """Uncovered check: correct when extracted count is within 10%."""
        task = self._oss_task("uncovered", uncovered_count=20)
        result = script_run_bench._evaluate_oss(task, "20 uncovered symbols")
        assert result.scored is True
        assert result.correct is True

    def test_unknown_check_type_returns_unscored(self, script_run_bench: Any) -> None:
        """An unrecognised check value results in scored=False."""
        task = self._oss_task("unknown_check", some_count=5)
        result = script_run_bench._evaluate_oss(task, "5 things found")
        assert result.scored is False

    def test_evaluator_name_recorded(self, script_run_bench: Any) -> None:
        """evaluator_used is always _evaluate_oss."""
        task = self._oss_task("coupled", top_dep_count=10)
        result = script_run_bench._evaluate_oss(task, "dep_count: 10")
        assert result.evaluator_used == "_evaluate_oss"

    @pytest.mark.parametrize(
        "check,gt_fields",
        [
            pytest.param("coupled", {}, id="coupled"),
            pytest.param("undocumented", {}, id="undocumented-punctuation"),
            pytest.param("uncovered", {}, id="uncovered-punctuation"),
            pytest.param("undocumented", {"undocumented_count": "many"}, id="undocumented-undocumented_count-many"),
            pytest.param("uncovered", {"uncovered_count": None}, id="uncovered-uncovered_count-none"),
        ],
    )
    def test_missing_or_malformed_count_fields_keep_no_metric_as_extraction_failed(
        self, script_run_bench: Any, check: str, gt_fields: dict[str, Any]
    ) -> None:
        """Malformed GT count fields must not hide failure when no count is extractable."""
        task = self._oss_task(check, **gt_fields)
        result = script_run_bench._evaluate_oss(task, "No numeric metric is present.")
        assert result.scored is True
        assert result.extraction_failed is True
        assert result.metric_got is None


# ===========================================================================
# _evaluate_debug
# ===========================================================================


class TestEvaluateDebug:
    """debug_from_trace evaluator: function name and file basename both present."""

    def _debug_task(self, fn: str = "my_function", filepath: str = "src/mod/utils.py") -> dict:
        """Return a minimal debug_from_trace task dict."""
        return {
            "id": "DG-01",
            "type": "debug_from_trace",
            "ground_truth": {"function": fn, "file": filepath, "start_line": 10},
        }

    def test_correct_when_both_fn_and_file_present(self, script_run_bench: Any) -> None:
        """Accept output when both function name and file basename appear in output."""
        task = self._debug_task()
        result = script_run_bench._evaluate_debug(task, "The bug is in my_function inside utils.py")
        assert result.scored is True
        assert result.correct is True

    def test_incorrect_when_only_fn_present(self, script_run_bench: Any) -> None:
        """Reject output when only the function name appears but not the file."""
        task = self._debug_task()
        result = script_run_bench._evaluate_debug(task, "The bug is in my_function somewhere")
        assert result.correct is False

    def test_incorrect_when_only_file_present(self, script_run_bench: Any) -> None:
        """Reject output when only the file basename appears but not the function."""
        task = self._debug_task()
        result = script_run_bench._evaluate_debug(task, "Check inside utils.py for the problem")
        assert result.correct is False

    def test_incorrect_when_neither_present(self, script_run_bench: Any) -> None:
        """Reject output when neither token appears."""
        task = self._debug_task()
        result = script_run_bench._evaluate_debug(task, "Completely unrelated output text")
        assert result.correct is False
        assert result.extraction_failed is True

    def test_recall_1_when_both_found(self, script_run_bench: Any) -> None:
        """Report full recall when both expected tokens are present in the output."""
        task = self._debug_task()
        result = script_run_bench._evaluate_debug(task, "my_function in utils.py is the culprit")
        assert result.recall == pytest.approx(1.0)

    def test_recall_0_5_when_one_of_two_found(self, script_run_bench: Any) -> None:
        """Report half recall when exactly one of two expected tokens is found."""
        task = self._debug_task()
        result = script_run_bench._evaluate_debug(task, "only my_function appears here")
        assert result.recall == pytest.approx(0.5)

    def test_evaluator_name_recorded(self, script_run_bench: Any) -> None:
        """evaluator_used is always _evaluate_debug."""
        task = self._debug_task()
        result = script_run_bench._evaluate_debug(task, "my_function utils")
        assert result.evaluator_used == "_evaluate_debug"

    def test_match_is_case_insensitive(self, script_run_bench: Any) -> None:
        """Word-boundary matching is case-insensitive."""
        task = self._debug_task(fn="MyFunction")
        result = script_run_bench._evaluate_debug(task, "MYFUNCTION in utils.py")
        assert result.correct is True

    @pytest.mark.parametrize(
        "output",
        ["The issue is in my_function_extra inside utils_extra.py", "my_functionality moved to old_utils.py"],
    )
    def test_longer_identifiers_and_unrelated_filenames_do_not_match(self, script_run_bench: Any, output: str) -> None:
        """Function/file substrings embedded in longer names are not enough to score."""
        task = self._debug_task(fn="my_function", filepath="src/mod/utils.py")
        result = script_run_bench._evaluate_debug(task, output)
        assert result.correct is False


# ===========================================================================
# _evaluate_feature
# ===========================================================================


class TestEvaluateFeature:
    """feature_scaffolding evaluator: entry_point method and primary_file basename."""

    def _feature_task(
        self,
        entry_point: str = "Trainer.validate",
        primary_file: str = "src/lightning/trainer/trainer.py",
    ) -> dict:
        """Return a minimal feature_scaffolding task dict."""
        return {
            "id": "FT-01",
            "type": "feature_scaffolding",
            "ground_truth": {
                "entry_point": entry_point,
                "primary_file": primary_file,
            },
        }

    def test_correct_when_exact_entry_point_and_file_found(self, script_run_bench: Any) -> None:
        """Accept output when labelled Class.method and file path appear in output."""
        task = self._feature_task()
        result = script_run_bench._evaluate_feature(
            task, "## Files\nprimary_file: src/lightning/trainer/trainer.py\nentry_point: Trainer.validate\n"
        )
        assert result.scored is True
        assert result.correct is True
        assert result.evaluator_version == "v4"

    @pytest.mark.parametrize(
        ("entry_point_line", "expected_correct"),
        [
            pytest.param("entry_point: Trainer.validate.", True, id="terminal-period"),
            pytest.param("entry_point: `Trainer.validate`.", True, id="backticked-terminal-period"),
            pytest.param("entry_point: validate.", False, id="bare-method"),
            pytest.param("entry_point: Other.validate.", False, id="wrong-class"),
            pytest.param("entry_point: Trainer.validate_extra.", False, id="longer-entry-point"),
            pytest.param("entry_point: Trainer.validate..", False, id="doubled-period"),
        ],
    )
    def test_terminal_period_only_terminates_the_exact_labelled_entry_point(
        self, script_run_bench: Any, entry_point_line: str, expected_correct: bool
    ) -> None:
        """A sentence period is accepted without relaxing exact entry-point matching."""
        task = self._feature_task()
        result = script_run_bench._evaluate_feature(
            task,
            f"## Files\nprimary_file: src/lightning/trainer/trainer.py\n{entry_point_line}\n",
        )

        assert result.correct is expected_correct

    def test_bare_method_name_is_not_an_entry_point(self, script_run_bench: Any) -> None:
        """A bare method name cannot substitute for the requested Class.method."""
        task = self._feature_task(entry_point="Trainer.validate")
        result = script_run_bench._evaluate_feature(
            task, "## Files\nprimary_file: src/lightning/trainer/trainer.py\nentry_point: validate\n"
        )
        assert result.correct is False

    def test_exploration_prose_cannot_credit_a_different_entry_point(self, script_run_bench: Any) -> None:
        """An exploratory mention of the GT method cannot override the final labelled answer."""
        task = self._feature_task(
            entry_point="BaseFinetuning.freeze",
            primary_file="src/lightning/pytorch/callbacks/finetuning.py",
        )
        output = (
            "I inspected BaseFinetuning.freeze while narrowing the change.\n"
            "## Files\n"
            "primary_file: src/lightning/pytorch/callbacks/finetuning.py\n"
            "entry_point: BackboneFinetuning.finetune_function\n"
        )

        result = script_run_bench._evaluate_feature(task, output)

        assert result.correct is False

    def test_incorrect_when_file_missing_from_output(self, script_run_bench: Any) -> None:
        """Reject output when the file basename is absent from the output."""
        task = self._feature_task()
        result = script_run_bench._evaluate_feature(task, "implement validate somewhere")
        assert result.correct is False

    def test_extraction_failed_when_neither_found(self, script_run_bench: Any) -> None:
        """Mark extraction as failed when neither the entry point nor file stem is found."""
        task = self._feature_task()
        result = script_run_bench._evaluate_feature(task, "nothing relevant here")
        assert result.extraction_failed is True

    def test_evaluator_name_recorded(self, script_run_bench: Any) -> None:
        """evaluator_used is always _evaluate_feature."""
        task = self._feature_task()
        result = script_run_bench._evaluate_feature(task, "validate inside trainer.py")
        assert result.evaluator_used == "_evaluate_feature"

    @pytest.mark.parametrize(
        "output",
        ["Add invalidate logic in trainer_extra.py", "validation belongs in pretrainer.py"],
    )
    def test_longer_identifiers_and_unrelated_filenames_do_not_match(self, script_run_bench: Any, output: str) -> None:
        """Method/file substrings embedded in longer names are not enough to score."""
        task = self._feature_task(entry_point="Trainer.validate", primary_file="src/lightning/trainer/trainer.py")
        result = script_run_bench._evaluate_feature(task, output)
        assert result.correct is False


# ===========================================================================
# _evaluate_real_issue
# ===========================================================================


class TestEvaluateRealIssue:
    """real_issue evaluator: file-set recall >= 0.70 threshold."""

    def _ri_task(self, files: list[str]) -> dict:
        """Return a minimal real_issue task dict."""
        return {
            "id": "RI-01",
            "type": "real_issue",
            "ground_truth": {"files_changed": files},
        }

    def test_correct_when_all_files_found(self, script_run_bench: Any) -> None:
        """Accept output when all GT file basenames appear in the output."""
        task = self._ri_task(["src/trainer.py", "src/loops.py"])
        result = script_run_bench._evaluate_real_issue(task, "Edit trainer.py and loops.py to fix the issue")
        assert result.scored is True
        assert result.correct is True
        assert result.recall == pytest.approx(1.0)

    def test_correct_at_exactly_threshold(self, script_run_bench: Any) -> None:
        """Accept output when recall equals exactly 0.70."""
        files = [f"src/mod{i}.py" for i in range(10)]
        task = self._ri_task(files)
        # Mention exactly 7 out of 10 basenames → recall = 0.70
        text = " ".join(f"mod{i}" for i in range(7))
        result = script_run_bench._evaluate_real_issue(task, text)
        assert result.correct is True

    def test_incorrect_below_threshold(self, script_run_bench: Any) -> None:
        """Reject output when fewer than 70% of GT files are found."""
        files = [f"src/mod{i}.py" for i in range(10)]
        task = self._ri_task(files)
        # Only 5 of 10 → recall = 0.50 < 0.70
        text = " ".join(f"mod{i}" for i in range(5))
        result = script_run_bench._evaluate_real_issue(task, text)
        assert result.correct is False

    def test_empty_files_changed_returns_unscored(self, script_run_bench: Any) -> None:
        """Leave the result unscored when ground_truth.files_changed is empty."""
        task = self._ri_task([])
        result = script_run_bench._evaluate_real_issue(task, "anything")
        assert result.scored is False

    def test_extraction_failed_when_no_file_found(self, script_run_bench: Any) -> None:
        """Mark extraction as failed when no expected file basename appears."""
        task = self._ri_task(["src/trainer.py"])
        result = script_run_bench._evaluate_real_issue(task, "completely unrelated output")
        assert result.extraction_failed is True

    def test_evaluator_name_recorded(self, script_run_bench: Any) -> None:
        """evaluator_used is always _evaluate_real_issue."""
        task = self._ri_task(["src/trainer.py"])
        result = script_run_bench._evaluate_real_issue(task, "trainer.py")
        assert result.evaluator_used == "_evaluate_real_issue"

    def test_bare_basename_does_not_match_deep_path(self, script_run_bench: Any) -> None:
        """A bare basename no longer scores a deeply-nested GT file — path-with-parent required."""
        task = self._ri_task(["deeply/nested/path/trainer.py"])
        result = script_run_bench._evaluate_real_issue(task, "edit trainer.py to fix the bug")
        assert result.correct is False

    def test_path_with_parent_matches(self, script_run_bench: Any) -> None:
        """A path-with-parent form (parent/stem) scores the GT file."""
        task = self._ri_task(["deeply/nested/path/trainer.py"])
        result = script_run_bench._evaluate_real_issue(task, "## Files\npath/trainer.py\n")
        assert result.correct is True

    def test_verbose_prose_mentioning_common_stem_does_not_score(self, script_run_bench: Any) -> None:
        """A verbose answer that only name-drops `trainer` in prose scores nothing."""
        task = self._ri_task(["src/lightning/pytorch/trainer/trainer.py"])
        prose = "The trainer orchestrates training; the trainer loop runs many trainer callbacks."
        result = script_run_bench._evaluate_real_issue(task, prose)
        assert result.correct is False

    def test_structured_answer_with_path_scores(self, script_run_bench: Any) -> None:
        """The same task scores when the answer names the file by its path in a block."""
        task = self._ri_task(["src/lightning/pytorch/trainer/trainer.py"])
        answer = "## Files\nlightning/pytorch/trainer/trainer.py\n"
        result = script_run_bench._evaluate_real_issue(task, answer)
        assert result.correct is True

    def test_path_substring_inside_unrelated_filename_does_not_score(self, script_run_bench: Any) -> None:
        """A path candidate embedded in a longer filename must not count as a file hit."""
        task = self._ri_task(["src/pkg/foo.py"])
        result = script_run_bench._evaluate_real_issue(task, "## Files\npkg/foobar.py\n")
        assert result.correct is False


# ===========================================================================
# Structured-block scoring helpers
# ===========================================================================


class TestStructuredBlockScoring:
    """Answer-block extraction, stem blocklist, and RI path matching helpers."""

    def test_answer_region_returns_block_after_heading(self, script_run_bench: Any) -> None:
        """Region starts at the answer heading, dropping the exploration prose before it."""
        text = "exploring trainer everywhere\n## Files\npkg/trainer.py\n"
        region, degraded = script_run_bench._answer_region(text, ("files",))
        assert degraded is False
        assert "exploring" not in region
        assert "pkg/trainer.py" in region

    def test_answer_region_degraded_without_heading(self, script_run_bench: Any) -> None:
        """No heading → degraded=True and the full text is returned as fallback."""
        region, degraded = script_run_bench._answer_region("just prose about trainer", ("files",))
        assert degraded is True
        assert region == "just prose about trainer"

    def test_answer_region_matches_labelled_line(self, script_run_bench: Any) -> None:
        """A bold/plain `Files:` label line is recognised as a heading."""
        _, degraded = script_run_bench._answer_region("**Files:**\npkg/x.py\n", ("files",))
        assert degraded is False

    @pytest.mark.parametrize(
        "stem,text,expected",
        [
            pytest.param(
                "trainer", "the trainer runs the loop", False, id="trainer-the-trainer-runs-the-loop"
            ),  # bare blocklisted word
            pytest.param(
                "trainer", "see trainer.py for details", True, id="trainer-see-trainer.py-for-details"
            ),  # .py-qualified
            pytest.param("trainer", "in pkg.trainer here", True, id="trainer-in-pkg.trainer-here"),  # dotted-qualified
            pytest.param(
                "utils", "utility helpers live in utils somewhere", False, id="utils"
            ),  # bare blocklisted word
            pytest.param(
                "fit_loop", "the fit_loop advances the epoch", True, id="fit_loop"
            ),  # non-blocklisted plain word
        ],
    )
    def test_stem_matches(self, script_run_bench: Any, stem: str, text: str, expected: bool) -> None:
        """Blocklisted stems need a qualified reference; other stems match on a word boundary."""
        assert script_run_bench._stem_matches(stem, text) is expected

    @pytest.mark.parametrize(
        "file_path,text,expected",
        [
            pytest.param(
                "a/b/logger_connector.py",
                "edit b/logger_connector to fix",
                True,
                id="a-b-logger_connector.py-edit-b-logger_connector-to-fix",
            ),  # path-with-parent
            pytest.param(
                "a/b/logger_connector.py",
                "just logger_connector alone",
                False,
                id="a-b-logger_connector.py-just-logger_connector-alone",
            ),  # bare stem
            pytest.param(
                "src/pkg/trainer.py", "pkg/trainer.py has the bug", True, id="src-pkg-trainer.py"
            ),  # full relative path
        ],
    )
    def test_ri_file_matches(self, script_run_bench: Any, file_path: str, text: str, expected: bool) -> None:
        """A real_issue file counts only via a full path or a path-with-parent form."""
        assert script_run_bench._ri_file_matches(file_path, text) is expected


# ===========================================================================
# Contamination detection
# ===========================================================================


class TestContaminationDetection:
    """Plain-arm access to the prebuilt index or codemap binary is flagged."""

    def _runner(self, script_run_bench: Any, tmp_path: Path) -> Any:
        """Build a BenchRunner with dummy paths (no subprocess is launched by these tests)."""
        return script_run_bench.BenchRunner("haiku", "claude-haiku", tmp_path, tmp_path / "idx.json", timeout=1)

    def _read_event(self, tmp_path: Path) -> dict:
        """Assistant event with a single Read of the prebuilt codemap index."""
        return {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "id": "t1",
                        "input": {"file_path": str(tmp_path / ".cache" / "codemap" / "proj.json")},
                    }
                ]
            },
        }

    @pytest.mark.parametrize(
        "text,expected",
        [
            pytest.param("cat /repo/.cache/codemap/proj.json", True, id="cat-repo-.cache-codemap-proj.json"),
            pytest.param("less .cache/scan/x.json", True, id="less-.cache-scan-x.json"),
            pytest.param(
                "python3 plugins/codemap-py/bin/scan-query symbol X",
                True,
                id="python3-plugins-codemap-py-bin-scan-query-symbol-x",
            ),
            pytest.param("grep -rn Trainer src/", False, id="grep--rn-trainer-src"),
            # Windows backslash separators must still match the forward-slash markers.
            pytest.param(r"C:\repo\.cache\codemap\proj.json", True, id="c-repo-.cache-codemap-proj.json"),
            pytest.param(r"type C:\repo\.cache\scan\proj.json", True, id="type-c-repo-.cache-scan-proj.json"),
        ],
    )
    def test_is_contaminating_access(self, script_run_bench: Any, text: str, expected: bool) -> None:
        """Full-string index/binary access is detected on POSIX and Windows paths; ordinary grep is not."""
        assert script_run_bench._is_contaminating_access(text) is expected

    def test_plain_arm_index_read_flags_contamination(self, script_run_bench: Any, tmp_path: Path) -> None:
        """A plain-arm Read of .cache/codemap/*.json increments contamination_hits."""
        runner = self._runner(script_run_bench, tmp_path)
        result = script_run_bench.BenchRun(
            arm="plain", task_id="RI-01", task_type="real_issue", model="haiku", success=False
        )
        runner._handle(self._read_event(tmp_path), result, {}, 0.0)
        assert result.contamination_hits == 1

    def test_codemap_arm_index_read_not_flagged(self, script_run_bench: Any, tmp_path: Path) -> None:
        """The codemap arm legitimately touches the index — no contamination is recorded for it."""
        runner = self._runner(script_run_bench, tmp_path)
        result = script_run_bench.BenchRun(
            arm="codemap", task_id="RI-01", task_type="real_issue", model="haiku", success=False
        )
        runner._handle(self._read_event(tmp_path), result, {}, 0.0)
        assert result.contamination_hits == 0


# ===========================================================================
# _evaluate_develop_br
# ===========================================================================


class TestEvaluateDevelopBr:
    """develop_blast_radius evaluator: recall over expected caller list."""

    def _br_task(self, callers: list[str]) -> dict:
        """Return a minimal develop_blast_radius task dict."""
        return {
            "id": "BR-01",
            "type": "develop_blast_radius",
            "ground_truth": {
                "fn_callers": callers,
                "unique_caller_count": len(callers),
            },
        }

    def test_correct_when_all_callers_found(self, script_run_bench: Any) -> None:
        """Accept output containing every expected caller in canonical form."""
        callers = [
            "lightning.pytorch.trainer.trainer::Trainer.fit",
            "lightning.pytorch.loops.fit_loop::FitLoop.advance",
        ]
        task = self._br_task(callers)
        output = (
            "lightning.pytorch.trainer.trainer::Trainer.fit and "
            "lightning.pytorch.loops.fit_loop::FitLoop.advance both call the function"
        )
        result = script_run_bench._evaluate_develop_br(task, output)
        assert result.scored is True
        assert result.correct is True
        assert result.recall == pytest.approx(1.0)

    def test_incorrect_when_recall_below_threshold(self, script_run_bench: Any) -> None:
        """Reject output when fewer than 70% of expected callers are found."""
        callers = [f"lightning.pytorch.mod::Cls.method{i}" for i in range(10)]
        task = self._br_task(callers)
        # Mention only 5 of 10 → recall = 0.50 < 0.70
        output = " ".join(f"lightning.pytorch.mod::Cls.method{i}" for i in range(5))
        result = script_run_bench._evaluate_develop_br(task, output)
        assert result.correct is False
        assert result.recall is not None and result.recall < 0.70

    def test_empty_callers_returns_unscored(self, script_run_bench: Any) -> None:
        """Leave the result unscored when ground_truth.fn_callers is an empty list."""
        task = self._br_task([])
        result = script_run_bench._evaluate_develop_br(task, "anything")
        assert result.scored is False

    def test_recall_field_set_on_result(self, script_run_bench: Any) -> None:
        """Recall field is always populated (not None) on a scoreable task."""
        callers = ["lightning.pytorch.trainer::Trainer.fit"]
        task = self._br_task(callers)
        result = script_run_bench._evaluate_develop_br(task, "lightning.pytorch.trainer::Trainer.fit")
        assert result.recall is not None

    def test_dotted_form_matched_as_fallback(self, script_run_bench: Any) -> None:
        """Dotted-form output (no ::) is matched against expected callers."""
        callers = ["lightning.pytorch.trainer.trainer::Trainer.fit"]
        task = self._br_task(callers)
        # Write the dotted equivalent without :: separator
        output = "lightning.pytorch.trainer.trainer.Trainer.fit was found"
        result = script_run_bench._evaluate_develop_br(task, output)
        assert result.recall == pytest.approx(1.0)

    def test_extraction_failed_when_no_caller_found(self, script_run_bench: Any) -> None:
        """Mark extraction as failed when output contains no recognizable caller token."""
        callers = ["lightning.pytorch.trainer::Trainer.fit"]
        task = self._br_task(callers)
        result = script_run_bench._evaluate_develop_br(task, "completely unrelated prose output")
        assert result.extraction_failed is True

    def test_caller_count_gt_set(self, script_run_bench: Any) -> None:
        """caller_count_gt is populated from ground_truth.unique_caller_count."""
        callers = ["lightning.pytorch.mod::Cls.fn"]
        task = self._br_task(callers)
        result = script_run_bench._evaluate_develop_br(task, "")
        assert result.caller_count_gt == 1

    def test_evaluator_name_recorded(self, script_run_bench: Any) -> None:
        """evaluator_used is always _evaluate_develop_br."""
        callers = ["lightning.pytorch.mod::Cls.fn"]
        task = self._br_task(callers)
        result = script_run_bench._evaluate_develop_br(task, "lightning.pytorch.mod::Cls.fn")
        assert result.evaluator_used == "_evaluate_develop_br"

    def test_fuzzy_underscore_prefix_matched(self, script_run_bench: Any) -> None:
        """Underscore-prefixed class names match when method is identical (same module)."""
        callers = ["lightning.pytorch.loops.fit_loop::_FitLoop.advance"]
        task = self._br_task(callers)
        # Agent wrote without underscore prefix
        output = "lightning.pytorch.loops.fit_loop::FitLoop.advance"
        result = script_run_bench._evaluate_develop_br(task, output)
        assert result.recall == pytest.approx(1.0)

    def test_abbreviated_module_underscore_variant_still_scores(self, script_run_bench: Any) -> None:
        """An abbreviated-module + dropped-underscore answer still scores (module suffix)."""
        callers = ["lightning.pytorch.loops.evaluation_loop::_EvaluationLoop.advance"]
        task = self._br_task(callers)
        output = "loops.evaluation_loop::EvaluationLoop.advance"
        result = script_run_bench._evaluate_develop_br(task, output)
        assert result.recall == pytest.approx(1.0)

    def test_wrong_module_same_tail_rejected(self, script_run_bench: Any) -> None:
        """A same Class.method tail in a DIFFERENT module must not credit the GT caller."""
        callers = ["lightning.pytorch.loops.evaluation_loop::_EvaluationLoop._evaluation_step"]
        task = self._br_task(callers)
        # Fully qualified but wrong module (and dropped underscore) — the tail matches, the module does not.
        output = "lightning.pytorch.other.mod::EvaluationLoop._evaluation_step"
        result = script_run_bench._evaluate_develop_br(task, output)
        assert result.recall == pytest.approx(0.0)

    def test_plausible_but_wrong_qname_rejected(self, script_run_bench: Any) -> None:
        """A fully-qualified but entirely wrong caller scores nothing."""
        callers = ["lightning.pytorch.trainer.trainer::Trainer.fit"]
        task = self._br_task(callers)
        output = "lightning.pytorch.wrong.module::WrongClass.wrong_method"
        result = script_run_bench._evaluate_develop_br(task, output)
        assert result.recall == pytest.approx(0.0)

    def test_bare_common_tail_fallback_rejected(self, script_run_bench: Any) -> None:
        """An unqualified bare `Class.common_method` tail is too weak to credit."""
        callers = ["lightning.pytorch.trainer.trainer::Trainer.setup"]
        task = self._br_task(callers)
        # No module-qualified name anywhere → Form 11 fires, but `setup` is a common method tail.
        output = "The relevant caller looks like Trainer.setup based on my search."
        result = script_run_bench._evaluate_develop_br(task, output)
        assert result.recall == pytest.approx(0.0)

    def test_bare_distinctive_tail_fallback_still_scores(self, script_run_bench: Any) -> None:
        """A distinctive (non-common) bare Class.method tail still credits via Form 11."""
        callers = ["lightning.pytorch.loops.evaluation_loop::_EvaluationLoop._evaluation_step"]
        task = self._br_task(callers)
        output = "The caller is _EvaluationLoop._evaluation_step here."
        result = script_run_bench._evaluate_develop_br(task, output)
        assert result.recall == pytest.approx(1.0)

    def test_md_pointer_does_not_read_file(self, script_run_bench: Any, tmp_path: Path) -> None:
        """A `→ foo.md` pointer is treated as inline text; the file is never read."""
        caller = "lightning.pytorch.loops.evaluation_loop::_EvaluationLoop._evaluation_step"
        dump = tmp_path / "reply-dump.md"
        # The distinctive caller lives ONLY in the file — if it were read, recall would be 1.0.
        dump.write_text(f"Callers:\n{caller}\n")
        task = self._br_task([caller])
        output = f"No callers identified inline.\n→ {dump}\n"
        result = script_run_bench._evaluate_develop_br(task, output)
        assert result.recall == pytest.approx(0.0)


# ===========================================================================
# _module_compatible
# ===========================================================================


class TestModuleCompatible:
    """Module compatibility gate for the underscore-insensitive fuzzy tier."""

    @pytest.mark.parametrize(
        "gt_module,found_module,expected",
        [
            pytest.param("x.mod", "x.mod", True, id="equal"),
            pytest.param("lightning.pytorch.loops.evaluation_loop", "loops.evaluation_loop", True, id="found-suffix"),
            pytest.param("loops.evaluation_loop", "lightning.pytorch.loops.evaluation_loop", True, id="gt-suffix"),
            pytest.param("a.right", "a.wrong", False, id="sibling-mismatch"),
            pytest.param("lightning.pytorch.trainer", "lightning.pytorch.other", False, id="different-leaf"),
        ],
    )
    def test_compatibility(self, script_run_bench: Any, gt_module: str, found_module: str, expected: bool) -> None:
        """Equal or dotted-suffix modules are compatible; genuinely different modules are not."""
        assert script_run_bench._module_compatible(gt_module, found_module) is expected


# ===========================================================================
# _max_turns_for_task
# ===========================================================================


class TestMaxTurnsForTask:
    """Per-task turn cap must be identical for both arms."""

    def test_non_caller_task_uses_flat_floor(self, script_run_bench: Any) -> None:
        """A non-caller task type gets the flat 40-turn floor."""
        assert script_run_bench._max_turns_for_task({"type": "symbol_extraction"}) == 40

    @pytest.mark.parametrize(
        "task_type,callers,expected",
        [
            pytest.param("develop_blast_radius", 30, 120, id="br-scales-with-callers"),
            pytest.param("fn_call_graph", 5, 80, id="fn-hits-floor"),
            pytest.param("develop_blast_radius", 0, 80, id="zero-callers-floor"),
        ],
    )
    def test_caller_task_scales_with_count(
        self, script_run_bench: Any, task_type: str, callers: int, expected: int
    ) -> None:
        """Caller tasks scale 4x with unique_caller_count, floored at 80."""
        task = {"type": task_type, "ground_truth": {"unique_caller_count": callers}}
        assert script_run_bench._max_turns_for_task(task) == expected

    def test_cap_is_arm_independent(self, script_run_bench: Any) -> None:
        """The cap depends only on the task, so both arms receive the identical value."""
        task = {"type": "develop_blast_radius", "ground_truth": {"unique_caller_count": 37}}
        # _max_turns_for_task takes no arm argument — one call is the cap for plain AND codemap.
        assert script_run_bench._max_turns_for_task(task) == max(80, 37 * 4)


# ===========================================================================
# _paired_accuracy / _arm_extracted
# ===========================================================================


class TestPairedAccuracy:
    """Paired accuracy is computed over the same both-extracted task set for both arms."""

    def _run(self, script_run_bench: Any, arm: str, task_id: str, **quality: Any) -> Any:
        """Build a scored BenchRun for one arm/task with the given quality overrides."""
        incomplete = quality.pop("incomplete", False)
        q = script_run_bench.BenchQuality(**quality)
        return _make_run(
            script_run_bench,
            arm=arm,
            task_id=task_id,
            task_type="develop_blast_radius",
            quality=q,
            incomplete=incomplete,
        )

    def _both_scored(self, **kw: Any) -> dict:
        """Default kwargs for a scored, extracted run."""
        return dict(scored=True, extraction_failed=False, **kw)

    @pytest.mark.parametrize(
        "run,expected",
        [
            pytest.param(None, False, id="none"),
            pytest.param(dict(scored=True, correct=True), True, id="extracted"),
            pytest.param(dict(scored=True, extraction_failed=True), False, id="extraction-failed"),
            pytest.param(dict(scored=False), False, id="unscored"),
        ],
    )
    def test_arm_extracted(self, script_run_bench: Any, run: Any, expected: bool) -> None:
        """_arm_extracted is True only for a scored, extracted, completed run."""
        obj = None if run is None else _make_run(script_run_bench, quality=script_run_bench.BenchQuality(**run))
        assert script_run_bench._arm_extracted(obj) is expected

    def test_arm_extracted_false_for_incomplete(self, script_run_bench: Any) -> None:
        """A turn-budget-exhausted (incomplete) run does not count as extracted."""
        obj = _make_run(script_run_bench, quality=script_run_bench.BenchQuality(scored=True), incomplete=True)
        assert script_run_bench._arm_extracted(obj) is False

    def test_paired_uses_only_both_extracted_tasks(self, script_run_bench: Any) -> None:
        """Paired-n and per-arm correct counts include only tasks where BOTH arms extracted."""
        runs = [
            # Task A: both extracted, both correct → paired
            self._run(script_run_bench, "plain", "A", **self._both_scored(correct=True)),
            self._run(script_run_bench, "codemap", "A", **self._both_scored(correct=True)),
            # Task B: both extracted, plain wrong / codemap right → paired
            self._run(script_run_bench, "plain", "B", **self._both_scored(correct=False)),
            self._run(script_run_bench, "codemap", "B", **self._both_scored(correct=True)),
            # Task C: plain extraction-failed → NOT paired
            self._run(script_run_bench, "plain", "C", scored=True, extraction_failed=True),
            self._run(script_run_bench, "codemap", "C", **self._both_scored(correct=True)),
            # Task D: only codemap ran → NOT paired
            self._run(script_run_bench, "codemap", "D", **self._both_scored(correct=True)),
        ]
        result = script_run_bench._paired_accuracy(runs)
        assert result == {"n": 2, "plain_correct": 1, "codemap_correct": 2}

    def test_paired_none_when_no_task_pairs(self, script_run_bench: Any) -> None:
        """Return None when no task has both arms extracted."""
        runs = [
            self._run(script_run_bench, "plain", "A", scored=True, extraction_failed=True),
            self._run(script_run_bench, "codemap", "A", **self._both_scored(correct=True)),
        ]
        assert script_run_bench._paired_accuracy(runs) is None


# ===========================================================================
# _extract_diff
# ===========================================================================


class TestDiffImpactQuality:
    """Diff-impact scoring keeps answer sections and false positives observable."""

    @staticmethod
    def _task() -> dict:
        """Return a compact DI oracle with two callers and two tests."""
        return {
            "id": "DI-fixture",
            "type": "diff_impact",
            "ground_truth": {
                "fn_callers": ["lightning.pytorch.mod::Worker.run", "lightning.fabric.other::Other.go"],
                "test_modules": ["tests_pytorch.test_mod", "tests_fabric.test_other"],
                "unique_caller_count": 2,
            },
        }

    def test_exact_sections_score_caller_and_test_fitness_with_false_positive_penalty(
        self, script_run_bench: Any
    ) -> None:
        """DI quality rewards complete scoped answers but penalizes extra listed modules."""
        output = """exploration: lightning.pytorch.mod::Wrong.ignore\n## Callers
lightning.pytorch.mod::Worker.run
lightning.fabric.other::Other.go
lightning.pytorch.wrong::Wrong.ignore
## Tests
tests_pytorch.test_mod
tests_fabric.test_other
tests_pytorch.test_wrong
"""

        result = script_run_bench._evaluate_diff_impact(self._task(), output)

        assert result.correct is True
        assert result.recall == pytest.approx(0.8)
        assert result.scoring_detail["caller_recall"] == 1.0
        assert result.scoring_detail["test_recall"] == 1.0
        assert result.scoring_detail["caller_precision"] == pytest.approx(0.667)
        assert result.scoring_detail["test_precision"] == pytest.approx(0.667)
        assert result.scoring_detail["caller_fitness"] == pytest.approx(0.8)
        assert result.scoring_detail["test_fitness"] == pytest.approx(0.8)

    def test_missing_or_missectioned_answers_receive_no_cross_section_credit(self, script_run_bench: Any) -> None:
        """Only explicit matching H2 sections can contribute to the two DI components."""
        output = """## Tests
lightning.pytorch.mod::Worker.run
lightning.fabric.other::Other.go
tests_pytorch.test_mod
tests_fabric.test_other
"""

        result = script_run_bench._evaluate_diff_impact(self._task(), output)

        assert result.correct is False
        assert result.recall == 0.5
        assert result.scoring_detail["callers_section_found"] is False
        assert result.scoring_detail["tests_section_found"] is True
        assert result.scoring_detail["caller_got"] == 0
        assert result.scoring_detail["test_got"] == 2

    def test_missing_tests_or_wrong_test_module_cannot_pass_di_binary_gate(self, script_run_bench: Any) -> None:
        """Missing sections and wrong modules retain partial fitness but fail transparent correctness."""
        callers = "lightning.pytorch.mod::Worker.run\nlightning.fabric.other::Other.go"
        missing_tests = script_run_bench._evaluate_diff_impact(self._task(), f"## Callers\n{callers}\n")
        wrong_module = script_run_bench._evaluate_diff_impact(
            self._task(),
            f"## Callers\n{callers}\n## Tests\ntests_pytorch.wrong_module\n",
        )

        assert missing_tests.correct is False
        assert missing_tests.recall == 0.5
        assert missing_tests.scoring_detail["tests_section_found"] is False
        assert wrong_module.correct is False
        assert wrong_module.scoring_detail["test_got"] == 0
        assert wrong_module.scoring_detail["test_recall"] == 0.0


class TestExtractDiff:
    """Unified-diff extractor from agent output text."""

    _SIMPLE_DIFF = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"

    def test_valid_diff_extracted(self, script_run_bench: Any) -> None:
        """A well-formed unified diff is returned verbatim."""
        text = f"here is the fix\n{self._SIMPLE_DIFF}"
        result = script_run_bench._extract_diff(text)
        assert result is not None
        assert "--- a/x.py" in result
        assert "+++ b/x.py" in result

    def test_none_when_no_diff_present(self, script_run_bench: Any) -> None:
        """None returned when the text contains no unified diff headers."""
        assert script_run_bench._extract_diff("no diff here") is None

    def test_trailing_fence_stripped(self, script_run_bench: Any) -> None:
        """Trailing markdown code fence is stripped from the extracted diff."""
        text = "Here:\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n```"
        result = script_run_bench._extract_diff(text)
        assert result is not None
        assert "```" not in result

    def test_diff_ends_with_newline(self, script_run_bench: Any) -> None:
        """Extracted diff always ends with a newline character."""
        text = f"prefix\n{self._SIMPLE_DIFF.rstrip()}"
        result = script_run_bench._extract_diff(text)
        assert result is not None
        assert result.endswith("\n")

    def test_empty_string_returns_none(self, script_run_bench: Any) -> None:
        """Empty input string returns None."""
        assert script_run_bench._extract_diff("") is None

    def test_partial_diff_header_returns_none(self, script_run_bench: Any) -> None:
        """Text with only --- but no +++ header returns None."""
        assert script_run_bench._extract_diff("--- a/x.py\njust a comment") is None


# ===========================================================================
# _workflow_type_of
# ===========================================================================


class TestWorkflowTypeOf:
    """Workflow key accessor with task_type fallback."""

    def test_returns_workflow_type_when_set(self, script_run_bench: Any) -> None:
        """Return workflow_type field when it is non-empty."""
        run = _make_run(script_run_bench, workflow_type="query", task_type="symbol_extraction")
        assert script_run_bench._workflow_type_of(run) == "query"

    def test_falls_back_to_task_type_when_empty(self, script_run_bench: Any) -> None:
        """Return task_type when workflow_type is empty string."""
        run = _make_run(script_run_bench, workflow_type="", task_type="symbol_extraction")
        assert script_run_bench._workflow_type_of(run) == "symbol_extraction"

    @pytest.mark.parametrize(
        "workflow_type,task_type,expected",
        [
            pytest.param("query", "symbol_extraction", "query", id="query"),
            pytest.param("", "fn_call_graph", "fn_call_graph", id="empty"),
            pytest.param("debug", "debug_from_trace", "debug", id="debug"),
        ],
    )
    def test_parametrized_fallback_logic(
        self, script_run_bench: Any, workflow_type: str, task_type: str, expected: str
    ) -> None:
        """workflow_type wins when non-empty; task_type is the fallback."""
        run = _make_run(script_run_bench, workflow_type=workflow_type, task_type=task_type)
        assert script_run_bench._workflow_type_of(run) == expected


# ===========================================================================
# _effective_recall
# ===========================================================================


class TestBuildSystemPrompt:
    """System-prompt assembly must keep every non-tool sentence identical across arms."""

    _EFFICIENCY = "Answer in as few tool calls as possible; do not re-verify results you already have."
    _OUTPUT_HDR = "For symbol location tasks: report exactly in this format:"

    def _plain(self, script_run_bench: Any) -> str:
        """Build the plain-arm system prompt with fixed framing arguments."""
        return script_run_bench._build_system_prompt("plain", "demo", "/repo", "/idx.json")

    def _codemap(self, script_run_bench: Any) -> str:
        """Build the codemap-arm system prompt with fixed framing arguments."""
        return script_run_bench._build_system_prompt("codemap", "demo", "/repo", "/idx.json")

    def test_efficiency_sentence_present_in_both_arms(self, script_run_bench: Any) -> None:
        """The single efficiency instruction appears verbatim in both arms."""
        assert self._EFFICIENCY in self._plain(script_run_bench)
        assert self._EFFICIENCY in self._codemap(script_run_bench)

    def test_output_format_block_identical_across_arms(self, script_run_bench: Any) -> None:
        """The output-format requirements block is byte-identical in both arms."""
        plain = self._plain(script_run_bench)
        codemap = self._codemap(script_run_bench)
        marker = self._OUTPUT_HDR
        plain_block = plain[plain.index(marker) :]
        codemap_block = codemap[codemap.index(marker) :]
        assert plain_block == codemap_block

    def test_plain_forbids_scan_query(self, script_run_bench: Any) -> None:
        """Plain arm keeps the scan-query prohibition; codemap arm does not forbid it."""
        plain = self._plain(script_run_bench)
        assert "Do NOT use scan-query" in plain
        assert "Do NOT use the Skill tool" in plain

    def test_codemap_documents_subcommands(self, script_run_bench: Any) -> None:
        """Codemap arm documents scan-query syntax and the subcommand reference list."""
        codemap = self._codemap(script_run_bench)
        assert "scan-query --index /idx.json <subcommand>" in codemap
        assert "fn-rdeps" in codemap
        assert "coupled" in codemap

    def test_index_path_substituted_into_codemap(self, script_run_bench: Any) -> None:
        """The concrete index path is interpolated into the codemap tool section."""
        assert "/custom/index.json" in script_run_bench._build_system_prompt(
            "codemap", "demo", "/repo", "/custom/index.json"
        )

    @pytest.mark.parametrize(
        "removed",
        [
            "STOP after one call",
            "Trust scan-query output as authoritative",
            "burns tokens",
            "MUST be your first tool call",
            "Do NOT grep",
        ],
    )
    def test_strategy_coaching_removed_from_codemap(self, script_run_bench: Any, removed: str) -> None:
        """Efficiency-steering / per-task strategy coaching is gone from the codemap prompt."""
        assert removed not in self._codemap(script_run_bench)

    def test_count_semantics_state_unique_callers(self, script_run_bench: Any) -> None:
        """Any retained fn-rdeps count note states unique callers, not call-site edges."""
        codemap = self._codemap(script_run_bench)
        assert "call-site EDGES" not in codemap
        assert "`count` = unique callers" in codemap


class TestEffectiveRecall:
    """Recall accessor: true recall when set, else binary correctness in [0, 1]."""

    def test_returns_recall_field_when_set(self, script_run_bench: Any) -> None:
        """Return quality.recall directly when it is populated."""
        run = _make_run(script_run_bench)
        run.quality = script_run_bench.BenchQuality(scored=True, recall=0.85)
        assert script_run_bench._effective_recall(run) == pytest.approx(0.85)

    def test_returns_none_for_unscored_run(self, script_run_bench: Any) -> None:
        """Return None only when the run's quality was not scored."""
        run = _make_run(script_run_bench)
        run.quality = script_run_bench.BenchQuality(scored=False)
        assert script_run_bench._effective_recall(run) is None

    def test_extraction_failed_returns_none_not_zero(self, script_run_bench: Any) -> None:
        """Parse failure is a separate signal → None (excluded from recall), never a 0.0 miss."""
        run = _make_run(script_run_bench)
        run.quality = script_run_bench.BenchQuality(scored=True, correct=False, extraction_failed=True)
        assert script_run_bench._effective_recall(run) is None

    def test_correct_line_task_scores_one(self, script_run_bench: Any) -> None:
        """Line/count evaluators (no recall field) score 1.0 when correct within tolerance."""
        run = _make_run(script_run_bench)
        run.quality = script_run_bench.BenchQuality(scored=True, correct=True, metric_got=100, metric_expected=98)
        assert script_run_bench._effective_recall(run) == pytest.approx(1.0)

    def test_wrong_but_parsed_line_scores_zero_not_ratio(self, script_run_bench: Any) -> None:
        """A parsed-but-wrong line number (got≫expected) is a real miss → 0.0, never a ratio above 1.0."""
        run = _make_run(script_run_bench)
        run.quality = script_run_bench.BenchQuality(
            scored=True, correct=False, extraction_failed=False, metric_got=1116, metric_expected=1024
        )
        assert script_run_bench._effective_recall(run) == pytest.approx(0.0)

    def test_returns_none_when_none_run(self, script_run_bench: Any) -> None:
        """Return None for a None run argument."""
        assert script_run_bench._effective_recall(None) is None


# ---------------------------------------------------------------------------
# Relocated-index admission
# ---------------------------------------------------------------------------


def _relocated_worktree_index(tmp_path: Path) -> tuple[Path, Path, dict[str, Any], dict[str, str]]:
    """Build a clean committed worktree holding a root-relocated copy of a frozen index.

    Args:
        tmp_path: Directory the worktree, index, and manifest are created under.

    Returns:
        The worktree root, the relocated index path, a parity manifest locking the frozen index
        bytes, and the relocation provenance a run in that worktree would carry.
    """
    import hashlib
    import subprocess

    from _bench_common.mutation_isolation import relocate_frozen_index_for_worktree

    source = tmp_path / "managed-clone"
    source.mkdir()
    repo = tmp_path / "worktree"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "a.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    for args in (
        ["init", "-q"],
        ["add", "-A"],
        ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    frozen_payload = {
        "scan_root": str(source.resolve()),
        "git_sha": commit,
        "scan_version": 3,
        "modules": [{"name": "pkg.a"}],
    }
    frozen_bytes = (json.dumps(frozen_payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    derived_bytes, relocation = relocate_frozen_index_for_worktree(frozen_bytes, source_root=source, worktree_root=repo)
    index_path = tmp_path / "relocated-index.json"
    index_path.write_bytes(derived_bytes)
    manifest = {
        "target_source": {"commit": commit},
        "index": {
            "raw_sha256": hashlib.sha256(frozen_bytes).hexdigest(),
            "git_sha": commit,
            "scan_version": 3,
        },
    }
    return repo, index_path, manifest, dict(relocation)


class TestRelocatedIndexAdmission:
    """A worktree run proves index identity through relocation provenance, never byte identity."""

    def test_valid_provenance_admits_a_worktree_index(
        self, script_run_bench: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Relocation provenance admits an index whose bytes differ from the locked hash.

        Scenario: a run executes in its own worktree, so its index carries the worktree's
        ``scan_root`` and can never reproduce the manifest's locked raw hash; the provenance written
        by the relocation is what proves the graph is still the frozen one.
        """
        repo, index_path, manifest, relocation = _relocated_worktree_index(tmp_path)
        monkeypatch.setattr(script_run_bench, "_PARITY_MANIFEST", manifest)

        script_run_bench._validate_primary_runtime(repo, index_path, relocation)

        assert manifest["index"]["raw_sha256"] == relocation["frozen_index_sha256"]

    def test_provenance_naming_the_wrong_frozen_source_is_rejected(
        self, script_run_bench: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Provenance whose frozen source is not the locked index is rejected.

        Scenario: a caller supplies provenance derived from some other frozen index; admitting it
        would let an unrelated graph enter the run under the locked manifest's authority.
        """
        repo, index_path, manifest, relocation = _relocated_worktree_index(tmp_path)
        monkeypatch.setattr(script_run_bench, "_PARITY_MANIFEST", manifest)
        relocation["frozen_index_sha256"] = "0" * 64

        with pytest.raises(ValueError, match="wrong frozen source"):
            script_run_bench._validate_primary_runtime(repo, index_path, relocation)

    def test_provenance_disagreeing_with_the_bytes_on_disk_is_rejected(
        self, script_run_bench: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Provenance whose derived hash misses the on-disk index is rejected.

        Scenario: the relocated copy changed after its provenance was written, so the digest the
        run would attest to is no longer the index the model actually reads.
        """
        repo, index_path, manifest, relocation = _relocated_worktree_index(tmp_path)
        monkeypatch.setattr(script_run_bench, "_PARITY_MANIFEST", manifest)
        relocation["derived_index_sha256"] = "1" * 64

        with pytest.raises(ValueError, match="changed after relocation"):
            script_run_bench._validate_primary_runtime(repo, index_path, relocation)

    def test_absent_provenance_keeps_the_byte_gate(
        self, script_run_bench: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Without provenance a non-locked index is still rejected on its bytes.

        Scenario: the same relocated index is offered by a run that claims no relocation; the
        original byte-identity gate must reject it exactly as before.
        """
        repo, index_path, manifest, _relocation = _relocated_worktree_index(tmp_path)
        monkeypatch.setattr(script_run_bench, "_PARITY_MANIFEST", manifest)

        with pytest.raises(ValueError, match="canonical run requires the locked index bytes"):
            script_run_bench._validate_primary_runtime(repo, index_path)
