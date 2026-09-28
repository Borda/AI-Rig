"""Guard oss:resolve's liveness sentinel — written at Step 1, cleared on completion."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_RESOLVE = Path(__file__).resolve().parents[2] / "skills/resolve"
_PLUGIN = _RESOLVE.parents[1]
_BASH = shutil.which("bash")


def _bash_path(path: Path) -> str:
    """Return the fixture path in Git Bash syntax on native Windows."""
    if sys.platform != "win32":
        return str(path)
    return subprocess.check_output(["cygpath", "-u", str(path)], text=True).strip()


def _step_1_parse_block(skill: str) -> str:
    """Read the real Step 1 argument-parsing and sentinel-producer Bash block."""
    start = skill.index("```bash", skill.index("Parse $ARGUMENTS:")) + len("```bash\n")
    return skill[start : skill.index("```", start)]


def _env(tmp_path: Path, session: str, arguments: str) -> dict[str, str]:
    return os.environ | {
        "ARGUMENTS": arguments,
        "CLAUDE_CODE_SESSION_ID": session,
        "CLAUDE_PLUGIN_ROOT": str(_PLUGIN),
        "TMPDIR": str(tmp_path),
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
    }


@pytest.mark.skipif(_BASH is None, reason="Resolve Step 1 uses Bash")
@pytest.mark.parametrize(
    ("arguments", "expected_sentinel"),
    [
        pytest.param("1542", "1542", id="pr-number"),
        pytest.param("report", "report", id="report-mode-no-pr"),
    ],
)
def test_step_1_writes_liveness_sentinel(tmp_path: Path, arguments: str, expected_sentinel: str) -> None:
    """Resolve must publish a same-session liveness signal oss:review's Step 7 can read.

    Regression for the incident where review's Step 7a gate had no reliable way to tell a running resolve apart from a
    crashed one — the fix reads this dedicated sentinel instead of the shared, unrelated compaction-contract file review
    itself clobbers.
    """
    skill = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    block = _step_1_parse_block(skill)

    session = "resolve-liveness-test"
    result = subprocess.run(
        [_BASH, "-c", block],
        cwd=tmp_path,
        env=_env(tmp_path, session, arguments),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    sentinel = tmp_path / f"oss-resolve-active-{session}"
    assert sentinel.read_text(encoding="utf-8").strip() == expected_sentinel


@pytest.mark.skipif(_BASH is None, reason="Resolve completion uses Bash")
@pytest.mark.parametrize(
    "use_first",
    [
        pytest.param(True, id="step-11-pr-report-mode"),
        pytest.param(False, id="step-12-comment-dispatch"),
    ],
)
def test_completion_clears_liveness_sentinel(tmp_path: Path, use_first: bool) -> None:
    """Both completion paths must clear the liveness sentinel, not just the compaction contract.

    A sentinel left behind after resolve finishes would make a later, unrelated review of the same PR number falsely
    believe resolve is still running and skip its own follow-up gate.
    """
    dispatch = (_RESOLVE / "SKILL.md").read_text(encoding="utf-8")
    marker = "rm -f .temp/state/skill-contract.md"
    anchor = "The post-PR sentinel needs no cleanup"
    pos = dispatch.index(marker, dispatch.index(anchor)) if use_first else dispatch.rindex(marker)
    start = dispatch.rindex("```bash", 0, pos) + len("```bash\n")
    block = dispatch[start : dispatch.index("```", start)]

    session = "resolve-completion-test"
    (tmp_path / ".temp/state").mkdir(parents=True)
    (tmp_path / ".temp/state/skill-contract.md").write_text(
        "- skill: oss:resolve · phase: x\n", encoding="utf-8", newline="\n"
    )
    sentinel = tmp_path / f"oss-resolve-active-{session}"
    sentinel.write_text("1542\n", encoding="utf-8", newline="\n")

    result = subprocess.run(
        [_BASH, "-c", block],
        cwd=tmp_path,
        env=os.environ | {"CLAUDE_CODE_SESSION_ID": session, "TMPDIR": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not sentinel.exists()
    assert not (tmp_path / ".temp/state/skill-contract.md").exists()
