"""Verify untracked source remains covered by native direct reviews.

Each test builds a real review run (real git checkout, real direct producer and assembler) and checks that the
untracked-file inventory is honoured: a plan cannot hide an untracked file, an empty inventory stays valid, and a path-
scoped review ignores unrelated untracked files.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import test_review_direct as direct
from test_review_prepare import HELPER, _review_inputs

UNTRACKED = "new.txt"


def _write_compact(path: Path, data: Any) -> None:
    """Write JSON in the compact form the review inputs use."""
    path.write_text(json.dumps(data), encoding="utf-8", newline="\n")


def _write_pretty(path: Path, data: Any) -> None:
    """Write JSON in the sorted, indented form the direct producer emits."""
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def _drop_untracked_from_contexts(run: Path, plan: dict[str, Any], dispatch: dict[str, Any]) -> None:
    """Rewrite each reviewer task, its plan context and its dispatch call so none names the untracked file."""
    for context in plan["contexts"]:
        context["source_paths"].remove(UNTRACKED)
        task_path = run / context["context_path"]
        original_task = task_path.read_text(encoding="utf-8")
        task = original_task.replace(f'["widget.py", "{UNTRACKED}"]', '["widget.py"]')
        assert task != original_task
        task_path.write_text(task, encoding="utf-8", newline="\n")
        digest = hashlib.sha256(task_path.read_bytes()).hexdigest()
        context["context_sha256"] = digest
        call = next(item for item in dispatch["calls"] if item["role"] == context["role_id"])
        call["arguments"]["message"] = task
        call["arguments"]["task_name"] = f"review_{context['role_id'].replace('-', '_')}_{digest[:12]}_a1"


def _prepare_scoped(scope_run: Path, source_root: str) -> subprocess.CompletedProcess[str]:
    """Prepare the real direct producer against the `src` directory of the selected source root."""
    return subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "prepare",
            "--out",
            str(scope_run),
            "--run-id",
            "direct-test",
            "--parent-thread-id",
            "parent",
            "--source-root",
            source_root,
            "--scope-path",
            "src",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.integration
def test_direct_assembly_rejects_removed_untracked_source_from_coherently_changed_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rewritten plan cannot hide an untracked file still present in the verified worktree.

    The plan is first produced with `new.txt` in scope, then every parent-owned artifact (briefs, routing, plan,
    reviewer tasks, dispatch) is rebound consistently without it. Only the trusted checkout still reveals the omitted
    file, and native assembly must detect that through the stale untracked inventory.
    """
    run = _review_inputs(tmp_path, untracked=True)
    briefs_path = run / "review-briefs.json"
    briefs = json.loads(briefs_path.read_text(encoding="utf-8"))
    widened = {role: {**brief, "source_paths": [*brief["source_paths"], UNTRACKED]} for role, brief in briefs.items()}
    _write_compact(briefs_path, widened)
    prepared = direct._direct_prepare(run)
    assert prepared.returncode == 0, prepared.stderr
    plan_path = run / "direct-plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert (Path(plan["source_root"]) / UNTRACKED).is_file()
    assert any(record["path"] == UNTRACKED for record in plan["source_snapshot"]["files"])

    # Rebind parent-owned artifacts coherently: only the trusted checkout still reveals the omitted file.
    (run / "untracked.txt").unlink()
    _write_compact(briefs_path, briefs)
    routing = subprocess.run(
        [sys.executable, str(HELPER.parent / "review_routing.py"), "--out", str(run)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert routing.returncode == 0, routing.stderr
    plan["briefs_sha256"] = hashlib.sha256(briefs_path.read_bytes()).hexdigest()
    plan["routing_sha256"] = hashlib.sha256((run / "review-routing.json").read_bytes()).hexdigest()
    plan["source_snapshot"]["scope_paths"] = ["widget.py"]
    plan["source_snapshot"]["files"] = [
        record for record in plan["source_snapshot"]["files"] if record["path"] == "widget.py"
    ]
    dispatch_path = run / "dispatch.json"
    dispatch = json.loads(dispatch_path.read_text(encoding="utf-8"))
    _drop_untracked_from_contexts(run, plan, dispatch)
    _write_pretty(plan_path, plan)
    _write_pretty(dispatch_path, dispatch)
    # The existing fixture creates matching synthetic host lifecycle records; no production validator is mocked.
    monkeypatch.setattr(direct, "_review_inputs", lambda *args, **kwargs: run)
    monkeypatch.setattr(direct, "_direct_prepare", lambda *args, **kwargs: subprocess.CompletedProcess([], 0, "", ""))
    evidence_run, home = direct._direct_evidence(tmp_path)

    assembled = direct._assemble_direct(evidence_run, home)

    assert assembled.returncode != 0
    assert "review-direct-source-untracked-stale" in assembled.stderr


@pytest.mark.integration
def test_direct_assembly_allows_absent_empty_untracked_inventory(tmp_path: Path) -> None:
    """An unchanged empty inventory remains valid when the source has no untracked files.

    The default evidence run has no `untracked.txt` at all; assembly must treat the missing inventory as empty rather
    than as a stale or tampered one.
    """
    run, home = direct._direct_evidence(tmp_path)
    assert not (run / "untracked.txt").exists()

    assembled = direct._assemble_direct(run, home)

    assert assembled.returncode == 0, assembled.stderr


@pytest.mark.integration
def test_direct_path_scope_does_not_require_untracked_source_outside_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path-scoped review excludes unrelated untracked files in the verified checkout.

    The checkout holds an untracked file, but the review is scoped to `src`, so the inventory is not required to list it
    and assembly of the real scoped plan must succeed.
    """
    run = _review_inputs(tmp_path, source_name="src/widget.py", untracked=True)
    (run / "untracked.txt").unlink()
    receipt = json.loads((run / "local-source/review-worktree.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(direct, "_review_inputs", lambda *args, **kwargs: run)
    monkeypatch.setattr(
        direct, "_direct_prepare", lambda scope_run: _prepare_scoped(scope_run, receipt["review_worktree"])
    )
    evidence_run, home = direct._direct_evidence(tmp_path, source_name="src/widget.py")

    assembled = direct._assemble_direct(evidence_run, home)

    assert assembled.returncode == 0, assembled.stderr
