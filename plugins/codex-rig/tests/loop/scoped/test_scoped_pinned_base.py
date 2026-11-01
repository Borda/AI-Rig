"""Verify pinned comparison bases against real original and detached Git checkouts.

The tests run the shipped chunk planner and diff collector as subprocesses over throwaway repositories that mix
committed, staged, unstaged and untracked changes, under both host ``core.autocrlf`` policies. They prove that a pinned
base fixes the reviewed inventory and that drift in the original checkout, its review mirror or its index is refused.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest
from _git_fixtures import git_bytes

PLUGIN = Path(__file__).resolve().parents[3]
PLANNER = PLUGIN / "skills/challenge-resolve/chunk_diff.py"
COLLECTOR = PLUGIN / "shared/collect_diff.py"


class PinnedPlan(NamedTuple):
    """An origin-bound plan: the original checkout, its pinned base, the review mirror and the plan directory."""

    original: Path
    base: str
    mirror: Path
    out: Path


def _planner(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the shipped chunk planner CLI."""
    return subprocess.run([sys.executable, str(PLANNER), *args], capture_output=True, text=True, check=False)


def _plan(root: Path, out: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    """Plan chunks for ``root`` with a fixed goal, specification and done-when."""
    return _planner(
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
    )


def _scoped_plan(manifest: Path, assignments: Path, out: Path) -> subprocess.CompletedProcess[str]:
    """Generate a scoped manifest from a chunk manifest and an assignments file."""
    return _planner("scoped-plan", "--manifest", str(manifest), "--assignments", str(assignments), "--out", str(out))


def _check(manifest: Path) -> subprocess.CompletedProcess[str]:
    """Check a scoped manifest without results."""
    return _planner("check", "--manifest", str(manifest))


def _mirror(original: Path, receipt: Path) -> Path:
    """Collect a review worktree mirror of ``original`` and return its path."""
    subprocess.run(
        [sys.executable, str(COLLECTOR), "--review-worktree", "--repository", str(original), "--out", str(receipt)],
        check=True,
        capture_output=True,
    )
    return Path(json.loads((receipt / "review-worktree.json").read_bytes())["review_worktree"])


def _write_assignments(path: Path, manifest: dict, interactions: list[dict] | None = None) -> Path:
    """Write an assignments file with one audit covering every chunk of ``manifest``."""
    audit = {"id": "audit", "chunk_ids": [chunk["id"] for chunk in manifest["chunks"]]}
    path.write_text(json.dumps({"interactions": interactions or [], "audit_assignments": [audit]}))
    return path


@pytest.fixture(params=[pytest.param("false", id="autocrlf-false"), pytest.param("true", id="autocrlf-true")])
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> tuple[Path, str]:
    """Create a repository with committed, staged, unstaged and untracked changes beyond a returned base commit.

    The global ``core.autocrlf`` policy is varied so byte-written LF fixtures must stay stable under either host
    checkout policy.
    """
    global_config = tmp_path / "gitconfig"
    global_config.write_text(f"[core]\n\tautocrlf = {request.param}\n", encoding="utf-8", newline="\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    root = tmp_path / "source"
    root.mkdir()
    git_bytes(root, "init", "-q")
    git_bytes(root, "config", "user.email", "fixture@example.invalid")
    git_bytes(root, "config", "user.name", "Fixture")
    git_bytes(root, "config", "core.autocrlf", "false")
    (root / "committed.txt").write_bytes(b"before\n")
    (root / "mixed.txt").write_bytes(b"before\n")
    (root / "caller.txt").write_bytes(b"unchanged caller\n")
    git_bytes(root, "add", ".")
    git_bytes(root, "commit", "-qm", "base")
    base = git_bytes(root, "rev-parse", "HEAD").decode().strip()
    (root / "committed.txt").write_bytes(b"committed\n")
    git_bytes(root, "add", "committed.txt")
    git_bytes(root, "commit", "-qm", "change")
    (root / "mixed.txt").write_bytes(b"staged\n")
    git_bytes(root, "add", "mixed.txt")
    (root / "mixed.txt").write_bytes(b"staged and unstaged\n")
    (root / "untracked.txt").write_bytes(b"untracked\n")
    return root, base


@pytest.fixture
def pinned_plan(repository: tuple[Path, str], tmp_path: Path) -> PinnedPlan:
    """Plan the pinned base against a review mirror bound to the original checkout."""
    original, base = repository
    mirror = _mirror(original, tmp_path / "local-source")
    out = tmp_path / "plan"
    planned = _plan(mirror, out, "--diff-base", base, "--origin-repository", str(original))
    assert planned.returncode == 0, planned.stderr
    return PinnedPlan(original, base, mirror, out)


@pytest.fixture
def scoped_manifest(pinned_plan: PinnedPlan, tmp_path: Path) -> Path:
    """Generate the scoped manifest for the pinned plan with one audit over every chunk."""
    manifest = json.loads((pinned_plan.out / "chunks.json").read_bytes())
    assignments = _write_assignments(tmp_path / "assignments.json", manifest)
    scoped = pinned_plan.out / "scoped.json"
    created = _scoped_plan(pinned_plan.out / "chunks.json", assignments, scoped)
    assert created.returncode == 0, created.stderr
    return scoped


class TestPinnedPlan:
    """Planning against a pinned diff base."""

    def test_inventory_and_combined_diff_are_exact(self, repository: tuple[Path, str], tmp_path: Path) -> None:
        """A pinned plan inventories committed, staged and untracked paths and diffs them against the base.

        The chunk diff must equal Git's combined binary diff from the base, and the source snapshot must hold the
        working bytes (staged plus unstaged, untracked) rather than any intermediate state.
        """
        root, base = repository
        out = tmp_path / "plan"

        result = _plan(root, out, "--diff-base", base)

        assert result.returncode == 0, result.stderr
        manifest = json.loads((out / "chunks.json").read_bytes())
        assert manifest["diff_base_oid"] == base
        assert manifest["inventory"] == ["committed.txt", "mixed.txt", "untracked.txt"]
        chunk = manifest["chunks"][0]
        expected = git_bytes(
            root, "--literal-pathspecs", "diff", base, "--binary", "--no-renames", "--", *chunk["scope_paths"]
        )
        assert (out / chunk["diff_path"]).read_bytes() == expected
        records = json.loads((out / chunk["source_path"]).read_bytes())["files"]
        assert {r["path"]: r["content"] for r in records}["mixed.txt"] == "staged and unstaged\n"
        assert any(r["path"] == "untracked.txt" and r["content"] == "untracked\n" for r in records)

    def test_unknown_base_is_rejected(self, repository: tuple[Path, str], tmp_path: Path) -> None:
        """A diff base that is not an existing commit fails with the invalid-base error."""
        root, _ = repository

        result = _plan(root, tmp_path / "plan", "--diff-base", "0" * 40)

        assert result.returncode != 0
        assert "chunk-diff-base-invalid" in result.stderr

    def test_without_base_uses_head(self, repository: tuple[Path, str], tmp_path: Path) -> None:
        """A plan without a pinned base keeps the legacy schema and compares against ``HEAD``.

        The committed change is therefore absent from the inventory; only uncommitted paths remain.
        """
        root, _ = repository
        out = tmp_path / "plan"

        result = _plan(root, out)

        assert result.returncode == 0, result.stderr
        manifest = json.loads((out / "chunks.json").read_bytes())
        assert manifest["schema_version"] == 1
        assert "diff_base_oid" not in manifest
        assert manifest["inventory"] == ["mixed.txt", "untracked.txt"]

    def test_base_only_deletion_fails_closed(self, repository: tuple[Path, str], tmp_path: Path) -> None:
        """A file deleted since the base, with no working-tree trace, is refused instead of silently dropped."""
        root, base = repository
        git_bytes(root, "rm", "-q", "committed.txt")
        git_bytes(root, "commit", "-qm", "delete")

        result = _plan(root, tmp_path / "plan", "--diff-base", base)

        assert result.returncode != 0
        assert "chunk-base-only-deletion-unsupported" in result.stderr

    def test_binds_origin_repository(self, pinned_plan: PinnedPlan) -> None:
        """A plan over the mirror records its original checkout and a snapshot of it."""
        manifest = json.loads((pinned_plan.out / "chunks.json").read_bytes())

        assert manifest["origin_repository"] == pinned_plan.original.as_posix()
        assert "origin_snapshot_path" in manifest


class TestScopedPlanAdmission:
    """Admission of a scoped plan built on a pinned base."""

    def test_draft_cannot_be_promoted(self, repository: tuple[Path, str], tmp_path: Path) -> None:
        """A pinned draft plan is never accepted by the final check without its scoped profile."""
        root, base = repository
        out = tmp_path / "plan"
        assert _plan(root, out, "--diff-base", base).returncode == 0

        result = _check(out / "chunks.json")

        assert result.returncode != 0
        assert "scoped-comparison-profile-unverified" in result.stderr

    def test_original_drift_is_rejected(self, pinned_plan: PinnedPlan, tmp_path: Path) -> None:
        """Editing the original checkout after planning blocks scoped-plan creation."""
        (pinned_plan.original / "committed.txt").write_bytes(b"original drift\n")
        manifest = json.loads((pinned_plan.out / "chunks.json").read_bytes())
        assignments = _write_assignments(tmp_path / "assignments.json", manifest)

        result = _scoped_plan(pinned_plan.out / "chunks.json", assignments, pinned_plan.out / "scoped.json")

        assert result.returncode != 0
        assert "chunk-origin-source-changed" in result.stderr

    def test_mirror_drift_is_rejected(self, pinned_plan: PinnedPlan, tmp_path: Path) -> None:
        """Editing the review mirror after planning blocks scoped-plan creation."""
        (pinned_plan.mirror / "committed.txt").write_bytes(b"mirror drift\n")
        manifest = json.loads((pinned_plan.out / "chunks.json").read_bytes())
        assignments = _write_assignments(tmp_path / "assignments.json", manifest)

        result = _scoped_plan(pinned_plan.out / "chunks.json", assignments, pinned_plan.out / "scoped.json")

        assert result.returncode != 0
        assert "chunk-source-changed" in result.stderr or "chunk-diff-changed" in result.stderr

    def test_origin_index_drift_is_rejected_by_check(self, pinned_plan: PinnedPlan, scoped_manifest: Path) -> None:
        """Staging a change in the original checkout after the scoped plan makes the final check fail."""
        git_bytes(pinned_plan.original, "add", "mixed.txt")

        result = _check(scoped_manifest)

        assert result.returncode != 0
        assert "chunk-origin-source-changed" in result.stderr

    def test_verified_plan_is_admitted_before_dispatch(self, scoped_manifest: Path) -> None:
        """An origin-bound scoped plan passes the pre-dispatch check, before real reviewer results exist."""
        result = _check(scoped_manifest)

        assert result.returncode == 0, result.stderr

    def test_base_draft_without_original_cannot_create_scoped_plan(
        self, repository: tuple[Path, str], tmp_path: Path
    ) -> None:
        """A pinned plan made directly on the checkout, without an origin binding, cannot become a scoped plan."""
        root, base = repository
        out = tmp_path / "plan"
        assert _plan(root, out, "--diff-base", base).returncode == 0
        manifest = json.loads((out / "chunks.json").read_bytes())
        assignments = _write_assignments(tmp_path / "assignments.json", manifest)

        result = _scoped_plan(out / "chunks.json", assignments, out / "scoped.json")

        assert result.returncode != 0
        assert "chunk-origin-binding-required" in result.stderr

    def test_unchanged_supporting_source_is_bound_to_original_and_mirror(
        self, repository: tuple[Path, str], tmp_path: Path
    ) -> None:
        """An unchanged caller assigned as supporting source is re-checked against the original checkout.

        ``caller.txt`` is not part of the diff, but the interaction cites it; editing it in the original after the
        scoped plan must fail the final check with the supporting-source error.
        """
        original, base = repository
        mirror = _mirror(original, tmp_path / "local-source")
        out = tmp_path / "plan"
        planned = _plan(
            mirror, out, "--diff-base", base, "--origin-repository", str(original), "--scope-path", "committed.txt"
        )
        assert planned.returncode == 0, planned.stderr
        manifest = json.loads((out / "chunks.json").read_bytes())
        chunk = manifest["chunks"][0]
        interaction = {
            "chunk_id": chunk["id"],
            "requirement": {
                "id": "caller",
                "source_paths": sorted([chunk["scope_paths"][0], "caller.txt"]),
                "claim": "caller interaction",
            },
        }
        assignments = _write_assignments(tmp_path / "assignments.json", manifest, [interaction])
        scoped = out / "scoped.json"
        created = _scoped_plan(out / "chunks.json", assignments, scoped)
        assert created.returncode == 0, created.stderr
        (original / "caller.txt").write_bytes(b"drifted caller\n")

        result = _check(scoped)

        assert result.returncode != 0
        assert "chunk-origin-supporting-source-changed" in result.stderr


def test_child_wrong_base_is_rejected_before_report_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean child run bound to a different diff base fails before its report is validated.

    The child result looks clean, but its loop evidence names another base commit than the one the plan pinned; the
    coordinator must reject it with the base-mismatch error instead of trusting the clean verdict.
    """
    monkeypatch.syspath_prepend(str(PLUGIN / "shared"))
    spec = importlib.util.spec_from_file_location("tmp_chunk_diff", PLANNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    child = tmp_path / "child"
    child.mkdir()
    clean = {"reason": "clean", "scores": [0], "rounds": [{"decision": "clean"}]}
    (child / "result.json").write_text(json.dumps({"status": "pass", "metadata": {"adversarial_loop": clean}}))
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
