"""Admission and stage-contract tests for Claude's immutable P1 studies."""

from __future__ import annotations

import contextlib
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

import pytest

BENCHMARKS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BENCHMARKS))

from _bench_common import paid_lifecycle  # noqa: E402


def _readcrop_row() -> dict[str, Any]:
    """Build an in-memory ReadCrop row with deterministic source and a minimal contract double.

    >>> row = _readcrop_row()
    >>> row["source"], row["contract"].provider_binding()
    ('def method(): pass\\n', {'task': 'readcrop'})
    """
    contract = SimpleNamespace(
        task_id="RC-01",
        oracle_sha256="a" * 64,
        source_sha256="b" * 64,
        provider_binding=lambda: {"task": "readcrop"},
    )
    return {
        "task": {"id": "RC-01", "prompt": "Describe Example.method."},
        "source": "def method(): pass\n",
        "contract": contract,
    }


def _readcrop_task(script_run_agentic: Any) -> dict[str, Any]:
    """Build a real ReadCrop contract from inline source without starting a provider process.

    >>> row = _readcrop_task(getfixture("script_run_agentic"))
    >>> row["contract"].task_id == row["task"]["id"]
    True
    """
    task = {
        "id": "RC-fixture",
        "type": "read_crop",
        "prompt": "Describe Example.method.",
        "symbol": "Example.method",
        "expected_keywords": ["value"],
    }
    source = "def method(self, value: int) -> None:\n    pass\n"
    return {
        "task": task,
        "source": source,
        "contract": script_run_agentic.build_readcrop_contract(task, source=source),
    }


