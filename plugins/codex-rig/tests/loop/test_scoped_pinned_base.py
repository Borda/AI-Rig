"""Verify pinned comparison bases against real original and detached Git checkouts."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[2]
PLANNER = PLUGIN / "skills/challenge-resolve/chunk_diff.py"


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout


@pytest.fixture(params=["false", "true"])
def repository(tmp_path, monkeypatch, request):
    """Keep byte-written LF fixtures stable under either host checkout policy."""
    global_config = tmp_path / "gitconfig"
    global_config.write_text(f"[core]\n\tautocrlf = {request.param}\n", encoding="utf-8", newline="\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "core.autocrlf", "false")
    (root / "committed.txt").write_bytes(b"before\n")
    (root / "mixed.txt").write_bytes(b"before\n")
    (root / "caller.txt").write_bytes(b"unchanged caller\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "base")
    base = git(root, "rev-parse", "HEAD").decode().strip()
    (root / "committed.txt").write_bytes(b"committed\n")
    git(root, "add", "committed.txt")
    git(root, "commit", "-qm", "change")
    (root / "mixed.txt").write_bytes(b"staged\n")
    git(root, "add", "mixed.txt")
    (root / "mixed.txt").write_bytes(b"staged and unstaged\n")
    (root / "untracked.txt").write_bytes(b"untracked\n")
    return root, base


def run(root, out, *extra):
    return subprocess.run(
        [
            sys.executable,
            str(PLANNER),
            "plan",
            "--repository",
            str(root),
            "--out",
            str(out),
            "--goal",
            "review",
            "--specification",
            "complete",
            "--done-when",
            "reviewed",
            *extra,
        ],
        capture_output=True,
        text=True,
    )


def test_pinned_inventory_and_exact_combined_diff(repository, tmp_path):
    root, base = repository
    out = tmp_path / "plan"
    result = run(root, out, "--diff-base", base)
    assert result.returncode == 0, result.stderr
    manifest = json.loads((out / "chunks.json").read_bytes())
    assert manifest["diff_base_oid"] == base
    assert manifest["inventory"] == ["committed.txt", "mixed.txt", "untracked.txt"]
    paths = manifest["chunks"][0]["scope_paths"]
    expected = git(root, "--literal-pathspecs", "diff", base, "--binary", "--no-renames", "--", *paths)
    assert (out / manifest["chunks"][0]["diff_path"]).read_bytes() == expected
    records = json.loads((out / manifest["chunks"][0]["source_path"]).read_bytes())["files"]
    assert {r["path"]: r["content"] for r in records}["mixed.txt"] == "staged and unstaged\n"
    assert any(r["path"] == "untracked.txt" and r["content"] == "untracked\n" for r in records)


def test_wrong_base_rejected(repository, tmp_path):
    root, _ = repository
    result = run(root, tmp_path / "plan", "--diff-base", "0" * 40)
    assert result.returncode != 0
    assert "chunk-diff-base-invalid" in result.stderr


def test_old_plan_uses_head(repository, tmp_path):
    root, _ = repository
    out = tmp_path / "plan"
    result = run(root, out)
    assert result.returncode == 0, result.stderr
    manifest = json.loads((out / "chunks.json").read_bytes())
    assert manifest["schema_version"] == 1
    assert "diff_base_oid" not in manifest
    assert manifest["inventory"] == ["mixed.txt", "untracked.txt"]


def test_draft_cannot_be_promoted(repository, tmp_path):
    root, base = repository
    out = tmp_path / "plan"
    assert run(root, out, "--diff-base", base).returncode == 0
    result = subprocess.run(
        [sys.executable, str(PLANNER), "check", "--manifest", str(out / "chunks.json")], capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "scoped-comparison-profile-unverified" in result.stderr


def test_base_only_deletion_fails_closed(repository, tmp_path):
    root, base = repository
    git(root, "rm", "-q", "committed.txt")
    git(root, "commit", "-qm", "delete")
    result = run(root, tmp_path / "plan", "--diff-base", base)
    assert result.returncode != 0
    assert "chunk-base-only-deletion-unsupported" in result.stderr


def test_original_checkout_and_mirror_drift_are_rejected(repository, tmp_path):
    original, base = repository
    receipt = tmp_path / "local-source"
    collector = PLUGIN / "shared/collect_diff.py"
    subprocess.run(
        [sys.executable, str(collector), "--review-worktree", "--repository", str(original), "--out", str(receipt)],
        check=True,
        capture_output=True,
    )
    mirror = Path(json.loads((receipt / "review-worktree.json").read_bytes())["review_worktree"])
    out = tmp_path / "plan"
    result = run(mirror, out, "--diff-base", base, "--origin-repository", str(original))
    assert result.returncode == 0, result.stderr
    manifest = json.loads((out / "chunks.json").read_bytes())
    assert manifest["origin_repository"] == original.as_posix()
    assert "origin_snapshot_path" in manifest
    (original / "committed.txt").write_bytes(b"original drift\n")
    assignments = tmp_path / "assignments.json"
    assignments.write_text(
        json.dumps(
            {
                "interactions": [],
                "audit_assignments": [{"id": "audit", "chunk_ids": [c["id"] for c in manifest["chunks"]]}],
            }
        )
    )

    def create():
        return subprocess.run(
            [
                sys.executable,
                str(PLANNER),
                "scoped-plan",
                "--manifest",
                str(out / "chunks.json"),
                "--assignments",
                str(assignments),
                "--out",
                str(out / "scoped.json"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    result = create()
    assert result.returncode != 0
    assert "chunk-origin-source-changed" in result.stderr
    (original / "committed.txt").write_bytes(b"committed\n")
    (mirror / "committed.txt").write_bytes(b"mirror drift\n")
    result = create()
    assert result.returncode != 0
    assert "chunk-source-changed" in result.stderr or "chunk-diff-changed" in result.stderr


def test_scoped_check_rejects_origin_index_drift(repository, tmp_path):
    original, base = repository
    receipt = tmp_path / "local-source"
    subprocess.run(
        [
            sys.executable,
            str(PLUGIN / "shared/collect_diff.py"),
            "--review-worktree",
            "--repository",
            str(original),
            "--out",
            str(receipt),
        ],
        check=True,
        capture_output=True,
    )
    mirror = Path(json.loads((receipt / "review-worktree.json").read_bytes())["review_worktree"])
    out = tmp_path / "plan"
    result = run(mirror, out, "--diff-base", base, "--origin-repository", str(original))
    assert result.returncode == 0, result.stderr
    manifest = json.loads((out / "chunks.json").read_bytes())
    assignments = tmp_path / "assignments.json"
    assignments.write_text(
        json.dumps(
            {
                "interactions": [],
                "audit_assignments": [{"id": "audit", "chunk_ids": [c["id"] for c in manifest["chunks"]]}],
            }
        )
    )
    scoped = out / "scoped.json"
    result = subprocess.run(
        [
            sys.executable,
            str(PLANNER),
            "scoped-plan",
            "--manifest",
            str(out / "chunks.json"),
            "--assignments",
            str(assignments),
            "--out",
            str(scoped),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    git(original, "add", "mixed.txt")
    result = subprocess.run(
        [sys.executable, str(PLANNER), "check", "--manifest", str(scoped)], capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "chunk-origin-source-changed" in result.stderr


def test_pinned_scoped_plan_admits_verified_pre_dispatch(repository, tmp_path):
    """Admit an origin-bound profile before requiring real reviewer results at the final gate."""
    original, base = repository
    receipt = tmp_path / "local-source"
    subprocess.run(
        [
            sys.executable,
            str(PLUGIN / "shared/collect_diff.py"),
            "--review-worktree",
            "--repository",
            str(original),
            "--out",
            str(receipt),
        ],
        check=True,
        capture_output=True,
    )
    mirror = Path(json.loads((receipt / "review-worktree.json").read_bytes())["review_worktree"])
    out = tmp_path / "plan"
    assert run(mirror, out, "--diff-base", base, "--origin-repository", str(original)).returncode == 0
    manifest = json.loads((out / "chunks.json").read_bytes())
    assignments = tmp_path / "assignments.json"
    assignments.write_text(
        json.dumps(
            {
                "interactions": [],
                "audit_assignments": [{"id": "audit", "chunk_ids": [c["id"] for c in manifest["chunks"]]}],
            }
        )
    )
    scoped = out / "scoped.json"
    created = subprocess.run(
        [
            sys.executable,
            str(PLANNER),
            "scoped-plan",
            "--manifest",
            str(out / "chunks.json"),
            "--assignments",
            str(assignments),
            "--out",
            str(scoped),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert created.returncode == 0, created.stderr
    checked = subprocess.run(
        [sys.executable, str(PLANNER), "check", "--manifest", str(scoped)], capture_output=True, text=True, check=False
    )
    assert checked.returncode == 0, checked.stderr


def test_base_draft_without_original_cannot_create_scoped_plan(repository, tmp_path):
    root, base = repository
    out = tmp_path / "plan"
    assert run(root, out, "--diff-base", base).returncode == 0
    manifest = json.loads((out / "chunks.json").read_bytes())
    assignments = tmp_path / "assignments.json"
    assignments.write_text(
        json.dumps(
            {
                "interactions": [],
                "audit_assignments": [{"id": "audit", "chunk_ids": [c["id"] for c in manifest["chunks"]]}],
            }
        )
    )
    result = subprocess.run(
        [
            sys.executable,
            str(PLANNER),
            "scoped-plan",
            "--manifest",
            str(out / "chunks.json"),
            "--assignments",
            str(assignments),
            "--out",
            str(out / "scoped.json"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "chunk-origin-binding-required" in result.stderr


def test_child_wrong_base_is_rejected_before_report_validation(tmp_path, monkeypatch):
    import importlib.util

    monkeypatch.syspath_prepend(str(PLUGIN / "shared"))
    spec = importlib.util.spec_from_file_location("tmp_chunk_diff", PLANNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    child = tmp_path / "child"
    child.mkdir()
    (child / "result.json").write_text(
        json.dumps(
            {
                "status": "pass",
                "metadata": {"adversarial_loop": {"reason": "clean", "scores": [0], "rounds": [{"decision": "clean"}]}},
            }
        )
    )
    (child / "loop-evidence.json").write_text(json.dumps({"diff_base_oid": "b" * 40}))
    with pytest.raises(ValueError, match="chunk-result-diff-base-mismatch"):
        module._check_clean_run(
            tmp_path,
            str(tmp_path),
            ["a"],
            b"",
            b"",
            "child/result.json",
            "child",
            {"goal": "g", "specification": "s", "done_when": "d"},
            expected_base="a" * 40,
        )


def test_unchanged_supporting_source_is_bound_to_original_and_mirror(repository, tmp_path):
    original, base = repository
    receipt = tmp_path / "local-source"
    subprocess.run(
        [
            sys.executable,
            str(PLUGIN / "shared/collect_diff.py"),
            "--review-worktree",
            "--repository",
            str(original),
            "--out",
            str(receipt),
        ],
        check=True,
        capture_output=True,
    )
    mirror = Path(json.loads((receipt / "review-worktree.json").read_bytes())["review_worktree"])
    out = tmp_path / "plan"
    result = run(
        mirror, out, "--diff-base", base, "--origin-repository", str(original), "--scope-path", "committed.txt"
    )
    assert result.returncode == 0, result.stderr
    manifest = json.loads((out / "chunks.json").read_bytes())
    chunk = manifest["chunks"][0]["id"]
    source_path = manifest["chunks"][0]["scope_paths"][0]
    assignments = tmp_path / "assignments.json"
    assignments.write_text(
        json.dumps(
            {
                "interactions": [
                    {
                        "chunk_id": chunk,
                        "requirement": {
                            "id": "caller",
                            "source_paths": sorted([source_path, "caller.txt"]),
                            "claim": "caller interaction",
                        },
                    }
                ],
                "audit_assignments": [{"id": "audit", "chunk_ids": [c["id"] for c in manifest["chunks"]]}],
            }
        )
    )
    scoped = out / "scoped.json"
    result = subprocess.run(
        [
            sys.executable,
            str(PLANNER),
            "scoped-plan",
            "--manifest",
            str(out / "chunks.json"),
            "--assignments",
            str(assignments),
            "--out",
            str(scoped),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    (original / "caller.txt").write_bytes(b"drifted caller\n")
    result = subprocess.run(
        [sys.executable, str(PLANNER), "check", "--manifest", str(scoped)], capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "chunk-origin-supporting-source-changed" in result.stderr
