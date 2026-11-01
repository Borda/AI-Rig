"""Exercise scoped challenge inventory and interaction admission with real Git repositories.

The tests run the shipped ``chunk_diff.py`` coordinator as a subprocess against throwaway Git histories to prove that a
scoped plan indexes every changed file, rejects invalid interaction or audit assignments, and never certifies a review
without a pinned comparison profile.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

CHUNK_DIFF = Path(__file__).resolve().parents[3] / "skills" / "challenge-resolve" / "chunk_diff.py"
BOTH_CHUNKS_AUDIT = {
    "interactions": [],
    "audit_assignments": [{"id": "audit-001", "chunk_ids": ["chunk-001", "chunk-002"]}],
}


class ScopedPlan(NamedTuple):
    """A planned chunk run: the repository, the chunk output directory and the test's scratch directory."""

    repository: Path
    output: Path
    temp: Path


def _command(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the real coordinator without invoking an external reviewer."""
    return subprocess.run([sys.executable, str(CHUNK_DIFF), *args], capture_output=True, text=True, check=False)


def _git(repository: Path, *args: str) -> None:
    """Create a small real source history for the coordinator."""
    subprocess.run(["git", "-C", str(repository), *args], capture_output=True, check=True)


def _committed_repository(tmp_path: Path) -> Path:
    """Create a repository whose base commit holds ``producer.py`` and its consumer ``consumer.py``."""
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "producer.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repository / "consumer.py").write_text("from producer import VALUE\n", encoding="utf-8")
    _git(repository, "init", "-q")
    _git(repository, "add", ".")
    _git(repository, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
    return repository


def _plan(repository: Path, output: Path, *, budget: str, specification: str, done_when: str) -> None:
    """Plan chunks for ``repository`` and require success."""
    planned = _command(
        "plan",
        "--repository",
        str(repository),
        "--out",
        str(output),
        "--budget-bytes",
        budget,
        "--goal",
        "Review the change",
        "--specification",
        specification,
        "--done-when",
        done_when,
    )
    assert planned.returncode == 0, planned.stderr


def _scoped_plan(plan: ScopedPlan, assignments: object) -> subprocess.CompletedProcess[str]:
    """Write ``assignments`` and run ``scoped-plan`` into ``scoped-chunks.json``."""
    path = plan.temp / "assignments.json"
    path.write_text(json.dumps(assignments), encoding="utf-8")
    return _command(
        "scoped-plan",
        "--manifest",
        str(plan.output / "chunks.json"),
        "--assignments",
        str(path),
        "--out",
        str(plan.output / "scoped-chunks.json"),
    )


@pytest.fixture
def scoped_plan(tmp_path: Path) -> ScopedPlan:
    """Plan tracked changes, a new untracked file and an unchanged consumer into two single-byte-budget chunks."""
    repository = _committed_repository(tmp_path)
    (repository / "producer.py").write_text("VALUE = 2\n", encoding="utf-8")
    (repository / "new.py").write_text("NEW_VALUE = 3\n", encoding="utf-8")
    output = tmp_path / "chunks"
    _plan(
        repository,
        output,
        budget="1",
        specification="Validate the producer and its consumer",
        done_when="The requested change and material interactions are independently reviewed",
    )
    return ScopedPlan(repository, output, tmp_path)


@pytest.fixture
def scoped_manifest(scoped_plan: ScopedPlan) -> Path:
    """Generate the scoped manifest with one audit covering both chunks and return its path."""
    prepared = _scoped_plan(scoped_plan, BOTH_CHUNKS_AUDIT)
    assert prepared.returncode == 0, prepared.stderr
    return scoped_plan.output / "scoped-chunks.json"


class TestScopedPlan:
    """Generating a scoped manifest from a chunk plan."""

    def test_indexes_every_changed_path_including_untracked(self, scoped_plan: ScopedPlan) -> None:
        """A scoped plan lists each changed path, untracked ones included, as a hashed changed unit.

        The inventory is the audit's coverage target; a tracked-only index would let a new file escape review.
        """
        prepared = _scoped_plan(scoped_plan, BOTH_CHUNKS_AUDIT)

        assert prepared.returncode == 0, prepared.stderr
        manifest = json.loads((scoped_plan.output / "scoped-chunks.json").read_bytes())
        assert manifest["schema_version"] == 2
        assert [item["path"] for item in manifest["changed_units"]] == ["new.py", "producer.py"]
        assert all(len(item["id"]) == 64 for item in manifest["changed_units"])

    @pytest.mark.parametrize(
        "assignments",
        [
            pytest.param({"interactions": [], "audit_assignments": []}, id="missing-audit-assignment"),
            pytest.param(
                {"interactions": [], "audit_assignments": [{"id": "audit-001", "chunk_ids": ["chunk-001"]}]},
                id="unassigned-change-unit",
            ),
            pytest.param(
                {
                    "interactions": [],
                    "audit_assignments": [
                        {"id": "audit-001", "chunk_ids": ["chunk-001", "chunk-002"]},
                        {"id": "audit-002", "chunk_ids": ["chunk-002"]},
                    ],
                },
                id="duplicate-audit-assignment",
            ),
            pytest.param(
                {
                    "interactions": [
                        {
                            "chunk_id": "chunk-002",
                            "requirement": {
                                "id": "producer-to-unknown",
                                "source_paths": ["missing.py", "producer.py"],
                                "claim": "Review the alleged consumer.",
                            },
                        }
                    ],
                    "audit_assignments": BOTH_CHUNKS_AUDIT["audit_assignments"],
                },
                id="missing-endpoint",
            ),
            pytest.param(
                {
                    "interactions": [
                        {
                            "chunk_id": "chunk-002",
                            "requirement": {
                                "id": "interaction-inventory-audit",
                                "source_paths": ["producer.py"],
                                "claim": "Impersonate the independent audit.",
                            },
                        }
                    ],
                    "audit_assignments": BOTH_CHUNKS_AUDIT["audit_assignments"],
                },
                id="reserved-audit-id",
            ),
            pytest.param(
                {
                    "interactions": [
                        {
                            "chunk_id": "chunk-001",
                            "requirement": {
                                "id": "unrelated-owner",
                                "source_paths": ["consumer.py", "producer.py"],
                                "claim": "Inspect a producer outside this chunk.",
                            },
                        }
                    ],
                    "audit_assignments": BOTH_CHUNKS_AUDIT["audit_assignments"],
                },
                id="interaction-owner-does-not-cover-endpoint",
            ),
        ],
    )
    def test_rejects_invalid_assignments(self, scoped_plan: ScopedPlan, assignments: dict[str, object]) -> None:
        """Unknown endpoints and absent or impersonated audit coverage cannot be planned.

        Each case breaks one rule: no audit, an unassigned unit, a doubly assigned unit, an endpoint that is not a
        changed or known file, the reserved audit id, or an interaction whose owner chunk does not cover its endpoint.
        """
        result = _scoped_plan(scoped_plan, assignments)

        assert result.returncode != 0
        assert not (scoped_plan.output / "scoped-chunks.json").exists()

    def test_rejects_changed_unchanged_endpoint(self, tmp_path: Path) -> None:
        """An assigned supporting consumer stays bound even when Git hides its later edit.

        ``consumer.py`` is marked assume-unchanged and edited after planning, so Git reports nothing; the coordinator
        must still notice that the supporting source it bound at planning time has changed.
        """
        repository = _committed_repository(tmp_path)
        _git(repository, "update-index", "--assume-unchanged", "consumer.py")
        (repository / "producer.py").write_text("VALUE = 2\n", encoding="utf-8")
        output = tmp_path / "chunks"
        _plan(
            repository,
            output,
            budget="100000",
            specification="Review the consumer",
            done_when="Producer and consumer are independently reviewed",
        )
        interaction = {
            "chunk_id": "chunk-001",
            "requirement": {
                "id": "producer-consumer",
                "source_paths": ["consumer.py", "producer.py"],
                "claim": "Check the producer's consumer.",
            },
        }
        plan = ScopedPlan(repository, output, tmp_path)
        prepared = _scoped_plan(
            plan,
            {"interactions": [interaction], "audit_assignments": [{"id": "audit-001", "chunk_ids": ["chunk-001"]}]},
        )
        assert prepared.returncode == 0, prepared.stderr
        (repository / "consumer.py").write_text("VALUE = 999\n", encoding="utf-8")

        checked = _command("check", "--manifest", str(output / "scoped-chunks.json"))

        assert checked.returncode != 0
        assert "scoped-supporting-source-changed" in checked.stderr


class TestScopedCheck:
    """Final checking of a generated scoped manifest."""

    def test_unbound_plan_is_unverified(self, scoped_manifest: Path) -> None:
        """A freshly generated plan without a pinned comparison profile is reported as unverified."""
        checked = _command("check", "--manifest", str(scoped_manifest))

        assert checked.returncode != 0
        assert "scoped-comparison-profile-unverified" in checked.stderr

    def test_edited_untracked_file_fails_unbound_plan(self, scoped_plan: ScopedPlan, scoped_manifest: Path) -> None:
        """Editing an untracked changed file after planning cannot make an unbound plan pass."""
        (scoped_plan.repository / "new.py").write_text("NEW_VALUE = 4\n", encoding="utf-8")

        checked = _command("check", "--manifest", str(scoped_manifest))

        assert checked.returncode != 0

    def test_clean_gate_fails_without_pinned_comparison_profile(
        self, scoped_plan: ScopedPlan, scoped_manifest: Path
    ) -> None:
        """Ambient Git comparison settings must not certify a scoped review as complete.

        Even with an empty-but-well-formed results file, the clean gate demands the pinned comparison profile.
        """
        results = scoped_plan.temp / "results.json"
        results.write_text(json.dumps({"schema_version": 2, "chunks": [], "audits": []}))

        checked = _command("check", "--manifest", str(scoped_manifest), "--results", str(results))

        assert checked.returncode != 0
        assert "scoped-comparison-profile-unverified" in checked.stderr
