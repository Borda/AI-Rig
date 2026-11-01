"""Tests for benchmarks/run-claude-agentic.py public API.

Scope: black-box testing of public classes and functions against documented
contracts.  No live ``claude`` subprocess is launched.  Any test that
requires a real codemap index on disk is guarded with
``pytest.mark.skip`` and a clear reason.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import subprocess
import sys
import threading
from collections.abc import Mapping
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from types import SimpleNamespace
from typing import Any
from unittest import mock
from unittest.mock import MagicMock

import pytest
from _git_helpers import run_git

BENCHMARKS_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BENCHMARKS_DIR))

# Patch seams live in the package modules the runner shim re-exports from: patching the shim
# would leave each package module's own global binding untouched.
from _bench_claude.agentic import cli as agentic_cli  # noqa: E402
from _bench_claude.agentic import runner as agentic_runner  # noqa: E402
from _bench_common.mutation_isolation import relocate_frozen_index_for_worktree  # noqa: E402
from _bench_common.presentation import BENCHMARK_OUTPUT_WIDTH  # noqa: E402

AGENTIC_SUITE_PATH = BENCHMARKS_DIR / "suites" / "tasks-agentic.json"
CLAUDE_RUNNER_PATH = BENCHMARKS_DIR / "run-claude-agentic.py"
PARITY_MANIFEST_PATH = BENCHMARKS_DIR / "manifests" / "provider-parity-methodology.json"
ACTIVE_MANIFEST = json.loads(PARITY_MANIFEST_PATH.read_text(encoding="utf-8"))
ACTIVE_REVISION = ACTIVE_MANIFEST["experiment_revision"]
ACTIVE_AGENTIC_SUITE = next(
    suite for suite in ACTIVE_MANIFEST["suites"] if suite["path"] == "benchmarks/suites/tasks-agentic.json"
)
#: Task ids read from the shipped suite rather than counted out here, so adding a task to the suite changes the
#: expected scope instead of failing every scope assertion in this module.
AGENTIC_TASK_IDS = [task["id"] for task in json.loads(AGENTIC_SUITE_PATH.read_text(encoding="utf-8"))["tasks"]]
AGENTIC_ARMS = ("A_plain", "B_auto", "C_strict")
_DENIED_MARKERS = ("ORACLE-DO-NOT-EXPOSE", "HISTORY-DO-NOT-EXPOSE", "SIBLING-DO-NOT-EXPOSE")
_DENIED_TOOL_IDS = ("toolu_oracle_bash", "toolu_history_bash", "toolu_sibling_bash", "toolu_oracle_read")
_skip_claude_sandbox_integration = pytest.mark.skipif(
    os.environ.get("RUN_CLAUDE_SANDBOX_INTEGRATION") != "1",
    reason="set RUN_CLAUDE_SANDBOX_INTEGRATION=1 to exercise the installed Claude sandbox against a loopback mock",
)


def test_public_runner_stays_below_the_250_kilobyte_maintenance_limit() -> None:
    """The public runner must keep stage detail in focused private modules.

    Matches the gate the Codex structural runner already carries. Without it the file grows past the repository's check-
    added-large-files limit and the next commit that touches it fails on a size error rather than on its content.
    """
    assert CLAUDE_RUNNER_PATH.stat().st_size < 250_000


# ===========================================================================
# Fixtures
# ===========================================================================


def _make_task(mod: Any, **kwargs: Any) -> Any:
    """Build a minimal valid task, applying supplied fields over the synthetic defaults.

    >>> task = _make_task(getfixture("script_run_agentic"), id="example", difficulty="hard")
    >>> task.id, task.difficulty, task.type
    ('example', 'hard', 'fix')
    """
    defaults = {
        "id": "BA-01",
        "type": "fix",
        "prompt": "Describe blast radius.",
        "primary_module": "lightning.pytorch.callbacks.timer",
        "difficulty": "simple",
    }
    defaults.update(kwargs)
    return mod.Task(**defaults)


def _minimal_index(modules: list[dict]) -> dict:
    """Wrap the supplied module list without copying or enriching its contents.

    >>> modules = [{"name": "example"}]
    >>> _minimal_index(modules)
    {'modules': [{'name': 'example'}]}
    >>> _minimal_index(modules)["modules"] is modules
    True
    """
    return {"modules": modules}


def _mock_codemap_python_probe(
    monkeypatch: pytest.MonkeyPatch, script_run_agentic: Any, *, returncode: int = 0
) -> None:
    """Mock only the external interpreter capability probe used by Codemap treatments."""
    original_run = script_run_agentic.subprocess.run
    candidate = str(Path(script_run_agentic.sys.executable).resolve())

    def _run(command: list[str], *args: Any, **kwargs: Any) -> Any:
        """Return the selected capability result while preserving unrelated subprocesses."""
        if (
            command[:2] == [candidate, "-c"]
            and len(command) == 3
            and "sys.implementation.name" in command[2]
            and "sys.version_info" in command[2]
        ):
            return SimpleNamespace(returncode=returncode)
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(script_run_agentic.subprocess, "run", _run)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        pytest.param(PurePosixPath("/private/benchmark/evaluator"), "//private/benchmark/evaluator/**", id="posix"),
        pytest.param(PureWindowsPath("D:/benchmark/evaluator"), "//D:/benchmark/evaluator/**", id="windows"),
    ],
)
def test_absolute_filetool_pattern_is_portable(script_run_agentic: Any, path: PurePath, expected: str) -> None:
    """File-tool denies preserve absolute POSIX and Windows coordinates."""
    assert script_run_agentic._absolute_filetool_pattern(path) == expected


@pytest.fixture(name="tmp_index")
def _tmp_index(tmp_path: Path, script_run_agentic: Any) -> Path:
    """Write a minimal codemap index to a temporary file and return its path.

    Example:
        >>> modules = json.loads(getfixture("tmp_index").read_text())["modules"]
        >>> len(modules), sum(module["name"].startswith("tests.") for module in modules)
        (4, 1)
    """
    data = _minimal_index(
        [
            {
                "name": "lightning.pytorch.trainer.trainer",
                "direct_imports": ["lightning.pytorch.callbacks.timer"],
                "dep_count": 10,
                "status": "ok",
            },
            {
                "name": "lightning.pytorch.loops.fit_loop",
                "direct_imports": ["lightning.pytorch.callbacks.timer"],
                "dep_count": 5,
                "status": "ok",
            },
            {
                "name": "lightning.pytorch.callbacks.timer",
                "direct_imports": [],
                "dep_count": 0,
                "status": "ok",
            },
            {
                "name": "tests.test_timer",
                "direct_imports": ["lightning.pytorch.callbacks.timer"],
                "dep_count": 0,
                "status": "ok",
            },
        ]
    )
    index_file = tmp_path / "pytorch-lightning.json"
    index_file.write_text(json.dumps(data))
    return index_file


@pytest.fixture(name="ground_truth")
def _ground_truth(tmp_index: Path, script_run_agentic: Any) -> Any:
    """Build ground truth from the synthetic index, keeping test importers outside production expectations.

    >>> truth = getfixture("ground_truth")
    >>> expected = truth.expected["BA-01"]
    >>> len(expected), any(name.startswith("tests.") for name in expected)
    (2, False)
    """
    task = _make_task(
        script_run_agentic,
        id="BA-01",
        primary_module="lightning.pytorch.callbacks.timer",
    )
    return script_run_agentic.GroundTruth(tmp_index, [task])


def _bash_query_events(command: str, result: str) -> list[dict[str, Any]]:
    """Return the native assistant ``tool_use`` and user ``tool_result`` events of one Bash command.

    >>> use, answer = _bash_query_events("codemap-py query --compact", "{}")
    >>> use["message"]["content"][0]["input"], answer["message"]["content"][0]["content"]
    ({'command': 'codemap-py query --compact'}, '{}')
    """
    return [
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "id": "query", "name": "Bash", "input": {"command": command}}]
            },
        },
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "query", "content": result}]},
        },
    ]


def _mixed_native_events(diagnostic: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a native stream whose genuine compact query is interleaved with one diagnostic event."""
    query, answer = _bash_query_events(
        "codemap-py query --compact rdeps package --index /outside/index.json", '{"modules": []}'
    )
    return [
        query,
        diagnostic,
        answer,
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "finished"}]}},
        {"type": "result", "subtype": "success", "usage": {"input_tokens": 7, "output_tokens": 2}},
    ]


def _replay_events(runner: Any, result: Any, events: list[dict[str, Any]]) -> None:
    """Feed every native event through the runner's event handler, accumulating into ``result``."""
    pending: dict[str, float] = {}
    for event in events:
        runner._handle_event(event, result, pending, set(), set(), set(), 1.0)


def _answer_contract_raw_task(primary_module: str = "package.target") -> dict[str, Any]:
    """Return a raw suite task whose answer contract has one ``production_importers`` field."""
    return {
        "id": "BA-CONTRACT",
        "type": "blast_radius_analysis",
        "prompt": "List the production importers.",
        "primary_module": primary_module,
        "answer_contract": {"fields": ["production_importers"]},
    }


_DIAGNOSTIC_EVENT_TYPES = pytest.mark.parametrize("event_type", ["system", "assistant", "user"])
_DIAGNOSTIC_MESSAGES = pytest.mark.parametrize(
    "message",
    [
        pytest.param("provider diagnostic", id="string-message"),
        pytest.param(None, id="null-message"),
        pytest.param([], id="list-message"),
        pytest.param(7, id="number-message"),
        pytest.param({"content": "plain text"}, id="string-content"),
        pytest.param({"content": [None, "plain text", 7]}, id="non-object-blocks"),
    ],
)
_INHERITED_CODEMAP_ENV = ("CLAUDE_PLUGIN_ROOT", "CODEMAP_BIN", "CODEMAP_PYTHON", "SCAN_NO_AUTOBUILD")
_PROVENANCE_FIELDS = (
    "task_hash",
    "prompt_hash",
    "suite_hash",
    "evaluator_id",
    "evaluator_hash",
    "envelope_hash",
    "arm_contract_hash",
    "repo_sha",
    "index_sha",
)


@pytest.fixture(name="ba01_task")
def _ba01_task(script_run_agentic: Any) -> Any:
    """Shipped BA-01 task loaded with its locked provider-parity provenance."""
    tasks = script_run_agentic.load_tasks_with_provenance(AGENTIC_SUITE_PATH, PARITY_MANIFEST_PATH)
    return next(item for item in tasks if item.id == "BA-01")


@pytest.fixture(name="provenance_task")
def _provenance_task(script_run_agentic: Any) -> Any:
    """Synthetic BA-01 task carrying fixed provider-parity provenance, independent of the shipped suite."""
    return script_run_agentic.Task(
        id="BA-01",
        type="fix",
        prompt="p",
        primary_module="lightning.pytorch.callbacks.timer",
        experiment_revision="fixture-revision",
        task_hash="task-hash",
        prompt_hash="prompt-hash",
        oracle_class="independent",
        headline_eligible_v1=False,
        scoreable=True,
    )


@pytest.fixture(name="runner")
def _runner(script_run_agentic: Any, tmp_path: Path) -> Any:
    """Haiku ``ModelRunner`` rooted in ``tmp_path``; it launches nothing until a run is requested."""
    return script_run_agentic.ModelRunner("haiku", script_run_agentic.MODELS["haiku"], tmp_path, timeout=300)


@pytest.fixture(name="make_benchmark")
def _make_benchmark(script_run_agentic: Any, tmp_path: Path, tmp_index: Path) -> Any:
    """Return a factory building a one-model Benchmark over ``tmp_path`` for the given task and arms."""

    def build(task: Any, arms: list[str]) -> Any:
        return script_run_agentic.Benchmark(
            tasks=[task],
            arms=arms,
            models=[("haiku", script_run_agentic.MODELS["haiku"])],
            repo_path=tmp_path,
            index_path=tmp_index,
            output_path=tmp_path / "results.json",
            log_path=tmp_path / "tool-calls.jsonl",
        )

    return build


@pytest.fixture(name="run_cell")
def _run_cell(script_run_agentic: Any) -> Any:
    """Return a runner executing one silent haiku cell of a Benchmark and returning its result."""

    def run(benchmark: Any, task: Any, arm: str, *, repeat: int = 1, repeats: int = 1, metadata: Any = None) -> Any:
        return benchmark._run_single(
            task,
            "haiku",
            script_run_agentic.MODELS["haiku"],
            arm,
            repeat,
            repeats,
            print_fn=lambda _text: None,
            metadata=metadata or {},
        )

    return run


@pytest.fixture(name="answer_contract_benchmark")
def _answer_contract_benchmark(script_run_agentic: Any, make_benchmark: Any) -> Any:
    """Return a factory yielding ``(task, benchmark)`` for an A_plain cell with a labelled-answer contract."""

    def build(primary_module: str = "package.target") -> tuple[Any, Any]:
        raw_task = _answer_contract_raw_task(primary_module)
        task = script_run_agentic.Task(
            id=raw_task["id"],
            type=raw_task["type"],
            prompt=script_run_agentic.materialize_agentic_prompt(raw_task),
            primary_module=raw_task["primary_module"],
            answer_task=raw_task,
        )
        return task, make_benchmark(task, ["A_plain"])

    return build


