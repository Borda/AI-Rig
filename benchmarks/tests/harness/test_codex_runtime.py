"""No-model contracts for the shared Codex runtime lifecycle.

Covers the Codex JSONL stream parser (query attribution, fallback counting, usage coercion), the provider-neutral paid
stage lifecycle (ordering, durable per-cell persistence, failure and interrupt finalization), the checksum ledger, and
the unified progress counter. All callbacks are in-memory recorders, so no provider or paid runner starts.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

BENCHMARKS = Path(__file__).resolve().parent.parent.parent

_QUERY_COMMAND = "/bin/zsh -lc '\"$CODEMAP_BIN\" query --compact symbol Example.method'"
_QUERY_OUTPUT = '{"index":{"query_complete":true,"compact":true}}'
_TERMINAL = json.dumps({"type": "turn.completed", "status": "completed"})


@pytest.fixture(name="runtime", scope="module")
def _runtime() -> Any:
    """Load and register the private runtime once, without starting a provider or paid runner.

    >>> getfixture("runtime").__name__
    'benchmarks_codex_runtime'
    """
    if str(BENCHMARKS) not in sys.path:
        sys.path.insert(0, str(BENCHMARKS))
    spec = importlib.util.spec_from_file_location(
        "benchmarks_codex_runtime", BENCHMARKS / "_bench_codex" / "runtime.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(name="lifecycle", scope="module")
def _lifecycle() -> Any:
    """Import the provider-neutral lifecycle library once, without executing a stage.

    >>> getfixture("lifecycle").__name__
    '_bench_common.paid_lifecycle'
    """
    if str(BENCHMARKS) not in sys.path:
        sys.path.insert(0, str(BENCHMARKS))
    from _bench_common import paid_lifecycle

    return paid_lifecycle


@pytest.fixture(name="events")
def _events() -> list[tuple[str, Any]]:
    """Return an empty list that the recording lifecycle callbacks append their events to."""
    return []


def _callbacks(
    lifecycle: Any,
    events: list[tuple[str, Any]],
    *,
    fail_at: tuple[str, str] | None = None,
    invalid_at: tuple[str, str] | None = None,
) -> Any:
    """Build event-recording callbacks with optional failure coordinates for no-model execution.

    >>> events = []
    >>> callbacks = _callbacks(getfixture("lifecycle"), events, fail_at=("bad", "A_plain"))
    >>> callbacks.run_cell("example", "A_plain")
    {'task_id': 'example', 'arm': 'A_plain'}
    >>> events
    [('run', ('example', 'A_plain'))]
    >>> callbacks.run_cell("bad", "A_plain")
    Traceback (most recent call last):
        ...
    RuntimeError: fixture cell failed
    """

    def _run_cell(task: str, arm: str) -> dict[str, str]:
        """Record the attempted cell, raising at the configured coordinate before returning its row."""
        events.append(("run", (task, arm)))
        if (task, arm) == fail_at:
            raise RuntimeError("fixture cell failed")
        return {"task_id": task, "arm": arm}

    def _validate_row(task: str, arm: str, row: dict[str, str]) -> None:
        """Record a copy of the row and reject the configured invalid cell."""
        events.append(("validate", (task, arm, dict(row))))
        if (task, arm) == invalid_at:
            raise ValueError("fixture row rejected")

    def _persist_metadata(path: Path, metadata: dict[str, Any]) -> None:
        """Record lifecycle progress and replace the metadata file with deterministic JSON."""
        events.append(("metadata", (metadata["status"], metadata["persisted_cells"])))
        path.write_text(json.dumps(metadata, sort_keys=True), encoding="utf-8")

    return lifecycle.PaidStageCallbacks(
        run_cell=_run_cell,
        validate_row=_validate_row,
        prepare_run=lambda run_dir: events.append(("prepare", run_dir)),
        persist_metadata=_persist_metadata,
        emit_lifecycle=lambda event, values: events.append((event, dict(values))),
        emit_row=lambda row, completed, total, arm: events.append(("row", (dict(row), completed, total, arm))),
        write_checksums=lambda run_dir: events.append(("checksums", run_dir)),
        close_adapter=lambda: events.append(("closed", None)),
    )


def _native_command(command: str, *, exit_code: int = 0, output: str = "", item_id: str = "cmd") -> str:
    """Serialize a native command event, deriving failure status from its exit code.

    >>> item = json.loads(_native_command("example", exit_code=2, output="failed"))["item"]
    >>> item["status"], item["exit_code"], item["aggregated_output"]
    ('failed', 2, 'failed')
    """
    return json.dumps(
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "id": item_id,
                "command": command,
                "aggregated_output": output,
                "status": "completed" if exit_code == 0 else "failed",
                "exit_code": exit_code,
            },
        }
    )


def _stream(*lines: str) -> str:
    """Join JSONL event lines into the newline-delimited stream a Codex run emits.

    >>> _stream("a", "b")
    'a\\nb'
    """
    return "\n".join(lines)


class TestParseCodexJsonl:
    """Attribute Codemap queries, count fallbacks and coerce usage from a Codex JSONL stream."""

    def test_fallback_counts_search_and_read_items_but_not_unrelated_commands(self, runtime: Any) -> None:
        """Fallback means the agent searched by hand, not that any command ran.

        After a failed Codemap query the agent runs a search, a ``git status`` and a test run. Only the search is manual
        discovery, so the fallback counter must be one while all four commands still count as calls.
        """
        stream = _stream(
            _native_command(_QUERY_COMMAND, exit_code=1, item_id="failed-query"),
            _native_command("/bin/zsh -lc 'rg \"pkg.core\" src'", item_id="search"),
            _native_command("/bin/zsh -lc 'git status --short'", item_id="unrelated"),
            _native_command("/bin/zsh -lc 'pytest tests/test_core.py'", item_id="tests"),
            _TERMINAL,
        )

        parsed = runtime.parse_codex_jsonl(stream)

        assert parsed.codemap_errors == 1
        assert parsed.command_calls == 4
        assert parsed.fallback_calls == 1

    def test_legacy_assistant_tool_use_block_attributes_a_codemap_query(self, runtime: Any) -> None:
        """The compatibility path credits a Codemap call made through an assistant ``tool_use`` block.

        Older streams carry the command in an assistant message rather than a native command item; before this was
        handled such a call could never be credited as a Codemap query.
        """
        tool_use = {"type": "tool_use", "name": "Bash", "input": {"command": '"$CODEMAP_BIN" query --compact symbol X'}}
        stream = _stream(json.dumps({"type": "assistant", "message": {"content": [tool_use]}}), _TERMINAL)

        parsed = runtime.parse_codex_jsonl(stream)

        assert parsed.command_calls == 1
        assert parsed.codemap_calls == 1
        assert parsed.codemap_direct_calls == 1

    @pytest.mark.parametrize(
        ("usage", "expected_input", "expected_output", "expected_malformed"),
        [
            pytest.param({"input_tokens": 1200, "output_tokens": 34}, 1200, 34, 0, id="native-integers"),
            pytest.param({"input_tokens": "1200", "output_tokens": 34.0}, 1200, 34, 0, id="coerced-string-and-float"),
            pytest.param({"input_tokens": "n/a", "output_tokens": 34}, 0, 34, 1, id="unreadable-input-flagged"),
            pytest.param({"input_tokens": -5, "output_tokens": 1.5}, 0, 0, 2, id="impossible-counts-flagged"),
        ],
    )
    def test_malformed_usage_is_coerced_or_counted_never_silently_zeroed(
        self, runtime: Any, usage: dict[str, Any], expected_input: int, expected_output: int, expected_malformed: int
    ) -> None:
        """Provider schema drift must not degrade a paid turn into a free run.

        Numeric strings and whole floats are coerced; unreadable or impossible counts become zero but are tallied in
        ``malformed_usage`` so the loss is visible instead of silent.
        """
        stream = json.dumps({"type": "turn.completed", "status": "completed", "usage": usage})

        parsed = runtime.parse_codex_jsonl(stream)

        assert parsed.input_tokens == expected_input
        assert parsed.output_tokens == expected_output
        assert parsed.malformed_usage == expected_malformed

    def test_usage_events_are_cumulative_not_additive(self, runtime: Any) -> None:
        """The last usage event within a turn wins instead of being summed with earlier ones.

        Pins the documented cumulative-within-a-turn assumption (README): a progress event reporting 100 tokens followed
        by a completion reporting 250 means 250 in total, not 350.
        """
        stream = _stream(
            json.dumps({"type": "turn.progress", "usage": {"input_tokens": 100, "output_tokens": 10}}),
            json.dumps(
                {"type": "turn.completed", "status": "completed", "usage": {"input_tokens": 250, "output_tokens": 25}}
            ),
        )

        parsed = runtime.parse_codex_jsonl(stream)

        assert parsed.input_tokens == 250
        assert parsed.output_tokens == 25
        assert parsed.malformed_usage == 0

    def test_skill_versus_direct_attribution_follows_configuration_not_the_stream(
        self, runtime: Any, tmp_path: Path
    ) -> None:
        """Identical bytes are labelled skill or direct purely by the caller's arm.

        The same successful compact query is parsed twice, once without and once with a Skill path. The label must come
        from that configuration, never from anything the stream says, so a model cannot claim Skill credit.
        """
        stream = _stream(_native_command(_QUERY_COMMAND, output=_QUERY_OUTPUT, item_id="query"), _TERMINAL)
        skill_path = tmp_path / "SKILL.md"
        skill_path.write_text("---\nname: query-code\n---\n", encoding="utf-8")

        direct = runtime.parse_codex_jsonl(stream)
        skill = runtime.parse_codex_jsonl(stream, skill_path=skill_path)

        assert direct.codemap_direct_compact_successful_calls == 1
        assert direct.codemap_skill_compact_successful_calls == 0
        assert skill.codemap_skill_compact_successful_calls == 1
        assert skill.codemap_direct_compact_successful_calls == 0
        assert skill.skill_delivery_observed is False


class TestPaidStage:
    """The provider-neutral paid stage lifecycle: ordering, durable persistence and finalization."""

    def test_runs_exact_task_arm_order_and_persists_every_cell(
        self, lifecycle: Any, events: list[tuple[str, Any]], tmp_path: Path
    ) -> None:
        """The lifecycle preserves canonical order and durable per-cell progress.

        Two tasks times two arms run task-major; metadata is persisted before every cell and once more as completed, and
        telemetry, summary, checksum and adapter-close steps happen in that order.
        """
        run_dir = tmp_path / "runs" / "stage"

        result = lifecycle.run_paid_stage(
            tasks=("T01", "T02"),
            arms=("A", "B"),
            run_dir=run_dir,
            metadata={"scope": "fixture"},
            callbacks=_callbacks(lifecycle, events),
        )

        assert result == run_dir
        assert [value for kind, value in events if kind == "run"] == [
            ("T01", "A"),
            ("T01", "B"),
            ("T02", "A"),
            ("T02", "B"),
        ]
        assert [value for kind, value in events if kind == "metadata"] == [
            ("running", 0),
            ("running", 1),
            ("running", 2),
            ("running", 3),
            ("running", 4),
            ("completed", 4),
        ]
        assert [value[1:] for kind, value in events if kind == "row"] == [
            (1, 4, "A"),
            (2, 4, "B"),
            (3, 4, "A"),
            (4, 4, "B"),
        ]
        assert [kind for kind, _ in events if kind in {"artifacts", "summary"}] == ["artifacts", "summary"]
        assert [kind for kind, _ in events if kind in {"checksums", "closed"}] == ["checksums", "closed"]
        telemetry = (run_dir / "telemetry.jsonl").read_text(encoding="utf-8").splitlines()
        assert [json.loads(line) for line in telemetry] == [
            {"arm": "A", "task_id": "T01"},
            {"arm": "B", "task_id": "T01"},
            {"arm": "A", "task_id": "T02"},
            {"arm": "B", "task_id": "T02"},
        ]
        assert json.loads((run_dir / "run-metadata.json").read_text(encoding="utf-8")) == {
            "persisted_cells": 4,
            "scope": "fixture",
            "status": "completed",
        }

    def test_failure_persists_error_summary_and_finalizes_callbacks(
        self, lifecycle: Any, events: list[tuple[str, Any]], tmp_path: Path
    ) -> None:
        """A failed later cell retains the last durable row and closes the adapter.

        The second arm fails after the first cell persisted; the failure must be re-raised, recorded in the metadata
        with the persisted-cell count, and still followed by checksums and adapter close.
        """
        run_dir = tmp_path / "failed"

        with pytest.raises(RuntimeError, match="fixture cell failed"):
            lifecycle.run_paid_stage(
                tasks=("T01",),
                arms=("A", "B"),
                run_dir=run_dir,
                metadata={},
                callbacks=_callbacks(lifecycle, events, fail_at=("T01", "B")),
            )

        assert json.loads((run_dir / "run-metadata.json").read_text(encoding="utf-8")) == {
            "error": {"message": "fixture cell failed", "type": "RuntimeError"},
            "persisted_cells": 1,
            "status": "failed",
        }
        assert (run_dir / "telemetry.jsonl").read_text(encoding="utf-8") == '{"arm": "A", "task_id": "T01"}\n'
        assert [(kind, value) for kind, value in events if kind == "summary"] == [
            ("summary", {"persisted_cells": 1, "status": "failed", "total_cells": 2})
        ]
        assert [kind for kind, _ in events][-2:] == ["checksums", "closed"]

    def test_rejects_an_invalid_row_before_telemetry_persistence(
        self, lifecycle: Any, events: list[tuple[str, Any]], tmp_path: Path
    ) -> None:
        """Binding validation cannot leave an unvalidated row in durable telemetry.

        The only cell is rejected by ``validate_row``; the telemetry file must stay empty and the error must be recorded
        in the run metadata.
        """
        run_dir = tmp_path / "rejected"

        with pytest.raises(ValueError, match="fixture row rejected"):
            lifecycle.run_paid_stage(
                tasks=("T01",),
                arms=("A",),
                run_dir=run_dir,
                metadata={},
                callbacks=_callbacks(lifecycle, events, invalid_at=("T01", "A")),
            )

        assert (run_dir / "telemetry.jsonl").read_text(encoding="utf-8") == ""
        assert json.loads((run_dir / "run-metadata.json").read_text(encoding="utf-8"))["error"] == {
            "message": "fixture row rejected",
            "type": "ValueError",
        }

    def test_marks_keyboard_interrupt_as_interrupted_and_finalizes(
        self, lifecycle: Any, events: list[tuple[str, Any]], tmp_path: Path
    ) -> None:
        """Interrupts retain completed cells and run the same durable finalizers.

        A Ctrl-C during the second arm is not a failure: the metadata status must read ``interrupted`` with the first
        cell kept, and checksums and adapter close still run.
        """
        run_dir = tmp_path / "interrupted"

        def _interrupting_run_cell(task: str, arm: str) -> dict[str, str]:
            """Interrupt the second arm while returning ordinary rows for other arms."""
            if arm == "B":
                raise KeyboardInterrupt("stop fixture")
            return {"task_id": task, "arm": arm}

        callbacks = dataclasses.replace(_callbacks(lifecycle, events), run_cell=_interrupting_run_cell)
        with pytest.raises(KeyboardInterrupt, match="stop fixture"):
            lifecycle.run_paid_stage(tasks=("T01",), arms=("A", "B"), run_dir=run_dir, metadata={}, callbacks=callbacks)

        metadata = json.loads((run_dir / "run-metadata.json").read_text(encoding="utf-8"))
        assert metadata["status"] == "interrupted"
        assert metadata["persisted_cells"] == 1
        assert [kind for kind, _ in events][-2:] == ["checksums", "closed"]

    def test_rejects_an_existing_run_directory_and_still_closes_adapter(
        self, lifecycle: Any, events: list[tuple[str, Any]], tmp_path: Path
    ) -> None:
        """A run path is reserved exclusively even when setup cannot begin.

        Starting in a directory that already exists must raise ``FileExistsError`` before any cell runs, yet the adapter
        is still closed so a refused run leaks no provider session.
        """
        run_dir = tmp_path / "occupied"
        run_dir.mkdir()

        with pytest.raises(FileExistsError):
            lifecycle.run_paid_stage(
                tasks=("T01",), arms=("A",), run_dir=run_dir, metadata={}, callbacks=_callbacks(lifecycle, events)
            )

        assert events == [("closed", None)]


class TestChecksumLedger:
    """The shared checksum ledger over a run directory and its child stage ledgers."""

    def test_detects_retained_artifact_changes(self, lifecycle: Any, tmp_path: Path) -> None:
        """The shared ledger covers lifecycle evidence and rejects later tampering.

        A ledger written over telemetry and metadata verifies cleanly and never lists itself; rewriting the telemetry
        afterwards must make verification fail naming that file.
        """
        telemetry = tmp_path / "telemetry.jsonl"
        telemetry.write_text('{"task_id": "T01"}\n', encoding="utf-8")
        (tmp_path / "run-metadata.json").write_text('{"status": "completed"}\n', encoding="utf-8")
        lifecycle.write_checksums(tmp_path)
        assert "checksums.sha256" not in (tmp_path / "checksums.sha256").read_text(encoding="utf-8")
        lifecycle.verify_checksums(tmp_path)

        telemetry.write_text('{"task_id": "changed"}\n', encoding="utf-8")

        with pytest.raises(ValueError, match=r"checksum mismatch: telemetry.jsonl"):
            lifecycle.verify_checksums(tmp_path)

    def test_rejects_a_path_outside_the_run_directory(self, lifecycle: Any, tmp_path: Path) -> None:
        """Checksum verification never resolves a ledger entry outside its run root.

        A ledger line pointing at ``../outside.jsonl`` could make verification read arbitrary files, so it is refused as
        an unsafe path.
        """
        (tmp_path / "checksums.sha256").write_text(f"{'0' * 64}  ../outside.jsonl\n", encoding="utf-8")

        with pytest.raises(ValueError, match="unsafe path"):
            lifecycle.verify_checksums(tmp_path)

    def test_root_ledger_covers_each_child_stage_ledger(self, lifecycle: Any, tmp_path: Path) -> None:
        """A unified run cannot hide changed stage telemetry behind a child ledger.

        The root ledger lists the child's telemetry and its own ledger file; tampering with the child telemetry must
        therefore fail verification of the root.
        """
        child = tmp_path / "readcrop"
        child.mkdir()
        telemetry = child / "telemetry.jsonl"
        telemetry.write_text('{"task_id": "RC-01"}\n', encoding="utf-8")
        lifecycle.write_checksums(child)
        lifecycle.write_checksums(tmp_path)
        root_ledger = (tmp_path / "checksums.sha256").read_text(encoding="utf-8")
        assert "readcrop/telemetry.jsonl" in root_ledger
        assert "readcrop/checksums.sha256" in root_ledger
        lifecycle.verify_checksums(tmp_path)

        telemetry.write_text('{"task_id": "changed"}\n', encoding="utf-8")

        with pytest.raises(ValueError, match=r"checksum mismatch: readcrop/telemetry.jsonl"):
            lifecycle.verify_checksums(tmp_path)


class TestProgressScope:
    """The aggregate progress counter that unified runs overlay on native stage rows."""

    @pytest.mark.parametrize(
        ("local_completed", "local_total", "completed_offset", "expected_prefix"),
        [
            pytest.param(1, 165, 0, "(1/204)", id="structural-first"),
            pytest.param(165, 165, 0, "(165/204)", id="structural-last"),
            pytest.param(1, 18, 165, "(166/204)", id="readcrop-first"),
            pytest.param(18, 18, 165, "(183/204)", id="readcrop-last"),
            pytest.param(1, 12, 183, "(184/204)", id="fix-single-first"),
            pytest.param(12, 12, 183, "(195/204)", id="fix-single-last"),
            pytest.param(1, 9, 195, "(196/204)", id="fix-multi-first"),
            pytest.param(9, 9, 195, "(204/204)", id="fix-multi-last"),
        ],
    )
    def test_maps_every_native_stage_boundary_to_one_counter(
        self,
        runtime: Any,
        capsys: pytest.CaptureFixture[str],
        local_completed: int,
        local_total: int,
        completed_offset: int,
        expected_prefix: str,
    ) -> None:
        """Unified terminal rows advance monotonically while their content stays native.

        A stage-local ``(i/n)`` prefix is rewritten to the aggregate position by adding the completed offset, so the
        first and last row of each stage lands on a contiguous counter out of 204 cells.
        """
        suffix = " ✓  T-01 A_plain native-stage-fields"

        with runtime.progress_scope(completed_offset=completed_offset, total_cells=204):
            runtime.print_arm_row(f"({local_completed}/{local_total}){suffix}", "A_plain")

        assert capsys.readouterr().out.rstrip("\n") == f"{expected_prefix}{suffix}"

    def test_restores_stage_local_output_after_an_exception(
        self, runtime: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A failed child stage cannot leak aggregate presentation into later runs.

        A row printed inside the scope carries the aggregate counter; after the scope exits through an exception, the
        next row must come out with its own stage-local counter again.
        """

        def _run_expected_failure() -> None:
            """Run the statements expected to fail as one callable."""
            with runtime.progress_scope(completed_offset=3, total_cells=12):
                runtime.print_arm_row("(1/3) ✓ T-01 A_plain", "A_plain")
                raise RuntimeError("fixture failure")

        with pytest.raises(RuntimeError, match="fixture failure"):
            _run_expected_failure()
        runtime.print_arm_row("(1/3) ✓ T-01 A_plain", "A_plain")

        assert capsys.readouterr().out.splitlines() == ["(4/12) ✓ T-01 A_plain", "(1/3) ✓ T-01 A_plain"]

    @pytest.mark.parametrize(
        ("completed_offset", "total_cells"),
        [
            pytest.param(-1, 12, id="negative-offset"),
            pytest.param(0, 0, id="zero-total"),
            pytest.param(12, 12, id="completed-offset"),
        ],
    )
    def test_rejects_impossible_aggregate_coordinates(
        self, runtime: Any, completed_offset: int, total_cells: int
    ) -> None:
        """Invalid aggregate coordinates fail before any terminal row is emitted.

        A negative offset, an empty total, or an offset that already equals the total cannot describe a running stage,
        so entering the scope must raise instead of printing a nonsensical counter.
        """
        with pytest.raises(ValueError, match="progress scope"):
            with runtime.progress_scope(completed_offset=completed_offset, total_cells=total_cells):
                pass
