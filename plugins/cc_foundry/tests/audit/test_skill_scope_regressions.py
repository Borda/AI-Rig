"""Execute skill audit snippets against owner scope and child-file regressions."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from _hook_env import _bash_runs_posix_script

TEMPLATES = Path(__file__).resolve().parents[2] / "skills/audit/templates"
_BASH_REQUIRED = pytest.mark.skipif(
    not _bash_runs_posix_script() or any(shutil.which(tool) is None for tool in ("grep", "awk", "find", "sed")),
    reason="Executable skill checks require POSIX Bash and text utilities",
)


def _block(template: str, heading: str) -> str:
    """Read the first Bash block after a named heading without depending on prose wrapping."""
    lines = (TEMPLATES / template).read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(heading))
    fence = next(index for index in range(start, len(lines)) if lines[index] == "```bash")
    end = next(index for index in range(fence + 1, len(lines)) if lines[index] == "```")
    return "\n".join(lines[fence + 1 : end])


def _write(root: Path, relative: str, text: str) -> None:
    """Write an isolated fixture file with its parent directories."""
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _run(
    root: Path, template: str, heading: str, *, local: bool = True, shell: str = "bash"
) -> subprocess.CompletedProcess[str]:
    """Execute the shipped Bash block using an isolated session state."""
    _write(root, "audit-state-fixture/local-mode", "true\n" if local else "false\n")
    env = {**os.environ, "CLAUDE_CODE_SESSION_ID": "fixture", "TMPDIR": str(root)}
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    return subprocess.run(
        [shell, "-c", _block(template, heading)], cwd=root, env=env, capture_output=True, encoding="utf-8", check=False
    )


@pytest.mark.integration
@_BASH_REQUIRED
@pytest.mark.parametrize("skill_root", ["skills", "claude-skills"])
def test_tool_check_reads_child_file_under_owner(tmp_path: Path, skill_root: str) -> None:
    """A dispatch in a child mode is checked against its owning skill's permissions."""
    _write(tmp_path, "plugins/cc_demo/.claude-plugin/plugin.json", json.dumps({"name": "demo"}))
    _write(tmp_path, f"plugins/cc_demo/{skill_root}/sample/SKILL.md", "---\nallowed-tools: Bash\n---\n")
    _write(tmp_path, f"plugins/cc_demo/{skill_root}/sample/modes/run.md", 'Agent(subagent_type="demo:worker")\n')
    result = _run(tmp_path, "checks-skills-contract.md", "## Check 31 —")
    assert "body calls Agent()" in result.stdout
    assert f"plugins/cc_demo/{skill_root}/sample/SKILL.md" in result.stdout
    assert result.stderr == ""


@pytest.mark.integration
@_BASH_REQUIRED
@pytest.mark.parametrize("heading", ["## Check 31 —", "### Sub-check 31a", "## Check 38 —"])
def test_claude_checks_ignore_other_hosts(tmp_path: Path, heading: str) -> None:
    """Codex and OpenCode skills do not inherit Claude tool or frontmatter requirements."""
    _write(tmp_path, "plugins/other/.codex-plugin/plugin.json", json.dumps({"name": "other"}))
    _write(
        tmp_path,
        "plugins/other/skills/sample/SKILL.md",
        "---\nallowed-tools: Bash\n---\n```python\n" + "Agent()\nAskUserQuestion()\n" * 9 + "```\n",
    )
    result = _run(tmp_path, "checks-skills-contract.md", heading)
    assert "BREAKING" not in result.stdout
    assert "31a-INFO:" not in result.stdout
    assert "38-REVIEW:" not in result.stdout
    assert "plugins/other" not in result.stdout
    assert result.stderr == ""


@pytest.mark.integration
@_BASH_REQUIRED
@pytest.mark.parametrize(
    ("owner_protocol", "spawn", "flagged"),
    [
        pytest.param("", 'Agent(subagent_type="demo:worker")', True, id="unmonitored-mode"),
        pytest.param("agent-spawn-protocol.md", 'Agent(subagent_type="demo:worker")', False, id="owner-protocol"),
        pytest.param(
            "agent-resolution.md / Agent waits", 'Agent(subagent_type="demo:worker")', False, id="resolution-waits"
        ),
        pytest.param("", "Agent() is an example tool name", False, id="prose-only"),
        pytest.param("", 'Agent(\n  subagent_type="demo:worker"\n)', True, id="multiline-dispatch"),
        pytest.param("", 'Agent(description="work", subagent_type="demo:worker")', True, id="reordered-arguments"),
        pytest.param(
            "", 'Agent(description="audit (fast)", subagent_type="demo:worker")', True, id="parenthesized-description"
        ),
        pytest.param(
            "", 'Agent(\n description="work",\n subagent_type="demo:worker"\n)', True, id="multiline-reordered"
        ),
    ],
)
def test_health_check_uses_owner_and_real_dispatch(
    tmp_path: Path, owner_protocol: str, spawn: str, flagged: bool
) -> None:
    """Child dispatches require monitoring while owner references and prose examples remain valid."""
    _write(tmp_path, "plugins/cc_demo/.claude-plugin/plugin.json", json.dumps({"name": "demo"}))
    _write(tmp_path, "plugins/cc_demo/claude-skills/sample/SKILL.md", owner_protocol)
    _write(tmp_path, "plugins/cc_demo/claude-skills/sample/modes/run.md", spawn)
    result = _run(tmp_path, "checks-skills-gaps.md", "## Check 40 —")
    assert ("C40-HIGH" in result.stdout) is flagged
    assert result.stderr == ""


