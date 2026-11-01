"""Index self-check on load: a broken index is rejected, never partially served.

``load_index`` runs every decoded index through ``validate_index`` before any command reads it. A truncated write, a
hand-edited file, or an index from an incompatible tool must produce a hard, parseable JSON error advising a rebuild — a
half-valid index served to a query returns silently wrong answers.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def _query_raw(scan_query: Path, root: Path, index_path: Path, *args: str) -> subprocess.CompletedProcess:
    """Run scan-query without asserting success; caller inspects exit code + output."""
    return subprocess.run(
        [sys.executable, str(scan_query), "--index", str(index_path), *args],
        capture_output=True,
        text=True,
        cwd=str(root),
    )


@pytest.fixture(name="pristine_project", scope="module")
def _pristine_project(tmp_path_factory: pytest.TempPathFactory, scan_index: Path) -> Path:
    """Scan a two-module project once; tests copy it before corrupting the index."""
    root = tmp_path_factory.mktemp("pristine") / "proj"
    root.mkdir()
    (root / "leaf.py").write_text("def leaf_fn(x):\n    return x\n")
    (root / "consumer.py").write_text("import leaf\n\ndef use(x):\n    return leaf.leaf_fn(x)\n")
    result = subprocess.run(
        [sys.executable, str(scan_index), "--root", str(root)], capture_output=True, text=True, cwd=str(root)
    )
    assert result.returncode == 0, result.stderr
    return root


@pytest.fixture(name="healthy_project")
def _healthy_project(tmp_path: Path, pristine_project: Path) -> tuple[Path, Path]:
    """Copy the scanned project into ``tmp_path`` so a test may corrupt its index freely."""
    root = tmp_path / "proj"
    shutil.copytree(pristine_project, root)
    index_path = root / ".cache" / "codemap" / f"{root.name}.json"
    assert index_path.exists()
    return root, index_path


class TestSelfCheckOnLoad:
    """load_index rejects structurally broken indexes with a parseable JSON error."""

    def test_healthy_index_loads_and_serves(self, healthy_project, scan_query):
        """Contract baseline: an untouched scan-index output passes the self-check.

        Without this baseline every rejection below could be an always-failing query rather than a real self-check.
        """
        root, index_path = healthy_project

        result = _query_raw(scan_query, root, index_path, "deps", "consumer")

        assert result.returncode == 0, result.stderr + result.stdout
        assert "error" not in json.loads(result.stdout)

    @pytest.mark.parametrize(
        ("mutate", "reason"),
        [
            pytest.param(
                lambda idx: {k: v for k, v in idx.items() if k != "modules"}, "missing_keys", id="missing-modules-key"
            ),
            pytest.param(lambda idx: {**idx, "scan_version": "eleven"}, "bad_version", id="version-not-int"),
            pytest.param(lambda idx: {**idx, "scan_version": 1}, "version_too_old", id="version-too-old"),
            pytest.param(lambda idx: {**idx, "modules": {}}, "modules_not_list", id="modules-not-list"),
            pytest.param(lambda idx: {**idx, "collisions": "oops"}, "collisions_not_list", id="collisions-not-list"),
        ],
    )
    def test_broken_index_rejected_with_reason(self, healthy_project, scan_query, mutate, reason):
        """Each structural break yields exit 3 + JSON naming the failed check and the rebuild fix.

        A valid index is rewritten with one structural defect; the query must refuse it, name the failed check in
        ``reason`` and point the user at a rescan instead of answering from half-valid data.
        """
        root, index_path = healthy_project
        index_path.write_text(json.dumps(mutate(json.loads(index_path.read_text()))))

        result = _query_raw(scan_query, root, index_path, "deps", "consumer")

        assert result.returncode == 3
        data = json.loads(result.stdout)
        assert data["error"] == "index failed self-check"
        assert data["reason"] == reason
        assert "scan-codebase" in data["fix"]
        assert "self-check" in result.stderr

    def test_truncated_json_rejected(self, healthy_project, scan_query):
        """A half-written index file (invalid JSON) is refused with the same rebuild path.

        A writer killed mid-write leaves a prefix of the document; the failure must be a parseable JSON error, not a
        decoder traceback.
        """
        root, index_path = healthy_project
        raw = index_path.read_text()
        index_path.write_text(raw[: len(raw) // 2])

        result = _query_raw(scan_query, root, index_path, "deps", "consumer")

        assert result.returncode == 3
        assert json.loads(result.stdout)["error"] == "index is not valid JSON"

    def test_broken_index_never_partial_serves(self, healthy_project, scan_query):
        """No query output escapes before the self-check verdict — stdout is the error object only.

        A whole-graph query (``central``) over an index missing its modules must print nothing but the single error
        object, so a caller piping stdout never mixes partial results with the failure.
        """
        root, index_path = healthy_project
        index = json.loads(index_path.read_text())
        del index["modules"]
        index_path.write_text(json.dumps(index))

        result = _query_raw(scan_query, root, index_path, "central", "--top", "5")

        assert result.returncode == 3
        data = json.loads(result.stdout)  # the whole stdout is one JSON error object
        assert set(data) == {"error", "reason", "detail", "path", "fix"}