@pytest.fixture(name="stub_model_run")
def _stub_model_run(script_run_agentic: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Return an installer replacing ``ModelRunner.run`` with a stub that launches no provider."""

    def install(**fields: Any) -> None:
        def run(_self: Any, task: Any, arm: str, **_kwargs: Any) -> Any:
            return script_run_agentic.BenchmarkRun(
                arm=arm, task_id=task.id, task_type=task.type, model="haiku", **{"success": False, **fields}
            )

        monkeypatch.setattr(script_run_agentic.ModelRunner, "run", run)

    return install


@pytest.fixture(name="cli_inputs")
def _cli_inputs(tmp_index: Path) -> Any:
    """Stub the CLI's task loader, index discovery and runtime validation; yields the task-loader mock."""
    with (
        mock.patch("_bench_claude.agentic.cli.load_tasks_with_provenance", autospec=True) as load_tasks,
        mock.patch("_bench_claude.agentic.cli.find_index", autospec=True, return_value=tmp_index),
        mock.patch("_bench_claude.agentic.cli._validate_parity_runtime", autospec=True),
    ):
        yield load_tasks


@pytest.fixture(name="observed_timeout")
def _observed_timeout(script_run_agentic: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Swap the CLI's ``ModelRunner`` for a double that records the timeout each cell constructs it with."""
    observed: dict[str, int] = {}

    class FixtureRunner:
        """Record construction policy while supplying a deterministic native result."""

        _ARM_ALLOWED = script_run_agentic.ModelRunner._ARM_ALLOWED
        _ARM_DISALLOWED = script_run_agentic.ModelRunner._ARM_DISALLOWED

        def __init__(self, _model_short: str, _model_id: str, _repo_path: Path, *, timeout: int) -> None:
            """Remember the timeout the benchmark chose for this cell."""
            observed["timeout"] = timeout

        def run(self, selected_task: Any, selected_arm: str, **_kwargs: Any) -> Any:
            """Return an unsuccessful benchmark result without invoking a provider."""
            return script_run_agentic.BenchmarkRun(
                arm=selected_arm, task_id=selected_task.id, task_type=selected_task.type, model="haiku", success=False
            )

        def _system_prompt(self, _task_type: str, _arm: str) -> str:
            """Return a fixed prompt envelope for the fixture runner."""
            return "fixture envelope"

    monkeypatch.setattr(agentic_cli, "ModelRunner", FixtureRunner)
    return observed


# ===========================================================================
# class Task
# ===========================================================================


class TestTask:
    """Task dataclass construction, defaults and accepted types."""

    def test_construction_with_required_fields(self, script_run_agentic: Any) -> None:
        """Task with only required fields sets defaults for optional ones.

        Scenario: user builds a Task by supplying id, type, prompt only;
        expects primary_module='', difficulty='unknown' per docstring.
        """
        task = script_run_agentic.Task(id="T01", type="fix", prompt="Fix the bug.")
        assert task.id == "T01"
        assert task.type == "fix"
        assert task.prompt == "Fix the bug."
        assert task.primary_module == ""
        assert task.difficulty == "unknown"

    def test_construction_with_all_fields(self, script_run_agentic: Any) -> None:
        """Task stores every supplied field correctly.

        Scenario: user supplies all optional fields explicitly; values must
        round-trip through the dataclass without modification.
        """
        task = script_run_agentic.Task(
            id="BA-42",
            type="review",
            prompt="Review this change.",
            primary_module="lightning.pytorch.trainer.trainer",
            difficulty="hard",
        )
        assert task.id == "BA-42"
        assert task.type == "review"
        assert task.primary_module == "lightning.pytorch.trainer.trainer"
        assert task.difficulty == "hard"

    @pytest.mark.parametrize("task_type", ["fix", "feature", "refactor", "review"])
    def test_accepted_task_types(self, script_run_agentic: Any, task_type: str) -> None:
        """Task accepts all four documented type values without error.

        Scenario: user creates a Task for each benchmark arm type listed
        in the module docstring; no ValueError should be raised.
        """
        task = script_run_agentic.Task(id="X01", type=task_type, prompt="p")
        assert task.type == task_type


class TestProviderParityTaskIntegration:
    """Agentic task/result provenance must be supplied by the shared B1 contract."""

    def test_legacy_loader_preserves_unlocked_custom_suite_support(
        self, tmp_path: Path, script_run_agentic: Any
    ) -> None:
        """Legacy arms may consume a custom suite without claiming canonical provider-parity provenance."""
        suite_path = tmp_path / "custom-agentic-suite.json"
        suite_path.write_text(
            json.dumps([{"id": "CUSTOM-01", "type": "fix", "prompt": "Inspect the failure."}]),
            encoding="utf-8",
        )

        tasks = script_run_agentic.load_legacy_tasks(suite_path)

        assert [task.id for task in tasks] == ["CUSTOM-01"]
        assert tasks[0].experiment_revision == ""
        assert tasks[0].task_hash
        assert tasks[0].prompt_hash
        assert tasks[0].suite_hash
        assert tasks[0].suite_raw_hash

    def test_validate_parity_runtime_rejects_wrong_repository(
        self, tmp_index: Path, tmp_path: Path, script_run_agentic: Any
    ) -> None:
        """Canonical agentic runs cannot use a repository outside the locked target commit."""
        with pytest.raises(ValueError, match="target commit"):
            script_run_agentic._validate_parity_runtime(tmp_path, tmp_index)

    def test_load_tasks_with_provenance_matches_shared_manifest_policy(self, ba01_task: Any) -> None:
        """The agentic loader keeps raw task identity and attaches its locked policy revision."""
        locked_task = next(item for item in ACTIVE_AGENTIC_SUITE["tasks"] if item["id"] == "BA-01")

        assert ba01_task.experiment_revision == ACTIVE_REVISION
        assert ba01_task.task_hash == locked_task["canonical_task_sha256"]
        assert ba01_task.prompt_hash == locked_task["prompt_sha256"]
        assert ba01_task.oracle_class == "independent"
        assert ba01_task.headline_eligible_v1 is False
        assert ba01_task.scoreable is True

    def test_load_tasks_with_provenance_uses_the_locked_semantic_suite_hash(
        self, script_run_agentic: Any, ba01_task: Any
    ) -> None:
        """The loaded suite fingerprint tracks ordered task and prompt identity, not wrappers."""
        raw_tasks = script_run_agentic.load_task_suite(AGENTIC_SUITE_PATH)

        assert ba01_task.suite_hash == script_run_agentic.semantic_suite_hash(raw_tasks)
        assert ba01_task.suite_raw_hash == hashlib.sha256(AGENTIC_SUITE_PATH.read_bytes()).hexdigest()

    def test_load_tasks_with_provenance_rejects_known_task_with_tampered_prompt(
        self, tmp_path: Path, script_run_agentic: Any
    ) -> None:
        """A known ID cannot retain its policy after canonical task/prompt bytes change."""
        raw_suite = json.loads(AGENTIC_SUITE_PATH.read_text(encoding="utf-8"))
        raw_tasks = raw_suite["tasks"] if isinstance(raw_suite, dict) else raw_suite
        task = next(item for item in raw_tasks if item["id"] == "BA-01").copy()
        task["prompt"] = f"{task['prompt']}\nTampered after the lock."
        suite_path = tmp_path / "tampered-agentic-suite.json"
        suite_path.write_text(json.dumps([task]), encoding="utf-8")

        with pytest.raises(ValueError, match=r"(task|prompt).*hash|hash.*(task|prompt)"):
            script_run_agentic.load_tasks_with_provenance(suite_path, PARITY_MANIFEST_PATH)

    def test_load_tasks_with_provenance_rejects_task_missing_from_manifest_policy(
        self, tmp_path: Path, script_run_agentic: Any
    ) -> None:
        """A task cannot run under an absent policy or silently receive inferred eligibility."""
        suite_path = tmp_path / "unknown-task.json"
        suite_path.write_text(json.dumps([{"id": "unknown", "type": "fix", "prompt": "p"}]))

        with pytest.raises(ValueError, match="policy"):
            script_run_agentic.load_tasks_with_provenance(suite_path, PARITY_MANIFEST_PATH)

    @pytest.mark.parametrize(
        ("arm", "expected"),
        [
            pytest.param("plain", None, id="legacy-plain-is-not-relabelled"),
            pytest.param("codemap", None, id="legacy-codemap-is-not-relabelled"),
            pytest.param("A_plain", "A_plain", id="canonical-plain"),
            pytest.param("B_auto", "B_auto", id="canonical-auto"),
            pytest.param("C_strict", "C_strict", id="canonical-required"),
        ],
    )
    def test_parity_arm_identity_never_relabels_legacy_agentic_results(
        self, script_run_agentic: Any, arm: str, expected: str | None
    ) -> None:
        """Only explicitly requested A/B/C runs receive a canonical parity-arm identity."""
        assert script_run_agentic.parity_arm_identity(arm) == expected

    def test_auto_and_required_arms_have_distinct_no_call_semantics(self, script_run_agentic: Any, runner: Any) -> None:
        """B_auto exposes Codemap without required use; C_strict records the required-use instruction."""
        assert "Skill(codemap:query-code)" in script_run_agentic.ModelRunner._ARM_ALLOWED["B_auto"][1]
        assert "Skill(codemap:query-code)" in script_run_agentic.ModelRunner._ARM_ALLOWED["C_strict"][1]
        assert "must use Codemap at least once" not in runner._system_prompt("fix", "B_auto")
        assert "must use Codemap at least once" in runner._system_prompt("fix", "C_strict")

    @pytest.mark.parametrize("task_type", ["read_crop", "fix_single", "fix_multicaller"])
    def test_canonical_stage_prompts_use_one_current_skill_contract(self, runner: Any, task_type: str) -> None:
        """Canonical Claude stages must not mix legacy launchers with the installed Skill contract."""
        auto = runner._system_prompt(task_type, "B_auto")
        strict = runner._system_prompt(task_type, "C_strict")

        assert "/codemap-py:query-code" in auto
        assert "scan-query" not in auto
        assert "/codemap:query-code" not in auto
        assert "loading the Skill alone" in strict
        # The scorer credits adherence only for a successful compact query, so the arm text must ask for one:
        # a strict prompt that omits `--compact` scores its own contract against a requirement it never stated.
        assert "`codemap-py query --compact`" in strict

    def test_default_dry_run_schedules_the_full_canonical_matrix(
        self, cli_inputs: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str], script_run_agentic: Any
    ) -> None:
        """The default 16-task agentic execution plan uses A/B/C once per cell.

        Legacy arm labels remain available only when explicitly selected, so a normal ``--run_all`` invocation cannot
        silently produce an unpaired legacy experiment instead of the locked provider-parity matrix.
        """
        cli_inputs.return_value = [
            script_run_agentic.Task(id=f"BA-{number:02d}", type="fix", prompt="p") for number in range(1, 17)
        ]

        script_run_agentic.main(repo_path=tmp_path, run_all=True, dry_run=True)

        output = capsys.readouterr().out
        assert "tasks:       16, arms: 3, models: 3, repeat: 1" in output
        assert "total runs:  144" in output
        assert output.count("[DRY RUN]") == 144
        assert "| A_plain | rep=1/1" in output
        assert "| B_auto | rep=1/1" in output
        assert "| C_strict | rep=1/1" in output
        assert "| plain |" not in output

    def test_resolve_agentic_scope_binds_the_default_claude_matrix(self, script_run_agentic: Any) -> None:
        """The public resolver fingerprints every default Claude coordinate.

        Prevents a scope token that omits the provider, model cohort, task order, or retry-inclusive cell timeout. Those
        omissions could authorize a different paid study than the one a reviewer inspected.
        """
        first = script_run_agentic.resolve_agentic_scope()
        second = script_run_agentic.resolve_agentic_scope()
        payload = {key: value for key, value in first.items() if key != "scope_sha256"}
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

        assert first == second
        assert first["provider"] == "claude"
        assert first["task_ids"] == AGENTIC_TASK_IDS
        assert first["arms"] == list(AGENTIC_ARMS)
        assert first["models"] == list(script_run_agentic.MODELS)
        assert first["repetitions"] == 1
        assert first["coordinate_timeout_seconds"] == 600
        assert first["total_cells"] == len(AGENTIC_TASK_IDS) * len(AGENTIC_ARMS) * len(script_run_agentic.MODELS)
        assert "complete_run_max_wall_clock_seconds" not in first
        assert first["manifest_sha256"] == hashlib.sha256(PARITY_MANIFEST_PATH.read_bytes()).hexdigest()
        assert first["scope_sha256"] == hashlib.sha256(encoded).hexdigest()

    def test_nondefault_repeat_requires_its_exact_scope_hash(
        self, cli_inputs: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str], script_run_agentic: Any
    ) -> None:
        """An expanded Claude run is rejected until its exact derived scope is supplied.

        The regression is an accidental repeat override running without a separately reviewable authorization token. The
        paired successful call proves a valid scope hash is not rejected merely because it is nondefault.
        """
        # Ids come from the shipped suite: the derived scope hash fingerprints the real task order, so a synthetic
        # stand-in list would make the accepted call fail for the wrong reason.
        cli_inputs.return_value = [
            script_run_agentic.Task(id=task_id, type="fix", prompt="p") for task_id in AGENTIC_TASK_IDS
        ]
        scope = script_run_agentic.resolve_agentic_scope(repetitions=2)
        expected_runs = len(AGENTIC_TASK_IDS) * len(AGENTIC_ARMS) * len(script_run_agentic.MODELS) * 2

        with pytest.raises(SystemExit, match=r"scope.*SHA|SHA.*scope"):
            script_run_agentic.main(repo_path=tmp_path, run_all=True, repeat=2, dry_run=True)
        script_run_agentic.main(
            repo_path=tmp_path, run_all=True, repeat=2, dry_run=True, scope_sha256=scope["scope_sha256"]
        )

        output = capsys.readouterr().out
        assert f"tasks:       {len(AGENTIC_TASK_IDS)}, arms: 3, models: 3, repeat: 2" in output
        assert f"total runs:  {expected_runs}" in output
        assert output.count("[DRY RUN]") == expected_runs

    def test_canonical_loader_uses_the_shared_labelled_answer_prompt(
        self, tmp_path: Path, script_run_agentic: Any
    ) -> None:
        """A canonical Task keeps the exact shared prompt bytes used for its hash.

        The provider-visible prompt must include the shared labelled JSON instruction, while an explicit legacy load
        keeps only the task prose.
        """
        raw_task = _answer_contract_raw_task()
        suite_path = tmp_path / "suite.json"
        suite_path.write_text(json.dumps([raw_task]), encoding="utf-8")
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "experiment_revision": "fixture-revision",
                    "suites": [
                        {
                            "tasks": [
                                {
                                    "id": raw_task["id"],
                                    "canonical_task_sha256": script_run_agentic.canonical_task_hash(raw_task),
                                    "prompt_sha256": script_run_agentic._delivered_prompt_hash(
                                        raw_task, canonical=True
                                    ),
                                    "oracle_class": "independent",
                                    "headline_eligible_v1": False,
                                    "effective_scoreable": True,
                                }
                            ]
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        task = script_run_agentic.load_tasks_with_provenance(suite_path, manifest_path)[0]

        assert task.prompt == script_run_agentic.materialize_agentic_prompt(raw_task)
        assert task.prompt_hash == hashlib.sha256(task.prompt.encode("utf-8")).hexdigest()
        assert "BEGIN_ANSWER_JSON" in task.prompt
        assert script_run_agentic.load_legacy_tasks(suite_path)[0].prompt == raw_task["prompt"]

    @mock.patch("_bench_claude.agentic.cli.score_answer", autospec=True, side_effect=agentic_cli.score_answer)
    def test_canonical_result_uses_the_shared_labelled_answer_oracle(
        self, score_answer: mock.Mock, answer_contract_benchmark: Any, stub_model_run: Any, run_cell: Any
    ) -> None:
        """Canonical results parse the labelled response before shared answer scoring."""
        task, benchmark = answer_contract_benchmark()
        output_text = 'BEGIN_ANSWER_JSON\n{"production_importers": []}\nEND_ANSWER_JSON'
        stub_model_run(success=True, output_text=output_text)

        result = run_cell(benchmark, task, "A_plain")

        assert result.answer_scored is True
        assert result.answer_contract_valid is True
        assert result.answer_diagnostic_only is False
        assert result.answer_pooling_eligible is True
        assert result.answer_quality_score == 1.0
        assert result.answer_correct is True
        assert result.answer_components == {"production_importers": 1.0}
        assert result.answer_graded_score == 1.0
        assert result.answer_graded_components == {"production_importers": 1.0}
        assert score_answer.call_args.kwargs == {
            "exposure_text": output_text,
            "report_text": output_text,
            "tool_calls": 0,
        }

    def test_non_final_text_cannot_change_response_protocol_outcome(
        self, answer_contract_benchmark: Any, stub_model_run: Any, run_cell: Any
    ) -> None:
        """Response protocol validation considers only the final report corpus.

        Scenario: an earlier assistant turn holds a wrong labelled answer, the final report holds the right one;
        only text after the last tool use may decide the protocol outcome.
        """
        task, benchmark = answer_contract_benchmark()
        earlier_text = 'BEGIN_ANSWER_JSON\n{"production_importers":["package.nonfinal"]}\nEND_ANSWER_JSON\n'
        final_report = 'BEGIN_ANSWER_JSON\n{"production_importers": []}\nEND_ANSWER_JSON'
        stub_model_run(success=True, output_text=earlier_text + final_report, last_tool_text_offset=len(earlier_text))

        result = run_cell(benchmark, task, "A_plain")

        assert result.answer_contract_valid is True
        assert result.answer_diagnostic_only is False
        assert result.answer_pooling_eligible is True
        assert result.answer_scored is True
        assert result.answer_quality_score == 1.0

    @pytest.mark.parametrize(
        ("output_text", "diagnostic_only", "semantic_scored"),
        [
            pytest.param(
                '{"production_importers":["lightning.pytorch.loops.fit_loop","lightning.pytorch.trainer.trainer"]}',
                True,
                True,
                id="unique-bare-json",
            ),
            pytest.param(
                "The importers are lightning.pytorch.loops.fit_loop and lightning.pytorch.trainer.trainer.",
                False,
                False,
                id="prose-with-evidence",
            ),
        ],
    )
    def test_answer_wire_failure_keeps_independent_evidence_metrics(
        self,
        script_run_agentic: Any,
        answer_contract_benchmark: Any,
        stub_model_run: Any,
        run_cell: Any,
        output_text: str,
        diagnostic_only: bool,
        semantic_scored: bool,
    ) -> None:
        """Envelope failure cannot masquerade as zero retrieval or enter pooled evidence."""
        task, benchmark = answer_contract_benchmark("lightning.pytorch.callbacks.timer")
        benchmark.answer_oracles[task.id] = script_run_agentic.AgenticOracle(
            task_id=task.id,
            fields=("production_importers",),
            expected={
                "production_importers": ("lightning.pytorch.loops.fit_loop", "lightning.pytorch.trainer.trainer")
            },
        )
        stub_model_run(success=True, output_text=output_text)

        result = run_cell(benchmark, task, "A_plain")

        assert result.answer_contract_valid is False
        assert result.answer_diagnostic_only is diagnostic_only
        assert result.answer_pooling_eligible is False
        assert result.answer_scored is semantic_scored
        assert result.answer_error
        assert result.quality.erec == 1.0
        assert result.quality.rrec == 1.0
        assert result.answer_quality_score == (1.0 if semantic_scored else None)

    def test_canonical_result_persists_structured_semantic_answer_failures(
        self, answer_contract_benchmark: Any, stub_model_run: Any, run_cell: Any
    ) -> None:
        """Canonical telemetry keeps actual and expected values for a parsed wrong answer.

        Scenario: a valid envelope contains a wrong importer list. The run must
        retain the field-level semantic mismatch rather than reducing it to a
        score, so the prospective pass summary can explain the failed cell.
        """
        task, benchmark = answer_contract_benchmark()
        stub_model_run(
            success=True,
            output_text='BEGIN_ANSWER_JSON\n{"production_importers":["package.unrelated"]}\nEND_ANSWER_JSON',
        )

        result = run_cell(benchmark, task, "A_plain")

        assert result.answer_correct is False
        assert len(result.answer_failure_details) == 1
        failure = result.answer_failure_details[0]
        assert isinstance(failure["category"], str)
        assert failure["field"] == "production_importers"
        assert failure["actual"] == ["package.unrelated"]
        assert failure["expected"] == []

    def test_run_single_copies_task_provenance_to_provider_native_result(
        self, ba01_task: Any, make_benchmark: Any, stub_model_run: Any, run_cell: Any
    ) -> None:
        """Shared hashes/policy fields survive beside legacy Claude-native telemetry in snapshots."""
        benchmark = make_benchmark(ba01_task, ["plain"])
        stub_model_run()

        result = run_cell(
            benchmark, ba01_task, "plain", metadata={"experiment_revision": ba01_task.experiment_revision}
        )

        assert result.experiment_revision == "legacy-unversioned"
        assert result.parity_arm is None
        assert result.task_hash == ba01_task.task_hash
        assert result.prompt_hash == ba01_task.prompt_hash
        assert result.oracle_class == ba01_task.oracle_class
        assert result.headline_eligible_v1 is ba01_task.headline_eligible_v1
        assert result.scoreable is ba01_task.scoreable
        assert result.input_tokens == 0
        assert result.tools.total == 0

    def test_legacy_arm_keeps_legacy_revision_and_canonical_arm_gets_locked_one(
        self, ba01_task: Any, make_benchmark: Any, stub_model_run: Any, run_cell: Any
    ) -> None:
        """A legacy label is not upgraded from an internal canonical provider-parity policy."""
        benchmark = make_benchmark(ba01_task, ["plain", "A_plain"])
        stub_model_run()

        legacy = run_cell(benchmark, ba01_task, "plain", repeat=1, repeats=2)
        canonical = run_cell(benchmark, ba01_task, "A_plain", repeat=2, repeats=2)

        assert legacy.experiment_revision == "legacy-unversioned"
        assert legacy.parity_arm is None
        assert canonical.experiment_revision == ba01_task.experiment_revision
        assert canonical.parity_arm == "A_plain"

    @pytest.mark.parametrize(
        ("arm", "expected_timeout"),
        [
            pytest.param("plain", 210, id="legacy-model-default"),
            pytest.param("A_plain", 600, id="canonical-shared-default"),
        ],
    )
    def test_run_single_selects_shared_timeout_only_for_parity_arms(
        self,
        ba01_task: Any,
        make_benchmark: Any,
        observed_timeout: dict[str, int],
        run_cell: Any,
        arm: str,
        expected_timeout: int,
    ) -> None:
        """Canonical agentic cells use the provider-neutral timeout without altering legacy cells."""
        benchmark = make_benchmark(ba01_task, [arm])

        run_cell(benchmark, ba01_task, arm)

        assert observed_timeout == {"timeout": expected_timeout}

    def test_explicit_canonical_result_stamps_complete_common_provenance(
        self, ba01_task: Any, make_benchmark: Any, stub_model_run: Any, run_cell: Any
    ) -> None:
        """A canonical result has concrete suite/evaluator/envelope/arm/repo/index provenance."""
        benchmark = make_benchmark(ba01_task, ["A_plain"])
        stub_model_run()

        result = run_cell(benchmark, ba01_task, "A_plain")

        stamped = {
            field: isinstance(getattr(result, field), str) and bool(getattr(result, field))
            for field in _PROVENANCE_FIELDS
        }
        assert stamped == dict.fromkeys(_PROVENANCE_FIELDS, True)
        assert result.arm_contract_hash == "936a684f5b4bb6211669633a17d2a12980b24f2de43b265bbc28ef09d9a65ba7"

    def test_auto_no_call_stays_valid_while_required_no_call_fails_compliance(
        self,
        script_run_agentic: Any,
        monkeypatch: pytest.MonkeyPatch,
        provenance_task: Any,
        make_benchmark: Any,
        stub_model_run: Any,
        run_cell: Any,
    ) -> None:
        """B_auto preserves scoring on no-call; C_strict records a use-compliance failure without erasing it."""
        benchmark = make_benchmark(provenance_task, ["B_auto", "C_strict"])
        stub_model_run(success=True)
        monkeypatch.setattr(benchmark.gt, "score", lambda *_a, **_kw: script_run_agentic.QualityScore(scored=True))

        auto = run_cell(benchmark, provenance_task, "B_auto", repeat=1, repeats=2)
        required = run_cell(benchmark, provenance_task, "C_strict", repeat=2, repeats=2)

        assert auto.success is True
        assert auto.parity_arm == "B_auto"
        assert auto.codemap_compliant is None
        assert auto.treatment_adherence is True
        assert required.success is True
        assert required.error == ""
        assert required.quality.scored is True
        assert required.parity_arm == "C_strict"
        assert required.codemap_compliant is False
        assert required.treatment_adherence is False

    @pytest.mark.parametrize(
        ("compact_success", "expected"),
        [
            pytest.param(False, False, id="attempt-only"),
            pytest.param(True, True, id="matching-compact-result"),
        ],
    )
    def test_required_graph_compliance_uses_native_compact_result(
        self,
        script_run_agentic: Any,
        monkeypatch: pytest.MonkeyPatch,
        provenance_task: Any,
        make_benchmark: Any,
        stub_model_run: Any,
        run_cell: Any,
        compact_success: bool,
        expected: bool,
    ) -> None:
        """C admission follows the persisted native compact-result association, not its counters."""
        benchmark = make_benchmark(provenance_task, ["C_strict"])
        stub_model_run(
            success=True,
            tools=script_run_agentic.ToolCounts(bash=1, scan_query=1),
            codemap_compact_success=compact_success,
        )
        monkeypatch.setattr(benchmark.gt, "score", lambda *_a, **_kw: script_run_agentic.QualityScore(scored=True))

        result = run_cell(benchmark, provenance_task, "C_strict")

        assert result.tools.skill == 0
        assert result.tools.scan_query == 1
        assert result.codemap_compliant is expected

    @pytest.mark.parametrize(
        ("command", "result"),
        [
            pytest.param("codemap-py query --help", "usage", id="help"),
            pytest.param("echo codemap-py query --compact rdeps package", "{}", id="spoofed-shell-text"),
            pytest.param("codemap-py query --compact", "{}", id="malformed"),
            pytest.param("codemap-py query --compact rdeps package", "Exit code 127", id="failed-command"),
            pytest.param("codemap-py query --compact rdeps package", "not-json", id="malformed-result"),
            pytest.param(
                "codemap-py query --compact rdeps package 2>/dev/null || printf '{}'", "{}", id="shell-fallback"
            ),
            pytest.param("codemap-py query --compact rdeps package\nprintf '{}'", "{}", id="newline-fallback"),
            pytest.param(
                "codemap-py query --compact rdeps package", '{"error":"frozen index unavailable"}', id="error-payload"
            ),
            pytest.param("codemap-py query --compact 'unterminated", "{}", id="malformed-shell-quote"),
        ],
    )
    def test_codemap_evidence_rejects_non_successful_compact_query(
        self, script_run_agentic: Any, command: str, result: str
    ) -> None:
        """Help, spoofed, malformed, and failed Bash calls never produce strict compact success."""
        evidence = script_run_agentic._claude_codemap_evidence(_bash_query_events(command, result))

        assert evidence["codemap_compact_success"] is False

    def test_codemap_evidence_associates_native_compact_query_result(self, script_run_agentic: Any) -> None:
        """Only the matching successful result admits a syntactically valid compact native query."""
        events = _bash_query_events("codemap-py query --compact rdeps package", '{"modules": []}')

        evidence = script_run_agentic._claude_codemap_evidence(events)

        assert evidence["codemap_query_attempted"] == 1
        assert evidence["codemap_query_succeeded"] == 1
        assert evidence["codemap_compact_success"] is True

    @_DIAGNOSTIC_EVENT_TYPES
    @_DIAGNOSTIC_MESSAGES
    def test_mixed_native_events_preserve_codemap_query_evidence(
        self, script_run_agentic: Any, event_type: str, message: Any
    ) -> None:
        """Diagnostic message shapes cannot crash native scoring or discard a genuine query result.

        Scenario: a provider diagnostic with an odd ``message`` shape sits between a compact query and its result; the
        query is still attributed, and the diagnostic alone never counts as a query.
        """
        diagnostic = {"type": event_type, "message": message}

        evidence = script_run_agentic._claude_codemap_evidence(_mixed_native_events(diagnostic))

        assert evidence["codemap_query_attempted"] == 1
        assert evidence["codemap_query_succeeded"] == 1
        assert evidence["codemap_compact_success"] is True
        assert script_run_agentic._claude_codemap_evidence([diagnostic])["codemap_query_attempted"] == 0

    @_DIAGNOSTIC_EVENT_TYPES
    @_DIAGNOSTIC_MESSAGES
    def test_mixed_native_events_preserve_outside_path_and_recovery_evidence(
        self, script_run_agentic: Any, tmp_path: Path, event_type: str, message: Any
    ) -> None:
        """Diagnostic events cannot hide outside-workspace paths or a frozen-index recovery attempt."""
        diagnostic = {"type": event_type, "message": message}
        recovery = {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "codemap-py index"}}]},
        }
        events = _mixed_native_events(diagnostic)

        assert script_run_agentic._outside_workspace_path_evidence(events, tmp_path) == (
            ["/outside/index.json"],
            ["/outside/index.json"],
        )
        assert script_run_agentic._frozen_index_recovery_attempted(events) is False
        assert script_run_agentic._frozen_index_recovery_attempted([diagnostic, recovery]) is True

    @_DIAGNOSTIC_EVENT_TYPES
    @_DIAGNOSTIC_MESSAGES
    def test_mixed_native_events_preserve_summary_and_usage(
        self, script_run_agentic: Any, runner: Any, event_type: str, message: Any
    ) -> None:
        """Diagnostic events cannot crash the event summary or the usage and tool accounting of a replayed run.

        Scenario: the same mixed stream is summarised and replayed through the runner's event handler; the final text,
        token usage and Bash count survive, and the input events are never mutated.
        """
        events = _mixed_native_events({"type": event_type, "message": message})
        original = json.dumps(events)
        result = script_run_agentic.BenchmarkRun(
            arm="C_strict", task_id="fixture", task_type="query", model="haiku", success=False
        )

        summary = script_run_agentic._claude_event_summary(events)
        _replay_events(runner, result, events)

        assert summary["output_text"] == "finished"
        assert summary["usage_complete"] is True
        assert summary["raw_events"] == events
        assert result.success is True
        assert result.tools.bash == 1
        assert result.input_tokens == 7
        assert result.output_tokens == 2
        assert result.output_text.endswith("finished")
        assert json.dumps(events) == original

    def test_scan_query_detection_requires_an_executable_command_boundary(self, script_run_agentic: Any) -> None:
        """Telemetry credits direct or chained execution but not text that merely mentions the tool."""
        assert script_run_agentic._invokes_scan_query("scan-query symbol Trainer")
        assert script_run_agentic._invokes_scan_query("cd /repo && /plugin/bin/scan-query rdeps module")
        assert not script_run_agentic._invokes_scan_query("echo scan-query symbol Trainer")

    @pytest.mark.parametrize("quoted_pattern", [r"'Strategy\.setup_environment$'", r'"Strategy\.setup_environment$"'])
    def test_query_arguments_normalize_shell_quoted_patterns(
        self, script_run_agentic: Any, quoted_pattern: str
    ) -> None:
        """Exact-query evidence strips shell quotes while retaining query arguments and existing filters."""
        command = f"codemap-py query find-symbol {quoted_pattern} --exclude-tests --limit 0 --compact 2>/dev/null"

        assert script_run_agentic._query_arguments_from_bash(command) == (
            "find-symbol",
            r"Strategy\.setup_environment$",
            "--exclude-tests",
            "--limit",
            "0",
        )

    def test_query_arguments_preserve_unquoted_legacy_arguments(self, script_run_agentic: Any) -> None:
        """Shell parsing preserves the legacy unquoted tail while dropping compact and redirection tokens."""
        assert script_run_agentic._query_arguments_from_bash(
            "scan-query symbol Trainer --with-imports --compact 2>/dev/null"
        ) == ("symbol", "Trainer", "--with-imports")

    def test_required_compliance_credits_only_codemap_entry_points(self, script_run_agentic: Any) -> None:
        """An unrelated Skill call cannot satisfy C; Codemap Skill or scan-query can."""
        assert not script_run_agentic._codemap_use_attempted(script_run_agentic.ToolCounts(skill=1))
        assert script_run_agentic._codemap_use_attempted(script_run_agentic.ToolCounts(skill=1, codemap=1))
        assert script_run_agentic._codemap_use_attempted(script_run_agentic.ToolCounts(bash=1, scan_query=1))

    def test_bash_scan_query_event_updates_agentic_telemetry(self, script_run_agentic: Any, runner: Any) -> None:
        """A provider-native Bash event records both its tool class and Codemap invocation."""
        result = script_run_agentic.BenchmarkRun(
            arm="C_strict", task_id="BA-01", task_type="fix", model="haiku", success=True
        )
        tool_use = {
            "type": "tool_use",
            "name": "Bash",
            "id": "tool-1",
            "input": {"command": "scan-query symbol Trainer"},
        }

        runner._on_tool_use(tool_use, result, {}, 1.0)

        assert result.tools.bash == 1
        assert result.tools.scan_query == 1

    def test_skill_events_distinguish_codemap_from_other_skills(self, script_run_agentic: Any, runner: Any) -> None:
        """Provider telemetry retains total Skill calls while counting only Codemap for compliance."""
        result = script_run_agentic.BenchmarkRun(
            arm="C_strict", task_id="BA-01", task_type="fix", model="haiku", success=True
        )
        event = {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Skill",
                        "id": "codemap-call",
                        "input": {"skill": "codemap-py:query-code", "args": "rdeps module"},
                    },
                    {
                        "type": "tool_use",
                        "name": "Skill",
                        "id": "other-call",
                        "input": {"skill": "develop", "args": ""},
                    },
                ]
            },
        }

        runner._handle_event(event, result, {}, set(), set(), set(), 1.0)

        assert result.tools.skill == 2
        assert result.tools.codemap == 1


# ===========================================================================
# class ToolCounts
# ===========================================================================


class TestToolCounts:
    """ToolCounts totals and read-only property."""

    @pytest.mark.parametrize(
        ("kwargs", "expected_total"),
        [
            pytest.param({}, 0, id="default-instance"),
            pytest.param({"blocked": 5, "bash_for_imports": 3}, 0, id="diagnostic-counters-excluded"),
            pytest.param({"grep": 3, "bash": 1, "semble": 2}, 6, id="grep-3-bash-1-semble-2"),  # docstring example
            pytest.param(
                {"grep": 0, "glob": 0, "bash": 0, "skill": 0, "semble": 0},
                0,
                id="grep-0-glob-0-bash-0-skill-0-semble-0",
            ),
            pytest.param({"grep": 1}, 1, id="grep-1"),
            pytest.param({"glob": 5, "skill": 3}, 8, id="glob-5-skill-3"),
            pytest.param(
                {"grep": 10, "glob": 10, "bash": 10, "skill": 10, "semble": 10},
                50,
                id="grep-10-glob-10-bash-10-skill-10-semble-10",
            ),
        ],
    )
    def test_total_sums_main_counters(self, script_run_agentic: Any, kwargs: dict, expected_total: int) -> None:
        """ToolCounts.total sums grep+glob+bash+skill+semble only.

        Scenario: user constructs ToolCounts with various counter values (including none at all); total must equal
        the sum of the five core counters. ``blocked`` and ``bash_for_imports`` are diagnostic fields excluded from
        total per the documented formula, since they are not exploration tool calls.
        """
        tc = script_run_agentic.ToolCounts(**kwargs)
        assert tc.total == expected_total

    def test_total_is_read_only_property(self, script_run_agentic: Any) -> None:
        """ToolCounts.total cannot be assigned (it is a property).

        Scenario: attempting to set total raises AttributeError; the
        docstring declares it as a property, not a settable field.
        """
        tc = script_run_agentic.ToolCounts()
        with pytest.raises(AttributeError):
            tc.total = 99  # type: ignore[misc]


# ===========================================================================
# class QualityScore
# ===========================================================================


