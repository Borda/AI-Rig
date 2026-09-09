"""No-model contracts for the shared Codex agentic study."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest


BENCHMARKS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BENCHMARKS_DIR))

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
POSIX_SECURITY = pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX private-mode semantics")


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
    assert spec is not None and spec.loader is not None
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


def _query(item_id: str = "q") -> dict[str, Any]:
    """Build successful compact-query evidence using the caller's item identifier.

    >>> item = _query("example")["item"]
    >>> item["id"], item["exit_code"], json.loads(item["aggregated_output"])
    ('example', 0, {'index': {'query_complete': True, 'compact': True}})
    """
    return {
        "type": "item.completed",
        "item": {
            "id": item_id,
            "type": "command_execution",
            "command": "$CODEMAP_BIN query --compact rdeps lightning.pytorch.callbacks.timer",
            "status": "completed",
            "exit_code": 0,
            "aggregated_output": json.dumps({"index": {"query_complete": True, "compact": True}}),
        },
    }


def test_dry_run_has_three_probes_and_default_shared_coordinates(agentic: Any) -> None:
    """The default dry run exposes all shared tasks once in every canonical arm."""
    rows = agentic.dry_run()
    probes = [row for row in rows if row.startswith("PROBE")]
    plan = [row for row in rows if row.startswith("PLAN")]

    assert len(probes) == 3
    assert len(plan) == len(AGENTIC_TASK_IDS) * len(AGENTIC_ARMS)
    assert plan[0] == "PLAN    BA-01  rep=1  A_plain"
    # Tasks run in suite order, but arms are counterbalanced within each task, so the final row's arm is whatever the
    # last task's order puts last. Pinning it here would assert the counterbalancing policy, which has its own tests.
    assert plan[-1].split()[1] == AGENTIC_TASK_IDS[-1]


def test_dry_run_default_coordinates_equal_the_shared_suite_scope(agentic: Any) -> None:
    """The default no-model plan schedules each shared parity coordinate once.

    Prevents a runner that advertises parity while retaining the BA-01 pilot, omitting an arm, duplicating a coordinate,
    or using a hidden default repeat. A mere plan-length assertion could miss swapped or repeated coordinates, so this
    compares the complete coordinate set.
    """
    rows = agentic.dry_run()
    actual = {
        (parts[1], int(parts[2].removeprefix("rep=")), parts[3])
        for row in rows
        if row.startswith("PLAN")
        for parts in [row.split()]
    }
    expected = {
        (task_id, repetition, arm) for task_id in AGENTIC_TASK_IDS for repetition in (1,) for arm in AGENTIC_ARMS
    }

    assert actual == expected
    assert sum(row.startswith("PLAN") for row in rows) == len(expected)


def test_dry_run_accepts_a_positive_explicit_repeat_override(agentic: Any) -> None:
    """A caller can request an admitted repeat count without changing the task scope.

    Prevents the former fixed-three-repeat pilot behavior and a runner that silently ignores an explicit repeat request.
    Zero remains invalid because it would create no evidence-bearing parity coordinates.
    """
    rows = agentic.dry_run(repetitions=2)
    plan = [row.split() for row in rows if row.startswith("PLAN")]

    assert len(plan) == len(AGENTIC_TASK_IDS) * len(AGENTIC_ARMS) * 2
    assert {parts[1] for parts in plan} == set(AGENTIC_TASK_IDS)
    assert {parts[2] for parts in plan} == {"rep=1", "rep=2"}
    assert {parts[3] for parts in plan} == set(AGENTIC_ARMS)
    with pytest.raises(ValueError, match="positive|at least 1"):
        agentic.dry_run(repetitions=0)


def test_dry_run_preflights_snapshot_bound_c_admission_without_auth_or_model(
    agentic: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
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
            pass

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


def test_agentic_output_legend_uses_shared_renderer_contract(agentic: Any) -> None:
    """Agentic output declares its treatments and metrics in one shared-renderer block."""
    legend = agentic._OUTPUT_LEGEND
    assert legend.startswith(f"{LEGEND_OPEN_RULE}\n")
    assert legend.endswith(LEGEND_CLOSE_RULE)
    assert legend.count("LEGEND") == 2
    assert "treatments: A_plain=no Codemap, B_auto=optional CLI, C_strict=installed Codemap Skill" in legend
    assert "pass:" in legend
    assert "component:" in legend
    assert "SCORE:" not in legend
    assert "component: secondary partial-credit mean; n/a when unscored; scored denominator shown separately" in legend
    assert "EREC: expected-importer recall in all agent text (higher is better)" in legend
    assert "RREC: expected-importer recall in the final report (higher is better)" in legend
    assert (
        "DEFF: unbounded expected-importer exposure hits per command (higher is better within the same task)" in legend
    )
    assert (
        "input tokens: gross total; summaries separate fresh/gross/output tokens and time with eligible pair counts"
        in legend
    )
    assert "answer: ✓ strict envelope, △ diagnostic bare-JSON recovery (not poolable), ✗ absent or invalid" in legend
    assert "correct:" not in legend


@pytest.mark.parametrize(
    ("arm", "native_arm", "available"),
    [
        pytest.param("A_plain", "A_plain", False, id="plain"),
        pytest.param("B_auto", "B_auto", True, id="optional-direct-home"),
        pytest.param("C_strict", "C_strict", True, id="required-skill-home"),
    ],
)
def test_isolated_home_adapter_uses_existing_structural_isolation(
    agentic: Any, monkeypatch: pytest.MonkeyPatch, arm: str, native_arm: str, available: bool
) -> None:
    """The agentic slice maps its semantic arms to the established disposable homes."""
    calls: list[str] = []
    home = object()
    monkeypatch.setattr(
        agentic._structural,
        "prepare_arm_home",
        lambda selected_arm, **_kwargs: calls.append(selected_arm) or home,
    )
    monkeypatch.setattr(agentic._structural, "probe_arm_home", lambda _home: {"codemap_available": available})

    assert agentic.prepare_isolated_home(arm) is home
    assert calls == [native_arm]
    assert agentic.probe_isolated_home(home, arm) == {"codemap_available": available}


def test_clean_plain_stream_is_scored_without_codemap(task_and_truth: tuple[Any, Any], agentic: Any) -> None:
    """A_plain remains valid when its output names both expected importers without Codemap."""
    task, truth = task_and_truth
    text = "lightning.pytorch.trainer.trainer and lightning.pytorch.loops.fit_loop import the timer module."
    result = agentic.parse_agentic_stream(
        _stream(_message(_labelled(text, truth)), _completed()), arm="A_plain", task=task, ground_truth=truth
    )

    assert result.success is True
    assert result.codemap_used is False
    assert result.treatment_adherence is True
    assert result.input_tokens == 123
    assert result.cached_input_tokens == 23
    assert result.output_tokens == 45


def test_auto_direct_query_is_valid_but_not_required(task_and_truth: tuple[Any, Any], agentic: Any) -> None:
    """B_auto records an optional successful direct query and stays adherent."""
    task, truth = task_and_truth
    result = agentic.parse_agentic_stream(
        _stream(
            _query(),
            _message(_labelled("lightning.pytorch.trainer.trainer and lightning.pytorch.loops.fit_loop.", truth)),
            _completed(),
        ),
        arm="B_auto",
        task=task,
        ground_truth=truth,
    )

    assert result.success is True
    assert result.codemap_used is True
    assert result.compliance is None
    assert result.treatment_adherence is True
    assert result.command_calls == 1


def test_auto_no_call_is_valid(task_and_truth: tuple[Any, Any], agentic: Any) -> None:
    """B_auto preserves the Claude contract: not using Codemap is still valid."""
    task, truth = task_and_truth
    result = agentic.parse_agentic_stream(
        _stream(_message(_labelled("lightning.pytorch.trainer.trainer.", truth)), _completed()),
        arm="B_auto",
        task=task,
        ground_truth=truth,
    )

    assert result.success is True
    assert result.codemap_used is False
    assert result.treatment_adherence is True


def test_required_installed_skill_binding_accepts_compact_query_without_manual_read(
    task_and_truth: tuple[Any, Any], agentic: Any, tmp_path: Path
) -> None:
    """C_strict credits a compact query through its immutable installed-Skill treatment."""
    task, truth = task_and_truth
    skill_path = tmp_path / "SKILL.md"
    skill_path.write_text("# locked Codemap skill\n", encoding="utf-8")
    result = agentic.parse_agentic_stream(
        _stream(
            _query(),
            _message(_labelled("lightning.pytorch.trainer.trainer and lightning.pytorch.loops.fit_loop.", truth)),
            _completed(),
        ),
        arm="C_strict",
        task=task,
        ground_truth=truth,
        skill_path=skill_path,
    )

    assert result.success is True
    assert result.codemap_used is True
    assert result.compliance is True
    assert result.treatment_adherence is True


def test_plain_codemap_call_is_contamination(task_and_truth: tuple[Any, Any], agentic: Any) -> None:
    """A_plain keeps successful provider telemetry but fails treatment adherence on leakage."""
    task, truth = task_and_truth
    result = agentic.parse_agentic_stream(
        _stream(_query(), _message(_labelled("lightning.pytorch.trainer.trainer.", truth)), _completed()),
        arm="A_plain",
        task=task,
        ground_truth=truth,
    )

    assert result.success is True
    assert result.contaminated is True
    assert result.treatment_adherence is False


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


def test_paid_admission_requires_exact_hash_and_admitted_manifest(agentic: Any, tmp_path: Path) -> None:
    """Paid execution rejects stale approval and an explicitly unadmitted study manifest."""
    manifest_path, approved_hash = _admitted_manifest(agentic, tmp_path)

    assert agentic.validate_paid_admission(manifest_path, approved_hash)["admission"]["paid_execution"] == "admitted"
    with pytest.raises(ValueError, match="exact current manifest"):
        agentic.validate_paid_admission(manifest_path, "0" * 64)

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
    launcher_path.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
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
    launcher_path.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (launcher_path.parent / "source").mkdir()
    (launcher_path.parent / "source.sha256").write_text("fixture  run-all.sh\n", encoding="utf-8")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifact_sha256"]["run_all"] = hashlib.sha256(launcher_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return hashlib.sha256(manifest_path.read_bytes()).hexdigest(), launcher_path


def test_checksum_refresh_excludes_archived_source_tree(agentic: Any, tmp_path: Path) -> None:
    """Keep archived source validation separate from recurring result checksums."""
    run_dir = tmp_path / "result"
    source_path = run_dir / ".launcher" / "source" / "benchmarks" / "runner.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_text("snapshot\n", encoding="utf-8")
    (run_dir / ".launcher" / "run-all.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (run_dir / ".launcher" / "source.sha256").write_text("fixture  runner.py\n", encoding="utf-8")
    (run_dir / "telemetry.jsonl").write_text("{}\n", encoding="utf-8")

    agentic._write_checksums(run_dir)

    entries = (run_dir / "checksums.sha256").read_text(encoding="utf-8")
    assert ".launcher/run-all.sh" in entries
    assert ".launcher/source.sha256" in entries
    assert "telemetry.jsonl" in entries
    assert ".launcher/source/benchmarks/runner.py" not in entries


def test_paid_run_persists_full_scope_and_noncompliant_c_row(
    agentic: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Paid orchestration retains C no-call evidence while marking it treatment-ineligible."""
    manifest_path, _approval, index_path, _launcher_path = _prepare_paid_fixture(agentic, monkeypatch, tmp_path)
    run_dir = tmp_path / "result"
    approval, launcher_path = _lock_run_launcher(manifest_path, run_dir)

    agentic.run_paid(
        repo_path=tmp_path,
        index_path=index_path,
        auth_source=tmp_path / "auth-not-read.json",
        approval_sha256=approval,
        run_dir=run_dir,
        manifest_path=manifest_path,
        runner_factory=_FixtureRunner,
        invocation_launcher_path=launcher_path,
    )

    rows = [json.loads(line) for line in (run_dir / "telemetry.jsonl").read_text().splitlines()]
    metadata = json.loads((run_dir / "run-metadata.json").read_text())
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
    run_log = (run_dir / "run.log").read_text(encoding="utf-8")
    assert run_log == output


