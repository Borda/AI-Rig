"""Guard Phase 4: targeted tests inside the resolve fix loops, the full suite only at the Step 9 gate.

Measured resolve runs spent ~10 minutes per full-suite run, several times per run, because every implementation and QA
agent ran the whole suite after each fix. These tests pin the prompts and the gate that stop that.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_RESOLVE = Path(__file__).resolve().parents[2] / "skills/resolve"
_BASH = shutil.which("bash")


def _read(relative: str) -> str:
    """Read one resolve skill file."""
    return (_RESOLVE / relative).read_text(encoding="utf-8")


def _bash_path(path: Path) -> str:
    """Return the fixture path in Git Bash syntax on native Windows."""
    if sys.platform != "win32":
        return str(path)
    return subprocess.check_output(["cygpath", "-u", str(path)], text=True).strip()


@pytest.mark.parametrize(
    ("relative", "anchor", "rule"),
    [
        pytest.param(
            "modes/action-item-dispatch.md",
            "Per group, mark its items'",
            'resolve_test_plan.py\\" targeted --files',
            id="specialist-targeted",
        ),
        pytest.param(
            "modes/action-item-dispatch.md",
            "Per group, mark its items'",
            "Never run the\nwhole test suite, not even once",
            id="specialist-no-full-suite",
        ),
        pytest.param(
            "modes/action-item-dispatch.md",
            "**C1 —",
            "Never run the whole test suite — the caller runs it at the final gate.",
            id="bridge-no-full-suite",
        ),
        pytest.param(
            "modes/action-item-dispatch.md",
            "two-part challenge",
            "Read-only: run no tests.",
            id="challenge-runs-no-tests",
        ),
        pytest.param(
            "modes/lint-qa-gate.md",
            "foundry:qa-specialist",
            'resolve_test_plan.py\\" targeted --base $BASE_REF_MERGE',
            id="qa-targeted",
        ),
    ],
)
def test_fix_loop_prompts_run_targeted_tests_only(relative: str, anchor: str, rule: str) -> None:
    """Every agent working inside a fix loop is told to run targeted tests and never the full suite."""
    text = _read(relative)
    assert rule in text[text.index(anchor) :]


def test_qa_prompt_lost_its_full_suite_escape() -> None:
    """The old "unless CHANGE_SCOPE=full" escape let every QA iteration rerun the whole suite."""
    assert "do not run the full test suite unless $CHANGE_SCOPE=full" not in _read("modes/lint-qa-gate.md")


def test_gate_runs_full_suite_with_repo_command_and_reruns_after_fixes() -> None:
    """The gate runs the repository's own command in the background, and reruns it after any fix, within the cap.

    A fix verified only by targeted tests can break something elsewhere, so a green full suite is the last evidence.
    """
    gate = _read("modes/lint-qa-gate.md")
    section = gate[gate.index("**Full suite — once after the QA loop is clean") :]
    assert "full-command --script" in section
    assert "run_in_background: true" in section
    assert "**rerun the full suite**" in section
    assert "the gate-loop verification for a full-suite failure is always a full-suite rerun" in section
    assert "never the verification" in section
    assert "A green full-suite run must be the last test evidence before Step 10." in section
    assert "the full suite is not repeated" not in section
    assert "Skip it when `$CHANGE_SCOPE=lint-only`" in section


@pytest.mark.skipif(_BASH is None, reason="the gate block is Bash")
def test_full_suite_block_records_exit_code(tmp_path: Path) -> None:
    """The run block keeps the log and the exit code on disk, so a failing suite is never mistaken for a pass."""
    gate = _read("modes/lint-qa-gate.md")
    marker = gate.index('bash "$IMPL_DIR/full-suite.sh"')
    start = gate.rindex("```bash", 0, marker) + len("```bash\n")
    block = gate[start : gate.index("```", start)]
    session = "resolve-full-suite"
    impl_dir = tmp_path / "impl"
    impl_dir.mkdir()
    (impl_dir / "full-suite.sh").write_text("echo suite-ran\nexit 3\n", encoding="utf-8", newline="\n")
    (tmp_path / f"resolve-impl-dir-{session}").write_text(f"{_bash_path(impl_dir)}\n", encoding="utf-8", newline="\n")
    result = subprocess.run(
        [_BASH, "-c", block],
        cwd=tmp_path,
        env=os.environ | {"CLAUDE_CODE_SESSION_ID": session, "TMPDIR": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert ((impl_dir / "full-suite.rc").read_text(encoding="utf-8").strip(), "suite-ran" in result.stdout) == (
        "3",
        True,
    )
