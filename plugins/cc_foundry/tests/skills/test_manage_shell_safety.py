"""Execute manage recipes against isolated hook registries, permissions, and schema caches."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from _hook_env import _bash_runs_posix_script, _symlinks_supported

_PLUGIN = Path(__file__).resolve().parents[2]
_BASH = pytest.mark.skipif(not _bash_runs_posix_script(), reason="POSIX bash unavailable")
_SYMLINKS = pytest.mark.skipif(not _symlinks_supported(), reason="Symlink creation unavailable")


def _blocks(relative: str, heading: str) -> list[str]:
    """Extract fenced shell recipes from one named mode, preserving their command order."""
    lines = (_PLUGIN / "skills/manage" / relative).read_text(encoding="utf-8").splitlines()
    start = lines.index(heading)
    end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("### ")), len(lines))
    blocks = []
    current: list[str] | None = None
    for line in lines[start:end]:
        if line.strip() == "```bash":
            current = []
        elif line.strip() == "```" and current is not None:
            blocks.append(textwrap.dedent("\n".join(current)))
            current = None
        elif current is not None:
            current.append(line)
    return blocks


def _run(script: str, root: Path, **env: str) -> subprocess.CompletedProcess[str]:
    """Execute the actual recipe with this test environment's Python and plugin helpers."""
    return subprocess.run(
        ["bash"],
        input='python() { "$MANAGE_PYTHON" "$@"; }\n' + script,
        cwd=root,
        env={**os.environ, "MANAGE_PYTHON": sys.executable, "CLAUDE_PLUGIN_ROOT": str(_PLUGIN), **env},
        text=True,
        capture_output=True,
        check=False,
    )


def _hook_project(root: Path, settings: str) -> Path:
    """Create a real project hook plus an independently owned same-named plugin registration."""
    hook = root / ".claude/hooks/target.js"
    hook.parent.mkdir(parents=True)
    hook.write_text("process.exit(0);\n", encoding="utf-8")
    (root / ".claude/settings.json").write_text(settings, encoding="utf-8")
    shipped = root / "plugins/cc_foundry/hooks/hooks.json"
    shipped.parent.mkdir(parents=True)
    shipped.write_text(settings, encoding="utf-8")
    return hook


@_BASH
def test_delete_deregisters_project_hook_without_touching_shipped_registry(tmp_path: Path) -> None:
    """Only the project command disappears; same-basename plugin registration survives byte for byte."""
    settings = json.dumps({"hooks": {"PreToolUse": [{"hooks": [{"command": "node .claude/hooks/target.js"}]}]}})
    hook = _hook_project(tmp_path, settings)
    recipe = "\n".join(_blocks("modes/delete.md", "### Mode: Delete Hook")).replace("<name>", "target")
    result = _run(recipe, tmp_path)
    assert result.returncode == 0, result.stderr
    assert not hook.exists()
    assert json.loads((tmp_path / ".claude/settings.json").read_text())["hooks"] == {}
    assert (tmp_path / "plugins/cc_foundry/hooks/hooks.json").read_text() == settings


@_BASH
def test_delete_keeps_hook_when_deregistration_fails(tmp_path: Path) -> None:
    """Malformed settings stop deletion rather than leaving a registered missing script."""
    hook = _hook_project(tmp_path, "{broken")
    recipe = "\n".join(_blocks("modes/delete.md", "### Mode: Delete Hook")).replace("<name>", "target")
    result = _run(recipe, tmp_path)
    assert result.returncode != 0
    assert hook.read_text() == "process.exit(0);\n"
    assert (tmp_path / ".claude/settings.json").read_text() == "{broken"


@_BASH
def test_delete_keeps_hook_when_registration_remains(tmp_path: Path) -> None:
    """Even a zero-exit deregistration cannot authorize deletion while the command remains."""
    settings = json.dumps({"hooks": {"PreToolUse": [{"hooks": [{"command": "node .claude/hooks/target.js"}]}]}})
    hook = _hook_project(tmp_path, settings)
    recipe = "\n".join(_blocks("modes/delete.md", "### Mode: Delete Hook")).replace("<name>", "target")
    result = _run("python() { return 0; }\n" + recipe, tmp_path)
    assert result.returncode != 0
    assert hook.exists()
    assert (tmp_path / ".claude/settings.json").read_text() == settings


