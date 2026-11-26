"""Every resolve run directory names how far the run got: a stub from Step 9, then the final report."""

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
_SESSION = "resolve-report-stub-test"
_STUB_MARKER = "<!-- resolve-report-status: incomplete -->"
pytestmark = pytest.mark.skipif(_BASH is None or shutil.which("git") is None, reason="the shipped blocks are Bash")

#: Line unique to each shipped block the tests run.
_STEP_9_MARK = '--step "Step 9 — lint and QA gate started"'
_STEP_10_MARK = '--step "Step 10 — push step reached (push intent $PUSH_AUTH)"'
_STEP_11_MARK = '--step "Step 11 — final report being written (push status $PUSH_STATUS)"'
_STEP_11_STATUS = 'resolve_report_stub.py" status --run-dir "$_RUN_DIR"'


def _block_containing(relative: str, marker: str) -> str:
    """Return the fenced Bash block of a resolve mode file that contains ``marker``."""
    text = (_RESOLVE / relative).read_text(encoding="utf-8")
    position = text.index(marker)
    start = text.rindex("```bash\n", 0, position) + len("```bash\n")
    return text[start : text.index("```", position)]


def _env(tmp: Path) -> dict[str, str]:
    """Build a fresh-shell environment: session id, plugin root, scratch TMPDIR and the test interpreter on PATH."""
    return {
        **os.environ,
        "CLAUDE_CODE_SESSION_ID": _SESSION,
        "CLAUDE_PLUGIN_ROOT": str(_PLUGIN),
        "TMPDIR": str(tmp),
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }


def _run(block: str, cwd: Path, tmp: Path, *, suffix: str = "") -> subprocess.CompletedProcess[str]:
    """Execute one shipped block in a fresh Bash, as one Bash tool call would; ``suffix`` runs after it."""
    assert _BASH is not None
    script = render_shell(block, _PLUGIN, "oss") + suffix
    return subprocess.run(
        [_BASH, "-c", script],
        cwd=cwd,
        env=_env(tmp),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=60,
    )


def _sentinel(tmp: Path, name: str) -> Path:
    """Return the path of one session-scoped sentinel."""
    return tmp / f"{name}-{_SESSION}"


@pytest.fixture
def run_at_step_9(tmp_path: Path) -> tuple[Path, Path]:
    """Return ``(repo, tmp)``: a checkout and the sentinels a run holds when it enters the lint and QA gate."""
    repo, tmp, impl = tmp_path / "repo", tmp_path / "tmp", tmp_path / "impl"
    for directory in (repo, tmp, impl):
        directory.mkdir()
    env = _env(tmp)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, env=env, check=True)
    (repo / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "core.py"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=repo, env=env, check=True)
    values = {"resolve-base-ref": "main", "resolve-impl-dir": str(impl), "resolve-pr-number": "42"}
    values |= {"resolve-run-dir": "", "resolve-push-auth": "unset", "resolve-push-status": "skipped-by-user"}
    for name, value in values.items():
        # LF only: the blocks read sentinels with `IFS= read -r`, which keeps a Windows carriage return in the value
        write_lf(_sentinel(tmp, name), f"{value}\n" if value else "")
    return repo, tmp


def _report(repo: Path, tmp: Path) -> Path:
    """Return the report path the run recorded in its run-dir sentinel."""
    run_dir = _sentinel(tmp, "resolve-run-dir").read_text(encoding="utf-8").strip()
    assert run_dir, "Step 9 records the run dir for the later steps"
    return repo / run_dir / "resolve-report.md"


def test_writable_start_creates_the_stub(run_at_step_9: tuple[Path, Path]) -> None:
    """Step 9 writes an incomplete stub naming its step the moment it creates the run dir."""
    repo, tmp = run_at_step_9
    result = _run(_block_containing("modes/lint-qa-gate.md", _STEP_9_MARK), repo, tmp)
    assert result.returncode == 0, result.stdout + result.stderr
    lines = _report(repo, tmp).read_text(encoding="utf-8").splitlines()
    assert lines[0] == _STUB_MARKER
    assert "## Resolve Report — PR #42" in lines
    assert "**Last step reached**: Step 9 — lint and QA gate started" in lines
    assert "REPORT_STATE=incomplete" in result.stdout


