"""Protect manage's entity boundaries, rename preservation, and delegated editing gates."""

from pathlib import Path

import pytest

_MANAGE = Path(__file__).resolve().parents[2] / "skills/manage"


def _lines(relative: str) -> list[str]:
    """Read instruction lines without depending on paragraph wrapping."""
    return (_MANAGE / relative).read_text(encoding="utf-8").splitlines()


def test_hook_rename_is_rejected_before_confirmation() -> None:
    """A hook cannot enter the unsupported rename workflow and strand its registration."""
    lines = _lines("SKILL.md")
    guard = next(index for index, line in enumerate(lines) if line.startswith("**Hook rename guard**"))
    confirmation = next(index for index, line in enumerate(lines) if line.startswith("**Rename confirmation gate**"))
    assert guard < confirmation
    assert "stop" in lines[guard]
    assert "content-edit" in lines[guard]


def test_skill_rename_preserves_and_verifies_the_complete_directory() -> None:
    """Nested modes, templates, assets and hidden files survive skill renames."""
    lines = _lines("modes/rename.md")
    start = lines.index("### Mode: Update Skill (rename)")
    end = lines.index("### Mode: Update Rule (rename)")
    skill = lines[start:end]
    assert any("cp -R" in line and "<old-name>" in line and "<new-name>" in line for line in skill)
    assert any("including hidden files" in line for line in skill)
    assert any("file counts" in line and "byte" in line for line in skill)
    assert any("diff -r" in line for line in skill)
    assert any("line count" in line and "SKILL.md" in line for line in skill)
    assert not any("mkdir -p" in line for line in skill)


def test_rename_verification_reads_complete_files_and_compares_source() -> None:
    """A valid header cannot hide a truncated agent, rule, or skill body."""
    lines = _lines("modes/rename.md")
    assert not any("limit=5" in line for line in lines)
    assert any("line counts" in line and "source" in line for line in lines)
    assert any("cmp -s" in line for line in lines)


def test_source_content_edits_resolve_explicit_local_targets() -> None:
    """Plugin authoring finds source Markdown without silently editing an installed cache."""
    lines = _lines("SKILL.md")
    assert any("`--local`" in line and "content-edit" in line for line in lines)
    assert any("plugins/*/agents/<name>.md" in line for line in lines)
    assert any("ENTITY_PATH" in line and "SOURCE_ROOT" in line for line in lines)
    assert any("installed cache" in line and "never" in line for line in lines)


def test_hooks_cannot_take_the_trivial_edit_shortcut() -> None:
    """A short exit-code replacement still receives hook authoring review."""
    lines = _lines("SKILL.md")
    assert any("hook" in line.lower() and "EDIT_TRIVIAL=false" in line for line in lines)
    edit = _lines("modes/content-edit.md")
    assert any("EDIT_TRIVIAL=true" in line and "not a hook" in line for line in edit)


def test_skill_edit_prompt_includes_supporting_markdown_only() -> None:
    """A skill directive can reach modes and templates without opening arbitrary code editing."""
    lines = _lines("modes/content-edit.md")
    assert any("skill directory" in line and "modes/" in line and "templates/" in line for line in lines)
    assert any("ENTITY_PATH" in line for line in lines)
    rows = [line for line in lines if line.startswith("|")]
    assert not any("`.py`" in line or "`.ts`" in line or "`.sh`" in line for line in rows)


def test_rename_validation_uses_local_surfaces_and_scoped_scans() -> None:
    """A project skill rename cannot rewrite a same-named shipped foundry skill."""
    lines = _lines("modes/rename-validation.md")
    assert any("`/<old-name>`" in line and "project-local" in line for line in lines)
    assert any("never infer" in line and "foundry:" in line for line in lines)
    assert not any("plugins/ .claude/" in line for line in lines)
    assert any(".claude/ README.md" in line and "rg -n" in line for line in lines)
    assert any("SOURCE_ROOT" in line and "plugins/" in line for line in lines)


