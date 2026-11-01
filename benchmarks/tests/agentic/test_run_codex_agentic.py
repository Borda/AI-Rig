"""No-model contracts for the shared Codex agentic study.

The runner module (`run-codex-agentic.py`) is loaded once and driven with synthetic native streams, disposable
manifests, and fixture runners, so no model, credential, or plugin install is ever started. The only real external tool
call is the `git` build of one relocated-worktree fixture, because relocation admission reads real git state.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from _launcher_capability import _private_filesystem_available

BENCHMARKS_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BENCHMARKS_DIR))

# The structural runner is a re-export shim over a package; its seams are reached through
# the defining module, so patching the shim would leave those bindings untouched.
from _bench_codex.structural import diff_impact as codex_diff_impact  # noqa: E402
from _bench_codex.structural import provisioning as codex_provisioning  # noqa: E402
from _bench_common.mutation_isolation import relocate_frozen_index_for_worktree  # noqa: E402
from _bench_common.presentation import LEGEND_CLOSE_RULE, LEGEND_OPEN_RULE  # noqa: E402

#: Task ids read from the shipped suite rather than counted out here, so adding a task to the suite changes the
#: expected scope instead of failing every scope assertion in this module.
AGENTIC_TASK_IDS = tuple(
    task["id"]
    for task in json.loads((BENCHMARKS_DIR / "suites" / "tasks-agentic.json").read_text(encoding="utf-8"))["tasks"]
)
AGENTIC_ARMS = ("A_plain", "B_auto", "C_strict")
#: One repetition of every suite coordinate — the default no-model plan and paid scope size.
AGENTIC_CELLS = len(AGENTIC_TASK_IDS) * len(AGENTIC_ARMS)

POSIX_SECURITY = pytest.mark.skipif(not _private_filesystem_available(), reason="requires POSIX private-mode semantics")

_QUERY_ARGS = "query --compact rdeps lightning.pytorch.callbacks.timer"
_QUERY_COMMAND = f"$CODEMAP_BIN {_QUERY_ARGS}"
_TWO_IMPORTERS_SENTENCE = (
    "lightning.pytorch.trainer.trainer and lightning.pytorch.loops.fit_loop import the timer module."
)
_TWO_IMPORTERS = "lightning.pytorch.trainer.trainer and lightning.pytorch.loops.fit_loop."
_ONE_IMPORTER = "lightning.pytorch.trainer.trainer."
_RUN_ALL_SCRIPT = "#!/usr/bin/env bash\n"

REPLAY_LAUNCHERS = [
    pytest.param("/private/tmp/frozen-c-home/bin/codemap-py", id="posix-absolute"),
    pytest.param(r"C:\agentic\bin\codemap-py", id="windows-absolute"),
]


def _load_agentic() -> Any:
    """Load the runner once under a stable module name without entering its CLI.

    >>> _load_agentic() is _load_agentic()
    True
    """
    module_name = "run_codex_agentic"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    path = BENCHMARKS_DIR / "run-codex-agentic.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(name="agentic", scope="module")
def _agentic() -> Any:
    """Provide the cached runner to pytest without executing a model.

    >>> getfixture("agentic") is _load_agentic()
    True
    """
    return _load_agentic()


@pytest.fixture(name="task_and_truth")
def _task_and_truth(agentic: Any, tmp_path: Path) -> tuple[Any, Any]:
    """Build an isolated two-importer source tree and an oracle requiring importer, count, and ranking fields.

    >>> task, oracle = getfixture("task_and_truth")
    >>> oracle.task_id == task["id"], len(oracle.expected["production_importers"])
    (True, 2)
    """
    task = {
        "id": "BA-01",
        "type": "blast_radius_analysis",
        "prompt": "List importers.",
        "primary_module": "lightning.pytorch.callbacks.timer",
        "answer_contract": {
            "fields": ["production_importers", "rdep_counts", "ranking"],
            "params": {"ranking": {"candidate_set": "production_importers", "top_k": 1}},
        },
    }
    for relative, source in {
        "lightning/pytorch/callbacks/timer.py": "",
        "lightning/pytorch/trainer/trainer.py": "import lightning.pytorch.callbacks.timer\n",
        "lightning/pytorch/loops/fit_loop.py": "import lightning.pytorch.callbacks.timer\n",
    }.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return task, agentic.build_oracle(task, tmp_path)


@pytest.fixture(name="scored_result")
def _scored_result(agentic: Any, task_and_truth: tuple[Any, Any]) -> Any:
    """Parse a plain-arm stream whose answer envelope exactly matches the oracle."""
    task, truth = task_and_truth
    return agentic.parse_agentic_stream(
        _stream(_message(_labelled("", truth)), _completed()), arm="A_plain", task=task, ground_truth=truth
    )


@pytest.fixture(name="skill_path")
def _skill_path(tmp_path: Path) -> Path:
    """Write a disposable locked Codemap skill file for the strict arm."""
    path = tmp_path / "SKILL.md"
    path.write_text("# locked Codemap skill\n", encoding="utf-8")
    return path


@pytest.fixture(name="replay_output")
def _replay_output(tmp_path: Path) -> Path:
    """Return a replay file path in a sibling directory, outside the historical telemetry directory."""
    directory = tmp_path.with_name(f"{tmp_path.name}-replay")
    directory.mkdir()
    return directory / "replay.json"


def _stream(*events: dict[str, Any]) -> str:
    """Serialize events in order with separators but no trailing newline.

    >>> _stream()
    ''
    >>> [json.loads(line) for line in _stream({"id": 1}, {"id": 2}).splitlines()]
    [{'id': 1}, {'id': 2}]
    """
    return "\n".join(json.dumps(event) for event in events)


def _message(text: str) -> dict[str, Any]:
    """Wrap assistant text in a completed native message event without changing the text.

    >>> _message("example")["item"]
    {'id': 'm', 'type': 'agent_message', 'text': 'example'}
    """
    return {"type": "item.completed", "item": {"id": "m", "type": "agent_message", "text": text}}


def _labelled(text: str, oracle: Any) -> str:
    """Append sorted oracle expectations inside the shared answer-envelope markers.

    >>> from types import SimpleNamespace
    >>> _labelled("Summary", SimpleNamespace(expected={"z": 2, "a": 1})).splitlines()
    ['Summary', 'BEGIN_ANSWER_JSON', '{"a": 1, "z": 2}', 'END_ANSWER_JSON']
    """
    return f"{text}\nBEGIN_ANSWER_JSON\n{json.dumps(dict(oracle.expected), sort_keys=True)}\nEND_ANSWER_JSON"


def _completed() -> dict[str, Any]:
    """Build a successful terminal event with fixed gross, cached, and output token counts.

    >>> _completed()["usage"]
    {'input_tokens': 123, 'cached_input_tokens': 23, 'output_tokens': 45}
    """
    return {
        "type": "turn.completed",
        "status": "completed",
        "usage": {"input_tokens": 123, "cached_input_tokens": 23, "output_tokens": 45},
    }


def _query(item_id: str = "q", command: str = _QUERY_COMMAND) -> dict[str, Any]:
    """Build successful compact-query evidence using the caller's item identifier and command.

    >>> item = _query("example")["item"]
    >>> item["id"], item["exit_code"], json.loads(item["aggregated_output"])
    ('example', 0, {'index': {'query_complete': True, 'compact': True}})
    """
    return {
        "type": "item.completed",
        "item": {
            "id": item_id,
            "type": "command_execution",
            "command": command,
            "status": "completed",
            "exit_code": 0,
            "aggregated_output": json.dumps({"index": {"query_complete": True, "compact": True}}),
        },
    }


def _launcher_query(launcher: str) -> dict[str, Any]:
    """Build compact-query evidence whose command starts with an absolute launcher path."""
    return _query(command=f"{launcher} {_QUERY_ARGS}")


def _cell(arm: str, **fields: Any) -> dict[str, Any]:
    """Build the coordinate keys of one telemetry row; any key may be overridden through ``fields``.

    >>> _cell("A_plain", repetition=2)
    {'task_id': 'BA-01', 'repetition': 2, 'arm': 'A_plain'}
    """
    return {"task_id": "BA-01", "repetition": 1, "arm": arm, **fields}


def _write_jsonl(path: Path, *rows: dict[str, Any]) -> None:
    """Write rows as sorted-key JSON lines so the file hash is stable."""
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def _append_row(agentic: Any, result: Any, directory: Path, execution_index: int = 1) -> dict[str, Any]:
    """Append one parsed result to a fresh telemetry file and return the persisted row."""
    path = directory / "telemetry.jsonl"
    agentic._append_telemetry(path, result, execution_index=execution_index)
    return json.loads(path.read_text(encoding="utf-8"))


class TestDryRun:
    """The no-model plan lists probes and every shared coordinate, and preflights later admission."""

    def test_lists_three_probes_and_default_shared_coordinates(self, agentic: Any) -> None:
        """The default dry run exposes all shared tasks once in every canonical arm.

        The first plan row is the first task's plain arm; the last row belongs to the last suite task, whatever arm the
        counterbalancing puts last.
        """
        rows = agentic.dry_run()
        probes = [row for row in rows if row.startswith("PROBE")]
        plan = [row for row in rows if row.startswith("PLAN")]

        assert len(probes) == 3
        assert len(plan) == len(AGENTIC_TASK_IDS) * len(AGENTIC_ARMS)
        assert plan[0] == "PLAN    BA-01  rep=1  A_plain"
        # Tasks run in suite order, but arms are counterbalanced within each task, so the final row's arm is whatever
        # the last task's order puts last. Pinning it here would assert the counterbalancing policy, which has its own
        # tests.
        assert plan[-1].split()[1] == AGENTIC_TASK_IDS[-1]

    def test_default_coordinates_equal_the_shared_suite_scope(self, agentic: Any) -> None:
        """The default no-model plan schedules each shared parity coordinate once.

        Prevents a runner that advertises parity while retaining the BA-01 pilot, omitting an arm, duplicating a
        coordinate, or using a hidden default repeat. A mere plan-length assertion could miss swapped or repeated
        coordinates, so this compares the complete coordinate set.
        """
        rows = agentic.dry_run()
        actual = {
            (parts[1], int(parts[2].removeprefix("rep=")), parts[3])
            for row in rows
            if row.startswith("PLAN")
            for parts in [row.split()]
        }
        expected = {(task_id, 1, arm) for task_id in AGENTIC_TASK_IDS for arm in AGENTIC_ARMS}

        assert actual == expected
        assert sum(row.startswith("PLAN") for row in rows) == len(expected)

    def test_accepts_a_positive_explicit_repeat_override(self, agentic: Any) -> None:
        """A caller can request an admitted repeat count without changing the task scope.

        Prevents the former fixed-three-repeat pilot behavior and a runner that silently ignores an explicit repeat
        request.
        """
        rows = agentic.dry_run(repetitions=2)
        plan = [row.split() for row in rows if row.startswith("PLAN")]

        assert len(plan) == len(AGENTIC_TASK_IDS) * len(AGENTIC_ARMS) * 2
        assert {parts[1] for parts in plan} == set(AGENTIC_TASK_IDS)
        assert {parts[2] for parts in plan} == {"rep=1", "rep=2"}
        assert {parts[3] for parts in plan} == set(AGENTIC_ARMS)

    def test_rejects_zero_repetitions(self, agentic: Any) -> None:
        """A zero repeat count is invalid because it would create no evidence-bearing parity coordinates."""
        with pytest.raises(ValueError, match=r"positive|at least 1"):
            agentic.dry_run(repetitions=0)

    def test_preflights_snapshot_bound_c_admission_without_auth_or_model(
        self, agentic: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Run-all's no-model preflight must reach the later snapshot-bound C admission.

        Prevents the pure plan renderer from declaring a runnable agentic study even though later C homes cannot install
        their archived plugins. The probe raises before any transport/model call; a planner that skips runtime admission
        must therefore fail this test.
        """
        index_path = tmp_path / "locked-index.json"
        index_path.write_text("{}", encoding="utf-8")
        marketplace_root = tmp_path / "marketplace"
        marketplace_root.mkdir()
        codemap_bin = tmp_path / "codemap-py"
        codemap_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        codemap_bin.chmod(0o755)
        calls: list[dict[str, Any]] = []

        class SnapshotAdmissionProbe:
            """Reject only after the dry run has entered later C snapshot admission."""

            def __init__(self, **kwargs: Any) -> None:
                """Initialize the test double's fixture-controlled state."""
                assert kwargs["auth_source"] is None
                assert kwargs["repo_path"] == tmp_path.resolve()
                assert kwargs["index_path"] == index_path.resolve()
                assert kwargs["marketplace_root"] == marketplace_root
                assert kwargs["codemap_bin"] == codemap_bin
                calls.append(kwargs)

            def preflight_snapshot_bound_admission(self) -> None:
                """Make reaching the required no-model runtime transition observable."""
                raise RuntimeError("fixture later C snapshot admission")

            def close(self) -> None:
                """Match the production runner cleanup protocol."""
                return None

        monkeypatch.setattr(agentic, "AgenticCodexRunner", SnapshotAdmissionProbe)

        with pytest.raises(RuntimeError, match="fixture later C snapshot admission"):
            agentic.main(
                dry_run=True,
                repo_path=tmp_path,
                index_path=index_path,
                marketplace_root=marketplace_root,
                codemap_bin=codemap_bin,
            )

        assert len(calls) == 1


