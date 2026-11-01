"""No-model contract tests for the Codex read-crop adapter."""

from __future__ import annotations

import hashlib
import inspect
import io
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, NamedTuple
from unittest import mock

import pytest

BENCHMARKS = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BENCHMARKS))

SOURCE = "def method(self, value: int) -> None:\n    pass\n"
ANSWER = (
    "BEGIN_READ_CROP_JSON\n"
    '{"signature":"Example.method(self, value: int) -> None","parameters":["self","value"],'
    '"behavior":"Records the supplied value."}\nEND_READ_CROP_JSON'
)
TURN_COMPLETED = {"type": "turn.completed", "status": "completed"}
PROGRESS_ROW = "(1/3) ✓  RC-01  C_strict"
CURRENT_SCOPE = "b" * 64
ALL_ARMS_MISSING = {"A_plain": False, "B_auto": False, "C_strict": False}
ALL_ARMS_OBSERVED = {"A_plain": False, "B_auto": True, "C_strict": True}


def _load() -> Any:
    """Import the private read-crop stage without entering a command-line or provider workflow.

    >>> _load().__name__
    '_bench_codex.stage_readcrop'
    """
    from _bench_codex import stage_readcrop

    return stage_readcrop


def _agent_message(text: str) -> dict[str, Any]:
    """Build the completed agent-message event that carries a final answer."""
    return {"type": "item.completed", "item": {"type": "agent_message", "text": text}}


def _command(item_id: str, command: str, output: str) -> dict[str, Any]:
    """Build the completed command-execution event for one successful shell call."""
    item = {
        "type": "command_execution",
        "id": item_id,
        "command": command,
        "aggregated_output": output,
        "status": "completed",
        "exit_code": 0,
    }
    return {"type": "item.completed", "item": item}


def _stream(*events: dict[str, Any]) -> str:
    """Serialize events as the newline-delimited JSON stream Codex emits."""
    return "\n".join(json.dumps(event) for event in events)


def _planned(task_id: str, **fields: str) -> dict[str, Any]:
    """Build a planned read-crop task whose contract carries only the given fields."""
    return {"contract": SimpleNamespace(task_id=task_id, **fields)}


def _scoped(task_id: str) -> list[dict[str, Any]]:
    """Build one planned task with the digests scope resolution binds."""
    return [_planned(task_id, oracle_sha256="a" * 64, source_sha256="b" * 64)]


def _result_row(arm: str = "C_strict", **overrides: Any) -> dict[str, Any]:
    """Build a successful persisted result row for the given arm, with optional field overrides."""
    row = {
        "success": True,
        "task_id": "RC-01",
        "arm": arm,
        "input_tokens": 100,
        "output_tokens": 20,
        "command_calls": 1,
        "elapsed_s": 2.5,
        "primary_correct": True,
        "quality_score": 1.0,
        "codemap_used": arm != "A_plain",
        "compliance": True,
    }
    return row | overrides


def _unrelated_query_parse() -> SimpleNamespace:
    """Build a parsed Codex run whose only successful Skill query targeted a different symbol."""
    return SimpleNamespace(
        output_text=ANSWER,
        success=True,
        skill_delivery_observed=True,
        codemap_skill_compact_successful_calls=1,
        successful_query_arguments=[["symbol", "Other.method"]],
        codemap_calls=1,
        codemap_observed_calls=1,
        input_tokens=10,
        cached_input_tokens=0,
        output_tokens=1,
        reasoning_output_tokens=0,
        tool_result_tokens=None,
        command_calls=1,
        tool_elapsed_s=0.1,
        malformed_usage=0,
        raw_events=[],
    )


@pytest.fixture
def runner() -> Any:
    """Return the read-crop stage module under test."""
    return _load()


@pytest.fixture
def readcrop_task() -> dict[str, Any]:
    """Minimal read-crop task describing ``Example.method``."""
    return {
        "id": "RC-fixture",
        "type": "read_crop",
        "prompt": "Describe Example.method.",
        "primary_module": "package.module",
        "symbol": "Example.method",
        "expected_keywords": ["value"],
    }


@pytest.fixture
def contract(runner: Any, readcrop_task: dict[str, Any]) -> Any:
    """Read-crop contract built from the fixture task and its one-parameter source."""
    return runner.build_readcrop_contract(readcrop_task, source=SOURCE)


@pytest.fixture
def answer_stream() -> str:
    """Codex event stream holding one well-formed answer and a completed turn, with no tool calls."""
    return _stream(_agent_message(ANSWER), TURN_COMPLETED)


