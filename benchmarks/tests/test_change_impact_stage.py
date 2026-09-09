"""No-model admission and diagnostic reporting for held-out impact predictions."""

from __future__ import annotations

import json
import importlib.util
import sys
import shutil
import contextlib
from types import SimpleNamespace
from types import ModuleType
from pathlib import Path

import pytest

from _bench_common import change_impact_stage as stage


def test_paid_impact_lifecycle_uses_isolated_fixture_and_native_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An approved run scores native responses, preserves failure spend, and cleans every cell."""
    opened: list[Path] = []
    rendered: list[tuple[str, str]] = []
    monkeypatch.setattr(stage, "print_arm_row", lambda line, arm, **kwargs: rendered.append((line, arm)))

    @contextlib.contextmanager
    def _runtime(**kwargs):
        """Replace only the external provider boundary with deterministic native evidence."""
        root = kwargs["source_root"]
        opened.append(root)
        assert not root.is_relative_to(kwargs["run_dir"])
        assert kwargs["index_path"].is_file()
        assert stage.contracts.source_fingerprint(root) == kwargs["fixture_runtime_coordinate"]["source_fingerprint"]

        def _run(task, arm, prompt):
            """Return one truthful transport result while leaving semantic scoring to the stage."""
            assert "BEGIN_CHANGE_IMPACT_JSON" in prompt
            oracle = stage.contracts.build_change_impact_oracle(task, root)
            answer = {key: dict(value) if key == "reasons" else list(value) for key, value in oracle.expected.items()}
            return {
                "success": arm != "B_auto",
                "incomplete": False,
                "contaminated": False,
                "report_text": "BEGIN_CHANGE_IMPACT_JSON\n" + json.dumps(answer) + "\nEND_CHANGE_IMPACT_JSON",
                "raw_events": [],
                "input_tokens": 100,
                "cached_input_tokens": 40,
                "output_tokens": 20,
                "usage_complete": True,
                "elapsed_s": 1.0,
                "command_calls": 1,
                "codemap_calls": int(arm != "A_plain"),
                "codemap_used": arm != "A_plain",
                "treatment_adherence": True,
                "error": "transport failed" if arm == "B_auto" else "",
                "error_type": "",
            }

        yield SimpleNamespace(run_cell=_run)

    scope = stage.resolve_scope("codex", model="gpt-5.6-terra")
    destination = tmp_path / "paid"
    stage.run_stage(
        provider="codex",
        model="gpt-5.6-terra",
        paid_approval=scope["scope_sha256"][:16],
        output_dir=destination,
        runtime_factory=_runtime,
    )
    rows = [json.loads(line) for line in (destination / "telemetry.jsonl").read_text().splitlines()]
    assert len(rows) == 15
    assert sum(row["exact_pass"] for row in rows) == 10
    assert all(row["input_tokens"] == 100 for row in rows)
    assert all(row["quality"]["graded_score"] == 1 for row in rows)
    assert all(row["admitted_quality"] == 0 for row in rows if row["arm"] == "B_auto")
    assert len(opened) == 15 and all(not root.exists() for root in opened)
    assert json.loads((destination / "run-metadata.json").read_text())["status"] == "completed"
    assert (destination / "summary.json").is_file()
    assert (destination / "checksums.sha256").is_file()
    cell_lines = [(line, arm) for line, arm in rendered if line.startswith("(")]
    assert len(cell_lines) == 15
    assert [arm for _, arm in cell_lines] == ["A_plain", "B_auto", "C_strict"] * 5
    log = (destination / "run.log").read_text(encoding="utf-8")
    assert all(line in log for line, _ in cell_lines)
    assert "\x1b[" not in log


def _load_provider(provider: str) -> ModuleType:
    """Load an existing Fire runner without starting its command-line entrypoint."""
    path = Path(__file__).resolve().parents[1] / f"run-{provider}-agentic.py"
    spec = importlib.util.spec_from_file_location(f"_impact_dispatch_{provider}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_existing_provider_cli_dispatches_impact_preflight(
    provider: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both Fire entrypoints expose the same fixture validator with provider runtime delegation."""
    module = _load_provider(provider)

    @contextlib.contextmanager
    def _runtime(**kwargs):
        """Replace only native provider probing, never fixture/scorer admission."""
        assert kwargs["dry_run"] is True
        yield None

    monkeypatch.setattr(module, "impact_runtime", _runtime)
    module.main(study="change-impact", dry_run=True)
    assert "PAID_COMMAND" in capsys.readouterr().out


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("argument", ["repo_path", "index_relocation_path"])
def test_impact_cli_rejects_ignored_graph_coordinates(provider: str, argument: str) -> None:
    """Graph runtime coordinates must not appear accepted by the fixture-only diagnostic."""
    module = _load_provider(provider)
    with pytest.raises(SystemExit):
        module.main(study="change-impact", dry_run=True, **{argument: "irrelevant"})