@_BASH
@pytest.mark.parametrize(
    ("heading", "index"),
    [
        pytest.param("### Mode: Add Permission", 0, id="project-add"),
        pytest.param("### Mode: Add Permission", 1, id="shipped-add"),
        pytest.param("### Mode: Add Permission", 2, id="verify-add"),
        pytest.param("### Mode: Remove Permission", 0, id="project-remove"),
        pytest.param("### Mode: Remove Permission", 1, id="verify-remove"),
    ],
)
def test_unsubstituted_permission_is_rejected_before_any_helper(tmp_path: Path, heading: str, index: int) -> None:
    """Every independently executed permission recipe rejects its literal rule placeholder."""
    recipe = _blocks("modes/perm.md", heading)[index]
    result = _run("python() { echo unexpected-helper-call; return 0; }\n" + recipe, tmp_path)
    assert result.returncode != 0
    assert "unexpected-helper-call" not in result.stdout
    assert "BLOCKED" in result.stdout


@_BASH
@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        pytest.param('{"permissions":{"allow":["WebSearch"]}}', "present", id="present"),
        pytest.param('{"permissions":{"allow":[]}}', "absent", id="absent"),
        pytest.param("{}", "absent", id="no-permissions"),
        pytest.param('{"permissions":{"deny":[]}}', "absent", id="no-allow"),
        pytest.param('{"permissions":{"allow":null}}', "absent", id="null-allow"),
        pytest.param("{broken", None, id="malformed"),
        pytest.param('{"permissions":{"allow":"WebSearch"}}', None, id="invalid-shape"),
        pytest.param('{"permissions":{"allow":false}}', None, id="invalid-boolean"),
    ],
)
def test_permission_validation_distinguishes_absence_from_errors(
    tmp_path: Path, settings: str, expected: str | None
) -> None:
    """Only valid settings can establish whether an exact permission is present or absent."""
    config = tmp_path / ".claude/settings.json"
    config.parent.mkdir()
    config.write_text(settings, encoding="utf-8")
    recipe = _blocks("modes/perm.md", "### Permission validation (add/remove)")[0]
    result = _run(recipe.replace("<rule>", "WebSearch"), tmp_path)
    assert (result.returncode == 0) == (expected is not None)
    assert result.stdout.strip() == (expected or "! BLOCKED — permission lookup failed")
    assert config.read_text() == settings


@_BASH
@pytest.mark.parametrize(
    ("kind", "filename", "variable"),
    [
        pytest.param("Agent", "agent-schema.md", "MANAGE_SCHEMA_CACHED", id="agent"),
        pytest.param("Skill", "skill-schema.md", "MANAGE_SKILL_SCHEMA_CACHED", id="skill"),
    ],
)
@pytest.mark.parametrize(
    ("contents", "cached"),
    [
        pytest.param("", False, id="empty"),
        pytest.param("Fetching schema...\n", False, id="fieldless"),
        pytest.param("name\ndescription\n", False, id="interrupted"),
        pytest.param("<!-- manage-schema-complete -->\n", False, id="marker-only"),
        pytest.param("name\ndescription\n<!-- manage-schema-complete -->\n", True, id="complete"),
    ],
)
def test_schema_cache_requires_fields_and_completed_validation(
    tmp_path: Path, kind: str, filename: str, variable: str, contents: str, cached: bool
) -> None:
    """Recent but empty, fieldless, or interrupted explorer output is a cache miss."""
    cache = tmp_path / ".cache/manage" / filename
    cache.parent.mkdir(parents=True)
    cache.write_text(contents, encoding="utf-8")
    recipe = _blocks("modes/create.md", f"### Mode: Create {kind}")[0]
    result = _run(recipe + f'\nprintf "cache-state=%s\\n" "${variable}"\n', tmp_path)
    assert result.returncode == 0, result.stderr
    assert f"cache-state={str(cached).lower()}" in result.stdout.splitlines()


