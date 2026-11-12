"""Exercise audit snippets against mode files, nested agents and freely named feedback."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from _hook_env import _bash_runs_posix_script

_AUDIT = Path(__file__).resolve().parents[2] / "skills" / "audit"


def _blocks(relative: str, heading: str) -> list[str]:
    """Read executable Bash fences from one Markdown section."""
    selected = False
    block: list[str] | None = None
    result = []
    for line in (_AUDIT / relative).read_text(encoding="utf-8").splitlines():
        if line.startswith(heading):
            selected = True
        elif selected and line.startswith("## "):
            break
        if not selected:
            continue
        if line == "```bash":
            block = []
        elif line.startswith("```") and block is not None:
            result.append("\n".join(block))
            block = None
        elif block is not None:
            block.append(line)
    return result


def _run(block: str, root: Path) -> subprocess.CompletedProcess[str]:
    """Run a shipped snippet in an isolated project with stable session state."""
    state = root / "audit-state-sweep"
    state.mkdir(exist_ok=True)
    (state / "local-mode").write_text("true\n", encoding="utf-8")
    return subprocess.run(
        ["bash", "-c", block],
        cwd=root,
        env={**os.environ, "TMPDIR": str(root), "CLAUDE_CODE_SESSION_ID": "sweep"},
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


@pytest.mark.skipif(not _bash_runs_posix_script(), reason="POSIX Bash is unavailable")
@pytest.mark.parametrize(
    "dispatch", ['Agent(subagent_type="general-purpose")', 'Agent(\n subagent_type="general-purpose"\n)']
)
@pytest.mark.parametrize("relative", ["modes/run.md", "templates/worker-prompt.md"])
def test_health_sweep_reports_dispatch_inside_mode_file(tmp_path: Path, dispatch: str, relative: str) -> None:
    """A child-only dispatch is attributed to its owner and cannot evade C35."""
    skill = tmp_path / "plugins" / "sample" / "skills" / "inspect"
    child = skill / relative
    child.parent.mkdir(parents=True)
    (skill / "SKILL.md").write_text(f"# Inspect\nRead {relative} and execute it.\n", encoding="utf-8")
    child.write_text(dispatch + "\n", encoding="utf-8")
    blocks = _blocks("templates/checks-skills-sweep.md", "## Check C35")
    _run(blocks[0], tmp_path)
    checked = _run(blocks[1], tmp_path)
    assert "C35a:" in checked.stdout
    assert "inspect/SKILL.md" in checked.stdout
    assert relative in checked.stdout


@pytest.mark.skipif(not _bash_runs_posix_script(), reason="POSIX Bash is unavailable")
def test_health_sweep_accepts_owner_protocol_for_child(tmp_path: Path) -> None:
    """The owner's shared protocol covers its child without repeated boilerplate."""
    skill = tmp_path / "plugins" / "sample" / "skills" / "inspect"
    (skill / "modes").mkdir(parents=True)
    (skill / "SKILL.md").write_text("Load agent-spawn-protocol.md\n", encoding="utf-8")
    (skill / "modes" / "run.md").write_text('Agent(subagent_type="general-purpose")\n', encoding="utf-8")
    blocks = _blocks("templates/checks-skills-sweep.md", "## Check C35")
    _run(blocks[0], tmp_path)
    checked = _run(blocks[1], tmp_path)
    assert "references agent-spawn-protocol.md" in checked.stdout
    assert "C35a:" not in checked.stdout


@pytest.mark.skipif(not _bash_runs_posix_script(), reason="POSIX Bash is unavailable")
def test_model_check_reads_nested_local_agents(tmp_path: Path) -> None:
    """Local model checks inspect nested plugin agents without requiring .claude agents."""
    agents = tmp_path / "plugins" / "sample" / "agents" / "nested"
    agents.mkdir(parents=True)
    (agents / "worker.md").write_text("---\nmodel: sonnet\n---\n", encoding="utf-8")
    checked = _run(_blocks("templates/checks-agents.md", "## Check 19")[0], tmp_path)
    assert checked.returncode == 0
    assert "worker" in checked.stdout
    assert "sonnet" in checked.stdout


@pytest.mark.skipif(not _bash_runs_posix_script(), reason="POSIX Bash is unavailable")
@pytest.mark.parametrize(
    ("command", "expected", "exitcode"),
    [
        pytest.param("command() { return 1; }", "unavailable", 0, id="missing-runner"),
        pytest.param("pre-commit() { printf 'invalid config'; return 7; }", "7", 7, id="invalid-config"),
        pytest.param("pre-commit() { return 0; }", "0", 0, id="valid-config"),
    ],
)
def test_precommit_handoff_persists_validation_outcome(
    tmp_path: Path, command: str, expected: str, exitcode: int
) -> None:
    """The consolidator can distinguish unavailable validation from a passing empty output."""
    (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    block = _blocks("SKILL.md", "## Step 1: Validate pre-commit configuration")[0]
    result = _run(command + "\n" + block, tmp_path)
    assert result.returncode == exitcode, result.stderr
    saved = (tmp_path / "audit-state-sweep/precommit-status.txt").read_text(encoding="utf-8")
    assert f"validation_status={expected}" in saved.splitlines()
