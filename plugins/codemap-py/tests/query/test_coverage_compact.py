"""Reader-side edge cases for the scan-query session-scoped coverage diet.

Complements the ``TestCoverageDiet`` cases in ``test_scan_query.py`` (a full first response followed by compact ones,
verbose failure for a missing marker, and the ``--verbose-coverage`` override) by pinning the two fail-verbose branches
that block was missing and the compact block's honesty contract when a result is incomplete:

- Marker present but **unparsable** JSON → full block (never compact on a broken marker).
- Marker present but **ts older than the 30-min TTL** → full block (stale session).
- Compact block on an **incomplete** result keeps ``degraded`` + ``note`` so the diet never hides WHY a result is
  incomplete, alongside the always-present per-query honesty signals (``query_complete``, ``stale``, ``root_mismatch``).

The scan-query CLI resolves the runtime marker under ``<git-root>/.cache/codemap``, so each test tree is a real git
repo and each query a real CLI subprocess: the diet is a CLI contract. The per-session sentinel lives in the temp dir
keyed on the marker's session id; ``_run_coverage_query`` points ``TMPDIR`` at the test's own tree, so every test starts
without a sentinel and ordering is deterministic and isolated.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

# 30 min — mirrors _SESSION_MARKER_TTL_MS baked into scan-query.
_MARKER_TTL_MS = 30 * 60 * 1000

# Evaluated at import, so by test time it is older still: always past the TTL.
_STALE_MARKER = json.dumps({"session_id": "aged", "ts": int(time.time() * 1000) - (_MARKER_TTL_MS + 60_000)})


def _build_diet_repo(root: Path, scan_index: Path, *, degraded: bool = False) -> Path:
    """Git-init *root*, write one module, scan it, return the index path.

    When *degraded* is True a second module is written whose source is unparsable, so
    scan-index records it with ``status="degraded"`` — the whole-graph ``central`` query
    then reports ``query_complete=false`` and the compact block must keep its honesty
    fields.

    Args:
        root: directory to initialise as the scan root (also the git root).
        scan_index: path to the scan-index bin script.
        degraded: if True, add a syntactically broken module to force a degraded status.
    """
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
    (root / "modx.py").write_text("def fx(x):\n    return x\n")
    if degraded:
        (root / "broken.py").write_text("def oops(:\n    return\n")  # syntax error → degraded
    subprocess.run(
        [sys.executable, str(scan_index), "--root", str(root)], capture_output=True, cwd=str(root), check=True
    )
    return root / ".cache" / "codemap" / f"{root.name}.json"


def _write_marker(root: Path, session_id: str) -> None:
    """Write a fresh hook-owned Claude session marker for *session_id* matching the cross-agent contract."""
    _write_marker_text(root, "claude", json.dumps({"session_id": session_id, "ts": int(time.time() * 1000)}))


def _write_marker_text(root: Path, runtime: str, text: str) -> None:
    """Write *text* verbatim as the *runtime* session marker under ``<root>/.cache/codemap``."""
    marker = root / ".cache" / "codemap" / f"current-session-{runtime}.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(text)


def _run_coverage_query(scan_query: Path, root: Path, index_path: Path, *extra: str, runtime: str = "claude") -> dict:
    """Run ``central --top 1`` and return its ``index`` coverage block."""
    # TMPDIR points at the test's own tree: the coverage sentinel is keyed only on the
    # session id, and these tests hardcode theirs, so inheriting the machine-wide temp dir
    # makes two concurrent runs of this suite share one sentinel file and flip each other's
    # first query to compact (claude-config.md TMPDIR Sentinel Scoping).
    env = {**os.environ, "CODEMAP_LOGGING": "false", "TMPDIR": str(root), "CODEMAP_RUNTIME": runtime}
    result = subprocess.run(
        [sys.executable, str(scan_query), "--index", str(index_path), *extra, "central", "--top", "1"],
        capture_output=True,
        text=True,
        cwd=str(root),
        env=env,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    return json.loads(result.stdout)["index"]


class TestCoverageDietFailVerbose:
    """Marker present-but-invalid must never compact — the diet fails verbose."""

    @pytest.mark.parametrize(
        "marker_text",
        [
            pytest.param("{ this is not json", id="unparsable"),
            pytest.param(_STALE_MARKER, id="past-ttl"),
        ],
    )
    def test_invalid_marker_stays_verbose(self, tmp_path, scan_index, scan_query, marker_text):
        """An unparsable or expired session marker is treated as absent, so every response is the full block.

        Two successive queries run against a marker that is either not valid JSON or older than the 30-min TTL. A
        compact second response would mean a broken or stale marker engaged the diet.
        """
        index_path = _build_diet_repo(tmp_path, scan_index)
        _write_marker_text(tmp_path, "claude", marker_text)

        first = _run_coverage_query(scan_query, tmp_path, index_path)
        second = _run_coverage_query(scan_query, tmp_path, index_path)

        assert not first.get("compact")
        assert not second.get("compact"), "an invalid marker must never engage the diet"
        assert "total_modules" in second

    def test_each_runtime_reads_only_its_session_marker(self, tmp_path, scan_index, scan_query):
        """Codex compaction uses its own shard while preserving Claude's live marker.

        Both runtime markers are fresh with different session ids. A codex-runtime pair of queries must follow the
        codex marker alone: full first response, compact second.
        """
        index_path = _build_diet_repo(tmp_path, scan_index)
        now = int(time.time() * 1000)
        _write_marker_text(tmp_path, "claude", json.dumps({"session_id": "claude-session", "ts": now}))
        _write_marker_text(tmp_path, "codex", json.dumps({"session_id": "codex-session", "ts": now}))

        first = _run_coverage_query(scan_query, tmp_path, index_path, runtime="codex")
        second = _run_coverage_query(scan_query, tmp_path, index_path, runtime="codex")

        assert not first.get("compact")
        assert second.get("compact") is True


class TestCompactBlockHonesty:
    """The compact block keeps per-query honesty signals — and WHY when incomplete."""

    def test_always_carries_per_query_signals(self, tmp_path, scan_index, scan_query):
        """Every compact block carries the three per-query honesty signals regardless of value.

        A valid marker makes the second query compact. The diet may drop session-invariant keys, but never the signals
        that describe this particular result.
        """
        index_path = _build_diet_repo(tmp_path, scan_index)
        _write_marker(tmp_path, "signals")
        _run_coverage_query(scan_query, tmp_path, index_path)  # first → full, consumes sentinel

        compact = _run_coverage_query(scan_query, tmp_path, index_path)

        assert compact.get("compact") is True
        assert {"query_complete", "stale", "root_mismatch"} <= compact.keys()
        # The session-invariant full-block keys are dropped by the diet.
        assert "total_modules" not in compact
        assert "degraded_files" not in compact

    def test_keeps_degraded_and_note_when_incomplete(self, tmp_path, scan_index, scan_query):
        """When a degraded module makes the whole-graph query incomplete, the diet keeps WHY.

        The repo holds an unparsable module, so ``central`` reports ``query_complete=false``. The compact block must
        still carry the degraded count and the explanatory note, or an incomplete result would read as complete.
        """
        index_path = _build_diet_repo(tmp_path, scan_index, degraded=True)
        _write_marker(tmp_path, "incomplete")
        first = _run_coverage_query(scan_query, tmp_path, index_path)  # full block

        compact = _run_coverage_query(scan_query, tmp_path, index_path)  # compact block

        # Precondition: the degraded module really did make the query incomplete.
        assert first["query_complete"] is False, "expected the degraded module to make central incomplete"
        assert compact.get("compact") is True
        assert compact["query_complete"] is False
        # Honesty signal survives the diet: the reason for incompleteness is preserved.
        assert compact["degraded"] >= 1
        assert compact["note"], "the note explaining WHY must survive the compact diet"