def test_preflight_exposes_separate_nonpaid_scope(capsys: pytest.CaptureFixture[str]) -> None:
    """Fixture validation must not masquerade as paid provider sandbox admission."""
    stage.run_stage(provider="codex", dry_run=True)
    output = capsys.readouterr().out
    assert "change-impact" in output
    assert "15" in output
    assert "paid_admission=blocked" in output
    assert "PAID_COMMAND" not in output


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_impact_scope_binds_provider_model_and_frozen_index(provider: str) -> None:
    """The prospective suite records its own source and oracle identity for both hosts."""
    scope = stage.resolve_scope(provider)
    assert scope["study"] == "change-impact"
    assert scope["provider"] == provider
    assert scope["planned_cells"] == 15
    assert scope["paid_admission"] == "exact_scope_approval_and_native_runtime_preflight_required"
    assert len(scope["fixture_sha256"]) == 64
    assert len(scope["scope_sha256"]) == 64
    assert scope["model"] == ("sonnet" if provider == "claude" else "gpt-5.6-terra")
    assert len(scope["semantic_index_sha256"]) == 64


def test_paid_mode_fails_before_creating_artifacts(tmp_path: Path) -> None:
    """A new fixture scope cannot bypass the existing provider runtime locks."""
    destination = tmp_path / "result"
    with pytest.raises(ValueError, match="paid.*requires current scope approval"):
        stage.run_stage(provider="codex", output_dir=destination)
    assert not destination.exists()


def test_diagnostic_refuses_output_inside_model_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Failure details must never deposit oracle labels in the prospective model worktree."""
    source = tmp_path / "answers.jsonl"
    source.write_text("")
    fixture = tmp_path / "fixture"
    shutil.copytree(stage._FIXTURE, fixture)
    monkeypatch.setattr(stage, "_FIXTURE", fixture)
    destination = fixture / "diagnostic-oracle-evidence"
    assert not destination.exists()
    with pytest.raises(ValueError, match="outside.*fixture"):
        stage.run_stage(provider="codex", answers_file=source, output_dir=destination)
    assert not destination.exists()


def test_diagnostic_missing_cells_score_zero_and_rows_use_renderer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial answer file retains all assigned cells and never invents efficiency evidence."""
    source = tmp_path / "answers.jsonl"
    source.write_text(json.dumps({"task_id": "CI-01", "arm": "A_plain", "response": "{}"}) + "\n")
    destination = tmp_path / "diagnostic"
    rendered: list[tuple[str, str]] = []
    monkeypatch.setattr(stage, "print_arm_row", lambda line, arm, **kwargs: rendered.append((line, arm)))
    stage.run_stage(provider="claude", answers_file=source, output_dir=destination)
    summary = json.loads((destination / "summary.json").read_text())
    assert summary["diagnostic_only"] is True
    assert summary["assigned_cells"] == 15
    assert summary["observed_cells"] == 1
    assert summary["unobserved_cells"] == 14
    assert summary["quality"] == 0.0
    assert "efficiency" not in summary
    assert len(rendered) == 15
    telemetry = [json.loads(line) for line in (destination / "telemetry.jsonl").read_text().splitlines()]
    assert len(telemetry) == 15
    assert isinstance(telemetry[0]["components"], dict)
    assert (destination / "checksums.sha256").is_file()
    assert source.read_text().count("\n") == 1


