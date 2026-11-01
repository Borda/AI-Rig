"""Tests for bounded index-refresh provenance and what an incremental refresh can change."""

from __future__ import annotations

import argparse
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from _scan_helpers import IGNORE_RUNNER_THREAD_FORK_WARNING
from codemap_py import graph
from codemap_py.scanner.discovery import get_file_hashes
from codemap_py.scanner.docs_xrefs import _iter_doc_files


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


@pytest.fixture(name="set_env")
def _set_env(monkeypatch: pytest.MonkeyPatch) -> Callable[[dict[str, str | None]], None]:
    """Return a setter applying ``name -> value`` to the environment, removing a name whose value is ``None``."""

    def _set(env: dict[str, str | None]) -> None:
        for name, value in env.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)

    return _set


def _symbols(index: dict) -> list[str]:
    """Return every indexed qualified name, sorted."""
    return sorted(sym["qualified_name"] for module in index["modules"] for sym in module["symbols"])


class TestIncrementalScanInputs:
    """Incremental refresh observes source bytes regardless of Git staging state."""

    @IGNORE_RUNNER_THREAD_FORK_WARNING
    def test_unstaged_and_untracked_edits_are_reparsed(self, scanned_repo) -> None:
        """The requested automatic freshness includes modified and newly discovered source files.

        One tracked module gains a function without being staged, and a brand-new untracked module appears; the refresh
        must see both, change the stored hashes, and keep the recorded HEAD because nothing was committed.
        """
        root, index = scanned_repo
        (root / "a.py").write_text("def f():\n    return 1\n\n\ndef g():\n    return 2\n")
        (root / "new.py").write_text("def h():\n    return 3\n")

        refreshed = graph.incremental_scan(root, index)

        assert _symbols(refreshed) == ["f", "g", "h"]
        assert refreshed["file_shas"] != index["file_shas"]
        assert refreshed["git_sha"] == index["git_sha"]

    @IGNORE_RUNNER_THREAD_FORK_WARNING
    def test_staged_edit_is_reparsed(self, scanned_repo) -> None:
        """Staging the same edit makes the scanner pick it up.

        Staging must not be required for freshness, but it must not hide the edit either.
        """
        root, index = scanned_repo
        (root / "a.py").write_text("def f():\n    return 1\n\n\ndef g():\n    return 2\n")
        _git(root, "add", "a.py")

        refreshed = graph.incremental_scan(root, index)

        assert _symbols(refreshed) == ["f", "g"]

    @IGNORE_RUNNER_THREAD_FORK_WARNING
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


class TestRefreshResult:
    """The successful-refresh record only carries documented triggers and observed scalar facts."""

    @pytest.mark.parametrize(
        ("env", "expected"),
        [
            pytest.param(
                {
                    "CODEMAP_REFRESH_TRIGGER": "query_self_heal",
                    "CODEMAP_REFRESH_CHANGED_COUNT": "4",
                    "CODEMAP_REFRESH_STALE_BEFORE": "true",
                },
                ("query_self_heal", 4, True),
                id="query_self_heal",
            ),
            pytest.param(
                {
                    "CODEMAP_REFRESH_TRIGGER": "codex_prompt_background",
                    "CODEMAP_REFRESH_CHANGED_COUNT": "",
                    "CODEMAP_REFRESH_STALE_BEFORE": "true",
                },
                ("codex_prompt_background", None, True),
                id="codex-background",
            ),
            pytest.param(
                {
                    "CODEMAP_REFRESH_TRIGGER": "unknown",
                    "CODEMAP_REFRESH_CHANGED_COUNT": "-1",
                    "CODEMAP_REFRESH_STALE_BEFORE": "maybe",
                },
                ("direct_cli", None, None),
                id="unknown",
            ),
            pytest.param(
                {
                    "CODEMAP_REFRESH_TRIGGER": None,
                    "CODEMAP_REFRESH_CHANGED_COUNT": "",
                    "CODEMAP_REFRESH_STALE_BEFORE": "",
                },
                ("direct_cli", None, None),
                id="none",
            ),
        ],
    )
    def test_normalizes_closed_provenance(
        self, set_env: Callable[[dict[str, str | None]], None], env: dict, expected: tuple
    ) -> None:
        """Only documented triggers and observed scalar facts reach successful index telemetry.

        An unknown trigger, a negative count or a non-boolean stale flag must fall back to ``direct_cli`` with the facts
        dropped, so arbitrary environment text can never leak into the record.
        """
        set_env(env)

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
        """An environment path the hook could not have produced is never written, nor is a record without its key.

        A stray ``CODEMAP_REFRESH_RECORD`` pointing at an arbitrary file name, or a record with no fingerprint to match,
        must leave the filesystem untouched.
        """
        target = tmp_path / name
        monkeypatch.setenv("CODEMAP_REFRESH_RECORD", str(target))
        monkeypatch.setenv("CODEMAP_REFRESH_FINGERPRINT", fingerprint)

        graph._mark_refresh_published()

        assert not target.exists()


@pytest.mark.parametrize(
    ("folder", "filename", "discover"),
    [
        pytest.param("pkg", "mod.py", get_file_hashes, id="source"),
        pytest.param("docs", "api.rst", _iter_doc_files, id="documentation"),
    ],
)
@IGNORE_RUNNER_THREAD_FORK_WARNING
def test_unreadable_discovery_never_becomes_an_empty_fresh_map(
    scanned_repo, monkeypatch: pytest.MonkeyPatch, folder: str, filename: str, discover: Callable
) -> None:
    """A denied directory traversal preserves the prior index and surfaces an unknown-freshness failure.

    A directory the scanner may not read must raise rather than yield an empty file map: an empty map would look like
    "nothing changed" and publish a fresh-looking index over unreadable input.
    """
    root, _ = scanned_repo
    unreadable = root / folder
    unreadable.mkdir()
    (unreadable / filename).write_bytes(b"content\n")
    scan_dir = os.scandir

    def denied_scan(path):
        """Simulate a filesystem refusing traversal without depending on host privilege or mode bits."""
        if Path(path) == unreadable:
            raise PermissionError("directory cannot be read")
        return scan_dir(path)

    monkeypatch.setattr(os, "scandir", denied_scan)

    with pytest.raises(PermissionError, match="directory cannot be read"):
        discover(root)