@pytest.mark.integration
@_BASH_REQUIRED
@pytest.mark.parametrize("consumer", [False, True])
@pytest.mark.parametrize("shell", [name for name in ("bash", "zsh") if shutil.which(name)])
def test_shared_file_cannot_consume_itself(tmp_path: Path, consumer: bool, shell: str) -> None:
    """A shared file naming itself cannot hide a missing external consumer."""
    _write(tmp_path, "plugins/cc_demo/.claude-plugin/plugin.json", json.dumps({"name": "demo"}))
    _write(
        tmp_path,
        "plugins/cc_demo/skills/_shared/dependency.md",
        "<!-- file: dependency.md — consumers: plugins/cc_demo/skills/_shared/dependency.md -->",
    )
    _write(tmp_path, "plugins/cc_demo/skills/sample/SKILL.md", "dependency.md" if consumer else "No dependency")
    result = _run(tmp_path, "checks-skills-crossplugin.md", "**Step 4 — Orphaned", shell=shell)
    assert ("unreferenced:" in result.stdout) is not consumer
    assert result.stderr == ""


@pytest.mark.integration
@_BASH_REQUIRED
@pytest.mark.parametrize(
    ("template", "heading", "diagnostic"),
    [
        pytest.param("checks-skills-contract.md", "## Check 31 —", "BREAKING", id="tool-permissions"),
        pytest.param("checks-skills-gaps.md", "## Check 40 —", "C40-", id="deadline-protocol"),
    ],
)
@pytest.mark.parametrize("child", ["templates/pipeline-prompt.md", "templates/checks-example.md"])
def test_child_prompt_and_check_templates_do_not_change_owner_contract(
    tmp_path: Path, template: str, heading: str, diagnostic: str, child: str
) -> None:
    """Spawn prompts and audit check examples do not execute under the owning skill's tools."""
    _write(tmp_path, "plugins/cc_demo/.claude-plugin/plugin.json", json.dumps({"name": "demo"}))
    _write(tmp_path, "plugins/cc_demo/skills/audit/SKILL.md", "---\nallowed-tools: Bash\n---\n")
    _write(
        tmp_path,
        f"plugins/cc_demo/skills/audit/{child}",
        'Skill("demo:task")\nAgent(subagent_type="demo:worker")\nhealth_sentinel\n',
    )
    result = _run(tmp_path, template, heading)
    assert diagnostic not in result.stdout
    assert result.stderr == ""


@pytest.mark.integration
@_BASH_REQUIRED
@pytest.mark.parametrize("source", ["SKILL.md", "modes/run.md"])
def test_permission_repair_requires_owner_body_call(tmp_path: Path, source: str) -> None:
    """A child-only permission candidate requires review rather than an automatic tool expansion."""
    _write(tmp_path, "plugins/cc_demo/.claude-plugin/plugin.json", json.dumps({"name": "demo"}))
    body = "---\nallowed-tools: Bash\n---\n"
    _write(tmp_path, "plugins/cc_demo/skills/sample/SKILL.md", body)
    _write(tmp_path, f"plugins/cc_demo/skills/sample/{source}", body + 'Agent(subagent_type="demo:worker")\n')
    result = _run(tmp_path, "checks-skills-contract.md", "## Check 31 —")
    assert "body calls Agent()" in result.stdout
    assert ("fix: add 'Agent'" in result.stdout) is (source == "SKILL.md")
    assert result.stderr == ""


