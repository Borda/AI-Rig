"""Step 11's remote-branch and leftovers block reads the run's real sentinels and records, end to end."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _oss_shell_plugin import render_shell
from _text_io import write_lf

_RESOLVE = Path(__file__).resolve().parents[3] / "skills/resolve"
_PLUGIN = _RESOLVE.parents[1]
_BASH = shutil.which("bash")
_SESSION = "resolve-end-of-run-test"
_MARKER = 'resolve_remote_state.py" --push-status "$PUSH_STATUS"'
pytestmark = pytest.mark.skipif(_BASH is None or shutil.which("git") is None, reason="the shipped block is Bash")
_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _git(cwd: Path, *args: str) -> str:
    """Run one git command and return its stripped stdout."""
    env = os.environ | _GIT_ENV
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def _block() -> str:
    """Return the shipped Step 11 remote-branch and leftovers block."""
    text = (_RESOLVE / "modes/final-report.md").read_text(encoding="utf-8")
    position = text.index(_MARKER)
    start = text.rindex("```bash\n", 0, position) + len("```bash\n")
    return text[start : text.index("```", position)]


def _run_report(repo: Path, sentinels: Path) -> subprocess.CompletedProcess[str]:
    """Run the shipped final remote comparison against a local checkout and its evidence."""
    env = (
        os.environ
        | _GIT_ENV
        | {
            "CLAUDE_CODE_SESSION_ID": _SESSION,
            "CLAUDE_PLUGIN_ROOT": str(_PLUGIN),
            "TMPDIR": str(sentinels),
            "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
        }
    )
    assert _BASH is not None
    return subprocess.run(
        [_BASH, "-c", render_shell(_block(), _PLUGIN, "oss")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=60,
    )


def test_rejected_push_reports_delivery_from_remote_evidence() -> None:
    """Rejection describes the attempt, while confidence follows observed commit reachability."""
    lines = (_RESOLVE / "modes/final-report.md").read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("**A push the user approved"))
    end = next(i for i, line in enumerate(lines) if line.startswith("**Unblock push"))
    rejection = lines[start:end]

    assert any("`rejected-remote`" in line and "`rejected-non-ff`" in line for line in rejection)
    assert any("opening one-line summary" in line and "push failed" in line for line in rejection)
    assert any("`REMOTE_STATE`" in line and "`REMOTE_DETAIL`" in line for line in rejection)
    assert any("all" in line and "no delivery deduction" in line for line in rejection)
    assert any("part" in line and "missing commits" in line for line in rejection)
    assert any("unverified" in line and "`unknown`" in line for line in rejection)
    assert all("does not carry these commits" not in line for line in rejection)


def test_block_reports_remote_state_leftovers_and_fallbacks(tmp_path: Path) -> None:
    """After a skipped push the block names the unchanged remote, the kept branch and the whole-file fallback."""
    origin, work, tmp, impl = tmp_path / "origin.git", tmp_path / "work", tmp_path / "tmp", tmp_path / "impl"
    for directory in (tmp, impl):
        directory.mkdir()
    _git(tmp_path, "init", "-q", "--bare", "-b", "feature", str(origin))
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "switch", "-q", "-c", "feature")
    (work / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(work, "add", "core.py")
    _git(work, "commit", "-q", "-m", "base")
    start = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "-q", "origin", "feature")
    (work / "core.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(work, "commit", "-q", "-am", "fix")
    _git(work, "branch", "worktree-agent-a1")
    # records and sentinels LF only, as the run writes them: `IFS= read -r` keeps a Windows carriage return
    write_lf(impl / "recovery-branches.txt", "worktree-agent-a1\tuncommitted\n")
    write_lf(impl / "group-replay-fallbacks.txt", "core.py (item 2, group tests: …)\n")
    sentinels = {
        "resolve-push-status": "skipped-by-user",
        "resolve-fork-remote": "origin",
        "resolve-remote-head-ref": "feature",
        "resolve-pr-head-oid": start,
        "resolve-impl-dir": str(impl),
        "resolve-stash-before": "",
    }
    for name, value in sentinels.items():
        write_lf(tmp / f"{name}-{_SESSION}", f"{value}\n" if value else "")
    result = _run_report(work, tmp)

    lines = result.stdout.splitlines()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "REMOTE_STATE=unchanged" in lines
    assert any(line.startswith("LEFTOVER_BRANCH=worktree-agent-a1 at ") for line in lines)
    assert "CLEANUP=git branch -D worktree-agent-a1" in lines
    assert "GROUP_REPLAY_FALLBACK=core.py (item 2, group tests: …)" in lines


@pytest.fixture
def compared_branch(tmp_path: Path) -> tuple[Path, str, Path]:
    """Set up a feature branch, a local bare remote and the report's run-start sentinels."""
    origin, work, sentinels = tmp_path / "origin.git", tmp_path / "work", tmp_path / "sentinels"
    sentinels.mkdir()
    _git(tmp_path, "init", "-q", "--bare", "-b", "feature", str(origin))
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "commit", "-q", "--allow-empty", "-m", "run start")
    start = _git(work, "rev-parse", "HEAD")
    for name, value in {
        "push-status": "rejected-non-ff",
        "fork-remote": "origin",
        "remote-head-ref": "feature",
        "pr-head-oid": start,
    }.items():
        write_lf(sentinels / f"resolve-{name}-{_SESSION}", f"{value}\n")
    return work, start, sentinels


