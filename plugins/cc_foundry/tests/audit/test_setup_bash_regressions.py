"""Exercise executable audit templates against permission drift and Bash false positives."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from _hook_env import _bash_runs_posix_script

_PLUGIN = Path(__file__).resolve().parents[2]
_TEMPLATES = _PLUGIN / "skills/audit/templates"
_SHELLS = [name for name in ("bash", "zsh") if shutil.which(name)]
_REQUIRES_SHELL = pytest.mark.skipif(
    not _bash_runs_posix_script() or shutil.which("jq") is None,
    reason="Executable audit snippets require a POSIX shell and jq",
)


def _section(filename: str, heading: str) -> list[str]:
    """Read the requested heading through the next heading of equal or higher depth."""
    lines = (_TEMPLATES / filename).read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(heading))
    level = len(lines[start]) - len(lines[start].lstrip("#"))
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if any(lines[index].startswith("#" * depth + " ") for depth in range(2, level + 1))
        ),
        len(lines),
    )
    return lines[start + 1 : end]


def _block(filename: str, heading: str) -> str:
    """Extract the first Bash block without depending on prose wrapping."""
    lines = _section(filename, heading)
    opening = next(index for index, line in enumerate(lines) if line.startswith("```") and line.lstrip("`") == "bash")
    fence = lines[opening].removesuffix("bash")
    start = opening + 1
    end = next(index for index in range(start, len(lines)) if lines[index] == fence)
    return "\n".join(lines[start:end])


def _env(tmp_path: Path) -> dict[str, str]:
    """Prepare private per-session audit state and preserve the host process environment."""
    state = tmp_path / "audit-state-template-regression"
    state.mkdir()
    (state / "local-mode").write_text("true\n", encoding="utf-8")
    return {
        **os.environ,
        "TMPDIR": str(tmp_path),
        "CLAUDE_CODE_SESSION_ID": "template-regression",
        "JQ_AVAILABLE": "true",
        "PATH": os.pathsep.join([str(Path(os.sys.executable).parent), os.environ.get("PATH", "")]),
    }


@pytest.mark.integration
@_REQUIRES_SHELL
@pytest.mark.parametrize("shell", _SHELLS)
def test_permissions_guide_reports_missing_and_orphaned_entries(tmp_path: Path, shell: str) -> None:
    """A runnable guide check reports both drift directions while accepting deny-list documentation."""
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text(
        '{"permissions":{"allow":["Read","Bash(python:*)"],"deny":["Bash(rm:*)"]}}', encoding="utf-8"
    )
    (claude / "permissions-guide.md").write_text(
        "| `Read` | allowed |\n| `Bash(rm:*)` | denied |\n| `Write` | obsolete |\n", encoding="utf-8"
    )
    result = subprocess.run(
        [shell, "-c", _block("checks-setup.md", "## Check 4 —")],
        cwd=tmp_path,
        env=_env(tmp_path),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout.splitlines() == [
        "⚠ MISSING from guide: Bash(python:*)",
        "⚠ ORPHANED in guide: Write",
    ]


@pytest.mark.integration
@_REQUIRES_SHELL
@pytest.mark.parametrize(
    ("tracked", "untracked", "flagged"),
    [
        pytest.param("plugins/demo/tests/test_demo.py\n", "", False, id="tracked-tests-only"),
        pytest.param("", "plugins/demo/tests/test_new.py\n", False, id="untracked-tests-only"),
        pytest.param("plugins/demo/bin/tool.py\n", "", True, id="tracked-product-change"),
        pytest.param("", "plugins/demo/bin/new.py\n", True, id="untracked-product-change"),
    ],
)
def test_version_freeze_counts_product_changes_only(
    tmp_path: Path, tracked: str, untracked: str, flagged: bool
) -> None:
    """Only tracked or untracked product changes require a version bump, using a read-only Git boundary."""
    manifest = tmp_path / "plugins/demo/.claude-plugin/plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"version":"1.0.0"}', encoding="utf-8")
    env = {**_env(tmp_path), "DIFF_PATHS": tracked, "NEW_PATHS": untracked}
    git_boundary = """git() {
    case "$1" in
        show) printf '%s\\n' '{"version":"1.0.0"}' ;;
        diff) printf '%s' "$DIFF_PATHS" ;;
        ls-files) printf '%s' "$NEW_PATHS" ;;
        *) return 2 ;;
    esac
}
"""
    result = subprocess.run(
        ["bash", "-c", git_boundary + _block("checks-setup.md", "## Check 39 —")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert ("C39-MEDIUM:" in result.stdout) is flagged


@pytest.mark.integration
@_REQUIRES_SHELL
@pytest.mark.parametrize(
    ("body", "flagged"),
    [
        pytest.param("resolver | head -1", False, id="resolver-does-not-consume-status"),
        pytest.param("_RC=$?; resolver | head -1", False, id="status-consumed-before-pipeline"),
        pytest.param("gate | tail -5; _RC=$?", True, id="same-line-status-capture"),
        pytest.param("gate | head -1\n_RC=$?", True, id="next-line-status-capture"),
        pytest.param("gate | tail -5 || exit 1", True, id="failure-gate"),
        pytest.param("gate | tail -5 && echo PASS", True, id="success-gate"),
        pytest.param("if gate | tail -5; then echo PASS; fi", True, id="conditional-gate"),
        pytest.param("while gate | tail -5; do break; done", True, id="while-gate"),
        pytest.param("until gate | tail -5; do break; done", True, id="until-gate"),
        pytest.param("gate | tail -5 \\\n  || exit 1", True, id="continued-failure-gate"),
        pytest.param("set -o pipefail\ngate | tail -5\n_RC=$?", False, id="pipefail-enforces-status"),
        pytest.param("set +o pipefail\ngate | tail -5\n_RC=$?", True, id="disabled-pipefail-does-not-protect"),
        pytest.param(
            "printf ready\n```\n````bash\ngate | tail -5\n_RC=$?\n````\n```text",
            True,
            id="longer-bash-fence-status-capture",
        ),
        pytest.param(
            "printf ready\n```\n````bash\ncat <<'EOF'\n```\nEOF\ngate | tail -5\n_RC=$?\n````\n```text",
            True,
            id="shorter-fence-in-heredoc",
        ),
        pytest.param("gate | head -1\necho done\n_RC=$?", False, id="status-belongs-to-other-command"),
        pytest.param("gate | head -1\n```\n```bash\n_RC=$?", False, id="fresh-shell-status-is-unrelated"),
    ],
)
def test_pipeline_status_check_requires_status_consumption(tmp_path: Path, body: str, flagged: bool) -> None:
    """Resolver pipelines remain silent while unprotected gate status captures become review candidates."""
    skill = tmp_path / "plugins/demo/skills/task/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(f"```bash\n{body}\n```\n", encoding="utf-8")
    result = subprocess.run(
        ["bash", "-c", _block("checks-skills-bash.md", "### 30a —")],
        cwd=tmp_path,
        env=_env(tmp_path),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert ("plugins/demo/skills/task/SKILL.md:" in result.stdout) is flagged


@pytest.mark.integration
@_REQUIRES_SHELL
@pytest.mark.parametrize(
    ("body", "hint"),
    [
        pytest.param("resolver | head -1", False, id="unused-status-is-silent"),
        pytest.param("gate | tail -5; _RC=$?", True, id="candidate-needs-review"),
    ],
)
def test_pipeline_review_hint_requires_candidate(tmp_path: Path, body: str, hint: bool) -> None:
    """The status review hint appears only when the scanner emits a candidate."""
    skill = tmp_path / "plugins/demo/skills/task/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(f"```bash\n{body}\n```\n", encoding="utf-8")
    result = subprocess.run(
        ["bash", "-c", _block("checks-skills-bash.md", "### 30a —")],
        cwd=tmp_path,
        env=_env(tmp_path),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert ("hint:" in result.stdout) is hint
    assert result.stderr == ""


@pytest.mark.integration
@_REQUIRES_SHELL
@pytest.mark.parametrize(
    ("filename", "body", "candidate"),
    [
        pytest.param("SKILL.md", 'python -c "print(1)"', False, id="bare-python-is-allow-listed"),
        pytest.param("modes/run.md", 'VALUE=$(python -c "print(1)")', True, id="mode-capture-needs-manifest-review"),
    ],
)
def test_python_check_collects_captures_in_child_markdown(
    tmp_path: Path, filename: str, body: str, candidate: bool
) -> None:
    """Bare inline Python is allowed while captured Python in modes reaches the manifest review gate."""
    skill = tmp_path / "plugins/demo/skills/task" / filename
    skill.parent.mkdir(parents=True)
    skill.write_text(f"```bash\n{body}\n```\n", encoding="utf-8")
    result = subprocess.run(
        ["bash", "-c", _block("checks-skills-bash.md", "### Sub-check 23a —")],
        cwd=tmp_path,
        env=_env(tmp_path),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert (f"plugins/demo/skills/task/{filename}:" in result.stdout) is candidate


@pytest.mark.integration
@_REQUIRES_SHELL
@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize("local", [True, False])
def test_agent_colors_use_selected_root_with_nested_agents(tmp_path: Path, shell: str, local: bool) -> None:
    """Agent color inventory follows the selected root and reaches nested agent files in either shell."""
    agent = tmp_path / ("plugins/demo/agents/nested/helper.md" if local else ".claude/agents/nested/helper.md")
    agent.parent.mkdir(parents=True)
    agent.write_text("---\ncolor: blue\n---\n", encoding="utf-8")
    excluded = agent.parents[2] / "worktrees/copy/agents/ignored.md"
    excluded.parent.mkdir(parents=True)
    excluded.write_text("---\ncolor: red\n---\n", encoding="utf-8")
    env = _env(tmp_path)
    (tmp_path / "audit-state-template-regression/local-mode").write_text(f"{str(local).lower()}\n", encoding="utf-8")
    result = subprocess.run(
        [shell, "-c", _block("checks-setup.md", "## Check 9 —")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout.splitlines() == ["helper: blue"]


@pytest.mark.integration
@_REQUIRES_SHELL
@pytest.mark.parametrize("color", ["blue", "unsupported"])
def test_all_plugin_agent_colors_use_foundry_statusline(tmp_path: Path, color: str) -> None:
    """Agents without their own statusline are checked against foundry's shared palette."""
    agent = tmp_path / "plugins/other/agents/helper.md"
    agent.parent.mkdir(parents=True)
    agent.write_text(f"---\ncolor: {color}\n---\n", encoding="utf-8")
    statusline = tmp_path / "plugins/cc_foundry/hooks/statusline.js"
    statusline.parent.mkdir(parents=True)
    statusline.write_text('const COLOR_MAP = {\n  blue: "ansi",\n};\n', encoding="utf-8")
    result = subprocess.run(
        ["bash", "-c", _block("checks-setup.md", "## Check 9 —")],
        cwd=tmp_path,
        env=_env(tmp_path),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert ("C9-MEDIUM:" in result.stdout) is (color == "unsupported")
    assert result.stderr == ""


@pytest.mark.integration
@_REQUIRES_SHELL
@pytest.mark.parametrize("local", [True, False])
def test_native_tool_candidates_use_selected_root(tmp_path: Path, local: bool) -> None:
    """Bash misuse candidates come from the selected local or installed tree."""
    skill = tmp_path / ("plugins/demo/skills/task/SKILL.md" if local else ".claude/skills/task/SKILL.md")
    skill.parent.mkdir(parents=True)
    skill.write_text("```bash\ncat input.txt\n```\n", encoding="utf-8")
    excluded = skill.parents[2] / "worktrees/copy/skills/task/SKILL.md"
    excluded.parent.mkdir(parents=True)
    excluded.write_text(
        "```bash\ncat ignored.txt\ngrep pattern ignored.txt\nfind ignored.txt -name '*.py'\n"
        "echo ignored > ignored.txt\nsed 's/a/b/' ignored.txt\n```\n",
        encoding="utf-8",
    )
    env = _env(tmp_path)
    (tmp_path / "audit-state-template-regression/local-mode").write_text(f"{str(local).lower()}\n", encoding="utf-8")
    result = subprocess.run(
        ["bash", "-c", _block("checks-skills-bash.md", "## Check 23 —")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert f"{skill.relative_to(tmp_path).as_posix()}:2:cat input.txt" in result.stdout
    assert "ignored.txt" not in result.stdout


@pytest.mark.integration
@_REQUIRES_SHELL
@pytest.mark.parametrize("heading", ["### Sub-check 23a —", "### 30a —"])
def test_bash_status_and_capture_checks_exclude_worktree_copies(tmp_path: Path, heading: str) -> None:
    """Captured Python and pipeline gates in a worktree copy stay outside the active scan."""
    copied = tmp_path / "plugins/demo/worktrees/copy/skills/task/SKILL.md"
    copied.parent.mkdir(parents=True)
    copied.write_text(
        '```bash\nVALUE=$(python -c "print(1)")\npython << "EOF"\nEOF\ngate | tail -5; _RC=$?\n```\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", "-c", _block("checks-skills-bash.md", heading)],
        cwd=tmp_path,
        env=_env(tmp_path),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert "worktrees/" not in result.stdout
    assert result.stderr == ""


@pytest.mark.integration
@_REQUIRES_SHELL
def test_security_example_reads_parser_sentinel_without_argument_environment(tmp_path: Path) -> None:
    """The documented acceptable assignment reads parser output rather than an absent ARGUMENTS variable."""
    line = next(
        line
        for line in _section("checks-security.md", "## Check 35 —")
        if line.startswith("- `") and "The named variable" in line
    )
    assignment = line.split("`", 2)[1].replace("<prefix>", "demo")
    env = _env(tmp_path)
    env.pop("ARGUMENTS", None)
    env["CSID"] = "template-regression"
    parser = subprocess.run(
        [
            os.sys.executable,
            str(_PLUGIN / "bin/parse-skill-flags.py"),
            "--flags",
            "reply",
            "--keep-state",
            "demo",
            "a target",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    result = subprocess.run(
        ["bash", "-c", assignment + '\nprintf "%s" "${CLEAN_ARGS:-${EXEC_ARGS:-}}"'],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert parser.returncode == 0, parser.stderr
    assert result.returncode == 0, result.stderr
    assert result.stdout == "a target"


@pytest.mark.integration
@_REQUIRES_SHELL
def test_inventory_is_returned_without_shared_temporary_files(tmp_path: Path) -> None:
    """Inventory comparison consumes sorted stdout without leaving a shared temporary file."""
    agents = tmp_path / ".claude/agents"
    agents.mkdir(parents=True)
    (agents / "zebra.md").write_text("An agent", encoding="utf-8")
    (agents / "alpha.md").write_text("An agent", encoding="utf-8")
    command = _block("checks-setup.md", "## Check 1 —")
    result = subprocess.run(
        ["bash", "-c", command],
        cwd=tmp_path,
        env=_env(tmp_path),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["alpha", "zebra"]
    assert not list(tmp_path.rglob("*_disk.txt"))