class TestQualityScore:
    """QualityScore defaults."""

    def test_default_instance_has_scored_false(self, script_run_agentic: Any) -> None:
        """Verify that an empty quality score is unscored by default.

        Scenario: quality scoring not applicable (task has no primary_module);
        the sentinel field scored must be False, all metrics 0.
        """
        qs = script_run_agentic.QualityScore()
        assert qs.scored is False

    def test_default_numeric_fields_are_zero(self, script_run_agentic: Any) -> None:
        """All numeric fields in a default QualityScore are 0 or 0.0.

        Scenario: user inspects a blank QualityScore expecting clean zeros;
        no NaN or None values should appear in primary metric fields.
        """
        float_fields = ("erec", "rrec", "delta", "deff", "erec_top10", "precision", "recall", "f1", "leaf_recall")
        int_fields = (
            "erec_tp",
            "erec_fn",
            "rrec_tp",
            "rrec_fn",
            "erec_top10_k",
            "tp",
            "fp",
            "fn",
            "leaf_tp",
            "leaf_fn",
        )
        qs = script_run_agentic.QualityScore()
        assert {attr: getattr(qs, attr) for attr in float_fields} == dict.fromkeys(float_fields, 0.0)
        assert {attr: getattr(qs, attr) for attr in (*int_fields, "ambiguous_leaves")} == dict.fromkeys(
            (*int_fields, "ambiguous_leaves"), 0
        )

    def test_optional_skill_fields_default_to_none(self, script_run_agentic: Any) -> None:
        """skill_coverage and skill_returned default to None.

        Scenario: plain arm never invokes the Skill tool; fields must be
        None (not 0.0) to distinguish 'not applicable' from 'zero coverage'.
        """
        qs = script_run_agentic.QualityScore()
        assert qs.skill_coverage is None
        assert qs.skill_returned is None


# ===========================================================================
# class BenchmarkRun
# ===========================================================================


class TestRunCostUsd:
    """run_cost_usd returns the captured total_cost_usd — no local price table to drift."""

    def _run(self, script_run_agentic: Any, **kw: Any) -> Any:
        """Build a BenchmarkRun with the required fields plus overrides."""
        base = dict(arm="codemap", task_id="BA-01", task_type="fix", model="opus", success=True)
        base.update(kw)
        return script_run_agentic.BenchmarkRun(**base)

    def test_returns_captured_cost(self, script_run_agentic: Any) -> None:
        """The run's captured cost_usd is returned verbatim, independent of model/token counts."""
        run = self._run(script_run_agentic, cost_usd=0.1234, input_tokens=999_999, output_tokens=5_000)
        assert script_run_agentic.run_cost_usd(run) == pytest.approx(0.1234)

    def test_zero_when_cost_absent(self, script_run_agentic: Any) -> None:
        """No captured cost → 0.0, so callers omit the $ column rather than inventing a price."""
        assert script_run_agentic.run_cost_usd(self._run(script_run_agentic)) == 0.0


class TestBenchmarkRun:
    """BenchmarkRun field storage and defaults."""

    def test_required_fields_stored(self, script_run_agentic: Any) -> None:
        """BenchmarkRun stores the four required positional fields.

        Scenario: benchmark creates a run result record; arm, task_id,
        task_type and model must be accessible after construction.
        """
        run = script_run_agentic.BenchmarkRun(
            arm="plain", task_id="BA-01", task_type="fix", model="haiku", success=False
        )
        assert run.arm == "plain"
        assert run.task_id == "BA-01"
        assert run.task_type == "fix"
        assert run.model == "haiku"
        assert run.success is False

    def test_optional_fields_default_to_zero(self, script_run_agentic: Any) -> None:
        """BenchmarkRun token and timing fields default to 0 / 0.0.

        Scenario: a freshly created run before any events are parsed must
        have zero metrics to avoid polluting aggregation.
        """
        run = script_run_agentic.BenchmarkRun(
            arm="codemap", task_id="T1", task_type="fix", model="haiku", success=False
        )
        assert run.input_tokens == 0
        assert run.output_tokens == 0
        assert run.tool_result_tokens == 0
        assert run.elapsed_s == 0.0
        assert run.tool_elapsed_s == 0.0
        assert run.error == ""

    @pytest.fixture(name="fresh_run")
    def _fresh_run(self, script_run_agentic: Any) -> Any:
        """Plain-arm BenchmarkRun with only the required fields set."""
        return script_run_agentic.BenchmarkRun(arm="plain", task_id="T1", task_type="fix", model="haiku", success=False)

    def test_tools_defaults_to_empty_tool_counts(self, script_run_agentic: Any, fresh_run: Any) -> None:
        """BenchmarkRun.tools is an empty ToolCounts by default.

        Scenario: no tool calls have occurred yet; total must be 0.
        """
        assert isinstance(fresh_run.tools, script_run_agentic.ToolCounts)
        assert fresh_run.tools.total == 0

    def test_quality_defaults_to_unscored_quality_score(self, script_run_agentic: Any, fresh_run: Any) -> None:
        """BenchmarkRun.quality defaults to QualityScore(scored=False).

        Scenario: run before quality scoring is applied must not falsely
        claim scored=True.
        """
        assert isinstance(fresh_run.quality, script_run_agentic.QualityScore)
        assert fresh_run.quality.scored is False

    def test_tool_log_and_output_text_default_to_empty(self, fresh_run: Any) -> None:
        """tool_log and output_text start as empty collections / strings."""
        assert fresh_run.tool_log == []
        assert fresh_run.output_text == ""

    @pytest.mark.parametrize("arm", ["plain", "codemap", "semble", "combined"])
    def test_accepted_arm_values(self, script_run_agentic: Any, arm: str) -> None:
        """BenchmarkRun stores all four documented arm identifiers.

        Scenario: the benchmark creates one BenchmarkRun per arm; all four
        documented values must be stored without error.
        """
        run = script_run_agentic.BenchmarkRun(arm=arm, task_id="T1", task_type="fix", model="haiku", success=True)
        assert run.arm == arm


# ===========================================================================
# find_index
# ===========================================================================


class TestFindIndex:
    """find_index resolution order and failure when no index exists."""

    def test_explicit_path_returned_resolved(self, script_run_agentic: Any, tmp_path: Path) -> None:
        """find_index returns the explicit path resolved when supplied.

        Scenario: user passes ``--index /some/path``; the function must return
        that exact path (resolved) without searching .cache/.
        """
        index = tmp_path / "my-index.json"
        index.write_text("{}")
        result = script_run_agentic.find_index(tmp_path, index)
        assert result == index.resolve()

    @pytest.mark.parametrize(
        ("cache_dir", "filename"),
        [
            pytest.param("codemap", "myrepo.json", id="preferred-name-in-codemap-cache"),
            pytest.param("codemap", "other.json", id="first-json-fallback-in-codemap-cache"),
            pytest.param("scan", "myrepo.json", id="legacy-scan-cache-when-codemap-empty"),
        ],
    )
    def test_discovers_index_in_cache_dir(
        self, script_run_agentic: Any, tmp_path: Path, cache_dir: str, filename: str
    ) -> None:
        """find_index discovers the index under ``.cache/codemap/`` or the legacy ``.cache/scan/``.

        Scenario: the repo is named ``myrepo``. A file named after the repo is preferred inside
        ``.cache/codemap/``; when only ``other.json`` exists there, the lexicographically first ``*.json`` is
        returned; when ``.cache/codemap/`` has no JSON, the legacy ``.cache/scan/`` directory is searched.
        """
        repo = tmp_path / "myrepo"
        repo.mkdir()
        cache = repo / ".cache" / cache_dir
        cache.mkdir(parents=True)
        idx = cache / filename
        idx.write_text("{}")
        result = script_run_agentic.find_index(repo, None)
        assert result == idx.resolve()

    def test_raises_file_not_found_when_no_index_exists(self, script_run_agentic: Any, tmp_path: Path) -> None:
        """find_index raises FileNotFoundError when no index is found anywhere.

        Scenario: user forgot to run scan-index; the function must raise
        FileNotFoundError with an actionable message (per Raises: doc).
        """
        repo = tmp_path / "emptyrepo"
        repo.mkdir()
        with pytest.raises(FileNotFoundError, match="No codemap index found"):
            script_run_agentic.find_index(repo, None)

    def test_codemap_cache_preferred_over_scan_cache(self, script_run_agentic: Any, tmp_path: Path) -> None:
        """find_index checks .cache/codemap/ before .cache/scan/.

        Scenario: both cache dirs contain a JSON file; the one under
        .cache/codemap/ must take priority per documented search order.
        """
        repo = tmp_path / "myrepo"
        repo.mkdir()
        codemap_cache = repo / ".cache" / "codemap"
        codemap_cache.mkdir(parents=True)
        scan_cache = repo / ".cache" / "scan"
        scan_cache.mkdir(parents=True)
        codemap_idx = codemap_cache / "myrepo.json"
        codemap_idx.write_text("{}")
        scan_idx = scan_cache / "myrepo.json"
        scan_idx.write_text("{}")
        result = script_run_agentic.find_index(repo, None)
        assert result == codemap_idx.resolve()


# ===========================================================================
# count_tokens
# ===========================================================================


class TestCountTokens:
    """count_tokens return type and monotonicity."""

    def test_empty_string_returns_non_negative_int(self, script_run_agentic: Any) -> None:
        """count_tokens on an empty string returns 0 (tiktoken encodes nothing).

        Scenario: tool result with no content; token count must be a
        non-negative integer so it does not corrupt running totals.
        """
        result = script_run_agentic.count_tokens("")
        assert isinstance(result, int)
        assert result >= 0

    def test_non_empty_string_returns_positive_count(self, script_run_agentic: Any) -> None:
        """count_tokens on non-empty text returns a positive integer.

        Scenario: a real tool result string; token estimate must be > 0.
        """
        result = script_run_agentic.count_tokens("hello world")
        assert result > 0

    def test_longer_text_has_higher_count_than_shorter(self, script_run_agentic: Any) -> None:
        """Longer text produces a higher token count than shorter text.

        Scenario: comparing two tool results of different lengths; the
        longer one must always produce a higher count (monotonicity).
        """
        short = "hello"
        long_text = "hello world this is a much longer sentence with many more tokens"
        assert script_run_agentic.count_tokens(long_text) > script_run_agentic.count_tokens(short)

    def test_returns_integer_type(self, script_run_agentic: Any) -> None:
        """count_tokens always returns int, not float.

        Scenario: the return value is added to BenchmarkRun.tool_result_tokens
        (an int field); non-integer would cause a type error at runtime.
        """
        result = script_run_agentic.count_tokens("some text")
        assert isinstance(result, int)


# ===========================================================================
# GroundTruth — _generate_match_set (static, pure)
# ===========================================================================


class TestGroundTruthGenerateMatchSet:
    """Tests for the static pattern-generation helper via its observable effects."""

    @pytest.mark.parametrize(
        ("module", "corpus", "should_match"),
        [
            # Full dotted path must match
            pytest.param(
                "lightning.pytorch.trainer.trainer",
                "lightning.pytorch.trainer.trainer",
                True,
                id="lightning.pytorch.trainer.trainer-lightning.pytorch.trainer.trainer",
            ),
            # File path form must match
            pytest.param(
                "lightning.pytorch.trainer.trainer",
                "lightning/pytorch/trainer/trainer.py",
                True,
                id="lightning.pytorch.trainer.trainer-lightning-pytorch-trainer-trainer.py",
            ),
            # src/ file path form must match
            pytest.param(
                "lightning.pytorch.trainer.trainer",
                "src/lightning/pytorch/trainer/trainer.py",
                True,
                id="lightning.pytorch.trainer.trainer-src-lightning-pytorch-trainer-trainer.py",
            ),
            # 2-component suffix dotted must match
            pytest.param(
                "lightning.pytorch.trainer.trainer",
                "trainer.trainer",
                True,
                id="lightning.pytorch.trainer.trainer-trainer.trainer",
            ),
            # 2-component suffix slash must match
            pytest.param(
                "lightning.pytorch.trainer.trainer",
                "trainer/trainer",
                True,
                id="lightning.pytorch.trainer.trainer-trainer-trainer",
            ),
            # 3-component suffix must match
            pytest.param(
                "lightning.pytorch.trainer.trainer",
                "pytorch.trainer.trainer",
                True,
                id="lightning.pytorch.trainer.trainer-pytorch.trainer.trainer",
            ),
            # Bare leaf name must NOT match (enforced minimum 2 components)
            pytest.param(
                "lightning.pytorch.trainer.trainer", "trainer", False, id="lightning.pytorch.trainer.trainer-trainer"
            ),
            # Unrelated text must not match
            pytest.param(
                "lightning.pytorch.trainer.trainer",
                "completely unrelated text",
                False,
                id="lightning.pytorch.trainer.trainer-completely-unrelated-text",
            ),
        ],
    )
    def test_pattern_matches_expected_forms(
        self, script_run_agentic: Any, module: str, corpus: str, should_match: bool
    ) -> None:
        """_generate_match_set creates patterns matching documented surface forms.

        Scenario: the docstring states at least 2 path components are required
        and lists specific forms; each form is tested for correct match/no-match.
        """
        patterns = script_run_agentic.GroundTruth._generate_match_set(module)
        matched = any(p.search(corpus) for p in patterns)
        assert matched == should_match, (
            f"module={module!r}, corpus={corpus!r}: expected match={should_match}, got match={matched}"
        )


# ===========================================================================
# GroundTruth — score()
# ===========================================================================


def _score_corpus(
    ground_truth: Any, corpus: str = "", *, task_id: str = "BA-01", report: str | None = None, **kwargs: Any
) -> Any:
    """Score ``task_id`` with ``corpus`` as output and exposure text; ``report`` overrides the report corpus."""
    return ground_truth.score(
        task_id=task_id,
        output_text=corpus,
        exposure_corpus=corpus,
        report_corpus=corpus if report is None else report,
        **kwargs,
    )


class TestGroundTruthScore:
    """GroundTruth.score recall, efficiency and skill-coverage contracts."""

    def test_returns_unscored_when_no_ground_truth_for_task(self, ground_truth: Any) -> None:
        """Leave a task unscored when it has no expected reverse dependencies.

        Scenario: task_id unknown to the index; score() must return an
        unscored sentinel per the documented contract.
        """
        result = _score_corpus(ground_truth, "some output", task_id="NONEXISTENT")
        assert result.scored is False
        assert result.erec == 0.0
        assert result.rrec == 0.0

    @pytest.mark.parametrize(
        ("corpus", "expected_recall"),
        [
            pytest.param(
                "lightning.pytorch.trainer.trainer lightning.pytorch.loops.fit_loop", 1.0, id="all-rdeps-found"
            ),
            pytest.param("I could not find the answer.", 0.0, id="no-rdeps-found"),
            pytest.param("lightning.pytorch.trainer.trainer", 0.5, id="one-of-two-rdeps-found"),
        ],
    )
    def test_recall_is_the_fraction_of_expected_rdeps_found(
        self, ground_truth: Any, corpus: str, expected_recall: float
    ) -> None:
        """Report exposure and report recall as the share of expected reverse dependencies in the corpus.

        Scenario: the index expects two reverse dependencies; the agent's output mentions all, none or one of them.
        The same text is both the exposure and the report corpus, so both recall figures must agree.
        """
        result = _score_corpus(ground_truth, corpus)
        assert result.scored is True
        assert result.erec == pytest.approx(expected_recall)
        assert result.rrec == pytest.approx(expected_recall)

    @pytest.mark.parametrize(
        ("found_count", "expected_recall"),
        [pytest.param(7, 0.7, id="7"), pytest.param(6, 0.6, id="6"), pytest.param(10, 1.0, id="10")],
    )
    def test_recall_boundary_values_are_exact(
        self, script_run_agentic: Any, tmp_path: Path, found_count: int, expected_recall: float
    ) -> None:
        """Preserve exact recall fractions around the downstream acceptance boundary."""
        primary = "pkg.primary"
        callers = [f"pkg.caller{i}" for i in range(10)]
        data = _minimal_index(
            [{"name": primary, "direct_imports": [], "dep_count": 0, "status": "ok"}]
            + [{"name": caller, "direct_imports": [primary], "dep_count": 0, "status": "ok"} for caller in callers]
        )
        index_file = tmp_path / "index.json"
        index_file.write_text(json.dumps(data))
        task = _make_task(script_run_agentic, id="BND-01", primary_module=primary)
        ground_truth = script_run_agentic.GroundTruth(index_file, [task])

        result = _score_corpus(ground_truth, " ".join(callers[:found_count]), task_id="BND-01")

        assert result.scored is True
        assert result.erec == pytest.approx(expected_recall)
        assert result.rrec == pytest.approx(expected_recall)

    def test_delta_equals_erec_minus_rrec(self, ground_truth: Any) -> None:
        """Verify that the score delta measures the information gap.

        Scenario: agent exposes module in tool output but omits it from final
        answer; delta must reflect the gap.
        """
        exposure = "lightning.pytorch.trainer.trainer lightning.pytorch.loops.fit_loop"
        result = _score_corpus(ground_truth, exposure, report="lightning.pytorch.trainer.trainer")  # one in answer
        assert result.scored is True
        assert result.delta == pytest.approx(result.erec - result.rrec)

    @pytest.mark.parametrize(
        ("tool_calls", "denominator"),
        [
            pytest.param(4, 4, id="four-tool-calls"),
            pytest.param(0, 1, id="zero-tool-calls-uses-denominator-one"),
        ],
    )
    def test_deff_equals_erec_tp_divided_by_tool_calls(
        self, ground_truth: Any, tool_calls: int, denominator: int
    ) -> None:
        """Verify that discovery efficiency accounts for the tool-call count without dividing by zero.

        Scenario: agent uses 4 tool calls and finds 1 rdep; deff = 1/4 = 0.25. With tool_calls=0 (e.g. an arm that
        produced no calls) deff must equal erec_tp / 1 (not raise ZeroDivisionError).
        """
        result = _score_corpus(ground_truth, "lightning.pytorch.trainer.trainer", tool_calls=tool_calls)
        assert result.scored is True
        assert result.deff == pytest.approx(result.erec_tp / denominator)

    @pytest.mark.parametrize(
        "test_module",
        [
            pytest.param(
                {"name": "tests.test_thing", "direct_imports": ["lightning.pytorch.core.thing"]},
                id="tests-name-prefix",
            ),
            pytest.param(
                {
                    "name": "tests_pytorch.test_thing",
                    "direct_imports": ["lightning.pytorch.core.thing"],
                    "is_test": True,
                },
                id="is-test-flag-outside-tests-prefix",
            ),
        ],
    )
    def test_test_modules_excluded_from_expected_rdeps(
        self, script_run_agentic: Any, tmp_path: Path, test_module: dict
    ) -> None:
        """GroundTruth excludes test modules (tests prefix or is_test flag) from expected.

        Scenario: the index has a test module importing primary_module; it must
        not appear in the expected rdeps set (production callers only, per docs).
        The scanner's is_test flag catches roots like tests_pytorch.* that the
        name-prefix rule misses (BA-16 gt-divergence regression).
        """
        data = _minimal_index(
            [
                {
                    "name": "lightning.pytorch.core.thing",
                    "direct_imports": [],
                    "dep_count": 0,
                    "status": "ok",
                },
                {"dep_count": 0, "status": "ok", **test_module},
            ]
        )
        index_file = tmp_path / "index.json"
        index_file.write_text(json.dumps(data))
        task = _make_task(script_run_agentic, id="X1", primary_module="lightning.pytorch.core.thing")
        gt = script_run_agentic.GroundTruth(index_file, [task])
        assert test_module["name"] not in gt.expected.get("X1", set())

    def test_skill_coverage_parsed_from_json_result(self, ground_truth: Any) -> None:
        """Compute skill coverage from a valid structural-query result.

        Scenario: codemap skill returns JSON with 'imported_by' list
        containing one of two expected rdeps; skill_coverage must be 0.5.
        """
        skill_json = json.dumps({"imported_by": ["lightning.pytorch.trainer.trainer"]})
        result = _score_corpus(ground_truth, skill_result_text=skill_json)
        assert result.scored is True
        assert result.skill_coverage == pytest.approx(0.5)
        assert result.skill_returned == 1

    def test_skill_coverage_unions_multiple_one_line_json_results(self, ground_truth: Any) -> None:
        """Combine imported modules across multiple newline-delimited query results.

        Scenario: the agent ran scan-query rdeps more than once, so skill_result_text
        holds two one-line JSON objects joined by a newline — whole-text json.loads
        fails with "Extra data", which previously yielded sc=None for every
        multi-call run; each expected rdep appears in exactly one result.
        """
        skill_json = (
            json.dumps({"module": "m1", "imported_by": ["lightning.pytorch.trainer.trainer"]})
            + "\n"
            + json.dumps({"module": "m2", "imported_by": ["lightning.pytorch.loops.fit_loop"]})
        )
        result = _score_corpus(ground_truth, skill_result_text=skill_json)
        assert result.skill_coverage == pytest.approx(1.0)
        assert result.skill_returned == 2

    def test_skill_coverage_is_none_when_skill_result_text_absent(self, ground_truth: Any) -> None:
        """Leave skill coverage unset when no structural-query result is provided.

        Scenario: plain arm has no skill result; skill_coverage must remain
        None (not 0.0) to distinguish 'not applicable' from 'zero coverage'.
        """
        result = _score_corpus(ground_truth, skill_result_text=None)
        assert result.skill_coverage is None
        assert result.skill_returned is None

    @pytest.mark.parametrize(
        "skill_result_text",
        [
            pytest.param("not valid json at all", id="malformed-json"),
            pytest.param(json.dumps({"some_other_key": []}), id="imported-by-key-missing"),
        ],
    )
    def test_skill_coverage_is_none_for_unusable_query_result(self, ground_truth: Any, skill_result_text: str) -> None:
        """Leave skill coverage unset when the structural-query result cannot yield imported modules.

        Scenario: the skill returned an error or prose instead of JSON, or valid JSON for a non-rdeps query (e.g.
        deps) lacking ``imported_by``; skill_coverage must stay None rather than raising.
        """
        result = _score_corpus(ground_truth, skill_result_text=skill_result_text)
        assert result.skill_coverage is None

    def test_matching_ignores_letter_case(self, ground_truth: Any) -> None:
        """Match module names without regard to letter case.

        Scenario: documentation states patterns are case-insensitive;
        upper-case corpus text must still count as a match.
        """
        result = _score_corpus(ground_truth, "LIGHTNING.PYTORCH.TRAINER.TRAINER")
        assert result.scored is True
        assert result.erec_tp >= 1


# ===========================================================================
# GroundTruth — _extract_modules (classmethod, pure)
# ===========================================================================


class TestGroundTruthExtractModules:
    """Tests for the module extractor whose package set is derived from the tasks."""

    @pytest.mark.parametrize(
        ("text", "expected_subset"),
        [
            pytest.param("lightning.pytorch.trainer.trainer", {"lightning.pytorch.trainer.trainer"}, id="dotted"),
            pytest.param(
                "src/lightning/pytorch/trainer/trainer.py",
                {"lightning.pytorch.trainer.trainer"},
                id="src-path",
            ),
            pytest.param("unrelated text with no lightning modules", set(), id="no-match"),
        ],
    )
    def test_extracts_lightning_module_names(self, ground_truth: Any, text: str, expected_subset: set) -> None:
        """_extract_modules extracts dotted lightning.* names and converts paths.

        Scenario: agent output contains dotted module names or file paths;
        the extractor must return the canonical dotted form in both cases.
        """
        result = ground_truth._extract_modules(text)
        assert expected_subset <= result

    def test_generalizes_to_non_lightning_package(self, tmp_index: Path, script_run_agentic: Any) -> None:
        """_extract_modules derives its package from the task, not a hardcoded 'lightning'.

        Scenario: a repo whose primary module is ``torch.*`` must be extracted, while
        the legacy hardcoded ``lightning`` prefix must NOT leak in as a match.
        """
        task = _make_task(script_run_agentic, id="T-torch", primary_module="torch.nn.modules.conv")
        gt = script_run_agentic.GroundTruth(tmp_index, [task])
        text = "see torch.nn.functional and src/torch/optim/adam.py but lightning.pytorch.trainer is off-repo"
        result = gt._extract_modules(text)
        assert {"torch.nn.functional", "torch.optim.adam"} <= result
        assert not any(m.startswith("lightning") for m in result)


# ===========================================================================
# aggregate
# ===========================================================================


