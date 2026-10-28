"""Verify prior-only source coverage against both pinned review checkouts."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_loop_review_evidence import _loop_evidence_run

PLUGIN = Path(__file__).resolve().parents[2]
CHUNKER = PLUGIN / "skills" / "challenge-resolve" / "chunk_diff.py"
COLLECTOR = PLUGIN / "shared" / "collect_diff.py"


def _git(repository: Path, *arguments: str) -> str:
    """Run a real Git operation in the isolated fixture repository."""
    return subprocess.run(
        ["git", "-C", str(repository), *arguments], capture_output=True, text=True, check=True
    ).stdout.strip()


def _command(script: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the public helper without replacing its source or evidence checks."""
    return subprocess.run([sys.executable, str(script), *arguments], capture_output=True, text=True, check=False)


def _plan(tmp_path: Path, *, missing: bool = False) -> tuple[Path, Path, Path]:
    """Prepare pinned original and mirror checkouts with one prior-only source path."""
    original = tmp_path / "repository"
    original.mkdir()
    _git(original, "init", "-q")
    _git(original, "config", "user.name", "Review fixture")
    _git(original, "config", "user.email", "review@example.invalid")
    (original / "changed.py").write_text("old\n", encoding="utf-8")
    (original / "caller.py").write_text("caller\n", encoding="utf-8")
    _git(original, "add", ".")
    _git(original, "commit", "-qm", "base")
    base = _git(original, "rev-parse", "HEAD")
    (original / "changed.py").write_text("committed\n", encoding="utf-8")
    _git(original, "add", "changed.py")
    _git(original, "commit", "-qm", "change")
    (original / "changed.py").write_text("working\n", encoding="utf-8")
    receipt = tmp_path / "local-source"
    collected = _command(COLLECTOR, "--review-worktree", "--repository", str(original), "--out", str(receipt))
    assert collected.returncode == 0, collected.stderr
    mirror = Path(json.loads((receipt / "review-worktree.json").read_bytes())["review_worktree"])

    prior_root = tmp_path / "prior"
    prior_root.mkdir()
    prior_evidence = _loop_evidence_run(
        prior_root,
        findings=[
            {
                "signature": "caller-origin",
                "tier": "high",
                "structural": False,
                "disposition": "open",
                "evidence": ["Caller behavior needs review."],
            }
        ],
    )
    prior = prior_evidence["run"]
    prior_evidence["validator"].validate_loop_evidence(prior, prior_evidence["codex_home"])
    coverage = tmp_path / "coverage.json"
    coverage.write_text(
        json.dumps(
            [
                {
                    "signature": "caller-origin",
                    "source_paths": ["absent.py" if missing else "caller.py"],
                    "review_kind": "chunk",
                    "chunk_ids": ["chunk-001"],
                }
            ]
        ),
        encoding="utf-8",
    )
    output = tmp_path / "chunks"
    planned = _command(
        CHUNKER,
        "plan",
        "--repository",
        str(mirror),
        "--origin-repository",
        str(original),
        "--diff-base",
        base,
        "--scope-path",
        "changed.py",
        "--out",
        str(output),
        "--goal",
        "Review",
        "--specification",
        "Preserve caller behavior",
        "--done-when",
        "Reviewed",
        "--caller-run",
        str(prior),
        "--prior-ledger",
        str(prior / "loop-ledger.json"),
        "--prior-coverage",
        str(coverage),
    )
    assert planned.returncode == 0, planned.stderr
    assignments = tmp_path / "assignments.json"
    assignments.write_text(
        json.dumps({"interactions": [], "audit_assignments": [{"id": "audit", "chunk_ids": ["chunk-001"]}]}),
        encoding="utf-8",
    )
    scoped = output / "scoped.json"
    generated = _command(
        CHUNKER,
        "scoped-plan",
        "--manifest",
        str(output / "chunks.json"),
        "--assignments",
        str(assignments),
        "--out",
        str(scoped),
    )
    if missing:
        assert generated.returncode != 0
        assert "scoped-prior-supporting-source-missing" in generated.stderr
    else:
        assert generated.returncode == 0, generated.stderr
    return original, mirror, scoped


@pytest.mark.parametrize("checkout", ["original", "mirror"])
def test_prior_only_source_drift_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, checkout: str) -> None:
    """A prior finding's supporting path is bound even without an interaction assignment."""
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-thread")
    original, mirror, scoped = _plan(tmp_path)
    manifest = json.loads(scoped.read_bytes())
    assert [unit["path"] for unit in manifest["supporting_units"]] == ["caller.py"]
    current = _command(CHUNKER, "check", "--manifest", str(scoped))
    assert current.returncode != 0
    assert "scoped-comparison-profile-unverified" in current.stderr
    (original if checkout == "original" else mirror).joinpath("caller.py").write_text("drift\n", encoding="utf-8")
    checked = _command(CHUNKER, "check", "--manifest", str(scoped))
    assert checked.returncode != 0
    assert "supporting-source-changed" in checked.stderr


def test_prior_only_missing_source_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A nonexistent prior-only path cannot be presented as reviewable support."""
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-thread")
    _plan(tmp_path, missing=True)
