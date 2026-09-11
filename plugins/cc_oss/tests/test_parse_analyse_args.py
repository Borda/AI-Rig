"""Tests for ``bin/parse_analyse_args.py``.

Pure classification and slug parsing run without subprocesses; the repository-resolution paths monkeypatch
``subprocess.run`` so no real ``gh`` or ``git`` invocation occurs. Sentinel assertions read back the files the script
writes under an isolated ``TMPDIR``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import parse_analyse_args as paa


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


def _fake_commands(monkeypatch: pytest.MonkeyPatch, responses: dict[str, _FakeCompleted]) -> None:
    """Route ``subprocess.run`` to canned responses keyed by the executable name."""

    def fake_run(cmd: list[str], **_kwargs: object) -> _FakeCompleted:
        return responses.get(cmd[0], _FakeCompleted(returncode=1))

    monkeypatch.setattr(paa.subprocess, "run", fake_run)


# ---------------------------------------------------------------------------
# classify_report_path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(".reports/analyse/thread/out.md", (True, ".reports/analyse/thread/out.md"), id="report-path"),
        pytest.param("42", (False, ""), id="thread-number"),
        pytest.param("vitality owner/repo", (False, ""), id="vitality-keyword"),
        pytest.param("ecosystem", (False, ""), id="ecosystem-keyword"),
        pytest.param("vitality-notes.md", (False, ""), id="vitality-prefixed-md-not-a-report"),
    ],
)
def test_classify_report_path(raw: str, expected: tuple[bool, str]) -> None:
    """Only a plain .md path that is not a mode keyword counts as a direct report path."""
    assert paa.classify_report_path(raw) == expected


@pytest.mark.parametrize("raw", [".plans/todo_thing.md", "notes/todo_release.md"])
def test_classify_report_path_rejects_plan_files(raw: str) -> None:
    """Plan and todo files abort with exit code 1 rather than being treated as reports."""
    with pytest.raises(paa._Abort) as excinfo:
        paa.classify_report_path(raw)
    assert excinfo.value.code == 1
    assert "not valid report paths" in excinfo.value.lines[0]


def test_classify_mode_writes_sentinels(tmp_sentinels: Path) -> None:
    """Classify mode persists direct-path state and pins the analysis date."""
    assert paa.main(["--mode", "classify", "--args", ".reports/analyse/thread/out.md"]) == 0
    assert _sentinel(tmp_sentinels, "analyse-direct-path-mode") == "true"
    assert _sentinel(tmp_sentinels, "analyse-report-file") == ".reports/analyse/thread/out.md"
    assert len(_sentinel(tmp_sentinels, "analyse-today")) == len("2026-01-01")


def test_classify_mode_keeps_existing_today(tmp_sentinels: Path) -> None:
    """An already-pinned date survives a second run, so paths cannot straddle midnight."""
    (tmp_sentinels / "analyse-today-shared").write_text("2020-02-02\n", encoding="utf-8")
    assert paa.main(["--mode", "classify", "--args", "42"]) == 0
    assert _sentinel(tmp_sentinels, "analyse-today") == "2020-02-02"


def test_classify_mode_invalid_path_skips_sentinel_writes(tmp_sentinels: Path, capsys: pytest.CaptureFixture) -> None:
    """A plan file exits 1 before any sentinel is written, matching the original block's early exit."""
    assert paa.main(["--mode", "classify", "--args", ".plans/active/todo_x.md"]) == 1
    assert not (tmp_sentinels / "analyse-direct-path-mode-shared").exists()
    assert "Invalid report path" in capsys.readouterr().out


def test_sentinel_has_trailing_newline(tmp_sentinels: Path) -> None:
    """Sentinels end in a newline — ``IFS= read -r`` fails on a file without one."""
    paa.main(["--mode", "classify", "--args", "42"])
    assert (tmp_sentinels / "analyse-direct-path-mode-shared").read_text(encoding="utf-8") == "false\n"


# ---------------------------------------------------------------------------
# vitality repository resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        pytest.param("owner/repo", "owner/repo", id="bare-slug"),
        pytest.param("https://github.com/owner/repo", "owner/repo", id="url"),
        pytest.param("https://github.com/owner/repo/tree/main", "owner/repo", id="url-with-extra-segments"),
    ],
)
def test_repo_from_argument(extra: str, expected: str) -> None:
    """An explicit argument resolves to owner/repo for both accepted shapes."""
    assert paa.repo_from_argument(extra) == expected


def test_repo_from_argument_rejects_non_github_url() -> None:
    """A non-GitHub URL stops the run with a soft exit code."""
    with pytest.raises(paa._Abort) as excinfo:
        paa.repo_from_argument("https://gitlab.com/owner/repo")
    assert excinfo.value.code == 0
    assert "Not a GitHub URL" in excinfo.value.lines[0]