def test_permission_shipping_requires_an_explicit_scope_choice() -> None:
    """Source-tree presence alone never opts a project permission into the shipped allow list."""
    lines = (_PLUGIN / "skills/manage/modes/perm.md").read_text(encoding="utf-8").splitlines()
    choice = next(index for index, line in enumerate(lines) if "AskUserQuestion" in line)
    first_write = next(index for index, line in enumerate(lines) if "jq_write.py" in line)
    assert choice < first_write
    assert "Project-only" in lines[choice]
    assert "default" in lines[choice]
    assert any("SHIP_PERMISSION=true" in line and "explicit" in line for line in lines)
    assert any('"$SHIP_PERMISSION" = true' in line for line in lines)


@_BASH
def test_skill_copy_preserves_nested_assets_and_hidden_files(tmp_path: Path) -> None:
    """The real copy recipe retains all skill payload bytes before any name edit or source deletion."""
    source = tmp_path / ".claude/skills/old-skill"
    (source / "templates").mkdir(parents=True)
    (source / "SKILL.md").write_bytes(b"---\nname: old-skill\n---\nbody\n")
    (source / "templates/asset.bin").write_bytes(b"\x00\x01\xff")
    (source / ".private").write_bytes(b"hidden payload\n")
    recipe = _blocks("modes/rename.md", "### Mode: Update Skill (rename)")[0]
    result = _run(recipe.replace("<old-name>", "old-skill").replace("<new-name>", "new-skill"), tmp_path)
    target = source.with_name("new-skill")
    assert result.returncode == 0, result.stderr
    assert {path.relative_to(target): path.read_bytes() for path in target.rglob("*") if path.is_file()} == {
        Path("SKILL.md"): b"---\nname: old-skill\n---\nbody\n",
        Path("templates/asset.bin"): b"\x00\x01\xff",
        Path(".private"): b"hidden payload\n",
    }
    assert (source / "templates/asset.bin").read_bytes() == b"\x00\x01\xff"


@_BASH
def test_skill_copy_refuses_an_existing_destination(tmp_path: Path) -> None:
    """Copying cannot merge into or overwrite a pre-existing skill directory."""
    source = tmp_path / ".claude/skills/old-skill"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("old content\n", encoding="utf-8")
    target = source.with_name("new-skill")
    target.mkdir()
    (target / "SKILL.md").write_text("other owner's content\n", encoding="utf-8")
    recipe = _blocks("modes/rename.md", "### Mode: Update Skill (rename)")[0]
    result = _run(recipe.replace("<old-name>", "old-skill").replace("<new-name>", "new-skill"), tmp_path)
    assert result.returncode != 0
    assert (target / "SKILL.md").read_text() == "other owner's content\n"
    assert not (target / "old-skill").exists()
    assert (source / "SKILL.md").read_text() == "old content\n"