class TestAggregate:
    """Aggregate groups, medians and failure accounting per task, arm and model."""

    def _make_run(
        self,
        script_agentic: Any,
        task_id: str,
        arm: str,
        model: str,
        success: bool,
        total_tools: int = 4,
        input_tokens: int = 1000,
        elapsed_s: float = 10.0,
    ) -> Any:
        """Build a minimal BenchmarkRun for aggregation tests."""
        run = script_agentic.BenchmarkRun(
            arm=arm,
            task_id=task_id,
            task_type="fix",
            model=model,
            success=success,
        )
        run.tools.grep = total_tools
        run.input_tokens = input_tokens
        run.elapsed_s = elapsed_s
        return run

    def _one_success_two_failures_cell(self, script_agentic: Any) -> dict:
        """Aggregate one fast success and two wall-clock failures into the codemap cell."""
        good = self._make_run(script_agentic, "T01", "codemap", "haiku", success=True, elapsed_s=5.0)
        bad_one = self._make_run(script_agentic, "T01", "codemap", "haiku", success=False, elapsed_s=300.0)
        bad_two = self._make_run(script_agentic, "T01", "codemap", "haiku", success=False, elapsed_s=300.0)
        out = script_agentic.aggregate([good, bad_one, bad_two], ["T01"], model_short="haiku")
        return out["T01"]["codemap"]

    def test_returns_empty_dict_for_empty_results(self, script_run_agentic: Any) -> None:
        """Aggregate returns empty nested dicts when results list is empty.

        Scenario: benchmark run with no completed tasks; aggregate must
        return a dict keyed by task_id with no arm data.
        """
        out = script_run_agentic.aggregate([], ["T01"], model_short=None)
        assert out == {"T01": {}}

    def test_single_run_median_equals_its_value(self, script_run_agentic: Any) -> None:
        """Aggregate computes median metrics from a single successful run.

        Scenario: one run per (task, arm) cell; median equals the single
        value and must be present in the output dict.
        """
        run = self._make_run(script_run_agentic, "T01", "plain", "haiku", success=True, input_tokens=2000)
        out = script_run_agentic.aggregate([run], ["T01"], model_short=None)
        assert "plain" in out["T01"]
        assert out["T01"]["plain"]["input_tokens"] == pytest.approx(2000.0)

    def test_model_filter_excludes_other_models(self, script_run_agentic: Any) -> None:
        """Exclude runs from other model tiers when aggregating one tier.

        Scenario: results contain haiku and sonnet runs; filtering to haiku
        must not expose sonnet metrics in the output.
        """
        haiku_run = self._make_run(script_run_agentic, "T01", "plain", "haiku", success=True, input_tokens=100)
        sonnet_run = self._make_run(script_run_agentic, "T01", "plain", "sonnet", success=True, input_tokens=999)
        out = script_run_agentic.aggregate([haiku_run, sonnet_run], ["T01"], model_short="haiku")
        assert out["T01"]["plain"]["input_tokens"] == pytest.approx(100.0)

    def test_failed_runs_excluded_from_median(self, script_run_agentic: Any) -> None:
        """Aggregate excludes success=False runs from median computation.

        Scenario: cell contains one successful and one failed run; median
        must be derived from the successful run only.
        """
        good = self._make_run(script_run_agentic, "T01", "codemap", "haiku", success=True, elapsed_s=5.0)
        bad = self._make_run(script_run_agentic, "T01", "codemap", "haiku", success=False, elapsed_s=300.0)
        out = script_run_agentic.aggregate([good, bad], ["T01"], model_short="haiku")
        assert out["T01"]["codemap"]["elapsed_s"] == pytest.approx(5.0)

    def test_success_rate_reflects_pass_fraction(self, script_run_agentic: Any) -> None:
        """Compute each cell's success rate from its completed runs.

        Scenario: 1 success and 1 failure in same cell; success_rate = 0.5.
        """
        good = self._make_run(script_run_agentic, "T01", "plain", "haiku", success=True)
        bad = self._make_run(script_run_agentic, "T01", "plain", "haiku", success=False)
        out = script_run_agentic.aggregate([good, bad], ["T01"], model_short="haiku")
        assert out["T01"]["plain"]["success_rate"] == pytest.approx(0.5)

    def test_all_success_false_reports_spend_without_quality_medians(self, script_run_agentic: Any) -> None:
        """Aggregate keeps an all-failed cell's spend but computes no success-only medians.

        Scenario: every run in the cell timed out. The cell used to collapse to {},
        hiding the paid timeouts entirely; it must now report the run/failure counts
        and all-runs spend while the success-only medians stay absent.
        """
        bad = self._make_run(script_run_agentic, "T01", "plain", "haiku", success=False, elapsed_s=300.0)
        out = script_run_agentic.aggregate([bad], ["T01"], model_short="haiku")
        cell = out["T01"]["plain"]
        assert cell["n_runs"] == 1
        assert cell["n_failures"] == 1
        assert cell["success_rate"] == pytest.approx(0.0)
        assert cell["elapsed_s_all"] == pytest.approx(300.0)
        assert "elapsed_s" not in cell
        assert "erec" not in cell

    def test_failure_count_accompanies_success_only_medians(self, script_run_agentic: Any) -> None:
        """Aggregate reports how many runs failed next to the medians they were excluded from.

        Scenario: two of three runs failed; the median is still the survivor's, but the
        cell must state that it rests on one run out of three.
        """
        cell = self._one_success_two_failures_cell(script_run_agentic)
        assert cell["elapsed_s"] == pytest.approx(5.0)
        assert cell["n_runs"] == 3
        assert cell["n_failures"] == 2

    def test_all_runs_aggregates_include_failed_runs(self, script_run_agentic: Any) -> None:
        """Aggregate's ``*_all`` metrics span every run, so a failure-heavy arm cannot read as cheap.

        Scenario: one fast success and two wall-clock failures; the success-only median
        elapsed time is the survivor's, while the all-runs median reflects the timeouts.
        """
        cell = self._one_success_two_failures_cell(script_run_agentic)
        assert cell["elapsed_s"] == pytest.approx(5.0)
        assert cell["elapsed_s_all"] == pytest.approx(300.0)
        assert cell["input_tokens_all"] == pytest.approx(1000.0)

    def test_groups_metrics_per_task(self, script_run_agentic: Any) -> None:
        """Aggregate groups metrics per task_id independently.

        Scenario: results for T01 and T02; each task's arm dict must
        reflect only that task's runs.
        """
        run_t1 = self._make_run(script_run_agentic, "T01", "plain", "haiku", success=True, input_tokens=100)
        run_t2 = self._make_run(script_run_agentic, "T02", "plain", "haiku", success=True, input_tokens=999)
        out = script_run_agentic.aggregate([run_t1, run_t2], ["T01", "T02"], model_short="haiku")
        assert out["T01"]["plain"]["input_tokens"] == pytest.approx(100.0)
        assert out["T02"]["plain"]["input_tokens"] == pytest.approx(999.0)


# ===========================================================================
# parse_claude_readcrop_events — unscoreable-cell recall policy
# ===========================================================================


class TestReadcropUnscoreableRecallFields:
    """An unscoreable read-crop answer must report no recall measurement at all."""

    @staticmethod
    def _row_without_answer_envelope(script_agentic: Any) -> dict:
        """Parse a completed run whose output carries no strict answer envelope."""
        contract = script_agentic.build_readcrop_contract(
            {
                "id": "RC-fixture",
                "type": "read_crop",
                "prompt": "Describe Example.method.",
                "symbol": "Example.method",
                "expected_keywords": ["value"],
            },
            source="def method(self, value: int) -> None:\n    pass\n",
        )
        events = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "no envelope here"}]}},
            {
                "type": "result",
                "subtype": "success",
                "usage": {
                    "input_tokens": 10,
                    "cache_creation_input_tokens": 2,
                    "cache_read_input_tokens": 1,
                    "output_tokens": 3,
                },
            },
        ]
        return script_agentic.parse_claude_readcrop_events(events, arm="A_plain", contract=contract)

    def test_every_recall_field_is_none_when_the_answer_cannot_be_scored(self, script_run_agentic: Any) -> None:
        """No recall field may hold 0.0 on an unscoreable cell — that asserts a measurement never taken.

        Scenario: a completed run with no answer envelope. Two fields used to be 0.0
        and two None, so the same failure was averaged into two means and omitted from
        the other two. Mirrors the Codex lane policy in _bench_codex/stage_readcrop.py.
        """
        row = self._row_without_answer_envelope(script_run_agentic)

        assert row["answer_error"] == "missing strict read-crop answer envelope"
        assert row["parameter_recall"] is None
        assert row["behavior_fact_recall"] is None
        assert row["behavior_facts_correct"] is None
        assert row["keyword_recall_diagnostic"] is None

    def test_the_unscoreable_cell_still_carries_its_failure(self, script_run_agentic: Any) -> None:
        """None recall must never read as a passing cell — success and primary_correct stay False."""
        row = self._row_without_answer_envelope(script_run_agentic)

        assert row["success"] is False
        assert row["primary_correct"] is False
        assert row["quality_score"] is None
        assert row["quality_components"] == {}


# ===========================================================================
# _iter_combos — execution order
# ===========================================================================


class TestIterCombosArmOrder:
    """Canonical agentic blocks must be counterbalanced, not run in a fixed A→B→C order."""

    @staticmethod
    def _orders(script_agentic: Any, tasks: list[Any], arms: list[str], repeat: int) -> list[list[str]]:
        """Return the arm sequence of each (task, model, repetition) block, in execution order."""
        blocks: list[list[str]] = []
        for index, (_task, _model_short, _model_id, arm, _rep) in enumerate(
            script_agentic._iter_combos(tasks, [("haiku", "id")], arms, repeat)
        ):
            if index % len(arms) == 0:
                blocks.append([])
            blocks[-1].append(arm)
        return blocks

    def test_canonical_arms_follow_the_shared_deterministic_order(self, script_run_agentic: Any) -> None:
        """Each canonical block runs in the revision-bound order the structural lanes use.

        Scenario: two tasks, one model, one repetition; every block must match
        ``deterministic_arm_order`` for its own coordinates rather than the declared list.
        """
        tasks = [
            _make_task(script_run_agentic, id="BA-01", experiment_revision=ACTIVE_REVISION),
            _make_task(script_run_agentic, id="BA-03", experiment_revision=ACTIVE_REVISION),
        ]
        arms = list(script_run_agentic.AGENTIC_ARMS)

        blocks = self._orders(script_run_agentic, tasks, arms, repeat=1)

        assert blocks == [
            list(script_run_agentic.deterministic_arm_order(ACTIVE_REVISION, "claude", "haiku", "BA-01", 1)),
            list(script_run_agentic.deterministic_arm_order(ACTIVE_REVISION, "claude", "haiku", "BA-03", 1)),
        ]
        assert blocks != [arms, arms]

    def test_repetitions_of_one_cell_do_not_replay_a_single_order(self, script_run_agentic: Any) -> None:
        """The repetition index is an ordering coordinate, so repeated blocks differ.

        Scenario: one task run twice; the two blocks must carry the two distinct
        repetition-keyed orders rather than the same sequence twice.
        """
        tasks = [_make_task(script_run_agentic, id="BA-01", experiment_revision=ACTIVE_REVISION)]
        arms = list(script_run_agentic.AGENTIC_ARMS)

        blocks = self._orders(script_run_agentic, tasks, arms, repeat=2)

        assert blocks == [
            list(script_run_agentic.deterministic_arm_order(ACTIVE_REVISION, "claude", "haiku", "BA-01", 1)),
            list(script_run_agentic.deterministic_arm_order(ACTIVE_REVISION, "claude", "haiku", "BA-01", 2)),
        ]

    @pytest.mark.parametrize(
        "arms",
        [
            pytest.param(["A_plain"], id="single-canonical-arm"),
            pytest.param(["plain", "codemap"], id="legacy-arm-pair"),
        ],
    )
    def test_non_canonical_arm_sets_keep_the_declared_order(self, script_run_agentic: Any, arms: list[str]) -> None:
        """Arm sets with no shared ordering policy run exactly as the caller declared them."""
        tasks = [_make_task(script_run_agentic, id="BA-01", experiment_revision=ACTIVE_REVISION)]

        blocks = self._orders(script_run_agentic, tasks, arms, repeat=1)

        assert blocks == [arms]

    def test_every_cell_is_still_scheduled_exactly_once(self, script_run_agentic: Any) -> None:
        """Counterbalancing reorders the matrix without adding or dropping a cell."""
        tasks = [
            _make_task(script_run_agentic, id="BA-01", experiment_revision=ACTIVE_REVISION),
            _make_task(script_run_agentic, id="BA-02", experiment_revision=ACTIVE_REVISION),
        ]
        arms = list(script_run_agentic.AGENTIC_ARMS)

        combos = list(script_run_agentic._iter_combos(tasks, [("haiku", "id")], arms, 2))

        assert sorted((task.id, model, arm, rep) for task, model, _id, arm, rep in combos) == sorted(
            (task.id, "haiku", arm, rep) for task in tasks for arm in arms for rep in range(2)
        )


# ===========================================================================
# check_semble_mcp
# ===========================================================================


