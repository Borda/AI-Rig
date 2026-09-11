"""Tests for ``bin/build_analyse_paths.py``.

Path construction and slug sanitisation are pure; the repository lookup monkeypatches ``subprocess.run`` so no real
``gh`` invocation occurs. Sentinel assertions read back the files the script writes under an isolated ``TMPDIR``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import build_analyse_paths as bap


class _FakeCompleted:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        """Store the status and output consumed by the resolver."""
        self.returncode = returncode
        self.stdout = stdout


@pytest.fixture
def tmp_sentinels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point sentinel writes at an isolated temp directory."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    return tmp_path


def _sentinel(tmp_path: Path, name: str) -> str:
    """Read one sentinel written by the script, without its trailing newline."""
    return (tmp_path / f"{name}-shared").read_text(encoding="utf-8").rstrip("\n")


def _fake_gh(monkeypatch: pytest.MonkeyPatch, stdout: str, returncode: int = 0) -> None:
    """Make every subprocess call answer as ``gh repo view`` would."""
    monkeypatch.setattr(
        bap.subprocess,
        "run",
        lambda *_a, **_k: _FakeCompleted(returncode=returncode, stdout=stdout),
    )


# ---------------------------------------------------------------------------
# slug sanitisation — report and cache modes deliberately differ
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("owner/repo", "owner-repo", id="plain"),
        pytest.param("Owner.AI/repo_x", "OwnerAI-repox", id="strips-dots-and-underscores"),
        pytest.param("", "local", id="unresolved-falls-back"),
    ],
)
def test_report_slug(raw: str, expected: str) -> None:
    """Report slugs keep only alphanumerics and dashes, falling back to ``local``."""
    assert bap.report_slug(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("owner/repo", "owner-repo", id="plain"),
        pytest.param("Owner.AI/repo_x", "Owner.AI-repo_x", id="keeps-dots-and-underscores"),
        pytest.param("", "", id="unresolved-stays-empty"),
    ],
)
def test_cache_slug_keeps_more_than_report_slug(raw: str, expected: str) -> None:
    """Cache keys only replace slashes — the stricter report sanitisation does not apply here."""
    assert bap.cache_slug(raw) == expected


def test_build_report_path() -> None:
    """The report path embeds subdir, slug, argument and date."""
    got = bap.build_report_path("thread", "owner-repo", "42", "2026-09-11")
    assert got == ".reports/analyse/thread/output-analyse-thread-owner-repo-42-2026-09-11.md"


def test_build_cache_path_disabled_without_slug() -> None:
    """An unresolvable repository disables caching rather than risking a cross-repo collision."""
    assert bap.build_cache_path("", "42", "2026-09-11") == ""


# ---------------------------------------------------------------------------
# report mode
# ---------------------------------------------------------------------------


def test_report_mode_without_existing_report(tmp_sentinels: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing report leaves the fast path off and the mtime at zero."""
    monkeypatch.chdir(tmp_sentinels)
    monkeypatch.setenv("TMPDIR", str(tmp_sentinels))
    _fake_gh(monkeypatch, "owner/repo")
    assert bap.main(["--clean-args", "42", "--today", "2026-09-11"]) == 0
    assert _sentinel(tmp_sentinels, "analyse-fast-path-tentative") == "false"
    assert _sentinel(tmp_sentinels, "analyse-report-mtime") == "0"
    assert _sentinel(tmp_sentinels, "analyse-drift") == "false"
    assert _sentinel(tmp_sentinels, "analyse-fast-path") == "false"