@pytest.fixture
def codex_runner_double(runner: Any, monkeypatch: pytest.MonkeyPatch) -> type:
    """Replace the structural Codex adapter with a recording double and return its class.

    The class carries ``init_kwargs``, ``probed`` and ``snapshot_kwargs`` so a test can read what the stage handed the
    adapter. Snapshot creation raises ``KeyboardInterrupt`` so a paid run stops before any provider subprocess starts.
    """

    class Adapter:
        """Capture construction, probes and snapshot arguments without creating real homes."""

        init_kwargs: ClassVar[dict[str, Any]] = {}
        probed: ClassVar[list[str]] = []
        snapshot_kwargs: ClassVar[dict[str, Any]] = {}

        def __init__(self, *_args: Any, **kwargs: Any) -> None:
            """Record the keyword arguments the stage built the adapter with."""
            type(self).init_kwargs.update(kwargs)

        def probe_arm(self, arm: str) -> None:
            """Record the treatment passed to the preflight probe."""
            type(self).probed.append(arm)

        def create_input_snapshot(self, _run_dir: Path, **kwargs: Any) -> None:
            """Record snapshot arguments and interrupt before provider execution."""
            type(self).snapshot_kwargs.update(kwargs)
            raise KeyboardInterrupt

        def close(self) -> None:
            """Accept teardown without releasing anything."""
            return None

    monkeypatch.setattr(runner._structural(), "CodexRunner", Adapter)
    return Adapter


