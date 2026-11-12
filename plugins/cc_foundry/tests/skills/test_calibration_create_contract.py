"""Exercise calibration discovery and preserve honest measurement and content-creation contracts."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _hook_env import _bash_runs_posix_script, _symlinks_supported

_PLUGIN = Path(__file__).resolve().parents[2]
_SKILLS = _PLUGIN / "skills"
_BASH = pytest.mark.skipif(not _bash_runs_posix_script(), reason="POSIX Bash execution unavailable")


def _lines(relative: str, heading: str = "") -> list[str]:
    """Read a heading section without relying on prose wrapping."""
    lines = (_SKILLS / relative).read_text(encoding="utf-8").splitlines()
    if not heading:
        return lines
    start = next(index for index, line in enumerate(lines) if line.startswith(heading))
    end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("### Phase ")), len(lines))
    return lines[start:end]


def _block(relative: str, prefix: str) -> str:
    """Extract the entire Bash fence containing the specified command."""
    lines = _lines(relative)
    for start, line in enumerate(lines):
        if line.strip() != "```bash":
            continue
        end = next(index for index in range(start + 1, len(lines)) if lines[index].strip() == "```")
        block = [line.removeprefix("  ") for line in lines[start + 1 : end]]
        if any(line.strip().startswith(prefix) for line in block):
            return "\n".join(block)
    raise AssertionError(f"No Bash block containing {prefix!r} in {relative}")


@_BASH
def test_routing_rejects_unsubstituted_timestamp_before_creating_reports(tmp_path: Path) -> None:
    """An unresolved run identity must fail before any filesystem output is created."""
    script = _block("calibrate/templates/routing-pipeline-prompt.md", "RUN_DIR=")
    result = subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "BLOCKED" in result.stdout
    assert not (tmp_path / ".reports").exists()


@pytest.fixture
def routing_tree(tmp_path: Path) -> dict[str, str]:
    """Prepare local agents, live cache versions and an orphan in an isolated home."""
    local = tmp_path / "plugins/cc_example/agents"
    local.mkdir(parents=True)
    (local / "local.md").write_text("name: local\n", encoding="utf-8")
    versions = tmp_path / ".claude/plugins/cache/borda-ai-rig/example"
    for version in ("1.0.0", "2.0.0", "3.0.0"):
        root = versions / version
        (root / "agents").mkdir(parents=True)
        (root / "agents" / f"installed-{version}.md").write_text("name: installed\n", encoding="utf-8")
        (root / ".claude-plugin").mkdir()
        (root / ".claude-plugin/plugin.json").write_text('{"name":"example"}', encoding="utf-8")
    (versions / "3.0.0/.orphaned_at").touch()
    foundry = tmp_path / ".claude/plugins/cache/borda-ai-rig/foundry/1.0.0"
    (foundry / "bin").mkdir(parents=True)
    shutil.copy2(_PLUGIN / "bin/resolve_plugin_root.py", foundry / "bin/resolve_plugin_root.py")
    (foundry / ".claude-plugin").mkdir()
    (foundry / ".claude-plugin/plugin.json").write_text('{"name":"foundry"}', encoding="utf-8")
    state = tmp_path / "calibrate-state-contract"
    state.mkdir()
    (state / "local-mode").write_text("false\n", encoding="utf-8")
    return {
        **os.environ,
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "CLAUDE_PLUGIN_ROOT": str(_PLUGIN),
        "CLAUDE_CODE_SESSION_ID": "contract",
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), os.environ.get("PATH", "")]),
    }


@_BASH
@pytest.mark.integration
@pytest.mark.parametrize("local_mode", [True, False])
@pytest.mark.parametrize("plugin_root_set", [True, False])
def test_routing_uses_local_sources_only_in_local_mode(
    tmp_path: Path, routing_tree: dict[str, str], local_mode: bool, plugin_root_set: bool
) -> None:
    """Installed discovery excludes orphaned versions and local mode selects the source roster."""
    (tmp_path / "calibrate-state-contract/local-mode").write_text(f"{str(local_mode).lower()}\n", encoding="utf-8")
    env = {key: value for key, value in routing_tree.items() if plugin_root_set or key != "CLAUDE_PLUGIN_ROOT"}
    script = _block("calibrate/templates/routing-pipeline-prompt.md", "RUN_DIR=").replace("<TIMESTAMP>", "run")
    script = script.replace("${CLAUDE_PLUGIN_ROOT}", routing_tree["CLAUDE_PLUGIN_ROOT"])
    result = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10)
    roster = (tmp_path / ".reports/calibrate/run/routing/roster-files.txt").read_text(encoding="utf-8").splitlines()
    expected = (
        "plugins/cc_example/agents/local.md"
        if local_mode
        else str(tmp_path / ".claude/plugins/cache/borda-ai-rig/example/2.0.0/agents/installed-2.0.0.md")
    )
    assert result.returncode == 0, result.stderr
    assert roster == [expected]


@_BASH
@pytest.mark.integration
def test_routing_prefers_registered_install_over_newer_cache(tmp_path: Path, routing_tree: dict[str, str]) -> None:
    """A retained newer cache does not replace the active registered plugin roster."""
    selected = tmp_path / ".claude/plugins/cache/borda-ai-rig/example/1.0.0"
    registry = {"plugins": {"example@borda-ai-rig": [{"installPath": str(selected), "installedAt": "2026-01-01"}]}}
    (tmp_path / ".claude/plugins/installed_plugins.json").write_text(json.dumps(registry), encoding="utf-8")
    script = _block("calibrate/templates/routing-pipeline-prompt.md", "RUN_DIR=").replace("<TIMESTAMP>", "run")
    result = subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, env=routing_tree, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    # The script lists `<recorded install path>/agents/<file>`: the root verbatim, then a "/" from `find`.
    expected = f"{selected}/agents/installed-1.0.0.md"
    roster = (tmp_path / ".reports/calibrate/run/routing/roster-files.txt").read_text(encoding="utf-8").splitlines()
    assert roster == [expected]


def test_rule_trigger_fidelity_is_reported_unmeasured() -> None:
    """Injected instructions cannot produce numeric host path-loading measurements."""
    lines = _lines("calibrate/templates/rules-pipeline-prompt.md")
    assert any("Trigger fidelity is not measured" in line for line in lines)
    assert not any('"type": "trigger"' in line for line in lines)
    assert any('"trigger_recall":null,"trigger_precision":null' in line for line in lines)
    assert any("Do not infer" in line and "glob" in line for line in lines)


def test_rule_run_identity_and_proposals_use_resolved_paths() -> None:
    """Same-basename rules stay isolated and proposals identify authored files rather than install links."""
    lines = _lines("calibrate/templates/rules-pipeline-prompt.md")
    proposals = _lines("calibrate/templates/rules-pipeline-prompt.md", "### Phase 5")
    assert any("RULE_DIR=" in line and "tier" in line and "plugin" in line for line in lines)
    assert any("RULE_PATH=" in line for line in lines)
    assert any("symlink" in line and "source" in line for line in proposals)
    assert any("**File**: `<RULE_SOURCE_PATH>`" in line for line in proposals)
    assert not any(line.startswith(">") and ".claude/rules/<RULE_BASENAME>" in line for line in proposals)


@_BASH
@pytest.mark.parametrize("filename", ["routing-pipeline-prompt.md", "rules-pipeline-prompt.md"])
@pytest.mark.parametrize(
    ("batch", "expected", "exit_code"),
    [
        pytest.param("<CHILD_BATCH>", "1", 0, id="unsubstituted-serial-default"),
        pytest.param("1", "1", 0, id="caller-serial-budget"),
        pytest.param("2", "2", 0, id="explicit-bounded-budget"),
        pytest.param("6", "! BLOCKED — child batch must be 1–5\n", 1, id="over-ceiling-rejected"),
    ],
)
def test_calibration_children_obey_shared_tier_budgets(
    tmp_path: Path, filename: str, batch: str, expected: str, exit_code: int
) -> None:
    """Every pipeline has a usable serial default and respects an explicit bounded child budget."""
    script = _block(f"calibrate/templates/{filename}", "CHILD_BATCH=").replace("<CHILD_BATCH>", batch)
    result = subprocess.run(
        ["bash", "-c", script + '\nprintf "%s" "$CHILD_BATCH"'],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == exit_code
    assert result.stdout == expected


def test_fast_routing_has_explicit_constraint_precedence_and_agent_pairs() -> None:
    """Five problems have a feasible hard budget and examples use dispatchable roster agents."""
    lines = _lines("calibrate/templates/routing-pipeline-prompt.md", "### Phase 2")
    assert any("precedence" in line and "exactly" in line and "<N>" in line for line in lines)
    assert any("when" in line and "N" in line and "every agent" in line for line in lines)
    assert not any("oss:analyse" in line or "oss:review" in line or "oss:shepherd" in line for line in lines)


@_BASH
@pytest.mark.parametrize("slug", ["<slug>", "bad/name", "", "topic-name"])
def test_outline_listing_rejects_unresolved_or_invalid_slug(tmp_path: Path, slug: str) -> None:
    """Listing an outline uses a validated slug before interpolating the glob."""
    script = _block("create/SKILL.md", "SLUG=").replace("<slug>", slug)
    result = subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == (0 if slug == "topic-name" else 1)
    assert ("BLOCKED" in result.stdout) == (slug != "topic-name")


def test_create_can_read_existing_arc_and_watch_files() -> None:
    """Repeat runs read existing files before Write attempts to replace them."""
    lines = _lines("create/SKILL.md")
    assert "Read" in next(line for line in lines if line.startswith("allowed-tools:")).split(":", 1)[1].strip().split(
        ", "
    )
    assert any("Read" in line and "exists" in line and "<slug>-arc.md" in line for line in lines)
    assert any("Read" in line and "exists" in line and "agent-watch-creator.tsv" in line for line in lines)


def test_video_generation_declares_latest_tracking_and_executable_resume() -> None:
    """Network dependency and latest-version policy are disclosed before scaffold consent and manual resume."""
    lines = _lines("create/SKILL.md")
    assert any("deliberate latest-tracking" in line and "npm registry" in line for line in lines)
    assert any("npm --version" in line for line in lines)
    assert any("npx --version" in line for line in lines)
    assert any("npx hyperframes@latest init" in line for line in lines)
    assert any("**Resume lines**" in line and "standalone" in line and "substituted" in line for line in lines)
    assert not any("run `/foundry:create`'s scaffold block" in line for line in lines)
    assert any("generation gate" in line and "download" in line and "npm" in line for line in lines)


@_BASH
def test_routing_missing_resolver_stops_with_specific_diagnostic(tmp_path: Path, routing_tree: dict[str, str]) -> None:
    """A missing installed helper is distinguished from an empty agent roster."""
    routing_tree.pop("CLAUDE_PLUGIN_ROOT", None)
    (tmp_path / ".claude/plugins/cache/borda-ai-rig/foundry/1.0.0/bin/resolve_plugin_root.py").unlink()
    script = (
        _block("calibrate/templates/routing-pipeline-prompt.md", "RUN_DIR=")
        .replace("<TIMESTAMP>", "run")
        .replace("<MODE>", "fast")
    )
    result = subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, env=routing_tree, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 1
    assert "! BLOCKED — resolver unavailable" in result.stdout
    assert "can't open file" not in result.stderr
    record = json.loads((tmp_path / ".reports/calibrate/run/routing/result.jsonl").read_text(encoding="utf-8"))
    assert record == {
        "ts": "run",
        "target": "routing",
        "mode": "fast",
        "routing_accuracy": None,
        "confusion_rate": None,
        "hard_accuracy": None,
        "auto_invoke_accuracy": None,
        "problems": 0,
        "verdict": "incomplete",
        "confused_pairs": [],
        "gaps": ["resolver unavailable"],
    }


@_BASH
@pytest.mark.integration
def test_routing_reports_dropped_orphaned_registry_version(tmp_path: Path, routing_tree: dict[str, str]) -> None:
    """An orphaned active install is excluded with a diagnostic naming the discarded version."""
    selected = tmp_path / ".claude/plugins/cache/borda-ai-rig/example/3.0.0"
    registry = {"plugins": {"example@borda-ai-rig": [{"installPath": str(selected), "installedAt": "2026-01-01"}]}}
    (tmp_path / ".claude/plugins/installed_plugins.json").write_text(json.dumps(registry), encoding="utf-8")
    local = tmp_path / ".claude/agents"
    local.mkdir(parents=True)
    (local / "project.md").write_text("name: project\n", encoding="utf-8")
    script = _block("calibrate/templates/routing-pipeline-prompt.md", "RUN_DIR=").replace("<TIMESTAMP>", "run")
    result = subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, env=routing_tree, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0
    assert "orphaned" in result.stderr
    assert str(selected) in result.stderr
    assert (tmp_path / ".reports/calibrate/run/routing/roster-files.txt").read_text(encoding="utf-8") == (
        f"{(selected.parent / '2.0.0/agents/installed-2.0.0.md').as_posix()}\n.claude/agents/project.md\n"
    )


@_BASH
@pytest.mark.integration
@pytest.mark.parametrize("local_mode", [True, False])
def test_routing_loader_obeys_persisted_local_mode(
    tmp_path: Path, routing_tree: dict[str, str], local_mode: bool
) -> None:
    """Local template edits are used only when the caller persisted its local-mode sentinel."""
    install = tmp_path / ".claude/plugins/cache/borda-ai-rig/foundry/1.0.0"
    shutil.copy2(_PLUGIN / "bin/load_mode.py", install / "bin/load_mode.py")
    shutil.copy2(_PLUGIN / "bin/render_plugin_root.py", install / "bin/render_plugin_root.py")
    shutil.copy2(_PLUGIN / "bin/resolve_skill_subdir.py", install / "bin/resolve_skill_subdir.py")
    installed_template = install / "skills/calibrate/templates"
    installed_template.mkdir(parents=True)
    (installed_template / "routing-pipeline-prompt.md").write_text("installed\n", encoding="utf-8")
    source = tmp_path / "plugins/cc_foundry/skills/calibrate/templates"
    source.mkdir(parents=True)
    (source / "routing-pipeline-prompt.md").write_text("source\n", encoding="utf-8")
    (tmp_path / "calibrate-state-contract/local-mode").write_text(f"{str(local_mode).lower()}\n", encoding="utf-8")
    routing_tree["CLAUDE_PLUGIN_ROOT"] = str(install)
    script = _block("calibrate/modes/routing.md", 'python "')
    result = subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, env=routing_tree, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ("source\n" if local_mode else "installed\n")


@_BASH
@pytest.mark.parametrize("rule_path", ["<RULE_PATH>", ".claude/rules/example.md"])
def test_rule_proposal_guard_distinguishes_missing_substitution(tmp_path: Path, rule_path: str) -> None:
    """A caller omission blocks proposals while a substituted path proceeds to source resolution."""
    script = _block("calibrate/templates/rules-pipeline-prompt.md", "case '<RULE_PATH>'").replace(
        "<RULE_PATH>", rule_path
    )
    result = subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == (1 if rule_path == "<RULE_PATH>" else 0)
    assert ("! BLOCKED — RULE_PATH not substituted" in result.stdout) == (rule_path == "<RULE_PATH>")


@_BASH
@pytest.mark.skipif(not _symlinks_supported(), reason="symlink creation unavailable")
@pytest.mark.parametrize(
    ("rule_path", "plugin_name", "expected"),
    [
        pytest.param(".claude/rules/shared.md", "example", "project-shared", id="project-tier"),
        pytest.param(
            "plugins/cc_example/rules/shared.md", "example", "source-cc_example-shared", id="source-plugin-tier"
        ),
        pytest.param("user-rules/shared.md", "example", "user-example-shared", id="installed-plugin-tier"),
        pytest.param("user-rules/shared.md", "other", "user-other-shared", id="another-installed-plugin"),
    ],
)
def test_rule_run_directory_preserves_tier_and_installed_plugin(
    tmp_path: Path, routing_tree: dict[str, str], rule_path: str, plugin_name: str, expected: str
) -> None:
    """Identical rule basenames in installed, project and source tiers produce distinct directories."""
    root = tmp_path / ".claude/plugins/cache/borda-ai-rig" / plugin_name / "2.0.0"
    installed = root / "rules"
    installed.mkdir(parents=True)
    (root / ".claude-plugin").mkdir(exist_ok=True)
    (root / ".claude-plugin/plugin.json").write_text(json.dumps({"name": plugin_name}), encoding="utf-8")
    (installed / "shared.md").write_text("A rule.\n", encoding="utf-8")
    link = tmp_path / "user-rules/shared.md"
    link.parent.mkdir()
    link.symlink_to(installed / "shared.md")
    script = _block("calibrate/modes/rules.md", "RULE_PATH=").replace("<rule-file>", rule_path)
    result = subprocess.run(
        ["bash", "-c", script + '\nprintf "%s\\n%s" "$RULE_PATH" "$RULE_DIR"'],
        cwd=tmp_path,
        env=routing_tree,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [rule_path, expected]


@_BASH
@pytest.mark.parametrize("slug", ["<slug>", "bad/name", "", "topic-name"])
def test_video_listing_reuses_slug_guard(tmp_path: Path, slug: str) -> None:
    """Video-directory listing rejects invalid substitutions before using the glob."""
    (tmp_path / ".plans/content/topic-name-video-2").mkdir(parents=True)
    (tmp_path / ".plans/content/other-video").mkdir()
    script = _block("create/SKILL.md", "ls -d ").replace("<slug>", slug)
    result = subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == (0 if slug == "topic-name" else 1)
    assert ("BLOCKED" in result.stdout) == (slug != "topic-name")
    assert result.stdout == (
        ".plans/content/topic-name-video-2/\n"
        if slug == "topic-name"
        else "! BLOCKED — slug not substituted or invalid\n"
    )


@_BASH
@pytest.mark.parametrize("failure", ["invalid-path", "npx-failure"])
def test_video_resume_failure_keeps_parent_shell_running(tmp_path: Path, failure: str) -> None:
    """Pasted resume commands may fail without exiting the user's surrounding interactive shell."""
    video_dir = "elsewhere" if failure == "invalid-path" else ".plans/content/topic-video"
    script = (
        _block("create/SKILL.md", "VIDEO_DIR=").replace("<video-dir>", video_dir).replace("<resolution>", "landscape")
    )
    result = subprocess.run(
        ["bash", "-c", "npx() { return 19; }\n" + script + '\nprintf "parent-alive"'],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout.endswith("parent-alive")
    assert not (tmp_path / ".plans").exists()
