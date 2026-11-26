"""Exercise advisory review-anchor drift against local Git snapshots."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "bin/resolve_item_drift.py"
_REQUIRES_GIT = pytest.mark.skipif(shutil.which("git") is None, reason="compares local Git commit snapshots")
_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Reviewer",
    "GIT_AUTHOR_EMAIL": "reviewer@example.com",
    "GIT_COMMITTER_NAME": "Maintainer",
    "GIT_COMMITTER_EMAIL": "maintainer@example.com",
}


def _git(repo: Path, *args: str) -> str:
    """Run a fixture command inside the temporary repository."""
    return subprocess.run(
        ["git", *args], cwd=repo, env=_GIT_ENV, capture_output=True, text=True, check=True, timeout=10
    ).stdout.strip()


@pytest.fixture
def snapshots(tmp_path: Path) -> tuple[Path, str, str]:
    """Make two snapshots and leave a conflicting uncommitted edit outside them."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "package").mkdir()
    source = repo / "package" / "source.py"
    source.write_text("keep\nrewrite\nkeep at end\n", encoding="utf-8", newline="\n")
    (repo / "shifted.py").write_text("anchor\nkeep\n", encoding="utf-8", newline="\n")
    (repo / "gone.py").write_text("first\nsecond\n", encoding="utf-8", newline="\n")
    (repo / "stable.py").write_text("same\n", encoding="utf-8", newline="\n")
    (repo / "data.bin").write_bytes(b"a\x00b")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "review snapshot")
    review = _git(repo, "rev-parse", "HEAD")
    source.write_text("keep\nreplacement\nkeep at end\n", encoding="utf-8", newline="\n")
    (repo / "shifted.py").write_text("new line\nanchor\nkeep\n", encoding="utf-8", newline="\n")
    (repo / "gone.py").unlink()
    (repo / "data.bin").write_bytes(b"a\x00c")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "PR snapshot")
    head = _git(repo, "rev-parse", "HEAD")
    (repo / "stable.py").write_text("working tree only\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "stable.py")
    return repo, review, head


def _classify(repo: Path, review: str, head: str, items: list[dict]) -> subprocess.CompletedProcess[str]:
    """Call the production CLI with files laid out as a review and resolve run."""
    report = repo.parent / "review-report.md"
    report.touch()
    report.with_name("head-sha.txt").write_text(review + "\n", encoding="utf-8")
    items_file = repo.parent / "action-items.jsonl"
    items_file.write_text("".join(json.dumps(item) + "\n" for item in items), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--report-file",
            str(report),
            "--current-head",
            head,
            "--items",
            str(items_file),
        ],
        cwd=repo / "package",
        env=_GIT_ENV,
        capture_output=True,
        encoding="utf-8",
        check=False,
        timeout=10,
    )


@_REQUIRES_GIT
@pytest.mark.integration
def test_classifies_committed_file_and_line_changes(snapshots: tuple[Path, str, str]) -> None:
    """Distinguish a rewritten anchor, a shifted anchor, and a removed or unchanged file."""
    repo, review, head = snapshots
    items = [
        {"id": 1, "file": "package/source.py", "line": "2", "status": "pending"},
        {"id": 2, "file": "package/source.py", "line": 1, "status": "resolved"},
        {"id": 3, "file": "shifted.py", "line": 1},
        {"id": 4, "file": "gone.py", "line": 1},
        {"id": 5, "file": "stable.py", "line": 1},
        {"id": 6, "file": "data.bin", "line": 1},
        {"id": 7, "file": "package/source.py", "line": 2, "type": "[gh][req]"},
        {"id": 8, "file": "package/source.py", "line": 99},
        {"id": 9, "file": "missing.py", "line": 1},
        {"id": 10, "file": "", "line": ""},
        {"id": 11, "file": "package/*.py", "line": 2},
        {"id": 12, "file": "package", "line": 2},
    ]
    result = _classify(repo, review[:12], head, items)
    assert result.returncode == 0, result.stderr
    assert [(row["id"], row["path"], row["marker"], row["diff_stat"]) for row in json.loads(result.stdout)] == [
        (1, "package/source.py", "already-fixed-candidate", {"added": 1, "deleted": 1}),
        (2, "package/source.py", "file-changed-since-review", {"added": 1, "deleted": 1}),
        (3, "shifted.py", "file-changed-since-review", {"added": 1, "deleted": 0}),
        (4, "gone.py", "file-removed", {"added": 0, "deleted": 2}),
        (5, "stable.py", "current", {"added": 0, "deleted": 0}),
        (6, "data.bin", "file-changed-since-review", {"added": None, "deleted": None}),
        (7, "package/source.py", "file-changed-since-review", {"added": 1, "deleted": 1}),
        (8, "package/source.py", "file-changed-since-review", {"added": 1, "deleted": 1}),
    ]
    assert json.loads(result.stdout)[0]["line"] == 2
    assert (repo.parent / "action-items.jsonl").read_text(encoding="utf-8").splitlines() == [
        json.dumps(item) for item in items
    ]


@_REQUIRES_GIT
@pytest.mark.integration
@pytest.mark.parametrize("review", ["", "--output=patch", "0" * 40])
def test_unavailable_review_head_produces_no_markers(snapshots: tuple[Path, str, str], review: str) -> None:
    """Leave stdout empty when the recorded review cannot supply a commit."""
    repo, _, head = snapshots
    result = _classify(repo, review, head, [{"id": 1, "file": "stable.py", "line": 1}])
    assert result.returncode == 1
    assert result.stdout == ""
    assert "review" in result.stderr