class TestReadcropPrompt:
    """The prompt each treatment arm receives for a read-crop task."""

    def test_keeps_answer_contract_shared_and_arm_availability_separate(self, runner: Any) -> None:
        """Arm supplements cannot alter the strict source-answer schema.

        Every arm must be asked for the same BEGIN/END answer envelope, while only the availability paragraph differs:
        the plain arm learns Codemap is absent, the others learn how to reach it. A supplement that changed the schema
        would make arms incomparable.
        """
        task = {"prompt": "Describe Example.method.", "symbol": "Example.method"}

        plain = runner.readcrop_prompt("A_plain", task)
        strict = runner.readcrop_prompt("C_strict", task)

        assert "BEGIN_READ_CROP_JSON" in plain
        assert "BEGIN_READ_CROP_JSON" in strict
        assert "Codemap is absent" in plain
        assert 'cat "$CODEMAP_SKILL_FILE"' not in strict
        assert "installed $codemap-py:query-code Skill is available" in strict
        assert '"$CODEMAP_BIN" query --compact symbol Example.method' in strict
        assert "frozen static source-graph query tool" in strict
        assert "frozen static source-graph query tool" in runner.readcrop_prompt("B_auto", task)
        assert '"behavior":"non-empty contract summary"' in strict
        assert "every exact source parameter name" in strict

    def test_asks_for_each_named_required_parameter(self, runner: Any) -> None:
        """A task that names required parameters changes the prompt from every parameter to each named one.

        Focused tasks list the parameters the answer must contain. The prompt has to switch wording so the model is not
        asked for every source parameter when only the named ones are scored.
        """
        task = {"prompt": "Describe Example.method.", "symbol": "Example.method", "required_parameters": ["value"]}

        prompt = runner.readcrop_prompt("A_plain", task)

        assert "each exact required source parameter name" in prompt

    def test_rejects_an_arm_without_an_availability_supplement(
        self, runner: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An arm with no availability supplement is refused with ``ValueError`` and not a bare assert.

        The guard must survive ``python -O``, which strips an ``assert``; an arm added to the tuple without a supplement
        would otherwise be asked a prompt that never states whether Codemap exists.
        """
        monkeypatch.setattr(runner, "ARMS", (*runner.ARMS, "D_phantom"))

        with pytest.raises(ValueError, match="no availability supplement"):
            runner.readcrop_prompt("D_phantom", {"prompt": "Describe Example.method.", "symbol": "Example.method"})


class TestParseReadcropStream:
    """Scoring one Codex event stream into a persisted result row."""

    def test_preserves_answer_and_redacted_event_evidence(self, runner: Any, contract: Any, answer_stream: str) -> None:
        """A failed calibration remains diagnosable without a second paid call.

        The row keeps the verbatim answer, the retained raw events and their SHA-256, so a reviewer can re-check a
        scored cell offline. The behaviour facts stay unscored because the fixture task names none.
        """
        row = runner.parse_readcrop_stream(answer_stream, arm="A_plain", contract=contract, skill_path=None)

        assert row["success"] is True
        assert row["behavior_fact_recall"] is None
        assert row["behavior_facts_correct"] is None
        assert row["compliance"] is True
        assert row["output_text"] == ANSWER
        assert row["raw_events"]
        assert len(row["raw_events_sha256"]) == 64

    def test_auto_arm_answer_without_codemap_use_is_compliant(
        self, runner: Any, contract: Any, answer_stream: str
    ) -> None:
        """The optional-use arm stays compliant when its answer never touches Codemap.

        B_auto only makes Codemap available, so an answer produced from source reading alone must not be flagged as a
        treatment violation.
        """
        row = runner.parse_readcrop_stream(answer_stream, arm="B_auto", contract=contract, skill_path=None)

        assert row["compliance"] is True

    def test_unscoreable_answer_leaves_every_recall_field_none(self, runner: Any, contract: Any) -> None:
        """One failure cannot be averaged into two means and omitted from two.

        A reply with no answer envelope has no score at all, so every recall and quality field must be ``None`` rather
        than zero; a zero would pull the cell into some means while others skip it.
        """
        stream = _stream(_agent_message("no envelope here"), TURN_COMPLETED)

        row = runner.parse_readcrop_stream(stream, arm="A_plain", contract=contract, skill_path=None)

        assert row["answer_error"] == "missing strict read-crop answer envelope"
        assert row["parameter_recall"] is None
        assert row["behavior_fact_recall"] is None
        assert row["behavior_facts_correct"] is None
        assert row["keyword_recall_diagnostic"] is None
        assert row["quality_score"] is None
        assert row["success"] is False
        assert row["primary_correct"] is False

    @mock.patch("_bench_codex.runtime.parse_codex_jsonl", autospec=True)
    def test_strict_arm_requires_the_locked_symbol_query(
        self, mock_parse: mock.MagicMock, runner: Any, contract: Any, tmp_path: Path
    ) -> None:
        """A successful unrelated Skill query cannot satisfy strict treatment use.

        The strict arm must query the task's own symbol. Here the only successful query asked about ``Other.method``, so
        the cell is scored but flagged non-conformant instead of being credited as treatment use. The provider parse is
        replaced because the contract under test is the stage's conformance decision, not Codex JSON parsing.
        """
        mock_parse.return_value = _unrelated_query_parse()
        skill_path = tmp_path / "SKILL.md"
        skill_path.write_text("fixture\n", encoding="utf-8")

        row = runner.parse_readcrop_stream("ignored", arm="C_strict", contract=contract, skill_path=skill_path)

        assert row["success"] is True
        assert row["strict_query_conformance"] is False
        assert row["compliance"] is True
        assert row["fresh_input_tokens"] == 10
        assert row["token_accounting_inconsistent"] is False
        assert row["command_calls"] == 1

    @mock.patch("_bench_codex.runtime.parse_codex_jsonl", autospec=True)
    def test_plain_arm_that_used_codemap_is_contaminated(
        self, mock_parse: mock.MagicMock, runner: Any, contract: Any
    ) -> None:
        """A plain-arm run that nevertheless made Codemap calls is contaminated and fails compliance.

        The plain arm has no Codemap, so observed calls mean the isolation leaked; counting that cell as a success would
        credit the control arm with the treatment's tool.
        """
        mock_parse.return_value = _unrelated_query_parse()

        row = runner.parse_readcrop_stream("ignored", arm="A_plain", contract=contract, skill_path=None)

        assert row["compliance"] is False
        assert row["contaminated"] is True
        assert row["success"] is False

    def test_strict_arm_credits_the_verified_skill_file_and_locked_query(
        self, runner: Any, contract: Any, tmp_path: Path
    ) -> None:
        """Strict treatment accepts only the immutable Skill read followed by its exact query.

        The stream reads the Skill file verbatim and then runs the locked symbol query for the task's own symbol, which
        is the one sequence that earns conformance.
        """
        skill_text = "---\nname: query-code\n---\n"
        skill_path = tmp_path / "SKILL.md"
        skill_path.write_text(skill_text, encoding="utf-8")
        stream = _stream(
            _command("skill-read", "/bin/zsh -lc 'cat \"$CODEMAP_SKILL_FILE\"'", skill_text),
            _command(
                "symbol-query",
                "/bin/zsh -lc '\"$CODEMAP_BIN\" query --compact symbol Example.method'",
                '{"index":{"query_complete":true,"compact":true}}',
            ),
            _agent_message(ANSWER),
            TURN_COMPLETED,
        )

        row = runner.parse_readcrop_stream(stream, arm="C_strict", contract=contract, skill_path=skill_path)

        assert row["success"] is True
        assert row["strict_query_conformance"] is True
        assert row["compliance"] is True


class TestFormatting:
    """The progress rows a paid run prints and persists."""

    def test_result_row_reports_progress_and_cell_state(self, runner: Any) -> None:
        """Paid runs expose persisted results instead of ending with a bare path.

        The row carries the position, tokens, command count, elapsed time and verdicts, so an operator watching a paid
        study sees each cell's outcome as it lands.
        """
        row = _result_row(input_tokens=101_400, output_tokens=2_020, command_calls=3, elapsed_s=62.5)

        assert runner.format_result_row(row, completed=2, total=6) == (
            "(2/6) ✓  RC-01  C_strict  in=101.4k out= 2.0k cmd= 3 time= 1m2s quality=1.000 correct=✓ codemap=✓"
        )

    @pytest.mark.parametrize(
        ("arm", "ansi_code"),
        [
            pytest.param("A_plain", "33", id="a_plain"),
            pytest.param("B_auto", "36", id="b_auto"),
            pytest.param("C_strict", "35", id="c_strict"),
        ],
    )
    def test_rows_use_the_shared_arm_palette(self, runner: Any, arm: str, ansi_code: str) -> None:
        """ReadCrop labels retain the structural A/B/C palette when ANSI is forced.

        Read-crop rows are drawn by the structural renderer, so each arm must come out in the colour that arm has
        everywhere else, and the codemap column must follow whether the arm used Codemap.
        """
        formatted = runner.format_result_row(_result_row(arm), completed=1, total=3)
        output = io.StringIO()

        runner.runtime.render_result_rows([f"{formatted}\n"], output, force_color=True)

        assert output.getvalue() == f"\x1b[{ansi_code}m{formatted}\x1b[0m\n"
        assert formatted.endswith(f"codemap={'✓' if arm != 'A_plain' else '✗'}")
        assert "compliance=" not in formatted


class TestEmitProgress:
    """Where a progress line goes: the stage's own run log and the terminal."""

    @mock.patch("_bench_codex.runtime.print_arm_row", autospec=True)
    def test_delegates_paid_rows_to_the_shared_arm_renderer(
        self, mock_print_arm_row: mock.MagicMock, runner: Any, tmp_path: Path
    ) -> None:
        """ReadCrop rows keep the structural renderer's interactive arm colors.

        The terminal side is handed to the shared renderer with the arm so colours match the other stages, while the run
        log still receives the plain line. The renderer is replaced since drawing it is covered elsewhere.
        """
        runner.emit_progress(tmp_path / "run.log", PROGRESS_ROW, arm="C_strict")

        mock_print_arm_row.assert_called_once_with(PROGRESS_ROW, "C_strict")
        assert (tmp_path / "run.log").read_text(encoding="utf-8") == f"{PROGRESS_ROW}\n"

    def test_keeps_native_log_while_terminal_uses_aggregate_counter(
        self, runner: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Unified presentation cannot mutate ReadCrop's stage-native run log.

        Inside a multi-stage study the terminal shows the aggregate position ``(4/12)``, but the stage's own log keeps
        its local ``(1/3)`` so a log replayed on its own still reads correctly.
        """
        run_log = tmp_path / "run.log"

        with runner.runtime.progress_scope(completed_offset=3, total_cells=12):
            runner.emit_progress(run_log, PROGRESS_ROW, arm="C_strict")

        assert capsys.readouterr().out == "(4/12) ✓  RC-01  C_strict\n"
        assert run_log.read_text(encoding="utf-8") == f"{PROGRESS_ROW}\n"


class TestChecksums:
    """The artifact ledger a read-crop run retains."""

    @pytest.fixture
    def run_dir(self, tmp_path: Path) -> Path:
        """Create a run directory holding one top-level and one nested retained artifact."""
        (tmp_path / "telemetry.jsonl").write_text("{}\n", encoding="utf-8")
        nested = tmp_path / "inputs"
        nested.mkdir()
        (nested / "input-snapshot.json").write_text("{}\n", encoding="utf-8")
        return tmp_path

    def test_cover_the_retained_artifacts_and_verify_clean(self, runner: Any, run_dir: Path) -> None:
        """Completed or failed runs retain an independently checkable artifact ledger.

        The ledger lists top-level and nested artifacts but never itself, and an untouched run verifies against it, so a
        later reader can prove nothing was altered after the run finished.
        """
        runner.write_checksums(run_dir)

        entries = (run_dir / "checksums.sha256").read_text(encoding="utf-8")
        assert "telemetry.jsonl" in entries
        assert "inputs/input-snapshot.json" in entries
        assert "checksums.sha256" not in entries
        runner.verify_checksums(run_dir)

    def test_verification_rejects_a_changed_artifact(self, runner: Any, run_dir: Path) -> None:
        """Changing a retained artifact after the ledger was written fails verification.

        The ledger exists to catch exactly this edit, so a mismatch has to raise instead of passing quietly.
        """
        runner.write_checksums(run_dir)
        (run_dir / "telemetry.jsonl").write_text('{"changed": true}\n', encoding="utf-8")

        with pytest.raises(ValueError, match="checksum mismatch"):
            runner.verify_checksums(run_dir)


class TestResolveScope:
    """The scope digest a paid approval is bound to."""

    def test_changes_when_task_selection_changes(self, runner: Any) -> None:
        """A paid approval cannot silently expand from a selected calibration.

        Selecting a different task must move the scope digest, otherwise an approval for one cell would authorize
        another.
        """
        manifest = BENCHMARKS / "manifests" / "provider-parity-methodology.json"

        first = runner.resolve_scope(_scoped("RC-01"), manifest, runner.STRUCTURAL_PATH, "gpt-6.1-sol")
        second = runner.resolve_scope(_scoped("RC-02"), manifest, runner.STRUCTURAL_PATH, "gpt-6.1-sol")

        assert first["scope_sha256"] != second["scope_sha256"]

    def test_binds_the_stage_and_structural_runner_hashes(self, runner: Any) -> None:
        """The scope records the digests of the stage file and the structural runner it will execute.

        Approving a scope has to pin the code that will run; an edit to either runner afterwards must therefore
        invalidate it.
        """
        manifest = BENCHMARKS / "manifests" / "provider-parity-methodology.json"

        scope = runner.resolve_scope(_scoped("RC-01"), manifest, runner.STRUCTURAL_PATH, "gpt-6.1-sol")

        assert scope["stage_runner_sha256"] == hashlib.sha256(Path(runner.__file__).read_bytes()).hexdigest()
        assert scope["structural_runner_sha256"] == hashlib.sha256(runner.STRUCTURAL_PATH.read_bytes()).hexdigest()

    def test_changes_when_treatment_manifest_changes(self, runner: Any, tmp_path: Path) -> None:
        """A paid approval cannot survive a changed installed-treatment lock.

        The treatment manifest names the installed artifacts under test, so rewriting it must move both its own digest
        and the overall scope digest.
        """
        methodology = BENCHMARKS / "manifests" / "provider-parity-methodology.json"
        treatment_manifest = tmp_path / "codex-integration.json"
        treatment_manifest.write_text('{"artifact_sha256":{"codex_rig_adapter":"a"}}\n', encoding="utf-8")
        tasks = _scoped("RC-01")

        first = runner.resolve_scope(tasks, methodology, treatment_manifest, "gpt-6.1-sol")
        treatment_manifest.write_text('{"artifact_sha256":{"codex_rig_adapter":"b"}}\n', encoding="utf-8")
        second = runner.resolve_scope(tasks, methodology, treatment_manifest, "gpt-6.1-sol")

        assert first["treatment_manifest_sha256"] != second["treatment_manifest_sha256"]
        assert first["scope_sha256"] != second["scope_sha256"]


class TestStageEntry:
    """Importing the stage and the public switches of ``run_stage``."""

    def test_import_does_not_execute_the_structural_runner(self) -> None:
        """Importing a prompt helper must not run the 5000-line paid runner.

        This runs in a fresh interpreter because the check is about import-time side effects: any earlier import in
        the pytest process would already have loaded the structural module and hide a regression.
        """
        probe = (
            "import sys, json; sys.path.insert(0, '.');"
            "from _bench_codex import stage_readcrop;"
            "target = stage_readcrop.STRUCTURAL_PATH.resolve();"
            "loaded = [m for m in tuple(sys.modules.values())"
            " if getattr(m, '__file__', None) and __import__('pathlib').Path(m.__file__).resolve() == target];"
            "print(json.dumps({'loaded': bool(loaded), 'lazy': stage_readcrop._STRUCTURAL_MODULE is None}))"
        )

        completed = subprocess.run(
            [sys.executable, "-c", probe], cwd=BENCHMARKS, capture_output=True, text=True, check=True, timeout=120
        )

        assert json.loads(completed.stdout.strip().splitlines()[-1]) == {"loaded": False, "lazy": True}

    def test_execution_is_controlled_only_by_dry_run(self, runner: Any) -> None:
        """The unified launcher must not leak a public ``--paid`` switch into ReadCrop.

        Whether a model is called is decided by ``dry_run_requested`` and the approval digest alone; a second ``paid``
        parameter would give a second, unreviewed way to spend money.
        """
        parameters = inspect.signature(runner.run_stage).parameters

        assert "dry_run_requested" in parameters
        assert "paid" not in parameters

    @mock.patch("_bench_codex.stage_readcrop.run_paid", autospec=True)
    @mock.patch("_bench_codex.stage_readcrop._prepare_readcrop_scope", autospec=True)
    def test_scope_drift_reports_hashes_and_safe_recovery(
        self, mock_prepare: mock.MagicMock, mock_run_paid: mock.MagicMock, runner: Any, tmp_path: Path
    ) -> None:
        """A changed stage scope must stop before a model call and explain how to recover.

        The approval was granted for one scope digest and the inputs now resolve to another. The error has to name both
        digests, state that no model call was made, point at the dry run that regenerates the approval, and leave no run
        directory behind. Scope preparation and the paid run are replaced: the decision under test is the stage's
        comparison, not scope hashing or provider execution.
        """
        mock_prepare.return_value = ([_planned("RC-01")], {"scope_sha256": CURRENT_SCOPE})

        with pytest.raises(ValueError, match="scope inputs changed after aggregate approval") as error:
            runner.run_stage(
                repo_path=tmp_path / "repo",
                model="gpt-6.1-sol",
                tasks_selector="RC-01",
                dry_run_requested=False,
                resolve_scope_requested=False,
                auth_source=tmp_path / "auth.json",
                run_dir=tmp_path / "run",
                paid_approval="a" * 64,
            )

        message = str(error.value)
        assert f"approved child scope: {'a' * 64}" in message
        assert f"current child scope: {CURRENT_SCOPE}" in message
        assert "No model call was made." in message
        assert "--tasks RC-01 --dry-run" in message
        assert not (tmp_path / "run").exists()
        mock_run_paid.assert_not_called()


class TestPreflightIsolation:
    """The no-model preflight that probes each native arm before a plan is accepted."""

    def test_probes_every_native_arm(self, runner: Any, codex_runner_double: type, tmp_path: Path) -> None:
        """A green read-crop dry run must exercise the real A/B/C home boundary.

        A plan that only checked one arm would pass while another arm's disposable home was broken, so the preflight has
        to probe all three in order.
        """
        runner.preflight_isolation(
            repo_path=tmp_path,
            index_path=tmp_path / "index.json",
            marketplace_root=tmp_path,
            codemap_bin=tmp_path / "codemap-py",
            model="gpt-6.1-sol",
            structural_manifest_path=tmp_path / "manifest.json",
        )

        assert codex_runner_double.probed == ["A_plain", "B_auto", "C_strict"]

    def test_hands_the_run_relocation_to_the_adapter(
        self, runner: Any, codex_runner_double: type, tmp_path: Path
    ) -> None:
        """The ReadCrop preflight adapter is built with this run's relocation provenance.

        A run outside the canonical clone carries the locked graph by relocation, and this preflight admits the source
        and index before probing each arm. An adapter built without that provenance demands byte identity the worktree
        cannot have, which failed the whole isolated study after its plan had already been accepted.
        """
        relocation = {"frozen_index_sha256": "a" * 64}

        runner.preflight_isolation(
            repo_path=tmp_path / "repo",
            index_path=tmp_path / "repo" / "index.json",
            marketplace_root=tmp_path,
            codemap_bin=tmp_path / "codemap-py",
            model="gpt-6.1-sol",
            structural_manifest_path=tmp_path / "manifest.json",
            index_relocation=relocation,
        )

        assert codex_runner_double.init_kwargs["index_relocation"] == relocation


class TestRunPaid:
    """The provenance a paid run records before any provider subprocess starts."""

    def test_snapshot_binds_the_structural_launcher_and_readcrop_stage(
        self, runner: Any, codex_runner_double: type, tmp_path: Path
    ) -> None:
        """Paid provenance binds the one Fire launcher and the stage-specific scorer.

        The input snapshot must name the structural launcher as the runner and ship the read-crop contracts, stage and
        runtime as additional files, so a replay can prove which scorer produced a cell. The adapter interrupts at
        snapshot creation, which stops the run before any provider subprocess.
        """
        with pytest.raises(KeyboardInterrupt):
            runner.run_paid(
                scope={},
                tasks=[{"task": {"id": "RC-01"}, "contract": SimpleNamespace(task_id="RC-01")}],
                repo_path=tmp_path,
                index_path=tmp_path / "index.json",
                marketplace_root=tmp_path,
                codemap_bin=tmp_path / "codemap-py",
                auth_source=tmp_path / "auth.json",
                run_dir=tmp_path / "run",
                model="gpt-6.1-sol",
                structural_manifest_path=tmp_path / "manifest.json",
            )

        kwargs = codex_runner_double.snapshot_kwargs
        assert kwargs["runner_path"] == runner.STRUCTURAL_PATH
        assert kwargs["additional_shared_files"] == {
            "readcrop-contracts.py": runner.ROOT / "benchmarks" / "_bench_common" / "readcrop_contracts.py",
            "codex-readcrop-stage.py": Path(runner.__file__),
            "codex-runtime.py": runner.BENCHMARKS / "_bench_codex" / "runtime.py",
        }


class TestDryRun:
    """The no-model plan the stage prints: probe rows first, then one plan row per task and arm."""

    def test_plans_every_locked_task_in_every_arm(self, runner: Any) -> None:
        """The adapter plans each committed read-crop task in every treatment.

        Six tasks across three arms must give eighteen plan rows, with the last row being the final task in the strict
        arm, so no treatment silently drops a task.
        """
        tasks = [_planned(f"RC-{number:02d}") for number in range(1, 7)]

        rows = runner.dry_run(tasks, observed_codemap=ALL_ARMS_OBSERVED)

        assert sum(row.startswith("PLAN") for row in rows) == 18
        assert rows[-1] == "PLAN    RC-06  rep=1  C_strict"

    def test_rows_are_probe_and_plan_rows_in_shared_columns(self, runner: Any) -> None:
        """The read-crop plan returns only machine-shaped rows, with probes in the shared columns.

        The stage banner used to travel inside the row list, so a caller could not draw it as a section heading, and the
        probe rows carried Python-cased booleans that matched no other lane. The banner is now the caller's heading and
        the probe rows share one formatter.
        """
        rows = runner.dry_run([_planned("RC-01")], observed_codemap=ALL_ARMS_OBSERVED)

        assert [row.split()[0] for row in rows[:3]] == ["PROBE"] * 3
        assert rows[0] == "PROBE   A_plain   codemap=false  use=forbidden"
        assert not any("PREFLIGHT" in row or "\t" in row for row in rows)

    @pytest.mark.parametrize(
        ("observed", "row_index", "expected"),
        [
            pytest.param(
                ALL_ARMS_MISSING, 2, "PROBE   C_strict  codemap=false  use=required", id="missing-against-required-arm"
            ),
            pytest.param(
                ALL_ARMS_MISSING, 1, "PROBE   B_auto    codemap=false  use=optional", id="missing-against-optional-arm"
            ),
            pytest.param(
                {"A_plain": False}, 0, "PROBE   A_plain   codemap=false  use=forbidden", id="measured-arm-is-reported"
            ),
            pytest.param(
                {"A_plain": False}, 2, "PROBE   C_strict  codemap=unknown use=required", id="unmeasured-arm-is-unknown"
            ),
        ],
    )
    def test_probe_row_reports_the_measurement_not_the_arm_label(
        self, runner: Any, observed: dict[str, bool], row_index: int, expected: str
    ) -> None:
        """A probe row states what was measured beside the arm's required use, and unknown when nothing was.

        C_strict obliges the run to call Codemap, so a missing binary is precisely when the row matters most; the field
        used to come from the arm label and read true without any observation. An arm absent from the probe evidence
        must say unknown, so a reader can tell an unobserved arm from one observed to be missing Codemap.
        """
        rows = runner.dry_run([], observed_codemap=observed)

        assert rows[row_index] == expected


class ArchivedRun(NamedTuple):
    """A synthetic checksummed historical run and the locked repository layout it was built against."""

    repo: Path
    source_run: Path
    source_scope: dict[str, Any]


@pytest.fixture
def archived_readcrop_run(runner: Any, tmp_path: Path) -> ArchivedRun:
    """Build checksummed synthetic historical telemetry against real locked task loaders."""
    repo = tmp_path / "repo"
    tasks = runner.load_task_suite(runner.TASKS_PATH)
    modules: dict[str, dict[str, list[str]]] = {}
    for task in tasks:
        class_name, method_name = task["symbol"].split(".")
        parameters = ["self", *task.get("required_parameters", task["expected_keywords"])]
        method = f"    def {method_name}({', '.join(parameters)}):\n        pass\n"
        modules.setdefault(task["primary_module"], {}).setdefault(class_name, []).append(method)
    for module, classes in modules.items():
        path = repo / Path(*module.split(".")).with_suffix(".py")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(f"class {name}:\n" + "\n".join(methods) for name, methods in classes.items()))
    loaded = runner.load_readcrop_tasks(runner.TASKS_PATH, runner.METHODOLOGY_PATH, repo)
    contract = loaded[0]["contract"]
    answer = (
        "BEGIN_READ_CROP_JSON\n"
        + json.dumps(
            {
                "signature": contract.symbol,
                "parameters": list(contract.required_parameter_names),
                "behavior": "Synthetic fixture.",
            }
        )
        + "\nEND_READ_CROP_JSON"
    )
    stream = [_agent_message(answer), TURN_COMPLETED]
    row = runner.parse_readcrop_stream(
        (json.dumps(event) for event in stream), arm="A_plain", contract=contract, skill_path=None
    )
    row["elapsed_s"] = 1.0
    source_scope = {"model": "gpt-5.6-luna", "scope_sha256": "a" * 64, "fixture": "synthetic archived replay"}
    source_run = tmp_path / "archived-run"
    source_run.mkdir()
    (source_run / "run-metadata.json").write_text(json.dumps({"status": "completed", "scope": source_scope}))
    (source_run / "telemetry.jsonl").write_text(json.dumps(row) + "\n")
    runner.write_checksums(source_run)
    return ArchivedRun(repo, source_run, source_scope)


@pytest.fixture
def no_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any model or provisioning subprocess fail the test, so offline replay is proven to start none."""

    def forbidden_process(*_args: Any, **_kwargs: Any) -> Any:
        """Fail on any attempted model or provisioning subprocess during offline replay."""
        pytest.fail("offline replay must not start a subprocess")

    from _bench_codex.structural.runner import CodexRunner

    monkeypatch.setattr(CodexRunner, "__init__", forbidden_process)
    monkeypatch.setattr(subprocess, "run", forbidden_process)
    monkeypatch.setattr(subprocess, "Popen", forbidden_process)


def _replay_through_stage(runner: Any, archived: ArchivedRun, output: Path) -> None:
    """Replay the archived run by calling the stage entrypoint directly."""
    runner.run_stage(
        repo_path=archived.repo,
        model="gpt-5.6-luna",
        tasks_selector=None,
        dry_run_requested=False,
        resolve_scope_requested=False,
        auth_source=None,
        run_dir=None,
        paid_approval=None,
        rescore_run_dir=archived.source_run,
        rescore_output_dir=output,
    )


def _replay_through_cli(runner: Any, archived: ArchivedRun, output: Path) -> None:
    """Replay the archived run through the structural command-line entrypoint."""
    from _bench_codex.structural.cli import cli

    cli(
        repo_path=str(archived.repo),
        model="gpt-5.6-luna",
        rescore_readcrop_run_dir=str(archived.source_run),
        rescore_readcrop_output_dir=str(output),
    )


class TestArchivedReplay:
    """Offline replay of historical telemetry for a model that live scope no longer admits."""

    @pytest.mark.integration
    @pytest.mark.parametrize(
        "replay",
        [pytest.param(_replay_through_stage, id="stage"), pytest.param(_replay_through_cli, id="cli")],
    )
    def test_preserves_scope_without_live_admission(
        self,
        runner: Any,
        archived_readcrop_run: ArchivedRun,
        no_subprocess: None,
        replay: Callable[[Any, ArchivedRun, Path], None],
        tmp_path: Path,
    ) -> None:
        """Replay an explicit archived model offline while retaining its original source scope.

        A synthetic archived run is re-parsed through the real task loaders from either entrypoint, with every
        subprocess forbidden. The output must record the original scope as ``source_scope``, leave the source run byte-
        identical, and carry a checksum ledger that verifies.
        """
        output = tmp_path / "replayed"
        source_bytes = {path.name: path.read_bytes() for path in archived_readcrop_run.source_run.iterdir()}

        replay(runner, archived_readcrop_run, output)

        metadata = json.loads((output / "run-metadata.json").read_bytes())
        assert metadata["status"] == "completed"
        assert metadata["kind"] == "offline-readcrop-reparse-v1"
        assert metadata["source_scope"] == archived_readcrop_run.source_scope
        assert metadata["persisted_cells"] == 1
        assert {path.name: path.read_bytes() for path in archived_readcrop_run.source_run.iterdir()} == source_bytes
        runner.verify_checksums(output)

    def test_model_is_still_rejected_for_live_scope(self, runner: Any, archived_readcrop_run: ArchivedRun) -> None:
        """Offline compatibility cannot authorize a new live scope for the archived model.

        Replay admits the retired model only to re-read old telemetry; asking the same stage to resolve a live scope for
        it must still fail on the supported-model check.
        """
        with pytest.raises(ValueError, match="supported Codex benchmark model"):
            runner.run_stage(
                repo_path=archived_readcrop_run.repo,
                model="gpt-5.6-luna",
                tasks_selector=None,
                dry_run_requested=False,
                resolve_scope_requested=True,
                auth_source=None,
                run_dir=None,
                paid_approval=None,
            )