class TestCheckSembleMcp:
    """check_semble_mcp fails with actionable errors and passes when semble is registered."""

    def test_raises_runtime_error_when_semble_not_installed(
        self, script_run_agentic: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """check_semble_mcp raises RuntimeError when semble package missing.

        Scenario: benchmark operator forgets to install semble; the function
        must raise RuntimeError with an actionable install message (per
        Raises: docstring).
        """
        monkeypatch.setitem(sys.modules, "semble", None)

        with pytest.raises((RuntimeError, ImportError)):
            script_run_agentic.check_semble_mcp()

    @mock.patch("subprocess.run", autospec=True, return_value=MagicMock(returncode=1, stdout="", stderr="not found"))
    def test_raises_runtime_error_when_claude_mcp_get_fails(
        self, mock_run: mock.Mock, script_run_agentic: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """check_semble_mcp raises RuntimeError when 'claude mcp get semble' fails.

        Scenario: semble is installed but not registered as an MCP server;
        the subprocess returns non-zero; RuntimeError with register instructions
        must be raised.
        """
        monkeypatch.setitem(sys.modules, "semble", MagicMock())

        with pytest.raises(RuntimeError, match="semble MCP server not configured"):
            script_run_agentic.check_semble_mcp()

    @mock.patch("subprocess.run", autospec=True, return_value=MagicMock(returncode=0, stdout="semble", stderr=""))
    def test_passes_silently_when_semble_installed_and_mcp_registered(
        self, mock_run: mock.Mock, script_run_agentic: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """check_semble_mcp returns None (no exception) when both checks pass.

        Scenario: operator has semble installed and registered; function
        must complete without raising.
        """
        monkeypatch.setitem(sys.modules, "semble", MagicMock())

        result = script_run_agentic.check_semble_mcp()

        assert result is None


# ===========================================================================
# ModelRunner — _ARM_ALLOWED / _ARM_DISALLOWED (headless -p pre-approval)
# ===========================================================================


class TestArmToolPermissions:
    """Each arm's primary discriminator must be pre-approved, else it is denied in -p mode.

    Regression guard: the codemap/combined arms' primary tool is the /codemap:query-code Skill.
    When it was missing from ``--allowedTools`` every codemap Skill call returned <tool_use_error>
    (permission denied), the run fell back to grep, and the arm scored codemap_skill_errored —
    silently unmeasurable. Both plugin namespaces (codemap, codemap-py) must be allowed since the
    agent may invoke either.
    """

    @staticmethod
    def _allowed(script_run_agentic: Any, arm: str) -> str:
        """Return the ``--allowedTools`` value string for *arm* (empty when the arm has none)."""
        flags = script_run_agentic.ModelRunner._ARM_ALLOWED.get(arm, [])
        return flags[1] if len(flags) == 2 else ""

    @pytest.mark.parametrize("arm", ["codemap", "combined"])
    def test_skill_preapproved_for_skill_arms(self, script_run_agentic: Any, arm: str) -> None:
        """Codemap + combined arms pre-approve the /codemap:query-code Skill in both namespaces."""
        allowed = self._allowed(script_run_agentic, arm)
        assert "Skill(codemap:query-code)" in allowed
        assert "Skill(codemap-py:query-code)" in allowed

    @pytest.mark.parametrize("arm", ["codemap", "combined"])
    def test_scan_query_still_preapproved(self, script_run_agentic: Any, arm: str) -> None:
        """The scan-query CLI stays pre-approved alongside the Skill for the codemap/combined arms."""
        assert "Bash(scan-query:*)" in self._allowed(script_run_agentic, arm)

    def test_semble_arm_does_not_preapprove_skill(self, script_run_agentic: Any) -> None:
        """Semble arm must not pre-approve the Skill — it hard-blocks it as the control discriminator."""
        assert "Skill(" not in self._allowed(script_run_agentic, "semble")
        disallowed = script_run_agentic.ModelRunner._ARM_DISALLOWED["semble"]
        assert any("Skill" in tok for tok in disallowed)

    def test_canonical_plain_explicitly_blocks_scan_query(self, script_run_agentic: Any) -> None:
        """A_plain keeps Codemap inaccessible even when scan-query already exists on the host PATH."""
        disallowed = script_run_agentic.ModelRunner._ARM_DISALLOWED["A_plain"]
        assert "Bash(scan-query:*)" in disallowed[1]

    @pytest.mark.parametrize("arm", ["B_auto", "C_strict"])
    def test_canonical_treatments_preapprove_the_path_codemap_query_cli(
        self, script_run_agentic: Any, arm: str
    ) -> None:
        """The direct frozen query is not denied by Claude's outer Bash policy."""
        allowed = script_run_agentic.ModelRunner._ARM_ALLOWED[arm][1]

        assert "Bash(codemap-py query:*)" in allowed
        assert "Bash(*/bin/codemap-py* query:*)" in allowed

    @pytest.mark.parametrize("arm", ["A_plain", "B_auto", "C_strict"])
    def test_canonical_cells_disallow_unmetered_nested_agents(self, script_run_agentic: Any, arm: str) -> None:
        """Parent-row token accounting remains complete by excluding child sessions."""
        disallowed = script_run_agentic.ModelRunner._ARM_DISALLOWED[arm][1]

        assert "Agent" in disallowed
        assert "Task" in disallowed


# ===========================================================================
# ModelRunner — config isolation (``--setting-sources`` / ``--plugin-dir`` / ``--mcp-config``)
# ===========================================================================


class TestConfigIsolation:
    """The subprocess excludes user config, then re-supplies each arm's tool under test.

    Excluding user-level config (``--setting-sources project,local``) strips the caveman plugin, foundry Re:Anchor, user
    CLAUDE.md and hooks so the agent's output is not shaped/inflated by the operator's setup. It also drops the codemap
    plugin (needed for the Skill) and semble MCP, which _arm_isolation_flags re-supplies per arm.
    """

    def test_base_cmd_excludes_user_config(self, script_run_agentic: Any) -> None:
        """_CMD passes ``--setting-sources project,local`` so USER config never loads."""
        cmd = script_run_agentic.ModelRunner._CMD
        assert "--setting-sources" in cmd
        assert cmd[cmd.index("--setting-sources") + 1] == "project,local"

    def test_base_cmd_disables_session_persistence(self, script_run_agentic: Any) -> None:
        """Every agentic cell starts a non-resumable Claude process session."""
        cmd = script_run_agentic.ModelRunner._CMD
        assert "--no-session-persistence" in cmd
        assert "--continue" not in cmd
        assert "--resume" not in cmd
        assert "--session-id" not in cmd

    @pytest.mark.parametrize("arm", ["A_plain", "B_auto", "C_strict"])
    def test_canonical_commands_omit_fixed_turn_cap_while_legacy_keeps_40(
        self, script_run_agentic: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: str
    ) -> None:
        """Canonical agentic cells use wall-clock control; legacy cells retain the fixed 40-turn cap."""
        repo = tmp_path / "repo"
        repo.mkdir()
        runner = script_run_agentic.ModelRunner("haiku", "fixture-model", repo, timeout=600)
        task = script_run_agentic.Task(id="fixture", type="fix", prompt="answer")
        commands: dict[str, list[str]] = {}

        def _capture_command(
            _self: Any,
            cmd: list[str],
            result: Any,
            update_fn: Any = None,
            cwd: Any = None,
            arm: str = "",
        ) -> None:
            """Record argv by arm and supply minimal input-token usage."""
            commands[arm] = cmd
            result.input_tokens = 1

        monkeypatch.setattr(script_run_agentic.ModelRunner, "_stream_events", _capture_command)

        runner.run(task, arm)
        runner.run(task, "plain")

        assert "--max-turns" not in commands[arm]
        legacy = commands["plain"]
        assert legacy[legacy.index("--max-turns") + 1] == "40"

    def test_plain_arm_gets_no_extra_tools(self, script_run_agentic: Any) -> None:
        """The control arm re-supplies nothing — no plugin, no MCP."""
        assert script_run_agentic.ModelRunner._arm_isolation_flags("plain") == []

    def test_codemap_fixture_is_independent_of_empty_user_cache(
        self, script_run_agentic: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Codemap arm resolves the checked-out fixture when the user cache is empty."""
        monkeypatch.setenv("HOME", str(tmp_path))
        plugin_dir = script_run_agentic.ModelRunner._codemap_plugin_dir()
        assert plugin_dir == str(BENCHMARKS_DIR.parent / "plugins" / "codemap-py")

    @pytest.mark.parametrize("arm", ["codemap", "combined"])
    def test_skill_arms_load_codemap_plugin(self, script_run_agentic: Any, arm: str) -> None:
        """Codemap/combined re-supply the codemap plugin via ``--plugin-dir`` (Skill availability)."""
        flags = script_run_agentic.ModelRunner._arm_isolation_flags(arm)
        assert "--plugin-dir" in flags

    @pytest.mark.parametrize("arm", ["codemap", "combined", "B_auto", "C_strict"])
    def test_codemap_arms_fail_closed_when_plugin_fixture_is_missing(
        self, script_run_agentic: Any, monkeypatch: pytest.MonkeyPatch, arm: str
    ) -> None:
        """A missing Codemap fixture cannot silently produce a valid treatment arm."""
        monkeypatch.setattr(script_run_agentic.ModelRunner, "_codemap_plugin_dir", staticmethod(lambda: None))

        with pytest.raises(RuntimeError, match="Codemap plugin fixture"):
            script_run_agentic.ModelRunner._arm_isolation_flags(arm)

    @pytest.mark.parametrize("arm", ["semble", "combined"])
    def test_semble_arms_load_semble_mcp_strictly(self, script_run_agentic: Any, arm: str) -> None:
        """Semble/combined re-supply the semble server via ``--mcp-config``, restricted with ``--strict``."""
        flags = script_run_agentic.ModelRunner._arm_isolation_flags(arm)
        assert "--mcp-config" in flags
        assert "--strict-mcp-config" in flags

    def test_semble_mcp_config_is_valid_stdio_server(self, script_run_agentic: Any) -> None:
        """The reconstructed semble config is a valid stdio-server JSON for ``--mcp-config``."""
        path = script_run_agentic.ModelRunner._semble_mcp_config_path()
        cfg = json.loads(Path(path).read_text())
        assert cfg["mcpServers"]["semble"]["command"] == "uvx"


class TestZeroTokenRetryPredicate:
    """ModelRunner.run retry loop: fast connectivity failures retry, wall-clock timeouts never."""

    @pytest.fixture(name="stream_with")
    def _stream_with(self, script_run_agentic: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
        """Return an installer that swaps in a fake ``_stream_events`` and skips the retry back-off sleep."""

        def install(fake: Any) -> None:
            monkeypatch.setattr(script_run_agentic.time, "sleep", lambda _s: None)
            monkeypatch.setattr(script_run_agentic.ModelRunner, "_stream_events", fake)

        return install

    @staticmethod
    def _runner_and_task(script_run_agentic: Any, tmp_path: Path) -> tuple[Any, Any]:
        """Create a disposable repository with a runner and minimal task, without running the task."""
        repo = tmp_path / "repo"
        repo.mkdir()
        runner = script_run_agentic.ModelRunner("haiku", "fixture-model", repo, timeout=600)
        task = script_run_agentic.Task(id="fixture", type="fix", prompt="answer")
        return runner, task

    def test_timeout_by_elapsed_alone_is_not_retried(
        self, script_run_agentic: Any, tmp_path: Path, stream_with: Any
    ) -> None:
        """0 tokens with elapsed >= timeout runs exactly once, even when no timeout error string was recorded."""
        runner, task = self._runner_and_task(script_run_agentic, tmp_path)
        calls: list[str] = []

        def _timeout_stream(
            _self: Any, cmd: list[str], result: Any, update_fn: Any = None, cwd: Any = None, arm: str = ""
        ) -> None:
            """Record the arm and supply this scenario's timeout evidence."""
            calls.append(arm)
            result.elapsed_s = 600.0

        stream_with(_timeout_stream)

        runner.run(task, "plain")

        assert calls == ["plain"]

    def test_timeout_by_error_string_alone_is_not_retried(
        self, script_run_agentic: Any, tmp_path: Path, stream_with: Any
    ) -> None:
        """0 tokens with a recorded timeout error runs exactly once and keeps the error, even with small elapsed."""
        runner, task = self._runner_and_task(script_run_agentic, tmp_path)
        calls: list[str] = []

        def _timeout_stream(
            _self: Any, cmd: list[str], result: Any, update_fn: Any = None, cwd: Any = None, arm: str = ""
        ) -> None:
            """Record the arm and supply this scenario's timeout evidence."""
            calls.append(arm)
            result.elapsed_s = 5.0
            result.error = "timeout (600s)"

        stream_with(_timeout_stream)

        result = runner.run(task, "plain")

        assert calls == ["plain"]
        assert result.error == "timeout (600s)"

    def test_fast_zero_token_failure_still_retries(
        self, script_run_agentic: Any, tmp_path: Path, stream_with: Any
    ) -> None:
        """A fast 0-token exit (connectivity failure) is retried up to the 3-attempt budget."""
        runner, task = self._runner_and_task(script_run_agentic, tmp_path)
        calls: list[str] = []

        def _failing_stream(
            _self: Any, cmd: list[str], result: Any, update_fn: Any = None, cwd: Any = None, arm: str = ""
        ) -> None:
            """Record the arm and leave the streamed result unsuccessful."""
            calls.append(arm)
            result.elapsed_s = 0.5

        stream_with(_failing_stream)

        runner.run(task, "plain")

        assert len(calls) == 3

    def test_each_retry_attempt_gets_a_fresh_sandbox(
        self, script_run_agentic: Any, tmp_path: Path, stream_with: Any
    ) -> None:
        """A retry must start from the baseline tree, not from the failed attempt's leftovers.

        Scenario: three 0-token attempts, each writing a file into its sandbox. Every
        attempt must receive a distinct sandbox that carries no file from its predecessor.
        """
        runner, task = self._runner_and_task(script_run_agentic, tmp_path)
        sandboxes: list[Path] = []
        inherited: list[bool] = []

        def _editing_stream(
            _self: Any, cmd: list[str], result: Any, update_fn: Any = None, cwd: Any = None, arm: str = ""
        ) -> None:
            """Record inherited edits and create a fresh edit in the current sandbox."""
            inherited.append((cwd / "agent-edit.txt").exists())
            (cwd / "agent-edit.txt").write_text("edited\n", encoding="utf-8")
            sandboxes.append(cwd)
            result.elapsed_s = 0.5

        stream_with(_editing_stream)

        runner.run(task, "plain")

        assert len(sandboxes) == 3
        assert len(set(sandboxes)) == 3
        assert inherited == [False, False, False]

    def test_captured_diff_ignores_the_directories_the_sandbox_never_copies(
        self, script_run_agentic: Any, tmp_path: Path
    ) -> None:
        """The post-run diff must mirror the copytree ignore list instead of reporting it as change.

        Scenario: the repo carries .git and .cache trees that copytree skips; only the
        agent's real edit may appear in the captured diff.
        """
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (repo / ".cache" / "codemap").mkdir(parents=True)
        (repo / ".cache" / "codemap" / "index.json").write_text("{}\n", encoding="utf-8")
        (repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
        runner = script_run_agentic.ModelRunner("haiku", "fixture-model", repo, timeout=600)
        task = script_run_agentic.Task(id="fixture", type="fix", prompt="answer", requires_reset=True)
        captured: list[str] = []

        with runner._effective_cwd(task, "plain", captured, []) as cwd:
            (cwd / "module.py").write_text("VALUE = 2\n", encoding="utf-8")

        # Without the excludes, diff reports both skipped trees as "Only in <repo>: …".
        assert "module.py" in captured[0]
        assert "Only in" not in captured[0]


# ===========================================================================
# ModelRunner — _system_prompt (pure string builder)
# ===========================================================================


class TestModelRunnerSystemPrompt:
    """ModelRunner._system_prompt wording per task type and arm."""

    @pytest.mark.parametrize("task_type", ["fix", "feature", "refactor", "review"])
    def test_plain_arm_prompt_contains_base_skill(self, script_run_agentic: Any, runner: Any, task_type: str) -> None:
        """Plain arm system prompt contains the skill-specific base text.

        Scenario: each of the four task types has a distinct preamble in
        _PLAIN_SKILLS; the assembled prompt must include that preamble.
        """
        prompt = runner._system_prompt(task_type, "plain")
        assert "software engineer" in prompt.lower()

    @pytest.mark.parametrize(
        ("arm", "keyword"),
        [
            pytest.param("codemap", "codemap:query", id="codemap-arm-mentions-codemap-query"),
            pytest.param("semble", "mcp__semble__search", id="semble-arm-mentions-mcp-search-tool"),
        ],
    )
    def test_tool_arm_prompt_contains_tool_keyword(
        self, script_run_agentic: Any, runner: Any, arm: str, keyword: str
    ) -> None:
        """A tool arm's system prompt mentions its tool: /codemap:query or mcp__semble__search.

        Scenario: user runs the codemap arm; the injected supplement described in the module docstring must
        reference 'codemap:query'. User runs the semble arm; the supplement must mention the MCP tool name so
        the agent knows to use it.
        """
        prompt = runner._system_prompt("fix", arm)
        assert keyword in prompt

    def test_combined_arm_prompt_contains_both_tools(self, script_run_agentic: Any, runner: Any) -> None:
        """Combined arm system prompt mentions both codemap and semble.

        Scenario: user runs the combined arm; agent must know about both
        structural tools.
        """
        prompt = runner._system_prompt("fix", "combined")
        assert "codemap:query" in prompt
        assert "mcp__semble__search" in prompt

    def test_unknown_task_type_falls_back_to_fix_prompt(self, script_run_agentic: Any, runner: Any) -> None:
        """_system_prompt falls back to 'fix' base for unknown task types.

        Scenario: tasks file contains an unexpected type value; prompt
        assembly must not raise KeyError (the implementation uses .get
        with 'fix' as default).
        """
        prompt = runner._system_prompt("unknown_type", "plain")
        assert len(prompt) > 0

    def test_semble_prompt_contains_repo_path(self, script_run_agentic: Any, runner: Any, tmp_path: Path) -> None:
        """Semble arm prompt includes the actual repo_path string.

        Scenario: agent needs to pass repo= on every semble call; the
        prompt must contain the resolved repo path so the agent can copy it.
        """
        prompt = runner._system_prompt("fix", "semble")
        assert str(tmp_path) in prompt


# ===========================================================================
# ModelRunner._system_prompt — arm symmetry
# ===========================================================================


class TestPromptSymmetry:
    """The measured signal must come from tool availability, not asymmetric steering.

    Every arm must share the same answer format and efficiency instruction, and none may carry call caps, verification
    bans, or step protocols — that prescriptive steering is what manufactured the deff/tool-call gap the benchmark
    claims to observe.
    """

    _ARMS = ("plain", "codemap", "semble", "combined")

    # Phrases that biased tool-call count in the old prompts; none may survive in any arm.
    _FORBIDDEN = (
        "Do NOT grep",
        "do NOT grep",
        "Maximum 3",
        "max 2 codemap",
        "guard hook",
        "DENIES",
        "STEP 1",
        "STEP 2",
        "STEP 3",
        "Convergence rule",
        "Count Grep matches per module",
        "do NOT call any tool",
        "Copy EVERY module from the codemap result",
    )

    @pytest.mark.parametrize("arm", _ARMS)
    def test_answer_format_block_present_for_every_arm(self, script_run_agentic: Any, runner: Any, arm: str) -> None:
        """The shared 'Reverse Dependencies Found' answer format appears in every arm prompt.

        Scenario: erec/rrec are extracted from this block; it is the measurement target and
        must be identical across arms, so each arm's prompt must contain it.
        """
        prompt = runner._system_prompt("fix", arm)
        assert "## Required answer format" in prompt
        assert "## Reverse Dependencies Found" in prompt

    @pytest.mark.parametrize("arm", _ARMS)
    def test_efficiency_sentence_present_for_every_arm(self, script_run_agentic: Any, runner: Any, arm: str) -> None:
        """The single shared efficiency instruction appears in every arm prompt.

        Scenario: all arms get the same 'as few tool calls as possible' nudge so no arm is
        uniquely steered toward more or fewer calls.
        """
        prompt = runner._system_prompt("fix", arm)
        assert "as few tool calls as possible" in prompt

    @pytest.mark.parametrize("arm", _ARMS)
    def test_answer_format_is_arm_neutral(self, script_run_agentic: Any, runner: Any, arm: str) -> None:
        """The answer format hardcodes no corpus-specific example module paths.

        Scenario: hardcoded 'lightning.*' example lines would prime some arms with real rdep
        names; the shared block must use generic placeholders only.
        """
        prompt = runner._system_prompt("fix", arm)
        answer_block = prompt[prompt.index("## Required answer format") :]
        assert "lightning.pytorch.trainer.trainer" not in answer_block

    @pytest.mark.parametrize("arm", _ARMS)
    def test_no_forbidden_steering_in_any_arm(self, script_run_agentic: Any, runner: Any, arm: str) -> None:
        """No arm prompt contains call caps, verification bans, or step-protocol steering.

        Scenario: the deff gap was a denominator artifact of instructing the codemap arm to
        stop calling tools; none of the prescriptive phrases may remain in any arm.
        """
        prompt = runner._system_prompt("fix", arm)
        present = [p for p in self._FORBIDDEN if p in prompt]
        assert not present, f"arm={arm!r} still contains steering phrases: {present}"

    def test_all_arms_share_identical_answer_format(self, script_run_agentic: Any, runner: Any) -> None:
        """The answer-format block is byte-identical across all four arms.

        Scenario: any per-arm wording in the extraction target would bias erec/rrec; the shared
        constant must appear verbatim in every arm prompt.
        """
        blocks = {arm: runner._system_prompt("fix", arm) for arm in self._ARMS}
        shared = script_run_agentic.ModelRunner._ANSWER_FORMAT
        assert all(shared in prompt for prompt in blocks.values())


# ===========================================================================
# _tool_key_arg (internal utility tested via documented docstring examples)
# ===========================================================================


class TestToolKeyArg:
    """Validates the tool log formatter against its own docstring examples."""

    @pytest.mark.parametrize(
        ("tool", "tool_input", "expected"),
        [
            pytest.param(
                "Grep", {"pattern": "import auth", "path": "src/"}, "'import auth' in src/", id="grep-with-path"
            ),
            pytest.param(
                "mcp__semble__search",
                {"query": "import checkpoint_connector", "repo": "/tmp/r", "top_k": 20},
                "query='import checkpoint_connector'",
                id="semble-search-returns-query-repr",
            ),
            pytest.param(
                "mcp__semble__find_related",
                {"query": "find related", "line": 42},
                "query='find related'",
                id="semble-find-related-returns-query-repr",
            ),
            pytest.param("Glob", {"pattern": "**/*.py"}, "**/*.py", id="glob-returns-pattern"),
        ],
    )
    def test_formats_key_argument(self, script_run_agentic: Any, tool: str, tool_input: dict, expected: str) -> None:
        """_tool_key_arg formats the documented key argument per tool.

        Grep produces ``'pattern' in path``, the two semble tools return a ``query=`` repr, and Glob returns the pattern
        string directly. The function docstring shows the Grep and semble examples verbatim.
        """
        assert script_run_agentic._tool_key_arg(tool, tool_input) == expected

    def test_bash_truncates_to_120_chars(self, script_run_agentic: Any) -> None:
        """_tool_key_arg for Bash truncates command to 120 characters.

        Scenario: very long shell commands must be truncated at 120 chars
        to keep tool logs readable.
        """
        long_cmd = "x" * 200
        result = script_run_agentic._tool_key_arg("Bash", {"command": long_cmd})
        assert len(result) <= 120

    def test_skill_returns_skill_and_args(self, script_run_agentic: Any) -> None:
        """_tool_key_arg for Skill returns 'skill args' combined string."""
        result = script_run_agentic._tool_key_arg(
            "Skill", {"skill": "codemap:query", "args": "rdeps lightning.pytorch.trainer"}
        )
        assert "codemap:query" in result
        assert "rdeps" in result

    def test_unknown_tool_returns_string(self, script_run_agentic: Any) -> None:
        """_tool_key_arg for unknown tool name returns a non-empty string.

        Scenario: a future tool name not listed in the dispatcher;
        the fallback str() path must not raise.
        """
        result = script_run_agentic._tool_key_arg("UnknownTool", {"key": "val"})
        assert isinstance(result, str)


# ===========================================================================
# ModelRunner._on_tool_result (static, pure accumulator)
# ===========================================================================


class TestOnToolResult:
    """ModelRunner._on_tool_result accounting of tool output, tokens and Codemap results."""

    def _make_run(self, script_agentic: Any) -> Any:
        """Build a minimal BenchmarkRun for _on_tool_result tests."""
        return script_agentic.BenchmarkRun(arm="codemap", task_id="T1", task_type="fix", model="haiku", success=False)

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param("hello world from tool result", id="string-content"),
            pytest.param([{"type": "text", "text": "result text here"}], id="list-of-content-blocks"),
        ],
    )
    def test_content_accumulates_tokens(self, script_run_agentic: Any, content: Any) -> None:
        """_on_tool_result tokenises string or list-of-block content and adds it to tool_result_tokens.

        Scenario: a grep result arrives as a plain string, or as a list of content blocks whose text must be
        extracted first; either way the token count in the BenchmarkRun must increase.
        """
        run = self._make_run(script_run_agentic)
        script_run_agentic.ModelRunner._on_tool_result(content, run)
        assert run.tool_result_tokens > 0

    @pytest.mark.parametrize(
        ("content", "flags"),
        [
            pytest.param(
                "<tool_use_error>Permission denied</tool_use_error>", {"is_rdeps": True}, id="tool-use-error-block"
            ),
            pytest.param(
                "Launching skill: codemap:query rdeps ...", {"is_rdeps": True}, id="launching-skill-placeholder"
            ),
        ],
    )
    def test_skips_error_and_placeholder_content(self, script_run_agentic: Any, content: str, flags: dict) -> None:
        """_on_tool_result captures neither a ``<tool_use_error>`` block nor a 'Launching skill:' placeholder.

        Scenario: a disallowed tool returns an error block, or the skill executor emits a status line before
        its result; neither may be added to the exposure corpus (codemap_results / skill_result_text) or
        captured as an rdep answer.
        """
        run = self._make_run(script_run_agentic)
        script_run_agentic.ModelRunner._on_tool_result(content, run, **flags)
        assert run.codemap_results == []
        assert run.skill_result_text == ""

    def test_rdep_result_captured_in_codemap_results(self, script_run_agentic: Any) -> None:
        """_on_tool_result appends is_rdeps=True content to codemap_results.

        Scenario: a valid codemap rdeps result arrives; it must be
        appended to codemap_results for erec corpus construction.
        """
        run = self._make_run(script_run_agentic)
        script_run_agentic.ModelRunner._on_tool_result(
            '{"imported_by": ["lightning.pytorch.trainer.trainer"]}',
            run,
            is_rdeps=True,
        )
        assert len(run.codemap_results) == 1
        assert "imported_by" in run.codemap_results[0]

    def test_semble_result_captured_in_semble_results(self, script_run_agentic: Any) -> None:
        """_on_tool_result appends is_semble=True content to semble_results.

        Scenario: semble MCP result arrives; it must be stored in
        semble_results for erec corpus construction.
        """
        run = self._make_run(script_run_agentic)
        script_run_agentic.ModelRunner._on_tool_result(
            "File: src/lightning/pytorch/trainer.py\nLine 10: import x",
            run,
            is_semble=True,
        )
        assert len(run.semble_results) == 1

    def test_non_rdep_codemap_does_not_populate_codemap_results(self, script_run_agentic: Any) -> None:
        """_on_tool_result with is_codemap=True only (not is_rdeps) skips capture.

        Scenario: a codemap deps (not rdeps) call arrives; it must NOT
        be appended to codemap_results per the documented distinction.
        """
        run = self._make_run(script_run_agentic)
        script_run_agentic.ModelRunner._on_tool_result(
            '{"imports": ["something"]}',
            run,
            is_codemap=True,
            is_rdeps=False,
        )
        assert run.codemap_results == []


# ===========================================================================
# MODELS constant
# ===========================================================================


class TestModelsConstant:
    """MODELS exposes the documented tiers with usable model ids."""

    @pytest.mark.parametrize("tier", ["haiku", "sonnet", "opus"])
    def test_documented_tier_present(self, script_run_agentic: Any, tier: str) -> None:
        """MODELS contains the three documented model tiers.

        Scenario: user passes ``--model haiku/sonnet/opus``; all three must
        be valid keys in the MODELS dict.
        """
        assert tier in script_run_agentic.MODELS

    def test_model_ids_are_non_empty_strings(self, script_run_agentic: Any) -> None:
        """Every MODELS value is a non-empty string (full model ID).

        Scenario: the model ID is passed directly to the claude CLI
        via ``--model``; an empty string would silently use the wrong model.
        """
        empty_tiers = [
            tier
            for tier, model_id in script_run_agentic.MODELS.items()
            if not isinstance(model_id, str) or not model_id
        ]
        assert empty_tiers == []


# ===========================================================================
# Integration-level: GroundTruth loaded from fixture index
# ===========================================================================


class TestGroundTruthIntegration:
    """GroundTruth built from a synthetic index end to end."""

    def test_expected_rdeps_are_the_production_importers(self, ground_truth: Any) -> None:
        """GroundTruth.expected for BA-01 holds both production importers and not the tests module.

        Scenario: the tmp_index fixture has two non-test modules and tests.test_timer all importing
        lightning.pytorch.callbacks.timer; only the production callers may be expected (an exact set).
        """
        assert ground_truth.expected["BA-01"] == {
            "lightning.pytorch.trainer.trainer",
            "lightning.pytorch.loops.fit_loop",
        }

    def test_top10_expected_populated_for_scored_task(self, ground_truth: Any) -> None:
        """GroundTruth.top10_expected is populated for tasks with rdeps.

        Scenario: BA-01 has 2 rdeps; top10_expected must contain a frozenset
        for BA-01 (since |rdeps| ≤ 10, all are included).
        """
        assert ground_truth.top10_expected["BA-01"] == {
            "lightning.pytorch.trainer.trainer",
            "lightning.pytorch.loops.fit_loop",
        }

    def test_all_modules_populated_from_index(self, ground_truth: Any) -> None:
        """GroundTruth.all_modules contains all ok-status modules from index.

        Scenario: four modules in the fixture index, all status='ok';
        all_modules must contain all four names for leaf-name computation.
        """
        assert ground_truth.all_modules >= {
            "lightning.pytorch.callbacks.timer",
            "lightning.pytorch.trainer.trainer",
            "lightning.pytorch.loops.fit_loop",
            "tests.test_timer",
        }

    def test_task_without_primary_module_skipped_silently(self, script_run_agentic: Any, tmp_index: Path) -> None:
        """GroundTruth silently skips tasks with no primary_module.

        Scenario: a task definition missing primary_module must not raise;
        the task ID must not appear in expected.
        """
        task_no_pm = script_run_agentic.Task(id="NOPRIMARY", type="fix", prompt="x")
        gt = script_run_agentic.GroundTruth(tmp_index, [task_no_pm])
        assert "NOPRIMARY" not in gt.expected


# ===========================================================================
# AST import scan helpers
# ===========================================================================


def _touch(root: Path, *relative_paths: str, text: str = "") -> None:
    """Create each file under ``root`` holding ``text``, making parent directories as needed."""
    for relative_path in relative_paths:
        (root / relative_path).parent.mkdir(parents=True, exist_ok=True)
        (root / relative_path).write_text(text)


class TestDeriveModuleName:
    """_derive_module_name maps file paths to dotted module names."""

    @pytest.mark.parametrize(
        ("files", "target", "expected"),
        [
            pytest.param(
                ("pkg/__init__.py", "pkg/sub/__init__.py", "pkg/sub/mod.py"),
                "pkg/sub/mod.py",
                "pkg.sub.mod",
                id="regular-module-uses-package-chain",
            ),
            pytest.param(
                ("pkg/__init__.py", "pkg/sub/__init__.py"),
                "pkg/sub/__init__.py",
                "pkg.sub",
                id="init-file-is-its-package-name",
            ),
            pytest.param(
                ("examples/pytorch/x.py",),
                "examples/pytorch/x.py",
                "examples.pytorch.x",
                id="loose-file-is-path-relative",
            ),
        ],
    )
    def test_names_file_by_package_chain(
        self, script_run_agentic: Any, tmp_path: Path, files: tuple[str, ...], target: str, expected: str
    ) -> None:
        """_derive_module_name returns the dotted module name scan-index would derive for the same file.

        Scenario: a src-layout module joins its __init__.py chain ('pkg.sub.mod'); an __init__.py names its
        package ('pkg.sub', not 'pkg.sub.__init__'); a loose file with no __init__.py in its chain is named
        path-relative ('examples.pytorch.x', not the bare stem) so the AST oracle and the index oracle share one
        namespace and stop emitting spurious gt-divergence lines for the module.
        """
        _touch(tmp_path, *files)

        assert script_run_agentic._derive_module_name(tmp_path / target, tmp_path) == expected


class TestResolveRelativeBase:
    """_resolve_relative_base resolves relative-import bases against the importing module."""

    @pytest.mark.parametrize(
        ("package", "level", "module", "expected"),
        [
            pytest.param("a.b", 1, "c", "a.b.c", id="level1-with-module"),
            pytest.param("a.b", 1, None, "a.b", id="level1-bare"),
            pytest.param("a.b.c", 2, "u", "a.b.u", id="level2-parent"),
            pytest.param("a", 3, "x", None, id="level-above-root"),
        ],
    )
    def test_relative_resolution(
        self, script_run_agentic: Any, package: str, level: int, module: Any, expected: Any
    ) -> None:
        """resolve_relative_base resolves dotted relative imports against the package.

        Scenario: each documented relative form (current package, parent, over-ascend) must
        map to the correct absolute base or None when it walks above the root.
        """
        assert script_run_agentic.resolve_relative_base(package, level, module) == expected


class TestScanRepoImporters:
    """AST scan builds a tool-independent {module: importers} map across import forms."""

    @pytest.fixture(name="scanned_repo")
    def _scanned_repo(self, tmp_path: Path) -> Path:
        """Create a src-layout package exercising every import form plus a test file."""
        pkg = tmp_path / "src" / "app"
        (pkg / "sub").mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "sub" / "__init__.py").write_text("")
        (pkg / "target.py").write_text("X = 1\n")
        (pkg / "caller_submodule.py").write_text("from app import target\n")  # index-blind form
        (pkg / "caller_base.py").write_text("from app.target import X\n")
        (pkg / "caller_relative.py").write_text("from . import target\n")
        (pkg / "caller_plain.py").write_text("import app.target\n")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_app.py").write_text("from app.target import X\n")
        return tmp_path

    def test_scan_captures_all_import_forms(self, script_run_agentic: Any, scanned_repo: Path) -> None:
        """_scan_repo_importers finds importers via plain, from-base, from-submodule, and relative forms.

        Scenario: four production callers import app.target through different syntaxes; all four
        must appear as importers, and the test-tree caller must be excluded.
        """
        importers = script_run_agentic._scan_repo_importers(scanned_repo)
        assert importers.get("app.target") == {
            "app.caller_submodule",
            "app.caller_base",
            "app.caller_relative",
            "app.caller_plain",
        }


class TestGroundTruthAstOracle:
    """GroundTruth uses the AST scan (not the index) as the rdep oracle and logs divergences."""

    @pytest.fixture(name="ast_gt")
    def _ast_gt(self, tmp_path: Path, script_run_agentic: Any) -> Any:
        """Build a package where the index under-records rdeps vs the AST scan."""
        pkg = tmp_path / "src" / "app"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "target.py").write_text("X = 1\n")
        (pkg / "caller_submodule.py").write_text("from app import target\n")  # index records 'app'
        (pkg / "caller_base.py").write_text("from app.target import X\n")  # index records 'app.target'
        index = _minimal_index(
            [
                {"name": "app.target", "direct_imports": [], "dep_count": 0, "status": "ok"},
                {"name": "app.caller_submodule", "direct_imports": ["app"], "dep_count": 1, "status": "ok"},
                {"name": "app.caller_base", "direct_imports": ["app.target"], "dep_count": 1, "status": "ok"},
                {"name": "app", "direct_imports": [], "dep_count": 0, "status": "ok"},
            ]
        )
        index_file = tmp_path / "idx.json"
        index_file.write_text(json.dumps(index))
        task = script_run_agentic.Task(
            id="BA-01", type="blast_radius_analysis", prompt="p", primary_module="app.target"
        )
        return script_run_agentic.GroundTruth(index_file, [task], repo_path=tmp_path)

    def test_expected_uses_ast_superset(self, ast_gt: Any) -> None:
        """GroundTruth.expected includes the submodule-import caller the index misses.

        Scenario: 'from app import target' makes caller_submodule a real rdep; the AST oracle
        must credit it even though the index only recorded a dependency on 'app'.
        """
        assert ast_gt.expected["BA-01"] == {"app.caller_submodule", "app.caller_base"}

    def test_index_expected_retained_as_diagnostic(self, ast_gt: Any) -> None:
        """The index-derived list is kept separately and is a strict subset here.

        Scenario: index_expected must still be available for diagnostics and must omit the
        submodule-form caller that only the AST scan catches.
        """
        assert ast_gt.index_expected["BA-01"] == {"app.caller_base"}

    def test_divergence_recorded_with_missing_in_index(self, ast_gt: Any) -> None:
        """A divergence entry flags real importers absent from the index.

        Scenario: caller_submodule is in the AST set but not the index set, so it must appear
        under missing_in_index — the harness's blind-spot signal.
        """
        div = ast_gt.divergences["BA-01"]
        assert div["missing_in_index"] == ["app.caller_submodule"]
        assert div["ast"] == 2
        assert div["index"] == 1

    def test_ast_only_rdep_is_matchable_in_corpus(self, ast_gt: Any) -> None:
        """An AST-only expected rdep can still be credited when found in agent output.

        Scenario: match patterns must cover AST-only expected modules so an arm that reports
        the index-missed caller scores recall for it rather than being silently penalized.
        """
        corpus = "app.caller_submodule app.caller_base"
        score = _score_corpus(ast_gt, corpus)
        assert score.erec == pytest.approx(1.0)


# ===========================================================================
# Fix-family prompt symmetry
# ===========================================================================


class TestFixFamilyPromptSymmetry:
    """Fix / read_crop prompts must share the efficiency nudge and carry no anti-grep steering."""

    _ARMS = ("plain", "codemap", "semble", "combined")
    _FIX_TYPES = ("fix_single", "fix_multicaller", "read_crop")

    @pytest.mark.parametrize("task_type", _FIX_TYPES)
    @pytest.mark.parametrize("arm", _ARMS)
    def test_efficiency_sentence_present_in_fix_family(self, runner: Any, task_type: str, arm: str) -> None:
        """The shared efficiency sentence appears in every fix-family arm prompt.

        Scenario: fix / read_crop prompts previously returned before the shared efficiency
        instruction, so only rdep tasks were symmetric; the shared sentence now appends to every arm here too.
        """
        prompt = runner._system_prompt(task_type, arm)
        assert "as few tool calls as possible" in prompt

    @pytest.mark.parametrize("phrase", ["do NOT grep", "decisive advantage", "plain grep misses"])
    def test_fixmulti_codemap_has_no_anti_grep_steering(self, runner: Any, phrase: str) -> None:
        """The fix_multicaller codemap supplement carries only tool availability + syntax.

        Scenario: the old supplement told the codemap arm 'do NOT grep for more' and framed
        codemap as a 'decisive advantage' — that steering was the measured signal.
        """
        prompt = runner._system_prompt("fix_multicaller", "codemap")
        assert phrase not in prompt

    @pytest.mark.parametrize("task_type", _FIX_TYPES)
    @pytest.mark.parametrize("arm", _ARMS)
    def test_rdep_answer_format_absent_from_fix_family(self, runner: Any, task_type: str, arm: str) -> None:
        """Fix / read_crop prompts do not append the rdep-only 'Reverse Dependencies Found' block.

        Scenario: fix tasks are scored by diff / keyword recall, so the reverse-dependency answer
        format is irrelevant and must not be injected.
        """
        prompt = runner._system_prompt(task_type, arm)
        assert "## Reverse Dependencies Found" not in prompt


# ===========================================================================
# Arm tool policy — semble Bash symmetry
# ===========================================================================


class TestArmToolPolicy:
    """Semble must keep a shell fallback like every other arm; only its primary tool differs."""

    def test_semble_arm_no_longer_blocks_bash(self, script_run_agentic: Any) -> None:
        """The semble disallowed list no longer contains Bash."""
        disallowed = script_run_agentic.ModelRunner._ARM_DISALLOWED["semble"]
        assert "Bash" not in ",".join(disallowed)

    def test_semble_supplement_advertises_bash_fallback(self, runner: Any) -> None:
        """The semble supplement mirrors codemap's Grep/Bash error fallback clause."""
        assert "Grep/Bash fallback" in runner._system_prompt("fix", "semble")


# ===========================================================================
# Subprocess env — SCAN_NO_AUTOBUILD opt-out
# ===========================================================================


class TestSubprocessEnv:
    """Codemap / combined arms opt out of in-task index builds; other arms are untouched."""

    @pytest.fixture(autouse=True)
    def _accept_external_codemap_python(self, monkeypatch: pytest.MonkeyPatch, script_run_agentic: Any) -> None:
        """Keep environment-policy tests independent of the CI interpreter version."""
        _mock_codemap_python_probe(monkeypatch, script_run_agentic)

    @pytest.mark.parametrize("arm", ["codemap", "combined"])
    def test_scan_no_autobuild_set_for_structural_arms(self, script_run_agentic: Any, arm: str) -> None:
        """Disable automatic index builds for arms that invoke the structural-query skill."""
        env = script_run_agentic.ModelRunner._subprocess_env(arm)
        assert env.get("SCAN_NO_AUTOBUILD") == "1"

    @pytest.mark.parametrize("arm", ["codemap", "combined", "B_auto", "C_strict"])
    def test_claude_plugin_root_set_for_codemap_arms(self, script_run_agentic: Any, arm: str) -> None:
        """CLAUDE_PLUGIN_ROOT is exported for codemap-consuming arms, pointed at the repo fixture.

        Without this, the Skill's ``${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}`` fallback resolves to a relative path
        absent from the benchmark's copied sandbox repo, and the agent burns calls hunting for the binary instead of
        querying it.
        """
        env = script_run_agentic.ModelRunner._subprocess_env(arm)
        assert env.get("CLAUDE_PLUGIN_ROOT") == script_run_agentic.ModelRunner._codemap_plugin_dir()

    @pytest.mark.parametrize(
        ("variable", "arm"),
        [
            pytest.param("SCAN_NO_AUTOBUILD", "plain", id="no-autobuild-plain"),
            pytest.param("SCAN_NO_AUTOBUILD", "semble", id="no-autobuild-semble"),
            pytest.param("SCAN_NO_AUTOBUILD", "", id="no-autobuild-empty-arm"),
            pytest.param("CLAUDE_PLUGIN_ROOT", "plain", id="plugin-root-plain"),
            pytest.param("CLAUDE_PLUGIN_ROOT", "A_plain", id="plugin-root-a-plain"),
            pytest.param("CLAUDE_PLUGIN_ROOT", "semble", id="plugin-root-semble"),
            pytest.param("CLAUDE_PLUGIN_ROOT", "", id="plugin-root-empty-arm"),
        ],
    )
    def test_codemap_variable_absent_for_other_arms(self, script_run_agentic: Any, variable: str, arm: str) -> None:
        """Non-codemap arms receive neither the build opt-out nor the plugin root.

        Non-structural arms never call the skill, so they get no ``SCAN_NO_AUTOBUILD`` opt-out. A_plain's contract is
        codemap absent and inaccessible, so leaking ``CLAUDE_PLUGIN_ROOT`` would break isolation.
        """
        env = script_run_agentic.ModelRunner._subprocess_env(arm)
        assert variable not in env

    @pytest.mark.parametrize("arm", ["plain", "A_plain", "semble", ""])
    def test_codemap_bin_path_absent_for_non_codemap_arms(
        self, script_run_agentic: Any, monkeypatch: pytest.MonkeyPatch, arm: str
    ) -> None:
        """A/plain environments cannot inherit the benchmark-injected Codemap launcher.

        Regression: `_subprocess_env` previously prepended the plugin cache `bin/`
        directory before checking the arm, contradicting A's absent-tool control.
        """
        monkeypatch.setattr(
            agentic_runner,
            "codemap_bin_on_path",
            lambda env: env.update(PATH=f"/sentinel/codemap-bin:{env.get('PATH', '')}") or env,
        )

        env = script_run_agentic.ModelRunner._subprocess_env(arm)

        assert "/sentinel/codemap-bin" not in env.get("PATH", "")

    @pytest.mark.parametrize("arm", ["codemap", "combined", "B_auto", "C_strict"])
    def test_codemap_bin_path_present_for_codemap_arms(
        self, script_run_agentic: Any, monkeypatch: pytest.MonkeyPatch, arm: str
    ) -> None:
        """Codemap treatments receive the benchmark-injected launcher path."""
        monkeypatch.setattr(
            agentic_runner,
            "codemap_bin_on_path",
            lambda env, plugin_root: env.update(PATH=f"{plugin_root}/bin:{env.get('PATH', '')}") or env,
        )

        env = script_run_agentic.ModelRunner._subprocess_env(arm)

        assert env["PATH"].startswith(f"{script_run_agentic.ModelRunner._codemap_plugin_dir()}/bin:")

    def test_codemap_bin_path_uses_the_locked_repository_fixture(self, script_run_agentic: Any) -> None:
        """A mutable user-cache installation cannot replace the scope-locked launcher."""
        env = script_run_agentic.ModelRunner._subprocess_env("C_strict")
        expected = Path(script_run_agentic.ModelRunner._codemap_plugin_dir()) / "bin"

        assert Path(env["PATH"].split(os.pathsep)[0]).resolve() == expected.resolve()

    def test_codemap_treatment_binds_an_eligible_external_python(self, script_run_agentic: Any) -> None:
        """The staged launcher receives an authoritative interpreter outside the denied checkout."""
        env = script_run_agentic.ModelRunner._subprocess_env("C_strict")

        assert Path(env["CODEMAP_PYTHON"]).resolve() == Path(sys.executable).resolve()

    def test_codemap_treatment_rejects_an_ineligible_external_python(
        self, script_run_agentic: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed external capability probe preserves the staged runtime version gate."""
        _mock_codemap_python_probe(monkeypatch, script_run_agentic, returncode=127)

        with pytest.raises(RuntimeError, match=r"CPython >=3.11,<3.15"):
            script_run_agentic.ModelRunner._eligible_codemap_python()

    @pytest.mark.parametrize("arm", ["plain", "A_plain", "semble", ""])
    @mock.patch.dict("os.environ", dict.fromkeys(_INHERITED_CODEMAP_ENV, "/parent-only/sentinel"))
    def test_non_codemap_arms_strip_inherited_codemap_environment(self, script_run_agentic: Any, arm: str) -> None:
        """Plain controls cannot inherit a launcher, interpreter, or plugin root from the parent process."""
        env = script_run_agentic.ModelRunner._subprocess_env(arm)

        assert not set(_INHERITED_CODEMAP_ENV) & set(env)


# ===========================================================================
# _seed_index_cache — index present in fix-task sandbox
# ===========================================================================


class TestSeedIndexCache:
    """The prebuilt index cache dirs are copied into a sandbox for non-plain arms."""

    @pytest.mark.parametrize("cache_name", ["codemap", "scan"])
    def test_relocates_only_index_root_in_disposable_copy(
        self, script_run_agentic: Any, tmp_path: Path, cache_name: str
    ) -> None:
        """Copied graph queries retain frozen facts but describe the actual disposable tree.

        Scenario: the prebuilt index in either cache dir is copied into a sandbox; only its ``scan_root`` may
        change (to the sandbox), the source index stays byte-identical, and one relocation record carries both hashes.
        """
        repo = tmp_path / "source"
        index = repo / ".cache" / cache_name / "source.json"
        index.parent.mkdir(parents=True)
        payload = {"scan_root": str(repo.resolve()), "scan_version": 13, "modules": {"sample": {"path": "sample.py"}}}
        original = json.dumps(payload).encode()
        index.write_bytes(original)
        sandbox = tmp_path / "copy" / "source"
        sandbox.mkdir(parents=True)
        runner = script_run_agentic.ModelRunner("haiku", script_run_agentic.MODELS["haiku"], repo)

        relocations = runner._seed_index_cache(sandbox)

        copied = sandbox / ".cache" / cache_name / "source.json"
        assert json.loads(copied.read_bytes()) == {**payload, "scan_root": str(sandbox.resolve())}
        assert index.read_bytes() == original
        assert len(relocations) == 1
        assert relocations[0]["frozen_index_sha256"] == hashlib.sha256(original).hexdigest()
        assert relocations[0]["derived_index_sha256"] == hashlib.sha256(copied.read_bytes()).hexdigest()

    @pytest.mark.parametrize(
        ("source_files", "expected"),
        [
            pytest.param(
                (".cache/codemap/myrepo.json", ".cache/scan/myrepo.json"),
                [".cache", ".cache/codemap", ".cache/codemap/myrepo.json", ".cache/scan", ".cache/scan/myrepo.json"],
                id="seeds-codemap-and-scan-cache",
            ),
            pytest.param((), [], id="missing-source-cache-is-skipped-silently"),
        ],
    )
    def test_seeds_only_cache_dirs_present_in_source(
        self, script_run_agentic: Any, tmp_path: Path, source_files: tuple[str, ...], expected: list[str]
    ) -> None:
        """_seed_index_cache copies .cache/codemap and .cache/scan when present and creates nothing otherwise.

        Scenario: fix tasks run in a copy that excludes .cache; without the prebuilt index the codemap arm would
        build it inside the measured window, so it is seeded in. A source with no cache dirs must not raise and
        must leave the sandbox without a .cache directory.
        """
        repo = tmp_path / "myrepo"
        repo.mkdir()
        _touch(repo, *source_files, text="{}")
        sandbox = tmp_path / "sandbox" / "myrepo"
        sandbox.mkdir(parents=True)
        runner = script_run_agentic.ModelRunner("haiku", script_run_agentic.MODELS["haiku"], repo, timeout=300)

        runner._seed_index_cache(sandbox)

        assert sorted(path.relative_to(sandbox).as_posix() for path in sandbox.rglob("*")) == expected


# ===========================================================================
# Semble chunk-hit rate lens
# ===========================================================================


class TestSembleChunkHitRate:
    """chunk_hit_rate credits expected rdeps whose module/file appears in any semble chunk."""

    def test_chunk_hit_rate_partial_when_one_of_two_in_chunks(self, script_run_agentic: Any, ground_truth: Any) -> None:
        """One of two expected rdeps appears in the semble chunks → chunk_hit_rate = 0.5.

        Scenario: BA-01 has two expected rdeps; a semble result mentioning only one file must
        yield 0.5 without requiring the exhaustive dotted rdep list.
        """
        chunks = "File: src/lightning/pytorch/trainer/trainer.py\nLine 10: import timer"
        result = _score_corpus(ground_truth, semble_result_text=chunks)
        assert result.chunk_hit_rate == pytest.approx(0.5)

    def test_chunk_hit_rate_none_when_no_semble_corpus(self, script_run_agentic: Any, ground_truth: Any) -> None:
        """chunk_hit_rate stays None for arms that carry no semble corpus (plain / codemap)."""
        result = _score_corpus(ground_truth, "x", semble_result_text=None)
        assert result.chunk_hit_rate is None


# ===========================================================================
# Report rendering — success rate, quality, savings n, failures
# ===========================================================================


class TestReportRendering:
    """The report surfaces success rate, quality medians, savings denominators, and failures."""

    def _run(
        self,
        script: Any,
        task_id: str,
        arm: str,
        success: bool,
        erec: float = 0.0,
        chunk: float | None = None,
    ) -> Any:
        """Build a BenchmarkRun with minimal metrics and a scored QualityScore."""
        run = script.BenchmarkRun(arm=arm, task_id=task_id, task_type="fix", model="haiku", success=success)
        run.tools.grep = 4
        run.input_tokens = 1000
        run.elapsed_s = 10.0
        run.quality = script.QualityScore(scored=True, erec=erec, rrec=erec, chunk_hit_rate=chunk)
        return run

    @pytest.fixture(name="report")
    def _report(self, script_run_agentic: Any) -> Any:
        """Report with a plain+codemap success pair and one codemap failure."""
        tasks = [script_run_agentic.Task(id="BA-01", type="blast_radius_analysis", prompt="p")]
        results = [
            self._run(script_run_agentic, "BA-01", "plain", success=True, erec=0.5),
            self._run(script_run_agentic, "BA-01", "codemap", success=True, erec=0.75),
            self._run(script_run_agentic, "BA-01", "codemap", success=False),
        ]
        return script_run_agentic.Report(results, tasks, {"date": "2026-07-03"})

    def test_prospective_canonical_metadata_renders_graded_quality_not_legacy_savings(
        self, script_run_agentic: Any
    ) -> None:
        """A prospective canonical report renders graded quality before its exact-pass diagnostic."""
        complete = {
            "success": True,
            "answer_contract_valid": True,
            "answer_pooling_eligible": True,
            "treatment_adherence": True,
            "quality": {
                "correct": True,
                "quality_score": 1.0,
                "components": {"importers": 1.0},
                "graded_score": 1.0,
                "graded_components": {"importers": 1.0},
            },
            "input_tokens": 100,
            "cached_input_tokens": 20,
            "fresh_input_tokens": 80,
            "output_tokens": 10,
            "elapsed_s": 5.0,
        }
        rows = [{"task_id": "BA-01", "repetition": 1, "arm": arm, **complete} for arm in ("A_plain", "C_strict")]
        rows.append(
            {
                "task_id": "BA-02",
                "repetition": 1,
                "arm": "C_strict",
                **complete,
                "quality": {
                    "correct": False,
                    "quality_score": 0.5,
                    "components": {"importers": 0.5},
                    "graded_score": 0.75,
                    "graded_components": {"importers": 0.75},
                },
                "failure_details": [
                    {
                        "category": "missing_facts",
                        "field": "production_importers",
                        "actual": [],
                        "expected": ["package.consumer"],
                    }
                ],
            }
        )
        summary = script_run_agentic.summarize_agentic(
            rows,
            task_ids=["BA-01", "BA-02"],
            repetitions=1,
        )
        tasks = [
            script_run_agentic.Task(id="BA-01", type="blast_radius_analysis", prompt="p"),
            script_run_agentic.Task(id="BA-02", type="blast_radius_analysis", prompt="p"),
        ]
        report = script_run_agentic.Report(
            [],
            tasks,
            {
                "date": "2026-09-08",
                "models": "haiku",
                "agentic_reporting": {
                    "reporting_version": script_run_agentic.agentic_reporting.REPORTING_VERSION,
                    "summaries_by_model": {"haiku": summary},
                },
            },
        ).render()

        assert "## Canonical graded quality summary" in report
        assert "quality=50.0%" in report
        assert "exact_pass=" in report
        assert "quality  A_plain-vs-C_strict" in report
        assert "component=" in report
        assert "both_pass_only" in report
        assert "FAILURE  BA-02 rep=1 C_strict categories=missing_facts" in report
        assert "Savings =" not in report

    @pytest.mark.parametrize(
        "section",
        [
            pytest.param("Success rate (successful / total runs)", id="success-rate-table"),
            pytest.param("Exposure recall (erec)", id="erec-quality-table"),
            pytest.param("Chunk hit rate (semble lens)", id="chunk-hit-quality-table"),
            pytest.param("### Failed runs", id="failed-runs-section"),
        ],
    )
    def test_render_includes_section(self, script_run_agentic: Any, report: Any, section: str) -> None:
        """The rendered report contains its success-rate table, quality tables, and failed-runs section.

        The quality tables cover erec and chunk-hit rate; failed runs are listed explicitly, not silently dropped.
        """
        assert section in report.render()

    def test_savings_summary_has_pair_count_n(self, script_run_agentic: Any, report: Any) -> None:
        """Every savings row carries an 'n' pair-count denominator."""
        agg = script_run_agentic.aggregate(report.results, report.task_ids, model_short="haiku")
        rows = report._savings_summary(agg)
        assert rows
        assert all("n" in row for row in rows)

    def test_success_table_counts_failures(self, script_run_agentic: Any, report: Any) -> None:
        """The success table reports 1/2 for the codemap cell (one success, one failure)."""
        counts = report._cell_counts("haiku")
        assert counts["BA-01"]["codemap"] == [2, 1]


class TestReportFixFamilySuppression:
    """Fix-family suites emit no biased efficiency savings row."""

    def _fix_run(self, script: Any, arm: str) -> Any:
        """Build a successful fix_multicaller BenchmarkRun."""
        run = script.BenchmarkRun(arm=arm, task_id="FM-01", task_type="fix_multicaller", model="haiku", success=True)
        run.tools.grep = 4
        run.input_tokens = 1000
        run.elapsed_s = 10.0
        run.quality = script.QualityScore(scored=True, erec=1.0, rrec=1.0)
        return run

    def test_efficiency_savings_suppressed_for_fix_suite(self, script_run_agentic: Any) -> None:
        """A pure fix_multicaller suite yields no efficiency savings rows.

        Scenario: fix tasks differ in edit workload, so token / tool-call savings would be biased;
        _savings_summary must exclude them, leaving no rows for an all-fix suite.
        """
        tasks = [script_run_agentic.Task(id="FM-01", type="fix_multicaller", prompt="p")]
        results = [self._fix_run(script_run_agentic, "plain"), self._fix_run(script_run_agentic, "codemap")]
        report = script_run_agentic.Report(results, tasks, {"date": "2026-07-03"})
        agg = script_run_agentic.aggregate(results, ["FM-01"], model_short="haiku")
        assert report._savings_summary(agg) == []

    def test_arm_cells_savings_not_applicable_shows_na(self, script_run_agentic: Any) -> None:
        """_arm_cells renders 'n/a' savings when savings_applicable is False."""
        tasks = [script_run_agentic.Task(id="FM-01", type="fix_multicaller", prompt="p")]
        report = script_run_agentic.Report([], tasks, {"date": "2026-07-03"})
        cells = report._arm_cells("codemap", 10.0, 5.0, script_run_agentic.Report._fmt_s, savings_applicable=False)
        assert cells["Codemap savings"] == "n/a"


# ===========================================================================
# Atomic snapshot persistence
# ===========================================================================


class TestAtomicSnapshot:
    """_save_snapshot writes via a temp file + os.replace so an interrupt cannot truncate it."""

    @pytest.fixture(name="benchmark")
    def _benchmark(self, script_run_agentic: Any, make_benchmark: Any) -> Any:
        """Minimal Benchmark whose output path lives in a writable temp dir."""
        task = script_run_agentic.Task(id="BA-01", type="blast_radius_analysis", prompt="p")
        return make_benchmark(task, ["codemap"])

    def test_snapshot_writes_valid_json_and_leaves_no_tmp(self, script_run_agentic: Any, benchmark: Any) -> None:
        """The rolling snapshot is valid JSON and no .tmp residue remains after a successful write.

        Scenario: a run has accumulated; _save_snapshot persists it and the file must round-trip
        through json.load with no half-written temp file left in the directory.
        """
        benchmark.results.append(
            script_run_agentic.BenchmarkRun(
                arm="codemap", task_id="BA-01", task_type="fix", model="haiku", success=True
            )
        )
        benchmark._save_snapshot({"date": "2026-07-03"})
        loaded = json.loads(benchmark.output_path.read_text())
        assert loaded["results"][0]["task_id"] == "BA-01"
        assert not list(benchmark.output_path.parent.glob("*.tmp"))

    def test_snapshot_uses_os_replace_from_a_temp_source(
        self, script_run_agentic: Any, benchmark: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The final file is produced by os.replace from a temp file, not written in place.

        Scenario: an interrupt mid-write must never truncate the real file; the only way that holds
        is if the payload lands via an atomic rename from a distinct temp path.
        """
        seen: dict[str, Any] = {}
        real_replace = script_run_agentic.os.replace

        def _spy_replace(src: Any, dst: Any) -> Any:
            """Record source and destination before performing the real atomic replacement."""
            seen["src"], seen["dst"] = str(src), str(dst)
            return real_replace(src, dst)

        monkeypatch.setattr(script_run_agentic.os, "replace", _spy_replace)

        benchmark._save_snapshot({"date": "2026-07-03"})

        assert seen["src"].endswith(".tmp")
        assert seen["src"] != str(benchmark.output_path)
        assert seen["dst"] == str(benchmark.output_path)

    @mock.patch("json.dump", autospec=True, side_effect=RuntimeError("boom"))
    def test_snapshot_failure_cleans_temp_and_preserves_nothing_partial(
        self, mock_dump: mock.Mock, benchmark: Any
    ) -> None:
        """A serialisation failure removes the temp file and never overwrites the target.

        Scenario: json.dump raises mid-write; the pre-existing (or absent) results file must stay
        untouched and no orphan .tmp file may be left behind.
        """
        with pytest.raises(RuntimeError):
            benchmark._save_snapshot({"date": "2026-07-03"})

        assert not benchmark.output_path.exists()
        assert not list(benchmark.output_path.parent.glob("*.tmp"))


# ===========================================================================
# Degenerate-loop classification ordering
# ===========================================================================


class TestDegenerateLoopClassification:
    """A grep-heavy zero-skill BA codemap run is degenerate_grep_loop, not a plain no-call."""

    @pytest.fixture(name="classify")
    def _classify(
        self, script_run_agentic: Any, make_benchmark: Any, run_cell: Any, monkeypatch: pytest.MonkeyPatch
    ) -> Any:
        """Return a function driving one blast-radius codemap cell whose native run result is the one supplied."""
        task = script_run_agentic.Task(
            id="BA-01",
            type="blast_radius_analysis",
            prompt="p",
            primary_module="lightning.pytorch.callbacks.timer",
        )
        benchmark = make_benchmark(task, ["codemap"])

        def classify(crafted: Any) -> Any:
            monkeypatch.setattr(script_run_agentic.ModelRunner, "run", lambda _self, *_a, **_kw: crafted)
            return run_cell(benchmark, task, "codemap", metadata={"date": "2026-07-03"})

        return classify

    @pytest.fixture(name="crafted")
    def _crafted(self, script_run_agentic: Any) -> Any:
        """Successful blast-radius codemap run result with no tool calls yet."""
        return script_run_agentic.BenchmarkRun(
            arm="codemap", task_id="BA-01", task_type="blast_radius_analysis", model="haiku", success=True
        )

    def test_grep_heavy_zero_skill_ba_run_is_degenerate(self, classify: Any, crafted: Any) -> None:
        """BA codemap run with skill==0 and ≥70% grep-like calls is labelled degenerate_grep_loop.

        Scenario: the no-skill-call guard used to claim this run first ("codemap skill never
        called"), leaving the grep-ratio classification unreachable for BA tasks.
        """
        crafted.tools.grep = 8  # total 8, all grep-like, skill 0 → ratio 1.0
        result = classify(crafted)
        assert result.error_type == "degenerate_grep_loop"
        assert result.success is False

    def test_zero_skill_but_not_grep_heavy_falls_through_to_no_call(self, classify: Any, crafted: Any) -> None:
        """A zero-skill BA run below the grep threshold still fails as 'codemap skill never called'.

        Scenario: the reordering must not swallow the no-call guard — a skill==0 run that is NOT
        grep-dominated keeps the original no-call label.
        """
        crafted.tools.grep = 1
        crafted.tools.bash = 9  # total 10, grep-like 1 → ratio 0.1 < 0.70
        result = classify(crafted)
        assert result.error_type != "degenerate_grep_loop"
        assert result.error == "codemap skill never called"
        assert result.success is False


# ===========================================================================
# top10 centrality axis — in-degree not out-degree
# ===========================================================================


class TestTop10InDegree:
    """top10_expected ranks central rdeps by in-degree (reverse-dep count), not dep_count."""

    @pytest.fixture(name="gt")
    def _gt(self, script_run_agentic: Any, tmp_path: Path) -> Any:
        """Index with 11 reverse deps: reverse1..reverse10 imported once; reverse11 has high dep_count."""
        modules: list[dict] = [{"name": "app.target", "direct_imports": [], "dep_count": 0, "status": "ok"}]
        for i in range(1, 11):
            modules.append({"name": f"app.reverse{i}", "direct_imports": ["app.target"], "status": "ok"})
            # One importer for each reverse dependency gives it in-degree 1.
            modules.append({"name": f"imp.reverse{i}", "direct_imports": [f"app.reverse{i}"], "status": "ok"})
        # reverse11 has huge out-degree but zero in-degree: nobody imports it.
        modules.append(
            {"name": "app.reverse11", "direct_imports": ["app.target", "x.a", "x.b"], "dep_count": 999, "status": "ok"}
        )
        index_file = tmp_path / "idx.json"
        index_file.write_text(json.dumps(_minimal_index(modules)))
        task = _make_task(script_run_agentic, id="X", primary_module="app.target")
        return script_run_agentic.GroundTruth(index_file, [task])

    def test_high_in_degree_kept_and_high_out_degree_dropped(self, gt: Any) -> None:
        """The zero-in-degree / high-dep_count rdep is dropped; an in-degree≥1 rdep is kept.

        Scenario: 11 rdeps trim to 10. Ranking by in-degree drops app.reverse11 (imported by nobody);
        ranking by the old dep_count axis would have kept it and dropped a real central module.
        """
        top10 = gt.top10_expected["X"]
        assert len(top10) == 10
        assert "app.reverse1" in top10
        assert "app.reverse11" not in top10


# ===========================================================================
# Keyword scoring — whitespace tolerance + opt-in test signal
# ===========================================================================


class TestFixKeywordNormalization:
    """score_fix / score_read_crop tolerate operator whitespace and carry the test_passed signal."""

    def test_score_fix_matches_operator_keyword_despite_whitespace(self, script_run_agentic: Any) -> None:
        """A '< 1' keyword matches a '<1' diff line after whitespace normalisation.

        Scenario: the agent writes 'if patience<1:' while the ground-truth keyword is 'patience < 1';
        the recall scorer must credit it rather than penalising the spacing variant.
        """
        diff = "--- a/x.py\n+++ b/x.py\n+    if patience<1:\n+        raise ValueError\n"
        score = script_run_agentic.score_fix(diff, ["patience < 1"], [])
        assert score.erec == pytest.approx(1.0)

    @pytest.mark.parametrize(
        ("answer", "keywords", "expected_erec"),
        [
            pytest.param("guard returns <1 on misconfig", ["< 1"], 1.0, id="operator-keyword-despite-whitespace"),
            pytest.param("this text has no such token here", ["raise Error"], 0.0, id="word-boundaries-preserved"),
        ],
    )
    def test_score_read_crop_normalises_whitespace_without_merging_words(
        self, script_run_agentic: Any, answer: str, keywords: list[str], expected_erec: float
    ) -> None:
        """score_read_crop credits a '< 1' keyword when the answer writes '<1', yet never merges distinct words.

        Normalisation keeps word-word spaces so distinct identifiers are not merged: an answer that never
        mentions 'raise Error' must not falsely match it just because whitespace was collapsed elsewhere.
        """
        score = script_run_agentic.score_read_crop(answer, keywords)
        assert score.erec == pytest.approx(expected_erec)

    @pytest.mark.parametrize(
        ("test_passed", "expected"),
        [pytest.param(True, True, id="passed"), pytest.param(False, False, id="failed")],
    )
    def test_score_fix_records_test_passed_when_supplied(
        self, script_run_agentic: Any, test_passed: bool, expected: bool
    ) -> None:
        """score_fix stores the supplied targeted-test outcome alongside erec."""
        diff = "+++ b/x.py\n+    fixed = True\n"
        score = script_run_agentic.score_fix(diff, ["fixed"], ["x.py"], test_passed=test_passed)
        assert score.test_passed is expected
        assert score.erec == pytest.approx(1.0)  # erec column is unchanged by the test signal

    def test_score_fix_test_passed_defaults_to_none(self, script_run_agentic: Any) -> None:
        """A task with no declared test leaves test_passed=None."""
        score = script_run_agentic.score_fix("+++ b/x.py\n+ fixed\n", ["fixed"], ["x.py"])
        assert score.test_passed is None


class TestRunTargetedTest:
    """_run_targeted_test runs pytest on the post-edit sandbox and reports pass/fail/None."""

    @pytest.mark.parametrize(
        ("filename", "body", "expected"),
        [
            pytest.param("test_ok.py", "def test_ok():\n    assert 1 + 1 == 2\n", True, id="passing-target-is-true"),
            pytest.param("test_bad.py", "def test_bad():\n    assert False\n", False, id="failing-target-is-false"),
        ],
    )
    def test_target_outcome_is_true_or_false(
        self, runner: Any, tmp_path: Path, filename: str, body: str, expected: bool
    ) -> None:
        """A passing pytest node yields True; a failing one yields False (not None — the run launched fine)."""
        (tmp_path / filename).write_text(body)
        assert runner._run_targeted_test(tmp_path, filename) is expected


def _completed_stream() -> Any:
    """Return the transport outcome of a stream that finished normally without a model."""
    return SimpleNamespace(error=None, stderr="", returncode=0, exc_timeout=False, elapsed_s=0.1)


def _flag_with_value(cmd: list[str], flag: str) -> list[str]:
    """Return ``flag`` and the argument following it, or an empty list when ``flag`` is absent from ``cmd``."""
    return [arg for previous, arg in zip(["", *cmd], cmd) if arg == flag or previous == flag]


def _launch_snapshot(captured: dict[str, Any]) -> Any:
    """Build a ``stream_claude`` side effect that records argv, env and the settings file while it still exists."""

    def _stream(cmd: list[str], **kwargs: Any) -> Any:
        """Capture the no-model transport contract and supply a normal completion."""
        settings_path = Path(cmd[cmd.index("--settings") + 1])
        captured.update(cmd=cmd, environment=kwargs["env"], settings=json.loads(settings_path.read_text("utf-8")))
        return _completed_stream()

    return _stream


def _write_private_oracles(fixture: Path) -> None:
    """Plant an ``oracle.txt`` under each private directory a staged plugin copy must leave behind."""
    for private_directory in (".cache", ".plans", ".reports", ".pytest_cache", "__pycache__", "tests"):
        sentinel = fixture / private_directory / "oracle.txt"
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text("PRIVATE-ORACLE\n", encoding="utf-8")


@pytest.fixture(name="stream_spy")
def _stream_spy() -> Any:
    """Replace the Claude stream transport with an autospecced double that completes normally."""
    with mock.patch(
        "_bench_claude.agentic.runner.stream_claude", autospec=True, return_value=_completed_stream()
    ) as spy:
        yield spy


@pytest.fixture(name="worktree")
def _worktree(tmp_path: Path) -> Path:
    """Empty disposable worktree directory beside the runner root."""
    path = tmp_path / "worktree"
    path.mkdir()
    return path


class TestStageTransport:
    """Canonical stage transport: permission mode, evidence isolation and runtime staging before launch."""

    @pytest.mark.parametrize(
        ("writable", "expected"),
        [pytest.param(False, [], id="false"), pytest.param(True, ["--permission-mode", "acceptEdits"], id="true")],
    )
    def test_native_edits_only_for_executable_workspaces(
        self, runner: Any, stream_spy: mock.MagicMock, worktree: Path, writable: bool, expected: list[str]
    ) -> None:
        """Headless fix cells authorize native edits without making ReadCrop writable.

        Regression: executable Claude cells inherited the default interactive
        permission mode, so every Edit/Write request was denied until timeout.
        """
        runner.run_stage_events(
            prompt="Inspect the fixture.",
            system_prompt="Use only the fixture.",
            arm="A_plain",
            cwd=worktree,
            writable=writable,
        )

        assert _flag_with_value(stream_spy.call_args.args[0], "--permission-mode") == expected

    def test_denies_benchmark_evidence_to_filetools_and_bash(
        self, runner: Any, stream_spy: mock.MagicMock, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, worktree: Path
    ) -> None:
        """A canonical Claude cell receives an OS-enforced evidence deny and file-tool denies.

        The regression is a sandboxed evaluator reading a sibling oracle or earlier result through Bash while its
        ordinary Claude file tools remain unrestricted. The stage transport must load one temporary Claude settings file
        that blocks both routes while preserving its disposable worktree and staged runtime.
        """
        evidence_root = tmp_path / "evaluator"
        evidence_root.mkdir()
        (evidence_root / "oracle.json").write_text("answer\n", encoding="utf-8")
        captured: dict[str, Any] = {}
        stream_spy.side_effect = _launch_snapshot(captured)
        monkeypatch.setenv("BENCHMARK_EVIDENCE_ROOTS", json.dumps([str(evidence_root)]))

        runner.run_stage_events(
            prompt="Inspect the fixture.", system_prompt="Use only the fixture.", arm="A_plain", cwd=worktree
        )

        settings = captured["settings"]
        expected_deny = f"Read(//{evidence_root.as_posix().lstrip('/')}/**)"
        assert settings["sandbox"]["enabled"] is True
        assert settings["sandbox"]["failIfUnavailable"] is True
        assert str(evidence_root) in settings["sandbox"]["filesystem"]["denyRead"]
        assert str(worktree) in settings["sandbox"]["filesystem"]["allowRead"]
        assert settings["permissions"]["deny"] == [expected_deny]
        assert "BENCHMARK_EVIDENCE_ROOTS" not in captured["environment"]

    def test_stages_runtime_outside_denied_evidence(
        self,
        script_run_agentic: Any,
        runner: Any,
        stream_spy: mock.MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        worktree: Path,
    ) -> None:
        """Structural cells never re-allow a plugin nested under evaluator evidence.

        Regression: the original Codemap fixture lives in the evaluator checkout. Adding
        it to ``allowRead`` would contradict the broad evidence deny for both native Bash
        and Claude file tools. The transport must copy it beside the disposable worktree
        and load only that copy.
        """
        evidence_root = tmp_path / "evaluator"
        fixture = evidence_root / "plugins" / "codemap-py"
        (fixture / ".claude-plugin").mkdir(parents=True)
        (fixture / ".claude-plugin" / "plugin.json").write_text("{}\n", encoding="utf-8")
        (fixture / "claude-skills" / "query-code").mkdir(parents=True)
        (fixture / "claude-skills" / "query-code" / "SKILL.md").write_text("skill\n", encoding="utf-8")
        (fixture / "bin").mkdir()
        (fixture / "bin" / "codemap-py").write_text("runtime\n", encoding="utf-8")
        _write_private_oracles(fixture)
        captured: dict[str, Any] = {}
        snapshot = _launch_snapshot(captured)

        def _stream(cmd: list[str], **kwargs: Any) -> Any:
            """Capture the staged runtime while it exists, on top of the shared launch snapshot."""
            plugin_dir = Path(cmd[cmd.index("--plugin-dir") + 1])
            captured.update(
                plugin_dir=plugin_dir,
                runtime_file=(plugin_dir / "bin" / "codemap-py").read_text(encoding="utf-8"),
                copied_private_files=[p.relative_to(plugin_dir).as_posix() for p in plugin_dir.rglob("oracle.txt")],
            )
            return snapshot(cmd, **kwargs)

        stream_spy.side_effect = _stream
        monkeypatch.setenv("BENCHMARK_EVIDENCE_ROOTS", json.dumps([str(evidence_root)]))
        monkeypatch.setattr(script_run_agentic.ModelRunner, "_codemap_plugin_dir", lambda: str(fixture))
        _mock_codemap_python_probe(monkeypatch, script_run_agentic)

        runner.run_stage_events(
            prompt="Inspect the fixture.", system_prompt="Use only the fixture.", arm="C_strict", cwd=worktree
        )

        plugin_dir = captured["plugin_dir"]
        allow_read = captured["settings"]["sandbox"]["filesystem"]["allowRead"]
        assert not plugin_dir.is_relative_to(evidence_root)
        assert plugin_dir.is_relative_to(worktree.parent)
        assert captured["runtime_file"] == "runtime\n"
        assert captured["copied_private_files"] == []
        assert captured["environment"]["CLAUDE_PLUGIN_ROOT"] == str(plugin_dir)
        assert str(plugin_dir) in allow_read
        assert str(fixture) not in allow_read
        assert not plugin_dir.exists()

    def test_missing_evidence_root_fails_before_launch(
        self, runner: Any, stream_spy: mock.MagicMock, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, worktree: Path
    ) -> None:
        """A future or misspelled result root cannot downgrade a cell to unsafe mode.

        The evidence root list names a directory that was never created; the transport must refuse before it would hand
        the model an unconfined workspace.
        """
        monkeypatch.setenv("BENCHMARK_EVIDENCE_ROOTS", json.dumps([str(tmp_path / "not-created")]))

        with pytest.raises(ValueError, match="benchmark evidence root is unavailable"):
            runner.run_stage_events(
                prompt="Inspect the fixture.", system_prompt="Use only the fixture.", arm="A_plain", cwd=worktree
            )

        stream_spy.assert_not_called()


def test_evidence_settings_reject_allowing_a_path_nested_under_denied_evidence(
    script_run_agentic: Any, tmp_path: Path
) -> None:
    """A nested allow is rejected before Claude can receive contradictory policy.

    The runtime directory sits inside the denied evidence root, so allowing it would contradict the deny.
    """
    evidence_root = tmp_path / "evaluator"
    worktree = tmp_path / "worktree"
    runtime = evidence_root / "plugins" / "codemap-py"
    worktree.mkdir()
    runtime.mkdir(parents=True)

    with pytest.raises(ValueError, match="cannot be inside a denied evidence root"):
        script_run_agentic._claude_evidence_isolation_settings(
            worktree, evidence_roots=[evidence_root], runtime_paths=[runtime]
        )


@pytest.fixture(name="impact_fixture")
def _impact_fixture(script_run_agentic: Any, tmp_path: Path) -> SimpleNamespace:
    """Parent-owned disposable change-impact fixture: source tree, frozen index, run dir and runtime coordinate."""
    source_root = tmp_path / "repo"
    (source_root / "package").mkdir(parents=True)
    (source_root / "package" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    index_path = source_root / ".cache" / "codemap" / "repo.json"
    index_path.parent.mkdir(parents=True)
    index_path.write_text(json.dumps({"scan_version": 1, "scan_root": str(source_root), "modules": []}), "utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    coordinate = {
        "source_fingerprint": script_run_agentic.change_impact_source_fingerprint(source_root),
        "raw_index_sha256": hashlib.sha256(index_path.read_bytes()).hexdigest(),
        "scan_version": 1,
        "index_scan_root": str(source_root),
    }
    return SimpleNamespace(source_root=source_root, index_path=index_path, run_dir=run_dir, coordinate=coordinate)


@pytest.fixture(name="open_impact_runtime")
def _open_impact_runtime(script_run_agentic: Any, impact_fixture: SimpleNamespace) -> Any:
    """Return a factory opening the Claude change-impact runtime context over ``impact_fixture``."""

    def open_runtime(*, dry_run: bool) -> Any:
        return script_run_agentic.impact_runtime(
            model="sonnet",
            source_root=impact_fixture.source_root,
            index_path=impact_fixture.index_path,
            run_dir=impact_fixture.run_dir,
            timeout=600,
            dry_run=dry_run,
            fixture_runtime_coordinate=impact_fixture.coordinate,
        )

    return open_runtime


@pytest.fixture(name="run_impact_cell")
def _run_impact_cell(open_impact_runtime: Any) -> Any:
    """Return a runner executing one ``CI-01`` change-impact cell for an arm and returning its result row."""

    def run(arm: str) -> dict[str, Any]:
        with open_impact_runtime(dry_run=False) as adapter:
            assert adapter is not None
            return adapter.run_cell({"id": "CI-01"}, arm, "Classify callsites.")

    return run


class TestChangeImpactRuntime:
    """Claude change-impact runtime adapter: fixture admission and normalisation of native stream facts."""

    @mock.patch("_bench_claude.agentic.paid._native_change_impact_preflight", autospec=True)
    def test_validates_only_in_dry_run(
        self, preflight: mock.MagicMock, open_impact_runtime: Any, impact_fixture: SimpleNamespace
    ) -> None:
        """A no-model preflight validates fixture identity without exposing a launch callable.

        A dry run must reach the native preflight with the source and evidence roots and hand back no runner.
        """
        with open_impact_runtime(dry_run=True) as run_cell:
            assert run_cell is None

        preflight.assert_called_once_with(impact_fixture.source_root, impact_fixture.run_dir)

    @mock.patch("_bench_claude.agentic.runner.ModelRunner.run_stage_events", autospec=True)
    def test_normalizes_native_stream_facts(
        self,
        run_stage_events: mock.MagicMock,
        script_run_agentic: Any,
        impact_fixture: SimpleNamespace,
        run_impact_cell: Any,
    ) -> None:
        """The adapter reports native Claude facts without converting an answer into a pass claim.

        A plain-arm stream with one answer and a terminal success carries usage; the row must expose the measured token
        totals and elapsed time while the treatment-only fields stay at their neutral values.
        """
        events = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "report"}]}},
            {
                "type": "result",
                "subtype": "success",
                "usage": {"input_tokens": 10, "cache_read_input_tokens": 2, "output_tokens": 3},
            },
        ]
        run_stage_events.return_value = (events, 1.5, None)

        row = run_impact_cell("A_plain")

        kwargs = run_stage_events.call_args.kwargs
        assert kwargs["cwd"] == impact_fixture.source_root
        assert kwargs["evidence_roots"] == (impact_fixture.run_dir,)
        assert kwargs["writable"] is False
        assert script_run_agentic.ARM_CONTRACTS["A_plain"]["contract"] in kwargs["system_prompt"]
        assert row == {
            "success": True,
            "incomplete": False,
            "contaminated": False,
            "report_text": "report",
            "raw_events": events,
            "input_tokens": 12,
            "cached_input_tokens": 2,
            "output_tokens": 3,
            "usage_complete": True,
            "elapsed_s": 1.5,
            "command_calls": 0,
            "codemap_calls": 0,
            "codemap_used": False,
            "codemap_query_attempted": 0,
            "codemap_query_succeeded": 0,
            "codemap_compact_success": False,
            "treatment_adherence": True,
            "error": None,
            "error_type": None,
            "launcher_path": None,
        }

    def test_rejects_stale_fixture_coordinate(self, open_impact_runtime: Any, impact_fixture: SimpleNamespace) -> None:
        """Fixture/index drift stops before a provider transport can receive unsafe bytes.

        The frozen index is rewritten after the coordinate was taken, so its fingerprint no longer matches.
        """
        impact_fixture.index_path.write_text(
            json.dumps({"scan_version": 2, "scan_root": str(impact_fixture.source_root), "modules": []}), "utf-8"
        )

        with pytest.raises(ValueError, match="frozen index fingerprint drifted"):
            with open_impact_runtime(dry_run=True):
                pass

    @pytest.mark.parametrize(
        ("arm", "events", "expected"),
        [
            pytest.param(
                "C_strict",
                [{"type": "result", "subtype": "success", "usage": {"input_tokens": 1, "output_tokens": 1}}],
                {"success": True, "incomplete": False, "treatment_adherence": False},
                id="completed-nonadherent-c-cell-stays-observed",
            ),
            pytest.param(
                "A_plain",
                [{"type": "result", "subtype": "success"}],
                {
                    "success": True,
                    "incomplete": False,
                    "usage_complete": False,
                    "input_tokens": None,
                    "cached_input_tokens": None,
                    "output_tokens": None,
                },
                id="terminal-success-without-usage-is-unscoped",
            ),
            pytest.param(
                "A_plain",
                [],
                {"success": False, "incomplete": True, "usage_complete": False, "input_tokens": None},
                id="missing-terminal-result-is-incomplete",
            ),
        ],
    )
    @mock.patch("_bench_claude.agentic.runner.ModelRunner.run_stage_events", autospec=True)
    def test_row_reflects_stream_outcome(
        self,
        run_stage_events: mock.MagicMock,
        run_impact_cell: Any,
        arm: str,
        events: list[dict[str, Any]],
        expected: dict[str, Any],
    ) -> None:
        """The row classifies completion, usage and treatment adherence from the stream's terminal facts alone.

        A completed C response without Codemap use is a treatment violation, not an incomplete execution; a terminal
        success without native usage keeps its answer but has unavailable resource measurements; a stream with no
        terminal provider result is incomplete and has no measured usage.
        """
        run_stage_events.return_value = (events, 1.0, None)

        row = run_impact_cell(arm)

        assert {key: row[key] for key in expected} == expected


