"""Pin the compaction contract ``/develop:plan`` writes before each gate that waits on the user.

A gate left open for hours outlives the prompt cache. Plan used to write no contract at all, so a ``/compact`` during
such a wait lost the goal, the scope findings and the plan path. Each idle gate now refreshes the contract and carries
the ``/compact`` hint in its question text; the skill clears the contract when it ends. Every plan fence that reads the
namespace sentinel also refuses an empty namespace, which would otherwise point paths at the filesystem root and let the
resolution loop's iteration cap fail open.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[1]
_SKILL = _PLUGIN / "skills" / "plan" / "SKILL.md"
_BASH = shutil.which("bash")
_CSID = "plan-gate-test"
_NS_READ = 'IFS= read -r PLAN_NS < "${TMPDIR:-/tmp}/dev-plan-ns-current-${CSID}" 2>/dev/null || PLAN_NS=""'
_NS_GUARD = '[ -n "$PLAN_NS" ] || { echo "! BLOCKED — plan namespace sentinel missing; re-run Flag parsing"; exit 1; }'

_skip_no_bash = pytest.mark.skipif(_BASH is None, reason="bash unavailable")


def _text() -> str:
    return _SKILL.read_text(encoding="utf-8")


def _fences() -> list[str]:
    return re.findall(r"```bash\n(.*?)\n```", _text(), re.DOTALL)


def _gate_fence() -> str:
    return next(fence for fence in _fences() if '"develop:plan" "idle gate"' in fence)


def test_every_namespace_read_is_guarded() -> None:
    """Each fence that reads the plan namespace sentinel stops on an empty value right after reading it."""
    readers = [fence for fence in _fences() if _NS_READ in fence]
    assert readers
    assert all(_NS_READ + "\n" + _NS_GUARD in fence for fence in readers)


@pytest.mark.parametrize(
    "gate_opening",
    [
        pytest.param("At depth 0:", id="depth-cap"),
        pytest.param("**Breaking change gate**", id="breaking-change"),
        pytest.param("- **Blockers found**", id="challenger-blockers"),
        pytest.param("Before invoking `AskUserQuestion`, print the saved brief", id="final"),
    ],
)
def test_idle_gate_refreshes_the_contract_and_carries_the_hint(gate_opening: str) -> None:
    """Each idle gate runs the contract block and puts the ``/compact`` hint in its question text."""
    gate = next(line for line in _text().splitlines() if line.startswith(gate_opening))
    assert re.search(r"[Rr]un the Idle-gate contract block", gate)
    assert "`/compact` hint line" in gate


def test_hint_text_names_the_saved_state() -> None:
    """The hint line follows the shared wording and names the path the gate block prints."""
    assert "`` Long wait? `/compact` now — plan state saved at <STATE>, resume lossless. ``" in _text()


def test_contract_is_cleared_at_start_and_end() -> None:
    """A stale contract is cleared owner-safely at Flag parsing; plan's own contract is removed when the skill ends."""
    text = _text()
    assert 'bin/extract-keep-flag.py" dev-plan "$ARGUMENTS"' in text
    assert text.index("rm -f .temp/state/skill-contract.md") > text.index("## Step 5: Final output")


@_skip_no_bash
class TestIdleGateBlock:
    """The shipped idle-gate fence, run in bash against a scratch namespace."""

    def _run(self, tmp_path: Path, namespace: Path | None) -> subprocess.CompletedProcess[str]:
        sentinel = tmp_path / f"dev-plan-ns-current-{_CSID}"
        if namespace is not None:
            sentinel.write_text(f"{namespace.as_posix()}\n", encoding="utf-8", newline="\n")
        env = {
            **os.environ,
            "TMPDIR": tmp_path.as_posix(),
            "CLAUDE_CODE_SESSION_ID": _CSID,
            "CLAUDE_PLUGIN_ROOT": _PLUGIN.as_posix(),
            "PATH": os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", ""),
        }
        return subprocess.run(
            [_BASH, "-c", _gate_fence()], capture_output=True, encoding="utf-8", env=env, cwd=tmp_path, check=False
        )

    def test_before_step_two_points_at_the_scope_file(self, tmp_path: Path) -> None:
        """Before a plan file exists the contract names the goal and scope file, and the block prints that path."""
        namespace = tmp_path / "ns"
        namespace.mkdir()
        (namespace / "clean-args").write_text("add retry to loader \n", encoding="utf-8", newline="\n")
        result = self._run(tmp_path, namespace)
        contract = (tmp_path / ".temp" / "state" / "skill-contract.md").read_text(encoding="utf-8")
        assert result.returncode == 0, result.stderr
        assert f"STATE=.temp/develop/plan-scope-{_CSID}.md" in result.stdout
        assert "develop:plan" in contract
        assert "goal=add retry to loader" in contract
        assert "plan-file=not written yet" in contract

    def test_after_step_two_points_at_the_plan_file(self, tmp_path: Path) -> None:
        """Once Step 2 persisted the plan path, the contract and the printed state both name the plan file."""
        namespace = tmp_path / "ns"
        namespace.mkdir()
        (namespace / "clean-args").write_text("add retry \n", encoding="utf-8", newline="\n")
        (namespace / "plan-file").write_text(".plans/active/plan_add-retry.md\n", encoding="utf-8", newline="\n")
        result = self._run(tmp_path, namespace)
        contract = (tmp_path / ".temp" / "state" / "skill-contract.md").read_text(encoding="utf-8")
        assert "STATE=.plans/active/plan_add-retry.md" in result.stdout
        assert "plan-file=.plans/active/plan_add-retry.md" in contract

    def test_missing_namespace_blocks_without_writing(self, tmp_path: Path) -> None:
        """A lost namespace sentinel stops the block before any contract is written."""
        result = self._run(tmp_path, None)
        assert (result.returncode, "! BLOCKED" in result.stdout) == (1, True)
        assert not (tmp_path / ".temp" / "state" / "skill-contract.md").exists()