def test_rename_classifies_occurrences_before_any_reference_edit() -> None:
    """Propagation cannot corrupt excluded namespaced references before validation sees them."""
    lines = _lines("SKILL.md")
    assert any("skip directly" in line and "Rename occurrence validation" in line for line in lines)
    assert not any("replace_all: true" in line for line in lines)
    validation = _lines("modes/rename-validation.md")
    assert any(">3 files" in line and "foundry:curator" in line for line in validation)


def test_concrete_reference_searches_use_the_selected_source_root() -> None:
    """The explicit search recipe cannot override source selection or omit skill support files."""
    lines = _lines("SKILL.md")
    start = lines.index("Use Grep to find all references:")
    end = next(index for index in range(start + 1, len(lines)) if lines[index].startswith("**For update"))
    searches = [line for line in lines[start:end] if line.startswith("- Pattern")]
    assert len(searches) == 5
    assert all("SOURCE_ROOT" in line for line in searches)
    assert any("skills/**/*.md" in line for line in searches)


def test_rename_classifier_batches_have_a_type_and_run_sequentially() -> None:
    """Each classifier uses the supported Agent interface and retains its own watch row."""
    lines = _lines("modes/rename-validation.md")
    assert any('subagent_type="general-purpose"' in line and 'model="haiku"' in line for line in lines)
    assert any('name="rename-hits-' in line and "description=" in line for line in lines)
    assert any("sequential" in line and "agent-watch-manage.tsv" in line for line in lines)


def test_targeted_audit_has_a_deliverable_and_critical_stop() -> None:
    """Manage records critical findings before finishing, without offering a challenger gate."""
    lines = _lines("SKILL.md")
    assert not any("audit-initiated fix session" in line or "proceed to sync" in line for line in lines)
    assert any('Agent(subagent_type="foundry:curator"' in line and "Return ONLY:" in line for line in lines)
    assert any("critical" in line and "Step 10" in line and "challenger" in line for line in lines)


def test_readme_rename_cannot_bypass_occurrence_classification() -> None:
    """The later README step cannot rewrite references intentionally excluded by Step 5."""
    lines = _lines("SKILL.md")
    assert any("README rename" in line and "classified genuine" in line for line in lines)
    assert not any("find and replace old name in table row" in line for line in lines)


def test_targeted_hook_validation_uses_its_code_owner() -> None:
    """Hook validation cannot be declined by a curator that explicitly excludes JavaScript hooks."""
    lines = _lines("SKILL.md")
    assert any("Hook operations" in line and 'subagent_type="foundry:sw-engineer"' in line for line in lines)


def test_type_ambiguity_has_one_four_entity_question() -> None:
    """Summary guidance cannot omit hooks from the authoritative type-selection gate."""
    lines = _lines("SKILL.md")
    assert any("Multiple matches" in line and "Step 1" in line for line in lines)
    assert any("Which one? (a) agent (b) skill (c) rule (d) hook" in line for line in lines)


def test_agent_scaffold_uses_balanced_normal_tags_and_declares_tools() -> None:
    """Scaffolded agents have usable navigation and an explicit tool boundary."""
    lines = _lines("templates/agent-scaffold.md")
    assert not any("\\<" in line or line.startswith("`<workflow>`") for line in lines)
    assert "</role>" in lines
    assert any(line.startswith("<notes>") for line in lines)
    create = _lines("modes/create.md")
    assert any("Frontmatter:" in line and "tools=<tools>" in line for line in create)


@pytest.mark.parametrize("kind", ["Agent", "Skill"])
def test_schema_prompt_uses_resolved_cache_path_in_both_branches(kind: str) -> None:
    """A cache hit cannot refer to a JSON envelope from a skipped explorer spawn."""
    lines = _lines("modes/create.md")
    start = lines.index(f"### Mode: Create {kind}")
    end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("### Mode:")), len(lines))
    section = lines[start:end]
    assert not any("path returned in the step 1 JSON" in line for line in section)
    assert any("resolved schema file" in line for line in section)
    assert any("manage-schema-complete" in line for line in section)