@_BASH
@pytest.mark.parametrize("ship", [False, True])
def test_permission_source_write_obeys_scope_even_when_source_exists(tmp_path: Path, ship: bool) -> None:
    """Only an explicit shipping choice changes the existing source allow list."""
    source = tmp_path / "plugins/cc_foundry/.claude-plugin/permissions-allow.json"
    source.parent.mkdir(parents=True)
    source.write_text("[]\n", encoding="utf-8")
    recipe = _blocks("modes/perm.md", "### Mode: Add Permission")[1]
    result = _run(recipe.replace("<rule>", "WebSearch").replace("<true-or-false>", str(ship).lower()), tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(source.read_text()) == (["WebSearch"] if ship else [])


@_BASH
@pytest.mark.parametrize("index", [0, 1])
def test_rename_scans_include_project_docs_and_exclude_shipped_plugins(tmp_path: Path, index: int) -> None:
    """Initial and final scans include project documentation without touching shipped plugins."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/CLAUDE.md").write_text("Use /old-name\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("Local old-name\n", encoding="utf-8")
    shipped = tmp_path / "plugins/example/skills/old-name/SKILL.md"
    shipped.parent.mkdir(parents=True)
    shipped.write_text("Use /foundry:old-name\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/guide.md").write_text("old-name reference\n", encoding="utf-8")
    recipe = _blocks("modes/rename-validation.md", "# Mode: Rename Occurrence Validation")[index]
    result = _run(recipe.replace("<old-name>", "old-name"), tmp_path)
    assert result.returncode == 0, result.stderr
    assert ".claude/CLAUDE.md" in result.stdout
    assert "README.md" in result.stdout
    assert "plugins/" not in result.stdout
    assert "docs/guide.md" in result.stdout


@_BASH
@pytest.mark.parametrize("local", [False, True])
def test_manage_flag_parser_persists_local_source_selection(tmp_path: Path, local: bool) -> None:
    """The declared local flag survives argument cleaning without becoming an unknown token."""
    recipe = _blocks("SKILL.md", "## Step 1: Parse and validate")[0]
    arguments = 'update planner "revise workflow"' + (" --local" if local else "")
    result = _run(
        recipe,
        tmp_path,
        ARGUMENTS=arguments,
        TMPDIR=str(tmp_path),
        CLAUDE_CODE_SESSION_ID="manage-flags",
    )
    assert result.returncode == 0, result.stderr
    assert f"Local source mode: {str(local).lower()}" in result.stdout
    assert (tmp_path / "manage-clean-args-manage-flags").read_text().strip() == 'update planner "revise workflow"'
    assert (tmp_path / "manage-local-manage-flags").read_text().strip() == str(local).lower()


@_BASH
def test_delete_deregisters_a_local_settings_only_hook(tmp_path: Path) -> None:
    """A local-only registration is removed even when project settings do not exist."""
    settings = json.dumps({"hooks": {"PreToolUse": [{"hooks": [{"command": "node .claude/hooks/target.js"}]}]}})
    hook = _hook_project(tmp_path, settings)
    config = tmp_path / ".claude/settings.local.json"
    (tmp_path / ".claude/settings.json").rename(config)
    recipe = _blocks("modes/delete.md", "### Mode: Delete Hook")[0].replace("<name>", "target")
    result = _run(recipe, tmp_path)
    assert result.returncode == 0, result.stderr
    assert not hook.exists()
    assert json.loads(config.read_text())["hooks"] == {}
    assert not (tmp_path / ".claude/settings.json").exists()


@_BASH
def test_delete_unregistered_hook_without_any_settings(tmp_path: Path) -> None:
    """An unregistered hook needs no settings file to be safely deleted."""
    hook = _hook_project(tmp_path, "{}")
    (tmp_path / ".claude/settings.json").unlink()
    recipe = _blocks("modes/delete.md", "### Mode: Delete Hook")[0].replace("<name>", "target")
    result = _run(recipe, tmp_path)
    assert result.returncode == 0, result.stderr
    assert not hook.exists()


@_BASH
def test_delete_removes_registrations_from_both_settings_files(tmp_path: Path) -> None:
    """Deleting the script requires clearing both active project registries."""
    settings = json.dumps({"hooks": {"PreToolUse": [{"hooks": [{"command": "node .claude/hooks/target.js"}]}]}})
    hook = _hook_project(tmp_path, settings)
    config = tmp_path / ".claude/settings.local.json"
    config.write_text(settings, encoding="utf-8")
    recipe = _blocks("modes/delete.md", "### Mode: Delete Hook")[0].replace("<name>", "target")
    result = _run(recipe, tmp_path)
    assert result.returncode == 0, result.stderr
    assert not hook.exists()
    assert json.loads(config.read_text())["hooks"] == {}
    assert json.loads((tmp_path / ".claude/settings.json").read_text())["hooks"] == {}


@_BASH
@pytest.mark.parametrize(
    ("settings", "prefix"),
    [
        pytest.param("{broken", "", id="malformed-local-settings"),
        pytest.param(
            '{"hooks":{"PreToolUse":[{"hooks":[{"command":"node .claude/hooks/target.js"}]}]}}',
            "python() { return 0; }\n",
            id="registration-remains-local",
        ),
    ],
)
def test_delete_keeps_hook_when_local_verification_fails(tmp_path: Path, settings: str, prefix: str) -> None:
    """A failure in the second registry cannot authorize removing its registered script."""
    hook = _hook_project(tmp_path, "{}")
    config = tmp_path / ".claude/settings.local.json"
    config.write_text(settings, encoding="utf-8")
    recipe = _blocks("modes/delete.md", "### Mode: Delete Hook")[0].replace("<name>", "target")
    result = _run(prefix + recipe, tmp_path)
    assert result.returncode != 0
    assert hook.read_text() == "process.exit(0);\n"
    assert config.read_text() == settings


@_BASH
def test_delete_uses_only_the_guarded_hook_basename(tmp_path: Path) -> None:
    """The deletion target follows the validated variable without a second substitution."""
    hook = _hook_project(tmp_path, "{}")
    other = hook.with_name("other.js")
    other.write_bytes(b"keep\n")
    recipe = _blocks("modes/delete.md", "### Mode: Delete Hook")[0].replace('HOOK_NAME="<name>"', 'HOOK_NAME="target"')
    result = _run(recipe, tmp_path)
    assert result.returncode == 0, result.stderr
    assert not hook.exists()
    assert other.read_bytes() == b"keep\n"


@_BASH
@pytest.mark.parametrize(
    ("contents", "accepted"),
    [
        pytest.param("name: new\nbody\n", True, id="intended-rename"),
        pytest.param("name: new\n", False, id="truncated-body"),
    ],
)
def test_rename_comparison_checks_line_counts_and_shows_diff(tmp_path: Path, contents: str, accepted: bool) -> None:
    """The shared comparison exposes substitutions and blocks a truncated renamed file."""
    source = tmp_path / "old.md"
    target = tmp_path / "new.md"
    source.write_bytes(b"name: old\nbody\n")
    target.write_bytes(contents.encode())
    recipe = _blocks("modes/rename.md", "### Rename comparison (agents/skills)")[0]
    result = _run(recipe.replace("<source-path>", "old.md").replace("<destination-path>", "new.md"), tmp_path)
    assert (result.returncode == 0) == accepted
    assert "-name: old" in result.stdout
    assert "+name: new" in result.stdout
    assert source.read_bytes() == b"name: old\nbody\n"


@_BASH
@_SYMLINKS
@pytest.mark.parametrize(
    ("link_target", "accepted"),
    [
        pytest.param("missing", True, id="preserved-dangling-link"),
        pytest.param("different-missing", False, id="changed-dangling-target"),
    ],
)
def test_skill_comparison_checks_dangling_symlink_targets(tmp_path: Path, link_target: str, accepted: bool) -> None:
    """Recursive verification compares link targets even when diff ignores dangling links."""
    source = tmp_path / "old"
    target = tmp_path / "new"
    source.mkdir()
    target.mkdir()
    (source / "SKILL.md").write_bytes(b"name: old\n")
    (target / "SKILL.md").write_bytes(b"name: new\n")
    (source / "asset").symlink_to("missing")
    (target / "asset").symlink_to(link_target)
    recipe = _blocks("modes/rename.md", "### Rename comparison (agents/skills)")[0]
    result = _run(recipe.replace("<source-path>", "old").replace("<destination-path>", "new"), tmp_path)
    assert (result.returncode == 0) == accepted
    assert (source / "asset").is_symlink()


@_BASH
@_SYMLINKS
def test_skill_comparison_rejects_a_missing_dangling_link(tmp_path: Path) -> None:
    """An inventory mismatch blocks deletion even when all regular files match."""
    source = tmp_path / "old"
    target = tmp_path / "new"
    source.mkdir()
    target.mkdir()
    (source / "SKILL.md").write_bytes(b"body\n")
    (target / "SKILL.md").write_bytes(b"body\n")
    (source / "asset").symlink_to("missing")
    recipe = _blocks("modes/rename.md", "### Rename comparison (agents/skills)")[0]
    result = _run(recipe.replace("<source-path>", "old").replace("<destination-path>", "new"), tmp_path)
    assert result.returncode != 0
    assert "asset" in result.stdout
    assert (source / "asset").is_symlink()


@_BASH
@pytest.mark.parametrize(
    "contents",
    [
        pytest.param(b"body\n", id="trailing-newline"),
        pytest.param(b"body", id="no-trailing-newline"),
        pytest.param(b"\x00\xff\n", id="binary-content"),
    ],
)
def test_rule_rename_copies_exact_bytes_before_deleting(tmp_path: Path, contents: bytes) -> None:
    """Rule copying preserves trailing newlines and arbitrary bytes without a Write-tool round trip."""
    source = tmp_path / ".claude/rules/old.md"
    source.parent.mkdir(parents=True)
    source.write_bytes(contents)
    recipe = _blocks("modes/rename.md", "### Mode: Update Rule (rename)")[0]
    result = _run(recipe.replace("<old-name>", "old").replace("<new-name>", "new"), tmp_path)
    assert result.returncode == 0, result.stderr
    assert source.read_bytes() == contents
    assert source.with_name("new.md").read_bytes() == contents