def test_repo_from_argument_rejects_garbage() -> None:
    """An unrecognised argument shape stops the run with a usage hint."""
    with pytest.raises(paa._Abort) as excinfo:
        paa.repo_from_argument("not a repo")
    assert excinfo.value.code == 0
    assert "Unrecognised vitality argument" in excinfo.value.lines[0]


def test_repo_from_context_prefers_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    """``gh repo view`` answers first when it is available."""
    _fake_commands(monkeypatch, {"gh": _FakeCompleted(stdout="owner/repo\n")})
    assert paa.repo_from_context(5) == "owner/repo"


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        pytest.param("git@github.com:owner/repo.git", "owner/repo", id="ssh"),
        pytest.param("https://github.com/owner/repo.git", "owner/repo", id="https"),
    ],
)
def test_repo_from_context_falls_back_to_remote(monkeypatch: pytest.MonkeyPatch, remote: str, expected: str) -> None:
    """With gh unavailable, the origin remote URL is parsed instead."""
    _fake_commands(
        monkeypatch,
        {"gh": _FakeCompleted(returncode=1), "git": _FakeCompleted(stdout=remote)},
    )
    assert paa.repo_from_context(5) == expected


def test_repo_from_context_rejects_non_github_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-GitHub origin stops the run rather than guessing a slug."""
    _fake_commands(
        monkeypatch,
        {"gh": _FakeCompleted(returncode=1), "git": _FakeCompleted(stdout="https://gitlab.com/owner/repo.git")},
    )
    with pytest.raises(paa._Abort) as excinfo:
        paa.repo_from_context(5)
    assert "not a GitHub repository" in excinfo.value.lines[0]


def test_repo_from_context_without_any_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    """No remote at all asks the user for an explicit URL."""
    _fake_commands(monkeypatch, {"gh": _FakeCompleted(returncode=1), "git": _FakeCompleted(returncode=1)})
    with pytest.raises(paa._Abort) as excinfo:
        paa.repo_from_context(5)
    assert "No GitHub repository detected" in excinfo.value.lines[0]


def test_vitality_mode_normalises_args(tmp_sentinels: Path) -> None:
    """A vitality invocation records owner/repo and collapses the argument to the bare keyword."""
    assert paa.main(["--args", "vitality owner/repo"]) == 0
    assert _sentinel(tmp_sentinels, "analyse-clean-args") == "vitality"
    assert _sentinel(tmp_sentinels, "analyse-gh-owner") == "owner"
    assert _sentinel(tmp_sentinels, "analyse-gh-repo") == "repo"


def test_vitality_mode_passes_through_thread_number(tmp_sentinels: Path) -> None:
    """A non-vitality argument is written back unchanged with an empty owner/repo pair."""
    assert paa.main(["--args", "42"]) == 0
    assert _sentinel(tmp_sentinels, "analyse-clean-args") == "42"
    assert _sentinel(tmp_sentinels, "analyse-gh-owner") == ""


def test_vitality_mode_soft_stop_skips_sentinel_writes(tmp_sentinels: Path) -> None:
    """An unrecognised vitality argument exits before rewriting any sentinel."""
    assert paa.main(["--args", "vitality not a repo"]) == 0
    assert not (tmp_sentinels / "analyse-clean-args-shared").exists()


def test_cut_field_mirrors_cut_passthrough() -> None:
    """A delimiter-free value is echoed for every field, exactly as ``cut`` without ``-s`` does."""
    assert paa.cut_field("owner", 1) == "owner"
    assert paa.cut_field("owner", 2) == "owner"


class TestDryRun:
    """Covers --dry-run: compute and print, write no session state.

    These sentinels are live skill state keyed by ``CSID``, read by later steps and by PreToolUse hooks. An inspection
    run of this script must not forge them — one such run wrote an ``analyse-report-file`` naming a report that was
    never produced, and the hook denial that followed blocked an unrelated question.
    """

    def test_writes_no_sentinels(self, tmp_sentinels: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """--dry-run leaves the sentinel directory empty and exits 0."""
        assert paa.main(["--mode", "classify", "--args", "42", "--dry-run"]) == 0
        assert list(tmp_sentinels.glob("*")) == []

    def test_announces_what_it_would_write(self, tmp_sentinels: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Each suppressed write is still reported, so a verifier sees the computed value."""
        assert paa.main(["--mode", "classify", "--args", "42", "--dry-run"]) == 0
        assert "[dry-run] would write " in capsys.readouterr().out

    def test_default_still_writes(self, tmp_sentinels: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Without the flag the real skill path is unchanged."""
        assert paa.main(["--mode", "classify", "--args", "42"]) == 0
        assert list(tmp_sentinels.glob("*")) != []