def test_interrupted_run_leaves_an_incomplete_stub(run_at_step_9: tuple[Path, Path]) -> None:
    """A run killed right after Step 9 leaves a stub that says incomplete and names Step 9, never a missing report."""
    repo, tmp = run_at_step_9
    killed = _run(_block_containing("modes/lint-qa-gate.md", _STEP_9_MARK), repo, tmp, suffix="\nkill -9 $$\n")
    assert killed.returncode != 0, "the fixture kills the shell after the Step 9 block"
    report = _report(repo, tmp)
    text = report.read_text(encoding="utf-8")
    assert text.splitlines()[0] == _STUB_MARKER
    assert "**Status**: incomplete" in text
    assert "aborted" not in text, "the stub states how far the run got, never a cause it cannot know"
    status = _run(_block_containing("modes/final-report.md", _STEP_11_STATUS), repo, tmp)
    assert "REPORT_STATE=incomplete" in status.stdout


def test_push_step_updates_the_stub(run_at_step_9: tuple[Path, Path]) -> None:
    """Reaching Step 10 rewrites the stub's last step, so a run idle at the push question says so."""
    repo, tmp = run_at_step_9
    assert _run(_block_containing("modes/lint-qa-gate.md", _STEP_9_MARK), repo, tmp).returncode == 0
    _run(_block_containing("modes/push.md", _STEP_10_MARK), repo, tmp)
    text = _report(repo, tmp).read_text(encoding="utf-8")
    assert "**Last step reached**: Step 10 — push step reached (push intent unset)" in text.splitlines()


def test_graceful_exit_replaces_the_stub_with_the_final_report(run_at_step_9: tuple[Path, Path]) -> None:
    """Step 11 names the report path, the final report replaces the stub there, and no later mark overwrites it."""
    repo, tmp = run_at_step_9
    assert _run(_block_containing("modes/lint-qa-gate.md", _STEP_9_MARK), repo, tmp).returncode == 0
    step_11 = _run(_block_containing("modes/final-report.md", _STEP_11_MARK), repo, tmp)
    assert step_11.returncode == 0, step_11.stdout + step_11.stderr
    report = _report(repo, tmp)
    assert f"RESOLVE_REPORT={report.relative_to(repo).as_posix()}" in step_11.stdout
    assert "Step 11 — final report being written (push status skipped-by-user)" in report.read_text(encoding="utf-8")
    final = "## Resolve Report — PR #42\n\nAll items resolved.\n"
    report.write_text(final, encoding="utf-8", newline="\n")  # the Write tool's final write over the stub
    status = _run(_block_containing("modes/final-report.md", _STEP_11_STATUS), repo, tmp)
    assert "REPORT_STATE=final" in status.stdout
    _run(_block_containing("modes/push.md", _STEP_10_MARK), repo, tmp)
    assert report.read_text(encoding="utf-8") == final, "a resumed step never overwrites the final report"


def test_final_step_mints_the_run_dir_when_step_9_never_ran(run_at_step_9: tuple[Path, Path]) -> None:
    """A run that reaches Step 11 without a Step 9 run dir still gets one, holding the report path."""
    repo, tmp = run_at_step_9
    result = _run(_block_containing("modes/final-report.md", _STEP_11_MARK), repo, tmp)
    assert result.returncode == 0, result.stdout + result.stderr
    report = _report(repo, tmp)
    assert report.parent.parent == repo / ".reports/resolve"
    assert report.read_text(encoding="utf-8").splitlines()[0] == _STUB_MARKER


def test_final_report_rules_cover_every_state_the_helper_prints() -> None:
    """Each ``REPORT_STATE`` the stub helper can print has its own rule, and the Write over the stub reads it first.

    The Write tool refuses to overwrite a file not read in the session, and the report path holds the stub, so an
    instruction to Write without a Read fails on the first attempt.
    """
    from resolve_report_stub import ReportState

    lines = (_RESOLVE / "modes/final-report.md").read_text(encoding="utf-8").splitlines()
    rules = {line.split("`", 2)[1] for line in lines if line.startswith("- `") and line.count("`") >= 2}
    assert sorted(state.value for state in ReportState if state.value not in rules) == []
    [write] = [line for line in lines if line.startswith("**Write the final report to the printed `RESOLVE_REPORT`")]
    assert "Read the stub at `RESOLVE_REPORT` with the Read tool first, then Write over it" in write
    [incomplete] = [line for line in lines if line.startswith("- `incomplete`")]
    assert incomplete.index("Read") < incomplete.index("Write")


def test_each_run_starts_without_the_previous_run_dir() -> None:
    """Step 1 empties the run-dir sentinel, so a run that skips Step 9 cannot write over the last run's report."""
    preflight = (_RESOLVE / "modes/preflight.md").read_text(encoding="utf-8").splitlines()
    assert ': > "${TMPDIR:-/tmp}/resolve-run-dir-${CSID}"' in [line.split("  #", 1)[0] for line in preflight]