class TestChangeImpactCli:
    """Change-impact CLI entry: argument validation and delegation to the shared stage."""

    @mock.patch("_bench_claude.agentic.cli.run_change_impact_stage", autospec=True)
    def test_delegates_approved_runtime_coordinate(
        self, stage: mock.MagicMock, script_run_agentic: Any, tmp_path: Path
    ) -> None:
        """The impact CLI passes its only mutable paid coordinates to the shared stage.

        The shared stage is replaced so no scope is resolved and no Claude process starts.
        """
        run_dir = tmp_path / "new-run"

        script_run_agentic.main(
            study="change-impact",
            model="sonnet",
            paid_approval="impact-approved",
            scope_sha256="scope",
            timeout=123,
            run_dir=run_dir,
        )

        assert stage.call_args.kwargs == {
            "provider": "claude",
            "dry_run": False,
            "resolve_scope_requested": False,
            "answers_file": None,
            "output_dir": run_dir,
            "model": "sonnet",
            "paid_approval": "impact-approved",
            "timeout": 123,
            "runtime_factory": script_run_agentic.impact_runtime,
            "scope_sha256": "scope",
        }

    @mock.patch("_bench_claude.agentic.cli.run_change_impact_stage", autospec=True)
    def test_rejects_unknown_model_before_shared_stage(self, stage: mock.MagicMock, script_run_agentic: Any) -> None:
        """An invalid impact model cannot reach scope resolution, output setup, or provider admission."""
        with pytest.raises(SystemExit, match="change-impact model"):
            script_run_agentic.main(study="change-impact", model="not-a-model")

        stage.assert_not_called()


