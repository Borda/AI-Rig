"""Verify prior-only source coverage against both pinned review checkouts.

A prior finding can name a supporting source path that no current interaction assignment mentions. The chunk planner
must still bind that path (in the original checkout and its review mirror) and refuse a path that does not exist. The
tests drive the shipped ``chunk_diff.py`` and ``collect_diff.py`` helpers against a real throwaway Git repository.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest
from _git_fixtures import git_text
from test_loop_review_evidence import _loop_evidence_run

PLUGIN = Path(__file__).resolve().parents[3]
CHUNKER = PLUGIN / "skills" / "challenge-resolve" / "chunk_diff.py"
COLLECTOR = PLUGIN / "shared" / "collect_diff.py"


class PlannedReview(NamedTuple):
    """The pinned checkouts and the outcome of generating a scoped plan that cites one prior-only support path."""

    original: Path
    mirror: Path
    scoped: Path
    generated: subprocess.CompletedProcess[str]


def _command(script: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the public helper without replacing its source or evidence checks."""
    return subprocess.run([sys.executable, str(script), *arguments], capture_output=True, text=True, check=False)


def _plan(tmp_path: Path, *, supporting_path: str) -> PlannedReview:
    """Prepare pinned original and mirror checkouts and a prior finding whose only support is ``supporting_path``."""
    original = tmp_path / "repository"
    original.mkdir()
    git_text(original, "init", "-q")
    git_text(original, "config", "user.name", "Review fixture")
    git_text(original, "config", "user.email", "review@example.invalid")
    (original / "changed.py").write_text("old\n", encoding="utf-8")
    (original / "caller.py").write_text("caller\n", encoding="utf-8")
    git_text(original, "add", ".")
    git_text(original, "commit", "-qm", "base")
    base = git_text(original, "rev-parse", "HEAD")
    (original / "changed.py").write_text("committed\n", encoding="utf-8")
    git_text(original, "add", "changed.py")
    git_text(original, "commit", "-qm", "change")
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
                    "source_paths": [supporting_path],
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
    return PlannedReview(original, mirror, scoped, generated)


@pytest.fixture
def parent_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run as a child of a parent Codex thread, which the chunk checker's comparison profile depends on."""
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-thread")


@pytest.mark.usefixtures("parent_thread")
class TestPriorOnlySupportingSource:
    """A prior finding's supporting path is bound and verified even without an interaction assignment."""

    @pytest.mark.parametrize("checkout", ["original", "mirror"])
    def test_source_drift_is_rejected(self, tmp_path: Path, checkout: str) -> None:
        """A prior finding's supporting path is bound even without an interaction assignment.

        The scoped plan lists ``caller.py`` as supporting unit solely because a prior finding cites it. Before any
        change the check reports an unverified comparison profile; once ``caller.py`` is edited in either the original
        or the mirror checkout, the check must additionally report ``supporting-source-changed``.
        """
        planned = _plan(tmp_path, supporting_path="caller.py")
        assert planned.generated.returncode == 0, planned.generated.stderr
        manifest = json.loads(planned.scoped.read_bytes())
        assert [unit["path"] for unit in manifest["supporting_units"]] == ["caller.py"]
        before_drift = _command(CHUNKER, "check", "--manifest", str(planned.scoped))
        assert before_drift.returncode != 0
        assert "scoped-comparison-profile-unverified" in before_drift.stderr
        (getattr(planned, checkout) / "caller.py").write_text("drift\n", encoding="utf-8")

        checked = _command(CHUNKER, "check", "--manifest", str(planned.scoped))

        assert checked.returncode != 0
        assert "supporting-source-changed" in checked.stderr

    def test_missing_source_is_rejected(self, tmp_path: Path) -> None:
        """A nonexistent prior-only path cannot be presented as reviewable support.

        The prior finding cites ``absent.py``, which exists in neither checkout; generating the scoped plan must fail
        with ``scoped-prior-supporting-source-missing`` instead of recording a phantom supporting unit.
        """
        planned = _plan(tmp_path, supporting_path="absent.py")

        assert planned.generated.returncode != 0
        assert "scoped-prior-supporting-source-missing" in planned.generated.stderr