@pytest.mark.integration
@_BASH_REQUIRED
@pytest.mark.parametrize(
    ("declaration", "manifest", "orphan"),
    [
        pytest.param("plugins/other/skills/task/SKILL.md", "", False, id="cross-plugin-consumers-header"),
        pytest.param(
            "",
            'MANIFEST = [{"canonical": "plugins/cc_demo/skills/_shared/dependency.md"}]',
            False,
            id="canonical-source",
        ),
        pytest.param(
            "",
            'MANIFEST = [{"canonical": "plugins/other/skills/_shared/dependency.md"}]',
            True,
            id="other-canonical-path",
        ),
        pytest.param(
            "",
            'MANIFEST = [{"copies": ["plugins/cc_demo/skills/_shared/dependency.md"]}]',
            True,
            id="copy-is-not-canonical",
        ),
    ],
)
def test_shared_reachability_accepts_external_headers_and_canonical_sources(
    tmp_path: Path, declaration: str, manifest: str, orphan: bool
) -> None:
    """Declared consumers and exact canonical entries preserve shared sources without self-reference."""
    _write(tmp_path, "plugins/cc_demo/.claude-plugin/plugin.json", json.dumps({"name": "demo"}))
    _write(
        tmp_path,
        "plugins/cc_demo/skills/_shared/dependency.md",
        f"<!-- file: dependency.md — consumers: {declaration} -->",
    )
    _write(tmp_path, "plugins/cc_foundry/bin/propagate_shared.py", manifest)
    result = _run(tmp_path, "checks-skills-crossplugin.md", "**Step 4 — Orphaned")
    assert ("unreferenced:" in result.stdout) is orphan
    assert result.stderr == ""


@pytest.mark.integration
@_BASH_REQUIRED
@pytest.mark.parametrize(
    ("body", "calls"),
    [
        pytest.param("---\nallowed-tools: AskUserQuestion\n---\n", 0, id="permission-declaration"),
        pytest.param("Use AskUserQuestion to group related choices.\n", 0, id="prose-mention"),
        pytest.param("`AskUserQuestion` is available.\n", 0, id="inline-tool-name"),
        pytest.param(
            "Use `name` before AskUserQuestion (the gate) and `other` after.\n", 0, id="prose-between-code-spans"
        ),
        pytest.param("```bash\n# Use AskUserQuestion (one per decision)\n```\n", 0, id="fenced-comment"),
        pytest.param("Call `AskUserQuestion(questions=[one])`.\n", 1, id="inline-call"),
        pytest.param("Call ``AskUserQuestion(questions=[one])``.\n", 1, id="longer-inline-delimiter"),
        pytest.param("```python\nAskUserQuestion(questions=[one])\n```\n", 1, id="fenced-call"),
    ],
)
def test_question_review_collects_only_call_forms(tmp_path: Path, body: str, calls: int) -> None:
    """Question review ignores declarations and prose while retaining inline and fenced calls."""
    _write(tmp_path, "plugins/cc_demo/.claude-plugin/plugin.json", json.dumps({"name": "demo"}))
    _write(tmp_path, "plugins/cc_demo/skills/sample/SKILL.md", body)
    result = _run(tmp_path, "checks-skills-contract.md", "## Check 38 —")
    assert result.stdout.count("38-REVIEW:") == calls
    assert result.stderr == ""


@pytest.mark.integration
@_BASH_REQUIRED
def test_shared_orphan_scan_skips_nonlocal_tree(tmp_path: Path) -> None:
    """A project plugin-looking tree is not audited when local mode is disabled."""
    _write(tmp_path, "plugins/cc_demo/skills/_shared/dependency.md", "unreferenced")
    result = _run(tmp_path, "checks-skills-crossplugin.md", "**Step 4 — Orphaned", local=False)
    assert "unreferenced:" not in result.stdout
    assert result.stderr == ""


@pytest.mark.integration
@_BASH_REQUIRED
def test_crossplugin_dispatch_scan_reads_mode(tmp_path: Path) -> None:
    """Cross-plugin dependencies inside mode files remain visible to fallback review."""
    _write(tmp_path, "plugins/cc_demo/.claude-plugin/plugin.json", json.dumps({"name": "demo"}))
    _write(tmp_path, "plugins/cc_demo/skills/sample/SKILL.md", "A skill")
    _write(tmp_path, "plugins/cc_demo/skills/sample/modes/run.md", 'Agent(subagent_type="foreign:worker")')
    result = _run(tmp_path, "checks-skills-crossplugin.md", "**Step 2 — Collect cross-plugin")
    assert 'modes/run.md:1:Agent(subagent_type="foreign:worker")' in result.stdout
    assert result.stderr == ""


