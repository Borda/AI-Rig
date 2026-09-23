"""Tests for ``bin/run_audit_checks.py``.

``subprocess.run`` and ``which`` monkeypatched — no real tools invoked. ``monkeypatch.chdir`` places filesystem scans
(version/signal grep) under ``tmp_path``. Tests cover arg parsing, tag injection guard, gh auth checks, check-banner
emission, and pure-Python helper functions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import run_audit_checks as rac


class _FakeCompleted:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        """Store the status and streams consumed by the audit runner."""
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _dispatch(responses: dict[str, tuple[int, str]]) -> Any:
    """Build a subprocess.run fake dispatching on binary + first subcommand."""

    def _fake_run(cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        """Dispatch a fake response by executable and first subcommand."""
        binary = Path(cmd[0]).name
        subcmd = cmd[1] if len(cmd) > 1 else ""
        key = f"{binary} {subcmd}".strip()
        for pattern, (rc, out) in responses.items():
            if pattern in key:
                return _FakeCompleted(returncode=rc, stdout=out)
        return _FakeCompleted(returncode=0, stdout="")

    return _fake_run


def _happy_dispatch() -> Any:
    """Return a subprocess mock that makes all checks pass."""
    return _dispatch(
        {
            "gh auth": (0, "Logged in to github.com\n"),
            "git status": (0, ""),
            "git log": (0, "abc123 some commit\n"),
            "git rev-parse": (0, "main"),
            "git diff": (0, ""),
            "git describe": (0, "v1.0.0"),
            "git remote": (0, "HEAD branch: main\n"),
            "gh run": (0, "[]"),
            "gh issue": (0, "[]"),
            "gh pr": (0, "[]"),
        }
    )


# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------


def test_unknown_arg_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unrecognized flag → exit 1 with 'unknown arg' on stderr."""
    monkeypatch.chdir(tmp_path)
    rc = rac.main(["--unknown"])
    assert rc == 1
    assert "unknown arg" in capsys.readouterr().err


def test_known_args_accepted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Accept repository, tag, and range options together."""
    monkeypatch.setattr(rac, "which", lambda cmd: "/fake/" + cmd)
    monkeypatch.setattr(rac.subprocess, "run", _happy_dispatch())
    monkeypatch.chdir(tmp_path)
    rc = rac.main(["--repo", "owner/repo", "--tag", "v1.0.0", "--range", "v0.9.0..HEAD"])
    assert rc == 0


# ---------------------------------------------------------------------------
# Tag injection guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_tag", ["-injected", "--injected", "-x"])
def test_tag_injection_guard_exits_2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    bad_tag: str,
) -> None:
    """LAST_TAG starting with '-' → exit 2 with 'invalid tag' on stderr."""
    monkeypatch.setenv("LAST_TAG", bad_tag)
    monkeypatch.setattr(rac, "which", lambda cmd: "/fake/" + cmd)
    monkeypatch.setattr(rac.subprocess, "run", _dispatch({}))
    monkeypatch.chdir(tmp_path)
    rc = rac.main([])
    assert rc == 2
    assert "invalid tag" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# gh preflight
# ---------------------------------------------------------------------------


def test_gh_not_found_exits_2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Gh not on PATH → exit 2."""
    monkeypatch.setenv("LAST_TAG", "v1.0.0")
    monkeypatch.setattr(rac, "which", lambda cmd: None if cmd == "gh" else "/fake/" + cmd)
    monkeypatch.setattr(rac.subprocess, "run", _dispatch({"git describe": (0, "v1.0.0")}))
    monkeypatch.chdir(tmp_path)
    rc = rac.main(["--range", "v1.0.0..HEAD"])
    assert rc == 2


def test_gh_not_authenticated_exits_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Gh present but auth fails → exit 2."""
    monkeypatch.setattr(rac, "which", lambda cmd: "/fake/" + cmd)
    monkeypatch.setattr(
        rac.subprocess,
        "run",
        _dispatch({"gh auth": (1, "not logged in")}),
    )
    monkeypatch.chdir(tmp_path)
    rc = rac.main(["--range", "v1.0.0..HEAD"])
    assert rc == 2


# ---------------------------------------------------------------------------
# Happy path — banner emission
# ---------------------------------------------------------------------------


def test_happy_path_emits_all_check_banners(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """All mocked tools pass → all six check banners + end banner emitted."""
    monkeypatch.setattr(rac, "which", lambda cmd: "/fake/" + cmd)
    monkeypatch.setattr(rac.subprocess, "run", _happy_dispatch())
    monkeypatch.chdir(tmp_path)
    rc = rac.main(["--range", "v1.0.0..HEAD"])
    assert rc == 0
    out = capsys.readouterr().out
    for banner in (
        "--- check: gh-auth ---",
        "--- check: repo-state ---",
        "--- check: ci-health ---",
        "--- check: open-issues-prs ---",
        "--- check: docs-alignment ---",
        "--- check: version-consistency ---",
        "--- check: code-signals ---",
        "--- check: end ---",
    ):
        assert banner in out, f"missing banner: {banner!r}"


def test_happy_path_exits_0(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """All checks pass → exit 0."""
    monkeypatch.setattr(rac, "which", lambda cmd: "/fake/" + cmd)
    monkeypatch.setattr(rac.subprocess, "run", _happy_dispatch())
    monkeypatch.chdir(tmp_path)
    rc = rac.main(["--range", "v1.0.0..HEAD"])
    assert rc == 0


def test_tag_arg_emitted_in_version_section(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Include the requested tag in version-consistency output."""
    monkeypatch.setattr(rac, "which", lambda cmd: "/fake/" + cmd)
    monkeypatch.setattr(rac.subprocess, "run", _happy_dispatch())
    monkeypatch.chdir(tmp_path)
    rac.main(["--range", "v1.0.0..HEAD", "--tag", "v2.0.0"])
    out = capsys.readouterr().out
    assert "v2.0.0" in out