def test_report_mode_with_existing_report(tmp_sentinels: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An existing report arms the tentative fast path and records its mtime."""
    monkeypatch.chdir(tmp_sentinels)
    monkeypatch.setenv("TMPDIR", str(tmp_sentinels))
    _fake_gh(monkeypatch, "owner/repo")
    report = tmp_sentinels / bap.build_report_path("thread", "owner-repo", "42", "2026-09-11")
    report.parent.mkdir(parents=True)
    report.write_text("x", encoding="utf-8")
    assert bap.main(["--clean-args", "42", "--today", "2026-09-11"]) == 0
    assert _sentinel(tmp_sentinels, "analyse-fast-path-tentative") == "true"
    assert int(_sentinel(tmp_sentinels, "analyse-report-mtime")) > 0


# ---------------------------------------------------------------------------
# cache mode
# ---------------------------------------------------------------------------


def test_cache_mode_creates_dir_and_sentinel(tmp_sentinels: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cache mode records the key path and creates the cache directory."""
    monkeypatch.chdir(tmp_sentinels)
    monkeypatch.setenv("TMPDIR", str(tmp_sentinels))
    _fake_gh(monkeypatch, "owner/repo")
    assert bap.main(["--mode", "cache", "--clean-args", "42", "--today", "2026-09-11"]) == 0
    assert _sentinel(tmp_sentinels, "analyse-cache-file") == ".cache/gh/owner-repo-42-2026-09-11.json"
    assert (tmp_sentinels / ".cache/gh").is_dir()


def test_cache_mode_stops_thread_without_repo_context(
    tmp_sentinels: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A numeric argument outside a GitHub repo prints the stop notice — after the sentinel and mkdir."""
    monkeypatch.chdir(tmp_sentinels)
    monkeypatch.setenv("TMPDIR", str(tmp_sentinels))
    _fake_gh(monkeypatch, "", returncode=1)
    assert bap.main(["--mode", "cache", "--clean-args", "42", "--today", "2026-09-11"]) == 0
    assert _sentinel(tmp_sentinels, "analyse-cache-file") == ""
    assert (tmp_sentinels / ".cache/gh").is_dir()
    assert "No GitHub repository context" in capsys.readouterr().out


def test_cache_mode_non_numeric_without_repo_is_quiet(
    tmp_sentinels: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The thread-mode guard only fires for numeric arguments."""
    monkeypatch.chdir(tmp_sentinels)
    monkeypatch.setenv("TMPDIR", str(tmp_sentinels))
    _fake_gh(monkeypatch, "", returncode=1)
    assert bap.main(["--mode", "cache", "--clean-args", "ecosystem", "--today", "2026-09-11"]) == 0
    assert "No GitHub repository context" not in capsys.readouterr().out


def test_sentinel_has_trailing_newline(tmp_sentinels: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sentinels end in a newline — ``IFS= read -r`` fails on a file without one."""
    monkeypatch.chdir(tmp_sentinels)
    monkeypatch.setenv("TMPDIR", str(tmp_sentinels))
    _fake_gh(monkeypatch, "owner/repo")
    bap.main(["--clean-args", "42", "--today", "2026-09-11"])
    assert (tmp_sentinels / "analyse-drift-shared").read_text(encoding="utf-8") == "false\n"


class TestDryRun:
    """Covers --dry-run: compute and print, write no session state.

    These sentinels are live skill state keyed by ``CSID``, read by later steps and by PreToolUse hooks. An inspection
    run of this script must not forge them — one such run wrote an ``analyse-report-file`` naming a report that was
    never produced, and the hook denial that followed blocked an unrelated question.
    """

    def test_writes_no_sentinels(self, tmp_sentinels: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """--dry-run leaves the sentinel directory empty and exits 0."""
        assert bap.main(["--clean-args", "42", "--today", "2026-09-11", "--dry-run"]) == 0
        assert list(tmp_sentinels.glob("*")) == []

    def test_announces_what_it_would_write(self, tmp_sentinels: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Each suppressed write is still reported, so a verifier sees the computed value."""
        assert bap.main(["--clean-args", "42", "--today", "2026-09-11", "--dry-run"]) == 0
        assert "[dry-run] would write " in capsys.readouterr().out

    def test_default_still_writes(self, tmp_sentinels: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Without the flag the real skill path is unchanged."""
        assert bap.main(["--clean-args", "42", "--today", "2026-09-11"]) == 0
        assert list(tmp_sentinels.glob("*")) != []