@pytest.mark.integration
@_BASH_REQUIRED
def test_orphan_script_check_has_source_fallback(tmp_path: Path) -> None:
    """An unset runtime plugin root still invokes the source-tree orphan checker."""
    _write(tmp_path, "plugins/cc_foundry/.claude-plugin/plugin.json", '{"name":"foundry"}')
    _write(tmp_path, ".claude-plugin/marketplace.json", '{"name":"borda-ai-rig"}')
    resolver = TEMPLATES.parents[2] / "bin/resolve_plugin_root.py"
    _write(tmp_path, "plugins/cc_foundry/bin/resolve_plugin_root.py", resolver.read_text(encoding="utf-8"))
    _write(tmp_path, "plugins/cc_foundry/bin/check_orphaned_bin.py", 'print("checker invoked")\n')
    _write(tmp_path, "audit-state-fixture/local-mode", "true\n")
    script = _block("checks-skills-deadfiles.md", "### Sub-check 32d").replace(
        "${CLAUDE_PLUGIN_ROOT}", (tmp_path / "plugins/cc_foundry").as_posix()
    )
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env={
            **os.environ,
            "HOME": str(tmp_path / "home"),
            "USERPROFILE": str(tmp_path / "home"),
            "TMPDIR": str(tmp_path),
            "CLAUDE_CODE_SESSION_ID": "fixture",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "checker invoked" in result.stdout


@pytest.mark.integration
@_BASH_REQUIRED
def test_crossplugin_prose_scan_reads_mode(tmp_path: Path) -> None:
    """A child file's cross-plugin follow-up is attributed to its owning skill."""
    _write(tmp_path, "plugins/cc_demo/.claude-plugin/plugin.json", json.dumps({"name": "demo"}))
    _write(tmp_path, "plugins/cc_demo/skills/sample/SKILL.md", "A skill")
    _write(tmp_path, "plugins/cc_demo/skills/sample/modes/run.md", "Next: `/foreign:inspect`\n")
    result = _run(tmp_path, "checks-skills-crossplugin.md", "### Sub-check 28c")
    assert "28c:" in result.stdout
    assert "modes/run.md" in result.stdout
    assert "owner=plugins/cc_demo/skills/sample/SKILL.md" in result.stdout
    assert result.stderr == ""


@pytest.mark.integration
@_BASH_REQUIRED
def test_question_scan_does_not_invent_branch_cap(tmp_path: Path) -> None:
    """Nine distinct single-question gates remain review candidates rather than cap violations."""
    _write(tmp_path, "plugins/cc_demo/.claude-plugin/plugin.json", json.dumps({"name": "demo"}))
    _write(
        tmp_path,
        "plugins/cc_demo/skills/sample/SKILL.md",
        "```python\n" + "AskUserQuestion(questions=[one])\n" * 9 + "```\n",
    )
    result = _run(tmp_path, "checks-skills-contract.md", "## Check 38 —")
    assert "C38-HIGH" not in result.stdout
    assert "C38-MEDIUM" not in result.stdout
    assert result.stdout.count("38-REVIEW:") == 9


@pytest.mark.integration
@_BASH_REQUIRED
def test_omitted_effort_is_model_default_advice(tmp_path: Path) -> None:
    """Claude effort defaults are advice rather than a required auto-fix field."""
    _write(tmp_path, "plugins/cc_demo/.claude-plugin/plugin.json", json.dumps({"name": "demo"}))
    _write(tmp_path, "plugins/cc_demo/skills/sample/SKILL.md", "---\ndescription: A skill\n---\n")
    result = _run(tmp_path, "checks-skills-contract.md", "### Sub-check 31a")
    assert "31a-INFO:" in result.stdout
    assert "model/runtime default" in result.stdout
    assert "required; no default" not in result.stdout


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("zsh") is None, reason="Empty-tree zsh behavior requires a real zsh executable")
@pytest.mark.parametrize(
    ("template", "heading"),
    [
        pytest.param("checks-skills-contract.md", "## Check 31 —", id="tool-contract"),
        pytest.param("checks-skills-contract.md", "### Sub-check 31a", id="frontmatter"),
        pytest.param("checks-skills-contract.md", "## Check 38 —", id="question-limits"),
        pytest.param("checks-skills-gaps.md", "## Check 40 —", id="health-monitoring"),
        pytest.param("checks-skills-crossplugin.md", "**Step 1 — Map", id="dispatch-owners"),
        pytest.param("checks-skills-crossplugin.md", "**Step 2 — Collect", id="dispatch-sources"),
        pytest.param("checks-skills-crossplugin.md", "### Sub-check 28c", id="prose-sources"),
        pytest.param("checks-skills-crossplugin.md", "**Step 4 — Orphaned", id="shared-consumers"),
    ],
)
@pytest.mark.parametrize("plugin_file", ["empty.txt", "plugins/other/.codex-plugin/plugin.json"])
def test_empty_claude_scope_does_not_abort_zsh(tmp_path: Path, template: str, heading: str, plugin_file: str) -> None:
    """An empty or non-Claude-only plugin tree emits no zsh glob error."""
    _write(tmp_path, "audit-state-fixture/local-mode", "true\n")
    _write(tmp_path, plugin_file, json.dumps({"name": "other"}))
    result = subprocess.run(
        ["zsh", "-c", _block(template, heading)],
        cwd=tmp_path,
        env={**os.environ, "CLAUDE_CODE_SESSION_ID": "fixture", "TMPDIR": str(tmp_path)},
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    assert result.stderr == ""
    assert result.returncode == 0