# ---------------------------------------------------------------------------
# pip-audit missing-tool signal — Check 6 (see templates/audit-checks.md
# "Check 6 interpretation" for the AskUserQuestion install-or-skip gate)
# ---------------------------------------------------------------------------


def test_pip_audit_missing_emits_greppable_signal_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Report a missing audit dependency without failing the overall command."""
    monkeypatch.setattr(rac, "which", lambda cmd: None if cmd == "pip-audit" else "/fake/" + cmd)
    monkeypatch.setattr(rac.subprocess, "run", _happy_dispatch())
    monkeypatch.chdir(tmp_path)
    rc = rac.main(["--range", "v1.0.0..HEAD"])
    assert rc == 0
    out = capsys.readouterr().out
    assert rac.PIP_AUDIT_MISSING_SIGNAL in out
    assert "install with: pip install pip-audit" in out


def test_pip_audit_present_does_not_emit_missing_signal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Omit the missing-tool signal when the audit dependency is available."""
    monkeypatch.setattr(rac, "which", lambda cmd: "/fake/" + cmd)
    monkeypatch.setattr(rac.subprocess, "run", _happy_dispatch())
    monkeypatch.chdir(tmp_path)
    rc = rac.main(["--range", "v1.0.0..HEAD"])
    assert rc == 0
    out = capsys.readouterr().out
    assert rac.PIP_AUDIT_MISSING_SIGNAL not in out


# ---------------------------------------------------------------------------
# _grep_version_files
# ---------------------------------------------------------------------------


