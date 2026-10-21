"""Pin the team-protocol guard every develop team mode runs before it creates a run dir or spawns a teammate.

Every ``--team`` spawn prompt tells teammates to read ``~/.claude/TEAM_PROTOCOL.md``, which ``/foundry:setup`` writes.
Without that file the teammates follow a protocol they cannot read, so each team mode stops with one shared ``!
BLOCKED`` line naming the fix instead of offering a silent solo fallback (feature) or no guard at all (debug, fix,
refactor).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_SKILLS = Path(__file__).resolve().parents[1] / "skills"
_BASH = shutil.which("bash")
_GUARD = (
    '[ -f ~/.claude/TEAM_PROTOCOL.md ] || { echo "! BLOCKED — --team needs ~/.claude/TEAM_PROTOCOL.md, written by'
    ' /foundry:setup (requires foundry plugin); run /foundry:setup, or re-run without --team"; exit 1; }'
)

_skip_no_bash = pytest.mark.skipif(_BASH is None, reason="bash unavailable")

#: Each team site and the first command that creates its run dir; the guard must run before it.
_SITES = [
    pytest.param("feature/modes/team-mode.md", "dev_setup_worktree_wrap.py", id="feature"),
    pytest.param("fix/modes/team-mode.md", "dev_setup_worktree_wrap.py", id="fix"),
    pytest.param("refactor/modes/team-mode.md", "dev_setup_worktree_wrap.py", id="refactor"),
    pytest.param("debug/SKILL.md", 'mkdir -p ".temp/develop/$TS"', id="debug"),
]


def _read(relative: str) -> str:
    return (_SKILLS / relative).read_text(encoding="utf-8")


@pytest.mark.parametrize(("relative", "run_dir_command"), _SITES)
def test_guard_runs_before_the_run_dir_is_created(relative: str, run_dir_command: str) -> None:
    """Each team site carries the identical guard line ahead of its run-dir creation.

    A guard placed after the run dir would leave an orphaned run dir behind every blocked run, and a reworded copy would
    drift from the message the other team modes print.
    """
    text = _read(relative)
    assert _GUARD in text
    assert text.index(_GUARD) < text.index(run_dir_command)


@pytest.mark.parametrize(("relative", "run_dir_command"), _SITES)
def test_no_home_variable_in_team_text(relative: str, run_dir_command: str) -> None:
    """Team text names the protocol file as ``~/.claude/...``, never ``${HOME}``, which never expands in a prompt."""
    assert "${HOME}" not in _read(relative)


def test_feature_no_longer_falls_back_to_solo() -> None:
    """Feature team mode stops on a missing protocol file instead of asking to continue solo.

    The former question let a ``--team`` run silently become a solo run, unlike every sibling team mode.
    """
    text = _read("feature/modes/team-mode.md")
    assert "Continue solo" not in text
    assert "TEAM_PROTOCOL_ABSENT" not in text


@pytest.mark.parametrize(
    "relative",
    [
        pytest.param("feature/modes/team-mode.md", id="feature"),
        pytest.param("fix/modes/team-mode.md", id="fix"),
        pytest.param("refactor/modes/team-mode.md", id="refactor"),
        pytest.param("debug/SKILL.md", id="debug"),
    ],
)
def test_wake_up_names_the_deadline_helper(relative: str) -> None:
    """Each team mode resumes on notifications and runs the ``agent_watch.py`` check, never a synchronous wait."""
    text = _read(relative)
    assert "end the turn" in text
    assert re.search(r"check block[^.\n]*`agent_watch\.py`", text)


@_skip_no_bash
class TestGuardBehaviour:
    """The guard line itself, run in bash against a scratch home directory."""

    def _run(self, home: Path) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "HOME": home.as_posix()}
        return subprocess.run([_BASH, "-c", _GUARD], capture_output=True, encoding="utf-8", env=env, check=False)

    def test_missing_protocol_blocks_with_the_setup_fix(self, tmp_path: Path) -> None:
        """No protocol file: exit 1 and a ``! BLOCKED`` line that names ``/foundry:setup`` and the solo re-run."""
        result = self._run(tmp_path)
        assert result.returncode == 1
        assert result.stdout.startswith("! BLOCKED")
        assert "/foundry:setup" in result.stdout
        assert "re-run without --team" in result.stdout

    def test_present_protocol_passes_silently(self, tmp_path: Path) -> None:
        """Protocol file present: exit 0, nothing printed, so the run-dir commands after it run as before."""
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "TEAM_PROTOCOL.md").write_text("protocol\n", encoding="utf-8")
        result = self._run(tmp_path)
        assert (result.returncode, result.stdout) == (0, "")