class TestOutputLegend:
    """The agentic legend is one shared-renderer block and prints identically to the archived run log."""

    def test_declares_treatments_and_metrics_in_one_block(self, agentic: Any) -> None:
        """Agentic output declares its treatments and metrics in one shared-renderer block.

        Readers rely on the legend to interpret each progress and summary column, so a dropped or renamed metric
        definition leaves archived runs unreadable.
        """
        legend = agentic._OUTPUT_LEGEND

        assert legend.startswith(f"{LEGEND_OPEN_RULE}\n")
        assert legend.endswith(LEGEND_CLOSE_RULE)
        assert legend.count("LEGEND") == 2
        assert "treatments: A_plain=no Codemap, B_auto=optional CLI, C_strict=installed Codemap Skill" in legend
        assert "pass:" in legend
        assert "component:" in legend
        assert "SCORE:" not in legend
        assert (
            "component: secondary partial-credit mean; n/a when unscored; scored denominator shown separately" in legend
        )
        assert "EREC: expected-importer recall in all agent text (higher is better)" in legend
        assert "RREC: expected-importer recall in the final report (higher is better)" in legend
        assert (
            "DEFF: unbounded expected-importer exposure hits per command (higher is better within the same task)"
            in legend
        )
        assert (
            "input tokens: gross total; summaries separate fresh/gross/output tokens and time with eligible pair counts"
            in legend
        )
        assert (
            "answer: ✓ strict envelope, △ diagnostic bare-JSON recovery (not poolable), ✗ absent or invalid" in legend
        )
        assert "correct:" not in legend

    def test_emitted_form_stays_framed_plain_text_when_redirected(
        self, agentic: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A redirected agentic legend writes exactly the framed text the run log also archives.

        Scenario: the legend renders as a Rich panel on a terminal, but the same run writes the plain constant into its
        own log file. A drift between the two would make an archived run unreplayable against the stream it was
        captured from.
        """
        agentic._emit_output_legend()

        assert capsys.readouterr().out == f"{agentic._OUTPUT_LEGEND}\n"


def test_run_line_forwards_through_shared_renderer_and_keeps_logs_ansi_free(
    agentic: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A summary/progress arm row has one plain archived representation.

    The terminal helper may add Rich color on a TTY, but a redirected run log is an input to later replay and must never
    contain terminal control codes.
    """
    log = tmp_path / "run.log"

    agentic._emit_run_line(log, "SUMMARY  C_strict cells=1", arm="C_strict")

    assert capsys.readouterr().out == "SUMMARY  C_strict cells=1\n"
    assert log.read_text(encoding="utf-8") == "SUMMARY  C_strict cells=1\n"
    assert "\x1b" not in log.read_text(encoding="utf-8")


class TestProbeRows:
    """Probe rows share one formatter and name each arm's Codemap obligation."""

    def test_share_the_structural_column_layout(self, agentic: Any) -> None:
        """Every agentic probe row puts its fields in the same tab-free columns as the other lanes.

        Scenario: the three lanes each padded their probe rows differently, so the same capability read printed in
        three shapes across one combined run log. All three now go through one formatter, and the longer C_strict label
        must not push its own fields out of the shared column.
        """
        rows = [agentic._format_probe(agentic.probe_arm(arm)) for arm in AGENTIC_ARMS]

        assert not any("\t" in row for row in rows)
        assert {row.index("use=") for row in rows} == {rows[0].index("use=")}

    def test_separate_the_optional_and_required_arms(self, agentic: Any) -> None:
        """B_auto and C_strict probe rows state different Codemap obligations.

        Scenario: both arms find the binary, so a row carrying only ``codemap=true`` read identically for an arm that
        may use Codemap and an arm that must. The measured availability stays where it was — a missing binary must
        still surface as a failure — and the contract is named beside it.
        """
        optional, required = (agentic._format_probe(agentic.probe_arm(arm)) for arm in ("B_auto", "C_strict"))

        assert "codemap=true" in optional
        assert "codemap=true" in required
        assert "use=optional" in optional
        assert "use=required" in required


@pytest.mark.parametrize(
    ("arm", "available"),
    [
        pytest.param("A_plain", False, id="plain"),
        pytest.param("B_auto", True, id="optional-direct-home"),
        pytest.param("C_strict", True, id="required-skill-home"),
    ],
)
def test_isolated_home_adapter_maps_arms_to_structural_homes(
    agentic: Any, monkeypatch: pytest.MonkeyPatch, arm: str, available: bool
) -> None:
    """The agentic slice maps its semantic arms to the established disposable homes.

    The structural lane owns home preparation and probing; the agentic adapter must forward the arm unchanged and return
    the structural home and probe result untouched, so both lanes isolate identically.
    """
    calls: list[str] = []
    home = object()
    monkeypatch.setattr(
        agentic._structural, "prepare_arm_home", lambda selected_arm, **_kwargs: calls.append(selected_arm) or home
    )
    monkeypatch.setattr(agentic._structural, "probe_arm_home", lambda _home: {"codemap_available": available})

    assert agentic.prepare_isolated_home(arm) is home
    assert calls == [arm]
    assert agentic.probe_isolated_home(home, arm) == {"codemap_available": available}


class TestParseStream:
    """A native stream is scored by the shared oracle and judged against the arm's Codemap obligation."""

    @pytest.mark.parametrize(
        ("arm", "prelude", "text", "expected"),
        [
            pytest.param(
                "A_plain",
                (),
                _TWO_IMPORTERS_SENTENCE,
                {
                    "success": True,
                    "codemap_used": False,
                    "treatment_adherence": True,
                    "input_tokens": 123,
                    "cached_input_tokens": 23,
                    "output_tokens": 45,
                },
                id="plain-scored-without-codemap",
            ),
            pytest.param(
                "B_auto",
                (_query(),),
                _TWO_IMPORTERS,
                {
                    "success": True,
                    "codemap_used": True,
                    "compliance": None,
                    "treatment_adherence": True,
                    "command_calls": 1,
                },
                id="auto-direct-query-is-optional-and-adherent",
            ),
            pytest.param(
                "B_auto",
                (),
                _ONE_IMPORTER,
                {"success": True, "codemap_used": False, "treatment_adherence": True},
                id="auto-no-call-is-valid",
            ),
            pytest.param(
                "A_plain",
                (_query(),),
                _ONE_IMPORTER,
                {"success": True, "contaminated": True, "treatment_adherence": False},
                id="plain-codemap-call-is-contamination",
            ),
        ],
    )
    def test_scores_unconstrained_arms(
        self,
        task_and_truth: tuple[Any, Any],
        agentic: Any,
        arm: str,
        prelude: tuple[dict[str, Any], ...],
        text: str,
        expected: dict[str, Any],
    ) -> None:
        """A_plain stays valid without Codemap and fails adherence on leakage; B_auto may use Codemap or not.

        Scenario: a completed stream with an oracle-exact answer, optionally preceded by one compact Codemap query.
        B_auto preserves the Claude contract (not using Codemap is still valid), and a plain-arm query keeps its
        successful provider telemetry while failing treatment adherence.
        """
        task, truth = task_and_truth

        result = agentic.parse_agentic_stream(
            _stream(*prelude, _message(_labelled(text, truth)), _completed()), arm=arm, task=task, ground_truth=truth
        )

        assert {name: getattr(result, name) for name in expected} == expected

    @pytest.mark.parametrize(
        ("prelude", "expected"),
        [
            pytest.param(
                (_query(),),
                {"success": True, "codemap_used": True, "compliance": True, "treatment_adherence": True},
                id="compact-query-credited-without-manual-read",
            ),
            pytest.param(
                (),
                {"success": True, "compliance": False, "treatment_adherence": False},
                id="no-call-row-kept-but-not-adherent",
            ),
        ],
    )
    def test_scores_strict_arm_against_installed_skill(
        self,
        task_and_truth: tuple[Any, Any],
        agentic: Any,
        skill_path: Path,
        prelude: tuple[dict[str, Any], ...],
        expected: dict[str, Any],
    ) -> None:
        """C_strict credits a compact query through its installed Skill and keeps a no-call row as diagnosis.

        Scenario: the strict arm must use Codemap. A compact query counts without a manual skill read; a completed
        no-call stream stays a scored row for diagnosis but records compliance and adherence false, so aggregation
        excludes that coordinate instead of erasing its raw evidence.
        """
        task, truth = task_and_truth

        result = agentic.parse_agentic_stream(
            _stream(*prelude, _message(_labelled(_TWO_IMPORTERS, truth)), _completed()),
            arm="C_strict",
            task=task,
            ground_truth=truth,
            skill_path=skill_path,
        )

        assert {name: getattr(result, name) for name in expected} == expected
        assert result.quality.scored is True

    @pytest.mark.parametrize(
        ("stream", "error_type"),
        [
            pytest.param("{\n" + _stream(_completed()), "malformed_stream", id="malformed"),
            pytest.param(_stream({"type": "turn.failed", "error": "transport failed"}), "turn_failed", id="failed"),
            pytest.param(_stream(_message("unfinished")), "missing_terminal", id="incomplete"),
        ],
    )
    def test_fails_closed_on_malformed_incomplete_and_error_streams(
        self, task_and_truth: tuple[Any, Any], agentic: Any, stream: str, error_type: str
    ) -> None:
        """Malformed, incomplete, and provider-error native streams cannot become successful rows.

        Scenario: a stream with a broken first line, a provider failure event, or no terminal event must surface as an
        incomplete execution failure with its own error type rather than a scored pass.
        """
        task, truth = task_and_truth

        result = agentic.parse_agentic_stream(stream, arm="A_plain", task=task, ground_truth=truth)

        assert result.success is False
        assert result.incomplete is True
        assert result.error_type == error_type

    def test_scores_identical_output_with_the_shared_answer_contract(
        self, task_and_truth: tuple[Any, Any], agentic: Any
    ) -> None:
        """Codex uses the exact shared scorer rather than a second quality implementation.

        Scenario: the same labelled report is scored by the runner's stream parser and directly by the shared scorer;
        every legacy recall metric and the quality score must agree.
        """
        task, truth = task_and_truth
        result = agentic.parse_agentic_stream(
            _stream(_message(_labelled(_TWO_IMPORTERS_SENTENCE, truth)), _completed()),
            arm="A_plain",
            task=task,
            ground_truth=truth,
        )
        report = _labelled(_TWO_IMPORTERS_SENTENCE, truth)
        expected = agentic.score_answer(
            truth,
            agentic.parse_labeled_answer(task, report),
            exposure_text=report,
            report_text=report,
            tool_calls=0,
        )

        assert result.quality.erec == expected.erec
        assert result.quality.rrec == expected.rrec
        assert result.quality.deff == expected.deff
        assert result.quality.quality_score == expected.quality_score

    def test_report_recall_uses_only_text_after_the_last_tool(
        self, task_and_truth: tuple[Any, Any], agentic: Any
    ) -> None:
        """RREC excludes exploratory prose emitted before the final tool call.

        Scenario: one importer is named before a query and the other in the final labelled report; all-text recall
        counts both, while the report text starts only at the post-tool message.
        """
        task, truth = task_and_truth

        result = agentic.parse_agentic_stream(
            _stream(
                _message("lightning.pytorch.trainer.trainer is one candidate."),
                _query(),
                _message(_labelled("Final: lightning.pytorch.loops.fit_loop.", truth)),
                _completed(),
            ),
            arm="B_auto",
            task=task,
            ground_truth=truth,
        )

        assert result.quality.erec == 1.0
        assert result.quality.rrec == 1.0
        assert result.report_text.startswith("Final: lightning.pytorch.loops.fit_loop.")


class TestCompletedInvalidAnswer:
    """A completed stream whose answer breaks the envelope is unscored, not a transport failure."""

    @pytest.fixture(
        name="result",
        params=[
            pytest.param("Final answer without the required envelope.", id="no-envelope"),
            pytest.param("BEGIN_ANSWER_JSON\n{not-json}\nEND_ANSWER_JSON", id="unparseable-envelope"),
        ],
    )
    def _result(self, request: pytest.FixtureRequest, agentic: Any, task_and_truth: tuple[Any, Any]) -> Any:
        """Parse a completed plain-arm stream whose final answer text is invalid."""
        task, truth = task_and_truth
        return agentic.parse_agentic_stream(
            _stream(_message(request.param), _completed()), arm="A_plain", task=task, ground_truth=truth
        )

    def test_is_unscored_without_becoming_transport_failure(self, result: Any) -> None:
        """Malformed answer syntax remains a wire failure, not a fabricated zero semantic score.

        The transport itself succeeded, so the row stays a success with an answer-contract error and no quality grade.
        """
        assert result.success is True
        assert result.incomplete is False
        assert result.error_type == "answer_contract_failed"
        assert result.error
        assert result.answer_error == result.error
        assert result.answer_contract_valid is False
        assert result.diagnostic_only is False
        assert result.answer_pooling_eligible is False
        assert result.quality is None
        assert result.evidence.erec == 0.0
        assert result.evidence.rrec == 0.0
        assert result.evidence.deff == 0.0

    def test_progress_line_marks_the_answer_absent(self, agentic: Any, result: Any) -> None:
        """The progress line shows a failed row with an absent answer and no component score."""
        line = agentic._progress_line(1, 1, result)

        assert line.startswith("(1/1) ✗  ")
        assert "component=n/a" in line
        assert "pass=" not in line
        assert "status=" not in line
        assert "EREC=" in line
        assert "RREC=" in line
        assert "DEFF=" in line
        assert "answer:✗" in line
        assert "correct:" not in line

    def test_telemetry_keeps_null_quality_and_zero_evidence(self, agentic: Any, result: Any, tmp_path: Path) -> None:
        """The persisted row records the answer error with null quality instead of a zero grade."""
        telemetry = _append_row(agentic, result, tmp_path)

        assert telemetry["success"] is True
        assert telemetry["answer_error"] == result.answer_error
        assert telemetry["quality"] is None
        assert telemetry["evidence"] == {"deff": 0.0, "erec": 0.0, "rrec": 0.0}


class TestUniqueBareJsonAnswer:
    """A unique bare JSON answer is diagnosed for content but never pooled under the wire contract."""

    @pytest.fixture(name="result")
    def _result(self, agentic: Any, task_and_truth: tuple[Any, Any]) -> Any:
        """Parse a completed plain-arm stream whose answer is the oracle JSON without the envelope."""
        task, truth = task_and_truth
        bare_answer = json.dumps(dict(truth.expected), sort_keys=True)
        return agentic.parse_agentic_stream(
            _stream(_message(bare_answer), _completed()), arm="A_plain", task=task, ground_truth=truth
        )

    def test_is_diagnostic_only_but_keeps_semantic_and_evidence_scores(self, result: Any) -> None:
        """Recoverable bare JSON diagnoses content without weakening the pooled wire contract.

        The row still fails the answer contract and cannot be pooled, yet its quality and recall metrics reflect the
        recovered answer.
        """
        assert result.success is True
        assert result.error_type == "answer_contract_failed"
        assert result.answer_contract_valid is False
        assert result.diagnostic_only is True
        assert result.answer_pooling_eligible is False
        assert result.quality.quality_score == 1.0
        assert result.evidence.erec == 1.0
        assert result.evidence.rrec == 1.0

    def test_progress_line_marks_diagnostic_recovery(self, agentic: Any, result: Any) -> None:
        """The progress line uses the diagnostic recovery symbol rather than the strict-envelope one."""
        assert "answer:△" in agentic._progress_line(1, 1, result)

    def test_telemetry_flags_diagnostic_only_and_keeps_scores(self, agentic: Any, result: Any, tmp_path: Path) -> None:
        """The persisted row is flagged diagnostic-only and not poolable while keeping its scores."""
        telemetry = _append_row(agentic, result, tmp_path)

        assert telemetry["diagnostic_only"] is True
        assert telemetry["answer_pooling_eligible"] is False
        assert telemetry["quality"]["quality_score"] == 1.0
        assert telemetry["evidence"]["erec"] == 1.0


_FULL_QUALITY = {"correct": True, "quality_score": 1.0, "graded_score": 1.0}
_PARTIAL_QUALITY = {"correct": False, "quality_score": 2 / 3, "graded_score": 56 / 57}


class TestProgressLine:
    """One progress line per cell carries a single verdict symbol and fixed-precision metrics."""

    @pytest.mark.parametrize(
        ("success", "incomplete", "adherent", "quality", "symbol", "shown_quality"),
        [
            pytest.param(True, False, True, _FULL_QUALITY, "✓", "100.0%", id="fully-passing"),
            pytest.param(True, False, True, _PARTIAL_QUALITY, "✗", "98.2%", id="partial-answer"),
            pytest.param(True, False, False, _FULL_QUALITY, "✗", "0.0%", id="treatment-violation"),
            pytest.param(False, False, True, _FULL_QUALITY, "!", "0.0%", id="execution-failed-despite-correct-answer"),
            pytest.param(True, True, True, _FULL_QUALITY, "!", "0.0%", id="incomplete-despite-correct-answer"),
            pytest.param(False, True, False, _PARTIAL_QUALITY, "!", "0.0%", id="execution-failure-precedes-others"),
        ],
    )
    def test_symbol_gives_execution_failure_priority_over_answer_correctness(
        self,
        agentic: Any,
        scored_result: Any,
        success: bool,
        incomplete: bool,
        adherent: bool,
        quality: dict[str, Any],
        symbol: str,
        shown_quality: str,
    ) -> None:
        """Use a single verdict symbol with execution failure taking priority over answer correctness.

        Scenario: a oracle-exact row is rewritten to each combination of execution, adherence, and answer quality; the
        line must show one symbol, the component mean, and a quality that is zero whenever the cell is not a clean pass.
        """
        result = replace(
            scored_result,
            success=success,
            incomplete=incomplete,
            treatment_adherence=adherent,
            quality=replace(scored_result.quality, **quality),
        )

        line = agentic._progress_line(3, 48, result)

        assert line.startswith(f"(3/48) {symbol}  BA-01")
        assert "pass=" not in line
        assert "status=" not in line
        assert f"component={quality['quality_score']:.3f}" in line
        assert f"quality={shown_quality}" in line
        assert (
            "outcome: ✓ pass, ✗ completed but not passing, ! execution failed or incomplete" in agentic._OUTPUT_LEGEND
        )

    @pytest.mark.parametrize(
        ("deff", "display"),
        [
            pytest.param(0.0, "0.00", id="zero"),
            pytest.param(5.4, "5.40", id="trailing-zero"),
            pytest.param(23.333333, "23.33", id="above-ten"),
            pytest.param(123.456, "123.46", id="rounding"),
        ],
    )
    def test_deff_uses_two_decimals_without_changing_evidence(
        self, agentic: Any, scored_result: Any, deff: float, display: str
    ) -> None:
        """Round the unbounded display metric while retaining full-precision telemetry evidence.

        Scenario: DEFF has no upper bound, so the line shows two decimals for any magnitude while the row keeps the
        original value for telemetry.
        """
        result = replace(scored_result, evidence=replace(scored_result.evidence, deff=deff))

        line = agentic._progress_line(1, 1, result)

        assert f" DEFF={display} " in line
        assert "component=1.000  EREC=1.000  RREC=1.000" in line
        assert result.evidence.deff == deff


@pytest.fixture(name="admitted")
def _admitted(agentic: Any, tmp_path: Path) -> tuple[Path, str]:
    """Provide a disposable admitted manifest and its exact digest."""
    return _admitted_manifest(agentic, tmp_path)


def _admitted_manifest(agentic: Any, tmp_path: Path) -> tuple[Path, str]:
    """Write a disposable admitted manifest and return its exact digest for mocked execution tests.

    The tracked manifest is only read; no provider or admission workflow is started.

    >>> from tempfile import TemporaryDirectory
    >>> with TemporaryDirectory() as directory:
    ...     path, digest = _admitted_manifest(_load_agentic(), Path(directory))
    ...     manifest = json.loads(path.read_text(encoding="utf-8"))
    ...     manifest["admission"]["paid_execution"], digest == hashlib.sha256(path.read_bytes()).hexdigest()
    ('admitted', True)
    """
    manifest = json.loads(agentic._MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest["admission"]["paid_execution"] = "admitted"
    manifest["artifact_sha256"]["codex_agentic_runner"] = hashlib.sha256(
        Path(agentic.__file__).read_bytes()
    ).hexdigest()
    manifest["artifact_sha256"]["run_all"] = "a" * 64
    path = tmp_path / "codex-agentic.json"
    path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


class TestPaidAdmission:
    """Paid execution needs the exact current manifest digest and an explicitly admitted study manifest."""

    def test_accepts_the_exact_digest_of_an_admitted_manifest(self, agentic: Any, admitted: tuple[Path, str]) -> None:
        """The approval digest of the current admitted manifest unlocks paid execution."""
        manifest_path, approved_hash = admitted

        manifest = agentic.validate_paid_admission(manifest_path, approved_hash)

        assert manifest["admission"]["paid_execution"] == "admitted"

    def test_rejects_a_stale_approval_hash(self, agentic: Any, admitted: tuple[Path, str]) -> None:
        """Paid execution rejects a digest that is not the exact current manifest hash."""
        manifest_path, _approved_hash = admitted

        with pytest.raises(ValueError, match="exact current manifest"):
            agentic.validate_paid_admission(manifest_path, "0" * 64)

    def test_rejects_an_explicitly_unadmitted_manifest(self, agentic: Any, admitted: tuple[Path, str]) -> None:
        """A manifest marked not admitted is refused even when its approval digest is current.

        Scenario: the digest is recomputed after editing the admission flag, so only the flag can cause the refusal.
        """
        manifest_path, _approved_hash = admitted
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["admission"]["paid_execution"] = "not_admitted"
        manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

        with pytest.raises(ValueError, match="does not admit"):
            agentic.validate_paid_admission(manifest_path, hashlib.sha256(manifest_path.read_bytes()).hexdigest())


class _FixtureRunner:
    """No-auth, no-model runner proving paid artifact persistence contracts."""

    def __init__(self, *, fail_after: int | None = None, **_kwargs: Any) -> None:
        """Initialize the test double's fixture-controlled state."""
        self.calls = 0
        self.fail_after = fail_after

    def close(self) -> None:
        """Match the production runner cleanup protocol."""

    def create_input_snapshot(self, run_dir: Path, **_kwargs: Any) -> dict[str, Any]:
        """Create the minimal immutable inputs and verified runtime evidence used by paid-path tests."""
        snapshot_root = run_dir / "inputs"
        snapshot_root.mkdir(mode=0o700)
        snapshot_path = snapshot_root / "input-snapshot.json"
        serialized = b'{"schema_version":"fixture-agentic-input-snapshot-v1"}\n'
        snapshot_path.write_bytes(serialized)
        identity = {
            "codemap-py": {"version": "fixture", "manifest_sha256": "a" * 64},
            "codex-rig": {"version": "fixture", "manifest_sha256": "b" * 64},
        }
        evidence_path = run_dir / "runtime-isolation.jsonl"
        evidence_path.write_text(
            json.dumps(
                {
                    "arm": "C_strict",
                    "status": "verified",
                    "error": None,
                    "expected_plugin_identities": identity,
                    "observed_plugin_identities": identity,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return {
            "schema_version": "fixture-agentic-input-snapshot-v1",
            "files": [],
            "path": str(snapshot_path.resolve()),
            "sha256": hashlib.sha256(serialized).hexdigest(),
            "bytes": len(serialized),
        }

    def run(self, task: Any, arm: str, *, repetition: int, **_kwargs: Any) -> Any:
        """Return a completed scored row or inject a deterministic interruption."""
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise RuntimeError("fixture interruption")
        compliant = None if arm != "C_strict" else False
        return _load_agentic().AgenticRun(
            arm=arm,
            task_id=task["id"],
            repetition=repetition,
            success=True,
            input_tokens=10,
            cached_input_tokens=0,
            output_tokens=2,
            command_calls=0,
            codemap_calls=0,
            codemap_successful_calls=0,
            codemap_used=False,
            compliance=compliant,
            treatment_adherence=arm != "C_strict",
            contaminated=False,
            incomplete=False,
            malformed_lines=0,
            error="",
            error_type="",
            output_text="",
            report_text="",
            quality=None,
            raw_events=[],
        )


class _SnapshotFailureRunner(_FixtureRunner):
    """Fixture that fails while writing pre-cell immutable inputs."""

    def create_input_snapshot(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        """Exercise durable failure handling before a coordinate starts."""
        raise RuntimeError("fixture snapshot interruption")


def _prepare_paid_fixture(
    agentic: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, str, Path, Path]:
    """Create local fixture artifacts and temporarily bypass runtime validation through the supplied monkeypatch.

    This prepares a mock boundary only; it never invokes paid execution.

    >>> from tempfile import TemporaryDirectory
    >>> with TemporaryDirectory() as directory, pytest.MonkeyPatch.context() as patch:
    ...     manifest, digest, index, launcher = _prepare_paid_fixture(_load_agentic(), patch, Path(directory))
    ...     json.loads(index.read_text()), launcher.name, digest == hashlib.sha256(manifest.read_bytes()).hexdigest()
    ({'modules': []}, 'run-all.sh', True)
    """
    manifest_path, _approval = _admitted_manifest(agentic, tmp_path)
    launcher_path = tmp_path / "run-all.sh"
    launcher_path.write_text(_RUN_ALL_SCRIPT, encoding="utf-8")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifact_sha256"]["run_all"] = hashlib.sha256(launcher_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    approval = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    index_path = tmp_path / "index.json"
    index_path.write_text(json.dumps({"modules": []}), encoding="utf-8")
    monkeypatch.setattr(agentic, "_validate_agentic_runtime", lambda *_args: None)
    return manifest_path, approval, index_path, launcher_path


def _lock_run_launcher(manifest_path: Path, run_dir: Path) -> tuple[str, Path]:
    """Create a disposable launcher snapshot and update the supplied manifest's launcher digest.

    >>> from tempfile import TemporaryDirectory
    >>> with TemporaryDirectory() as directory:
    ...     root = Path(directory)
    ...     manifest, _ = _admitted_manifest(_load_agentic(), root)
    ...     digest, launcher = _lock_run_launcher(manifest, root / "run")
    ...     launcher.name, (launcher.parent / "source.sha256").is_file()
    ...     digest == hashlib.sha256(manifest.read_bytes()).hexdigest()
    ('run-all.sh', True)
    True
    """
    launcher_path = run_dir / ".launcher" / "run-all.sh"
    launcher_path.parent.mkdir(parents=True)
    launcher_path.write_text(_RUN_ALL_SCRIPT, encoding="utf-8")
    (launcher_path.parent / "source").mkdir()
    (launcher_path.parent / "source.sha256").write_text("fixture  run-all.sh\n", encoding="utf-8")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifact_sha256"]["run_all"] = hashlib.sha256(launcher_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return hashlib.sha256(manifest_path.read_bytes()).hexdigest(), launcher_path


@dataclass(frozen=True)
class _PaidEnv:
    """A disposable admitted manifest, index, and locked launcher snapshot ready for ``run_paid``."""

    agentic: Any
    tmp_path: Path
    manifest_path: Path
    index_path: Path
    run_dir: Path
    approval: str
    launcher_path: Path

    def run(self, **overrides: Any) -> Any:
        """Call ``run_paid`` with the locked fixture defaults, replacing any keyword through ``overrides``."""
        kwargs = {
            "repo_path": self.tmp_path,
            "index_path": self.index_path,
            "auth_source": self.tmp_path / "auth-not-read.json",
            "approval_sha256": self.approval,
            "run_dir": self.run_dir,
            "manifest_path": self.manifest_path,
            "runner_factory": _FixtureRunner,
            "invocation_launcher_path": self.launcher_path,
        }
        return self.agentic.run_paid(**{**kwargs, **overrides})


@pytest.fixture(name="paid")
def _paid(agentic: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _PaidEnv:
    """Prepare an admitted manifest with a locked launcher snapshot and runtime validation bypassed."""
    manifest_path, _approval, index_path, _launcher_path = _prepare_paid_fixture(agentic, monkeypatch, tmp_path)
    run_dir = tmp_path / "result"
    approval, launcher_path = _lock_run_launcher(manifest_path, run_dir)
    return _PaidEnv(agentic, tmp_path, manifest_path, index_path, run_dir, approval, launcher_path)


def test_checksum_refresh_excludes_archived_source_tree(agentic: Any, tmp_path: Path) -> None:
    """Keep archived source validation separate from recurring result checksums.

    Scenario: a run directory holds the launcher, its source manifest, the archived source tree, and telemetry; the
    refreshed checksum file lists the first three kinds but never descends into the immutable source tree.
    """
    run_dir = tmp_path / "result"
    source_path = run_dir / ".launcher" / "source" / "benchmarks" / "runner.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("snapshot\n", encoding="utf-8")
    (run_dir / ".launcher" / "run-all.sh").write_text(_RUN_ALL_SCRIPT, encoding="utf-8")
    (run_dir / ".launcher" / "source.sha256").write_text("fixture  runner.py\n", encoding="utf-8")
    (run_dir / "telemetry.jsonl").write_text("{}\n", encoding="utf-8")

    agentic._write_checksums(run_dir)

    entries = (run_dir / "checksums.sha256").read_text(encoding="utf-8")
    assert ".launcher/run-all.sh" in entries
    assert ".launcher/source.sha256" in entries
    assert "telemetry.jsonl" in entries
    assert ".launcher/source/benchmarks/runner.py" not in entries


class TestPaidRun:
    """Paid orchestration persists every artifact it produced, even on interruption, and checks its launcher."""

    def test_persists_full_scope_and_noncompliant_c_row(
        self, agentic: Any, paid: _PaidEnv, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Paid orchestration retains C no-call evidence while marking it treatment-ineligible.

        Scenario: a fixture runner completes every assigned cell, with each C_strict row non-adherent. Telemetry,
        metadata, summary, checksums, and the console output must all describe the full scope, and the run log must be
        byte-identical to what was printed.
        """
        paid.run()

        run_dir = paid.run_dir
        rows = [json.loads(line) for line in (run_dir / "telemetry.jsonl").read_text(encoding="utf-8").splitlines()]
        metadata = json.loads((run_dir / "run-metadata.json").read_text(encoding="utf-8"))
        assert len(rows) == AGENTIC_CELLS
        assert metadata["status"] == "completed"
        assert metadata["execution"]["comparability_scope"]["repetitions_limit"].startswith("variance screening")
        # Selected rather than taken from the end: arm order is counterbalanced, so the last persisted row is not
        # reliably the C row this scenario makes noncompliant.
        final_strict_row = next(row for row in reversed(rows) if row["arm"] == "C_strict")
        assert final_strict_row["treatment_adherence"] is False
        assert (run_dir / "telemetry-canonical.jsonl").is_file()
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        assert summary["all_assigned"]["C_strict"]["cells"] == len(AGENTIC_TASK_IDS)
        assert summary["all_assigned"]["A_plain"]["passed_cells"] == 0
        assert summary["comparisons"]["B_auto"]["diagnostic_only"] is True
        assert (run_dir / "checksums.sha256").is_file()
        output = capsys.readouterr().out
        assert f"(1/{AGENTIC_CELLS}) ✗  BA-01" in output
        assert "component=n/a" in output
        assert "SUMMARY  cohort=all_assigned  C_strict" in output
        assert f"SUMMARY  status=completed  persisted_cells={AGENTIC_CELLS}/{AGENTIC_CELLS}" in output
        assert (run_dir / "run.log").read_text(encoding="utf-8") == output

    def test_preserves_partial_artifacts_on_failure(self, paid: _PaidEnv) -> None:
        """A later runner failure cannot erase previously appended agentic evidence.

        Scenario: the runner completes one cell and then raises; the one persisted row stays on disk, the metadata
        records the failure, and checksums are still written.
        """
        with pytest.raises(RuntimeError, match="fixture interruption"):
            paid.run(runner_factory=lambda **kwargs: _FixtureRunner(fail_after=1, **kwargs))

        run_dir = paid.run_dir
        assert len((run_dir / "telemetry.jsonl").read_text(encoding="utf-8").splitlines()) == 1
        assert json.loads((run_dir / "run-metadata.json").read_text(encoding="utf-8"))["status"] == "failed"
        assert (run_dir / "checksums.sha256").is_file()

    @pytest.mark.parametrize(
        ("runner_factory", "error"),
        [
            pytest.param(
                lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("fixture factory interruption")),
                "fixture factory interruption",
                id="factory-initialization",
            ),
            pytest.param(_SnapshotFailureRunner, "fixture snapshot interruption", id="snapshot-initialization"),
        ],
    )
    def test_persists_failed_artifact_when_initialization_fails(
        self, paid: _PaidEnv, runner_factory: Any, error: str
    ) -> None:
        """Runner and snapshot initialization failures leave a complete failed artifact.

        Scenario: the failure happens before any cell starts, so no row exists; the summary must still expose the full
        assigned denominator as unobserved and the log must state zero persisted cells.
        """
        with pytest.raises(RuntimeError, match=error):
            paid.run(runner_factory=runner_factory)

        run_dir = paid.run_dir
        metadata = json.loads((run_dir / "run-metadata.json").read_text(encoding="utf-8"))
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        unobserved = {
            "assigned_cells": len(AGENTIC_TASK_IDS),
            "passed_cells": 0,
            "unobserved_cells": len(AGENTIC_TASK_IDS),
            "component_mean": None,
        }
        assert metadata["status"] == "failed"
        assert metadata["persisted_cells"] == 0
        assert {arm: {key: values[key] for key in unobserved} for arm, values in summary["all_assigned"].items()} == {
            arm: unobserved for arm in AGENTIC_ARMS
        }
        assert (run_dir / "telemetry.jsonl").read_text(encoding="utf-8") == ""
        assert f"SUMMARY  status=failed  persisted_cells=0/{AGENTIC_CELLS}" in (run_dir / "run.log").read_text(
            encoding="utf-8"
        )
        assert (run_dir / "checksums.sha256").is_file()

    def test_rejects_a_changed_launcher(self, paid: _PaidEnv) -> None:
        """A paid invocation cannot proceed when its launcher differs from the agentic lock.

        Scenario: the launcher bytes are edited after the manifest locked their digest, so the run must be refused
        before any cell executes.
        """
        paid.launcher_path.write_text("changed\n", encoding="utf-8")

        with pytest.raises(ValueError, match="invocation launcher changed"):
            paid.run()


def _stage_new_directory(root: Path) -> tuple[Path, Path]:
    """Return a not-yet-created run directory and an unrelated launcher file outside it."""
    launcher = root / "launcher.sh"
    launcher.write_text(_RUN_ALL_SCRIPT, encoding="utf-8")
    return root / "new", launcher


def _stage_launcher_only(root: Path) -> tuple[Path, Path]:
    """Return a run directory holding only its locked launcher, without a source snapshot."""
    run_dir = root / "launcher"
    launcher = run_dir / ".launcher" / "run-all.sh"
    launcher.parent.mkdir(parents=True)
    launcher.write_text(_RUN_ALL_SCRIPT, encoding="utf-8")
    return run_dir, launcher


def _stage_snapshot(root: Path) -> tuple[Path, Path]:
    """Return a run directory holding the locked launcher, a source directory, and a source manifest file."""
    run_dir, launcher = _stage_launcher_only(root)
    (launcher.parent / "source").mkdir()
    (launcher.parent / "source.sha256").write_text("fixture  run-all.sh\n", encoding="utf-8")
    return run_dir, launcher


def _stage_source_manifest_directory(root: Path) -> tuple[Path, Path]:
    """Return a snapshot whose source manifest is a directory instead of a regular file."""
    run_dir, launcher = _stage_launcher_only(root)
    (launcher.parent / "source").mkdir()
    (launcher.parent / "source.sha256").mkdir()
    return run_dir, launcher


def _stage_reused_artifact(root: Path, artifact_name: str) -> tuple[Path, Path]:
    """Return an existing run directory that already holds an old evidence artifact, plus an outside launcher."""
    _run_dir, launcher = _stage_new_directory(root)
    run_dir = root / "artifact"
    run_dir.mkdir()
    (run_dir / artifact_name).write_text("old evidence\n", encoding="utf-8")
    return run_dir, launcher


class TestRunDirectoryAdmission:
    """Run-all may pre-create only its locked launcher and source snapshot; every other reuse is rejected."""

    def test_admits_the_locked_launcher_and_source_snapshot(self, agentic: Any, tmp_path: Path) -> None:
        """A run directory holding exactly the locked launcher and its source snapshot is admitted.

        Scenario: run-all pre-creates `.launcher/run-all.sh`, a `source` directory, and a `source.sha256` file; the
        runner must accept that layout and keep the directory.
        """
        run_dir, launcher = _stage_snapshot(tmp_path)
        launcher_hash = hashlib.sha256(launcher.read_bytes()).hexdigest()

        agentic._admit_run_directory(run_dir, launcher, launcher_hash)

        assert run_dir.is_dir()

    @pytest.mark.parametrize(
        "stage",
        [
            pytest.param(_stage_new_directory, id="new-directory-without-launcher"),
            pytest.param(_stage_launcher_only, id="launcher-without-source-snapshot"),
            pytest.param(_stage_source_manifest_directory, id="source-manifest-must-be-a-regular-file"),
            pytest.param(lambda root: _stage_reused_artifact(root, "telemetry.jsonl"), id="reused-telemetry-artifact"),
            pytest.param(
                lambda root: _stage_reused_artifact(root, ".agentic-console.log"), id="reused-console-artifact"
            ),
        ],
    )
    def test_rejects_every_other_layout(self, agentic: Any, tmp_path: Path, stage: Any) -> None:
        """A run directory that is not exactly a locked launcher plus source snapshot is rejected.

        Scenario: a missing launcher, a launcher without its source snapshot, a source manifest that is a directory,
        or a directory already holding old evidence must never be silently reused for a new paid run.
        """
        run_dir, launcher = stage(tmp_path)
        launcher_hash = hashlib.sha256(launcher.read_bytes()).hexdigest()

        with pytest.raises(FileExistsError):
            agentic._admit_run_directory(run_dir, launcher, launcher_hash)


class TestInputSnapshot:
    """Paid input snapshots archive their own runner, clean every disposable home, and keep failure evidence."""

    @pytest.fixture(name="archived")
    def _archived(self, agentic: Any, tmp_path: Path) -> dict[str, Any]:
        """Write an agentic input snapshot under ``tmp_path / "inputs"`` and return its payload."""
        return agentic._write_agentic_input_snapshot(
            tmp_path / "inputs",
            manifest_path=agentic._MANIFEST_PATH,
            tasks_path=agentic._TASKS_PATH,
            runner_path=Path(agentic.__file__),
            invocation_launcher_path=BENCHMARKS_DIR / "run-all.sh",
            index_path=agentic._MANIFEST_PATH,
            auth_source=None,
            arm_archives={},
            arm_files={},
        )

    def test_records_its_own_runner_identity(self, agentic: Any, archived: dict[str, Any], tmp_path: Path) -> None:
        """Agentic bytes are archived under their real name, role, and immutable hash.

        Scenario: the snapshot must list the agentic runner itself, never the structural runner it wraps, so a replay
        can prove which bytes produced the study.
        """
        runner_path = Path(agentic.__file__)

        runner_entries = [entry for entry in archived["files"] if entry["role"] == "agentic_runner"]

        assert runner_entries == [
            {
                "role": "agentic_runner",
                "archived_path": "shared/run-codex-agentic.py",
                "sha256": hashlib.sha256(runner_path.read_bytes()).hexdigest(),
                "bytes": runner_path.stat().st_size,
                "mode": 0o600,
            }
        ]
        assert not (tmp_path / "inputs" / "shared" / "run-codex-structural.py").exists()

    @pytest.mark.parametrize("name", ["agentic_reporting", "agentic_contracts"])
    def test_archives_shared_modules_byte_for_byte(self, archived: dict[str, Any], tmp_path: Path, name: str) -> None:
        """Each shared reporting module is archived with its source bytes and recorded under its own role.

        Scenario: the study reads these modules at replay time, so the archived copy must be identical to the source
        and the recorded digest must describe the source bytes.
        """
        source = BENCHMARKS_DIR / "_bench_common" / f"{name}.py"
        archived_copy = tmp_path / "inputs" / "shared" / f"{name}.py"

        entry = next(entry for entry in archived["files"] if entry["role"] == name)

        assert archived_copy.read_bytes() == source.read_bytes()
        assert entry["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()

    @POSIX_SECURITY
    def test_first_strict_admission_failure_keeps_identity_evidence_after_cleanup(
        self, agentic: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A first C_strict admission failure persists identities before home cleanup.

        Scenario: plugin installation stages observable identities and then fails admission. The private evidence file
        must keep the expected and observed identities after the staged homes are removed, no model call may start, and
        a second snapshot into the same run directory must be refused without installing again.
        """
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "artifact_sha256": {
                        "codemap_candidate_manifest": "codemap-locked",
                        "codex_rig_plugin_manifest": "rig-locked",
                    },
                    "codemap_candidate": {"version": "0.27.0"},
                    "codex_rig_candidate": {"version": "0.4.0"},
                }
            ),
            encoding="utf-8",
        )
        marketplace_root = tmp_path / "marketplace"
        marketplace_root.mkdir()
        install_calls: list[str] = []
        model_calls: list[str] = []
        adapter = agentic._structural.CodexRunner(
            "gpt-6.1-sol",
            tmp_path,
            manifest_path=manifest_path,
            marketplace_root=marketplace_root,
            transport=lambda *_args, **_kwargs: model_calls.append("model") or "",
        )
        runner = _bare_runner(agentic, adapter, tmp_path)

        def _fail_after_staging(home: Any, *_args: Any, **_kwargs: Any) -> bool:
            """Stage observable plugin identities, then fail their admission."""
            install_calls.append(home.arm)
            for name, version in (("codemap-py", "0.27.0"), ("codex-rig", "0.4.0")):
                plugin = home.path / "plugins" / name
                (plugin / ".codex-plugin").mkdir(parents=True)
                (plugin / ".codex-plugin" / "plugin.json").write_text(
                    json.dumps({"name": name, "version": version}), encoding="utf-8"
                )
                setattr(home, "codemap_plugin_path" if name == "codemap-py" else "codex_rig_path", plugin)
            raise RuntimeError("fixture plugin identity mismatch")

        monkeypatch.setattr(agentic, "AGENTIC_ARMS", ("C_strict",))
        monkeypatch.setattr(codex_diff_impact, "_validate_locked_runtime", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(codex_provisioning, "_install_codemap_plugin", _fail_after_staging)

        with pytest.raises(RuntimeError, match="fixture plugin identity mismatch"):
            runner.create_input_snapshot(
                run_dir, manifest_path=manifest_path, invocation_launcher_path=BENCHMARKS_DIR / "run-all.sh"
            )

        evidence_path = run_dir / "runtime-isolation.jsonl"
        assert evidence_path.stat().st_mode & 0o777 == 0o600
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        assert evidence["arm"] == "C_strict"
        assert evidence["error"] == "fixture plugin identity mismatch"
        assert evidence["expected_plugin_identities"]["codemap-py"]["version"] == "0.27.0"
        assert evidence["observed_plugin_identities"]["codex-rig"]["version"] == "0.4.0"
        assert install_calls == ["C_strict"]
        assert model_calls == []
        assert not any(tmp_path.rglob("plugin.json"))

        with pytest.raises(FileExistsError):
            runner.create_input_snapshot(
                run_dir, manifest_path=manifest_path, invocation_launcher_path=BENCHMARKS_DIR / "run-all.sh"
            )
        assert install_calls == ["C_strict"]

    @mock.patch(
        "run_codex_agentic._write_agentic_input_snapshot",
        autospec=True,
        return_value={"ok": True, "path": "fixture"},
    )
    def test_attempts_every_cleanup_after_auth_refresh_failure(
        self, mock_write_snapshot: Any, agentic: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A snapshot refresh failure cannot skip coordination or disposable-home cleanup.

        Scenario: every disposable home fails its credential refresh; cleanup of each home's coordination root and the
        home itself must still run in order for all three arms before the failure is raised.
        """
        events: list[str] = []
        adapter = _SnapshotAdapter(
            lambda arm: _SnapshotHome(
                arm, tmp_path, events, auth_provisioned=True, coordination_path=tmp_path / f"coordination-{arm}"
            ),
            auth_state=_FailingAuth(events),
        )
        runner = _bare_runner(agentic, adapter, tmp_path)
        monkeypatch.setattr(
            agentic._structural, "_cleanup_coordination_root", lambda path: events.append(f"coordination:{path.name}")
        )

        with pytest.raises(RuntimeError, match="refresh failed"):
            runner.create_input_snapshot(
                tmp_path / "run",
                manifest_path=agentic._MANIFEST_PATH,
                invocation_launcher_path=BENCHMARKS_DIR / "run-all.sh",
            )

        assert events == [
            "refresh:A_plain",
            "coordination:coordination-A_plain",
            "home:A_plain",
            "refresh:B_auto",
            "coordination:coordination-B_auto",
            "home:B_auto",
            "refresh:C_strict",
            "coordination:coordination-C_strict",
            "home:C_strict",
        ]

    @mock.patch(
        "run_codex_agentic._write_agentic_input_snapshot",
        autospec=True,
        return_value={"ok": True, "path": "fixture"},
    )
    def test_cleans_a_shared_treatment_coordination_root_once(
        self, mock_write_snapshot: Any, agentic: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """B/C snapshot homes share one index-local coordination root safely.

        Scenario: the B and C homes point at the same coordination root and a second removal of an unavailable root
        raises; the root must therefore be cleaned exactly once, and the runtime sources bound for the cells must be the
        archived B and C inputs.
        """
        events: list[str] = []
        shared_root = tmp_path / ".index-rw"
        live_roots = {shared_root}
        coordination = {"A_plain": None, "B_auto": shared_root, "C_strict": shared_root}
        adapter = _SnapshotAdapter(
            lambda arm: _SnapshotHome(
                arm, tmp_path, events, auth_provisioned=False, coordination_path=coordination[arm]
            )
        )
        runner = _bare_runner(agentic, adapter, tmp_path)

        def _cleanup(path: Path) -> None:
            """Record coordination cleanup and reject repeated removal of an unavailable root."""
            events.append(f"coordination:{path.name}")
            if path not in live_roots:
                raise ValueError("Codemap coordination root is unavailable")
            live_roots.remove(path)

        monkeypatch.setattr(agentic._structural, "_cleanup_coordination_root", _cleanup)

        payload = runner.create_input_snapshot(
            tmp_path / "run",
            manifest_path=agentic._MANIFEST_PATH,
            invocation_launcher_path=BENCHMARKS_DIR / "run-all.sh",
        )

        inputs = tmp_path / "run" / "inputs"
        assert payload == {"ok": True, "path": "fixture"}
        assert events == ["home:A_plain", "coordination:.index-rw", "home:B_auto", "home:C_strict"]
        assert adapter.bound_sources == (
            inputs,
            {
                "B_auto": {"direct-cli": inputs / "B_auto" / "direct-cli"},
                "C_strict": {
                    "codemap-py": inputs / "C_strict" / "codemap-py",
                    "codex-rig": inputs / "C_strict" / "codex-rig",
                },
            },
        )


class _SnapshotHome:
    """Verified snapshot home whose cleanup is recorded in a shared event list."""

    codemap_context_path = None

    def __init__(
        self, arm: str, root: Path, events: list[str], *, auth_provisioned: bool, coordination_path: Path | None
    ) -> None:
        """Initialize the test double's fixture-controlled state."""
        self.arm = arm
        self.path = root / arm
        self.events = events
        self.auth_provisioned = auth_provisioned
        self.coordination_path = coordination_path
        self.codemap_plugin_path = root / "codemap"
        self.codex_rig_path = root / "codex-rig"

    def cleanup(self) -> None:
        """Record the home cleanup boundary."""
        self.events.append(f"home:{self.arm}")


class _RecordingAuth:
    """Credential state that records each refresh from a disposable home."""

    def __init__(self, events: list[str]) -> None:
        """Initialize the test double's fixture-controlled state."""
        self.events = events
        self.paths: list[Path] = []

    def refresh_from_home(self, path: Path) -> None:
        """Record authentication refresh through the scenario's controlled boundary."""
        self.paths.append(path)
        self.events.append(f"refresh:{path.name}")


class _FailingAuth(_RecordingAuth):
    """Credential state whose refresh always fails after being recorded."""

    def refresh_from_home(self, path: Path) -> None:
        """Record the refresh and then fail so the caller's cleanup sequence must continue."""
        super().refresh_from_home(path)
        raise RuntimeError("refresh failed")


class _SnapshotAdapter:
    """Snapshot adapter seam that hands out fixture homes and remembers the runtime sources bound for cells."""

    auth_source = None

    def __init__(self, make_home: Any, auth_state: Any = None) -> None:
        """Initialize the test double's fixture-controlled state."""
        self._make_home = make_home
        self._auth_state = auth_state
        self.bound_sources: tuple[Path, dict[str, dict[str, Path]]] | None = None

    def _bind_runtime_snapshot(self, root: Path, sources: dict[str, dict[str, Path]]) -> None:
        """Capture the runtime paths that agentic cells must reuse."""
        self.bound_sources = (root, sources)

    def _record_runtime_success(self, *_args: Any, **_kwargs: Any) -> None:
        """Accept verified identity recording at the adapter seam."""

    def _prepare_verified_home(self, native_arm: str) -> Any:
        """Return the fixture home through the runner's preparation interface."""
        return self._make_home(native_arm)


class _NativeHome:
    """Disposable home compatible with the native runner seam."""

    codemap_skill_path = None

    def __init__(self, path: Path, *, auth_provisioned: bool, coordination_path: Path | None = None) -> None:
        """Initialize the test double's fixture-controlled state."""
        self.env: dict[str, str] = {}
        self.path = path
        self.auth_provisioned = auth_provisioned
        self.coordination_path = coordination_path

    def cleanup(self) -> None:
        """Implement the test home or workspace cleanup boundary."""


class _NativeAdapter:
    """Native transport seam that returns one fixed stream without launching the Codex executable."""

    timeout = 600.0

    def __init__(self, home: _NativeHome, stream: str, auth_state: Any = None) -> None:
        """Initialize the test double's fixture-controlled state."""
        self.home = home
        self.stream = stream
        self._auth_state = auth_state

    def _prepare_verified_home(self, _arm: str) -> _NativeHome:
        """Return the fixture home through the runner's preparation interface."""
        return self.home

    def build_command(self, _prompt: str) -> list[str]:
        """Return fixed argv without launching the Codex executable."""
        return ["codex", "exec"]

    def _subprocess(self, _command: list[str], _env: dict[str, str], *, timeout: float) -> str:
        """Return the successful answer stream built from the fixture oracle."""
        del timeout
        return self.stream

    def close(self) -> None:
        """Implement the adapter cleanup boundary for the enclosing lifecycle test."""


def _bare_runner(agentic: Any, adapter: Any, tmp_path: Path) -> Any:
    """Build an agentic runner without its constructor, bound to a fixture adapter and a placeholder index."""
    runner = object.__new__(agentic.AgenticCodexRunner)
    runner.adapter = adapter
    runner.index_path = tmp_path / "index.json"
    return runner


def _native_runner(agentic: Any, adapter: Any, tmp_path: Path) -> Any:
    """Build a bare runner that can execute one native cell through a fixture adapter."""
    runner = _bare_runner(agentic, adapter, tmp_path)
    runner.repo_path = tmp_path
    runner.agentic_manifest = {}
    runner.transport = None
    return runner


class TestNativeRunner:
    """A native cell refreshes credentials, scores the answer, and turns runtime drift into an ineligible row."""

    @mock.patch("run_codex_agentic._validate_agentic_runtime", autospec=True, return_value=None)
    def test_refreshes_auth_from_the_disposable_home(
        self, mock_validate: Any, agentic: Any, task_and_truth: tuple[Any, Any], tmp_path: Path
    ) -> None:
        """Native cells propagate refreshed auth out of the disposable home after a successful run.

        Scenario: the cell answers correctly through a fixture transport; cleanup must hand the home's refreshed
        credential state back through the private auth state exactly once.
        """
        task, truth = task_and_truth
        auth = _RecordingAuth([])
        home = _NativeHome(tmp_path / "home", auth_provisioned=True)
        runner = _native_runner(
            agentic,
            _NativeAdapter(home, _stream(_message(_labelled(_ONE_IMPORTER, truth)), _completed()), auth),
            tmp_path,
        )

        result = runner.run(task, "A_plain", repetition=1, oracle=truth)

        assert result.success is True
        assert auth.paths == [home.path]

    @mock.patch(
        "run_codex_agentic._validate_agentic_runtime",
        autospec=True,
        side_effect=ValueError("locked index bytes changed"),
    )
    def test_postflight_contamination_makes_an_incomplete_row(
        self, mock_validate: Any, agentic: Any, task_and_truth: tuple[Any, Any], tmp_path: Path
    ) -> None:
        """Postflight drift turns an otherwise correct native cell into an ineligible row.

        Scenario: the runtime validation rejects the index as though its locked bytes changed after the cell ran; the
        row must be an incomplete failure typed as runtime contamination rather than a pass.
        """
        task, truth = task_and_truth
        home = _NativeHome(tmp_path / "home", auth_provisioned=True)
        runner = _native_runner(
            agentic,
            _NativeAdapter(home, _stream(_message(_labelled(_ONE_IMPORTER, truth)), _completed()), _RecordingAuth([])),
            tmp_path,
        )

        failed = runner.run(task, "A_plain", repetition=1, ground_truth=truth)

        assert failed.success is False
        assert failed.incomplete is True
        assert failed.error_type == "runtime_contamination"

    @mock.patch("run_codex_agentic._validate_agentic_runtime", autospec=True, return_value=None)
    def test_records_scratch_left_in_the_coordination_gate_beside_a_passing_cell(
        self, mock_validate: Any, agentic: Any, task_and_truth: tuple[Any, Any], tmp_path: Path
    ) -> None:
        """A cell that answers correctly still passes after writing scratch into its coordination gate.

        The gate is the only path a measured cell's sandbox may write to, so scratch there is permitted behaviour. It is
        reported on the row as an observation about how the cell worked; a paid run lost a correct answer because the
        same files were scored as an execution failure instead.
        """
        task, truth = task_and_truth
        gate = agentic._structural.prepare_coordination_root(tmp_path / "index-dir")
        (gate / "analyze_imports.py").write_text("print(1)\n", encoding="utf-8")
        home = _NativeHome(tmp_path / "home", auth_provisioned=False, coordination_path=gate)
        runner = _native_runner(
            agentic, _NativeAdapter(home, _stream(_message(_labelled(_ONE_IMPORTER, truth)), _completed())), tmp_path
        )

        result = runner.run(task, "A_plain", repetition=1, oracle=truth)

        assert result.success is True
        assert result.incomplete is False
        assert result.error_type == ""
        assert result.coordination_scratch_entries == ["analyze_imports.py"]
        assert not gate.exists()


@pytest.fixture(name="relocated_base", scope="module")
def _relocated_base(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, dict[str, Any], dict[str, str]]:
    """Build the real-git relocated-worktree fixture once; tests receive copies through ``relocated``."""
    return _relocated_worktree_index(tmp_path_factory.mktemp("relocated-index"))


@pytest.fixture(name="relocated")
def _relocated(
    relocated_base: tuple[Path, Path, dict[str, Any], dict[str, str]],
) -> tuple[Path, Path, dict[str, Any], dict[str, str]]:
    """Return the shared worktree, index path, and private copies of the manifest and relocation provenance."""
    repo, index_path, manifest, relocation = relocated_base
    return repo, index_path, copy.deepcopy(manifest), dict(relocation)


def _relocated_worktree_index(tmp_path: Path) -> tuple[Path, Path, dict[str, Any], dict[str, str]]:
    """Build a clean committed worktree holding a root-relocated copy of a frozen index.

    Args:
        tmp_path: Directory the worktree, index, and manifest are created under.

    Returns:
        The worktree root, the relocated index path, an agentic manifest locking the frozen index
        bytes, and the relocation provenance a run in that worktree would carry.
    """
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
        "frozen_index_contract": {
            "raw_sha256": hashlib.sha256(frozen_bytes).hexdigest(),
            "git_sha": commit,
            "scan_version": 3,
        },
    }
    return repo, index_path, manifest, dict(relocation)


class TestRelocatedIndexAdmission:
    """A worktree run proves index identity through relocation provenance, never byte identity."""

    def test_valid_provenance_admits_a_worktree_index(
        self, agentic: Any, relocated: tuple[Path, Path, dict[str, Any], dict[str, str]]
    ) -> None:
        """Relocation provenance admits an index whose bytes differ from the locked hash.

        Scenario: a run executes in its own worktree, so its index carries the worktree's
        ``scan_root`` and can never reproduce the manifest's locked raw hash; the provenance written
        by the relocation is what proves the graph is still the frozen one.
        """
        repo, index_path, manifest, relocation = relocated

        agentic._validate_agentic_runtime(manifest, repo, index_path, relocation)

        assert hashlib.sha256(index_path.read_bytes()).hexdigest() != relocation["frozen_index_sha256"]

    @pytest.mark.parametrize(
        ("field", "bad_digest", "match"),
        [
            pytest.param("frozen_index_sha256", "0" * 64, "wrong frozen source", id="wrong-frozen-source"),
            pytest.param(
                "derived_index_sha256", "1" * 64, "changed after relocation", id="derived-hash-misses-bytes-on-disk"
            ),
        ],
    )
    def test_inconsistent_provenance_is_rejected(
        self,
        agentic: Any,
        relocated: tuple[Path, Path, dict[str, Any], dict[str, str]],
        field: str,
        bad_digest: str,
        match: str,
    ) -> None:
        """Provenance whose frozen source is not the locked index, or whose derived hash misses the disk, is rejected.

        Scenario: a caller supplies provenance derived from some other frozen index, and admitting it would let an
        unrelated graph enter the run under the locked manifest's authority; or the relocated copy changed after its
        provenance was written, so the digest the run would attest to is no longer the index the model actually reads.
        """
        repo, index_path, manifest, relocation = relocated
        relocation[field] = bad_digest

        with pytest.raises(ValueError, match=match):
            agentic._validate_agentic_runtime(manifest, repo, index_path, relocation)

    def test_absent_provenance_keeps_the_byte_gate(
        self, agentic: Any, relocated: tuple[Path, Path, dict[str, Any], dict[str, str]]
    ) -> None:
        """Without provenance a non-locked index is still rejected on its bytes.

        Scenario: the same relocated index is offered by a run that claims no relocation; the
        original byte-identity gate must reject it exactly as before.
        """
        repo, index_path, manifest, _relocation = relocated

        with pytest.raises(ValueError, match="agentic Codex run requires the locked frozen index bytes"):
            agentic._validate_agentic_runtime(manifest, repo, index_path)


def _default_stratum(agentic: Any) -> str:
    """Return the model stratum the active agentic manifest names as its own default.

    Args:
        agentic: The loaded agentic runner module.

    Returns:
        The declared default stratum name.

    Examples:
        >>> _default_stratum(_load_agentic()).startswith("gpt-")
        True
    """
    return str(json.loads(agentic._MANIFEST_PATH.read_text(encoding="utf-8"))["model"]["name"])


class TestModelStratum:
    """A study runs one declared model stratum, and that stratum is part of its scope, approval, and metadata."""

    def test_selected_stratum_hashes_into_its_own_scope(self, agentic: Any) -> None:
        """A declared stratum other than the manifest default resolves to a scope of its own.

        Scenario: the agentic lane runs one stratum per study, and the study's identity is its scope
        hash. Leaving the stratum out of that hash would let one approval token pay for a study of any
        declared model, which is how three executions of the default model were once published as
        studies of the two strata an operator had actually named.
        """
        default = agentic.resolve_agentic_scope()
        selected = agentic.resolve_agentic_scope(model="gpt-6-luna")
        named_default = agentic.resolve_agentic_scope(model=_default_stratum(agentic))

        assert selected["scope_sha256"] != default["scope_sha256"]
        assert selected["total_cells"] == default["total_cells"]
        assert named_default["scope_sha256"] == default["scope_sha256"]

    def test_undeclared_stratum_is_refused_before_any_scope_exists(self, agentic: Any) -> None:
        """A model the manifest never declared fails instead of becoming the study's model.

        Scenario: the selection reaches the transport that spends money, so free text here would buy a
        run of whatever the operator typed. Only the manifest's own default and its declared additional
        strata may be selected.
        """
        with pytest.raises(ValueError, match="supported Codex benchmark model"):
            agentic.resolve_agentic_scope(model="gpt-5.3-codex")

    def test_paid_run_binds_the_selected_stratum_and_records_it(self, agentic: Any, paid: _PaidEnv) -> None:
        """Paid execution of another declared stratum demands that stratum's scope and records its name.

        Scenario: the run metadata is the only durable statement of which model was executed, and the
        approval is the only gate before the money is spent. A selected stratum admitted by the manifest
        digest would let the default study's token pay for it, and metadata naming the manifest default
        would publish it as the model it was not.
        """
        scope_approval = agentic.resolve_agentic_scope(paid.manifest_path, model="gpt-6-luna")["scope_sha256"]

        paid.run(approval_sha256=scope_approval, model="gpt-6-luna", scope_sha256=scope_approval)

        metadata = json.loads((paid.run_dir / "run-metadata.json").read_text(encoding="utf-8"))
        assert metadata["execution"]["model"] == "gpt-6-luna"
        assert metadata["approval_sha256"] == scope_approval
        assert paid.approval != scope_approval

    def test_paid_run_refuses_the_default_study_token_for_another_stratum(self, paid: _PaidEnv) -> None:
        """The token that admits the manifest's own stratum does not admit another one.

        Scenario: the manifest digest authorizes the study the manifest describes. Accepting it for a
        different stratum would make the approval a permission to run any declared model rather than the
        one the operator reviewed and priced.
        """
        with pytest.raises(ValueError, match="nondefault agentic scope"):
            paid.run(model="gpt-6-luna")


def _arms_by_coordinate(schedule: list[dict[str, Any]]) -> list[list[str]]:
    """Group a schedule's arms by (task, repetition) coordinate, keeping first-appearance order."""
    by_coordinate: dict[tuple[str, int], list[str]] = {}
    for coordinate in schedule:
        by_coordinate.setdefault((coordinate["task_id"], coordinate["repetition"]), []).append(coordinate["arm"])
    return list(by_coordinate.values())


class TestCyclicSchedule:
    """Cyclic scheduling balances arm order without random state or a second scheduler."""

    def test_balances_three_repetitions_and_records_each_cell(self, agentic: Any) -> None:
        """Three screening repeats put each arm in every cyclic position equally.

        A future controlled study needs order balance without random state or a second scheduler. This checks every
        coordinate instead of merely counting cells, so duplicate or fixed A/B/C ordering cannot pass by accident.
        """
        schedule = agentic._scheduled_coordinates(("BA-01", "BA-02"), repetitions=3, cyclic=True)

        assert _arms_by_coordinate(schedule) == [
            ["A_plain", "B_auto", "C_strict"],
            ["B_auto", "C_strict", "A_plain"],
            ["C_strict", "A_plain", "B_auto"],
            ["B_auto", "C_strict", "A_plain"],
            ["C_strict", "A_plain", "B_auto"],
            ["A_plain", "B_auto", "C_strict"],
        ]
        assert {coordinate["arm"] for coordinate in schedule[::3]} == set(AGENTIC_ARMS)

    def test_offsets_the_rotation_by_the_task_ordinal(self, agentic: Any) -> None:
        """A single task keeps its suite-wide rotation when scheduled on its own.

        Scenario: BA-02 is the second suite task, so its one repetition starts one position into the cycle even though
        it is the only scheduled task; rescheduling a subset must not change a cell's arm order.
        """
        schedule = agentic._scheduled_coordinates(("BA-02",), repetitions=1, cyclic=True, task_ordinals={"BA-02": 1})

        assert [coordinate["arm"] for coordinate in schedule] == ["B_auto", "C_strict", "A_plain"]


class TestSummary:
    """Telemetry summaries keep every assigned cell in the denominator and pair arms only when evidence allows."""

    def test_includes_every_unobserved_assigned_cell(self, agentic: Any) -> None:
        """No completed telemetry must still expose the manifest-sized pass denominator.

        Scenario: two tasks and two repetitions are assigned to each arm but nothing ran; every arm must report four
        assigned cells, none passed, and all four counted as unobserved.
        """
        summary = agentic._summarize_telemetry([], task_ids=["BA-01", "BA-02"], repetitions=2)

        assert summary["all_assigned"]["A_plain"]["assigned_cells"] == 4
        assert summary["all_assigned"]["A_plain"]["passed_cells"] == 0
        assert summary["all_assigned"]["A_plain"]["failure_counts"] == {"unobserved": 4}

    @pytest.mark.parametrize(
        ("success", "legacy", "grade", "assigned", "expected"),
        [
            pytest.param(True, 0.0, 56 / 57, 1, 56 / 57, id="near-correct"),
            pytest.param(False, 1.0, 1.0, 1, 0.0, id="failed-execution"),
            pytest.param(True, 1.0, 1.0, 2, 0.5, id="unobserved-cell"),
            pytest.param(True, 1.0, None, 1, None, id="unavailable-grade"),
        ],
    )
    def test_preserves_shared_granular_quality(
        self, agentic: Any, success: bool, legacy: float, grade: float | None, assigned: int, expected: float | None
    ) -> None:
        """Native telemetry must not overwrite the same shared summary consumed by Claude.

        Scenario: one native row is summarized by both the shared summarizer and the native wrapper; the quality mean
        follows the granular grade (zero for a failed execution, diluted by unobserved cells, absent when ungraded),
        the shared fields match exactly, and the native-only fields are never part of the shared ones.
        """
        row = _cell(
            "A_plain",
            task_id="synthetic-1",
            success=success,
            answer_contract_valid=True,
            answer_pooling_eligible=True,
            treatment_adherence=True,
            quality={"correct": legacy == 1.0, "quality_score": legacy, "graded_score": grade},
            input_tokens=123,
            command_calls=2,
        )
        task_ids = [f"synthetic-{number}" for number in range(1, assigned + 1)]

        shared = agentic.summarize_agentic([row], task_ids=task_ids, repetitions=1)
        summary = agentic._summarize_telemetry([row], task_ids=task_ids, repetitions=1)

        assert summary["all_assigned"]["A_plain"]["quality_mean"] == pytest.approx(expected)
        assert all(
            {key: summary["all_assigned"][arm][key] for key in values} == values
            for arm, values in shared["all_assigned"].items()
        )
        assert summary["comparisons"] == shared["comparisons"]
        assert summary["all_assigned"]["A_plain"]["gross_input_tokens"] == 123
        assert summary["all_assigned"]["A_plain"]["native_command_calls"] == 2
        assert not set(agentic._aggregate_arm_rows([row])) & set(shared["all_assigned"]["A_plain"])

    def test_separates_all_assigned_from_matched_adherent_rows(self, agentic: Any) -> None:
        """A failed C treatment never silently removes its expensive A/B comparison cells.

        Scenario: all three arms ran one cell but the strict arm is non-adherent; its cost stays in the assigned
        cohort while it earns no pass or pairing, and native command evidence is carried on the plain row.
        """
        rows = [
            _cell(
                arm,
                input_tokens=100,
                cached_input_tokens=20,
                fresh_input_tokens=80,
                output_tokens=10,
                elapsed_s=2.0,
                codemap_used=arm != "A_plain",
                treatment_adherence=arm != "C_strict",
                quality={"quality_score": 0.8},
            )
            for arm in AGENTIC_ARMS
        ]
        rows[0]["command_calls"] = 1
        rows[0]["raw_events"] = [_query(), _completed()]

        summary = agentic._summarize_telemetry(rows, task_ids=["BA-01"], repetitions=1)

        assert summary["all_assigned"]["C_strict"]["cells"] == 1
        assert summary["all_assigned"]["C_strict"]["fresh_input_tokens"] == 80
        assert summary["all_assigned"]["A_plain"]["passed_cells"] == 0
        assert summary["all_assigned"]["C_strict"]["passed_cells"] == 0
        assert summary["comparisons"]["C_strict"]["outcomes"]["both_pass"] == 0
        assert summary["all_assigned"]["A_plain"]["native_command_calls"] == 1
        assert summary["all_assigned"]["A_plain"]["captured_output_characters"] > 0
        assert summary["all_assigned"]["A_plain"]["max_concurrent_commands"] is None
        assert summary["comparisons"]["B_auto"]["diagnostic_only"] is True

    def test_a_c_pairing_does_not_depend_on_optional_b_adherence(self, agentic: Any) -> None:
        """An optional-use canary failure cannot erase an otherwise valid A/C comparison.

        Scenario: B_auto fails its adherence canary while A and C both pass; the A/C pair must still count as a joint
        pass with paired efficiency, and only B's own pass count drops to zero.
        """
        rows = [
            _cell(
                arm,
                input_tokens=10,
                cached_input_tokens=0,
                fresh_input_tokens=10,
                output_tokens=1,
                elapsed_s=1.0,
                codemap_used=arm != "A_plain",
                success=True,
                answer_contract_valid=True,
                answer_pooling_eligible=True,
                treatment_adherence=arm != "B_auto",
                quality={"correct": True, "quality_score": 1.0},
            )
            for arm in AGENTIC_ARMS
        ]

        summary = agentic._summarize_telemetry(rows, task_ids=["BA-01"], repetitions=1)

        assert summary["comparisons"]["C_strict"]["outcomes"]["both_pass"] == 1
        assert summary["comparisons"]["C_strict"]["efficiency"]["input_tokens"]["paired_cells"] == 1
        assert summary["all_assigned"]["A_plain"]["observed_cells"] == 1
        assert summary["all_assigned"]["C_strict"]["observed_cells"] == 1
        assert summary["all_assigned"]["B_auto"]["passed_cells"] == 0


class TestMissingNativeUsage:
    """A correct answer with unusable usage stays a pass but can never claim a token saving."""

    @pytest.fixture(
        name="measured",
        params=[
            pytest.param(None, id="absent"),
            pytest.param({}, id="empty"),
            pytest.param({"input_tokens": -1, "output_tokens": 0}, id="malformed"),
        ],
    )
    def _measured(
        self, request: pytest.FixtureRequest, agentic: Any, task_and_truth: tuple[Any, Any], tmp_path: Path
    ) -> tuple[Any, dict[str, Any]]:
        """Parse a correct answer whose terminal event carries unusable usage and persist it as a telemetry row."""
        task, oracle = task_and_truth
        answer = "BEGIN_ANSWER_JSON\n" + json.dumps(dict(oracle.expected)) + "\nEND_ANSWER_JSON"
        result = agentic.parse_agentic_stream(
            _stream(_message(answer), {"type": "turn.completed", "usage": request.param}),
            arm="A_plain",
            task=task,
            oracle=oracle,
        )
        return result, _append_row(agentic, result, tmp_path, execution_index=0)

    def test_row_is_a_pass_without_token_measurement(self, measured: tuple[Any, dict[str, Any]]) -> None:
        """A correct answer with unusable usage remains pass but records no token counts.

        Scenario: absent, empty, and negative usage payloads all mark the usage incomplete while the answer still
        passes, so the persisted tokens are null rather than zero.
        """
        result, row = measured

        assert result.quality.correct is True
        assert result.usage_complete is False
        assert row["passed"] is True
        assert row["input_tokens"] is None
        assert row["fresh_input_tokens"] is None

    def test_pairing_reports_efficiency_as_unavailable(
        self, agentic: Any, measured: tuple[Any, dict[str, Any]]
    ) -> None:
        """Pairing a row without usage against a measured control yields no efficiency measurement.

        Scenario: the control has complete usage and the treatment has none; both pass, but the pair counts as
        unavailable and no median saving is reported.
        """
        _result, row = measured
        control = {
            **row,
            "usage_complete": True,
            "input_tokens": 100,
            "cached_input_tokens": 20,
            "fresh_input_tokens": 80,
            "output_tokens": 10,
        }
        treatment = {**row, "arm": "C_strict"}

        summary = agentic._summarize_telemetry([control, treatment], task_ids=[row["task_id"]], repetitions=1)

        comparison = summary["comparisons"]["C_strict"]
        assert comparison["outcomes"]["both_pass"] == 1
        assert comparison["efficiency"]["input_tokens"]["paired_cells"] == 0
        assert comparison["efficiency"]["input_tokens"]["unavailable_pairs"] == 1
        assert comparison["efficiency"]["input_tokens"]["median_percent_change"] is None


class TestDiagnosticReplay:
    """Replay derives a new immutable diagnostic from historical telemetry without touching the source."""

    def test_preserves_original_row_and_corrects_only_observation(
        self, agentic: Any, tmp_path: Path, replay_output: Path
    ) -> None:
        """Replay creates a new immutable diagnostic without changing historical adherence or costs.

        Scenario: a historical strict row recorded no Codemap use although its raw events show a query through the
        recorded launcher; replay corrects only the observed-use flag and records the source and detector hashes.
        """
        launcher = tmp_path / "installed" / "bin" / "codemap-py"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        source = tmp_path / "telemetry.jsonl"
        original = _cell(
            "C_strict",
            input_tokens=123,
            cached_input_tokens=23,
            output_tokens=45,
            treatment_adherence=False,
            answer_contract_valid=True,
            codemap_used=False,
            raw_events=[_launcher_query(str(launcher)), _completed()],
            launcher_path=str(launcher),
        )
        _write_jsonl(source, original)

        agentic.replay_diagnostic(source, replay_output)

        replay = json.loads(replay_output.read_text(encoding="utf-8"))
        corrected = replay["rows"][0]
        assert corrected["original"] == original
        assert corrected["corrected_observed_use"] is True
        assert corrected["original"]["treatment_adherence"] is False
        assert corrected["original"]["input_tokens"] == 123
        assert replay["source"]["telemetry_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
        assert (
            replay["detector"]["sha256"]
            == hashlib.sha256((BENCHMARKS_DIR / "_bench_codex" / "runtime.py").read_bytes()).hexdigest()
        )

    def test_preserves_windows_launcher_text_on_non_windows_hosts(
        self, agentic: Any, tmp_path: Path, replay_output: Path
    ) -> None:
        """Replay preserves a Windows launcher string instead of applying the host path syntax.

        Scenario: a historical row recorded a `C:\\` launcher; on a POSIX host the string must still be matched against
        the command and reported as a per-cell recording, not parsed as a relative host path.
        """
        launcher = r"C:\agentic\bin\codemap-py"
        source = tmp_path / "telemetry.jsonl"
        _write_jsonl(
            source, _cell("C_strict", launcher_path=launcher, raw_events=[_launcher_query(launcher), _completed()])
        )

        agentic.replay_diagnostic(source, replay_output)

        corrected = json.loads(replay_output.read_text(encoding="utf-8"))["rows"][0]
        assert corrected["corrected_observed_use"] is True
        assert corrected["launcher_provenance"] == "recorded_per_cell"

    @pytest.mark.parametrize("launcher", REPLAY_LAUNCHERS)
    def test_uses_reviewed_coordinate_map_without_relabeling_a_rows(
        self, agentic: Any, tmp_path: Path, replay_output: Path, launcher: str
    ) -> None:
        """Legacy absolute commands require a reviewed per-cell path while A needs none.

        Scenario: the strict row recorded only a relative launcher but queried through an absolute one, so a reviewed
        coordinate map supplies the absolute path and its evidence; the plain row maps to no launcher and must stay
        unused rather than being relabeled.
        """
        source = tmp_path / "telemetry.jsonl"
        _write_jsonl(
            source,
            _cell(
                "C_strict", launcher_path="relative/codemap-py", raw_events=[_launcher_query(launcher), _completed()]
            ),
            _cell("A_plain", raw_events=[_completed()]),
        )
        launcher_map = tmp_path / "launchers.json"
        launcher_map.write_text(
            json.dumps(
                {
                    "coordinates": [
                        _cell("C_strict", launcher_path=launcher, source_evidence={"frozen_skill_sha256": "a" * 64}),
                        _cell("A_plain", launcher_path=None),
                    ]
                }
            ),
            encoding="utf-8",
        )

        agentic.replay_diagnostic(source, replay_output, launcher_map_path=launcher_map)

        replay = json.loads(replay_output.read_text(encoding="utf-8"))
        assert replay["rows"][0]["corrected_observed_use"] is True
        assert replay["rows"][0]["launcher_provenance"] == {
            "launcher_path": launcher,
            "source_evidence": {"frozen_skill_sha256": "a" * 64},
        }
        assert replay["rows"][1]["corrected_observed_use"] is False
        assert replay["rows"][1]["original"]["arm"] == "A_plain"
        assert (
            replay["source"]["coordinate_launcher_map"]["sha256"]
            == hashlib.sha256(launcher_map.read_bytes()).hexdigest()
        )

    @pytest.mark.parametrize("launcher", REPLAY_LAUNCHERS)
    def test_marks_relative_launcher_without_map_unavailable(
        self, agentic: Any, tmp_path: Path, replay_output: Path, launcher: str
    ) -> None:
        """A relative recorded launcher cannot be mistaken for reviewed absolute provenance.

        Scenario: the row recorded `relative/codemap-py` yet queried through an absolute launcher and no reviewed map is
        supplied; observed use must stay unknown with an explicit unavailable status.
        """
        source = tmp_path / "telemetry.jsonl"
        _write_jsonl(
            source,
            _cell(
                "C_strict", launcher_path="relative/codemap-py", raw_events=[_launcher_query(launcher), _completed()]
            ),
        )

        agentic.replay_diagnostic(source, replay_output)

        row = json.loads(replay_output.read_text(encoding="utf-8"))["rows"][0]
        assert row["corrected_observed_use"] is None
        assert row["corrected_observed_use_status"] == "unavailable_launcher_provenance"
        assert row["launcher_provenance"] is None

    def test_rejects_output_inside_historical_run(self, agentic: Any, tmp_path: Path) -> None:
        """Derived replay data cannot be inserted into the immutable source directory.

        Scenario: the requested output sits beside the historical telemetry file; it must be refused before anything
        is written.
        """
        source = tmp_path / "telemetry.jsonl"
        source.write_text("{}\n", encoding="utf-8")

        with pytest.raises(ValueError, match="outside the historical telemetry directory"):
            agentic.replay_diagnostic(source, tmp_path / "observed-use-replay.json")

    def test_launcher_map_requires_object_evidence_and_known_coordinate_keys(
        self, agentic: Any, tmp_path: Path
    ) -> None:
        """Reviewed absolute provenance cannot be a truthy placeholder or carry ignored fields.

        Scenario: a map entry supplies a string instead of an evidence object; loading must refuse it as invalid
        coordinate provenance.
        """
        map_path = tmp_path / "launchers.json"
        map_path.write_text(
            json.dumps(
                {
                    "coordinates": [
                        _cell("C_strict", launcher_path="/private/tmp/codemap-py", source_evidence="reviewed")
                    ]
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="invalid coordinate provenance"):
            agentic._load_replay_launcher_map(map_path)


class TestImpactCli:
    """The impact CLI accepts only its own scope controls and binds auth outside the model environment."""

    @pytest.fixture(name="stage_kwargs")
    def _stage_kwargs(self, agentic: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
        """Capture the keywords of the provider-stage boundary without creating a fixture or invoking Codex."""
        observed: dict[str, object] = {}
        monkeypatch.setattr(agentic, "run_change_impact_stage", lambda **kwargs: observed.update(kwargs))
        return observed

    def test_passes_native_factory_and_private_auth_to_the_paid_stage(
        self, agentic: Any, stage_kwargs: dict[str, object], tmp_path: Path
    ) -> None:
        """A paid impact run forwards its scope controls and binds the private auth source in the factory.

        Scenario: every scope control reaches the stage unchanged, and the auth path is reachable only through the
        runtime factory rather than any model-visible argument.
        """
        auth_source = tmp_path / "auth.json"

        agentic.main(
            study="change-impact",
            model="gpt-6-luna",
            timeout=73,
            run_dir=tmp_path / "result",
            paid_approval="a" * 16,
            scope_sha256="b" * 64,
            auth_source=auth_source,
        )

        assert stage_kwargs["provider"] == "codex"
        assert stage_kwargs["model"] == "gpt-6-luna"
        assert stage_kwargs["timeout"] == 73
        assert stage_kwargs["output_dir"] == tmp_path / "result"
        assert stage_kwargs["paid_approval"] == "a" * 16
        assert stage_kwargs["scope_sha256"] == "b" * 64
        assert callable(stage_kwargs["runtime_factory"])
        assert stage_kwargs["runtime_factory"].keywords["auth_source"] == auth_source

    def test_dry_run_defaults_to_the_manifest_model_without_auth(
        self, agentic: Any, stage_kwargs: dict[str, object]
    ) -> None:
        """A dry run without a model uses the manifest default and binds no auth source."""
        agentic.main(study="change-impact", dry_run=True, timeout=600)

        assert stage_kwargs["model"] == "gpt-6.1-sol"
        assert stage_kwargs["runtime_factory"].keywords["auth_source"] is None

    def test_rejects_a_boolean_timeout(
        self, agentic: Any, stage_kwargs: dict[str, object], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A boolean is not a timeout: it exits with a positive-integer message instead of reaching the stage.

        Scenario: ``True`` is an ``int`` in Python, so a naive check would accept it as a one-second timeout.
        """
        with pytest.raises(SystemExit):
            agentic.main(study="change-impact", dry_run=True, timeout=True)

        assert "positive integer" in capsys.readouterr().err
        assert stage_kwargs == {}

    def test_rejects_an_unadmitted_model_when_resolving_scope(
        self, agentic: Any, stage_kwargs: dict[str, object], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Resolving the scope of an undeclared model exits with the supported-model message."""
        with pytest.raises(SystemExit):
            agentic.main(study="change-impact", resolve_scope=True, model="unadmitted-model")

        assert "supported Codex benchmark model" in capsys.readouterr().err
        assert stage_kwargs == {}