def test_paid_run_preserves_partial_artifacts_on_failure(
    agentic: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A later runner failure cannot erase previously appended agentic evidence."""
    manifest_path, _approval, index_path, _launcher_path = _prepare_paid_fixture(agentic, monkeypatch, tmp_path)
    run_dir = tmp_path / "partial"
    approval, launcher_path = _lock_run_launcher(manifest_path, run_dir)

    with pytest.raises(RuntimeError, match="fixture interruption"):
        agentic.run_paid(
            repo_path=tmp_path,
            index_path=index_path,
            auth_source=tmp_path / "auth-not-read.json",
            approval_sha256=approval,
            run_dir=run_dir,
            manifest_path=manifest_path,
            runner_factory=lambda **kwargs: _FixtureRunner(fail_after=1, **kwargs),
            invocation_launcher_path=launcher_path,
        )

    assert len((run_dir / "telemetry.jsonl").read_text().splitlines()) == 1
    assert json.loads((run_dir / "run-metadata.json").read_text())["status"] == "failed"
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
def test_paid_run_persists_failed_artifact_when_initialization_fails(
    agentic: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    runner_factory: Any,
    error: str,
) -> None:
    """Runner and snapshot initialization failures leave a complete failed artifact."""
    manifest_path, _approval, index_path, _launcher_path = _prepare_paid_fixture(agentic, monkeypatch, tmp_path)
    run_dir = tmp_path / "initialization-failure"
    approval, launcher_path = _lock_run_launcher(manifest_path, run_dir)

    with pytest.raises(RuntimeError, match=error):
        agentic.run_paid(
            repo_path=tmp_path,
            index_path=index_path,
            auth_source=tmp_path / "auth-not-read.json",
            approval_sha256=approval,
            run_dir=run_dir,
            manifest_path=manifest_path,
            runner_factory=runner_factory,
            invocation_launcher_path=launcher_path,
        )

    metadata = json.loads((run_dir / "run-metadata.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "failed"
    assert metadata["persisted_cells"] == 0
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    for values in summary["all_assigned"].values():
        assert values["assigned_cells"] == len(AGENTIC_TASK_IDS)
        assert values["passed_cells"] == 0
        assert values["unobserved_cells"] == len(AGENTIC_TASK_IDS)
        assert values["component_mean"] is None
    assert (run_dir / "telemetry.jsonl").read_text(encoding="utf-8") == ""
    assert f"SUMMARY  status=failed  persisted_cells=0/{AGENTIC_CELLS}" in (run_dir / "run.log").read_text(
        encoding="utf-8"
    )
    assert (run_dir / "checksums.sha256").is_file()


def test_paid_run_rejects_changed_or_unlocked_launcher(
    agentic: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A paid invocation cannot proceed when its launcher differs from the agentic lock."""
    manifest_path, _approval, index_path, _launcher_path = _prepare_paid_fixture(agentic, monkeypatch, tmp_path)
    run_dir = tmp_path / "launcher-rejected"
    approval, launcher_path = _lock_run_launcher(manifest_path, run_dir)
    launcher_path.write_text("changed\n", encoding="utf-8")

    with pytest.raises(ValueError, match="invocation launcher changed"):
        agentic.run_paid(
            repo_path=tmp_path,
            index_path=index_path,
            auth_source=tmp_path / "auth-not-read.json",
            approval_sha256=approval,
            run_dir=run_dir,
            manifest_path=manifest_path,
            runner_factory=_FixtureRunner,
            invocation_launcher_path=launcher_path,
        )


@pytest.mark.parametrize(
    ("kind", "artifact_name", "accepted"),
    [
        pytest.param("new", None, False, id="new-directory-without-launcher"),
        pytest.param("snapshot", None, True, id="run-all-launcher-and-source-snapshot-directory"),
        pytest.param("launcher", None, False, id="launcher-without-source-snapshot"),
        pytest.param("source-manifest-directory", None, False, id="source-manifest-must-be-a-regular-file"),
        pytest.param("artifact", "telemetry.jsonl", False, id="reused-telemetry-artifact-directory"),
        pytest.param("artifact", ".agentic-console.log", False, id="reused-agentic-console-artifact-directory"),
    ],
)
def test_run_directory_admission_allows_only_locked_launcher_snapshot(
    agentic: Any, tmp_path: Path, kind: str, artifact_name: str | None, accepted: bool
) -> None:
    """Run-all may pre-create only its locked launcher and source snapshot; all reuse is rejected."""
    run_dir = tmp_path / kind
    launcher = tmp_path / "launcher.sh"
    launcher.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    launcher_hash = hashlib.sha256(launcher.read_bytes()).hexdigest()
    if kind in {"launcher", "snapshot", "source-manifest-directory"}:
        launcher = run_dir / ".launcher" / "run-all.sh"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        launcher_hash = hashlib.sha256(launcher.read_bytes()).hexdigest()
        if kind in {"snapshot", "source-manifest-directory"}:
            (launcher.parent / "source").mkdir()
            source_manifest = launcher.parent / "source.sha256"
            if kind == "source-manifest-directory":
                source_manifest.mkdir()
            else:
                source_manifest.write_text("fixture  run-all.sh\n", encoding="utf-8")
    elif kind == "artifact":
        run_dir.mkdir()
        assert artifact_name is not None
        (run_dir / artifact_name).write_text("old evidence\n", encoding="utf-8")

    if accepted:
        agentic._admit_run_directory(run_dir, launcher, launcher_hash)
        assert run_dir.is_dir()
    else:
        with pytest.raises(FileExistsError):
            agentic._admit_run_directory(run_dir, launcher, launcher_hash)


def test_agentic_snapshot_records_its_own_runner_identity(agentic: Any, tmp_path: Path) -> None:
    """Agentic bytes are archived under their real name, role, and immutable hash."""
    runner_path = Path(agentic.__file__)
    payload = agentic._write_agentic_input_snapshot(
        tmp_path / "inputs",
        manifest_path=agentic._MANIFEST_PATH,
        tasks_path=agentic._TASKS_PATH,
        runner_path=runner_path,
        invocation_launcher_path=BENCHMARKS_DIR / "run-all.sh",
        index_path=agentic._MANIFEST_PATH,
        auth_source=None,
        arm_archives={},
        arm_files={},
    )

    runner_entries = [entry for entry in payload["files"] if entry["role"] == "agentic_runner"]
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
    for name in ("agentic_reporting", "agentic_contracts"):
        source = BENCHMARKS_DIR / "_bench_common" / f"{name}.py"
        archived = tmp_path / "inputs" / "shared" / f"{name}.py"
        assert archived.read_bytes() == source.read_bytes()
        assert (
            next(entry for entry in payload["files"] if entry["role"] == name)["sha256"]
            == hashlib.sha256(source.read_bytes()).hexdigest()
        )


@POSIX_SECURITY
def test_agentic_first_strict_admission_failure_keeps_identity_evidence_after_cleanup(
    agentic: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A first C_strict admission failure persists identities before home cleanup."""
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
        "fixture-model",
        tmp_path,
        manifest_path=manifest_path,
        marketplace_root=marketplace_root,
        transport=lambda *_args, **_kwargs: model_calls.append("model") or "",
    )
    runner = object.__new__(agentic.AgenticCodexRunner)
    runner.adapter = adapter
    runner.index_path = tmp_path / "index.json"

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
    monkeypatch.setattr(agentic._structural, "_validate_locked_runtime", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(agentic._structural, "_install_codemap_plugin", _fail_after_staging)

    with pytest.raises(RuntimeError, match="fixture plugin identity mismatch"):
        runner.create_input_snapshot(
            run_dir,
            manifest_path=manifest_path,
            invocation_launcher_path=BENCHMARKS_DIR / "run-all.sh",
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
            run_dir,
            manifest_path=manifest_path,
            invocation_launcher_path=BENCHMARKS_DIR / "run-all.sh",
        )
    assert install_calls == ["C_strict"]


def test_agentic_snapshot_attempts_every_cleanup_after_auth_refresh_failure(
    agentic: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A snapshot refresh failure cannot skip coordination or disposable-home cleanup."""
    events: list[str] = []

    class Home:
        """Minimal verified-home seam with identifiable independent cleanup steps."""

        auth_provisioned = True
        codemap_plugin_path = tmp_path / "codemap"
        codex_rig_path = tmp_path / "codex-rig"
        codemap_context_path = None

        def __init__(self, arm: str) -> None:
            """Initialize the test double's fixture-controlled state."""
            self.arm = arm
            self.path = tmp_path / arm
            self.coordination_path = tmp_path / f"coordination-{arm}"

        def cleanup(self) -> None:
            """Implement the test home or workspace cleanup boundary."""
            events.append(f"home:{self.arm}")

    class Adapter:
        """Snapshot adapter whose refresh fails after every disposable home."""

        auth_source = None

        class AuthState:
            """Fail refresh so the cleanup sequence must continue."""

            def refresh_from_home(self, path: Path) -> None:
                """Record authentication refresh through the scenario's controlled boundary."""
                events.append(f"refresh:{path.name}")
                raise RuntimeError("refresh failed")

        _auth_state = AuthState()

        def _bind_runtime_snapshot(self, *_args: Any, **_kwargs: Any) -> None:
            """Accept binding after the synthetic archive write."""

        def _record_runtime_success(self, *_args: Any, **_kwargs: Any) -> None:
            """Accept verified identity recording at the adapter seam."""

        def _prepare_verified_home(self, native_arm: str) -> Home:
            """Return the fixture home through the runner's preparation interface."""
            return Home(native_arm)

    runner = object.__new__(agentic.AgenticCodexRunner)
    runner.adapter = Adapter()
    runner.index_path = tmp_path / "index.json"
    monkeypatch.setattr(
        agentic,
        "_write_agentic_input_snapshot",
        lambda *_args, **_kwargs: {"ok": True, "path": "fixture"},
    )
    monkeypatch.setattr(
        agentic._structural,
        "_cleanup_coordination_root",
        lambda path: events.append(f"coordination:{path.name}"),
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


def test_agentic_snapshot_cleans_a_shared_treatment_coordination_root_once(
    agentic: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """B/C snapshot homes share one index-local coordination root safely."""
    events: list[str] = []
    shared_root = tmp_path / ".index-rw"
    live_roots = {shared_root}

    class Home:
        """Minimal snapshot home with one shared B/C coordination path."""

        auth_provisioned = False
        codemap_plugin_path = tmp_path / "codemap"
        codex_rig_path = tmp_path / "codex-rig"
        codemap_context_path = None

        def __init__(self, arm: str) -> None:
            """Initialize the test double's fixture-controlled state."""
            self.arm = arm
            self.path = tmp_path / arm
            self.coordination_path = shared_root if arm != "A_plain" else None

        def cleanup(self) -> None:
            """Implement the test home or workspace cleanup boundary."""
            events.append(f"home:{self.arm}")

    class Adapter:
        """Provide the three canonical homes without credential state."""

        auth_source = None
        _auth_state = None
        bound_sources: tuple[Path, dict[str, dict[str, Path]]] | None = None

        def _bind_runtime_snapshot(self, root: Path, sources: dict[str, dict[str, Path]]) -> None:
            """Capture the runtime paths that agentic cells must reuse."""
            self.bound_sources = (root, sources)

        def _record_runtime_success(self, *_args: Any, **_kwargs: Any) -> None:
            """Accept verified identity recording at the adapter seam."""

        def _prepare_verified_home(self, native_arm: str) -> Home:
            """Return the fixture home through the runner's preparation interface."""
            return Home(native_arm)

    def _cleanup(path: Path) -> None:
        """Record coordination cleanup and reject repeated removal of an unavailable root."""
        events.append(f"coordination:{path.name}")
        if path not in live_roots:
            raise ValueError("Codemap coordination root is unavailable")
        live_roots.remove(path)

    runner = object.__new__(agentic.AgenticCodexRunner)
    runner.adapter = Adapter()
    runner.index_path = tmp_path / "index.json"
    monkeypatch.setattr(
        agentic,
        "_write_agentic_input_snapshot",
        lambda *_args, **_kwargs: {"ok": True, "path": "fixture"},
    )
    monkeypatch.setattr(agentic._structural, "_cleanup_coordination_root", _cleanup)

    assert runner.create_input_snapshot(
        tmp_path / "run",
        manifest_path=agentic._MANIFEST_PATH,
        invocation_launcher_path=BENCHMARKS_DIR / "run-all.sh",
    ) == {"ok": True, "path": "fixture"}
    assert events == ["home:A_plain", "coordination:.index-rw", "home:B_auto", "home:C_strict"]
    assert runner.adapter.bound_sources == (
        tmp_path / "run" / "inputs",
        {
            "B_auto": {"direct-cli": tmp_path / "run" / "inputs" / "B_auto" / "direct-cli"},
            "C_strict": {
                "codemap-py": tmp_path / "run" / "inputs" / "C_strict" / "codemap-py",
                "codex-rig": tmp_path / "run" / "inputs" / "C_strict" / "codex-rig",
            },
        },
    )


def test_native_runner_refreshes_auth_and_fails_postflight_contamination(
    agentic: Any, monkeypatch: pytest.MonkeyPatch, task_and_truth: tuple[Any, Any], tmp_path: Path
) -> None:
    """Native cells propagate refreshed auth and turn postflight drift into an ineligible row."""
    task, truth = task_and_truth
    refreshes: list[Path] = []

    class Home:
        """Minimal disposable home compatible with the native runner seam."""

        auth_provisioned = True
        coordination_path = None
        codemap_skill_path = None

        def __init__(self) -> None:
            """Initialize the test double's fixture-controlled state."""
            self.cleaned = False
            self.env = {}
            self.path = tmp_path / "home"

        def cleanup(self) -> None:
            """Implement the test home or workspace cleanup boundary."""
            self.cleaned = True

    home = Home()

    class Adapter:
        """Native transport seam with a refreshable private auth state."""

        timeout = 600.0

        class AuthState:
            """Record whether cleanup transfers refreshed auth out of the disposable home."""

            def refresh_from_home(self, path: Path) -> None:
                """Record authentication refresh through the scenario's controlled boundary."""
                refreshes.append(path)

        _auth_state = AuthState()

        def _prepare_verified_home(self, _arm: str) -> Home:
            """Return the fixture home through the runner's preparation interface."""
            return home

        def build_command(self, _prompt: str) -> list[str]:
            """Return fixed argv without launching the Codex executable."""
            return ["codex", "exec"]

        def _subprocess(self, _command: list[str], _env: dict[str, str], *, timeout: float) -> str:
            """Return a successful answer stream built from the fixture oracle."""
            del timeout
            return _stream(_message(_labelled("lightning.pytorch.trainer.trainer.", truth)), _completed())

        def close(self) -> None:
            """Implement the adapter cleanup boundary for the enclosing lifecycle test."""
            return None

    runner = object.__new__(agentic.AgenticCodexRunner)
    runner.repo_path = tmp_path
    runner.index_path = tmp_path / "index.json"
    runner.agentic_manifest = {}
    runner.transport = None
    runner.adapter = Adapter()
    monkeypatch.setattr(agentic, "_validate_agentic_runtime", lambda *_args: None)
    successful = runner.run(task, "A_plain", repetition=1, oracle=truth)
    assert successful.success is True
    assert refreshes == [home.path]

    def _contaminated(*_args: Any) -> None:
        """Reject the runtime as though locked index bytes had changed."""
        raise ValueError("locked index bytes changed")

    monkeypatch.setattr(agentic, "_validate_agentic_runtime", _contaminated)
    failed = runner.run(task, "A_plain", repetition=1, ground_truth=truth)
    assert failed.success is False
    assert failed.incomplete is True
    assert failed.error_type == "runtime_contamination"


def test_scratch_left_in_the_coordination_gate_is_recorded_beside_a_passing_cell(
    agentic: Any, monkeypatch: pytest.MonkeyPatch, task_and_truth: tuple[Any, Any], tmp_path: Path
) -> None:
    """A cell that answers correctly still passes after writing scratch into its coordination gate.

    The gate is the only path a measured cell's sandbox may write to, so scratch there is permitted behaviour. It is
    reported on the row as an observation about how the cell worked; a paid run lost a correct answer because the same
    files were scored as an execution failure instead.
    """
    task, truth = task_and_truth
    gate = agentic._structural.prepare_coordination_root(tmp_path / "index-dir")
    (gate / "analyze_imports.py").write_text("print(1)\n", encoding="utf-8")

    class Home:
        """Disposable home whose coordination gate holds the cell's leftover scratch."""

        auth_provisioned = False
        codemap_skill_path = None
        coordination_path = gate

        def __init__(self) -> None:
            """Initialize the test double's fixture-controlled state."""
            self.env = {}
            self.path = tmp_path / "home"

        def cleanup(self) -> None:
            """Implement the test home or workspace cleanup boundary."""

    class Adapter:
        """Native transport seam returning one correct answer without a model."""

        timeout = 600.0
        _auth_state = None

        def _prepare_verified_home(self, _arm: str) -> Home:
            """Return the fixture home through the runner's preparation interface."""
            return Home()

        def build_command(self, _prompt: str) -> list[str]:
            """Return fixed argv without launching the Codex executable."""
            return ["codex", "exec"]

        def _subprocess(self, _command: list[str], _env: dict[str, str], *, timeout: float) -> str:
            """Return a successful answer stream built from the fixture oracle."""
            del timeout
            return _stream(_message(_labelled("lightning.pytorch.trainer.trainer.", truth)), _completed())

    runner = object.__new__(agentic.AgenticCodexRunner)
    runner.repo_path = tmp_path
    runner.index_path = tmp_path / "index.json"
    runner.agentic_manifest = {}
    runner.transport = None
    runner.adapter = Adapter()
    monkeypatch.setattr(agentic, "_validate_agentic_runtime", lambda *_args: None)

    result = runner.run(task, "A_plain", repetition=1, oracle=truth)

    assert result.success is True
    assert result.incomplete is False
    assert result.error_type == ""
    assert result.coordination_scratch_entries == ["analyze_imports.py"]
    assert not gate.exists()


@pytest.mark.parametrize(
    ("stream", "error_type"),
    [
        pytest.param("{\n" + _stream(_completed()), "malformed_stream", id="malformed"),
        pytest.param(_stream({"type": "turn.failed", "error": "transport failed"}), "turn_failed", id="failed"),
        pytest.param(_stream(_message("unfinished")), "missing_terminal", id="incomplete"),
    ],
)
def test_malformed_incomplete_and_error_streams_fail_closed(
    task_and_truth: tuple[Any, Any], agentic: Any, stream: str, error_type: str
) -> None:
    """Malformed, incomplete, and provider-error native streams cannot become successful rows."""
    task, truth = task_and_truth
    result = agentic.parse_agentic_stream(stream, arm="A_plain", task=task, ground_truth=truth)

    assert result.success is False
    assert result.incomplete is True
    assert result.error_type == error_type


@pytest.mark.parametrize(
    "answer_text",
    ["Final answer without the required envelope.", "BEGIN_ANSWER_JSON\n{not-json}\nEND_ANSWER_JSON"],
)
def test_completed_invalid_answer_is_unscored_without_becoming_transport_failure(
    task_and_truth: tuple[Any, Any], agentic: Any, answer_text: str, tmp_path: Path
) -> None:
    """Malformed answer syntax remains a wire failure, not a fabricated zero semantic score."""
    task, truth = task_and_truth
    result = agentic.parse_agentic_stream(
        _stream(_message(answer_text), _completed()), arm="A_plain", task=task, ground_truth=truth
    )

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
    line = agentic._progress_line(1, 1, result)
    assert line.startswith("(1/1) ✗  ")
    assert "component=n/a" in line
    assert "pass=" not in line and "status=" not in line
    assert "EREC=" in line and "RREC=" in line and "DEFF=" in line
    assert "answer:✗" in agentic._progress_line(1, 1, result)
    assert "correct:" not in agentic._progress_line(1, 1, result)

    telemetry_path = tmp_path / "telemetry.jsonl"
    agentic._append_telemetry(telemetry_path, result, execution_index=1)
    telemetry = json.loads(telemetry_path.read_text(encoding="utf-8"))
    assert telemetry["success"] is True
    assert telemetry["answer_error"] == result.answer_error
    assert telemetry["quality"] is None
    assert telemetry["evidence"] == {"deff": 0.0, "erec": 0.0, "rrec": 0.0}


@pytest.mark.parametrize(
    ("success", "incomplete", "adherent", "correct", "symbol"),
    [
        pytest.param(True, False, True, True, "✓", id="fully-passing"),
        pytest.param(True, False, True, False, "✗", id="partial-answer"),
        pytest.param(True, False, False, True, "✗", id="treatment-violation"),
        pytest.param(False, False, True, True, "!", id="execution-failed-despite-correct-answer"),
        pytest.param(True, True, True, True, "!", id="incomplete-despite-correct-answer"),
        pytest.param(False, True, False, False, "!", id="execution-failure-precedes-other-failures"),
    ],
)
def test_progress_symbol_distinguishes_execution_failure_from_pass(
    task_and_truth: tuple[Any, Any],
    agentic: Any,
    success: bool,
    incomplete: bool,
    adherent: bool,
    correct: bool,
    symbol: str,
) -> None:
    """Use a single verdict symbol with execution failure taking priority over answer correctness."""
    task, truth = task_and_truth
    result = agentic.parse_agentic_stream(
        _stream(_message(_labelled("", truth)), _completed()), arm="A_plain", task=task, ground_truth=truth
    )
    result = replace(
        result,
        success=success,
        incomplete=incomplete,
        treatment_adherence=adherent,
        quality=replace(
            result.quality,
            correct=correct,
            quality_score=1.0 if correct else 2 / 3,
            graded_score=1.0 if correct else 56 / 57,
        ),
    )

    line = agentic._progress_line(3, 48, result)

    assert line.startswith(f"(3/48) {symbol}  BA-01")
    assert "pass=" not in line and "status=" not in line
    assert f"component={'1.000' if correct else '0.667'}" in line
    expected_grade = 0.0 if not success or incomplete or not adherent else (1.0 if correct else 56 / 57)
    assert f"quality={expected_grade:.1%}" in line
    assert "outcome: ✓ pass, ✗ completed but not passing, ! execution failed or incomplete" in agentic._OUTPUT_LEGEND


@pytest.mark.parametrize(
    ("deff", "display"),
    [
        pytest.param(0.0, "0.00", id="zero"),
        pytest.param(5.4, "5.40", id="trailing-zero"),
        pytest.param(23.333333, "23.33", id="above-ten"),
        pytest.param(123.456, "123.46", id="rounding"),
    ],
)
def test_progress_deff_uses_two_decimals_without_changing_evidence(
    task_and_truth: tuple[Any, Any],
    agentic: Any,
    deff: float,
    display: str,
) -> None:
    """Round the unbounded display metric while retaining full-precision telemetry evidence."""
    task, truth = task_and_truth
    result = agentic.parse_agentic_stream(
        _stream(_message(_labelled("", truth)), _completed()), arm="A_plain", task=task, ground_truth=truth
    )
    result = replace(result, evidence=replace(result.evidence, deff=deff))

    line = agentic._progress_line(1, 1, result)

    assert f" DEFF={display} " in line
    assert "component=1.000  EREC=1.000  RREC=1.000" in line
    assert result.evidence.deff == deff


def test_unique_bare_json_is_diagnostic_only_but_keeps_semantic_and_evidence_scores(
    task_and_truth: tuple[Any, Any], agentic: Any, tmp_path: Path
) -> None:
    """Recoverable bare JSON diagnoses content without weakening the pooled wire contract."""
    task, truth = task_and_truth
    bare_answer = json.dumps(dict(truth.expected), sort_keys=True)
    result = agentic.parse_agentic_stream(
        _stream(_message(bare_answer), _completed()), arm="A_plain", task=task, ground_truth=truth
    )

    assert result.success is True
    assert result.error_type == "answer_contract_failed"
    assert result.answer_contract_valid is False
    assert result.diagnostic_only is True
    assert result.answer_pooling_eligible is False
    assert result.quality.quality_score == 1.0
    assert result.evidence.erec == 1.0
    assert result.evidence.rrec == 1.0
    assert "answer:△" in agentic._progress_line(1, 1, result)

    telemetry_path = tmp_path / "telemetry.jsonl"
    agentic._append_telemetry(telemetry_path, result, execution_index=1)
    telemetry = json.loads(telemetry_path.read_text(encoding="utf-8"))
    assert telemetry["diagnostic_only"] is True
    assert telemetry["answer_pooling_eligible"] is False
    assert telemetry["quality"]["quality_score"] == 1.0
    assert telemetry["evidence"]["erec"] == 1.0


def test_required_no_call_preserves_scored_row_but_fails_admission(
    task_and_truth: tuple[Any, Any], agentic: Any, tmp_path: Path
) -> None:
    """C_strict no-call evidence is retained for diagnosis rather than dropped from telemetry."""
    task, truth = task_and_truth
    skill_path = tmp_path / "SKILL.md"
    skill_path.write_text("# locked Codemap skill\n", encoding="utf-8")
    result = agentic.parse_agentic_stream(
        _stream(
            _message(_labelled("lightning.pytorch.trainer.trainer and lightning.pytorch.loops.fit_loop.", truth)),
            _completed(),
        ),
        arm="C_strict",
        task=task,
        ground_truth=truth,
        skill_path=skill_path,
    )

    assert result.success is True
    assert result.quality.scored is True
    assert result.compliance is False
    assert result.treatment_adherence is False


def test_identical_output_uses_shared_answer_contract_score_and_legacy_metrics(
    task_and_truth: tuple[Any, Any], agentic: Any
) -> None:
    """Codex uses the exact shared scorer rather than a second quality implementation."""
    task, truth = task_and_truth
    text = "lightning.pytorch.trainer.trainer and lightning.pytorch.loops.fit_loop import the timer module."
    result = agentic.parse_agentic_stream(
        _stream(_message(_labelled(text, truth)), _completed()), arm="A_plain", task=task, ground_truth=truth
    )
    report = _labelled(text, truth)
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


def test_report_recall_uses_only_text_after_the_last_tool(task_and_truth: tuple[Any, Any], agentic: Any) -> None:
    """RREC excludes exploratory prose emitted before the final tool call."""
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


def _relocated_worktree_index(tmp_path: Path) -> tuple[Path, Path, dict[str, Any], dict[str, str]]:
    """Build a clean committed worktree holding a root-relocated copy of a frozen index.

    Args:
        tmp_path: Directory the worktree, index, and manifest are created under.

    Returns:
        The worktree root, the relocated index path, an agentic manifest locking the frozen index
        bytes, and the relocation provenance a run in that worktree would carry.
    """
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
        "frozen_index_contract": {
            "raw_sha256": hashlib.sha256(frozen_bytes).hexdigest(),
            "git_sha": commit,
            "scan_version": 3,
        },
    }
    return repo, index_path, manifest, dict(relocation)


class TestRelocatedIndexAdmission:
    """A worktree run proves index identity through relocation provenance, never byte identity."""

    def test_valid_provenance_admits_a_worktree_index(self, agentic: Any, tmp_path: Path) -> None:
        """Relocation provenance admits an index whose bytes differ from the locked hash.

        Scenario: a run executes in its own worktree, so its index carries the worktree's
        ``scan_root`` and can never reproduce the manifest's locked raw hash; the provenance written
        by the relocation is what proves the graph is still the frozen one.
        """
        repo, index_path, manifest, relocation = _relocated_worktree_index(tmp_path)

        agentic._validate_agentic_runtime(manifest, repo, index_path, relocation)

        assert hashlib.sha256(index_path.read_bytes()).hexdigest() != relocation["frozen_index_sha256"]

    def test_provenance_naming_the_wrong_frozen_source_is_rejected(self, agentic: Any, tmp_path: Path) -> None:
        """Provenance whose frozen source is not the locked index is rejected.

        Scenario: a caller supplies provenance derived from some other frozen index; admitting it
        would let an unrelated graph enter the run under the locked manifest's authority.
        """
        repo, index_path, manifest, relocation = _relocated_worktree_index(tmp_path)
        relocation["frozen_index_sha256"] = "0" * 64

        with pytest.raises(ValueError, match="wrong frozen source"):
            agentic._validate_agentic_runtime(manifest, repo, index_path, relocation)

    def test_provenance_disagreeing_with_the_bytes_on_disk_is_rejected(self, agentic: Any, tmp_path: Path) -> None:
        """Provenance whose derived hash misses the on-disk index is rejected.

        Scenario: the relocated copy changed after its provenance was written, so the digest the
        run would attest to is no longer the index the model actually reads.
        """
        repo, index_path, manifest, relocation = _relocated_worktree_index(tmp_path)
        relocation["derived_index_sha256"] = "1" * 64

        with pytest.raises(ValueError, match="changed after relocation"):
            agentic._validate_agentic_runtime(manifest, repo, index_path, relocation)

    def test_absent_provenance_keeps_the_byte_gate(self, agentic: Any, tmp_path: Path) -> None:
        """Without provenance a non-locked index is still rejected on its bytes.

        Scenario: the same relocated index is offered by a run that claims no relocation; the
        original byte-identity gate must reject it exactly as before.
        """
        repo, index_path, manifest, _relocation = _relocated_worktree_index(tmp_path)

        with pytest.raises(ValueError, match="agentic Codex run requires the locked frozen index bytes"):
            agentic._validate_agentic_runtime(manifest, repo, index_path)


def test_probe_rows_share_the_structural_column_layout(agentic: Any) -> None:
    """Every agentic probe row puts its fields in the same tab-free columns as the other lanes.

    Scenario: the three lanes each padded their probe rows differently, so the same capability read
    printed in three shapes across one combined run log. All three now go through one formatter,
    and the longer C_strict label must not push its own fields out of the shared column.
    """
    rows = [agentic._format_probe(agentic.probe_arm(arm)) for arm in AGENTIC_ARMS]

    assert not any("\t" in row for row in rows)
    assert {row.index("use=") for row in rows} == {rows[0].index("use=")}


def test_probe_rows_separate_the_optional_and_required_arms(agentic: Any) -> None:
    """B_auto and C_strict probe rows state different Codemap obligations.

    Scenario: both arms find the binary, so a row carrying only ``codemap=true`` read identically
    for an arm that may use Codemap and an arm that must. The measured availability stays where it
    was — a missing binary must still surface as a failure — and the contract is named beside it.
    """
    optional, required = (agentic._format_probe(agentic.probe_arm(arm)) for arm in ("B_auto", "C_strict"))

    assert "codemap=true" in optional and "codemap=true" in required
    assert "use=optional" in optional
    assert "use=required" in required


def test_emitted_legend_keeps_the_framed_plain_form_when_redirected(
    agentic: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """A redirected agentic legend writes exactly the framed text the run log also archives.

    Scenario: the legend now renders as a Rich panel on a terminal, but the same run writes the
    plain constant into its own log file. A drift between the two would make an archived run
    unreplayable against the stream it was captured from.
    """
    agentic._emit_output_legend()

    assert capsys.readouterr().out == f"{agentic._OUTPUT_LEGEND}\n"


def test_arm_rows_forward_through_shared_renderer_and_keep_logs_ansi_free(
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


def test_selected_stratum_hashes_into_its_own_scope(agentic: Any) -> None:
    """A declared stratum other than the manifest default resolves to a scope of its own.

    Scenario: the agentic lane runs one stratum per study, and the study's identity is its scope
    hash. Leaving the stratum out of that hash would let one approval token pay for a study of any
    declared model, which is how three executions of the default model were once published as
    studies of the two strata an operator had actually named.
    """
    default = agentic.resolve_agentic_scope()
    selected = agentic.resolve_agentic_scope(model="gpt-5.6-terra")
    named_default = agentic.resolve_agentic_scope(model=_default_stratum(agentic))

    assert selected["scope_sha256"] != default["scope_sha256"]
    assert selected["total_cells"] == default["total_cells"]
    assert named_default["scope_sha256"] == default["scope_sha256"]


def test_undeclared_stratum_is_refused_before_any_scope_exists(agentic: Any) -> None:
    """A model the manifest never declared fails instead of becoming the study's model.

    Scenario: the selection reaches the transport that spends money, so free text here would buy a
    run of whatever the operator typed. Only the manifest's own default and its declared additional
    strata may be selected.
    """
    with pytest.raises(ValueError, match="Codex provider parity requires one of"):
        agentic.resolve_agentic_scope(model="gpt-5.3-codex")


def test_paid_run_of_a_selected_stratum_binds_and_records_it(
    agentic: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Paid execution of another declared stratum demands that stratum's scope and records its name.

    Scenario: the run metadata is the only durable statement of which model was executed, and the
    approval is the only gate before the money is spent. A selected stratum admitted by the manifest
    digest would let the default study's token pay for it, and metadata naming the manifest default
    would publish it as the model it was not.
    """
    manifest_path, manifest_approval, index_path, _launcher_path = _prepare_paid_fixture(agentic, monkeypatch, tmp_path)
    run_dir = tmp_path / "stratum"
    _manifest_approval, launcher_path = _lock_run_launcher(manifest_path, run_dir)
    scope_approval = agentic.resolve_agentic_scope(manifest_path, model="gpt-5.6-terra")["scope_sha256"]

    agentic.run_paid(
        repo_path=tmp_path,
        index_path=index_path,
        auth_source=tmp_path / "auth-not-read.json",
        approval_sha256=scope_approval,
        run_dir=run_dir,
        manifest_path=manifest_path,
        model="gpt-5.6-terra",
        scope_sha256=scope_approval,
        runner_factory=_FixtureRunner,
        invocation_launcher_path=launcher_path,
    )

    metadata = json.loads((run_dir / "run-metadata.json").read_text())
    assert metadata["execution"]["model"] == "gpt-5.6-terra"
    assert metadata["approval_sha256"] == scope_approval
    assert manifest_approval != scope_approval


def test_paid_run_of_a_selected_stratum_refuses_the_default_study_token(
    agentic: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The token that admits the manifest's own stratum does not admit another one.

    Scenario: the manifest digest authorizes the study the manifest describes. Accepting it for a
    different stratum would make the approval a permission to run any declared model rather than the
    one the operator reviewed and priced.
    """
    manifest_path, _approval, index_path, _launcher_path = _prepare_paid_fixture(agentic, monkeypatch, tmp_path)
    run_dir = tmp_path / "mismatched"
    manifest_approval, launcher_path = _lock_run_launcher(manifest_path, run_dir)

    with pytest.raises(ValueError, match="nondefault agentic scope"):
        agentic.run_paid(
            repo_path=tmp_path,
            index_path=index_path,
            auth_source=tmp_path / "auth-not-read.json",
            approval_sha256=manifest_approval,
            run_dir=run_dir,
            manifest_path=manifest_path,
            model="gpt-5.6-terra",
            runner_factory=_FixtureRunner,
            invocation_launcher_path=launcher_path,
        )


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


def test_cyclic_schedule_balances_three_repetitions_and_records_each_cell(agentic: Any) -> None:
    """Three screening repeats put each arm in every cyclic position equally.

    A future controlled study needs order balance without random state or a second scheduler.  This checks every
    coordinate instead of merely counting cells, so duplicate or fixed A/B/C ordering cannot pass by accident.
    """
    schedule = agentic._scheduled_coordinates(("BA-01", "BA-02"), repetitions=3, cyclic=True)

    by_coordinate = {(coordinate["task_id"], coordinate["repetition"]): [] for coordinate in schedule}
    for coordinate in schedule:
        by_coordinate[(coordinate["task_id"], coordinate["repetition"])].append(coordinate["arm"])

    assert list(by_coordinate.values()) == [
        ["A_plain", "B_auto", "C_strict"],
        ["B_auto", "C_strict", "A_plain"],
        ["C_strict", "A_plain", "B_auto"],
        ["B_auto", "C_strict", "A_plain"],
        ["C_strict", "A_plain", "B_auto"],
        ["A_plain", "B_auto", "C_strict"],
    ]
    assert {coordinate["arm"] for coordinate in schedule[::3]} == set(AGENTIC_ARMS)
    assert [
        coordinate["arm"]
        for coordinate in agentic._scheduled_coordinates(
            ("BA-02",), repetitions=1, cyclic=True, task_ordinals={"BA-02": 1}
        )
    ] == ["B_auto", "C_strict", "A_plain"]


def test_summary_includes_every_unobserved_assigned_cell(agentic: Any) -> None:
    """No completed telemetry must still expose the manifest-sized pass denominator."""
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
def test_native_summary_preserves_shared_granular_quality(
    agentic: Any, success: bool, legacy: float, grade: float | None, assigned: int, expected: float | None
) -> None:
    """Native telemetry must not overwrite the same shared summary consumed by Claude."""
    row = {
        "task_id": "synthetic-1",
        "repetition": 1,
        "arm": "A_plain",
        "success": success,
        "answer_contract_valid": True,
        "answer_pooling_eligible": True,
        "treatment_adherence": True,
        "quality": {"correct": legacy == 1.0, "quality_score": legacy, "graded_score": grade},
        "input_tokens": 123,
        "command_calls": 2,
    }
    task_ids = [f"synthetic-{number}" for number in range(1, assigned + 1)]
    shared = agentic.summarize_agentic([row], task_ids=task_ids, repetitions=1)
    summary = agentic._summarize_telemetry([row], task_ids=task_ids, repetitions=1)
    actual = summary["all_assigned"]["A_plain"]["quality_mean"]
    assert actual == pytest.approx(expected) if expected is not None else actual is None
    for arm, values in shared["all_assigned"].items():
        assert {key: summary["all_assigned"][arm][key] for key in values} == values
    assert summary["comparisons"] == shared["comparisons"]
    assert summary["all_assigned"]["A_plain"]["gross_input_tokens"] == 123
    assert summary["all_assigned"]["A_plain"]["native_command_calls"] == 2
    assert not set(agentic._aggregate_arm_rows([row])) & set(shared["all_assigned"]["A_plain"])


def test_summary_separates_all_assigned_from_matched_adherent_rows(agentic: Any) -> None:
    """A failed C treatment never silently removes its expensive A/B comparison cells."""
    rows = [
        {
            "task_id": "BA-01",
            "repetition": 1,
            "arm": arm,
            "input_tokens": 100,
            "cached_input_tokens": 20,
            "fresh_input_tokens": 80,
            "output_tokens": 10,
            "elapsed_s": 2.0,
            "codemap_used": arm != "A_plain",
            "treatment_adherence": arm != "C_strict",
            "quality": {"quality_score": 0.8},
        }
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


def test_diagnostic_replay_preserves_original_row_and_corrects_only_observation(agentic: Any, tmp_path: Path) -> None:
    """Replay creates a new immutable diagnostic without changing historical adherence or costs."""
    launcher = tmp_path / "installed" / "bin" / "codemap-py"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    raw_event = _query()
    raw_event["item"]["command"] = f"{launcher} query --compact rdeps lightning.pytorch.callbacks.timer"
    source = tmp_path / "telemetry.jsonl"
    original = {
        "task_id": "BA-01",
        "repetition": 1,
        "arm": "C_strict",
        "input_tokens": 123,
        "cached_input_tokens": 23,
        "output_tokens": 45,
        "treatment_adherence": False,
        "answer_contract_valid": True,
        "codemap_used": False,
        "raw_events": [raw_event, _completed()],
        "launcher_path": str(launcher),
    }
    source.write_text(json.dumps(original, sort_keys=True) + "\n", encoding="utf-8")
    replay_dir = tmp_path.with_name(f"{tmp_path.name}-replay")
    replay_dir.mkdir()
    destination = replay_dir / "replay.json"

    agentic.replay_diagnostic(source, destination)

    replay = json.loads(destination.read_text(encoding="utf-8"))
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


def test_diagnostic_replay_preserves_windows_launcher_text_on_non_windows_hosts(agentic: Any, tmp_path: Path) -> None:
    """Replay preserves a Windows launcher string instead of applying the host path syntax."""
    launcher = r"C:\agentic\bin\codemap-py"
    raw_event = _query()
    raw_event["item"]["command"] = f"{launcher} query --compact rdeps lightning.pytorch.callbacks.timer"
    source = tmp_path / "telemetry.jsonl"
    source.write_text(
        json.dumps(
            {
                "task_id": "BA-01",
                "repetition": 1,
                "arm": "C_strict",
                "launcher_path": launcher,
                "raw_events": [raw_event, _completed()],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    replay_dir = tmp_path.with_name(f"{tmp_path.name}-replay")
    replay_dir.mkdir()
    destination = replay_dir / "replay.json"

    agentic.replay_diagnostic(source, destination)

    corrected = json.loads(destination.read_text(encoding="utf-8"))["rows"][0]
    assert corrected["corrected_observed_use"] is True
    assert corrected["launcher_provenance"] == "recorded_per_cell"


def test_a_c_pairing_does_not_depend_on_optional_b_adherence(agentic: Any) -> None:
    """An optional-use canary failure cannot erase an otherwise valid A/C comparison."""
    rows = [
        {
            "task_id": "BA-01",
            "repetition": 1,
            "arm": arm,
            "input_tokens": 10,
            "cached_input_tokens": 0,
            "fresh_input_tokens": 10,
            "output_tokens": 1,
            "elapsed_s": 1.0,
            "codemap_used": arm != "A_plain",
            "success": True,
            "answer_contract_valid": True,
            "answer_pooling_eligible": True,
            "treatment_adherence": arm != "B_auto",
            "quality": {"correct": True, "quality_score": 1.0},
        }
        for arm in AGENTIC_ARMS
    ]

    summary = agentic._summarize_telemetry(rows, task_ids=["BA-01"], repetitions=1)

    assert summary["comparisons"]["C_strict"]["outcomes"]["both_pass"] == 1
    assert summary["comparisons"]["C_strict"]["efficiency"]["input_tokens"]["paired_cells"] == 1
    assert summary["all_assigned"]["A_plain"]["observed_cells"] == 1
    assert summary["all_assigned"]["C_strict"]["observed_cells"] == 1
    assert summary["all_assigned"]["B_auto"]["passed_cells"] == 0


@pytest.mark.parametrize(
    "usage",
    [
        pytest.param(None, id="absent"),
        pytest.param({}, id="empty"),
        pytest.param({"input_tokens": -1, "output_tokens": 0}, id="malformed"),
    ],
)
def test_missing_native_usage_cannot_claim_token_savings(
    agentic: Any, task_and_truth: tuple[Any, Any], tmp_path: Path, usage: Any
) -> None:
    """A correct answer with unusable usage remains pass but has no token efficiency measurement."""
    task, oracle = task_and_truth
    answer = "BEGIN_ANSWER_JSON\n" + json.dumps(dict(oracle.expected)) + "\nEND_ANSWER_JSON"
    result = agentic.parse_agentic_stream(
        _stream(_message(answer), {"type": "turn.completed", "usage": usage}),
        arm="A_plain",
        task=task,
        oracle=oracle,
    )
    assert result.quality.correct is True
    assert result.usage_complete is False
    path = tmp_path / "telemetry.jsonl"
    agentic._append_telemetry(path, result, 0)
    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["passed"] is True
    assert row["input_tokens"] is None
    assert row["fresh_input_tokens"] is None
    control = {
        **row,
        "usage_complete": True,
        "input_tokens": 100,
        "cached_input_tokens": 20,
        "fresh_input_tokens": 80,
        "output_tokens": 10,
    }
    treatment = {**row, "arm": "C_strict"}
    comparison = agentic._summarize_telemetry([control, treatment], task_ids=[task["id"]], repetitions=1)[
        "comparisons"
    ]["C_strict"]
    assert comparison["outcomes"]["both_pass"] == 1
    assert comparison["efficiency"]["input_tokens"]["paired_cells"] == 0
    assert comparison["efficiency"]["input_tokens"]["unavailable_pairs"] == 1
    assert comparison["efficiency"]["input_tokens"]["median_percent_change"] is None


@pytest.mark.parametrize("launcher", ["/private/tmp/frozen-c-home/bin/codemap-py", r"C:\agentic\bin\codemap-py"])
def test_diagnostic_replay_uses_reviewed_coordinate_map_without_relabeling_a_rows(
    agentic: Any, tmp_path: Path, launcher: str
) -> None:
    """Legacy absolute commands require a reviewed per-cell path while A needs none."""
    query = _query()
    query["item"]["command"] = f"{launcher} query --compact rdeps lightning.pytorch.callbacks.timer"
    source = tmp_path / "telemetry.jsonl"
    source.write_text(
        "\n".join(
            (
                json.dumps(
                    {
                        "task_id": "BA-01",
                        "repetition": 1,
                        "arm": "C_strict",
                        "launcher_path": "relative/codemap-py",
                        "raw_events": [query, _completed()],
                    }
                ),
                json.dumps({"task_id": "BA-01", "repetition": 1, "arm": "A_plain", "raw_events": [_completed()]}),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    launcher_map = tmp_path / "launchers.json"
    launcher_map.write_text(
        json.dumps(
            {
                "coordinates": [
                    {
                        "task_id": "BA-01",
                        "repetition": 1,
                        "arm": "C_strict",
                        "launcher_path": launcher,
                        "source_evidence": {"frozen_skill_sha256": "a" * 64},
                    },
                    {"task_id": "BA-01", "repetition": 1, "arm": "A_plain", "launcher_path": None},
                ]
            }
        ),
        encoding="utf-8",
    )
    replay_dir = tmp_path.with_name(f"{tmp_path.name}-replay")
    replay_dir.mkdir()
    output = replay_dir / "replay.json"

    agentic.replay_diagnostic(source, output, launcher_map_path=launcher_map)

    replay = json.loads(output.read_text(encoding="utf-8"))
    assert replay["rows"][0]["corrected_observed_use"] is True
    assert replay["rows"][0]["launcher_provenance"] == {
        "launcher_path": launcher,
        "source_evidence": {"frozen_skill_sha256": "a" * 64},
    }
    assert replay["rows"][1]["corrected_observed_use"] is False
    assert replay["rows"][1]["original"]["arm"] == "A_plain"
    assert (
        replay["source"]["coordinate_launcher_map"]["sha256"] == hashlib.sha256(launcher_map.read_bytes()).hexdigest()
    )


@pytest.mark.parametrize("launcher", ["/private/tmp/frozen-c-home/bin/codemap-py", r"C:\agentic\bin\codemap-py"])
def test_diagnostic_replay_marks_relative_launcher_without_map_unavailable(
    agentic: Any, tmp_path: Path, launcher: str
) -> None:
    """A relative recorded launcher cannot be mistaken for reviewed absolute provenance."""
    query = _query()
    query["item"]["command"] = f"{launcher} query --compact rdeps lightning.pytorch.callbacks.timer"
    source = tmp_path / "telemetry.jsonl"
    source.write_text(
        json.dumps(
            {
                "task_id": "BA-01",
                "repetition": 1,
                "arm": "C_strict",
                "launcher_path": "relative/codemap-py",
                "raw_events": [query, _completed()],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    replay_dir = tmp_path.with_name(f"{tmp_path.name}-replay")
    replay_dir.mkdir()
    output = replay_dir / "replay.json"
    agentic.replay_diagnostic(source, output)

    row = json.loads(output.read_text(encoding="utf-8"))["rows"][0]
    assert row["corrected_observed_use"] is None
    assert row["corrected_observed_use_status"] == "unavailable_launcher_provenance"
    assert row["launcher_provenance"] is None


def test_diagnostic_replay_rejects_output_inside_historical_run(agentic: Any, tmp_path: Path) -> None:
    """Derived replay data cannot be inserted into the immutable source directory."""
    source = tmp_path / "telemetry.jsonl"
    source.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="outside the historical telemetry directory"):
        agentic.replay_diagnostic(source, tmp_path / "observed-use-replay.json")


def test_replay_launcher_map_requires_object_evidence_and_known_coordinate_keys(agentic: Any, tmp_path: Path) -> None:
    """Reviewed absolute provenance cannot be a truthy placeholder or carry ignored fields."""
    map_path = tmp_path / "launchers.json"
    map_path.write_text(
        json.dumps(
            {
                "coordinates": [
                    {
                        "task_id": "BA-01",
                        "repetition": 1,
                        "arm": "C_strict",
                        "launcher_path": "/private/tmp/codemap-py",
                        "source_evidence": "reviewed",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid coordinate provenance"):
        agentic._load_replay_launcher_map(map_path)


def test_impact_cli_passes_native_factory_and_private_auth_only_to_paid_stage(
    agentic: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Impact CLI accepts only its own scope controls and binds auth outside the model environment."""
    observed: dict[str, object] = {}

    def _stage(**kwargs: object) -> None:
        """Capture the provider-stage boundary without creating a fixture or invoking Codex."""
        observed.update(kwargs)

    monkeypatch.setattr(agentic, "run_change_impact_stage", _stage)
    auth_source = tmp_path / "auth.json"
    agentic.main(
        study="change-impact",
        model="gpt-5.6-terra",
        timeout=73,
        run_dir=tmp_path / "result",
        paid_approval="a" * 16,
        scope_sha256="b" * 64,
        auth_source=auth_source,
    )

    assert observed["provider"] == "codex"
    assert observed["model"] == "gpt-5.6-terra"
    assert observed["timeout"] == 73
    assert observed["output_dir"] == tmp_path / "result"
    assert observed["paid_approval"] == "a" * 16
    assert observed["scope_sha256"] == "b" * 64
    assert callable(observed["runtime_factory"])
    assert observed["runtime_factory"].keywords["auth_source"] == auth_source

    observed.clear()
    agentic.main(study="change-impact", dry_run=True, timeout=600)

    assert observed["model"] == "gpt-5.6-terra"
    assert observed["runtime_factory"].keywords["auth_source"] is None
    with pytest.raises(SystemExit):
        agentic.main(study="change-impact", dry_run=True, timeout=True)
    assert "positive integer" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        agentic.main(study="change-impact", resolve_scope=True, model="unadmitted-model")
    assert "Codex provider parity requires" in capsys.readouterr().err
