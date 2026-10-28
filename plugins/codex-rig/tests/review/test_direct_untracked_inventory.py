"""Verify untracked source remains covered by native direct reviews."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import test_review_direct as direct
from test_review_prepare import HELPER, _review_inputs


@pytest.mark.integration
def test_direct_assembly_rejects_removed_untracked_source_from_coherently_changed_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rewritten plan cannot hide an untracked file still present in the verified worktree."""
    run = _review_inputs(tmp_path, untracked=True)
    briefs_path = run / "review-briefs.json"
    briefs = json.loads(briefs_path.read_text(encoding="utf-8"))
    for brief in briefs.values():
        brief["source_paths"].append("new.txt")
    briefs_path.write_text(json.dumps(briefs), encoding="utf-8", newline="\n")

    prepared = direct._direct_prepare(run)
    assert prepared.returncode == 0, prepared.stderr
    plan_path = run / "direct-plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    source_root = Path(plan["source_root"])
    assert (source_root / "new.txt").is_file()
    assert any(record["path"] == "new.txt" for record in plan["source_snapshot"]["files"])

    # Rebind parent-owned artifacts coherently: only the trusted checkout still reveals the omitted file.
    (run / "untracked.txt").unlink()
    for brief in briefs.values():
        brief["source_paths"].remove("new.txt")
    briefs_path.write_text(json.dumps(briefs), encoding="utf-8", newline="\n")
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
    for context in plan["contexts"]:
        context["source_paths"].remove("new.txt")
        task_path = run / context["context_path"]
        original_task = task_path.read_text(encoding="utf-8")
        task = original_task.replace('["widget.py", "new.txt"]', '["widget.py"]')
        assert task != original_task
        task_path.write_text(task, encoding="utf-8", newline="\n")
        digest = hashlib.sha256(task_path.read_bytes()).hexdigest()
        context["context_sha256"] = digest
        call = next(item for item in dispatch["calls"] if item["role"] == context["role_id"])
        call["arguments"]["message"] = task
        call["arguments"]["task_name"] = f"review_{context['role_id'].replace('-', '_')}_{digest[:12]}_a1"
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    dispatch_path.write_text(json.dumps(dispatch, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")

    # The existing fixture creates matching synthetic host lifecycle records; no production validator is mocked.
    monkeypatch.setattr(direct, "_review_inputs", lambda *args, **kwargs: run)
    monkeypatch.setattr(direct, "_direct_prepare", lambda *args, **kwargs: subprocess.CompletedProcess([], 0, "", ""))
    run, home = direct._direct_evidence(tmp_path)
    assembled = direct._assemble_direct(run, home)

    assert assembled.returncode != 0
    assert "review-direct-source-untracked-stale" in assembled.stderr


@pytest.mark.integration
def test_direct_assembly_allows_absent_empty_untracked_inventory(tmp_path: Path) -> None:
    """An unchanged empty inventory remains valid when the source has no untracked files."""
    run, home = direct._direct_evidence(tmp_path)
    assert not (run / "untracked.txt").exists()

    assembled = direct._assemble_direct(run, home)

    assert assembled.returncode == 0, assembled.stderr


@pytest.mark.integration
def test_direct_path_scope_does_not_require_untracked_source_outside_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path-scoped review excludes unrelated untracked files in the verified checkout."""
    run = _review_inputs(tmp_path, source_name="src/widget.py", untracked=True)
    (run / "untracked.txt").unlink()
    receipt = json.loads((run / "local-source/review-worktree.json").read_text(encoding="utf-8"))

    def prepare_scoped(scope_run: Path) -> subprocess.CompletedProcess[str]:
        """Prepare the real direct producer against the selected source directory."""
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
                receipt["review_worktree"],
                "--scope-path",
                "src",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    monkeypatch.setattr(direct, "_review_inputs", lambda *args, **kwargs: run)
    monkeypatch.setattr(direct, "_direct_prepare", prepare_scoped)
    run, home = direct._direct_evidence(tmp_path, source_name="src/widget.py")

    assembled = direct._assemble_direct(run, home)

    assert assembled.returncode == 0, assembled.stderr
