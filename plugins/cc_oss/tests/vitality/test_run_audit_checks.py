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
# _grep_version_files / _grep_code_signals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scan", "filename", "content", "needle"),
    [
        pytest.param(rac._grep_version_files, "mod.py", '__version__ = "1.0.0"\n', "__version__", id="version-py"),
        pytest.param(
            rac._grep_version_files, "pyproject.toml", '[project]\nversion = "0.1.0"\n', "version", id="version-toml"
        ),
        pytest.param(rac._grep_code_signals, "src.py", "# FIXME: clean this up\nx = 1\n", "FIXME", id="signal-fixme"),
        pytest.param(
            rac._grep_code_signals,
            "src.py",
            "# TODO before release: update changelog\n",
            "TODO",
            id="signal-todo-release",
        ),
    ],
)
def test_grep_finds_match(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scan: Any, filename: str, content: str, needle: str
) -> None:
    """A Python ``__version__`` or TOML ``version =`` line, and a FIXME or release-TODO comment, are returned."""
    (tmp_path / filename).write_text(content)
    monkeypatch.chdir(tmp_path)
    results = scan()
    assert any(needle in r for r in results)


@pytest.mark.parametrize(
    ("scan", "dirname", "filename", "content", "needle"),
    [
        pytest.param(
            rac._grep_version_files, ".git", "config.py", '__version__ = "0.0.1"\n', ".git", id="version-git-dir"
        ),
        pytest.param(
            rac._grep_code_signals, "tests", "test_x.py", "# FIXME in test\n", "FIXME", id="signals-tests-dir"
        ),
    ],
)
def test_grep_excludes_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scan: Any, dirname: str, filename: str, content: str, needle: str
) -> None:
    """Files inside ``.git/`` are not scanned for versions, and files inside ``tests/`` not for code signals."""
    excluded = tmp_path / dirname
    excluded.mkdir()
    (excluded / filename).write_text(content)
    monkeypatch.chdir(tmp_path)
    results = scan()
    assert not any(needle in r for r in results)


@pytest.mark.parametrize(
    ("scan", "limit_name", "prefix", "content"),
    [
        pytest.param(rac._grep_version_files, "_MAX_VERSION_LINES", "m", '__version__ = "{i}"\n', id="version-lines"),
        pytest.param(rac._grep_code_signals, "_MAX_SIGNAL_LINES", "s", "# FIXME item {i}\n", id="signal-lines"),
    ],
)
def test_grep_results_capped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scan: Any, limit_name: str, prefix: str, content: str
) -> None:
    """Results are capped at ``_MAX_VERSION_LINES`` / ``_MAX_SIGNAL_LINES``."""
    limit = getattr(rac, limit_name)
    for i in range(limit + 5):
        (tmp_path / f"{prefix}{i}.py").write_text(content.format(i=i))
    monkeypatch.chdir(tmp_path)
    results = scan()
    assert len(results) <= limit


@pytest.mark.parametrize("scan", [rac._grep_version_files, rac._grep_code_signals])
def test_grep_returns_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scan: Any) -> None:
    """Always returns a list (empty when no matches or signals)."""
    monkeypatch.chdir(tmp_path)
    results = scan()
    assert isinstance(results, list)


@pytest.mark.parametrize(
    ("scan", "prefix", "content"),
    [
        pytest.param(rac._grep_version_files, "m", '__version__ = "{i}"\n', id="version-files"),
        pytest.param(rac._grep_code_signals, "s", "# FIXME item {i}\n", id="code-signals"),
    ],
)
def test_grep_truncation_signal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    scan: Any,
    prefix: str,
    content: str,
) -> None:
    """Hitting the file-count cap prints SCAN_TRUNCATED_SIGNAL and still returns a bounded list.

    The cap is lowered via monkeypatch so the test only needs a handful of fixture files, not thousands, to exercise the
    truncation path.
    """
    monkeypatch.setattr(rac, "_MAX_SCAN_FILES", 2)
    for i in range(4):
        (tmp_path / f"{prefix}{i}.py").write_text(content.format(i=i))
    monkeypatch.chdir(tmp_path)
    results = scan()
    assert len(results) <= 2
    assert rac.SCAN_TRUNCATED_SIGNAL in capsys.readouterr().err


@pytest.mark.parametrize(
    ("scan", "content"),
    [
        pytest.param(
            rac._grep_version_files, '__version__ = "0.0.0"  # padding beyond the size cap\n', id="version-files"
        ),
        pytest.param(rac._grep_code_signals, "# FIXME padding beyond the size cap\n", id="code-signals"),
    ],
)
def test_grep_skips_oversized_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scan: Any, content: str) -> None:
    """A file above ``_MAX_SCAN_FILE_SIZE`` is never read, even if it would otherwise match."""
    monkeypatch.setattr(rac, "_MAX_SCAN_FILE_SIZE", 10)
    (tmp_path / "huge.py").write_text(content)
    monkeypatch.chdir(tmp_path)
    results = scan()
    assert results == []


# ---------------------------------------------------------------------------
# _detect_trunk
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("remote_response", "expected"),
    [
        pytest.param((0, "  HEAD branch: develop\n"), "develop", id="parses-head-branch"),
        pytest.param(
            (0, "  origin  https://example.com (fetch)\n"), "main", id="no-head-branch-line-falls-back-to-main"
        ),
        pytest.param((1, ""), "main", id="remote-failure-falls-back-to-main"),
    ],
)
def test_detect_trunk(monkeypatch: pytest.MonkeyPatch, remote_response: tuple[int, str], expected: str) -> None:
    """'HEAD branch: develop' in remote output → 'develop'; no such line, or a failing remote show → 'main'."""
    monkeypatch.setattr(rac.subprocess, "run", _dispatch({"git remote": remote_response}))
    assert rac._detect_trunk("/fake/git") == expected


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