def test_grep_version_files_finds_py_match(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Python file with ``__version__`` → match returned in results."""
    (tmp_path / "mod.py").write_text('__version__ = "1.0.0"\n')
    monkeypatch.chdir(tmp_path)
    results = rac._grep_version_files()
    assert any("__version__" in r for r in results)


def test_grep_version_files_finds_toml_match(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """TOML file with ``version =`` → match returned."""
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    monkeypatch.chdir(tmp_path)
    results = rac._grep_version_files()
    assert any("version" in r for r in results)


def test_grep_version_files_excludes_git_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Files inside ``.git/`` are not scanned."""
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config.py").write_text('__version__ = "0.0.1"\n')
    monkeypatch.chdir(tmp_path)
    results = rac._grep_version_files()
    assert not any(".git" in r for r in results)


def test_grep_version_files_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Results capped at ``_MAX_VERSION_LINES``."""
    for i in range(rac._MAX_VERSION_LINES + 5):
        (tmp_path / f"m{i}.py").write_text(f'__version__ = "{i}"\n')
    monkeypatch.chdir(tmp_path)
    results = rac._grep_version_files()
    assert len(results) <= rac._MAX_VERSION_LINES


def test_grep_version_files_returns_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Always returns a list (empty when no matches)."""
    monkeypatch.chdir(tmp_path)
    results = rac._grep_version_files()
    assert isinstance(results, list)


def test_grep_version_files_truncation_signal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Hitting the file-count cap prints SCAN_TRUNCATED_SIGNAL and still returns a bounded list.

    The cap is lowered via monkeypatch so the test only needs a handful of fixture files, not thousands, to exercise the
    truncation path.
    """
    monkeypatch.setattr(rac, "_MAX_SCAN_FILES", 2)
    for i in range(4):
        (tmp_path / f"m{i}.py").write_text(f'__version__ = "{i}"\n')
    monkeypatch.chdir(tmp_path)
    results = rac._grep_version_files()
    assert len(results) <= 2
    assert rac.SCAN_TRUNCATED_SIGNAL in capsys.readouterr().err


def test_grep_version_files_skips_oversized_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A file above ``_MAX_SCAN_FILE_SIZE`` is never read, even if it would otherwise match."""
    monkeypatch.setattr(rac, "_MAX_SCAN_FILE_SIZE", 10)
    (tmp_path / "huge.py").write_text('__version__ = "0.0.0"  # padding beyond the size cap\n')
    monkeypatch.chdir(tmp_path)
    results = rac._grep_version_files()
    assert results == []


# ---------------------------------------------------------------------------
# _grep_code_signals
# ---------------------------------------------------------------------------


def test_grep_code_signals_finds_fixme(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Python file with ``FIXME`` → match returned."""
    (tmp_path / "src.py").write_text("# FIXME: clean this up\nx = 1\n")
    monkeypatch.chdir(tmp_path)
    results = rac._grep_code_signals()
    assert any("FIXME" in r for r in results)


def test_grep_code_signals_finds_todo_release(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Match release tasks through the configured pattern."""
    (tmp_path / "src.py").write_text("# TODO before release: update changelog\n")
    monkeypatch.chdir(tmp_path)
    results = rac._grep_code_signals()
    assert any("TODO" in r for r in results)


def test_grep_code_signals_excludes_tests(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Files inside ``tests/`` directory not scanned."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text("# FIXME in test\n")
    monkeypatch.chdir(tmp_path)
    results = rac._grep_code_signals()
    assert not any("FIXME" in r for r in results)


def test_grep_code_signals_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Results capped at ``_MAX_SIGNAL_LINES``."""
    for i in range(rac._MAX_SIGNAL_LINES + 5):
        (tmp_path / f"s{i}.py").write_text(f"# FIXME item {i}\n")
    monkeypatch.chdir(tmp_path)
    results = rac._grep_code_signals()
    assert len(results) <= rac._MAX_SIGNAL_LINES


def test_grep_code_signals_truncation_signal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Hitting the file-count cap prints SCAN_TRUNCATED_SIGNAL and still returns a bounded list."""
    monkeypatch.setattr(rac, "_MAX_SCAN_FILES", 2)
    for i in range(4):
        (tmp_path / f"s{i}.py").write_text(f"# FIXME item {i}\n")
    monkeypatch.chdir(tmp_path)
    results = rac._grep_code_signals()
    assert len(results) <= 2
    assert rac.SCAN_TRUNCATED_SIGNAL in capsys.readouterr().err


def test_grep_code_signals_skips_oversized_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A file above ``_MAX_SCAN_FILE_SIZE`` is never read, even if it would otherwise match."""
    monkeypatch.setattr(rac, "_MAX_SCAN_FILE_SIZE", 10)
    (tmp_path / "huge.py").write_text("# FIXME padding beyond the size cap\n")
    monkeypatch.chdir(tmp_path)
    results = rac._grep_code_signals()
    assert results == []


def test_grep_code_signals_returns_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Always returns a list (empty when no signals)."""
    monkeypatch.chdir(tmp_path)
    results = rac._grep_code_signals()
    assert isinstance(results, list)


# ---------------------------------------------------------------------------
# _detect_trunk
# ---------------------------------------------------------------------------


def test_detect_trunk_parses_head_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """'HEAD branch: develop' in remote output → returns 'develop'."""
    monkeypatch.setattr(
        rac.subprocess,
        "run",
        _dispatch({"git remote": (0, "  HEAD branch: develop\n")}),
    )
    result = rac._detect_trunk("/fake/git")
    assert result == "develop"


def test_detect_trunk_fallback_main(monkeypatch: pytest.MonkeyPatch) -> None:
    """No 'HEAD branch' line in remote output → falls back to 'main'."""
    monkeypatch.setattr(
        rac.subprocess,
        "run",
        _dispatch({"git remote": (0, "  origin  https://example.com (fetch)\n")}),
    )
    result = rac._detect_trunk("/fake/git")
    assert result == "main"


def test_detect_trunk_remote_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Git remote show fails → falls back to 'main'."""
    monkeypatch.setattr(
        rac.subprocess,
        "run",
        _dispatch({"git remote": (1, "")}),
    )
    result = rac._detect_trunk("/fake/git")
    assert result == "main"


# ---------------------------------------------------------------------------
# Range resolution
# ---------------------------------------------------------------------------


def test_range_from_env_last_tag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """LAST_TAG env var used in range when --range not supplied."""
    monkeypatch.setenv("LAST_TAG", "v0.5.0")
    monkeypatch.setattr(rac, "which", lambda cmd: "/fake/" + cmd)
    monkeypatch.setattr(rac.subprocess, "run", _happy_dispatch())
    monkeypatch.chdir(tmp_path)
    rc = rac.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "v0.5.0..HEAD" in out


def test_no_tags_falls_back_to_initial_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No tags at all → initial commit SHA used as range left side."""
    monkeypatch.delenv("LAST_TAG", raising=False)
    initial_sha = "deadbeef"

    def _seq_run(cmd: list[str], **_: Any) -> _FakeCompleted:
        """Return the scripted no-tag audit responses in call order."""
        binary = Path(cmd[0]).name
        subcmd = cmd[1] if len(cmd) > 1 else ""
        if binary == "git" and subcmd == "describe":
            return _FakeCompleted(returncode=1, stdout="")
        if binary == "git" and subcmd == "rev-list":
            return _FakeCompleted(returncode=0, stdout=initial_sha + "\n")
        if binary == "gh" and subcmd == "auth":
            return _FakeCompleted(returncode=0, stdout="Logged in\n")
        return _FakeCompleted(returncode=0, stdout="")

    monkeypatch.setattr(rac, "which", lambda cmd: "/fake/" + cmd)
    monkeypatch.setattr(rac.subprocess, "run", _seq_run)
    monkeypatch.chdir(tmp_path)
    rc = rac.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert initial_sha in out