def test_fix_single_dry_run_uses_the_shared_contract_and_selected_scope(
    script_run_agentic: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fix-Single needs the same provider-neutral selected-scope admission as Fix-Multi.

    Regression: Claude had only a legacy mutable-task loop, leaving no immutable
    Fix-Single preflight or scope approval before a paid invocation. The captured
    stream is redirected, so the stage banner takes its plain ``== title ==`` form
    and the machine-parsed PLAN and SCOPE lines stay byte-identical beneath it.
    """
    script_run_agentic.main(study="fix-single", tasks=["FS-01"], dry_run=True)

    output = capsys.readouterr().out
    lines = output.splitlines()
    assert lines[:-1] == [
        "== FIX-SINGLE PREFLIGHT (no model) ==",
        "PLAN    FS-01  rep=1  A_plain",
        "PLAN    FS-01  rep=1  B_auto",
        "PLAN    FS-01  rep=1  C_strict",
    ]
    assert lines[-1].startswith("SCOPE   ")


def test_fix_single_loader_and_scope_preserve_the_shared_oracle_binding(script_run_agentic: Any) -> None:
    """Claude cannot replace the provider-neutral Fix-Single oracle or task bytes.

    Regression: the Claude runner had no canonical Fix-Single loader, so its
    historical keyword scorer could diverge from the executable shared oracle.
    """
    loaded = script_run_agentic.load_claude_fix_single_tasks(selected_ids=["FS-01"])
    scope = script_run_agentic.resolve_claude_fix_single_scope(loaded)

    assert [item["contract"].task_id for item in loaded] == ["FS-01"]
    assert scope["study"] == "fix-single"
    assert scope["total_cells"] == 3
    binding = scope["contracts"]["FS-01"]
    assert {"canonical_task_sha256", "prompt_sha256", "baseline_commit", "oracle_sha256", "scorer_sha256"} <= set(
        binding
    )


def test_paid_readcrop_requires_fresh_directory_and_matching_scope_token_before_dispatch(
    script_run_agentic: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Paid admission must reject stale coordinates before the runner can launch Claude.

    Regression: the no-model ReadCrop adapter could not preserve immutable paid
    admission, so a stale scope or reused result directory had no safe recovery.
    """
    loaded = [_readcrop_row()]
    scope_sha256 = "a" * 64
    scope = {"scope_sha256": scope_sha256, "task_ids": ["RC-01"], "total_cells": 3}
    dispatched: list[dict[str, Any]] = []
    index_path = tmp_path / "index.json"
    index_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(script_run_agentic, "load_claude_readcrop_tasks", lambda *_args: loaded)
    monkeypatch.setattr(script_run_agentic, "resolve_readcrop_scope", lambda *_args: scope)
    monkeypatch.setattr(script_run_agentic, "_resolve_claude_paid_scope", lambda **_kwargs: scope)
    monkeypatch.setattr(script_run_agentic, "find_index", lambda _repo, index: Path(index))
    monkeypatch.setattr(script_run_agentic, "run_claude_paid_stage", lambda **kwargs: dispatched.append(kwargs))

    with pytest.raises(ValueError, match=r"received: stale") as stale:
        script_run_agentic.main(
            repo_path=tmp_path,
            index=index_path,
            study="readcrop",
            tasks=["RC-01"],
            model="haiku",
            run_dir=tmp_path / "fresh",
            paid_approval="stale",
        )

    assert "ERROR: cannot start paid Claude readcrop stage." in str(stale.value)
    assert "stale or missing --paid-approval" in str(stale.value)
    assert "received: stale" in str(stale.value)
    assert "required token: aaaaaaaaaaaaaaaa" in str(stale.value)
    assert scope_sha256 not in str(stale.value)
    assert "--dry-run" in str(stale.value)
    assert "python3 benchmarks/run-claude-agentic.py \\\n  --study readcrop \\\n" in str(stale.value)
    assert "No model call was made." in str(stale.value)
    assert dispatched == []

    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(ValueError, match=r"already exists"):
        script_run_agentic.main(
            repo_path=tmp_path,
            index=index_path,
            study="readcrop",
            tasks=["RC-01"],
            model="haiku",
            run_dir=existing,
            paid_approval="a" * 16,
        )
    assert dispatched == []

    fresh = tmp_path / "fresh"
    script_run_agentic.main(
        repo_path=tmp_path,
        index=index_path,
        study="readcrop",
        tasks=["RC-01"],
        model="haiku",
        run_dir=fresh,
        paid_approval="a" * 16,
    )
    assert len(dispatched) == 1
    assert dispatched[0]["study"] == "readcrop"
    assert dispatched[0]["run_dir"] == fresh
    assert dispatched[0]["scope"] == scope
    assert dispatched[0]["model"] == "haiku"
    assert [item["contract"].task_id for item in dispatched[0]["tasks"]] == ["RC-01"]


def test_paid_scope_hashes_the_path_launcher_used_by_headless_claude(
    script_run_agentic: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Paid approval changes with the exact ``codemap-py`` bytes placed on PATH.

    Regression: the harness hashed ``scan-query`` but invoked ``codemap-py``
    from a mutable user cache, so treatment provenance did not cover execution.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    index = tmp_path / "index.json"
    index.write_text("{}", encoding="utf-8")
    plugin = tmp_path / "codemap-py"
    for relative in (
        ".claude-plugin/plugin.json",
        "claude-skills/query-code/SKILL.md",
        "bin/codemap-py",
        "bin/scan-query",
    ):
        path = plugin / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative, encoding="utf-8")
    monkeypatch.setattr(script_run_agentic, "_validate_parity_runtime", lambda *_args: None)
    monkeypatch.setattr(script_run_agentic, "_repository_fingerprint", lambda _path: "fixture-commit")
    monkeypatch.setattr(script_run_agentic.ModelRunner, "_codemap_plugin_dir", staticmethod(lambda: str(plugin)))

    scope = script_run_agentic._resolve_claude_paid_scope(
        base_scope={"provider": "claude", "scope_sha256": "base"},
        repo_path=repo,
        index_path=index,
        model="haiku",
    )

    assert scope["treatment_sha256"]["bin/codemap-py"] == script_run_agentic._sha256_file(plugin / "bin" / "codemap-py")


def test_paid_patch_scope_and_snapshot_close_over_shared_runtime_bytes(
    script_run_agentic: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Claude Patch approval and artifacts bind the exact shared scorer/runtime closure."""
    repo = tmp_path / "repo"
    repo.mkdir()
    index = tmp_path / "index.json"
    index.write_text("{}", encoding="utf-8")
    plugin = tmp_path / "codemap-py"
    for relative in (
        ".claude-plugin/plugin.json",
        "claude-skills/query-code/SKILL.md",
        "bin/codemap-py",
        "bin/scan-query",
    ):
        path = plugin / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative, encoding="utf-8")
    contract = SimpleNamespace(task_id="PT-01", baseline_commit="a" * 40, provider_binding=lambda: {})
    loaded = [{"task": {"id": "PT-01"}, "contract": contract}]
    coordinates = {"PT-01": {"baseline_commit": "a" * 40, "index_sha256": "b" * 64, "raw_index_sha256": "b" * 64}}
    monkeypatch.setattr(script_run_agentic, "_validate_parity_runtime", lambda *_args: None)
    monkeypatch.setattr(script_run_agentic, "_repository_fingerprint", lambda _path: "fixture-commit")
    monkeypatch.setattr(script_run_agentic.ModelRunner, "_codemap_plugin_dir", staticmethod(lambda: str(plugin)))
    monkeypatch.setattr(script_run_agentic, "load_claude_patch_tasks", lambda *_args: loaded)
    monkeypatch.setattr(script_run_agentic, "validate_patch_index_bundle", lambda *_args: coordinates)

    scope = script_run_agentic._resolve_claude_paid_scope(
        base_scope={
            "provider": "claude",
            "study": "patch",
            "task_ids": ["PT-01"],
            "historical_baselines": {"PT-01": "a" * 40},
            "scope_sha256": "base",
        },
        repo_path=repo,
        index_path=index,
        model="haiku",
    )
    files = script_run_agentic._patch_snapshot_files()

    assert set(files) == {
        "claude-runner.py",
        "paid-lifecycle.py",
        "edit-patch-contracts.py",
        "mutation-isolation.py",
        "patch-index-locks.json",
    }
    assert scope["patch_test_runtime"]["invocation"] == "absolute pytest executable"
    hashes = scope["implementation_sha256"]
    assert hashes["claude_runner"] == script_run_agentic._sha256_file(files["claude-runner.py"])
    assert hashes["paid_lifecycle"] == script_run_agentic._sha256_file(files["paid-lifecycle.py"])
    assert hashes["edit_patch_contracts"] == script_run_agentic._sha256_file(files["edit-patch-contracts.py"])
    assert hashes["mutation_isolation"] == script_run_agentic._sha256_file(files["mutation-isolation.py"])
    assert hashes["patch_index_locks"] == script_run_agentic._sha256_file(files["patch-index-locks.json"])


def test_paid_fix_multi_dispatches_the_shared_executable_contract_once(
    script_run_agentic: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Fix-Multi must enter the one shared lifecycle, not a Claude-specific task×arm loop.

    Regression: Claude's earlier Fix-Multi adapter stopped at preflight, while
    the legacy loop used provider-specific keyword scoring instead of the shared
    executable contract.
    """
    contract = SimpleNamespace(task_id="FM-01", provider_binding=lambda: {"task": "fix-multi"})
    loaded = [{"task": {"id": "FM-01", "prompt": "Fix callers."}, "contract": contract}]
    scope = {"scope_sha256": "b" * 64, "task_ids": ["FM-01"], "total_cells": 3}
    dispatched: list[dict[str, Any]] = []
    index_path = tmp_path / "index.json"
    index_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(script_run_agentic, "load_claude_fix_multi_tasks", lambda *_args: loaded)
    monkeypatch.setattr(script_run_agentic, "resolve_claude_fix_multi_scope", lambda *_args: scope)
    monkeypatch.setattr(script_run_agentic, "_resolve_claude_paid_scope", lambda **_kwargs: scope)
    monkeypatch.setattr(script_run_agentic, "find_index", lambda _repo, index: Path(index))
    monkeypatch.setattr(script_run_agentic, "run_claude_paid_stage", lambda **kwargs: dispatched.append(kwargs))

    script_run_agentic.main(
        repo_path=tmp_path,
        index=index_path,
        study="fix-multi",
        tasks=["FM-01"],
        model="haiku",
        run_dir=tmp_path / "fresh",
        paid_approval="b" * 16,
    )

    assert len(dispatched) == 1
    assert dispatched[0]["study"] == "fix-multi"
    assert dispatched[0]["tasks"] == loaded
    assert dispatched[0]["scope"] == scope
    assert dispatched[0]["model"] == "haiku"


def test_paid_patch_dispatches_the_shared_executable_contract_once(
    script_run_agentic: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Patch tasks must use the existing paid lifecycle, not a second provider loop.

    Regression: the historical patch prototype lived only in the old Claude
    runner, leaving the canonical A/B/C adapter unable to select PT tasks.
    """
    contract = SimpleNamespace(task_id="PT-01", provider_binding=lambda: {"task": "patch"})
    loaded = [{"task": {"id": "PT-01", "prompt": "Fix the regression."}, "contract": contract}]
    scope = {"scope_sha256": "c" * 64, "task_ids": ["PT-01"], "total_cells": 3}
    dispatched: list[dict[str, Any]] = []
    index_path = tmp_path / "index.json"
    index_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(script_run_agentic, "load_claude_patch_tasks", lambda *_args: loaded)
    monkeypatch.setattr(script_run_agentic, "resolve_claude_patch_scope", lambda *_args: scope)
    monkeypatch.setattr(script_run_agentic, "_resolve_claude_paid_scope", lambda **_kwargs: scope)
    monkeypatch.setattr(script_run_agentic, "find_index", lambda _repo, index: Path(index))
    monkeypatch.setattr(script_run_agentic, "run_claude_paid_stage", lambda **kwargs: dispatched.append(kwargs))

    script_run_agentic.main(
        repo_path=tmp_path,
        index=index_path,
        study="patch",
        tasks=["PT-01"],
        model="haiku",
        run_dir=tmp_path / "fresh",
        paid_approval="c" * 16,
    )

    assert len(dispatched) == 1
    assert dispatched[0]["study"] == "patch"
    assert dispatched[0]["tasks"] == loaded
    assert dispatched[0]["scope"] == scope


def test_patch_row_keeps_quality_separate_from_pooling_eligibility(script_run_agentic: Any) -> None:
    """Claude Patch presentation avoids duplicating nonpoolability in quality."""
    row = {
        "study": "patch",
        "task_id": "PT-01",
        "arm": "A_plain",
        "pooling_eligible": False,
        "success": True,
        "quality_score": 1.0,
        "usage_complete": True,
        "input_tokens": 1,
        "output_tokens": 1,
        "command_calls": 1,
        "elapsed_s": 1.0,
        "primary_correct": True,
        "codemap_used": False,
        "execution": {"patch_applied": True, "targeted_test_passed": True},
    }

    rendered = script_run_agentic._format_claude_stage_row(row, completed=1, total=3)

    assert rendered.startswith("(1/3) ✗")
    assert "quality=1.000" in rendered
    assert "^" not in rendered


def test_claude_patch_strict_query_anchors_pt02_at_existing_class(script_run_agentic: Any) -> None:
    """Claude PT-02 uses the same pre-fix structural anchor as Codex."""
    assert script_run_agentic._PATCH_QUERY_ARGUMENTS["PT-02"] == ("symbol", "DistributedSamplerWrapper")


def test_claude_patch_commands_preserve_the_admitted_pytest_runtime(
    script_run_agentic: Any,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Patch launch and recovery commands retain the runtime bound into their scope.

    Regression: the dry-run admitted an explicit historical pytest runtime but
    printed a paid command without it, allowing the copied command to select a
    different interpreter or fail before model execution.
    """
    pytest_executable = "/runtime with space/bin/pytest"
    expected_assignment = f"{script_run_agentic.PATCH_PYTEST_ENV}='{pytest_executable}'"
    script_run_agentic._print_claude_paid_command(
        study="patch",
        repo_path=tmp_path,
        index_path=tmp_path / "index.json",
        model="haiku",
        task_ids=["PT-02"],
        scope_sha256="a" * 64,
        patch_pytest=pytest_executable,
    )

    paid_output = capsys.readouterr().out
    # The label and its rule precede the copyable command, so the assignment opens the third line.
    assert paid_output.splitlines()[2].startswith(f"{expected_assignment} python3 ")
    # The command is framed, so its last flag sits above the closing rule rather than at the end.
    assert paid_output.splitlines()[-2] == "  --paid-approval aaaaaaaaaaaaaaaa"
    assert paid_output.splitlines()[-1] == "-" * 78

    script_run_agentic._require_claude_paid_request(
        study="patch",
        run_dir=tmp_path / "accepted",
        paid_approval="a" * 16,
        scope={
            "scope_sha256": "a" * 64,
            "task_ids": ["PT-02"],
            "patch_test_runtime": {"pytest_executable": pytest_executable},
        },
        repo_path=tmp_path,
        index_path=tmp_path / "index.json",
        model="haiku",
    )

    with pytest.raises(ValueError) as stale:
        script_run_agentic._require_claude_paid_request(
            study="patch",
            run_dir=tmp_path / "fresh",
            paid_approval="stale",
            scope={
                "scope_sha256": "b" * 64,
                "task_ids": ["PT-02"],
                "patch_test_runtime": {"pytest_executable": pytest_executable},
            },
            repo_path=tmp_path,
            index_path=tmp_path / "index.json",
            model="haiku",
        )

    assert str(stale.value).count(expected_assignment) == 2


def test_shared_paid_lifecycle_retains_completed_claude_cells_after_a_transport_failure(tmp_path: Path) -> None:
    """A stream failure preserves completed Claude evidence, ledger, and adapter cleanup.

    Regression: a provider-owned loop can lose evidence from the first cell or
    bypass final cleanup/checksums when a later Claude stream fails.
    """
    events: list[tuple[str, object]] = []

    def _persist(path: Path, payload: Any) -> None:
        """Replace lifecycle metadata with sorted UTF-8 JSON."""
        path.write_text(json.dumps(dict(payload), sort_keys=True), encoding="utf-8")

    def _run_cell(task: str, arm: str) -> dict[str, Any]:
        """Record each attempted arm and simulate transport failure for the optional-use arm."""
        events.append(("cell", (task, arm)))
        if arm == "B_auto":
            raise RuntimeError("fake Claude transport failure")
        return {"task_id": task, "arm": arm, "tool_result_tokens": None}

    def _validate(_task: str, _arm: str, row: dict[str, Any]) -> None:
        """Require unavailable tool-result usage to remain unknown rather than becoming zero."""
        assert row["tool_result_tokens"] is None

    callbacks = paid_lifecycle.PaidStageCallbacks(
        run_cell=_run_cell,
        validate_row=_validate,
        prepare_run=lambda run_dir: (run_dir / "input-snapshot.json").write_text("{}", encoding="utf-8"),
        persist_metadata=_persist,
        emit_lifecycle=lambda kind, payload: events.append((kind, dict(payload))),
        emit_row=lambda row, completed, total, arm: events.append(("row", (row, completed, total, arm))),
        write_checksums=paid_lifecycle.write_checksums,
        close_adapter=lambda: events.append(("closed", None)),
    )

    run_dir = tmp_path / "run"
    with pytest.raises(RuntimeError, match="fake Claude transport failure"):
        paid_lifecycle.run_paid_stage(
            tasks=["RC-01"], arms=["A_plain", "B_auto", "C_strict"], run_dir=run_dir, metadata={}, callbacks=callbacks
        )

    metadata = json.loads((run_dir / "run-metadata.json").read_text(encoding="utf-8"))
    telemetry = (run_dir / "telemetry.jsonl").read_text(encoding="utf-8").splitlines()
    assert metadata["status"] == "failed"
    assert metadata["persisted_cells"] == 1
    assert len(telemetry) == 1
    assert (run_dir / "checksums.sha256").is_file()
    paid_lifecycle.verify_checksums(run_dir)
    assert events[-2:] == [("summary", {"persisted_cells": 1, "status": "failed", "total_cells": 3}), ("closed", None)]


def test_paid_readcrop_fake_stream_persists_native_events_and_null_tool_usage(
    script_run_agentic: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Claude ReadCrop persistence carries native usage rather than invented tool tokens.

    Regression: provider adapters could estimate unavailable Claude tool payload
    tokens or discard event evidence before the immutable stage ledger is made.
    """
    entered: list[str] = []
    exited: list[str] = []
    source = tmp_path / "source"
    source.mkdir()
    index = tmp_path / "index.json"
    index.write_text("{}", encoding="utf-8")
    task = _readcrop_task(script_run_agentic)

    @contextlib.contextmanager
    def _workspace(_repo_path: Path, _index_path: Path, arm: str) -> Any:
        """Yield the fixture source through the workspace context-manager boundary."""
        entered.append(arm)
        try:
            yield source, None
        finally:
            exited.append(arm)

    answer = (
        "BEGIN_READ_CROP_JSON\n"
        '{"signature":"Example.method(self, value: int) -> None","parameters":["self","value"],'
        '"behavior":"Records the supplied value."}\nEND_READ_CROP_JSON'
    )
    native_events = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": answer}]}},
        {"type": "result", "subtype": "success", "usage": {"input_tokens": 12, "output_tokens": 3}},
    ]

    class FakeRunner:
        """Return a deterministic local stream without launching Claude."""

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            """Initialize the test double's fixture-controlled state."""
            pass

        def run_stage_events(self, **_kwargs: Any) -> tuple[list[dict[str, Any]], float, None]:
            """Return synthetic native events and elapsed time without starting Claude."""
            return native_events, 0.25, None

    monkeypatch.setattr(script_run_agentic, "ModelRunner", FakeRunner)
    monkeypatch.setattr(script_run_agentic, "_claude_readcrop_workspace", _workspace)
    monkeypatch.setattr(script_run_agentic, "_source_pair_unchanged", lambda *_args: True)

    run_dir = tmp_path / "run"
    script_run_agentic.run_claude_paid_stage(
        study="readcrop",
        tasks=[task],
        repo_path=source,
        index_path=index,
        manifest_path=script_run_agentic.PARITY_MANIFEST_PATH,
        tasks_path=script_run_agentic.READCROP_TASKS_PATH,
        model="haiku",
        run_dir=run_dir,
        scope={"scope_sha256": "scope"},
    )

    rows = [json.loads(line) for line in (run_dir / "telemetry.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [row["arm"] for row in rows] == ["A_plain", "B_auto", "C_strict"]
    assert all(row["tool_result_tokens"] is None for row in rows)
    assert all(row["raw_events"] == native_events for row in rows)
    assert rows[-1]["success"] is False  # valid answer, but C has no Codemap query in the fake stream
    assert entered == exited == ["A_plain", "B_auto", "C_strict"]
    output = capsys.readouterr().out
    assert f"ARTIFACTS:\n - telemetry={run_dir / 'telemetry.jsonl'}" in output
    assert f" - metadata={run_dir / 'run-metadata.json'}" in output
    paid_lifecycle.verify_checksums(run_dir)


def test_paid_claude_rows_forward_to_the_shared_rich_renderer(
    script_run_agentic: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Canonical Claude rows cannot regress to provider-local print or color handling.

    Regression: the first Claude P1 lifecycle duplicated arm styles and printed
    redirected rows itself, allowing its compact output to drift from Codex.
    """
    task = _readcrop_task(script_run_agentic)
    source = tmp_path / "source"
    source.mkdir()
    index = tmp_path / "index.json"
    index.write_text("{}", encoding="utf-8")
    rendered: list[tuple[str, str, Any]] = []

    @contextlib.contextmanager
    def _workspace(_repo_path: Path, _index_path: Path, _arm: str) -> Any:
        """Yield the fixture source through the workspace context-manager boundary."""
        yield source, None

    class FakeRunner:
        """Return one deterministic answer without launching Claude."""

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            """Initialize the test double's fixture-controlled state."""
            pass

        def run_stage_events(self, **_kwargs: Any) -> tuple[list[dict[str, Any]], float, None]:
            """Return synthetic native events and elapsed time without starting Claude."""
            return (
                [
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "BEGIN_READ_CROP_JSON\n"
                                        '{"signature":"Example.method(self, value: int) -> None",'
                                        '"parameters":["self","value"],"behavior":"Records value."}\n'
                                        "END_READ_CROP_JSON"
                                    ),
                                }
                            ]
                        },
                    },
                    {"type": "result", "subtype": "success", "usage": {"input_tokens": 12, "output_tokens": 3}},
                ],
                0.25,
                None,
            )

    monkeypatch.setattr(script_run_agentic, "ModelRunner", FakeRunner)
    monkeypatch.setattr(script_run_agentic, "_claude_readcrop_workspace", _workspace)
    monkeypatch.setattr(script_run_agentic, "_source_pair_unchanged", lambda *_args: True)
    monkeypatch.setattr(
        script_run_agentic.presentation,
        "print_arm_row",
        lambda row, arm, *, console: rendered.append((row, arm, console)),
    )

    script_run_agentic.run_claude_paid_stage(
        study="readcrop",
        tasks=[task],
        repo_path=source,
        index_path=index,
        manifest_path=script_run_agentic.PARITY_MANIFEST_PATH,
        tasks_path=script_run_agentic.READCROP_TASKS_PATH,
        model="haiku",
        run_dir=tmp_path / "run-renderer",
        scope={"scope_sha256": "scope"},
    )

    assert [arm for _row, arm, _console in rendered] == ["A_plain", "B_auto", "C_strict"]
    assert all("quality=" in row and "time=" in row and "in=" in row for row, _arm, _console in rendered)
    assert all(console is script_run_agentic._console for _row, _arm, console in rendered)


def test_timeout_stream_recovers_partial_input_without_inventing_final_output(script_run_agentic: Any) -> None:
    """A missing terminal result cannot turn measured timeout usage into zero.

    Regression: timed-out executable cells contained repeated assistant usage
    events but rendered ``in=0 out=0`` because only the absent result event was
    parsed. Repeated stream events for one provider message must count once.
    """
    first = {
        "type": "assistant",
        "message": {
            "id": "msg-1",
            "usage": {
                "input_tokens": 3,
                "cache_creation_input_tokens": 5,
                "cache_read_input_tokens": 7,
                "output_tokens": 11,
            },
            "content": [{"type": "text", "text": "partial"}],
        },
    }
    second = {
        "type": "assistant",
        "message": {
            "id": "msg-2",
            "usage": {
                "input_tokens": 13,
                "cache_creation_input_tokens": 17,
                "cache_read_input_tokens": 19,
                "output_tokens": 23,
            },
            "content": [],
        },
    }

    summary = script_run_agentic._claude_event_summary([first, first, second])

    assert summary["usage_complete"] is False
    assert summary["usage_source"] == "partial_stream"
    assert summary["usage"].input_tokens == 64
    assert summary["usage"].cache_creation_tokens == 22
    assert summary["usage"].cache_read_tokens == 26
    assert summary["usage"].output_tokens == 0


def test_incomplete_usage_row_marks_partial_input_and_unknown_output(script_run_agentic: Any) -> None:
    """Human timeout rows distinguish a lower-bound input count from unknown output."""
    rendered = script_run_agentic._format_claude_stage_row(
        {
            "success": False,
            "task_id": "FS-01",
            "arm": "A_plain",
            "input_tokens": 2_085_045,
            "output_tokens": 0,
            "usage_complete": False,
            "command_calls": 62,
            "elapsed_s": 210.0,
            "quality_score": 0.0,
            "study": "fix-single",
            "codemap_used": False,
            "execution": {"patch_applied": False, "targeted_test_passed": False},
        },
        completed=1,
        total=12,
    )

    assert "in= >2.1M" in rendered
    assert "out=    ?" in rendered


def test_executable_stage_row_uses_pooling_eligibility_for_its_leading_glyph(script_run_agentic: Any) -> None:
    """A transport-complete but oracle-failed cell must render as ineligible.

    Regression: Claude rendered the leading check from transport ``success``,
    which made an invalid executable patch look comparable to a passing cell.
    """
    rendered = script_run_agentic._format_claude_stage_row(
        {
            "success": True,
            "pooling_eligible": False,
            "task_id": "FS-01",
            "arm": "C_strict",
            "input_tokens": 12,
            "output_tokens": 3,
            "command_calls": 2,
            "elapsed_s": 1.0,
            "quality_score": 0.0,
            "study": "fix-single",
            "codemap_used": True,
            "execution": {"patch_applied": True, "targeted_test_passed": False},
        },
        completed=1,
        total=3,
    )

    assert rendered.startswith("(1/3) ✗")
    assert "quality=0.000" in rendered
    assert "patch=✓ oracle=✗ codemap=✓" in rendered


@pytest.mark.parametrize(
    ("study", "task_id", "loader_name", "tasks_path_name", "executor_name"),
    [
        pytest.param(
            "fix-single",
            "FS-01",
            "load_claude_fix_single_tasks",
            "FIX_SINGLE_TASKS_PATH",
            "execute_fix_single_patch",
            id="fix-single",
        ),
        pytest.param(
            "fix-multi",
            "FM-01",
            "load_claude_fix_multi_tasks",
            "FIX_MULTI_TASKS_PATH",
            "execute_fix_multi_patch",
            id="fix-multi",
        ),
    ],
)
def test_paid_executable_stage_preserves_canonical_diff_oracle_and_workspace_cleanup(
    script_run_agentic: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    study: str,
    task_id: str,
    loader_name: str,
    tasks_path_name: str,
    executor_name: str,
) -> None:
    """One executable cell reaches the shared oracle and cleans every editable worktree.

    Regression: a provider-owned fix loop could score Claude prose or a sandbox
    test directly, rather than capturing its canonical diff, applying it in the
    clean shared oracle workspace, and always removing the mutable checkout.
    """
    task = getattr(script_run_agentic, loader_name)(selected_ids=[task_id])[0]
    expected_query = (
        script_run_agentic._FIX_SINGLE_QUERY_ARGUMENTS
        if study == "fix-single"
        else script_run_agentic._FIX_MULTI_QUERY_ARGUMENTS
    )[task_id]
    created: list[Any] = []
    cleaned: list[Path] = []
    captured_diffs: list[str] = []
    executed: list[tuple[Path, Any, str]] = []
    writable_modes: list[bool] = []
    repo_path = tmp_path / "source"
    repo_path.mkdir()
    index_path = tmp_path / "index.json"
    index_path.write_text("{}", encoding="utf-8")

    class FakeWorkspace:
        """Record the canonical worktree protocol without making a Git worktree."""

        def __init__(self, number: int) -> None:
            """Initialize the test double's fixture-controlled state."""
            self.worktree = tmp_path / f"agent-worktree-{number}"
            self.worktree.mkdir()
            self.index_path = self.worktree / "derived-index.json"
            self.index_path.write_text("{}", encoding="utf-8")

        def capture_diff(self) -> str:
            """Record and return the canonical diff associated with this workspace."""
            diff = f"canonical diff {self.worktree.name}"
            captured_diffs.append(diff)
            return diff

        def index_unchanged(self) -> bool:
            """Report an unchanged index for this clean-workspace scenario."""
            return True

        def cleanup(self) -> bool:
            """Implement the test home or workspace cleanup boundary."""
            cleaned.append(self.worktree)
            return True

    def _create_workspace(*_args: Any) -> FakeWorkspace:
        """Create and retain a numbered workspace double for lifecycle assertions."""
        workspace = FakeWorkspace(len(created) + 1)
        created.append(workspace)
        return workspace

    class FakeExecution:
        """Expose the exact independent oracle result expected by the stage row."""

        def as_dict(self) -> dict[str, Any]:
            """Serialize passing patch, oracle, and cleanup evidence for the fixture contract."""
            return {
                "baseline_failed": True,
                "patch_applied": True,
                "changed_paths": list(task["contract"].expected_paths),
                "targeted_test_passed": True,
                "recount_recoverable": False,
                "recount_oracle_passed": None,
                "cleanup_verified": True,
                "error": None,
            }

    def _execute(source: Path, contract: Any, diff: str) -> FakeExecution:
        """Record patch execution inputs and return passing execution evidence."""
        executed.append((source, contract, diff))
        return FakeExecution()

    class FakeRunner:
        """Return local Claude event fixtures while preserving C's success evidence."""

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            """Initialize the test double's fixture-controlled state."""
            pass

        def run_stage_events(
            self, *, arm: str, writable: bool = False, **_kwargs: Any
        ) -> tuple[list[dict[str, Any]], float, None]:
            """Return synthetic native events and elapsed time without starting Claude."""
            writable_modes.append(writable)
            events: list[dict[str, Any]] = []
            if arm == "C_strict":
                events.extend(
                    [
                        {
                            "type": "assistant",
                            "message": {
                                "content": [
                                    {
                                        "type": "tool_use",
                                        "id": "codemap-skill",
                                        "name": "Skill",
                                        "input": {
                                            "skill": "codemap-py:query-code",
                                            "args": " ".join(expected_query),
                                        },
                                    },
                                    {
                                        "type": "tool_use",
                                        "id": "codemap-call",
                                        "name": "Bash",
                                        "input": {"command": f"codemap-py query {' '.join(expected_query)}"},
                                    },
                                ]
                            },
                        },
                        {
                            "type": "user",
                            "message": {
                                "content": [
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": "codemap-skill",
                                        "content": "skill launched",
                                        "is_error": False,
                                    },
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": "codemap-call",
                                        "content": "query completed",
                                        "is_error": False,
                                    },
                                ]
                            },
                        },
                    ]
                )
            events.append({"type": "result", "subtype": "success", "usage": {"input_tokens": 12, "output_tokens": 3}})
            return events, 0.25, None

    monkeypatch.setattr(script_run_agentic, "ModelRunner", FakeRunner)
    monkeypatch.setattr(script_run_agentic, "create_executable_agent_workspace", _create_workspace)
    monkeypatch.setattr(script_run_agentic, executor_name, _execute)
    monkeypatch.setattr(script_run_agentic, "_source_pair_unchanged", lambda *_args: True)

    run_dir = tmp_path / "run"
    script_run_agentic.run_claude_paid_stage(
        study=study,
        tasks=[task],
        repo_path=repo_path,
        index_path=index_path,
        manifest_path=script_run_agentic.PARITY_MANIFEST_PATH,
        tasks_path=getattr(script_run_agentic, tasks_path_name),
        model="haiku",
        run_dir=run_dir,
        scope={"scope_sha256": "scope"},
    )

    rows = [json.loads(line) for line in (run_dir / "telemetry.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [row["arm"] for row in rows] == ["A_plain", "B_auto", "C_strict"]
    assert all(row["success"] and row["pooling_eligible"] for row in rows)
    assert all(row["execution"]["patch_applied"] and row["execution"]["targeted_test_passed"] for row in rows)
    assert rows[-1]["strict_query_conformance"] is True
    assert rows[-1]["codemap_successful_calls"] == 1
    assert len(created) == len(cleaned) == len(captured_diffs) == len(executed) == 3
    assert writable_modes == [True, True, True]
    assert all(contract is task["contract"] for _source, contract, _diff in executed)
    assert [diff for _source, _contract, diff in executed] == captured_diffs
    assert [row["captured_diff"] for row in rows] == captured_diffs
    assert [row["captured_diff_sha256"] for row in rows] == [
        hashlib.sha256(diff.encode("utf-8")).hexdigest() for diff in captured_diffs
    ]
    paid_lifecycle.verify_checksums(run_dir)


@pytest.mark.parametrize("arm", ["A_plain", "B_auto", "C_strict"])
def test_claude_patch_strict_prompt_requires_the_observed_exact_cli_query(script_run_agentic: Any, arm: str) -> None:
    """Only Patch C_strict directs the required completed Codemap CLI query."""
    query = "symbol DistributedSamplerWrapper"
    item = {
        "task": {"prompt": "Implement the minimal fix."},
        "contract": SimpleNamespace(task_id="PT-02"),
    }

    prompt = script_run_agentic._claude_fix_prompt("patch", arm, item)

    if arm == "C_strict":
        assert f"`/codemap-py:query-code {query}`" in prompt
        assert f"`codemap-py query {query}`" in prompt
        assert "loading the Skill alone does not satisfy the treatment" in prompt
    else:
        assert f"`codemap-py query {query}`" not in prompt