def test_rule_creation_distinguishes_global_and_scoped_templates() -> None:
    """Global rules omit paths rather than generating a placeholder scope."""
    lines = _lines("modes/create.md")
    assert any("Global rule" in line and "omit" in line and "paths:" in line for line in lines)
    assert any("Scoped rule" in line for line in lines)
    assert not any("cascade primary" in line or "cache scan with" in line for line in lines)


def test_permission_scope_question_requires_an_existing_source_choice() -> None:
    """Installed projects proceed project-only without a one-option scope question."""
    lines = _lines("modes/perm.md")
    assert any(
        "permissions-allow.json" in line
        and "absent" in line
        and "skip the question" in line
        and "SHIP_PERMISSION=false" in line
        and "ask only when it exists" in line
        for line in lines
    )


def test_local_operation_guard_runs_before_permission_or_entity_routing() -> None:
    """Create, permission, delete and rename operations cannot bypass the local edit boundary."""
    lines = _lines("SKILL.md")
    start = lines.index("## Step 1: Parse and validate")
    flag_end = next(index for index in range(start, len(lines)) if lines[index] == "```")
    guard = next(index for index, line in enumerate(lines) if line.startswith("**Local operation check**"))
    assert not any(line.strip() for line in lines[flag_end + 1 : guard])
    assert "LOCAL=true" in lines[guard]
    assert "`update`" in lines[guard]
    assert "quoted directive or a spec path" in lines[guard]
    assert "! UNSUPPORTED — --local supports update content-edits only" in lines[guard]
    assert "stop" in lines[guard]


def test_local_skill_resolution_includes_alternate_source_roots() -> None:
    """Source skills declared under claude-skills remain reachable from either working directory."""
    lines = _lines("SKILL.md")
    resolution = next(line for line in lines if line.startswith("**Type auto-detection**"))
    assert "`claude-skills/<name>/SKILL.md`" in resolution
    assert "`plugins/*/claude-skills/<name>/SKILL.md`" in resolution


def test_manage_objective_includes_plugin_source_edits() -> None:
    """The objective includes the supported source-authoring mode alongside project lifecycle work."""
    lines = _lines("SKILL.md")
    objective = lines[lines.index("<objective>") + 1 : lines.index("</objective>")]
    assert any("plugin-source Markdown content-edits with `--local`" in line for line in objective)


def test_roster_refresh_reads_disk_without_memory_writes() -> None:
    """The summary roster is refreshed from disk without editing the session's memory index."""
    lines = _lines("SKILL.md")
    start = next(index for index, line in enumerate(lines) if line.startswith("## Step 6:"))
    end = next(index for index, line in enumerate(lines) if line.startswith("## Step 7:"))
    section = lines[start:end]
    assert not any("MEMORY.md" in line or "resolve_memory_dir.py" in line for line in section)
    assert not any("Edit tool" in line or "Write tool" in line for line in section)
    inventory = next(line for line in section if line.startswith("Use Glob"))
    assert "agents/*.md" in inventory
    assert "skills/*/" in inventory
    assert "rules/*.md" in inventory
    assert any(line.startswith("- **Current Roster**:") for line in lines)


def test_description_propagation_does_not_edit_memory_summaries() -> None:
    """Changing an entity description cannot write roster summaries into auto-memory."""
    lines = _lines("SKILL.md")
    start = lines.index("## Step 5: Propagate cross-references")
    end = next(index for index, line in enumerate(lines) if line.startswith("## Step 6:"))
    propagation = next(line for line in lines[start:end] if line.startswith("**For content-edit:**"))
    assert "README" in propagation
    assert "MEMORY.md" not in propagation


def test_post_change_rule_does_not_require_memory_roster_writes() -> None:
    """The shared config checklist cannot reintroduce memory writes after manage completes."""
    lines = (_MANAGE.parents[1] / "rules/foundry-config.md").read_text(encoding="utf-8").splitlines()
    start = lines.index("## After Any Change")
    end = lines.index("## Path Rules (foundry-infra)")
    section = lines[start:end]
    assert not any("MEMORY.md" in line or "resolve_memory_dir.py" in line for line in section)
    assert any("**`README.md`**" in line for line in section)
    assert any("**`settings.json` permissions**" in line for line in section)
