"""Run shared audit snippets against isolated repository and installed configuration trees."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from _hook_env import _bash_runs_posix_script

_CHECKS = Path(__file__).resolve().parents[2] / "skills/audit/templates/checks-shared.md"
_SHELLS = [
    pytest.param("bash", marks=pytest.mark.skipif(not _bash_runs_posix_script(), reason="POSIX Bash required")),
    pytest.param("zsh", marks=pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh unavailable")),
]
_TEXT_TOOLS = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("find", "grep", "awk", "sed", "sort", "wc", "tr", "basename")),
    reason="Shared audit snippets require POSIX text utilities",
)


def _bash_blocks(heading: str) -> list[str]:
    """Extract executable blocks from one Markdown section without depending on prose wrapping."""
    lines = _CHECKS.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(heading))
    blocks: list[str] = []
    block: list[str] | None = None
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        if line.startswith(("```bash", "````bash")):
            block = []
        elif line.startswith("```") and block is not None:
            blocks.append("\n".join(block))
            block = None
        elif block is not None:
            block.append(line)
    return blocks


def _write_tree(root: Path, label: str) -> None:
    """Create distinguishable violations in nested agents, rules, skills, and local context."""
    files = {
        f"agents/nested group/{label}-agent.md": (
            f"# {label}\n### {label}-heading\n/Users/{label}-user\n"
            f"## Example\n```text\n{label}-example\n```\n"
            f'Never switch to NumPy {label}\nsubagent_type="{label}-agent"\n'
            f"⚠ {label}-symbol\nrun `/{label}-command`\n.claude/rules/{label}-rule.md\n"
        ),
        f"rules/{label}-rule.md": f"# {label}-rule\n",
        f"skills/{label}/SKILL.md": f"# {label}-skill\n### {label}-skill-heading\n",
        "CLAUDE.md": f"# {label}-context\n",
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _run(block: str, shell: str, root: Path, local: bool) -> subprocess.CompletedProcess[str]:
    """Run one fresh tool shell with only persisted audit state selecting the scan root."""
    state_base = root / "state"
    state = state_base / "audit-state-scope-regression"
    state.mkdir(parents=True, exist_ok=True)
    (state / "local-mode").write_text("true\n" if local else "false\n", encoding="utf-8")
    env = {**os.environ, "TMPDIR": state_base.as_posix(), "CLAUDE_CODE_SESSION_ID": "scope-regression"}
    env.pop("LOCAL_MODE", None)
    env.pop("_ROOT", None)
    return subprocess.run(
        [shell, "-c", block], cwd=root, env=env, capture_output=True, encoding="utf-8", check=False, timeout=15
    )


@pytest.mark.integration
@_TEXT_TOOLS
@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize("local", [True, False])
@pytest.mark.parametrize(
    ("heading", "block_index", "expected"),
    [
        pytest.param("## Check 13 —", 0, "selected-agent.md", id="heading-jump-in-nested-agent"),
        pytest.param("## Check 15 —", 0, "/Users/selected-user", id="hardcoded-user-path"),
        pytest.param("## Check 16 —", 0, "{root}/CLAUDE.md", id="context-file-in-selected-tree"),
        pytest.param("## Check 16 —", 1, "selected-agent.md", id="examples-in-nested-agent"),
        pytest.param("## Check 18 —", 0, "selected-rule", id="rule-inventory"),
        pytest.param("## Check 18 —", 1, "selected-rule.md", id="rule-frontmatter"),
        pytest.param("## Check 18 —", 2, "selected-agent.md", id="rule-redundancy-in-nested-agent"),
        pytest.param("## Check 18 —", 3, "selected-rule.md", id="rule-reference-in-nested-agent"),
        pytest.param("## Check 25 —", 0, 'subagent_type="selected-agent"', id="implicit-dispatch-in-nested-agent"),
        pytest.param("## Check 26 —", 0, "selected-symbol", id="symbols-in-nested-agent"),
        pytest.param("## Check 26 —", 1, "selected-command", id="slash-commands-in-nested-agent"),
        pytest.param("## Check 29 —", 0, "selected-agent.md", id="context-minimality-targets"),
    ],
)
def test_fresh_shell_scans_only_selected_tree(
    tmp_path: Path, shell: str, local: bool, heading: str, block_index: int, expected: str
) -> None:
    """Persisted mode selects one tree, including nested agents with spaces, in each fresh shell."""
    selected_root = tmp_path / ("plugins/demo" if local else ".claude")
    other_root = tmp_path / (".claude" if local else "plugins/demo")
    _write_tree(selected_root, "selected")
    _write_tree(other_root, "unselected")
    _write_tree(selected_root / "worktrees/copy", "excluded")
    _write_tree(selected_root / "node_modules/dependency", "excluded")
    _write_tree(selected_root / ".venv/package", "excluded")
    _write_tree(selected_root / "tests/fixtures", "excluded")

    result = _run(_bash_blocks(heading)[block_index], shell, tmp_path, local)

    assert result.returncode in (0, 1)
    assert result.stderr == ""
    assert expected.format(root=selected_root.relative_to(tmp_path).as_posix()) in result.stdout
    assert "unselected" not in result.stdout
    assert "excluded" not in result.stdout
    assert not any(part in result.stdout for part in ("worktrees/", "node_modules/", ".venv/", "tests/"))


@pytest.mark.integration
@_TEXT_TOOLS
@pytest.mark.parametrize("local", [True, False])
@pytest.mark.parametrize("settings", ["settings.json", "settings.local.json"])
def test_absolute_hook_paths_include_project_settings(tmp_path: Path, local: bool, settings: str) -> None:
    """Project hook paths are checked even when the selected source tree is plugins."""
    config = tmp_path / ".claude" / settings
    config.parent.mkdir()
    config.write_text('{"hooks":{"command":"/Users/example/hook.js"}}\n', encoding="utf-8")

    result = _run(_bash_blocks("## Check 15 —")[0], "bash", tmp_path, local)

    assert result.stdout.splitlines() == [f'.claude/{settings}:1:{{"hooks":{{"command":"/Users/example/hook.js"}}}}']
    assert result.stderr == ""


@pytest.mark.integration
@_TEXT_TOOLS
@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize("block_index", [0, 1])
def test_absent_rules_do_not_abort_shell(tmp_path: Path, shell: str, block_index: int) -> None:
    """An installed configuration without rules produces no shell glob or missing-file diagnostic."""
    (tmp_path / ".claude").mkdir()

    result = _run(_bash_blocks("## Check 18 —")[block_index], shell, tmp_path, False)

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout == ""


@pytest.mark.integration
@_TEXT_TOOLS
@pytest.mark.parametrize("shell", _SHELLS)
def test_policy_siblings_exclude_worktree_copies(tmp_path: Path, shell: str) -> None:
    """A broken sibling in the active tree is reported while a worktree copy is excluded."""
    active = tmp_path / ".claude/rules/active.md"
    copied = tmp_path / ".claude/worktrees/copy/rules/copied.md"
    active.parent.mkdir(parents=True)
    copied.parent.mkdir(parents=True)
    active.write_text("<!-- policy-sibling: rules/active-missing.md -->\n", encoding="utf-8")
    copied.write_text("<!-- policy-sibling: rules/copied-missing.md -->\n", encoding="utf-8")

    result = _run(_bash_blocks("## Check 45 —")[0], shell, tmp_path, False)

    assert result.returncode == 0
    assert result.stderr == ""
    assert "45-BROKEN: .claude/rules/active.md" in result.stdout
    assert "copied" not in result.stdout