def _anthropic_sse(events: list[dict[str, Any]]) -> bytes:
    """Encode one minimal Anthropic streaming response for the installed Claude CLI."""
    return b"".join(
        b"event: "
        + event["type"].encode("utf-8")
        + b"\n"
        + b"data: "
        + json.dumps(event, separators=(",", ":")).encode("utf-8")
        + b"\n\n"
        for event in events
    )


@_skip_claude_sandbox_integration
def test_installed_claude_sandbox_denies_bash_evidence_via_loopback_model_mock(
    script_run_agentic: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Real Claude settings deny an oracle read requested by a loopback-only model mock.

    This is an opt-in provider integration check, not a transport-double assertion: the installed Claude executable
    receives a dummy API key and a local Anthropic streaming endpoint, then its own Bash tool tries to read the denied
    sentinel.
    """
    evaluator_root = tmp_path / "evaluator"
    history_root = tmp_path / "result-history"
    sibling_root = tmp_path / "sibling-arm"
    for root in (evaluator_root, history_root, sibling_root):
        root.mkdir()
    oracle = evaluator_root / "oracle.txt"
    history = history_root / "previous.json"
    sibling = sibling_root / "telemetry.json"
    oracle.write_text("ORACLE-DO-NOT-EXPOSE\n", encoding="utf-8")
    history.write_text("HISTORY-DO-NOT-EXPOSE\n", encoding="utf-8")
    sibling.write_text("SIBLING-DO-NOT-EXPOSE\n", encoding="utf-8")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    allowed_source = worktree / "allowed.py"
    allowed_index = worktree / ".cache" / "codemap" / "worktree.json"
    allowed_source.write_text('def allowed() -> str:\n    return "SOURCE-ALLOWED"\n', encoding="utf-8")
    allowed_index.parent.mkdir(parents=True)
    plugin_root = Path(script_run_agentic.ModelRunner._codemap_plugin_dir())
    build_env = os.environ.copy()
    build_env["CODEMAP_PYTHON"] = str(Path(sys.executable).resolve())
    index_build = subprocess.run(
        [str(plugin_root / "bin" / "codemap-py"), "index", "--root", str(worktree)],
        capture_output=True,
        check=False,
        env=build_env,
        text=True,
        timeout=30,
    )
    assert index_build.returncode == 0, index_build.stderr
    tool_uses = [
        ("toolu_oracle_bash", "Bash", {"command": f"cat {oracle}"}),
        ("toolu_history_bash", "Bash", {"command": f"cat {history}"}),
        ("toolu_sibling_bash", "Bash", {"command": f"cat {sibling}"}),
        ("toolu_source_bash", "Bash", {"command": f"cat {allowed_source}"}),
        ("toolu_index_bash", "Bash", {"command": f"cat {allowed_index}"}),
        ("toolu_oracle_read", "Read", {"file_path": str(oracle)}),
    ]
    requests: list[dict[str, Any]] = []

    class _LoopbackAnthropicHandler(http.server.BaseHTTPRequestHandler):
        """Serve a deterministic tool-use turn and record only local request bodies."""

        def log_message(self, _format: str, *_args: Any) -> None:
            """Suppress loopback request logs during the focused integration gate."""

        def do_POST(self) -> None:
            """Return tool use first, then a final text response after the tool result."""
            length = int(self.headers["content-length"])
            requests.append(json.loads(self.rfile.read(length)))
            request_text = json.dumps(requests[-1], separators=(",", ":"))
            if "Write the title in the predominant language" in request_text:
                response = _anthropic_sse(
                    [
                        {
                            "type": "message_start",
                            "message": {
                                "id": "msg_loopback_title",
                                "type": "message",
                                "role": "assistant",
                                "content": [],
                                "model": "claude-sonnet-5",
                                "stop_reason": None,
                                "stop_sequence": None,
                                "usage": {"input_tokens": 1, "output_tokens": 1},
                            },
                        },
                        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Test"}},
                        {"type": "content_block_stop", "index": 0},
                        {
                            "type": "message_delta",
                            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                            "usage": {"output_tokens": 1},
                        },
                        {"type": "message_stop"},
                    ]
                )
            elif "tool_result" not in request_text:
                tool_events: list[dict[str, Any]] = []
                for index, (tool_id, tool_name, tool_input) in enumerate(tool_uses):
                    tool_events.extend(
                        [
                            {
                                "type": "content_block_start",
                                "index": index,
                                "content_block": {"type": "tool_use", "id": tool_id, "name": tool_name, "input": {}},
                            },
                            {
                                "type": "content_block_delta",
                                "index": index,
                                "delta": {"type": "input_json_delta", "partial_json": json.dumps(tool_input)},
                            },
                            {"type": "content_block_stop", "index": index},
                        ]
                    )
                response = _anthropic_sse(
                    [
                        {
                            "type": "message_start",
                            "message": {
                                "id": "msg_loopback_1",
                                "type": "message",
                                "role": "assistant",
                                "content": [],
                                "model": "claude-sonnet-5",
                                "stop_reason": None,
                                "stop_sequence": None,
                                "usage": {"input_tokens": 1, "output_tokens": 1},
                            },
                        },
                        *tool_events,
                        {
                            "type": "message_delta",
                            "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                            "usage": {"output_tokens": 1},
                        },
                        {"type": "message_stop"},
                    ]
                )
            else:
                response = _anthropic_sse(
                    [
                        {
                            "type": "message_start",
                            "message": {
                                "id": "msg_loopback_2",
                                "type": "message",
                                "role": "assistant",
                                "content": [],
                                "model": "claude-sonnet-5",
                                "stop_reason": None,
                                "stop_sequence": None,
                                "usage": {"input_tokens": 1, "output_tokens": 1},
                            },
                        },
                        {
                            "type": "content_block_start",
                            "index": 0,
                            "content_block": {"type": "text", "text": ""},
                        },
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": "sandbox checked"},
                        },
                        {"type": "content_block_stop", "index": 0},
                        {
                            "type": "message_delta",
                            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                            "usage": {"output_tokens": 1},
                        },
                        {"type": "message_stop"},
                    ]
                )
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _LoopbackAnthropicHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-loopback-test-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv(
        "BENCHMARK_EVIDENCE_ROOTS", json.dumps([str(evaluator_root), str(history_root), str(sibling_root)])
    )
    try:
        runner = script_run_agentic.ModelRunner("sonnet", script_run_agentic.MODELS["sonnet"], worktree, timeout=30)
        events, _elapsed_s, error = runner.run_stage_events(
            prompt="Use the Bash tool when instructed.",
            system_prompt="Follow the model response.",
            arm="A_plain",
            cwd=worktree,
        )
        baseline_requests = list(requests)
        requests.clear()
        tool_uses[:] = [("toolu_runtime_query", "Bash", {"command": "codemap-py query --compact symbol allowed"})]
        graph_task = script_run_agentic.Task(
            id="native-graph-runtime",
            type="blast_radius_analysis",
            prompt="Run the staged compact Codemap query.",
        )
        runtime_result = runner.run(graph_task, "C_strict")
        runtime_events = runtime_result.raw_events
        runtime_requests = list(requests)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert error is None
    assert runtime_result.error == ""
    tool_result_request = next(
        (request for request in baseline_requests if "tool_result" in json.dumps(request, separators=(",", ":"))),
        None,
    )
    assert tool_result_request is not None, json.dumps([request.get("messages") for request in baseline_requests])
    serialized_tool_result = json.dumps(tool_result_request)
    for denied_marker in _DENIED_MARKERS:
        assert denied_marker not in serialized_tool_result
    assert "SOURCE-ALLOWED" in serialized_tool_result
    for denied_tool_id in _DENIED_TOOL_IDS:
        tool_result = next(
            content
            for message in tool_result_request["messages"]
            for content in message["content"]
            if isinstance(content, Mapping)
            and content.get("type") == "tool_result"
            and content.get("tool_use_id") == denied_tool_id
        )
        serialized_denial = json.dumps(tool_result)
        assert tool_result["is_error"] is True
        assert any(
            phrase in serialized_denial
            for phrase in ("Permission denied", "Operation not permitted", "denied by your permission settings")
        )
    assert any(event.get("type") == "result" and event.get("subtype") == "success" for event in events)
    runtime_request = next(
        request for request in runtime_requests if "toolu_runtime_query" in json.dumps(request, separators=(",", ":"))
    )
    assert "codemap-py query --compact symbol allowed" in json.dumps(runtime_request)
    runtime_tool_result_request = next(
        request for request in runtime_requests if "tool_result" in json.dumps(request, separators=(",", ":"))
    )
    runtime_tool_result = next(
        content
        for message in runtime_tool_result_request["messages"]
        for content in message["content"]
        if isinstance(content, Mapping)
        and content.get("type") == "tool_result"
        and content.get("tool_use_id") == "toolu_runtime_query"
    )
    assert runtime_tool_result.get("is_error") is not True
    assert "<tool_use_error>" not in json.dumps(runtime_tool_result)
    assert any(event.get("type") == "result" and event.get("subtype") == "success" for event in runtime_events)
    runtime_evidence = script_run_agentic._claude_codemap_evidence(runtime_events)
    assert runtime_evidence["codemap_compact_success"] is True, json.dumps(
        {"evidence": runtime_evidence, "tool_result": runtime_tool_result}, default=str
    )
    assert runtime_result.codemap_compact_success is True
    assert len(runtime_result.index_relocations) == 1
    assert runtime_result.index_relocations[0]["source_scan_root"] == str(worktree.resolve())
    assert json.loads(runtime_tool_result["content"])["index"]["root_mismatch"] is False


# ===========================================================================
# BA query arms run in an isolated copy, not the real repo
# ===========================================================================


class TestQueryArmIsolation:
    """Non-reset (query) codemap runs execute in a throwaway copy so they cannot mutate the repo."""

    def test_ba_codemap_run_uses_a_copy_and_cannot_mutate_repo(
        self, script_run_agentic: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A BA codemap run's cwd is a copy of the repo, and edits there never touch self.repo_path.

        Scenario: query arms used to run in-place with Edit/Bash unblocked; the cwd handed to
        the subprocess must be a distinct copy, and a write into it must not appear in the real repo.
        """
        repo = tmp_path / "myrepo"
        repo.mkdir()
        (repo / "mod.py").write_text("x = 1\n")
        runner = script_run_agentic.ModelRunner("haiku", script_run_agentic.MODELS["haiku"], repo, timeout=300)
        task = script_run_agentic.Task(id="BA-01", type="blast_radius_analysis", prompt="p", requires_reset=False)
        seen: dict[str, Any] = {}

        def _fake_stream(
            self: Any, cmd: Any, result: Any, update_fn: Any = None, cwd: Any = None, arm: Any = None
        ) -> None:
            """Write a stray sandbox edit and populate synthetic token usage."""
            seen["cwd"] = cwd
            (cwd / "MUTATED.txt").write_text("agent wrote this")  # simulate a stray edit in the sandbox
            result.input_tokens = 10
            result.output_tokens = 10

        monkeypatch.setattr(script_run_agentic.ModelRunner, "_stream_events", _fake_stream)

        runner.run(task, "codemap")

        assert seen["cwd"] != repo
        assert seen["cwd"].name == repo.name
        assert not (repo / "MUTATED.txt").exists()  # the stray edit stayed in the throwaway copy


# ===========================================================================
# report rendering — canonical parity arms
# ===========================================================================


def _ba_run(mod: Any, arm: str = "A_plain", **fields: Any) -> Any:
    """Build a successful haiku ``BenchmarkRun`` for BA-01; ``fields`` override or extend the defaults."""
    defaults = {"task_id": "BA-01", "task_type": "blast_radius_analysis", "model": "haiku", "success": True}
    return mod.BenchmarkRun(arm=arm, **{**defaults, **fields})


def _admitted_run(mod: Any, arm: str = "A_plain", **fields: Any) -> Any:
    """Build a BA-01 ``BenchmarkRun`` admitted for canonical pooling; ``fields`` override the defaults."""
    admitted = {
        "parity_arm": arm,
        "answer_contract_valid": True,
        "answer_pooling_eligible": True,
        "treatment_adherence": True,
    }
    return _ba_run(mod, arm, **{**admitted, **fields})


@pytest.fixture(name="ba_prompt_task")
def _ba_prompt_task(script_run_agentic: Any) -> Any:
    """Blast-radius ``Task`` BA-01 with a placeholder prompt."""
    return script_run_agentic.Task(id="BA-01", type="blast_radius_analysis", prompt="p")


class TestParityArmReport:
    """The markdown report must pair the canonical parity arms, not only the legacy plain/codemap names."""

    def test_savings_summary_pairs_each_treatment_with_the_parity_control(
        self, script_run_agentic: Any, ba_prompt_task: Any
    ) -> None:
        """A_plain is recognised as the control, so both treatment arms report savings.

        Scenario: the renderer assumed a ``plain`` baseline, so a parity snapshot produced only the
        "no completed baseline + injected arm pairs" placeholder for every model.
        """
        mod = script_run_agentic
        results = [
            _ba_run(mod, arm, elapsed_s=elapsed, input_tokens=tokens)
            for arm, elapsed, tokens in (
                ("A_plain", 120.0, 400_000),
                ("B_auto", 60.0, 200_000),
                ("C_strict", 30.0, 100_000),
            )
        ]

        markdown = mod.Report(results, [ba_prompt_task], {"date": "2026-09-06"}).render()

        assert mod.Report._NO_PAIRS_MD not in markdown
        assert "| C_strict | Elapsed (s)" in markdown
        assert "| B_auto   | Elapsed (s)" in markdown

    def test_baseline_arm_falls_back_to_the_legacy_name(self, script_run_agentic: Any, ba_prompt_task: Any) -> None:
        """Legacy snapshots keep rendering against their own ``plain`` control.

        Scenario: a pre-parity snapshot only has ``plain`` and ``codemap`` rows, so the baseline lookup must
        fall back to the legacy control name rather than finding no baseline.
        """
        mod = script_run_agentic
        results = [_ba_run(mod, "plain", elapsed_s=120.0), _ba_run(mod, "codemap", elapsed_s=60.0)]

        assert mod.Report(results, [ba_prompt_task], {})._baseline_arm() == "plain"


class TestCanonicalPassReporting:
    """Prospective Claude A/B/C rows must retain shared pass-reporting inputs."""

    @pytest.mark.parametrize(
        ("success", "incomplete", "correct", "quality_score", "graded_score", "marker", "quality_text"),
        [
            pytest.param(False, True, True, 1.0, 0.75, "!", "0.0%", id="execution-incomplete"),
            pytest.param(True, False, True, 1.0, 1.0, "✓", "100.0%", id="exact-pass"),
            pytest.param(True, False, False, 0.0, 56 / 57, "✗", "98.2%", id="completed-nearcorrect"),
        ],
    )
    def test_canonical_progress_uses_admitted_grade_and_exact_marker(
        self,
        script_run_agentic: Any,
        ba_prompt_task: Any,
        success: bool,
        incomplete: bool,
        correct: bool,
        quality_score: float,
        graded_score: float,
        marker: str,
        quality_text: str,
    ) -> None:
        """Canonical progress shows shared admitted quality and the execution/exact status marker.

        Scenario: an incomplete execution, an exact pass, and a completed near-correct answer each render the
        admitted graded quality plus a distinct marker, and never the legacy evidence-recall suffix.
        """
        result = _admitted_run(
            script_run_agentic,
            success=success,
            incomplete=incomplete,
            answer_correct=correct,
            answer_quality_score=quality_score,
            answer_components={"production_importers": quality_score},
            answer_graded_score=graded_score,
            answer_graded_components={"production_importers": graded_score},
        )

        line = script_run_agentic._run_line(1, 1, ba_prompt_task, "haiku", "A_plain", result)

        assert f"quality={quality_text}" in line
        assert f" exact={marker}" in line
        assert "erec=" not in line

    def test_legacy_progress_retains_evidence_recall(self, script_run_agentic: Any, ba_prompt_task: Any) -> None:
        """Legacy progress retains its existing evidence-recall quality suffix.

        Scenario: a legacy ``plain`` row carries only evidence recall, so its progress line keeps the erec/rrec
        suffix and gains no canonical ``exact=`` marker.
        """
        quality = script_run_agentic.QualityScore(scored=True, erec=0.5, rrec=0.25)
        result = _ba_run(script_run_agentic, "plain", quality=quality)

        line = script_run_agentic._run_line(1, 1, ba_prompt_task, "haiku", "plain", result)

        assert "erec= 50% rrec= 25%" in line
        assert " exact=" not in line

    def test_normalized_row_keeps_unavailable_native_usage_as_none(self, script_run_agentic: Any) -> None:
        """A failed stream without terminal usage is not a measured zero-token cell.

        Scenario: a timed-out C cell has no native usage, so its canonical row must report every token and
        elapsed field as None (unobserved) and an empty quality block rather than zeros.
        """
        result = _admitted_run(
            script_run_agentic,
            "C_strict",
            success=False,
            repetition=2,
            answer_contract_valid=False,
            answer_pooling_eligible=False,
            treatment_adherence=False,
            incomplete=True,
            error_type="error_timeout",
        )

        row = script_run_agentic._canonical_agentic_row(result)

        assert row["repetition"] == 2
        assert row["quality"] == {
            "correct": None,
            "quality_score": None,
            "components": {},
            "graded_score": None,
            "graded_components": {},
        }
        assert row["input_tokens"] is None
        assert row["cached_input_tokens"] is None
        assert row["fresh_input_tokens"] is None
        assert row["output_tokens"] is None
        assert row["elapsed_s"] is None

    def test_normalized_row_keeps_graded_semantic_quality_diagnostic(self, script_run_agentic: Any) -> None:
        """Canonical rows preserve additive graded quality without changing exact quality fields.

        Scenario: an A cell that is exactly wrong (0.0) but half right on the graded scale must expose both
        numbers side by side, with the exact fields unchanged by the graded diagnostic.
        """
        result = _admitted_run(
            script_run_agentic,
            answer_quality_score=0.0,
            answer_correct=False,
            answer_components={"production_importers": 0.0},
            answer_graded_score=0.5,
            answer_graded_components={"production_importers": 0.5},
            usage_complete=True,
        )

        row = script_run_agentic._canonical_agentic_row(result)

        assert row["quality"] == {
            "correct": False,
            "quality_score": 0.0,
            "components": {"production_importers": 0.0},
            "graded_score": 0.5,
            "graded_components": {"production_importers": 0.5},
        }

    def test_rolling_canonical_metadata_keeps_missing_cells_in_each_model_scope(
        self, script_run_agentic: Any, ba_prompt_task: Any, make_benchmark: Any
    ) -> None:
        """One completed A cell leaves B and C as unobserved, not omitted, in its model headline.

        Scenario: a rolling run has finished only the A_plain cell of a three-arm assignment; the metadata
        update must keep the unfinished arms in the denominator as unobserved cells.
        """
        benchmark = make_benchmark(ba_prompt_task, list(script_run_agentic.AGENTIC_ARMS))
        benchmark.results.append(
            _admitted_run(
                script_run_agentic,
                answer_quality_score=1.0,
                answer_correct=True,
                answer_graded_score=1.0,
                answer_graded_components={"production_importers": 1.0},
                usage_complete=True,
            )
        )

        metadata: dict[str, Any] = {}
        benchmark._update_canonical_reporting(metadata)

        summary = metadata["agentic_reporting"]["summaries_by_model"]["haiku"]
        assert summary["all_assigned"]["A_plain"]["passed_cells"] == 1
        assert summary["all_assigned"]["B_auto"]["unobserved_cells"] == 1
        assert summary["all_assigned"]["C_strict"]["unobserved_cells"] == 1

    @mock.patch("_bench_claude.agentic.runner.ModelRunner.run", autospec=True, side_effect=KeyboardInterrupt)
    def test_first_cell_interruption_persists_all_assigned_zero_observed_summary(
        self, mock_run: mock.Mock, script_run_agentic: Any, ba_prompt_task: Any, make_benchmark: Any
    ) -> None:
        """An interruption before the first native row still leaves a canonical denominator snapshot.

        Scenario: the first ``ModelRunner.run`` raises KeyboardInterrupt; the snapshot on disk must list no
        results yet count every assigned arm as one unobserved, failed cell.
        """
        benchmark = make_benchmark(ba_prompt_task, list(script_run_agentic.AGENTIC_ARMS))

        with pytest.raises(KeyboardInterrupt):
            benchmark.run({})

        snapshot = json.loads(benchmark.output_path.read_text(encoding="utf-8"))
        cells = snapshot["metadata"]["agentic_reporting"]["summaries_by_model"]["haiku"]["all_assigned"]
        assert snapshot["results"] == []
        assert {
            arm: (cells[arm]["observed_cells"], cells[arm]["unobserved_cells"], cells[arm]["failed_cells"])
            for arm in script_run_agentic.AGENTIC_ARMS
        } == dict.fromkeys(script_run_agentic.AGENTIC_ARMS, (0, 1, 1))

    @mock.patch("_bench_claude.agentic.runner.ModelRunner.run", autospec=True, side_effect=KeyboardInterrupt)
    def test_legacy_first_cell_interruption_does_not_create_a_new_snapshot(
        self, mock_run: mock.Mock, ba_prompt_task: Any, make_benchmark: Any
    ) -> None:
        """Legacy execution keeps its historical no-snapshot-before-first-result behavior.

        Scenario: the same first-cell interrupt on a legacy ``plain`` run must leave no output file and no
        canonical reporting metadata behind.
        """
        benchmark = make_benchmark(ba_prompt_task, ["plain"])
        metadata: dict[str, Any] = {}

        with pytest.raises(KeyboardInterrupt):
            benchmark.run(metadata)

        assert not benchmark.output_path.exists()
        assert "agentic_reporting" not in metadata


# ===========================================================================
# Relocated-index admission
# ===========================================================================


@pytest.fixture(name="relocated_worktree", scope="module")
def _relocated_worktree(tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    """Build a clean committed worktree holding a root-relocated copy of a frozen index.

    The real git repository is the contract of the admission check, so it stays real; it is built once because
    admission only reads it. Tests that need a mutated ``relocation`` mapping must copy it first.

    Returns:
        A namespace with the worktree root ``repo``, the relocated ``index_path``, a ``manifest_path`` locking
        the frozen index bytes, and the ``relocation`` provenance a run in that worktree would carry.
    """
    tmp_path = tmp_path_factory.mktemp("relocated-index")
    source = tmp_path / "managed-clone"
    source.mkdir()
    repo = tmp_path / "worktree"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "a.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
    run_git(repo, "init", "-q")
    run_git(repo, "add", "-A")
    run_git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    commit = run_git(repo, "rev-parse", "HEAD")
    tree = run_git(repo, "rev-parse", "HEAD^{tree}")
    frozen_payload = {
        "scan_root": str(source.resolve()),
        "git_sha": commit,
        "scan_version": 3,
        "modules": [{"name": "pkg.a"}, {"name": "pkg.b"}],
    }
    frozen_bytes = (json.dumps(frozen_payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    derived_bytes, relocation = relocate_frozen_index_for_worktree(frozen_bytes, source_root=source, worktree_root=repo)
    index_path = tmp_path / "relocated-index.json"
    index_path.write_bytes(derived_bytes)
    manifest_path = tmp_path / "relocated-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "target_source": {"commit": commit, "tree": tree},
                "index": {
                    "raw_sha256": hashlib.sha256(frozen_bytes).hexdigest(),
                    "git_sha": commit,
                    "scan_version": 3,
                    "module_count": 2,
                },
            }
        ),
        encoding="utf-8",
    )
    return SimpleNamespace(repo=repo, index_path=index_path, manifest_path=manifest_path, relocation=dict(relocation))


class TestRelocatedIndexAdmission:
    """A worktree run proves index identity through relocation provenance, never byte identity."""

    def test_valid_provenance_admits_a_worktree_index(
        self, script_run_agentic: Any, relocated_worktree: SimpleNamespace
    ) -> None:
        """Relocation provenance admits an index whose bytes differ from the locked hash.

        Scenario: a run executes in its own worktree, so its index carries the worktree's
        ``scan_root`` and can never reproduce the manifest's locked raw hash; the provenance written
        by the relocation is what proves the graph is still the frozen one.
        """
        w = relocated_worktree

        script_run_agentic._validate_parity_runtime(w.repo, w.index_path, w.manifest_path, w.relocation)

        assert hashlib.sha256(w.index_path.read_bytes()).hexdigest() != w.relocation["frozen_index_sha256"]

    @pytest.mark.parametrize(
        ("field", "digest", "message"),
        [
            pytest.param("frozen_index_sha256", "0" * 64, "wrong frozen source", id="wrong-frozen-source"),
            pytest.param("derived_index_sha256", "1" * 64, "changed after relocation", id="bytes-on-disk-disagree"),
        ],
    )
    def test_inconsistent_provenance_is_rejected(
        self, script_run_agentic: Any, relocated_worktree: SimpleNamespace, field: str, digest: str, message: str
    ) -> None:
        """Provenance whose frozen source is not the locked index, or whose derived hash misses the disk, is rejected.

        Scenarios: a caller supplies provenance derived from some other frozen index, so admitting it would
        let an unrelated graph enter the run under the locked manifest's authority; or the relocated copy
        changed after its provenance was written, so the digest the run would attest to is no longer the index
        the model actually reads.
        """
        w = relocated_worktree
        relocation = {**w.relocation, field: digest}

        with pytest.raises(ValueError, match=message):
            script_run_agentic._validate_parity_runtime(w.repo, w.index_path, w.manifest_path, relocation)

    def test_absent_provenance_keeps_the_byte_gate(
        self, script_run_agentic: Any, relocated_worktree: SimpleNamespace
    ) -> None:
        """Without provenance a non-locked index is still rejected on its bytes.

        Scenario: the same relocated index is offered by a run that claims no relocation; the
        original byte-identity gate must reject it exactly as before.
        """
        w = relocated_worktree

        with pytest.raises(ValueError, match="canonical run requires the locked index bytes"):
            script_run_agentic._validate_parity_runtime(w.repo, w.index_path, w.manifest_path)


# ===========================================================================
# Shared terminal presentation
# ===========================================================================


class TestSharedPresentation:
    """The runner renders through the shared benchmark console instead of its own."""

    def test_console_is_fixed_at_the_shared_benchmark_width(self, script_run_agentic: Any) -> None:
        """Framed output takes the shared benchmark width rather than the window's own width.

        Scenario: this runner built a bare ``rich.Console``, so a panel it printed stretched to
        whatever the terminal happened to be while the paid-command block beside it stayed at 78
        columns. Both now come from the one console every benchmark surface shares.
        """
        assert script_run_agentic._console.width == BENCHMARK_OUTPUT_WIDTH

    def test_stage_preflight_announces_itself_with_the_shared_section_rule(
        self, script_run_agentic: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A redirected stage preflight prints its banner as a plain titled rule.

        Scenario: an operator pipes a no-model preflight into an archived log. The banner was a
        bare upper-case line that no other run phase matched; its redirected form is now the same
        ``== title ==`` rule every phase uses, while the PLAN and SCOPE lines under it, which the
        launcher greps and paid admission depends on, stay byte-identical.
        """
        script_run_agentic.main(study="fix-single", tasks=["FS-01"], dry_run=True)

        lines = capsys.readouterr().out.splitlines()
        assert lines[0] == "== FIX-SINGLE PREFLIGHT (no model) =="
        assert lines[1] == "PLAN    FS-01  rep=1  A_plain"
        assert lines[-1].startswith("SCOPE   ")
