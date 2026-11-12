"""Run shared audit snippets against isolated repository and installed configuration trees."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_CHECKS = Path(__file__).resolve().parents[2] / "skills/audit/templates/checks-shared.md"
_BASH = shutil.which("bash")
_SHELLS = [
    pytest.param("bash", marks=pytest.mark.skipif(_BASH is None, reason="Bash unavailable")),
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


def _run(block: str, shell: str, root: Path, local: bool, timeout: int = 15) -> subprocess.CompletedProcess[str]:
    """Run one fresh tool shell with only persisted audit state selecting the scan root."""
    state_base = root / "state"
    state = state_base / "audit-state-scope-regression"
    state.mkdir(parents=True, exist_ok=True)
    (state / "local-mode").write_text("true\n" if local else "false\n", encoding="utf-8", newline="\n")
    env = {**os.environ, "TMPDIR": state_base.as_posix(), "CLAUDE_CODE_SESSION_ID": "scope-regression"}
    env.pop("LOCAL_MODE", None)
    env.pop("_ROOT", None)
    # Binary stdin avoids argv truncation and Windows text-mode CRLF translation of shell source.
    result = subprocess.run(
        [shutil.which(shell), "-s"],
        input=(block + "\n").encode("utf-8"),
        cwd=root,
        env=env,
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    return subprocess.CompletedProcess(
        result.args, result.returncode, result.stdout.decode("utf-8"), result.stderr.decode("utf-8")
    )


_SCOPE_CHECKS = [
    ("## Check 13 —", 0, "selected-agent.md"),
    ("## Check 15 —", 0, "/Users/selected-user"),
    ("## Check 16 —", 0, "{root}/CLAUDE.md"),
    ("## Check 16 —", 1, "selected-agent.md"),
    ("## Check 18 —", 0, "selected-rule"),
    ("## Check 18 —", 1, "selected-rule.md"),
    ("## Check 18 —", 2, "selected-agent.md"),
    ("## Check 18 —", 3, "selected-rule.md"),
    ("## Check 25 —", 0, 'subagent_type="selected-agent"'),
    ("## Check 26 —", 0, "selected-symbol"),
    ("## Check 26 —", 1, "selected-command"),
    ("## Check 29 —", 0, "selected-agent.md"),
]


def _run_scope_checks(shell: str, root: Path, local: bool) -> dict[str, tuple[str, int]]:
    """Batch real snippets in fresh subshells, preserving individual output and exit diagnostics."""
    # Each snippet runs in a separate subshell of an untouched parent: state cannot leak between checks.
    # One executable startup replaces twelve; retain the existing per-snippet budget for the whole batch.
    script = "\n".join(
        f"printf '\\036%s\\n' '{heading}{index}'\n(\n{_bash_blocks(heading)[index]}\n)\nprintf '\\037%s\\n' \"$?\""
        for heading, index, _ in _SCOPE_CHECKS
    )
    result = _run(script, shell, root, local, timeout=15 * len(_SCOPE_CHECKS))
    assert result.returncode == 0, result
    assert result.stderr == "", result
    outputs: dict[str, tuple[str, int]] = {}
    for record in result.stdout.split("\x1e")[1:]:
        name, body = record.split("\n", 1)
        stdout, status = body.rsplit("\x1f", 1)
        outputs[name] = stdout, int(status)
    return outputs


@pytest.mark.integration
@pytest.mark.parametrize("shell", _SHELLS)
def test_shell_script_survives_argument_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shell: str) -> None:
    """Keep long UTF-8 scripts intact when the native-to-POSIX argument transport truncates oversized arguments."""
    run = subprocess.run

    def truncate_script_argument(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        """Model Windows argv truncation and text-mode stdin translation before real shell execution."""
        if "-c" in command:
            index = command.index("-c") + 1
            command = [*command[:index], command[index][:8191], *command[index + 1 :]]
        if isinstance(kwargs.get("input"), str):
            kwargs["input"] = kwargs["input"].replace("\n", "\r\n")
        return run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", truncate_script_argument)
    script = "#" + "x" * 10000 + "\n(\nprintf '%s\\n' 'complete — \\\\ tail'\n)"

    result = _run(script, shell, tmp_path, local=False)

    assert result.returncode == 0, result
    assert result.stderr == "", result
    assert result.stdout == "complete — \\\\ tail\n"


@pytest.mark.integration
@_TEXT_TOOLS
@pytest.mark.timeout(15 * len(_SCOPE_CHECKS) + 60)
@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize("local", [True, False])
def test_fresh_shell_scans_only_selected_tree(tmp_path: Path, shell: str, local: bool) -> None:
    """Persisted mode selects one tree, including nested agents with spaces, in each fresh subshell."""
    selected_root = tmp_path / ("plugins/demo" if local else ".claude")
    other_root = tmp_path / (".claude" if local else "plugins/demo")
    _write_tree(selected_root, "selected")
    _write_tree(other_root, "unselected")
    _write_tree(selected_root / "worktrees/copy", "excluded")
    _write_tree(selected_root / "node_modules/dependency", "excluded")
    _write_tree(selected_root / ".venv/package", "excluded")
    _write_tree(selected_root / "tests/fixtures", "excluded")

    results = _run_scope_checks(shell, tmp_path, local)

    assert len(results) == len(_SCOPE_CHECKS), results
    assert all(status in (0, 1) for _, status in results.values()), results
    assert all(
        expected.format(root=selected_root.relative_to(tmp_path).as_posix()) in results[f"{heading}{index}"][0]
        for heading, index, expected in _SCOPE_CHECKS
    ), results
    assert all("unselected" not in stdout and "excluded" not in stdout for stdout, _ in results.values()), results
    assert all(
        part not in stdout
        for stdout, _ in results.values()
        for part in ("worktrees/", "node_modules/", ".venv/", "tests/")
    ), results


@pytest.mark.integration
@_TEXT_TOOLS
@pytest.mark.skipif(_BASH is None, reason="Bash unavailable")
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
