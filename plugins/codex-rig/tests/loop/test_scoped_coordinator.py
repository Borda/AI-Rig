"""Exercise scoped challenge inventory and interaction admission with real Git repositories."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

CHUNK_DIFF = Path(__file__).resolve().parents[2] / "skills" / "challenge-resolve" / "chunk_diff.py"


def _command(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the real coordinator without invoking an external reviewer."""
    return subprocess.run([sys.executable, str(CHUNK_DIFF), *args], capture_output=True, text=True, check=False)


def _git(repository: Path, *args: str) -> None:
    """Create a small real source history for the coordinator."""
    subprocess.run(["git", "-C", str(repository), *args], capture_output=True, check=True)


@pytest.fixture
def scoped_plan(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Prepare tracked changes, a new untracked file, and an unchanged consumer."""
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "producer.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repository / "consumer.py").write_text("from producer import VALUE\n", encoding="utf-8")
    _git(repository, "init", "-q")
    _git(repository, "add", ".")
    _git(repository, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
    (repository / "producer.py").write_text("VALUE = 2\n", encoding="utf-8")
    (repository / "new.py").write_text("NEW_VALUE = 3\n", encoding="utf-8")
    output = tmp_path / "chunks"
    result = _command(
        "plan",
        "--repository",
        str(repository),
        "--out",
        str(output),
        "--budget-bytes",
        "1",
        "--goal",
        "Review the change",
        "--specification",
        "Validate the producer and its consumer",
        "--done-when",
        "The requested change and material interactions are independently reviewed",
    )
    assert result.returncode == 0, result.stderr
    return repository, output, tmp_path


def test_scoped_inventory_includes_untracked_and_rechecks_source(scoped_plan: tuple[Path, Path, Path]) -> None:
    """A scoped plan indexes every changed path, including untracked bytes, and rejects drift."""
    repository, output, temp = scoped_plan
    assignments = temp / "assignments.json"
    assignments.write_text(
        json.dumps(
            {"interactions": [], "audit_assignments": [{"id": "audit-001", "chunk_ids": ["chunk-001", "chunk-002"]}]}
        ),
        encoding="utf-8",
    )

    prepared = _command(
        "scoped-plan",
        "--manifest",
        str(output / "chunks.json"),
        "--assignments",
        str(assignments),
        "--out",
        str(output / "scoped-chunks.json"),
    )

    assert prepared.returncode == 0, prepared.stderr
    manifest = json.loads((output / "scoped-chunks.json").read_bytes())
    assert manifest["schema_version"] == 2
    assert [item["path"] for item in manifest["changed_units"]] == ["new.py", "producer.py"]
    assert all(len(item["id"]) == 64 for item in manifest["changed_units"])
    unbound = _command("check", "--manifest", str(output / "scoped-chunks.json"))
    assert unbound.returncode != 0
    assert "scoped-comparison-profile-unverified" in unbound.stderr
    (repository / "new.py").write_text("NEW_VALUE = 4\n", encoding="utf-8")
    assert _command("check", "--manifest", str(output / "scoped-chunks.json")).returncode != 0


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
                "audit_assignments": [{"id": "audit-001", "chunk_ids": ["chunk-001", "chunk-002"]}],
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
                "audit_assignments": [{"id": "audit-001", "chunk_ids": ["chunk-001", "chunk-002"]}],
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
                "audit_assignments": [{"id": "audit-001", "chunk_ids": ["chunk-001", "chunk-002"]}],
            },
            id="interaction-owner-does-not-cover-endpoint",
        ),
    ],
)
def test_scoped_plan_rejects_invalid_assignments(
    scoped_plan: tuple[Path, Path, Path], assignments: dict[str, object]
) -> None:
    """Unknown endpoints and absent or impersonated audit coverage cannot be planned."""
    _, output, temp = scoped_plan
    path = temp / "assignments.json"
    path.write_text(json.dumps(assignments), encoding="utf-8")

    result = _command(
        "scoped-plan",
        "--manifest",
        str(output / "chunks.json"),
        "--assignments",
        str(path),
        "--out",
        str(output / "scoped-chunks.json"),
    )

    assert result.returncode != 0
    assert not (output / "scoped-chunks.json").exists()


def test_scoped_clean_gate_fails_without_pinned_comparison_profile(scoped_plan: tuple[Path, Path, Path]) -> None:
    """Ambient Git comparison settings must not certify a scoped review as complete."""
    _, output, temp = scoped_plan
    assignments = temp / "assignments.json"
    assignments.write_text(
        json.dumps(
            {"interactions": [], "audit_assignments": [{"id": "audit-001", "chunk_ids": ["chunk-001", "chunk-002"]}]}
        ),
        encoding="utf-8",
    )
    prepared = _command(
        "scoped-plan",
        "--manifest",
        str(output / "chunks.json"),
        "--assignments",
        str(assignments),
        "--out",
        str(output / "scoped-chunks.json"),
    )
    assert prepared.returncode == 0, prepared.stderr
    results = temp / "results.json"
    results.write_text(json.dumps({"schema_version": 2, "chunks": [], "audits": []}))

    checked = _command("check", "--manifest", str(output / "scoped-chunks.json"), "--results", str(results))

    assert checked.returncode != 0
    assert "scoped-comparison-profile-unverified" in checked.stderr


def test_scoped_plan_rejects_changed_unchanged_endpoint(tmp_path: Path) -> None:
    """An assigned supporting consumer remains bound even when Git hides its later edit."""
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "producer.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repository / "consumer.py").write_text("from producer import VALUE\n", encoding="utf-8")
    _git(repository, "init", "-q")
    _git(repository, "add", ".")
    _git(repository, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
    _git(repository, "update-index", "--assume-unchanged", "consumer.py")
    (repository / "producer.py").write_text("VALUE = 2\n", encoding="utf-8")
    output = tmp_path / "chunks"
    planned = _command(
        "plan",
        "--repository",
        str(repository),
        "--out",
        str(output),
        "--budget-bytes",
        "100000",
        "--goal",
        "Review the change",
        "--specification",
        "Review the consumer",
        "--done-when",
        "Producer and consumer are independently reviewed",
    )
    assert planned.returncode == 0, planned.stderr
    assignments = tmp_path / "assignments.json"
    assignments.write_text(
        json.dumps(
            {
                "interactions": [
                    {
                        "chunk_id": "chunk-001",
                        "requirement": {
                            "id": "producer-consumer",
                            "source_paths": ["consumer.py", "producer.py"],
                            "claim": "Check the producer's consumer.",
                        },
                    }
                ],
                "audit_assignments": [{"id": "audit-001", "chunk_ids": ["chunk-001"]}],
            }
        ),
        encoding="utf-8",
    )
    prepared = _command(
        "scoped-plan",
        "--manifest",
        str(output / "chunks.json"),
        "--assignments",
        str(assignments),
        "--out",
        str(output / "scoped-chunks.json"),
    )
    assert prepared.returncode == 0, prepared.stderr
    (repository / "consumer.py").write_text("VALUE = 999\n", encoding="utf-8")

    checked = _command("check", "--manifest", str(output / "scoped-chunks.json"))

    assert checked.returncode != 0
    assert "scoped-supporting-source-changed" in checked.stderr
