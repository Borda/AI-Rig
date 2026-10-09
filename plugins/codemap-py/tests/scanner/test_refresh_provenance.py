"""Tests for bounded index-refresh provenance and what an incremental refresh can change."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest
from codemap_py import graph


def _git(root: Path, *args: str) -> str:
    """Run one git command in *root* and return its stripped stdout."""
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture(name="scanned_repo")
def _scanned_repo(tmp_path: Path) -> tuple[Path, dict]:
    """Commit one module, full-scan it, and return ``(root, index)``."""
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.t")
    _git(root, "config", "user.name", "t")
    (root / "a.py").write_text("def f():\n    return 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root, graph.scan(root)


def _symbols(index: dict) -> list[str]:
    """Return every indexed qualified name, sorted."""
    return sorted(sym["qualified_name"] for module in index["modules"] for sym in module["symbols"])


class TestIncrementalScanInputs:
    """An incremental refresh keys on staged blobs and HEAD only; the prompt hook's skip rule depends on this.

    Mirrors a real dirty-repository probe: unstaged and untracked edits never reach the index, a staged edit is
    re-parsed, and a commit that leaves staged blobs unchanged re-parses nothing but must still move ``git_sha``.
    """

    def test_unstaged_and_untracked_edits_change_nothing(self, scanned_repo, monkeypatch) -> None:
        """Working-tree-only edits are invisible to the scanner, so refreshing for them is pure cost."""
        root, index = scanned_repo
        (root / "a.py").write_text("def f():\n    return 1\n\n\ndef g():\n    return 2\n")
        (root / "new.py").write_text("def h():\n    return 3\n")
        monkeypatch.setattr(graph, "_parse_file", lambda *a: pytest.fail("no file may be re-parsed"))

        refreshed = graph.incremental_scan(root, index)

        assert _symbols(refreshed) == ["f"]
        assert refreshed["file_shas"] == index["file_shas"]
        assert refreshed["git_sha"] == index["git_sha"]

    def test_staged_edit_is_reparsed(self, scanned_repo) -> None:
        """Staging the same edit makes the scanner pick it up."""
        root, index = scanned_repo
        (root / "a.py").write_text("def f():\n    return 1\n\n\ndef g():\n    return 2\n")
        _git(root, "add", "a.py")

        refreshed = graph.incremental_scan(root, index)

        assert _symbols(refreshed) == ["f", "g"]

    def test_commit_of_already_indexed_stage_restamps_head(self, scanned_repo, monkeypatch) -> None:
        """Committing an already-indexed staged edit re-parses nothing yet records the new HEAD.

        Keeping the old ``git_sha`` left the index permanently behind HEAD, so every prompt spawned another no-op
        refresh until the next staged edit.
        """
        root, _ = scanned_repo
        (root / "a.py").write_text("def f():\n    return 2\n")
        _git(root, "add", "a.py")
        staged_index = graph.scan(root)
        _git(root, "commit", "-q", "-m", "second")
        monkeypatch.setattr(graph, "_parse_file", lambda *a: pytest.fail("no file may be re-parsed"))

        refreshed = graph.incremental_scan(root, staged_index)

        assert refreshed["git_sha"] == _git(root, "rev-parse", "HEAD") != staged_index["git_sha"]
        assert refreshed["modules"] == staged_index["modules"]
        assert refreshed["scanned_at"] == staged_index["scanned_at"]


@pytest.mark.parametrize(
    ("trigger", "changed", "stale", "expected"),
    [
        pytest.param("query_self_heal", "4", "true", ("query_self_heal", 4, True), id="query_self_heal"),
        pytest.param(
            "codex_prompt_background", "", "true", ("codex_prompt_background", None, True), id="codex-background"
        ),
        pytest.param("unknown", "-1", "maybe", ("direct_cli", None, None), id="unknown"),
        pytest.param(None, "", "", ("direct_cli", None, None), id="none"),
    ],
)
def test_refresh_result_normalizes_closed_provenance(
    monkeypatch: pytest.MonkeyPatch,
    trigger: str | None,
    changed: str,
    stale: str,
    expected: tuple[str, int | None, bool | None],
) -> None:
    """Only documented triggers and observed scalar facts reach successful index telemetry."""
    if trigger is None:
        monkeypatch.delenv("CODEMAP_REFRESH_TRIGGER", raising=False)
    else:
        monkeypatch.setenv("CODEMAP_REFRESH_TRIGGER", trigger)
    monkeypatch.setenv("CODEMAP_REFRESH_CHANGED_COUNT", changed)
    monkeypatch.setenv("CODEMAP_REFRESH_STALE_BEFORE", stale)

    result = graph._refresh_result(argparse.Namespace(incremental=True))

    assert (result["trigger"], result["changed_count"], result["stale_before"]) == expected
    assert result["incremental"] is True
    assert result["result_currency"] == "current"


class TestMarkRefreshPublished:
    """The scan the prompt hook spawned, and only it, marks the hook's refresh record published."""

    def test_named_record_is_marked_published(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """With both handoff variables set, the record ends up holding the fingerprint and ``published``."""
        record = tmp_path / "codemap-refresh-fp-proj-0123456789ab"
        record.write_text("abc\n1\nspawned", encoding="utf-8")
        monkeypatch.setenv("CODEMAP_REFRESH_RECORD", str(record))
        monkeypatch.setenv("CODEMAP_REFRESH_FINGERPRINT", "abc")

        graph._mark_refresh_published()

        fingerprint, _, state = record.read_text(encoding="utf-8").split("\n")
        assert (fingerprint, state) == ("abc", "published")

    @pytest.mark.parametrize(
        ("name", "fingerprint"),
        [
            pytest.param("unrelated.txt", "abc", id="foreign-file-name"),
            pytest.param("codemap-refresh-fp-proj-0123456789ab", "", id="no-fingerprint"),
        ],
    )
    def test_foreign_or_incomplete_handoff_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, fingerprint: str
    ) -> None:
        """An environment path the hook could not have produced is never written, nor is a record without its key."""
        target = tmp_path / name
        monkeypatch.setenv("CODEMAP_REFRESH_RECORD", str(target))
        monkeypatch.setenv("CODEMAP_REFRESH_FINGERPRINT", fingerprint)

        graph._mark_refresh_published()

        assert not target.exists()