@pytest.mark.parametrize("invalid", ["duplicate", "unknown_task", "unknown_arm"])
def test_invalid_coordinates_fail_before_output_creation(tmp_path: Path, invalid: str) -> None:
    """Unknown or repeated coordinates cannot inflate the diagnostic denominator."""
    row = {"task_id": "CI-01", "arm": "A_plain", "response": "{}"}
    rows = [row, row] if invalid == "duplicate" else [dict(row)]
    if invalid == "unknown_task":
        rows[0]["task_id"] = "BA-01"
    if invalid == "unknown_arm":
        rows[0]["arm"] = "plain"
    source = tmp_path / "answers.jsonl"
    source.write_text("\n".join(json.dumps(item) for item in rows) + "\n")
    destination = tmp_path / "diagnostic"
    with pytest.raises(ValueError, match="coordinate"):
        stage.run_stage(provider="codex", answers_file=source, output_dir=destination)
    assert not destination.exists()


def test_native_quality_gates_and_partial_credit_remain_distinct(tmp_path: Path) -> None:
    """Native completion, semantic accuracy, and resource availability are independent gates."""
    outcomes = ["wrong_answer", "malformed", "failed", "incomplete", "contaminated", "nonadherent", "missing_usage"]
    observed = []

    @contextlib.contextmanager
    def _runtime(**kwargs):
        """Script native outcomes only; actual source/oracle/scorer/lifecycle remain active."""

        def _run(task, arm, prompt):
            """Inject one precisely scoped defect into an otherwise valid response."""
            outcome = outcomes[len(observed)] if len(observed) < len(outcomes) else "correct"
            observed.append(outcome)
            expected = stage.contracts.build_change_impact_oracle(task, kwargs["source_root"]).expected
            answer = {key: dict(value) if key == "reasons" else list(value) for key, value in expected.items()}
            if outcome == "wrong_answer":
                for location in answer["must_update_callsites"]:
                    del answer["reasons"][location]
                answer["must_update_callsites"] = []
            response = "BEGIN_CHANGE_IMPACT_JSON\n" + json.dumps(answer) + "\nEND_CHANGE_IMPACT_JSON"
            return {
                "success": outcome != "failed",
                "incomplete": outcome == "incomplete",
                "contaminated": outcome == "contaminated",
                "treatment_adherence": outcome != "nonadherent",
                "usage_complete": outcome != "missing_usage",
                "report_text": "{}" if outcome == "malformed" else response,
                "input_tokens": None if outcome == "missing_usage" else 100,
                "cached_input_tokens": 30,
                "output_tokens": 10,
                "elapsed_s": 1,
                "raw_events": [],
            }

        yield SimpleNamespace(run_cell=_run)

    scope = stage.resolve_scope("codex")
    destination = tmp_path / "native-gates"
    stage.run_stage(
        provider="codex", paid_approval=scope["scope_sha256"][:16], output_dir=destination, runtime_factory=_runtime
    )
    rows = [json.loads(line) for line in (destination / "telemetry.jsonl").read_text().splitlines()]
    assert 0 < rows[0]["admitted_quality"] < 1
    assert rows[0]["exact_pass"] is False
    assert rows[1]["answer_contract_valid"] is False
    assert all(row["admitted_quality"] == 0 and row["exact_pass"] is False for row in rows[1:6])
    assert all(row["quality"]["graded_score"] == 1 for row in rows[2:6])
    assert rows[6]["exact_pass"] is True and rows[6]["input_tokens"] is None
    summary = json.loads((destination / "summary.json").read_text())
    assert summary["comparisons"]["C_strict"]["efficiency"]["input_tokens"]["unavailable_pairs"] == 1


