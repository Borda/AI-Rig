"""Tests for benchmarks/run-claude-structural.py.

Public API surface covered, grouped by the class that owns each unit:
  - Runner wiring: arm orders, run-loop rendering, provenance stamping, treatment adherence, the ``main`` entry point
    and the shared evaluator registry (TestArmOrders .. TestSharedEvaluator)
  - BenchQuality / BenchRun dataclass defaults and per-instance isolation
  - Cost accounting and parity-arm summaries (_token_ratio_table, _arm_pairs, _print_summary)
  - DiffImpactStager revert guarantees and PatchSandbox exit-code, apply and interpreter-pinning behaviour
  - Helpers: _extract_int, _int_close, _count_tol_detail, _safe_ratio, _parse_scan_query_subcommand,
    _normalize_external_task, _load_tasks_file, _extract_diff, _workflow_type_of, _effective_recall,
    _paired_accuracy, _arm_extracted, _max_turns_for_task, _module_compatible, _build_system_prompt
  - Evaluators: _evaluate_symbol, _evaluate_rv, _evaluate_oss, _evaluate_debug, _evaluate_feature,
    _evaluate_real_issue, _evaluate_develop_br, _evaluate_diff_impact, plus the answer-region and stem helpers
  - Contamination detection and relocated-index admission (_validate_primary_runtime)

Tests never invoke the claude CLI and write only under ``tmp_path``. Real ``git`` runs only where its behaviour is the
contract: the DiffImpactStager revert guarantees and the relocated worktree index.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

BENCHMARKS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BENCHMARKS))

# Patch seams live in the package modules the runner shim re-exports from: patching the shim
# would leave each package module's own global binding untouched.
from _bench_claude.structural import cli as bench_cli  # noqa: E402
from _bench_claude.structural import config as bench_config  # noqa: E402
from _bench_claude.structural import report as bench_report  # noqa: E402
from _bench_claude.structural import runner as bench_runner  # noqa: E402
from _bench_claude.structural import tasks as bench_tasks  # noqa: E402
from _bench_common.mutation_isolation import relocate_frozen_index_for_worktree  # noqa: E402
from _bench_common.presentation import BENCHMARK_OUTPUT_WIDTH, LEGEND_CLOSE_RULE, LEGEND_OPEN_RULE  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers shared across test classes
# ---------------------------------------------------------------------------

_FOO_SOURCE = "def foo():\n    return 1\n"
_PATCH = "diff --git a/a.py b/a.py\n"
_FIXTURE_TASK = {"id": "fixture", "prompt": "answer", "type": "fixture", "scoreable": False}


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


def _patch_task(**fields: Any) -> dict[str, Any]:
    """Build a patch-task record pinned to a dummy commit, with ``fields`` naming how its tests run."""
    return {"id": "PT-fixture", "pre_fix_commit": "a" * 40, **fields}


def _execute_stub(script_bench: Any, arm: str, task: dict[str, Any]) -> Callable[..., Any]:
    """Build an ``_execute`` replacement that returns a successful run for ``arm`` and ``task``."""
    return lambda *_args, **_kwargs: script_bench.BenchRun(
        arm=arm, task_id=task["id"], task_type=task["type"], model="haiku", success=True
    )


def _stream_stub(**fields: Any) -> Callable[..., None]:
    """Build a ``_stream`` replacement that records a successful run with tokens and sets ``fields`` on it."""

    def _stream(_cmd: list[str], result: Any, _arm: str, update_fn: Any = None) -> None:
        """Populate a successful streamed result with the scenario's tool evidence."""
        result.success = True
        result.input_tokens = 1
        for name, value in fields.items():
            setattr(result, name, value)

    return _stream


class _StopRun(RuntimeError):
    """Raised by a test double to halt ``main`` once the behaviour under test has been observed."""


class _StubRunner:
    """Runner double that builds each result from ``make_result`` instead of invoking a provider."""

    def __init__(self, make_result: Callable[[dict[str, Any], str], Any]) -> None:
        self._make_result = make_result

    def run(self, task: dict[str, Any], arm: str, update_fn: Any = None) -> Any:
        """Return the scenario-specific benchmark result without invoking a provider."""
        return self._make_result(task, arm)


class _NullProgress:
    """The slice of the Rich progress API the run loop touches, rendering nothing."""

    console = SimpleNamespace(print=lambda *_args, **_kwargs: None)

    def add_task(self, *_args: Any, **_kwargs: Any) -> int:
        """Return a stable progress-task identifier without creating terminal output."""
        return 1

    def update(self, *_args: Any, **_kwargs: Any) -> None:
        """Accept progress updates without rendering a terminal display."""

    def remove_task(self, *_args: Any, **_kwargs: Any) -> None:
        """Accept task removal without maintaining a live progress display."""

    def advance(self, *_args: Any, **_kwargs: Any) -> None:
        """Accept task advancement without rendering a terminal display."""


def _make_loop(
    script_bench: Any, tmp_path: Path, make_result: Callable[[dict[str, Any], str], Any], arm_orders: dict[str, Any]
) -> Any:
    """Build the structural run loop over a stub runner producing ``make_result(task, arm)``."""
    return script_bench._StructuralRunLoop(
        runner=_StubRunner(make_result), repo_path=tmp_path, arm_orders=arm_orders, patch_ids=set()
    )


def _no_override(_command: list[str]) -> None:
    """Leave a subprocess command to the default fake handling."""
    return None


def _fake_worktree_boundary(
    script_bench: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pytest_codes: list[int],
    on_command: Callable[[list[str]], Any] = _no_override,
) -> SimpleNamespace:
    """Replace ``tempfile.mkdtemp`` and ``subprocess.run`` with an in-memory git worktree and pytest fake.

    ``on_command`` may answer any command first (a non-``None`` result wins); otherwise worktree add/remove are
    simulated on disk and each pytest invocation exits with the next of ``pytest_codes``. Returns the recorded
    ``commands`` and per-attempt ``roots``.
    """
    codes = iter(pytest_codes)
    roots: list[Path] = []
    commands: list[list[str]] = []

    def _mkdtemp(*_args: Any, **_kwargs: Any) -> str:
        """Create a fixture-owned attempt directory for worktree lifecycle assertions."""
        root = tmp_path / f"attempt-{len(roots)}"
        root.mkdir()
        roots.append(root)
        return str(root)

    def _run(command: list[str], **_kwargs: Any) -> SimpleNamespace:
        """Supply scenario-specific subprocess outcomes at the worktree and test-command boundary."""
        commands.append(command)
        if (override := on_command(command)) is not None:
            return override
        if "worktree" in command and "add" in command:
            Path(command[-2]).mkdir()
        elif _is_pytest_argv(command):
            return SimpleNamespace(returncode=next(codes), stderr="boom", stdout="")
        elif "worktree" in command and "remove" in command:
            worktree = Path(command[-1])
            for path in worktree.iterdir():
                path.unlink()
            worktree.rmdir()
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(script_bench.tempfile, "mkdtemp", _mkdtemp)
    monkeypatch.setattr(script_bench.subprocess, "run", _run)
    return SimpleNamespace(commands=commands, roots=roots)


def _init_commit(repo: Path) -> None:
    """Initialise ``repo`` as a git repository with everything in it committed."""
    for args in (
        ["init", "-q"],
        ["add", "-A"],
        ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def empty_index(tmp_path: Path) -> Path:
    """Create an empty JSON index file standing in for the Codemap index."""
    path = tmp_path / "index.json"
    path.write_text("{}", encoding="utf-8")
    return path


@pytest.fixture
def make_runner(script_run_bench: Any, tmp_path: Path) -> Callable[..., Any]:
    """Return a factory for a ``BenchRunner`` on dummy paths; keyword arguments reach the constructor."""

    def _make(**kwargs: Any) -> Any:
        """Build the runner with the fixed model names and paths shared by these tests."""
        return script_run_bench.BenchRunner("haiku", "fixture-model", tmp_path, tmp_path / "index.json", **kwargs)

    return _make


@pytest.fixture
def unrelated_sleeper() -> Iterator[threading.Thread]:
    """Run a thread that keeps calling ``time.sleep`` for the duration of a test, then stop and join it.

    A leaked polling thread in a shared xdist worker does the same in CI. A test that patches the global ``time.sleep``
    records this thread's calls too.
    """
    stop = threading.Event()
    slept = threading.Event()

    def _background_sleep() -> None:
        """Call the shared ``time.sleep`` until the fixture releases the thread."""
        while not stop.is_set():
            time.sleep(0.001)
            slept.set()
            stop.wait(0.001)

    helper = threading.Thread(target=_background_sleep, name="unrelated-sleeper", daemon=True)
    helper.start()
    assert slept.wait(5), "helper never completed a sleep"
    yield helper
    stop.set()
    helper.join(timeout=5)
    assert not helper.is_alive()


@pytest.fixture
def fn02_contract(script_run_bench: Any) -> SimpleNamespace:
    """FN-02 from the locked primary suite, with the policies and suite hash a canonical runner needs."""
    tasks, policies = script_run_bench._load_primary_parity_contract()
    task = next(task for task in tasks if task["id"] == "FN-02")
    runner_kwargs = {"task_policies": policies, "suite_hash": script_run_bench.PRIMARY_SUITE_HASH}
    return SimpleNamespace(task=task, policies=policies, runner_kwargs=runner_kwargs)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Create a committed git repository holding ``pkg/a.py`` and ``pkg/b.py``."""
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "a.py").write_text(_FOO_SOURCE)
    (repo / "pkg" / "b.py").write_text("def bar():\n    return 2\n")
    _init_commit(repo)
    return repo


@pytest.fixture
def refuse_checkout(script_run_bench: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every ``git checkout`` fail while all other subprocess calls run for real."""
    real_run = subprocess.run

    def _run(command: list[str], **kwargs: Any) -> Any:
        """Refuse git checkout and pass every other command through to the real subprocess."""
        if "checkout" in command:
            return SimpleNamespace(returncode=1, stderr="checkout refused", stdout="")
        return real_run(command, **kwargs)

    monkeypatch.setattr(script_run_bench.subprocess, "run", _run)


@pytest.fixture(scope="module")
def _relocated_template(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, dict[str, Any], dict[str, str]]:
    """Build one committed worktree holding a root-relocated copy of a frozen index, shared read-only.

    Returns:
        The worktree root, the relocated index path, a parity manifest locking the frozen index bytes, and the
        relocation provenance a run in that worktree would carry.
    """
    tmp_path = tmp_path_factory.mktemp("relocated-index")
    source = tmp_path / "managed-clone"
    source.mkdir()
    repo = tmp_path / "worktree"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "a.py").write_text(_FOO_SOURCE, encoding="utf-8")
    _init_commit(repo)
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


@pytest.fixture
def relocated_index(
    _relocated_template: tuple[Path, Path, dict[str, Any], dict[str, str]],
) -> tuple[Path, Path, dict[str, Any], dict[str, str]]:
    """Return the shared relocated worktree index with a private copy of the manifest and provenance."""
    repo, index_path, manifest, relocation = _relocated_template
    return repo, index_path, copy.deepcopy(manifest), dict(relocation)


