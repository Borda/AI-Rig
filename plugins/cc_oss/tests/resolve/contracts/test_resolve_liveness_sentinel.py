"""Guard oss:resolve's liveness sentinel — written at Step 1, cleared on completion."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _resolve_source import read_resolve_source

_RESOLVE = Path(__file__).resolve().parents[3] / "skills/resolve"
_PLUGIN = _RESOLVE.parents[1]
_BASH = shutil.which("bash")
_SKILL = _RESOLVE / "SKILL.md"
_requires_bash = pytest.mark.skipif(_BASH is None, reason="Resolve Step 1 and completion blocks use Bash")


def _step_1_parse_block(skill: str) -> str:
    """Read the real Step 1 argument-parsing and sentinel-producer Bash block."""
    start = skill.index("```bash", skill.index("Parse $ARGUMENTS:")) + len("```bash\n")
    return skill[start : skill.index("```", start)]


def _completion_block(skill: str, *, first: bool) -> str:
    """Read the Bash block holding a completion path's ``rm -f`` of the compaction contract."""
    marker = "rm -f .temp/state/skill-contract.md"
    anchor = "The post-PR sentinel needs no cleanup"
    pos = skill.index(marker, skill.index(anchor)) if first else skill.rindex(marker)
    start = skill.rindex("```bash", 0, pos) + len("```bash\n")
    return skill[start : skill.index("```", start)]


def _env(tmp_path: Path, session: str, arguments: str) -> dict[str, str]:
    """Build the Step 1 environment: session id, plugin root, scratch TMPDIR and the test interpreter on PATH."""
    return os.environ | {
        "ARGUMENTS": arguments,
        "CLAUDE_CODE_SESSION_ID": session,
        "CLAUDE_PLUGIN_ROOT": str(_PLUGIN),
        "TMPDIR": str(tmp_path),
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
    }


def _run_bash(block: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Execute one shipped block in a fresh Bash, as a separate Bash tool call would."""
    return subprocess.run([_BASH, "-c", block], cwd=cwd, env=env, capture_output=True, text=True, check=False)


@_requires_bash
@pytest.mark.parametrize(
    ("arguments", "expected_sentinel"),
    [
        pytest.param("1542", "1542", id="pr-number"),
        pytest.param("report", "report", id="report-mode-no-pr"),
    ],
)
def test_step_1_writes_liveness_sentinel(tmp_path: Path, arguments: str, expected_sentinel: str) -> None:
    """Resolve publishes a same-session liveness signal that oss:review's Step 7 can read.

    Regression for the incident where review's Step 7a gate had no reliable way to tell a running resolve apart from a
    crashed one — the fix reads this dedicated sentinel instead of the shared, unrelated compaction-contract file review
    itself clobbers.
    """
    session = "resolve-liveness-test"
    block = _step_1_parse_block(read_resolve_source(_SKILL))
    result = _run_bash(block, tmp_path, _env(tmp_path, session, arguments))
    assert result.returncode == 0, result.stderr
    assert (tmp_path / f"oss-resolve-active-{session}").read_text(encoding="utf-8").strip() == expected_sentinel


@_requires_bash
@pytest.mark.parametrize(
    "first",
    [
        pytest.param(True, id="step-11-pr-report-mode"),
        pytest.param(False, id="step-12-comment-dispatch"),
    ],
)
def test_completion_clears_liveness_sentinel(tmp_path: Path, first: bool) -> None:
    """Both completion paths clear the liveness sentinel, not just the compaction contract.

    A sentinel left behind after resolve finishes would make a later, unrelated review of the same PR number falsely
    believe resolve is still running and skip its own follow-up gate.
    """
    session = "resolve-completion-test"
    contract = tmp_path / ".temp/state/skill-contract.md"
    contract.parent.mkdir(parents=True)
    contract.write_text("- skill: oss:resolve · phase: x\n", encoding="utf-8", newline="\n")
    sentinel = tmp_path / f"oss-resolve-active-{session}"
    sentinel.write_text("1542\n", encoding="utf-8", newline="\n")
    block = _completion_block(read_resolve_source(_SKILL), first=first)
    env = os.environ | {"CLAUDE_CODE_SESSION_ID": session, "TMPDIR": str(tmp_path)}
    result = _run_bash(block, tmp_path, env)
    assert result.returncode == 0, result.stderr
    assert not sentinel.exists()
    assert not contract.exists()
