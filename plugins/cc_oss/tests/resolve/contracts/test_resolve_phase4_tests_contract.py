"""Guard Phase 4: targeted tests inside the resolve fix loops, the full suite only at the Step 9 gate.

Measured resolve runs spent ~10 minutes per full-suite run, several times per run, because every implementation and QA
agent ran the whole suite after each fix. These tests pin the prompts and the gate that stop that.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from _text_io import bash_path

_RESOLVE = Path(__file__).resolve().parents[3] / "skills/resolve"
_BASH = shutil.which("bash")


def _read(relative: str) -> str:
    """Read one resolve skill file with whitespace collapsed, so assertions never depend on where prose wraps."""
    return " ".join((_RESOLVE / relative).read_text(encoding="utf-8").split())


class TestFixLoopPrompts:
    """Pin the fix-loop prompts to targeted tests only."""

    @pytest.mark.parametrize(
        ("relative", "anchor", "rule"),
        [
            pytest.param(
                "modes/dispatch-phase2.md",
                "Per group, mark its items'",
                'resolve_test_plan.py\\" targeted --files',
                id="specialist-targeted",
            ),
            pytest.param(
                "modes/dispatch-phase2.md",
                "Per group, mark its items'",
                "Never run the whole test suite, not even once",
                id="specialist-no-full-suite",
            ),
            pytest.param(
                "modes/dispatch-prelude.md",
                "**C1 —",
                "Never run the whole test suite — the caller runs it at the final gate.",
                id="bridge-no-full-suite",
            ),
            pytest.param(
                "modes/dispatch-phase1.md",
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
    def test_run_targeted_tests_only(self, relative: str, anchor: str, rule: str) -> None:
        """Every agent working inside a fix loop is told to run targeted tests and never the full suite.

        Each case pins the prompt of one agent role (specialist, bridge, challenge, QA) after its own anchor, so a
        prompt that regained a whole-suite run, or lost its targeted command, fails here.
        """
        text = _read(relative)
        assert rule in text[text.index(anchor) :]

    def test_qa_prompt_lost_its_full_suite_escape(self) -> None:
        """The QA prompt no longer carries the "unless CHANGE_SCOPE=full" escape.

        That escape let every QA iteration rerun the whole suite, which is the cost this phase removes.
        """
        assert "do not run the full test suite unless $CHANGE_SCOPE=full" not in _read("modes/lint-qa-gate.md")


class TestFullSuiteGate:
    """Pin the full-suite gate that runs the repo command and reruns after fixes."""

    def test_runs_repo_command_and_reruns_after_fixes(self) -> None:
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
    def test_run_block_records_exit_code(self, tmp_path: Path) -> None:
        """The run block keeps the log and the exit code on disk, so a failing suite is never mistaken for a pass.

        A stub ``full-suite.sh`` prints a marker and exits 3; the shipped block must run it, exit 0 itself and persist
        the exit code beside the log for the later gate-loop step.
        """
        gate = (_RESOLVE / "modes/lint-qa-gate.md").read_text(encoding="utf-8")
        marker = gate.index('bash -- "$IMPL_DIR/full-suite.sh"')
        start = gate.rindex("```bash", 0, marker) + len("```bash\n")
        block = gate[start : gate.index("```", start)]
        session = "resolve-full-suite"
        impl_dir = tmp_path / "impl"
        impl_dir.mkdir()
        (impl_dir / "full-suite.sh").write_text("echo suite-ran\nexit 3\n", encoding="utf-8", newline="\n")
        sentinel = tmp_path / f"resolve-impl-dir-{session}"
        sentinel.write_text(f"{bash_path(impl_dir)}\n", encoding="utf-8", newline="\n")
        env = os.environ | {"CLAUDE_CODE_SESSION_ID": session, "TMPDIR": str(tmp_path)}
        result = subprocess.run(
            [_BASH, "-c", block], cwd=tmp_path, env=env, capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr
        assert (impl_dir / "full-suite.rc").read_text(encoding="utf-8").strip() == "3"
        assert "suite-ran" in result.stdout