def test_interrupted_impact_run_preserves_spend_summary_and_cleans_source(tmp_path: Path) -> None:
    """A failed second transport keeps the first row and every remaining assigned denominator."""
    roots = []

    @contextlib.contextmanager
    def _runtime(**kwargs):
        """Fail only the second native boundary after one result was persisted."""
        roots.append(kwargs["source_root"])

        def _run(task, arm, prompt):
            """Return malformed-but-observed output, then simulate a transport crash."""
            if len(roots) == 2:
                raise RuntimeError("native transport interrupted")
            return {
                "success": True,
                "incomplete": False,
                "contaminated": False,
                "treatment_adherence": True,
                "usage_complete": True,
                "report_text": "{}",
                "input_tokens": 200,
                "cached_input_tokens": 100,
                "output_tokens": 20,
                "elapsed_s": 1,
                "raw_events": [],
            }

        yield SimpleNamespace(run_cell=_run)

    scope = stage.resolve_scope("codex")
    destination = tmp_path / "interrupted"
    with pytest.raises(RuntimeError, match="native transport interrupted"):
        stage.run_stage(
            provider="codex", paid_approval=scope["scope_sha256"][:16], output_dir=destination, runtime_factory=_runtime
        )
    rows = [json.loads(line) for line in (destination / "telemetry.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["input_tokens"] == 200
    assert json.loads((destination / "run-metadata.json").read_text())["status"] == "failed"
    summary = json.loads((destination / "summary.json").read_text())
    assert summary["all_assigned"]["A_plain"]["unobserved_cells"] == 4
    assert summary["all_assigned"]["B_auto"]["unobserved_cells"] == 5
    assert len(roots) == 2 and all(not root.exists() for root in roots)
    assert (destination / "checksums.sha256").is_file()


def test_scope_is_stable_but_model_timeout_and_source_changes_invalidate_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Approval survives only temporary-root/timestamp changes, never semantic input changes."""
    original = stage.resolve_scope("codex")
    assert original == stage.resolve_scope("codex")
    assert original["scope_sha256"] != stage.resolve_scope("codex", model="gpt-5.6-sol")["scope_sha256"]
    assert original["scope_sha256"] != stage.resolve_scope("codex", timeout=5)["scope_sha256"]
    fixture = tmp_path / "fixture"
    shutil.copytree(stage._FIXTURE, fixture)
    monkeypatch.setattr(stage, "_FIXTURE", fixture)
    source = next(fixture.rglob("*.py"))
    source.write_bytes(source.read_bytes() + b"\n# changed fixture\n")
    changed = stage.resolve_scope("codex")
    assert changed["fixture_sha256"] != original["fixture_sha256"]
    assert changed["scope_sha256"] != original["scope_sha256"]


def test_stale_approval_never_opens_provider_or_output(tmp_path: Path) -> None:
    """A token bound to another model cannot authorize this execution."""

    def _runtime(**kwargs):
        """Fail the test if a rejected request reaches the external provider boundary."""
        pytest.fail("stale approval reached provider")

    scope = stage.resolve_scope("codex", model="gpt-5.6-sol")
    destination = tmp_path / "stale"
    with pytest.raises(ValueError, match="current scope approval"):
        stage.run_stage(
            provider="codex",
            model="gpt-5.6-terra",
            paid_approval=scope["scope_sha256"][:16],
            output_dir=destination,
            runtime_factory=_runtime,
        )
    assert not destination.exists()


@pytest.mark.parametrize("defect", ["missing_gate", "foreign_coordinate", "fixture_mutation"])
def test_rejected_native_evidence_is_retained_without_false_quality(tmp_path: Path, defect: str) -> None:
    """Malformed rows and non-Python fixture deposits cannot become valid benchmark observations."""
    roots = []

    @contextlib.contextmanager
    def _runtime(**kwargs):
        """Keep corrupt provider data at the external boundary while checking real persistence."""
        roots.append(kwargs["source_root"])

        def _run(task, arm, prompt):
            """Return one invalid gate/coordinate, or modify a forbidden source-side text file."""
            if defect == "fixture_mutation":
                (kwargs["source_root"] / "oracle-note.txt").write_text("unexpected evidence")
            return {
                "success": True,
                "incomplete": False,
                "contaminated": False,
                "usage_complete": True,
                "treatment_adherence": None if defect == "missing_gate" else True,
                "task_id": "foreign" if defect == "foreign_coordinate" else task["id"],
                "report_text": "{}",
                "input_tokens": 222,
                "cached_input_tokens": 100,
                "output_tokens": 20,
                "elapsed_s": 1,
                "raw_events": [],
            }

        yield SimpleNamespace(run_cell=_run)

    scope = stage.resolve_scope("codex")
    destination = tmp_path / defect
    if defect == "fixture_mutation":
        stage.run_stage(
            provider="codex", paid_approval=scope["scope_sha256"][:16], output_dir=destination, runtime_factory=_runtime
        )
        rows = [json.loads(line) for line in (destination / "telemetry.jsonl").read_text().splitlines()]
        assert all(row["contaminated"] and row["admitted_quality"] == 0 for row in rows)
    else:
        with pytest.raises(ValueError, match="explicit treatment_adherence|another coordinate"):
            stage.run_stage(
                provider="codex",
                paid_approval=scope["scope_sha256"][:16],
                output_dir=destination,
                runtime_factory=_runtime,
            )
        assert (destination / "telemetry.jsonl").read_bytes() == b""
        summary = json.loads((destination / "summary.json").read_text())
        assert summary["all_assigned"]["A_plain"]["unobserved_cells"] == 5
    native = json.loads((destination / "cells/CI-01/A_plain/native.json").read_text())
    assert native["input_tokens"] == 222
    assert all(not root.exists() for root in roots)


def test_implementation_identity_excludes_private_reports_and_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Private plugin evidence must neither enter paid snapshots nor churn approved runtime identity."""
    benchmarks = tmp_path / "benchmarks"
    plugin = tmp_path / "plugins/codemap-py"
    benchmarks.mkdir()
    (plugin / "src").mkdir(parents=True)
    (plugin / "src/runtime.py").write_text("VALUE = 1\n")
    monkeypatch.setattr(stage, "_BENCHMARKS", benchmarks)
    monkeypatch.setattr(stage, "_PLUGIN", plugin)
    before = stage._implementation_hashes()
    for directory in (".reports", ".cache", ".plans", "tests"):
        (plugin / directory).mkdir()
        (plugin / directory / "oracle.json").write_text('{"private": "evidence"}')
    assert stage._implementation_hashes() == before
    (plugin / "src/runtime.py").write_text("VALUE = 2\n")
    assert stage._implementation_hashes() != before


def test_real_claude_adapter_connects_native_transport_to_shared_quality(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the actual provider factory and common lifecycle with only the API stream replaced."""
    module = _load_provider("claude")
    tasks = stage.contracts.load_change_impact_tasks(stage._TASKS)
    native_calls = []
    original_run = module.subprocess.run

    def _probe_supported_python(command, *args, **kwargs):
        """Model an eligible external interpreter without replacing fixture subprocesses."""
        if len(command) == 3 and command[1] == "-c" and "sys.implementation.name" in command[2]:
            return SimpleNamespace(returncode=0)
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(module.subprocess, "run", _probe_supported_python)

    def _stream(command, *, cwd, env, on_event, **kwargs):
        """Return an exact native answer without Codemap tool use, testing optional versus strict arms."""
        task = next(task for task in tasks if task["prompt"] in command[-1])
        expected = stage.contracts.build_change_impact_oracle(task, cwd).expected
        answer = {key: dict(value) if key == "reasons" else list(value) for key, value in expected.items()}
        text = "BEGIN_CHANGE_IMPACT_JSON\n" + json.dumps(answer) + "\nEND_CHANGE_IMPACT_JSON"
        settings = json.loads(Path(command[command.index("--settings") + 1]).read_text())
        assert str(destination) in settings["sandbox"]["filesystem"]["denyRead"]
        assert "BENCHMARK_EVIDENCE_ROOTS" not in env
        native_calls.append(cwd)
        on_event(
            {
                "type": "assistant",
                "message": {"id": f"message-{len(native_calls)}", "content": [{"type": "text", "text": text}]},
            },
            0.0,
        )
        on_event(
            {
                "type": "result",
                "subtype": "success",
                "usage": {"input_tokens": 50, "cache_read_input_tokens": 100, "output_tokens": 20},
            },
            0.1,
        )
        return SimpleNamespace(error=None, stderr="", returncode=0, exc_timeout=False, elapsed_s=0.1)

    monkeypatch.setattr(module, "stream_claude", _stream)
    scope = stage.resolve_scope("claude", model="sonnet")
    destination = tmp_path / "actual-adapter"
    module.main(study="change-impact", model="sonnet", run_dir=destination, paid_approval=scope["scope_sha256"][:16])
    rows = [json.loads(line) for line in (destination / "telemetry.jsonl").read_text().splitlines()]
    assert len(native_calls) == 15 and all(not root.exists() for root in native_calls)
    assert all(row["quality"]["graded_score"] == 1 and row["success"] is True for row in rows)
    assert all(row["exact_pass"] is (row["arm"] != "C_strict") for row in rows)
    assert all(row["input_tokens"] == 150 and row["cached_input_tokens"] == 100 for row in rows)