def _empty_commit(repo: Path, message: str) -> str:
    """Create a distinct commit and return its full object ID."""
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def test_report_excludes_shared_base_history(compared_branch: tuple[Path, str, Path]) -> None:
    """Independent local and remote merges share the imported base, but neither shares a run commit."""
    repo, start, sentinels = compared_branch
    _git(repo, "switch", "-qc", "base")
    base = _empty_commit(repo, "base advancement")
    _git(repo, "switch", "-qc", "published", start)
    _git(repo, "merge", "--no-ff", "-qm", "remote base merge", base)
    _git(repo, "push", "-q", "origin", "HEAD:feature")
    _git(repo, "switch", "-q", "feature")
    _git(repo, "merge", "--no-ff", "-qm", "run base merge", base)
    _empty_commit(repo, "run fixes")
    write_lf(sentinels / f"resolve-remote-imports-{_SESSION}", f"base {start} {base}\n")

    result = _run_report(repo, sentinels)

    assert result.returncode == 0, result.stderr
    assert "REMOTE_STATE=diverged" in result.stdout.splitlines()
    assert "does not carry this run's commits" in result.stdout


@pytest.mark.parametrize(
    ("scenario", "expected", "detail"),
    [
        pytest.param("initial-only", "diverged", "does not carry", id="excludes-initial-pull"),
        pytest.param("no-op", "pushed-externally", "carries 1 of this run's 2", id="keeps-work-through-no-op-pull"),
        pytest.param("advance", "pushed-externally", "carries 1 of this run's 2", id="keeps-work-through-fast-forward"),
        pytest.param("interrupted", "unknown", "", id="interrupted-pull-is-unverified"),
        pytest.param("missing", "unknown", "", id="no-import-evidence-is-unverified"),
    ],
)
def test_report_uses_only_proven_pull_exclusions(
    compared_branch: tuple[Path, str, Path], scenario: str, expected: str, detail: str
) -> None:
    """An exact initial import is excluded, and a later pull cannot erase earlier work from the count."""
    repo, start, sentinels = compared_branch
    initial = _empty_commit(repo, "initial remote change")
    authored = _empty_commit(repo, "first run fix")
    advance = _empty_commit(repo, "later remote change") if scenario == "advance" else authored
    observed = initial if scenario == "initial-only" else advance
    _git(repo, "push", "-q", "origin", f"{observed}:refs/heads/feature")
    _empty_commit(repo, "last run fix")
    history = f"pull-start {start} {start}\npull-end {start} {initial}\nbase {initial} {start}\n"
    extra = {
        "initial-only": "",
        "no-op": f"pull-start {authored} {authored}\npull-end {authored} {authored}\n",
        "advance": f"pull-start {authored} {authored}\npull-end {authored} {advance}\n",
        "interrupted": f"pull-start {authored} {authored}\n",
        "missing": "",
    }
    write_lf(
        sentinels / f"resolve-remote-imports-{_SESSION}", "" if scenario == "missing" else history + extra[scenario]
    )

    result = _run_report(repo, sentinels)

    assert result.returncode == 0, result.stderr
    assert f"REMOTE_STATE={expected}" in result.stdout.splitlines()
    assert detail in next(line for line in result.stdout.splitlines() if line.startswith("REMOTE_DETAIL="))