class TestArmOrders:
    """Per-task arm order planning and the legacy versus parity arm label sets."""

    def test_orders_use_the_shared_coordinate_policy(
        self, script_run_bench: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each task delegates its canonical arm order to the shared Claude coordinate policy.

        Scenario: two tasks are planned for one model; each order must come from the shared policy keyed by
        revision, provider, model, task and repetition, so no local ordering rule can drift from the shared one.
        """
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

        monkeypatch.setattr(bench_config, "deterministic_arm_order", _order)

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

    def test_legacy_arms_keep_their_labels_and_have_no_parity_identity(self, script_run_bench: Any) -> None:
        """Existing transport labels stay available but cannot inherit A/B/C identity.

        Scenario: a pre-parity result file names only ``plain`` and ``codemap``; planning for those arms must keep
        the declared order and never map them onto the canonical A/B/C labels.
        """
        assert script_run_bench.ARMS == ("plain", "codemap")
        assert script_run_bench.PARITY_ARMS == ("A_plain", "B_auto", "C_strict")
        assert script_run_bench.PARITY_ARM_BY_LEGACY_ARM == {}
        assert script_run_bench._arm_orders_by_task(
            [{"id": "FN-02"}],
            script_run_bench.ARMS,
            model="haiku",
            provider_parity=False,
        ) == {"FN-02": script_run_bench.ARMS}


class TestRunLoop:
    """The structural run loop schedules arms per task and renders one aligned row per result."""

    def test_executes_each_task_in_its_arm_order(self, script_run_bench: Any, tmp_path: Path) -> None:
        """Paid execution consumes the same per-task arm order exposed by the plan.

        Scenario: the plan fixes ``C_strict, A_plain, B_auto`` for FN-02; the loop must schedule the cells in that
        order without starting a provider.
        """
        calls: list[tuple[str, str]] = []

        def _result(task: dict[str, Any], arm: str) -> Any:
            """Record each scheduled cell and return a synthetic run."""
            calls.append((task["id"], arm))
            return _make_run(script_run_bench, task_id=task["id"], arm=arm)

        loop = _make_loop(script_run_bench, tmp_path, _result, {"FN-02": ("C_strict", "A_plain", "B_auto")})

        loop.run_task_arms({"id": "FN-02", "type": "fixture", "scoreable": False}, _NullProgress(), 0)

        assert calls == [("FN-02", "C_strict"), ("FN-02", "A_plain"), ("FN-02", "B_auto")]

    def test_aligns_result_metrics_after_the_longest_arm(self, script_run_bench: Any, tmp_path: Path) -> None:
        """Every result row reserves the longest arm label plus one separating space.

        Scenario: a short arm label and a long one print rows with different elapsed times; their metric columns
        must still start at the same offset so rows stay comparable.
        """
        lines: list[str] = []
        loop = _make_loop(
            script_run_bench,
            tmp_path,
            lambda task, arm: _make_run(
                script_run_bench, task_id=task["id"], arm=arm, elapsed_s=8.0 if arm == "plain" else 95.0
            ),
            {"SE-01": ("plain", "C_strict")},
        )
        task = {"id": "SE-01", "type": "fixture", "scoreable": False}

        loop.run_combo(task, "plain", lines.append)
        loop.run_combo(task, "C_strict", lines.append)

        expected_width = max(map(len, (*script_run_bench.ARMS, *script_run_bench.PARITY_ARMS))) + 1
        assert lines[0].split("\t", 1)[0] == f"  ✓? SE-01 {'plain':<{expected_width}}"
        assert lines[1].split("\t", 1)[0] == f"  ✓? SE-01 {'C_strict':<{expected_width}}"
        assert lines[0].index("\ttok:") == lines[1].index("\ttok:")
        assert lines[0].index("recall=") == lines[1].index("recall=")

    def test_labels_and_aligns_unscored_recall(self, script_run_bench: Any, tmp_path: Path) -> None:
        """Unscored recall is explicit and does not shift the following total column.

        Scenario: one arm has no recall because its task is unscored while the other has a real recall value; the
        unscored row says so and the ``total=`` column still lines up.
        """
        lines: list[str] = []

        def _result(task: dict[str, Any], arm: str) -> Any:
            """Return one unscored and one scored result for output comparison."""
            run = _make_run(script_run_bench, task_id=task["id"], arm=arm)
            if arm == "C_strict":
                run.quality = script_run_bench.BenchQuality(scored=True, correct=True, recall=0.857)
            return run

        loop = _make_loop(script_run_bench, tmp_path, _result, {"CQ-01": ("A_plain", "C_strict")})
        task = {"id": "CQ-01", "type": "code_quality"}

        loop.run_combo(task, "A_plain", lines.append)
        loop.run_combo(task, "C_strict", lines.append)

        assert "recall=?unscored" in lines[0]
        assert lines[0].index("total=") == lines[1].index("total=")

    def test_summarizes_list_valued_expected_metrics(self, script_run_bench: Any, tmp_path: Path) -> None:
        """CQ-03 result rendering reports ranking size without formatting the list as a scalar.

        Scenario: the CQ-03 evaluator stores a two-row ranking as ``metric_expected``; the row shows the ranking
        length (2) instead of crashing or printing the list.
        """
        lines: list[str] = []

        def _result(task: dict[str, Any], arm: str) -> Any:
            """Return the list-valued quality contract produced by the CQ-03 evaluator."""
            run = _make_run(script_run_bench, task_id=task["id"], arm=arm)
            run.quality = script_run_bench.BenchQuality(
                scored=True,
                correct=True,
                metric_expected=[{"name": "pkg.alpha", "dep_count": 49}, {"name": "pkg.bravo", "dep_count": 42}],
            )
            return run

        loop = _make_loop(script_run_bench, tmp_path, _result, {"CQ-03": ("B_auto",)})

        loop.run_combo({"id": "CQ-03", "type": "code_quality"}, "B_auto", lines.append)

        assert "\ttotal=   2\t" in lines[0]


class TestRunnerProvenance:
    """Runner records stamp the experiment identity, hashes and policy of the arm that produced them."""

    def test_primary_contract_preserves_raw_task_identity_and_manifest_policy(
        self, script_run_bench: Any, fn02_contract: SimpleNamespace
    ) -> None:
        """The primary suite uses shared raw loading, hashes, and revision-bound policy without a model call.

        Scenario: FN-02 is loaded from the locked suite; its task and prompt hashes must equal the manifest's, and
        its policy must be bound to the current experiment revision.
        """
        task = fn02_contract.task
        manifest = json.loads(script_run_bench.PARITY_MANIFEST_FILE.read_text(encoding="utf-8"))
        expected_task = next(row for suite in manifest["suites"] for row in suite["tasks"] if row["id"] == task["id"])

        assert script_run_bench._task_hash(task) == expected_task["canonical_task_sha256"]
        assert script_run_bench._prompt_hash(task) == expected_task["prompt_sha256"]
        assert fn02_contract.policies[task["id"]].experiment_revision == script_run_bench.PARITY_EXPERIMENT_REVISION

    def test_legacy_run_keeps_only_the_legacy_identity(
        self, script_run_bench: Any, make_runner: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A legacy transport run retains only the explicit legacy experiment identity.

        Scenario: a ``codemap`` arm run goes through ``run``; the record must keep its legacy label and carry no
        parity arm, compliance, adherence or contract hash.
        """
        task = {"id": "legacy-fixture", "prompt": "answer", "type": "fixture", "scoreable": False}
        runner = make_runner()
        monkeypatch.setattr(runner, "_execute", _execute_stub(script_run_bench, "codemap", task))

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

    def test_canonical_run_stamps_the_locked_policy_and_provenance(
        self,
        script_run_bench: Any,
        make_runner: Callable[..., Any],
        fn02_contract: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Canonical arms receive provider-parity task and contract identity.

        Scenario: FN-02 runs as ``B_auto`` under the locked suite; the record carries the revision, suite hashes,
        the arm's contract hash and the evaluator identity.
        """
        task = fn02_contract.task
        runner = make_runner(**fn02_contract.runner_kwargs, suite_raw_hash=script_run_bench.PRIMARY_SUITE_RAW_HASH)
        monkeypatch.setattr(runner, "_execute", _execute_stub(script_run_bench, "B_auto", task))

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

    def test_canonical_task_hash_mismatch_fails_before_execution(
        self,
        make_runner: Callable[..., Any],
        fn02_contract: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Changed task bytes cannot enter a canonical provider-parity run.

        Scenario: the FN-02 prompt is tampered with after the suite was locked; the runner must refuse before it
        reaches ``_execute``, so no paid cell runs against unlocked input.
        """
        task = fn02_contract.task.copy()
        task["prompt"] = f"{task['prompt']} tampered"
        runner = make_runner(**fn02_contract.runner_kwargs)
        monkeypatch.setattr(runner, "_execute", lambda *_args, **_kwargs: pytest.fail("mismatch reached execution"))

        with pytest.raises(ValueError, match=r"task.*hash|hash.*task"):
            runner.run(task, "A_plain")

    def test_resume_rejects_cached_result_with_another_contract(
        self,
        script_run_bench: Any,
        make_runner: Callable[..., Any],
        fn02_contract: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A cached result cannot satisfy a canonical arm after its contract identity changes.

        Scenario: the resume cache holds a ``B_auto`` row recorded under a different arm contract hash; the runner
        must re-execute the cell instead of reusing the stale row.
        """
        task = fn02_contract.task
        runner = make_runner(**fn02_contract.runner_kwargs)
        cache_key = (
            task["id"],
            "B_auto",
            "haiku",
            runner.repo_sha,
            runner.index_sha,
            script_run_bench._task_hash(task),
        )
        monkeypatch.setattr(
            runner,
            "resume_cache",
            {
                cache_key: {
                    "experiment_revision": script_run_bench.PARITY_EXPERIMENT_REVISION,
                    "arm_contract_hash": "different-contract",
                }
            },
        )
        calls: list[str] = []

        def _execute(_task: dict, arm: str, **_kwargs: Any) -> Any:
            """Record the selected treatment and return a successful synthetic run."""
            calls.append(arm)
            return _execute_stub(script_run_bench, arm, task)()

        monkeypatch.setattr(runner, "_execute", _execute)

        runner.run(task, "B_auto")

        assert calls == ["B_auto"]

    def test_run_serialization_includes_treatment_provenance(
        self, script_run_bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """JSONL telemetry preserves adherence and contamination for later pairing ingestion.

        Scenario: a clean ``A_plain`` run is serialized and also saved to disk; both views must carry the
        ``contaminated`` and ``treatment_adherence`` fields.
        """
        run = _make_run(script_run_bench, arm="A_plain", contaminated=False, treatment_adherence=True)
        serialized = script_run_bench.asdict(run)
        monkeypatch.setattr(bench_report, "RESULTS_DIR", tmp_path)
        saved = script_run_bench._save_results([run], "haiku")

        persisted = json.loads(saved.read_text(encoding="utf-8"))
        assert serialized["contaminated"] is False
        assert serialized["treatment_adherence"] is True
        assert persisted["contaminated"] is False
        assert persisted["treatment_adherence"] is True


class TestTreatmentExecution:
    """What a single cell executes: its command line, retries, and the adherence it records."""

    def test_base_command_disables_session_persistence(self, script_run_bench: Any) -> None:
        """Every structural cell starts a non-resumable Claude process session.

        Scenario: a resumed or continued session would carry context between cells and bias the comparison, so the
        base command must forbid all of the resume flags.
        """
        assert "--no-session-persistence" in script_run_bench._CMD
        assert "--continue" not in script_run_bench._CMD
        assert "--resume" not in script_run_bench._CMD
        assert "--session-id" not in script_run_bench._CMD

    @pytest.mark.parametrize("arm", ["A_plain", "B_auto", "C_strict"])
    def test_canonical_commands_omit_turn_caps_while_legacy_keeps_its_task_cap(
        self, script_run_bench: Any, make_runner: Callable[..., Any], monkeypatch: pytest.MonkeyPatch, arm: str
    ) -> None:
        """Canonical structural cells use wall-clock control; legacy cells retain their task cap.

        Scenario: the same blast-radius task (12 callers) is executed under a canonical arm and the legacy
        ``plain`` arm; only the legacy command carries ``--max-turns`` (80, the floor for that count).
        """
        task = {
            "id": "fixture",
            "prompt": "answer",
            "type": "develop_blast_radius",
            "ground_truth": {"unique_caller_count": 12},
            "scoreable": False,
        }
        runner = make_runner()
        commands: dict[str, list[str]] = {}

        def _capture_command(cmd: list[str], result: Any, selected_arm: str, update_fn: Any = None) -> None:
            """Record argv by treatment and supply minimal input-token usage."""
            commands[selected_arm] = cmd
            result.input_tokens = 1

        monkeypatch.setattr(runner, "_stream", _capture_command)
        runner._execute(task, arm)
        runner._execute(task, "plain")

        assert "--max-turns" not in commands[arm]
        legacy = commands["plain"]
        assert legacy[legacy.index("--max-turns") + 1] == "80"

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
        make_runner: Callable[..., Any],
        script_run_bench: Any,
        monkeypatch: pytest.MonkeyPatch,
        arm: str,
        scan_query_calls: int,
        expected_compliance: bool | None,
        expected_adherence: bool,
    ) -> None:
        """Canonical A/B/C rows record assigned availability separately from answer quality.

        Scenario: an unscored fixture task streams with and without a Codemap call; a required-but-missing call
        (``C_strict``) is non-compliant and non-adherent, yet the quality stays the untouched unscored default.
        """
        runner = make_runner()
        monkeypatch.setattr(runner, "_stream", _stream_stub(scan_query_calls=scan_query_calls))

        result = runner._execute(_FIXTURE_TASK, arm)

        assert result.compliance is expected_compliance
        assert result.contaminated is False
        assert result.treatment_adherence is expected_adherence
        assert result.quality == script_run_bench.BenchQuality(scored=False)

    def test_canonical_contamination_makes_treatment_adherence_false(
        self, make_runner: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An answer-file-read is contamination and invalidates a canonical arm.

        Scenario: the plain arm reads the benchmark's answer file while solving; the cell is flagged contaminated
        with ``answer_file_read`` and cannot count as adherent.
        """
        runner = make_runner()
        task = {**_se_task(), "prompt": "answer"}
        monkeypatch.setattr(runner, "_stream", _stream_stub(tool_log=["Read: benchmarks/results/answer.json"]))

        result = runner._execute(task, "A_plain")

        assert result.error == "answer_file_read"
        assert result.contaminated is True
        assert result.treatment_adherence is False

    @pytest.mark.usefixtures("unrelated_sleeper")
    def test_zero_token_stream_is_retried_with_exponential_backoff(
        self, make_runner: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stream that produced no tokens is retried twice, sleeping one then two seconds between attempts.

        Scenario: the provider returns nothing (an API failure) on every attempt; the cell makes three attempts in
        total with 1s and 2s backoff. An unrelated thread sleeps concurrently; only the runner's own backoff calls
        belong in the assertion.
        """
        runner = make_runner()
        sleep = mock.create_autospec(time.sleep)
        monkeypatch.setattr(bench_runner, "time", SimpleNamespace(sleep=sleep))
        attempts: list[Any] = []
        monkeypatch.setattr(runner, "_stream", lambda _cmd, result, _arm, update_fn=None: attempts.append(result))

        runner._execute(_FIXTURE_TASK, "C_strict")

        assert len(attempts) == 3
        assert sleep.call_args_list == [mock.call(1), mock.call(2)]


class TestMainEntry:
    """The ``main`` entry point plans, frames and configures a run without calling a model."""

    def test_dry_run_plans_canonical_cells_without_executing_claude(
        self,
        script_run_bench: Any,
        tmp_path: Path,
        empty_index: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The no-model dry run validates inputs then emits deterministic A/B/C cells only.

        Scenario: FN-02 is planned with parity enabled; the runtime inputs are validated once and the printed plan
        lists the cells in the shared deterministic order, while any attempt to execute fails the test.
        """
        monkeypatch.setattr(
            script_run_bench.BenchRunner,
            "_execute",
            lambda *_args, **_kwargs: pytest.fail("dry run executed Claude"),
        )
        runtime_inputs: list[tuple[Path, Path]] = []
        monkeypatch.setattr(
            bench_cli,
            "_validate_primary_runtime",
            lambda repo_path, index, *_relocation: runtime_inputs.append((repo_path, index)),
        )
        expected_order = ("C_strict", "A_plain", "B_auto")
        monkeypatch.setattr(bench_config, "deterministic_arm_order", lambda *_args, **_kwargs: expected_order)

        script_run_bench.main(
            repo_path=tmp_path,
            index_path=empty_index,
            tasks=["FN-02"],
            arm="all",
            dry_run=True,
            provider_parity=True,
        )

        planned = [
            line.removeprefix("PLAN\t") for line in capsys.readouterr().out.splitlines() if line.startswith("PLAN\t")
        ]
        assert planned == [f"FN-02\t{arm_name}" for arm_name in expected_order]
        assert runtime_inputs == [(tmp_path, empty_index)]

    def test_run_header_and_legend_render_through_the_shared_framed_surfaces(
        self,
        script_run_bench: Any,
        tmp_path: Path,
        empty_index: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A redirected run frames its legend in titled rules and its banner as a plain section rule.

        Scenario: an operator pipes a run into an archived log. The Claude and Codex lanes used to
        disagree here — Codex framed its legend, this runner printed a bare ``Task series legend:``
        header and hand-drawn 64-column banners. The redirected forms are what the log keeps and
        what a replay upgrades to panels, so they are asserted rather than the terminal rendering.
        """
        monkeypatch.setattr(bench_cli, "_validate_primary_runtime", lambda *_args: None)
        monkeypatch.setattr(bench_cli, "BenchRunner", lambda **_kwargs: None)

        def _stop_before_the_first_cell(_console: Any) -> None:
            """Fail the run at progress construction, after the header block is printed."""
            raise _StopRun

        monkeypatch.setattr(bench_cli, "make_progress", _stop_before_the_first_cell)

        with pytest.raises(_StopRun):
            script_run_bench.main(repo_path=tmp_path, index_path=empty_index, tasks=["FN-02"], arm="all")

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
    def test_selects_shared_parity_timeout_without_changing_legacy_runs(
        self,
        script_run_bench: Any,
        tmp_path: Path,
        empty_index: Path,
        monkeypatch: pytest.MonkeyPatch,
        arm: str,
        timeout: int | None,
        expected: int,
    ) -> None:
        """Canonical cells use 600 seconds unless explicitly overridden; legacy policy is unchanged.

        Scenario: ``main`` is stopped at runner construction so the timeout it would pass can be read without
        running a cell.
        """
        monkeypatch.setattr(bench_cli, "_validate_primary_runtime", lambda *_args: None)
        observed: dict[str, int] = {}

        def _capture_runner(*_args: Any, **kwargs: Any) -> None:
            """Record the timeout then stop execution at runner construction."""
            observed["timeout"] = kwargs["timeout"]
            raise _StopRun

        monkeypatch.setattr(bench_cli, "BenchRunner", _capture_runner)

        with pytest.raises(_StopRun):
            script_run_bench.main(
                repo_path=tmp_path,
                index_path=empty_index,
                tasks=["FN-02"],
                arm=arm,
                timeout=timeout,
            )

        assert observed == {"timeout": expected}


class TestSharedEvaluator:
    """The Claude runner scores through the shared evaluator registry exactly once."""

    def test_runs_once_and_preserves_diagnostics(self, script_run_bench: Any) -> None:
        """The shared registry retains the one detailed Claude score rather than evaluating twice.

        Scenario: a registered evaluator returns a detailed ``BenchQuality``; the shared entry point must call it a
        single time and hand back that same object with its recall, evaluator name and scoring detail intact.
        """
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

    def test_uses_recall_as_normalized_quality_score(self, script_run_bench: Any) -> None:
        """Recall-based task families retain their continuous score for paired quality effects.

        Scenario: an evaluator reports recall 0.65 with a binary ``correct=False``; the normalized quality score
        must be the recall, not the binary verdict.
        """
        detailed = script_run_bench.BenchQuality(scored=True, correct=False, recall=0.65, evaluator_used="fixture")
        registry = script_run_bench.EvaluatorRegistry(
            {"demo": script_run_bench._wrap_bench_evaluator(lambda _task, _output: detailed)}
        )

        result = registry.evaluate({"id": "EV-02", "prompt": "score", "type": "demo", "scoreable": True}, "answer")

        assert result.quality_score == pytest.approx(0.65)


# ===========================================================================
# BenchQuality / BenchRun dataclasses
# ===========================================================================


class TestBenchQuality:
    """Field defaults and per-instance isolation for BenchQuality."""

    @pytest.mark.parametrize(
        ("field", "expected"),
        [
            pytest.param("scored", False, id="scored"),
            pytest.param("correct", False, id="correct"),
            pytest.param("extraction_failed", False, id="extraction-failed"),
            pytest.param("recall", None, id="recall"),
            pytest.param("metric_expected", None, id="metric-expected"),
            pytest.param("metric_got", None, id="metric-got"),
            pytest.param("caller_count_gt", None, id="caller-count-gt"),
            pytest.param("evaluator_used", None, id="evaluator-used"),
            pytest.param("evaluator_version", None, id="evaluator-version"),
            pytest.param("extracted_metric", None, id="extracted-metric"),
        ],
    )
    def test_field_defaults_to_unscored_state(self, script_run_bench: Any, field: str, expected: bool | None) -> None:
        """A fresh BenchQuality is unscored and incorrect, with every optional metric unset.

        Scenario: an evaluator that never ran leaves the default object behind; it must read as "no score
        computed" rather than as a zero or a failure.
        """
        assert getattr(script_run_bench.BenchQuality(), field) is expected

    def test_scoring_detail_is_not_shared_between_instances(self, script_run_bench: Any) -> None:
        """Each instance owns an empty scoring_detail dict instead of sharing one mutable default.

        Scenario: one evaluator records detail on its result; a second quality object created afterwards must not
        see that entry.
        """
        q1 = script_run_bench.BenchQuality()
        q2 = script_run_bench.BenchQuality()
        assert q1.scoring_detail == {}
        q1.scoring_detail["x"] = 1
        assert q2.scoring_detail == {}

    def test_explicit_fields_round_trip(self, script_run_bench: Any) -> None:
        """Explicitly set fields are stored and returned verbatim.

        Scenario: an evaluator builds a quality object with a count, a recall and its own name; reading the fields
        back must return exactly what was stored.
        """
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


class TestBenchRun:
    """Field defaults and per-instance isolation for BenchRun."""

    def test_required_fields_stored(self, script_run_bench: Any) -> None:
        """Required constructor arguments are stored in matching fields.

        Scenario: a run is built with only the required arguments (arm, task id and type, model, success); each must
        read back unchanged.
        """
        r = _make_run(script_run_bench)
        assert r.arm == "plain"
        assert r.task_id == "SE-01"
        assert r.task_type == "symbol_extraction"
        assert r.model == "haiku"
        assert r.success is True

    @pytest.mark.parametrize(
        ("field", "expected"),
        [
            pytest.param("input_tokens", 0, id="input-tokens"),
            pytest.param("output_tokens", 0, id="output-tokens"),
            pytest.param("skill_calls", 0, id="skill-calls"),
            pytest.param("grep_calls", 0, id="grep-calls"),
            pytest.param("bash_calls", 0, id="bash-calls"),
            pytest.param("read_calls", 0, id="read-calls"),
            pytest.param("scan_query_calls", 0, id="scan-query-calls"),
            pytest.param("elapsed_s", 0.0, id="elapsed-s"),
            pytest.param("workflow_type", "", id="workflow-type"),
        ],
    )
    def test_counters_and_labels_default_to_empty(self, script_run_bench: Any, field: str, expected: Any) -> None:
        """Token, tool-call and time counters start at zero and the workflow label starts empty.

        Scenario: a run is created before any streaming happens; token, tool-call and time counters start at zero so
        later accumulation adds to a clean base.
        """
        assert getattr(_make_run(script_run_bench), field) == expected

    @pytest.mark.parametrize(
        ("field", "expected"),
        [
            pytest.param("patch_pass", None, id="patch-pass-is-no-signal"),
            pytest.param("incomplete", False, id="incomplete"),
        ],
    )
    def test_signal_flags_default_to_no_signal(self, script_run_bench: Any, field: str, expected: bool | None) -> None:
        """patch_pass defaults to None (no signal) rather than False, and incomplete defaults to False.

        Scenario: a run that never reached patch scoring reads as 'no signal' (None), not as a failed patch, and
        does not start flagged incomplete.
        """
        assert getattr(_make_run(script_run_bench), field) is expected

    def test_quality_defaults_to_unscored(self, script_run_bench: Any) -> None:
        """Quality field must default to an unscored BenchQuality object.

        Scenario: a run created before any evaluator ran carries an unscored BenchQuality, so report code never
        meets a missing quality object.
        """
        r = _make_run(script_run_bench)
        assert isinstance(r.quality, script_run_bench.BenchQuality)
        assert r.quality.scored is False

    def test_tool_log_independent_per_instance(self, script_run_bench: Any) -> None:
        """tool_log is an independent list per BenchRun, not shared state.

        Scenario: two runs are created and one logs a tool call; the other's log stays empty, proving the default
        list is not shared.
        """
        first_run = _make_run(script_run_bench)
        second_run = _make_run(script_run_bench)
        first_run.tool_log.append("Bash: foo")
        assert second_run.tool_log == []

    def test_skill_counts_independent_per_instance(self, script_run_bench: Any) -> None:
        """skill_counts is an independent dict per instance (no shared mutable default).

        Scenario: two runs are created and one counts a skill; the other does not see it, proving the default dict
        is not shared.
        """
        first_run = _make_run(script_run_bench)
        second_run = _make_run(script_run_bench)
        first_run.skill_counts["fix"] = 1
        assert "fix" not in second_run.skill_counts


# ===========================================================================
# _extract_int
# ===========================================================================


class TestExtractInt:
    """Regex-based integer extraction from free-form model output."""

    @pytest.mark.parametrize(
        ("text", "patterns", "expected"),
        [
            pytest.param("found 42 callers in total", [r"(\d+) caller"], 42, id="caller-count"),
            pytest.param("there are 7 unique callers", [r"(\d+) unique"], 7, id="unique-count"),
            pytest.param("UNIQUE CALLERS: 15", [r"(\d+) caller", r"unique callers:\s*(\d+)"], 15, id="later-pattern"),
            # Bold markers are replaced by spaces; use whitespace-tolerant pattern.
            pytest.param("**42** callers", [r"(\d+)\s+caller"], 42, id="bold-markers-stripped"),
            # Backticked value — inline-code markers replaced by spaces (same as bold).
            pytest.param("`25` undocumented functions", [r"(\d+)\s+undocumented"], 25, id="backticks-stripped"),
            pytest.param("total: 100", [r"total[:\s]+(\d+)"], 100, id="total-label"),
            pytest.param(
                "12 unique callers, total: 99", [r"(\d+) unique", r"total[:\s]+(\d+)"], 12, id="first-pattern-wins"
            ),
            pytest.param("**7** unique callers", [r"(\d+)\s+unique"], 7, id="bold-digit-survives-stripping"),
        ],
    )
    def test_extracts_integer_from_matching_pattern(
        self, script_run_bench: Any, text: str, patterns: list[str], expected: int
    ) -> None:
        """_extract_int returns the integer from the first listed pattern that matches, after markup is stripped.

        Scenario: model output wraps the number in bold or backticks, or several patterns would match; the first
        listed pattern wins and the markup never hides the digits.
        """
        assert script_run_bench._extract_int(text, patterns) == expected

    @pytest.mark.parametrize(
        ("text", "patterns"),
        [
            pytest.param("no numbers here", [r"(\d+) caller"], id="no-numbers"),
            pytest.param("", [r"(\d+) caller"], id="empty-text"),
            pytest.param("123", [], id="no-patterns"),
        ],
    )
    def test_returns_none_when_no_match(self, script_run_bench: Any, text: str, patterns: list[str]) -> None:
        """_extract_int returns None when no pattern matches the text.

        Scenario: model output with no number, an empty answer, and a call with no patterns all mean 'nothing
        extracted' (None), never 0.
        """
        assert script_run_bench._extract_int(text, patterns) is None


# ===========================================================================
# cost accounting — captured total_cost_usd
# ===========================================================================


class TestCostAccounting:
    """The $ metric comes from each run's captured total_cost_usd, not a local price table."""

    def test_cost_usd_defaults_to_zero(self, script_run_bench: Any) -> None:
        """A run with no captured cost carries cost_usd 0.0 (so the $ column is omitted).

        Scenario: a run whose stream carried no cost data keeps 0.0, which reporting reads as 'omit the $ column'
        rather than as free.
        """
        assert _make_run(script_run_bench).cost_usd == 0.0

    @pytest.mark.parametrize(
        ("plain_cost", "codemap_cost", "expected"),
        [
            pytest.param(0.10, 0.04, pytest.approx(0.4), id="price-accurate-ratio"),
            pytest.param(0.0, 0.04, pytest.approx(math.nan, nan_ok=True), id="missing-cost-is-nan"),
        ],
    )
    def test_cost_ratio_is_codemap_over_plain(
        self, script_run_bench: Any, plain_cost: float, codemap_cost: float, expected: Any
    ) -> None:
        """The ratio table's cost_ratio is codemap $ / plain $, and NaN when a cost is missing.

        Scenario: a missing cost (0.0) must yield NaN — never a bogus number from a zero denominator.
        """
        plain = _make_run(script_run_bench, arm="plain", task_id="SE-01", cost_usd=plain_cost)
        codemap = _make_run(script_run_bench, arm="codemap", task_id="SE-01", cost_usd=codemap_cost)

        df = script_run_bench._token_ratio_table([plain, codemap])

        assert df[df["task_id"] == "SE-01"].iloc[0]["cost_ratio"] == expected


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


def _scored_parity_runs(script_bench: Any) -> list[Any]:
    """Build one task's three canonical parity runs, scored, where only the control arm answered wrongly."""
    return [
        _make_run(
            script_bench,
            arm=arm,
            task_id="FN-02",
            quality=script_bench.BenchQuality(scored=True, correct=arm != "A_plain"),
        )
        for arm in ("A_plain", "B_auto", "C_strict")
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
        """A parity run yields real token figures instead of the zeros a legacy-name lookup produced.

        Scenario: one task has A_plain and C_strict token counts (3,000,000 vs 41,000); with the arm names passed
        explicitly the table carries both figures and their ratio.
        """
        df = script_run_bench._token_ratio_table(_parity_runs(script_run_bench), "A_plain", "C_strict")
        row = df[df["task_id"] == "FN-02"].iloc[0]
        assert row["plain_tok"] == 3_000_000
        assert row["codemap_tok"] == 41_000
        assert row["ratio"] == pytest.approx(41_000 / 3_000_000)

    def test_paired_accuracy_reads_parity_arm_names(self, script_run_bench: Any) -> None:
        """The paired accuracy view pairs the named control and treatment arms.

        Scenario: the control answered wrongly and ``B_auto`` rightly on the one shared task, so the view reports
        one pair with zero control-correct and one treatment-correct.
        """
        paired = script_run_bench._paired_accuracy(_scored_parity_runs(script_run_bench), "A_plain", "B_auto")
        assert paired == {"n": 1, "plain_correct": 0, "codemap_correct": 1}

    def test_summary_renders_a_section_per_treatment_arm(self, script_run_bench: Any, capsys: Any) -> None:
        """Every treatment arm gets its own labelled comparison against the control.

        Scenario: a three-arm parity result is summarized; the output has one control-versus-treatment section per
        treatment plus the token figures.
        """
        script_run_bench._print_summary(_parity_runs(script_run_bench), "haiku")
        out = capsys.readouterr().out
        assert "A_plain (control) vs C_strict (treatment)" in out
        assert "A_plain (control) vs B_auto (treatment)" in out
        assert "Token ratio (C_strict/A_plain)" in out
        assert "3,000,000" in out

    def test_summary_still_reads_the_legacy_arm_pair(self, script_run_bench: Any, capsys: Any) -> None:
        """Legacy plain/codemap results keep rendering, so older result files stay replayable.

        Scenario: an older result file with only plain and codemap arms is summarized; the legacy pair still renders
        with its token figures.
        """
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
    """A failed stage must leave no residue: __enter__ reverts partial edits before raising.

    These tests run against a real throwaway git repository: restoring tracked files through ``git checkout`` is the
    contract under test.
    """

    def test_enter_reverts_partial_apply_on_missing_anchor(self, script_run_bench: Any, git_repo: Path) -> None:
        """First edit applies, second edit's anchor is absent → raise AND revert the first (no residue).

        Scenario: ``a.py`` is appended to, then the ``b.py`` anchor cannot be found; ``__enter__`` must undo the
        ``a.py`` write before propagating the dirty-tree error.
        """
        stage = [
            {"file": "pkg/a.py", "append": "\n# staged change\n"},
            {"file": "pkg/b.py", "find": "def does_not_exist(", "replace": "def x("},
        ]
        stager = script_run_bench.DiffImpactStager(git_repo, stage)
        with pytest.raises(script_run_bench.DirtyTreeError):
            stager.__enter__()
        assert (git_repo / "pkg" / "a.py").read_text() == _FOO_SOURCE

    def test_successful_revert_records_no_error(self, script_run_bench: Any, git_repo: Path) -> None:
        """The ordinary path leaves the tree clean and the error slot empty.

        Scenario: one append is staged against a real committed repo; inside the block the change is visible, and on
        exit the file is restored and no revert error is recorded.
        """
        stage = [{"file": "pkg/a.py", "append": "\n# staged change\n"}]

        with script_run_bench.DiffImpactStager(git_repo, stage) as stager:
            assert (git_repo / "pkg" / "a.py").read_text().endswith("# staged change\n")

        assert stager.revert_error is None
        assert (git_repo / "pkg" / "a.py").read_text() == _FOO_SOURCE

    @pytest.mark.usefixtures("refuse_checkout")
    def test_failed_revert_escalates_instead_of_silently_leaking(self, script_run_bench: Any, git_repo: Path) -> None:
        """A failed revert leaves the shared tree mutated, so it must not pass silently.

        Before the fix the git result was discarded entirely: the staged synthetic change
        survived into every later task, which then saw an unexplained dirty tree far from
        the task that caused it.
        """
        stage = [{"file": "pkg/a.py", "append": "\n# staged change\n"}]

        with pytest.raises(script_run_bench.DirtyTreeError, match="still mutated"):
            with script_run_bench.DiffImpactStager(git_repo, stage):
                pass

    @pytest.mark.usefixtures("refuse_checkout")
    def test_failed_revert_does_not_mask_an_in_flight_exception(self, script_run_bench: Any, git_repo: Path) -> None:
        """Escalation must never replace the exception carrying the real cause.

        Scenario: the body raises while the checkout that would revert the stage is also refused; the caller must
        still see the body's own error, not the secondary dirty-tree one.
        """
        stage = [{"file": "pkg/a.py", "append": "\n# staged change\n"}]

        with pytest.raises(RuntimeError, match="original cause"):
            with script_run_bench.DiffImpactStager(git_repo, stage):
                raise RuntimeError("original cause")


# ===========================================================================
# PatchSandbox — baseline semantics, apply isolation, interpreter pinning
# ===========================================================================


class TestPatchSandboxLifecycle:
    """PatchSandbox consumes the shared isolated-cell lifecycle: a fresh worktree per attempt, cleaned afterwards."""

    def test_recreates_a_unique_worktree_for_each_retry(
        self, script_run_bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two runs of one sandbox use two distinct worktree roots and leave none behind.

        Scenario: the existing Claude patch boundary is run twice (baseline fails, patched run passes, each time);
        each run must create its own attempt directory, remove it afterwards, and report the last worktree as
        restored with no action or cleanup error.
        """
        boundary = _fake_worktree_boundary(script_run_bench, tmp_path, monkeypatch, [1, 0, 1, 0])
        sandbox = script_run_bench.PatchSandbox(
            tmp_path, _patch_task(test_command="pytest tests/test_fixture.py::test_fix -x")
        )

        results = [sandbox.run(_PATCH), sandbox.run(_PATCH)]

        assert results == [True, True]
        assert len(boundary.roots) == 2
        assert all(not root.exists() for root in boundary.roots)
        assert sandbox.last_mutation_evidence == {
            "worktree": str(boundary.roots[-1] / "repo"),
            "action_error": None,
            "cleanup_error": None,
            "restored": True,
        }


class TestPatchSandboxExitCodes:
    """Pytest exit codes 2-5 carry no test result and must not score as a failed patch."""

    @pytest.mark.parametrize(
        ("codes", "message"),
        [
            pytest.param([2], "baseline pytest exited", id="baseline-exit-2"),
            pytest.param([3], "baseline pytest exited", id="baseline-exit-3"),
            pytest.param([4], "baseline pytest exited", id="baseline-exit-4"),
            pytest.param([5], "baseline pytest exited", id="baseline-exit-5"),
            pytest.param([1, 4], "post-patch pytest exited", id="post-patch-exit-4"),
        ],
    )
    def test_non_result_exit_raises_sandbox_error(
        self, script_run_bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, codes: list[int], message: str
    ) -> None:
        """A baseline or post-patch run that produced no test result is a sandbox error, not a failed patch.

        Exit 4 is the in-tree trigger: ``--timeout=60`` requires pytest-timeout, and
        without the plugin every patch task scored a silent zero. The same rule applies
        after the patch — no result means no evidence.
        """
        _fake_worktree_boundary(script_run_bench, tmp_path, monkeypatch, codes)
        sandbox = script_run_bench.PatchSandbox(
            tmp_path, _patch_task(test_command="pytest tests/test_fixture.py::test_fix -x")
        )

        with pytest.raises(script_run_bench.SandboxError, match=message):
            sandbox.run(_PATCH)

    @pytest.mark.parametrize(
        ("codes", "expected"),
        [
            pytest.param([1, 0], True, id="baseline-exit-one-then-pass-scores-fix"),
            pytest.param([0], False, id="baseline-exit-zero-is-already-passing"),
            pytest.param([1, 1], False, id="post-patch-exit-one-is-failed-patch"),
        ],
    )
    def test_run_scores_patch_from_test_result_exit_codes(
        self, script_run_bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, codes: list[int], expected: bool
    ) -> None:
        """Exit 1 at baseline means the target test genuinely fails, so scoring proceeds; exit 0 cannot validate a fix.

        A baseline that already passes cannot validate a fix, and exit 1 after patching is a genuine failed fix.
        """
        _fake_worktree_boundary(script_run_bench, tmp_path, monkeypatch, codes)
        sandbox = script_run_bench.PatchSandbox(
            tmp_path, _patch_task(test_command="pytest tests/test_fixture.py::test_fix -x")
        )

        assert sandbox.run(_PATCH) is expected


class TestPatchSandboxApply:
    """The apply path must not leave a half-patched tree or harness residue."""

    def test_git_apply_runs_without_reject(
        self, script_run_bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Preserve a nonzero exit when patch rejection follows partial application.

        Scenario: a patch is applied to the baseline worktree; ``--reject`` would leave partial hunks and a zero
        exit, after which the fallback re-applied the same file onto the already-mutated tree.
        """
        boundary = _fake_worktree_boundary(script_run_bench, tmp_path, monkeypatch, [1, 0])
        sandbox = script_run_bench.PatchSandbox(tmp_path, _patch_task(failing_test="tests/t.py::t"))

        sandbox.run(_PATCH)

        apply_commands = [command for command in boundary.commands if "apply" in command]
        assert apply_commands, "expected a git apply invocation"
        assert all("--reject" not in command for command in apply_commands)

    def test_tree_is_reset_before_the_fallback_apply(
        self, script_run_bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed ``git apply`` must not leave hunks behind for ``patch -p1``.

        Scenario: ``git apply`` rejects the diff, so the sandbox falls back to ``patch``; the tracked files must be
        checked out clean before that fallback runs.
        """

        def _reject_apply(command: list[str]) -> SimpleNamespace | None:
            """Fail ``git apply`` with a rejected hunk and leave every other command to the default fake."""
            if "apply" in command:
                return SimpleNamespace(returncode=1, stderr="hunk rejected", stdout="")
            return None

        boundary = _fake_worktree_boundary(script_run_bench, tmp_path, monkeypatch, [1, 0], on_command=_reject_apply)
        sandbox = script_run_bench.PatchSandbox(tmp_path, _patch_task(failing_test="tests/t.py::t"))

        sandbox.run(_PATCH)

        commands = boundary.commands
        checkout_index = next(
            index for index, command in enumerate(commands) if "checkout" in command and "--" in command
        )
        fallback_index = next(index for index, command in enumerate(commands) if command[:1] == ["patch"])
        assert checkout_index < fallback_index

    def test_patch_artifacts_are_removed_before_the_scored_run(
        self, script_run_bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The diff file and any ``.rej`` residue must not survive into the scored test.

        Scenario: the fake ``git apply`` drops a ``a.py.rej`` file next to the patch file; at the moment the scored
        pytest run starts, neither may still be in the worktree.
        """
        seen_during_test: list[list[str]] = []

        def _observe(command: list[str]) -> SimpleNamespace | None:
            """Snapshot the worktree at each pytest start and leave a reject file when ``git apply`` runs."""
            if _is_pytest_argv(command):
                worktree = boundary.roots[0] / "repo"
                seen_during_test.append(sorted(path.name for path in worktree.iterdir()))
                return SimpleNamespace(returncode=1 if len(seen_during_test) == 1 else 0, stderr="", stdout="")
            if "apply" in command:
                (boundary.roots[0] / "repo" / "a.py.rej").write_text("rejected hunk")
                return SimpleNamespace(returncode=0, stderr="", stdout="")
            return None

        boundary = _fake_worktree_boundary(script_run_bench, tmp_path, monkeypatch, [], on_command=_observe)
        sandbox = script_run_bench.PatchSandbox(tmp_path, _patch_task(failing_test="tests/t.py::t"))

        sandbox.run(_PATCH)

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
        """A PATH-resolved pytest can select a different interpreter than the harness.

        Scenario: a patch task names pytest as a bare command, a legacy alias or an absolute path; each is rewritten
        to run through this process's interpreter so every lane resolves the same pytest.
        """
        assert script_run_bench._pin_pytest_interpreter(argv)[:3] == [sys.executable, "-m", "pytest"]

    def test_a_non_pytest_command_is_left_untouched(self, script_run_bench: Any) -> None:
        """Only pytest invocations are rewritten.

        Scenario: a task whose test command is a different runner (tox) passes through unchanged.
        """
        argv = ["tox", "-e", "py311"]

        assert script_run_bench._pin_pytest_interpreter(argv) == argv

    def test_failing_test_argv_uses_the_pinned_interpreter(self, script_run_bench: Any) -> None:
        """The ``failing_test`` path builds its argv from the running interpreter too.

        Scenario: a patch task names only a failing test id; the argv the sandbox builds for it starts with the
        running interpreter and ``-m pytest``.
        """
        sandbox = script_run_bench.PatchSandbox(
            Path("/repo"),
            {"id": "PT-fixture", "pre_fix_commit": "a" * 40, "failing_test": "tests/t.py::t"},
        )

        assert sandbox._test_argv() == [sys.executable, "-m", "pytest", "tests/t.py::t", "-x"]


# ===========================================================================
# _int_close / _count_tol_detail / _safe_ratio
# ===========================================================================


class TestIntClose:
    """Tolerance-based integer comparison helper."""

    @pytest.mark.parametrize(
        ("got", "expected", "tolerance", "result"),
        [
            pytest.param(42, 40, 0.10, True, id="within-ten-percent"),
            pytest.param(44, 40, 0.10, True, id="exactly-at-boundary"),  # 4/40 = 0.10
            pytest.param(45, 40, 0.10, False, id="just-over-boundary"),  # 5/40 = 0.125
            pytest.param(40, 40, 0.10, True, id="exact-match"),
            pytest.param(0, 40, 0.10, False, id="far-below"),
            pytest.param(42, 30, 0.10, False, id="far-above"),  # 12/30 = 0.40
            pytest.param(1, 1, 0.0, True, id="zero-tolerance-exact"),
            pytest.param(2, 1, 0.0, False, id="zero-tolerance-mismatch"),
            pytest.param(None, 40, 0.10, False, id="none-got"),
            pytest.param(None, 0, 0.10, False, id="none-got-zero-expected"),
            pytest.param(1, 0, 0.10, False, id="zero-expected-denominator-floor-miss"),  # abs(1-0)/max(0,1) = 1.0
            pytest.param(0, 0, 0.10, True, id="zero-expected-denominator-floor-hit"),  # diff 0, no ZeroDivisionError
        ],
    )
    def test_within_tolerance(
        self, script_run_bench: Any, got: int | None, expected: int, tolerance: float, result: bool
    ) -> None:
        """_int_close is True iff the absolute deviation is within the fractional tolerance.

        A None ``got`` is never close, and an expected value of 0 clamps the denominator to 1 so no division by zero
        occurs.
        """
        assert script_run_bench._int_close(got, expected, tolerance=tolerance) is result


class TestCountTolDetail:
    """Scoring-detail dict builder for count-tolerance evaluators."""

    @pytest.mark.parametrize(
        ("expected", "got"),
        [
            pytest.param(10, 9, id="close-pair"),
            pytest.param(5, 3, id="distant-pair"),
        ],
    )
    def test_required_fields_present(self, script_run_bench: Any, expected: int, got: int) -> None:
        """Result contains metric_expected, metric_got, threshold, and method for the positional arguments.

        Scenario: a count evaluator records its expected and extracted counts; the detail carries them with the
        fixed 10% threshold and method name for diagnosis.
        """
        d = script_run_bench._count_tol_detail(expected, got)
        assert d["metric_expected"] == expected
        assert d["metric_got"] == got
        assert d["threshold"] == pytest.approx(0.10)
        assert d["method"] == "count_tolerance"

    def test_extra_kwargs_merged(self, script_run_bench: Any) -> None:
        """Extra keyword arguments are merged into the result dict.

        Scenario: an evaluator tags its detail with the check it ran (``check='coupled'``); the extra key appears
        alongside the fixed ones.
        """
        d = script_run_bench._count_tol_detail(10, 9, check="coupled")
        assert d["check"] == "coupled"

    def test_none_values_accepted(self, script_run_bench: Any) -> None:
        """None values for got are stored without error (extraction failed case).

        Scenario: extraction failed so there is no extracted count; the detail stores None for it instead of
        raising.
        """
        d = script_run_bench._count_tol_detail(10, None)
        assert d["metric_got"] is None


class TestSafeRatio:
    """Division helper that returns NaN for undefined denominators."""

    @pytest.mark.parametrize(
        ("num", "den", "expected"),
        [
            pytest.param(10, 4, 2.5, id="fraction"),
            pytest.param(0, 5, 0.0, id="zero-numerator"),
            pytest.param(1, 1, 1.0, id="unit"),
            pytest.param(100, 100, 1.0, id="equal-large"),
        ],
    )
    def test_normal_division(self, script_run_bench: Any, num: float, den: float, expected: float) -> None:
        """_safe_ratio performs ordinary division when denominator is non-zero.

        Scenario: ratios over ordinary non-zero denominators, including a zero numerator, are plain division.
        """
        assert script_run_bench._safe_ratio(num, den) == pytest.approx(expected)

    @pytest.mark.parametrize(
        ("num", "den"),
        [
            pytest.param(10, 0, id="zero-denominator"),
            pytest.param(0, 0, id="zero-over-zero"),
            pytest.param(10, None, id="none-denominator"),
            pytest.param(None, 4, id="none-numerator"),
            pytest.param(None, None, id="both-none"),
        ],
    )
    def test_returns_nan_for_undefined(self, script_run_bench: Any, num: Any, den: Any) -> None:
        """_safe_ratio returns NaN when denominator is zero or either operand is None.

        Scenario: report tables divide token counts that may be zero or missing; an undefined ratio shows as NaN
        rather than raising or printing a bogus number.
        """
        assert math.isnan(script_run_bench._safe_ratio(num, den))


# ===========================================================================
# _parse_scan_query_subcommand
# ===========================================================================


class TestParseScanQuerySubcommand:
    """Extract scan-query subcommand from Bash command strings."""

    @pytest.mark.parametrize(
        ("command", "expected"),
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
            pytest.param(
                "scan-query --index /some/path/index.json fn-rdeps lightning.pytorch.trainer",
                "fn-rdeps",
                id="index-flag-value-skipped",
            ),
            pytest.param("scan-query --index=/some/path.json symbol Trainer", "symbol", id="equals-form-flag-skipped"),
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
    def test_known_subcommands_extracted(self, script_run_bench: Any, command: str, expected: str) -> None:
        """_parse_scan_query_subcommand returns the recognised subcommand token.

        Covers the bare, full-path, ``--index <path>``, ``--index=<path>``, quoted-path, repeated-index-flag, and
        env-prefixed invocation shapes: each global flag and its value is skipped so the subcommand is exposed.
        """
        assert script_run_bench._parse_scan_query_subcommand(command) == expected

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("grep -r foo .", id="grep-command"),
            pytest.param("ls -la", id="ls-command"),
            pytest.param("python3 -m pytest", id="pytest-command"),
            pytest.param("", id="empty-command"),
            pytest.param("scan-query unknown-subcmd foo", id="unknown-subcommand"),
            pytest.param("echo scan-query symbol Trainer", id="scan-query-as-echo-data"),
            pytest.param("python -c 'print(\"scan-query symbol Trainer\")'", id="scan-query-inside-python-string"),
            pytest.param("scan-query --unknown value symbol Trainer", id="unknown-global-flag-with-value"),
            pytest.param("scan-query --unknown=value symbol Trainer", id="unknown-global-flag-equals-form"),
        ],
    )
    def test_non_invocations_return_none(self, script_run_bench: Any, command: str) -> None:
        """_parse_scan_query_subcommand returns None when no scan-query query is actually invoked.

        Unrelated commands, an empty command, unrecognised subcommands, scan-query mentioned only as data, and unknown
        global flags are not credited as a query.
        """
        assert script_run_bench._parse_scan_query_subcommand(command) is None


# ===========================================================================
# _normalize_external_task / _load_tasks_file
# ===========================================================================


class TestNormalizeExternalTask:
    """Schema normalizer for tasks loaded via ``--tasks-file``."""

    def test_queries_renamed_to_expected_queries(self, script_run_bench: Any) -> None:
        """'queries' key is renamed to 'expected_queries' and original key removed.

        Scenario: an external task file uses the short ``queries`` key while the runner works with
        ``expected_queries``; the value moves to the new key and the old one disappears.
        """
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
        """Tasks without a 'type' field get type=develop_skill.

        Scenario: an external task omits ``type``; it is treated as the generic external (develop-skill) task type.
        """
        task = {"id": "B-01", "prompt": "p", "skill": "fix"}
        result = script_run_bench._normalize_external_task(task)
        assert result["type"] == script_run_bench._EXTERNAL_TASK_TYPE

    def test_existing_type_preserved(self, script_run_bench: Any) -> None:
        """Tasks with an existing 'type' field keep that type unchanged.

        Scenario: an external task already names a built-in type (debug_from_trace); normalization does not
        overwrite it.
        """
        task = {
            "id": "DBG-01",
            "type": "debug_from_trace",
            "prompt": "p",
            "ground_truth": {"function": "f", "file": "a.py", "start_line": 1},
        }
        result = script_run_bench._normalize_external_task(task)
        assert result["type"] == "debug_from_trace"

    @pytest.mark.parametrize(
        ("task", "expected"),
        [
            pytest.param({"id": "B-01", "prompt": "p", "skill": "fix"}, False, id="no-ground-truth-forced-unscoreable"),
            pytest.param(
                {
                    "id": "DBG-01",
                    "type": "debug_from_trace",
                    "scoreable": True,
                    "prompt": "p",
                    "ground_truth": {"function": "f", "file": "a.py", "start_line": 1},
                },
                True,
                id="explicit-flag-preserved",
            ),
            pytest.param(
                {
                    "id": "DBG-02",
                    "type": "debug_from_trace",
                    "prompt": "p",
                    "ground_truth": {"function": "g", "file": "b.py", "start_line": 5},
                },
                True,
                id="defaults-true-without-flag",
            ),
        ],
    )
    def test_scoreable_follows_ground_truth(self, script_run_bench: Any, task: dict, expected: bool) -> None:
        """A task is scoreable only when it carries ground_truth, whether or not the flag was written.

        Scenario: a task without ground_truth is forced unscoreable; an explicit scoreable=True keeps that flag; a
        task carrying ground_truth but no flag defaults to True.
        """
        result = script_run_bench._normalize_external_task(task)
        assert result["scoreable"] is expected

    def test_original_task_dict_not_mutated(self, script_run_bench: Any) -> None:
        """_normalize_external_task must not mutate the input dict.

        Scenario: the loader normalizes the caller's task; the caller's own dict is left exactly as it was, so a
        task can be normalized more than once.
        """
        task = {"id": "B-01", "prompt": "p", "queries": [{"cmd": "rdeps"}]}
        original = dict(task)
        script_run_bench._normalize_external_task(task)
        assert task == original


class TestLoadTasksFile:
    """JSON task file loader — parsing, normalization, and error paths."""

    @staticmethod
    def _write_json(tmp_path: Path, data: Any) -> Path:
        """Write data as JSON to a temp file and return its path."""
        p = tmp_path / "tasks.json"
        p.write_text(json.dumps(data))
        return p

    @pytest.mark.parametrize(
        ("data", "expected_ids"),
        [
            pytest.param([{"id": "X-01", "prompt": "p", "skill": "fix"}], ["X-01"], id="bare-list"),
            pytest.param(
                {"repo": {"name": "myrepo"}, "tasks": [{"id": "Y-01", "prompt": "p", "skill": "fix"}]},
                ["Y-01"],
                id="object-with-tasks-key",
            ),
            pytest.param([], [], id="empty-list"),
            pytest.param(
                [{"id": "A-01", "prompt": "p"}, {"id": "A-02", "prompt": "p"}], ["A-01", "A-02"], id="several-in-order"
            ),
        ],
    )
    def test_loads_tasks_in_file_order(
        self, script_run_bench: Any, tmp_path: Path, data: Any, expected_ids: list[str]
    ) -> None:
        """A bare list or a ``{'repo': ..., 'tasks': [...]}`` object yields its tasks in file order.

        Scenario: the two accepted file shapes, an empty list (no error) and several tasks (none dropped).
        """
        result = script_run_bench._load_tasks_file(self._write_json(tmp_path, data))
        assert [task["id"] for task in result] == expected_ids

    @pytest.mark.parametrize(
        "data",
        [
            pytest.param([{"id": "X-01", "prompt": "p", "skill": "fix", "queries": [{"cmd": "rdeps"}]}], id="one-task"),
            pytest.param(
                [
                    {"id": "A-01", "prompt": "p", "queries": [{"cmd": "rdeps"}]},
                    {"id": "A-02", "prompt": "p", "queries": [{"cmd": "symbol"}]},
                ],
                id="every-task-of-several",
            ),
        ],
    )
    def test_every_loaded_task_is_normalized(self, script_run_bench: Any, tmp_path: Path, data: Any) -> None:
        """All tasks in the list are normalized, not just the first.

        Scenario: a task file with one task and one with several; every task, not only the first, has ``queries``
        renamed to ``expected_queries``.
        """
        result = script_run_bench._load_tasks_file(self._write_json(tmp_path, data))
        assert result
        assert all("expected_queries" in t and "queries" not in t for t in result)

    def test_missing_file_raises_file_not_found(self, script_run_bench: Any, tmp_path: Path) -> None:
        """FileNotFoundError raised when the file does not exist.

        Scenario: the path given to ``--tasks-file`` does not exist; the user gets a clear 'not found' error.
        """
        with pytest.raises(FileNotFoundError, match="not found"):
            script_run_bench._load_tasks_file(tmp_path / "nonexistent.json")

    @pytest.mark.parametrize(
        ("content", "message"),
        [
            pytest.param("{not valid json", "not valid JSON", id="malformed-json"),
            pytest.param("42", "must be a JSON list or object", id="unexpected-shape"),
        ],
    )
    def test_invalid_content_raises_value_error(
        self, script_run_bench: Any, tmp_path: Path, content: str, message: str
    ) -> None:
        """ValueError raised when the file is invalid JSON or JSON that is neither a list nor an object.

        Scenario: the file is not valid JSON, or is valid JSON of the wrong shape (a bare number); the user gets a
        ValueError naming the problem.
        """
        p = tmp_path / "bad.json"
        p.write_text(content)
        with pytest.raises(ValueError, match=message):
            script_run_bench._load_tasks_file(p)


# ===========================================================================
# _evaluate_symbol
# ===========================================================================


class TestEvaluateSymbol:
    """symbol_extraction evaluator: start_line within ±5 lines of ground truth."""

    @pytest.mark.parametrize(
        ("output_text", "start_line", "expected_correct"),
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
            pytest.param(
                "**start_line**: 200", 200, True, id="bold-markers-stripped-before-parsing"
            ),  # markdown bold around start_line is ignored by the parser
            pytest.param(
                "file_path: src/lightning/trainer/trainer.py:1110\N{EN DASH}1125",
                1110,
                True,
                id="source-location-range-without-generic-line-prose",
            ),  # a compact ``path.py:start-end`` answer is scoreable without the redundant word "Lines"
        ],
    )
    def test_correct_when_start_line_within_tolerance(
        self, script_run_bench: Any, output_text: str, start_line: int, expected_correct: bool
    ) -> None:
        """_evaluate_symbol marks correct when extracted start_line is within ±5 of GT.

        Scenario: answers phrase the start line many ways (labelled, ranged, backticked, bold, source-location);
        each shape is parsed and judged against the tolerance, with boundaries at exactly five lines.
        """
        task = _se_task(start_line=start_line)
        result = script_run_bench._evaluate_symbol(task, output_text)
        assert result.scored is True
        assert result.correct is expected_correct

    def test_extraction_failed_when_no_line_in_output(self, script_run_bench: Any) -> None:
        """Mark extraction as failed and incorrect when no start line can be parsed.

        Scenario: the model answered in prose with no line number; the run is scored as an extraction failure, not
        as a wrong line.
        """
        task = _se_task(start_line=100)
        result = script_run_bench._evaluate_symbol(task, "The function does something useful.")
        assert result.scored is True
        assert result.extraction_failed is True
        assert result.correct is False

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
        """metric_expected is always the ground-truth start_line value.

        Scenario: any answer; the report's expected-metric column shows the ground-truth start line.
        """
        task = _se_task(start_line=42)
        result = script_run_bench._evaluate_symbol(task, "start_line: 42")
        assert result.metric_expected == 42

    def test_scoring_detail_contains_threshold(self, script_run_bench: Any) -> None:
        """scoring_detail carries threshold and method for diagnosis.

        Scenario: any scored answer; its scoring detail names the line-tolerance method and the five-line threshold
        so a reader can see how it was judged.
        """
        task = _se_task(start_line=50)
        result = script_run_bench._evaluate_symbol(task, "start_line: 50")
        assert result.scoring_detail["threshold"] == 5
        assert result.scoring_detail["method"] == "line_tolerance"


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
        """A task with no sub_questions cannot be scored.

        Scenario: a review task with an empty sub-question list has nothing to grade, so it is left unscored instead
        of counting as a miss.
        """
        task = {"id": "RV-01", "type": "review_assistance", "sub_questions": []}
        result = script_run_bench._evaluate_rv(task, "something")
        assert result.scored is False

    @pytest.mark.parametrize(
        ("answer", "expected_correct"),
        [
            pytest.param("found 20 undocumented symbols", True, id="within-tolerance"),
            pytest.param("found 30 undocumented symbols", False, id="outside-tolerance"),
        ],
    )
    def test_count_path_correct_only_within_tolerance(
        self, script_run_bench: Any, answer: str, expected_correct: bool
    ) -> None:
        """Count path: correct when the extracted count is within 10% of expected, incorrect outside it.

        Scenario: the expected count is 20; 'found 20' is accepted and 'found 30' (50% over) is not.
        """
        task = self._rv_task_count(20)
        result = script_run_bench._evaluate_rv(task, answer)
        assert result.scored is True
        assert result.correct is expected_correct

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
        """Immutable RV-02 A/B/C answers must parse their directly-imported count.

        Scenario: six recorded RV-02 answers from the three arms, with narration glued to the number, bold list
        numbering and a production-versus-total module split; all must yield the count 64.
        """
        result = script_run_bench._evaluate_rv(self._rv_task_count(64), answer)

        assert result.metric_got == 64
        assert result.correct is True
        assert result.extraction_failed is False

    def test_rv05_exact_partial_answer_scores_count_and_symbol_components_separately(
        self, script_run_bench: Any
    ) -> None:
        """RV-05 keeps correct count credit while retaining its 2/5 symbol recall loss.

        Scenario: a recorded RV-05 answer gets the count (11) right but lists only two of the five expected symbols;
        the count component is credited, the symbol component is partial, and the overall result is not correct.
        """
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

    @pytest.mark.parametrize(
        ("count", "answer"),
        [
            pytest.param(
                24,
                "1. **24** production functions uniquely call `lightning.pytorch.trainer.call::_call_callback_hooks`.",
                id="trailing-qualifier-after-the-noun",
            ),
            pytest.param(
                11,
                "Checked the oracle directly.\n1. **11**\n\n2. Lexicographically first five uncovered symbols:\n",
                id="bare-numbered-sub-answer",
            ),
            pytest.param(
                20,
                "1. 3 candidate files inspected.\nThe module has 20 undocumented symbols.",
                id="phrased-count-outranks-enumerated-step-number",
            ),
        ],
    )
    def test_count_extracted_from_answer_shape(self, script_run_bench: Any, count: int, answer: str) -> None:
        """The expected count is extracted and credited across the answer shapes a model may produce.

        A qualifier following the noun instead of preceding it still counts, an enumerated sub-answer carrying the
        number alone is still an extractable count, and a noun-anchored count wins over a leading integer on an
        enumerated exploration line.
        """
        result = script_run_bench._evaluate_rv(self._rv_task_count(count), answer)

        assert result.metric_got == count
        assert result.extraction_failed is False
        assert result.correct is True

    @pytest.mark.parametrize(
        ("found", "expected_correct"),
        [
            pytest.param(7, True, id="at-threshold"),
            pytest.param(5, False, id="below-threshold"),
        ],
    )
    def test_symbol_recall_path_correct_only_at_threshold(
        self, script_run_bench: Any, found: int, expected_correct: bool
    ) -> None:
        """Symbol-recall path: correct when >=70% of expected symbols appear in output, incorrect below it.

        Of 10 expected symbols, 7 found by short name gives recall 0.70 (correct); only 5 gives 0.50 (incorrect).
        """
        syms = [f"lightning.pytorch.mod::Cls.method{i}" for i in range(10)]
        task = self._rv_task_symbols(syms)
        text = " ".join(s.split(".")[-1] for s in syms[:found])
        result = script_run_bench._evaluate_rv(task, text)
        assert result.scored is True
        assert result.correct is expected_correct
        assert result.recall is not None
        assert (result.recall >= 0.70) is expected_correct

    def test_symbol_path_extraction_failed_when_no_symbol_found(self, script_run_bench: Any) -> None:
        """Symbol path: no extractable symbol → extraction_failed=True (excluded from accuracy).

        Scenario: a symbol-set question answered with text naming none of the five expected symbols; the run counts
        as an extraction failure (excluded from accuracy) with nothing found.
        """
        syms = [f"lightning.pytorch.mod::Cls.method{i}" for i in range(5)]
        task = self._rv_task_symbols(syms)
        result = script_run_bench._evaluate_rv(task, "## Symbols\nnothing recognisable here at all\n")
        assert result.scored is True
        assert result.metric_got == 0
        assert result.extraction_failed is True
        assert result.correct is False

    def test_symbol_path_extraction_not_failed_when_symbol_found(self, script_run_bench: Any) -> None:
        """Symbol path: at least one symbol found → extraction_failed stays False.

        Scenario: four of five expected symbols appear by short name; extraction succeeded even though recall is
        partial.
        """
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

    @pytest.mark.parametrize(
        ("check", "gt_fields", "answer"),
        [
            pytest.param("coupled", {"top_dep_count": 100}, "dep_count: 100", id="coupled-within-tolerance"),
            pytest.param(
                "xrefs_broken",
                {"broken_count": 3, "broken_targets": []},
                "3 broken xrefs detected",
                id="xrefs-broken-exact-match",
            ),
            pytest.param(
                "undocumented",
                {"undocumented_count": 10},
                "10 undocumented symbols found",
                id="undocumented-within-tolerance",
            ),
            pytest.param("uncovered", {"uncovered_count": 20}, "20 uncovered symbols", id="uncovered-within-tolerance"),
            pytest.param(
                "combined_health",
                {"undocumented_count": 5, "uncovered_count": 8},
                "## Answer\nundocumented_count: 5\nuncovered_count: 8\n",
                id="combined-health-both-explicit-counts",
            ),
        ],
    )
    def test_scored_and_correct_when_count_matches_ground_truth(
        self, script_run_bench: Any, check: str, gt_fields: dict, answer: str
    ) -> None:
        """Every check type scores an answer whose extracted count matches the ground truth.

        coupled / undocumented / uncovered accept a count within 10% of GT, xrefs_broken needs the exact broken count,
        and combined_health requires and accepts its two labelled count fields.
        """
        task = self._oss_task(check, **gt_fields)
        result = script_run_bench._evaluate_oss(task, answer)
        assert result.scored is True
        assert result.correct is True

    @pytest.mark.parametrize(
        ("check", "gt_fields", "answer"),
        [
            pytest.param("coupled", {"top_dep_count": 100}, "120 dependencies found", id="coupled-outside-tolerance"),
            pytest.param(
                "xrefs_broken",
                {"broken_count": 3, "broken_targets": []},
                "4 broken xrefs",
                id="xrefs-broken-wrong-count",
            ),
        ],
    )
    def test_incorrect_when_count_misses_ground_truth(
        self, script_run_bench: Any, check: str, gt_fields: dict, answer: str
    ) -> None:
        """An extracted count outside the 10% coupled tolerance, or not exactly the xrefs_broken count, is incorrect.

        Scenario: a coupled answer of 120 against 100 (20% off) and an xrefs answer of 4 against 3; the first
        exceeds the tolerance and the second is not the exact broken count.
        """
        task = self._oss_task(check, **gt_fields)
        result = script_run_bench._evaluate_oss(task, answer)
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
        """CQ-03 accepts each requested module/count pair in rank order, not only rank one.

        Scenario: the five-module ranking is answered as a numbered list and as a Markdown table; both yield the
        full ordered module/count list.
        """
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
        """CQ-03 rejects missing, substituted, reordered, and mismatched requested rows.

        Scenario: the ranking answer is missing a row, has a substituted module, has two rows swapped, or has one
        wrong count; each is judged incorrect yet still parsed, not an extraction failure.
        """
        result = script_run_bench._evaluate_oss(self._cq03_task(), answer)

        assert result.extraction_failed is False
        assert result.correct is False

    def test_xrefs_broken_uses_target_names_when_available(self, script_run_bench: Any) -> None:
        """xrefs_broken check: count found symbols from broken_targets list.

        Scenario: the ground truth lists broken targets; an answer that mentions both short names finds both and
        matches the expected count of 2.
        """
        broken_targets = [{"target": "mod::foo"}, {"target": "mod::bar"}]
        task = self._oss_task("xrefs_broken", broken_count=2, broken_targets=broken_targets)
        # Both short names present in output → got=2 == expected=2 → correct
        result = script_run_bench._evaluate_oss(task, "References to foo and bar are broken.")
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
        """Explicit count labels retain count credit while required symbols remain missing.

        Scenario: the task requires an independent-AST count and symbol list; an answer giving only the labelled
        count earns the count half (recall 0.5) but stays incorrect with the symbols missing.
        """
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
        """Incidental numbers cannot satisfy a required count component.

        Scenario: the answer contains the right number only incidentally ('after 11 checks', 'in 11 files'); it must
        not satisfy the required count component.
        """
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
        """A label-first parenthesized uncovered count is a valid explicit answer.

        Scenario: the answer states 'Uncovered symbols (11)' under an Answer heading; the parenthesized count is
        read as the answer.
        """
        task = self._oss_task("uncovered", uncovered_count=11)

        result = script_run_bench._evaluate_oss(task, "## Answer\nUncovered symbols (11)\n")

        assert result.correct is True
        assert result.metric_got == 11

    def test_combined_health_requires_both_explicit_count_components(self, script_run_bench: Any) -> None:
        """A correct docstring count cannot hide a wrong uncovered count.

        Scenario: the docstring count is right (3) but the uncovered count is wrong (6 vs 7); the combined answer is
        incorrect and the uncovered component says why.
        """
        task = self._oss_task("combined_health", undocumented_count=3, uncovered_count=7)
        output = "## Answer\nundocumented_count: 3\nuncovered_count: 6\n"

        result = script_run_bench._evaluate_oss(task, output)

        assert result.correct is False
        assert result.scoring_detail["components"]["uncovered_count"]["correct"] is False

    def test_unknown_check_type_returns_unscored(self, script_run_bench: Any) -> None:
        """An unrecognised check value results in scored=False.

        Scenario: the ground truth names a check type the evaluator does not know; it declines to score instead of
        guessing.
        """
        task = self._oss_task("unknown_check", some_count=5)
        result = script_run_bench._evaluate_oss(task, "5 things found")
        assert result.scored is False

    @pytest.mark.parametrize(
        ("check", "gt_fields"),
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
        """Malformed GT count fields must not hide failure when no count is extractable.

        Scenario: the ground truth lacks its count field or holds 'many' or None, and the answer has no number; the
        result stays an extraction failure with no extracted metric.
        """
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
        """Accept output when both function name and file basename appear in output.

        Scenario: the answer names ``my_function`` and ``utils.py``, the basename of the ground-truth path.
        """
        task = self._debug_task()
        result = script_run_bench._evaluate_debug(task, "The bug is in my_function inside utils.py")
        assert result.scored is True
        assert result.correct is True

    @pytest.mark.parametrize(
        "output",
        [
            pytest.param("The bug is in my_function somewhere", id="only-fn-present"),
            pytest.param("Check inside utils.py for the problem", id="only-file-present"),
            pytest.param("The issue is in my_function_extra inside utils_extra.py", id="longer-identifiers"),
            pytest.param("my_functionality moved to old_utils.py", id="substrings-of-longer-names"),
        ],
    )
    def test_incorrect_unless_both_exact_function_and_file_present(self, script_run_bench: Any, output: str) -> None:
        """Reject output that names only one of function/file, or only longer identifiers embedding them.

        Only the function name, only the file basename, and function/file substrings embedded in longer names
        (``my_function_extra``, ``utils_extra.py``) are not enough to score.
        """
        task = self._debug_task(fn="my_function", filepath="src/mod/utils.py")
        result = script_run_bench._evaluate_debug(task, output)
        assert result.correct is False

    def test_incorrect_when_neither_present(self, script_run_bench: Any) -> None:
        """Reject output when neither token appears.

        Scenario: an answer naming neither the function nor the file is incorrect and an extraction failure, not a
        partial miss.
        """
        task = self._debug_task()
        result = script_run_bench._evaluate_debug(task, "Completely unrelated output text")
        assert result.correct is False
        assert result.extraction_failed is True

    @pytest.mark.parametrize(
        ("output", "expected_recall"),
        [
            pytest.param("my_function in utils.py is the culprit", 1.0, id="both-tokens-full-recall"),
            pytest.param("only my_function appears here", 0.5, id="one-of-two-half-recall"),
        ],
    )
    def test_recall_is_fraction_of_expected_tokens_found(
        self, script_run_bench: Any, output: str, expected_recall: float
    ) -> None:
        """Recall is the fraction of the two expected tokens (function, file) present in the output.

        Both present reports full recall; exactly one of two reports half recall.
        """
        task = self._debug_task()
        result = script_run_bench._evaluate_debug(task, output)
        assert result.recall == pytest.approx(expected_recall)

    def test_match_is_case_insensitive(self, script_run_bench: Any) -> None:
        """Word-boundary matching is case-insensitive.

        Scenario: the answer upper-cases the function name (MYFUNCTION); matching ignores case.
        """
        task = self._debug_task(fn="MyFunction")
        result = script_run_bench._evaluate_debug(task, "MYFUNCTION in utils.py")
        assert result.correct is True


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
        """Accept output when labelled Class.method and file path appear in output.

        Scenario: an answer with a Files block labelling both the primary file and the exact Class.method entry
        point is scored correct under evaluator version v4.
        """
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
            pytest.param("entry_point: validate", False, id="bare-method-without-period"),
            pytest.param("entry_point: Other.validate.", False, id="wrong-class"),
            pytest.param("entry_point: Trainer.validate_extra.", False, id="longer-entry-point"),
            pytest.param("entry_point: Trainer.validate..", False, id="doubled-period"),
        ],
    )
    def test_terminal_period_only_terminates_the_exact_labelled_entry_point(
        self, script_run_bench: Any, entry_point_line: str, expected_correct: bool
    ) -> None:
        """A sentence period is accepted without relaxing exact entry-point matching.

        Scenario: the labelled entry point ends with a sentence period (plain or backticked) and is accepted; a bare
        method name, wrong class, longer name or doubled period is not.
        """
        task = self._feature_task()
        result = script_run_bench._evaluate_feature(
            task,
            f"## Files\nprimary_file: src/lightning/trainer/trainer.py\n{entry_point_line}\n",
        )

        assert result.correct is expected_correct

    def test_exploration_prose_cannot_credit_a_different_entry_point(self, script_run_bench: Any) -> None:
        """An exploratory mention of the GT method cannot override the final labelled answer.

        Scenario: the answer's prose mentions the ground-truth method while exploring but its final labelled entry
        point names a different method; only the labelled line counts.
        """
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

    def test_extraction_failed_when_neither_found(self, script_run_bench: Any) -> None:
        """Mark extraction as failed when neither the entry point nor file stem is found.

        Scenario: an answer naming neither the entry point nor the file stem is an extraction failure.
        """
        task = self._feature_task()
        result = script_run_bench._evaluate_feature(task, "nothing relevant here")
        assert result.extraction_failed is True

    @pytest.mark.parametrize(
        "output",
        [
            pytest.param("implement validate somewhere", id="file-missing-from-output"),
            pytest.param("Add invalidate logic in trainer_extra.py", id="longer-method-and-file-names"),
            pytest.param("validation belongs in pretrainer.py", id="substrings-of-longer-names"),
        ],
    )
    def test_incorrect_unless_exact_entry_point_and_file_present(self, script_run_bench: Any, output: str) -> None:
        """Reject output that omits the file, or only embeds the method/file as substrings of longer names.

        A missing file basename, ``invalidate``/``trainer_extra.py`` and ``validation``/``pretrainer.py`` are not enough
        to score.
        """
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
        """Accept output when all GT file basenames appear in the output.

        Scenario: the answer edits both files named in the ground truth; recall is 1.0 and the result is correct.
        """
        task = self._ri_task(["src/trainer.py", "src/loops.py"])
        result = script_run_bench._evaluate_real_issue(task, "Edit trainer.py and loops.py to fix the issue")
        assert result.scored is True
        assert result.correct is True
        assert result.recall == pytest.approx(1.0)

    @pytest.mark.parametrize(
        ("mentioned", "expected_correct"),
        [
            pytest.param(7, True, id="exactly-threshold"),
            pytest.param(5, False, id="below-threshold"),
        ],
    )
    def test_correct_only_when_recall_reaches_threshold(
        self, script_run_bench: Any, mentioned: int, expected_correct: bool
    ) -> None:
        """Accept output when recall is at least 0.70 of GT files; reject below it.

        Of 10 GT files, mentioning exactly 7 (recall 0.70) is correct; mentioning only 5 (recall 0.50) is not.
        """
        files = [f"src/mod{i}.py" for i in range(10)]
        task = self._ri_task(files)
        text = " ".join(f"mod{i}" for i in range(mentioned))
        result = script_run_bench._evaluate_real_issue(task, text)
        assert result.correct is expected_correct

    def test_empty_files_changed_returns_unscored(self, script_run_bench: Any) -> None:
        """Leave the result unscored when ground_truth.files_changed is empty.

        Scenario: the ground truth lists no changed files, so there is nothing to compare and the result stays
        unscored.
        """
        task = self._ri_task([])
        result = script_run_bench._evaluate_real_issue(task, "anything")
        assert result.scored is False

    def test_extraction_failed_when_no_file_found(self, script_run_bench: Any) -> None:
        """Mark extraction as failed when no expected file basename appears.

        Scenario: the answer names none of the expected files; extraction failed rather than a zero-recall miss.
        """
        task = self._ri_task(["src/trainer.py"])
        result = script_run_bench._evaluate_real_issue(task, "completely unrelated output")
        assert result.extraction_failed is True

    @pytest.mark.parametrize(
        ("files", "answer", "expected_correct"),
        [
            pytest.param(
                ["deeply/nested/path/trainer.py"],
                "edit trainer.py to fix the bug",
                False,
                id="bare-basename-does-not-match-deep-path",
            ),
            pytest.param(
                ["deeply/nested/path/trainer.py"],
                "## Files\npath/trainer.py\n",
                True,
                id="path-with-parent-matches",
            ),
            pytest.param(
                ["src/lightning/pytorch/trainer/trainer.py"],
                "The trainer orchestrates training; the trainer loop runs many trainer callbacks.",
                False,
                id="verbose-prose-common-stem-does-not-score",
            ),
            pytest.param(
                ["src/lightning/pytorch/trainer/trainer.py"],
                "## Files\nlightning/pytorch/trainer/trainer.py\n",
                True,
                id="structured-answer-with-path-scores",
            ),
            pytest.param(
                ["src/pkg/foo.py"],
                "## Files\npkg/foobar.py\n",
                False,
                id="path-substring-inside-longer-filename-does-not-score",
            ),
        ],
    )
    def test_file_hit_requires_path_with_parent_not_bare_name(
        self, script_run_bench: Any, files: list[str], answer: str, expected_correct: bool
    ) -> None:
        """A GT file is credited only when the answer names it by a path-with-parent, never by prose or substring.

        A bare basename or a verbose name-drop of the stem scores nothing against a deeply-nested GT file, a path
        candidate embedded in a longer filename is not a hit, while a ``parent/stem`` path in a block scores.
        """
        task = self._ri_task(files)
        result = script_run_bench._evaluate_real_issue(task, answer)
        assert result.correct is expected_correct


# ===========================================================================
# Structured-block scoring helpers
# ===========================================================================


class TestStructuredBlockScoring:
    """Answer-block extraction, stem blocklist, and RI path matching helpers."""

    def test_answer_region_returns_block_after_heading(self, script_run_bench: Any) -> None:
        """Region starts at the answer heading, dropping the exploration prose before it.

        Scenario: exploration prose precedes a ``## Files`` heading; the region starts at the heading and drops the
        exploration.
        """
        text = "exploring trainer everywhere\n## Files\npkg/trainer.py\n"
        region, degraded = script_run_bench._answer_region(text, ("files",))
        assert degraded is False
        assert "exploring" not in region
        assert "pkg/trainer.py" in region

    def test_answer_region_degraded_without_heading(self, script_run_bench: Any) -> None:
        """No heading → degraded=True and the full text is returned as fallback.

        Scenario: the answer has no recognised heading; the whole text is returned and the result is flagged
        degraded.
        """
        region, degraded = script_run_bench._answer_region("just prose about trainer", ("files",))
        assert degraded is True
        assert region == "just prose about trainer"

    def test_answer_region_matches_labelled_line(self, script_run_bench: Any) -> None:
        """A bold/plain `Files:` label line is recognised as a heading.

        Scenario: the answer uses a bold ``**Files:**`` label line instead of a Markdown heading; it still opens the
        answer region.
        """
        _, degraded = script_run_bench._answer_region("**Files:**\npkg/x.py\n", ("files",))
        assert degraded is False

    @pytest.mark.parametrize(
        ("stem", "text", "expected"),
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
    def test_blocklisted_stems_need_a_qualified_reference(
        self, script_run_bench: Any, stem: str, text: str, expected: bool
    ) -> None:
        """Blocklisted stems need a qualified reference; other stems match on a word boundary.

        Scenario: words like 'trainer' and 'utils' are too common to count as a bare mention; they count only as
        ``trainer.py`` or ``pkg.trainer``, while a distinctive stem such as ``fit_loop`` matches as a plain word.
        """
        assert script_run_bench._stem_matches(stem, text) is expected

    @pytest.mark.parametrize(
        ("file_path", "text", "expected"),
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
    def test_counts_only_full_or_parent_qualified_paths(
        self, script_run_bench: Any, file_path: str, text: str, expected: bool
    ) -> None:
        """A real_issue file counts only via a full path or a path-with-parent form.

        Scenario: the real-issue file ``a/b/logger_connector.py`` is credited for ``b/logger_connector`` or its full
        relative path, not for the bare stem.
        """
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
        ("text", "expected"),
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
    def test_detects_index_and_binary_access(self, script_run_bench: Any, text: str, expected: bool) -> None:
        """Full-string index/binary access is detected on POSIX and Windows paths; ordinary grep is not.

        Scenario: commands that read the cached index or run the scan-query binary by path (POSIX or Windows
        separators) are contaminating for the plain arm; an ordinary grep is not.
        """
        assert script_run_bench._is_contaminating_access(text) is expected

    @pytest.mark.parametrize(
        ("arm", "expected_hits"),
        [
            pytest.param("plain", 1, id="plain-arm-flagged"),
            pytest.param("codemap", 0, id="codemap-arm-not-flagged"),
        ],
    )
    def test_index_read_flags_contamination_only_for_plain_arm(
        self, script_run_bench: Any, tmp_path: Path, arm: str, expected_hits: int
    ) -> None:
        """A Read of .cache/codemap/*.json increments contamination_hits for the plain arm only.

        The codemap arm legitimately touches the index, so no contamination is recorded for it.
        """
        runner = self._runner(script_run_bench, tmp_path)
        result = script_run_bench.BenchRun(
            arm=arm, task_id="RI-01", task_type="real_issue", model="haiku", success=False
        )
        runner._handle(self._read_event(tmp_path), result, {}, 0.0)
        assert result.contamination_hits == expected_hits


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
        """Accept output containing every expected caller in canonical form.

        Scenario: the answer names both expected callers in ``module::Class.method`` form; recall is 1.0 and the
        result is correct.
        """
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
        """Reject output when fewer than 70% of expected callers are found.

        Scenario: only five of ten expected callers are named (recall 0.50), under the 70% bar.
        """
        callers = [f"lightning.pytorch.mod::Cls.method{i}" for i in range(10)]
        task = self._br_task(callers)
        # Mention only 5 of 10 → recall = 0.50 < 0.70
        output = " ".join(f"lightning.pytorch.mod::Cls.method{i}" for i in range(5))
        result = script_run_bench._evaluate_develop_br(task, output)
        assert result.correct is False
        assert result.recall is not None
        assert result.recall < 0.70

    def test_empty_callers_returns_unscored(self, script_run_bench: Any) -> None:
        """Leave the result unscored when ground_truth.fn_callers is an empty list.

        Scenario: the ground truth lists no callers, so nothing can be scored.
        """
        task = self._br_task([])
        result = script_run_bench._evaluate_develop_br(task, "anything")
        assert result.scored is False

    def test_recall_field_set_on_result(self, script_run_bench: Any) -> None:
        """Recall field is always populated (not None) on a scoreable task.

        Scenario: a scoreable task always reports a recall figure, even for a perfect answer.
        """
        callers = ["lightning.pytorch.trainer::Trainer.fit"]
        task = self._br_task(callers)
        result = script_run_bench._evaluate_develop_br(task, "lightning.pytorch.trainer::Trainer.fit")
        assert result.recall is not None

    def test_extraction_failed_when_no_caller_found(self, script_run_bench: Any) -> None:
        """Mark extraction as failed when output contains no recognizable caller token.

        Scenario: the answer names no recognisable caller; it is counted as an extraction failure.
        """
        callers = ["lightning.pytorch.trainer::Trainer.fit"]
        task = self._br_task(callers)
        result = script_run_bench._evaluate_develop_br(task, "completely unrelated prose output")
        assert result.extraction_failed is True

    def test_caller_count_gt_set(self, script_run_bench: Any) -> None:
        """caller_count_gt is populated from ground_truth.unique_caller_count.

        Scenario: even an empty answer records the ground-truth unique caller count for the report.
        """
        callers = ["lightning.pytorch.mod::Cls.fn"]
        task = self._br_task(callers)
        result = script_run_bench._evaluate_develop_br(task, "")
        assert result.caller_count_gt == 1

    @pytest.mark.parametrize(
        ("callers", "output", "expected_recall"),
        [
            pytest.param(
                ["lightning.pytorch.trainer.trainer::Trainer.fit"],
                "lightning.pytorch.trainer.trainer.Trainer.fit was found",
                1.0,
                id="dotted-form-matched-as-fallback",
            ),
            pytest.param(
                ["lightning.pytorch.loops.fit_loop::_FitLoop.advance"],
                "lightning.pytorch.loops.fit_loop::FitLoop.advance",
                1.0,
                id="fuzzy-underscore-prefix-same-module",
            ),
            pytest.param(
                ["lightning.pytorch.loops.evaluation_loop::_EvaluationLoop.advance"],
                "loops.evaluation_loop::EvaluationLoop.advance",
                1.0,
                id="abbreviated-module-and-dropped-underscore",
            ),
            pytest.param(
                ["lightning.pytorch.loops.evaluation_loop::_EvaluationLoop._evaluation_step"],
                "The caller is _EvaluationLoop._evaluation_step here.",
                1.0,
                id="bare-distinctive-tail-credits-via-form-11",
            ),
            pytest.param(
                ["lightning.pytorch.loops.evaluation_loop::_EvaluationLoop._evaluation_step"],
                "lightning.pytorch.other.mod::EvaluationLoop._evaluation_step",
                0.0,
                id="same-tail-in-different-module-rejected",
            ),
            pytest.param(
                ["lightning.pytorch.trainer.trainer::Trainer.fit"],
                "lightning.pytorch.wrong.module::WrongClass.wrong_method",
                0.0,
                id="plausible-but-wrong-qname-rejected",
            ),
            pytest.param(
                ["lightning.pytorch.trainer.trainer::Trainer.setup"],
                "The relevant caller looks like Trainer.setup based on my search.",
                0.0,
                id="bare-common-tail-too-weak-to-credit",
            ),
        ],
    )
    def test_caller_form_variants_credit_or_reject(
        self, script_run_bench: Any, callers: list[str], output: str, expected_recall: float
    ) -> None:
        """Recall credits a caller answered in a tolerated form and nothing for a wrong or too-weak one.

        Dotted form (no ``::``), a dropped underscore prefix in the same module, an abbreviated module suffix,
        and a distinctive bare ``Class.method`` tail credit the GT caller. A same tail in a different module, a
        fully-qualified but wrong name, and a bare common method tail (``setup``) must score nothing.
        """
        task = self._br_task(callers)
        result = script_run_bench._evaluate_develop_br(task, output)
        assert result.recall == pytest.approx(expected_recall)

    def test_md_pointer_does_not_read_file(self, script_run_bench: Any, tmp_path: Path) -> None:
        """A `→ foo.md` pointer is treated as inline text; the file is never read.

        Scenario: the answer only points at a Markdown file that holds the callers; the pointer is plain text and
        the file is not opened, so recall stays 0.
        """
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
        ("gt_module", "found_module", "expected"),
        [
            pytest.param("x.mod", "x.mod", True, id="equal"),
            pytest.param("lightning.pytorch.loops.evaluation_loop", "loops.evaluation_loop", True, id="found-suffix"),
            pytest.param("loops.evaluation_loop", "lightning.pytorch.loops.evaluation_loop", True, id="gt-suffix"),
            pytest.param("a.right", "a.wrong", False, id="sibling-mismatch"),
            pytest.param("lightning.pytorch.trainer", "lightning.pytorch.other", False, id="different-leaf"),
        ],
    )
    def test_equal_or_suffix_modules_are_compatible(
        self, script_run_bench: Any, gt_module: str, found_module: str, expected: bool
    ) -> None:
        """Equal or dotted-suffix modules are compatible; genuinely different modules are not.

        Scenario: identical modules, a suffix of the other in either direction, sibling modules, and a different
        leaf; only the first three are compatible.
        """
        assert script_run_bench._module_compatible(gt_module, found_module) is expected


# ===========================================================================
# _max_turns_for_task
# ===========================================================================


class TestMaxTurnsForTask:
    """Per-task turn cap depends only on the task, so both arms receive the identical value."""

    @pytest.mark.parametrize(
        ("task", "expected"),
        [
            pytest.param({"type": "symbol_extraction"}, 40, id="non-caller-task-flat-floor"),
            pytest.param(
                {"type": "develop_blast_radius", "ground_truth": {"unique_caller_count": 30}},
                120,
                id="scales-4x-callers",
            ),
            pytest.param({"type": "fn_call_graph", "ground_truth": {"unique_caller_count": 5}}, 80, id="fn-hits-floor"),
            pytest.param(
                {"type": "develop_blast_radius", "ground_truth": {"unique_caller_count": 0}},
                80,
                id="zero-callers-floor",
            ),
            pytest.param(
                {"type": "develop_blast_radius", "ground_truth": {"unique_caller_count": 37}},
                148,
                id="odd-count-scales",
            ),
        ],
    )
    def test_cap_is_flat_for_non_caller_tasks_and_scales_with_callers(
        self, script_run_bench: Any, task: dict, expected: int
    ) -> None:
        """Non-caller tasks get a flat 40-turn floor; caller tasks scale 4x with unique_caller_count, floored at 80.

        Scenario: the function takes no arm argument, so one call is the cap for plain AND codemap alike.
        """
        assert script_run_bench._max_turns_for_task(task) == expected


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
        ("quality_fields", "incomplete", "expected"),
        [
            pytest.param(None, False, False, id="no-run"),
            pytest.param({"scored": True, "correct": True}, False, True, id="extracted"),
            pytest.param({"scored": True, "extraction_failed": True}, False, False, id="extraction-failed"),
            pytest.param({"scored": False}, False, False, id="unscored"),
            pytest.param({"scored": True}, True, False, id="incomplete-turn-budget-exhausted"),
        ],
    )
    def test_extracted_only_for_scored_completed_runs(
        self, script_run_bench: Any, quality_fields: dict | None, incomplete: bool, expected: bool
    ) -> None:
        """_arm_extracted is True only for a scored, extracted, completed run.

        Scenario: a missing run, an unscored run, an extraction failure, and a turn-budget-exhausted (incomplete) run
        all fail to count as extracted.
        """
        run = (
            None
            if quality_fields is None
            else _make_run(
                script_run_bench, quality=script_run_bench.BenchQuality(**quality_fields), incomplete=incomplete
            )
        )
        assert script_run_bench._arm_extracted(run) is expected

    def test_paired_uses_only_both_extracted_tasks(self, script_run_bench: Any) -> None:
        """Paired-n and per-arm correct counts include only tasks where BOTH arms extracted.

        Scenario: four tasks where one has a failed extraction on one arm and one ran on one arm only; only the two
        fully extracted tasks are paired and counted.
        """
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
        """Return None when no task has both arms extracted.

        Scenario: the only task has an extraction failure on the plain arm, so no pair exists and the view is None.
        """
        runs = [
            self._run(script_run_bench, "plain", "A", scored=True, extraction_failed=True),
            self._run(script_run_bench, "codemap", "A", **self._both_scored(correct=True)),
        ]
        assert script_run_bench._paired_accuracy(runs) is None


# ===========================================================================
# _evaluate_diff_impact
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
        """DI quality rewards complete scoped answers but penalizes extra listed modules.

        Scenario: the answer lists both expected callers and tests plus one wrong module in each section, with
        exploration prose naming another wrong module first; exploration is ignored and the extras lower precision.
        """
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
        """Only explicit matching H2 sections can contribute to the two DI components.

        Scenario: caller and test modules are all listed under a Tests heading; callers get no credit from the wrong
        section, tests get full credit.
        """
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
        """Missing sections and wrong modules retain partial fitness but fail transparent correctness.

        Scenario: the Tests section is absent, or lists a module that is not expected; fitness stays partial but the
        binary verdict is incorrect.
        """
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


# ===========================================================================
# _extract_diff
# ===========================================================================


class TestExtractDiff:
    """Unified-diff extractor from agent output text."""

    _SIMPLE_DIFF = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"

    def test_valid_diff_extracted(self, script_run_bench: Any) -> None:
        """A well-formed unified diff is returned verbatim.

        Scenario: the model wraps a unified diff in prose; the diff headers and hunk are returned.
        """
        text = f"here is the fix\n{self._SIMPLE_DIFF}"
        result = script_run_bench._extract_diff(text)
        assert result is not None
        assert "--- a/x.py" in result
        assert "+++ b/x.py" in result

    def test_trailing_fence_stripped(self, script_run_bench: Any) -> None:
        """Trailing markdown code fence is stripped from the extracted diff.

        Scenario: the model closes a fenced code block right after the diff; the fence must not leak into the patch.
        """
        text = "Here:\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n```"
        result = script_run_bench._extract_diff(text)
        assert result is not None
        assert "```" not in result

    def test_diff_ends_with_newline(self, script_run_bench: Any) -> None:
        """Extracted diff always ends with a newline character.

        Scenario: the diff's last line lacks a newline; the patch handed to git still ends with one.
        """
        text = f"prefix\n{self._SIMPLE_DIFF.rstrip()}"
        result = script_run_bench._extract_diff(text)
        assert result is not None
        assert result.endswith("\n")

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("no diff here", id="no-diff-headers"),
            pytest.param("", id="empty-string"),
            pytest.param("--- a/x.py\njust a comment", id="minus-header-without-plus-header"),
        ],
    )
    def test_returns_none_without_complete_diff(self, script_run_bench: Any, text: str) -> None:
        """None is returned for text with no unified diff headers, empty input, or only a ``---`` header.

        Scenario: the answer has no diff, is empty, or has only a ``---`` header; no patch can be built.
        """
        assert script_run_bench._extract_diff(text) is None


# ===========================================================================
# _workflow_type_of
# ===========================================================================


class TestWorkflowTypeOf:
    """Workflow key accessor with task_type fallback."""

    @pytest.mark.parametrize(
        ("workflow_type", "task_type", "expected"),
        [
            pytest.param("query", "symbol_extraction", "query", id="query"),
            pytest.param("", "symbol_extraction", "symbol_extraction", id="empty-falls-back-to-symbol-extraction"),
            pytest.param("", "fn_call_graph", "fn_call_graph", id="empty"),
            pytest.param("debug", "debug_from_trace", "debug", id="debug"),
        ],
    )
    def test_prefers_workflow_type_over_task_type(
        self, script_run_bench: Any, workflow_type: str, task_type: str, expected: str
    ) -> None:
        """workflow_type wins when non-empty; task_type is the fallback.

        Scenario: a run with a workflow type reports it; a run without one reports its task type instead.
        """
        run = _make_run(script_run_bench, workflow_type=workflow_type, task_type=task_type)
        assert script_run_bench._workflow_type_of(run) == expected


# ===========================================================================
# _build_system_prompt
# ===========================================================================


class TestBuildSystemPrompt:
    """System-prompt assembly must keep every non-tool sentence identical across arms."""

    _EFFICIENCY = "Answer in as few tool calls as possible; do not re-verify results you already have."
    _OUTPUT_HDR = "For symbol location tasks: report exactly in this format:"

    @staticmethod
    def _prompt(script_bench: Any, arm: str, index: str = "/idx.json") -> str:
        """Build the ``arm`` system prompt with fixed framing arguments."""
        return script_bench._build_system_prompt(arm, "demo", "/repo", index)

    @pytest.mark.parametrize("arm", ["plain", "codemap"])
    def test_efficiency_sentence_present_in_both_arms(self, script_run_bench: Any, arm: str) -> None:
        """The single efficiency instruction appears verbatim in both arms.

        Scenario: each arm's prompt is built with the same framing arguments and contains the single efficiency
        sentence.
        """
        assert self._EFFICIENCY in self._prompt(script_run_bench, arm)

    def test_output_format_block_identical_across_arms(self, script_run_bench: Any) -> None:
        """The output-format requirements block is byte-identical in both arms.

        Scenario: both arms are built; from the output-format header onward the two prompts are the same bytes, so
        only the tool section differs.
        """
        plain = self._prompt(script_run_bench, "plain")
        codemap = self._prompt(script_run_bench, "codemap")
        marker = self._OUTPUT_HDR
        plain_block = plain[plain.index(marker) :]
        codemap_block = codemap[codemap.index(marker) :]
        assert plain_block == codemap_block

    def test_plain_forbids_scan_query(self, script_run_bench: Any) -> None:
        """The plain arm prompt forbids scan-query and the Skill tool.

        Scenario: the plain arm must be steered away from the benchmarked tool and from skills.
        """
        plain = self._prompt(script_run_bench, "plain")
        assert "Do NOT use scan-query" in plain
        assert "Do NOT use the Skill tool" in plain

    def test_codemap_documents_subcommands(self, script_run_bench: Any) -> None:
        """Codemap arm documents scan-query syntax and the subcommand reference list.

        Scenario: the codemap arm needs the exact invocation shape and the subcommand reference to use the tool.
        """
        codemap = self._prompt(script_run_bench, "codemap")
        assert "scan-query --index /idx.json <subcommand>" in codemap
        assert "fn-rdeps" in codemap
        assert "coupled" in codemap

    def test_index_path_substituted_into_codemap(self, script_run_bench: Any) -> None:
        """The concrete index path is interpolated into the codemap tool section.

        Scenario: a custom index path passed in appears in the prompt's tool section.
        """
        assert "/custom/index.json" in self._prompt(script_run_bench, "codemap", "/custom/index.json")

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
        """Efficiency-steering / per-task strategy coaching is gone from the codemap prompt.

        Scenario: each coaching phrase from earlier prompt revisions stays out, so the arms differ only in tool
        availability.
        """
        assert removed not in self._prompt(script_run_bench, "codemap")

    def test_count_semantics_state_unique_callers(self, script_run_bench: Any) -> None:
        """Any retained fn-rdeps count note states unique callers, not call-site edges.

        Scenario: the fn-rdeps note describes counts as unique callers and does not mention call-site edges.
        """
        codemap = self._prompt(script_run_bench, "codemap")
        assert "call-site EDGES" not in codemap
        assert "`count` = unique callers" in codemap


# ===========================================================================
# _effective_recall
# ===========================================================================


class TestEffectiveRecall:
    """Recall accessor: true recall when set, else binary correctness in [0, 1]."""

    @pytest.mark.parametrize(
        ("quality_fields", "expected_recall"),
        [
            pytest.param({"scored": True, "recall": 0.85}, 0.85, id="recall-field-returned-directly"),
            pytest.param(
                {"scored": True, "correct": True, "metric_got": 100, "metric_expected": 98},
                1.0,
                id="correct-line-task-scores-one",
            ),
            pytest.param(
                {
                    "scored": True,
                    "correct": False,
                    "extraction_failed": False,
                    "metric_got": 1116,
                    "metric_expected": 1024,
                },
                0.0,
                id="wrong-but-parsed-line-scores-zero-not-ratio",
            ),
        ],
    )
    def test_returns_recall_or_binary_correctness(
        self, script_run_bench: Any, quality_fields: dict, expected_recall: float
    ) -> None:
        """Recall is returned as-is when set; line/count evaluators (no recall field) score 1.0 correct, 0.0 wrong.

        A parsed-but-wrong line number (got much greater than expected) is a real miss, never a ratio above 1.0.
        """
        run = _make_run(script_run_bench)
        run.quality = script_run_bench.BenchQuality(**quality_fields)
        assert script_run_bench._effective_recall(run) == pytest.approx(expected_recall)

    @pytest.mark.parametrize(
        "quality_fields",
        [
            pytest.param({"scored": False}, id="unscored-run"),
            pytest.param({"scored": True, "correct": False, "extraction_failed": True}, id="extraction-failed"),
        ],
    )
    def test_returns_none_when_no_recall_is_defined(self, script_run_bench: Any, quality_fields: dict) -> None:
        """An unscored run, or a parse failure, yields None (excluded from recall) — never a 0.0 miss.

        Scenario: an unscored run, or a scored run whose parse failed, has no recall to average.
        """
        run = _make_run(script_run_bench)
        run.quality = script_run_bench.BenchQuality(**quality_fields)
        assert script_run_bench._effective_recall(run) is None

    def test_returns_none_when_none_run(self, script_run_bench: Any) -> None:
        """Return None for a None run argument.

        Scenario: the caller has no run for this arm at all.
        """
        assert script_run_bench._effective_recall(None) is None


# ---------------------------------------------------------------------------
# Evaluator identity
# ---------------------------------------------------------------------------


class TestEvaluatorIdentity:
    """Every evaluator stamps its own function name into the result it returns."""

    @pytest.mark.parametrize(
        ("evaluator", "task", "output"),
        [
            pytest.param("_evaluate_symbol", _se_task(), "start_line: 100", id="symbol"),
            pytest.param(
                "_evaluate_rv",
                {
                    "id": "RV-01",
                    "type": "review_assistance",
                    "sub_questions": [{"id": "q1", "match": "integer_extract", "ground_truth": {"count": 5}}],
                },
                "found 5 undocumented",
                id="review",
            ),
            pytest.param(
                "_evaluate_oss",
                {"id": "CQ-01", "type": "code_quality", "ground_truth": {"check": "coupled", "top_dep_count": 10}},
                "dep_count: 10",
                id="code-quality",
            ),
            pytest.param(
                "_evaluate_debug",
                {
                    "id": "DG-01",
                    "type": "debug_from_trace",
                    "ground_truth": {"function": "my_function", "file": "src/mod/utils.py", "start_line": 10},
                },
                "my_function utils",
                id="debug",
            ),
            pytest.param(
                "_evaluate_feature",
                {
                    "id": "FT-01",
                    "type": "feature_scaffolding",
                    "ground_truth": {
                        "entry_point": "Trainer.validate",
                        "primary_file": "src/lightning/trainer/trainer.py",
                    },
                },
                "validate inside trainer.py",
                id="feature",
            ),
            pytest.param(
                "_evaluate_real_issue",
                {"id": "RI-01", "type": "real_issue", "ground_truth": {"files_changed": ["src/trainer.py"]}},
                "trainer.py",
                id="real-issue",
            ),
            pytest.param(
                "_evaluate_develop_br",
                {
                    "id": "BR-01",
                    "type": "develop_blast_radius",
                    "ground_truth": {"fn_callers": ["lightning.pytorch.mod::Cls.fn"], "unique_caller_count": 1},
                },
                "lightning.pytorch.mod::Cls.fn",
                id="blast-radius",
            ),
        ],
    )
    def test_result_records_the_evaluator_name(
        self, script_run_bench: Any, evaluator: str, task: dict, output: str
    ) -> None:
        """evaluator_used on the returned quality names the evaluator function that produced it.

        Scenario: each task family is scored with a minimal matching answer; the result must identify its evaluator
        so report rows can attribute scores correctly.
        """
        result = getattr(script_run_bench, evaluator)(task, output)
        assert result.evaluator_used == evaluator


# ---------------------------------------------------------------------------
# Relocated-index admission
# ---------------------------------------------------------------------------


class TestRelocatedIndexAdmission:
    """A worktree run proves index identity through relocation provenance, never byte identity."""

    def test_valid_provenance_admits_a_worktree_index(
        self,
        script_run_bench: Any,
        monkeypatch: pytest.MonkeyPatch,
        relocated_index: tuple[Path, Path, dict[str, Any], dict[str, str]],
    ) -> None:
        """Relocation provenance admits an index whose bytes differ from the locked hash.

        Scenario: a run executes in its own worktree, so its index carries the worktree's
        ``scan_root`` and can never reproduce the manifest's locked raw hash; the provenance written
        by the relocation is what proves the graph is still the frozen one.
        """
        repo, index_path, manifest, relocation = relocated_index
        monkeypatch.setattr(bench_tasks, "_PARITY_MANIFEST", manifest)

        script_run_bench._validate_primary_runtime(repo, index_path, relocation)

        assert manifest["index"]["raw_sha256"] == relocation["frozen_index_sha256"]

    @pytest.mark.parametrize(
        ("field", "digest", "message"),
        [
            pytest.param("frozen_index_sha256", "0" * 64, "wrong frozen source", id="wrong-frozen-source"),
            pytest.param("derived_index_sha256", "1" * 64, "changed after relocation", id="bytes-on-disk-disagree"),
        ],
    )
    def test_inconsistent_provenance_is_rejected(
        self,
        script_run_bench: Any,
        monkeypatch: pytest.MonkeyPatch,
        relocated_index: tuple[Path, Path, dict[str, Any], dict[str, str]],
        field: str,
        digest: str,
        message: str,
    ) -> None:
        """Provenance whose frozen source is not the locked index, or whose derived hash misses the disk, is rejected.

        Scenarios: a caller supplies provenance derived from some other frozen index, so admitting it would
        let an unrelated graph enter the run under the locked manifest's authority; or the relocated copy
        changed after its provenance was written, so the digest the run would attest to is no longer the index
        the model actually reads.
        """
        repo, index_path, manifest, relocation = relocated_index
        monkeypatch.setattr(bench_tasks, "_PARITY_MANIFEST", manifest)
        relocation[field] = digest

        with pytest.raises(ValueError, match=message):
            script_run_bench._validate_primary_runtime(repo, index_path, relocation)

    def test_absent_provenance_keeps_the_byte_gate(
        self,
        script_run_bench: Any,
        monkeypatch: pytest.MonkeyPatch,
        relocated_index: tuple[Path, Path, dict[str, Any], dict[str, str]],
    ) -> None:
        """Without provenance a non-locked index is still rejected on its bytes.

        Scenario: the same relocated index is offered by a run that claims no relocation; the
        original byte-identity gate must reject it exactly as before.
        """
        repo, index_path, manifest, _relocation = relocated_index
        monkeypatch.setattr(bench_tasks, "_PARITY_MANIFEST", manifest)

        with pytest.raises(ValueError, match="canonical run requires the locked index bytes"):
            script_run_bench._validate_primary_runtime(repo, index_path)
