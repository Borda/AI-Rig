"""Tests for check_routing_links.py — R1/R2/R3 path-duality checks."""

from __future__ import annotations

from pathlib import Path

import pytest

# conftest.py registers bin/ scripts as importable modules
from check_routing_links import (
    CheckResults,
    Severity,
    R1Finding,
    R2Finding,
    R3Finding,
    _resolve_computed_abs,
    _resolve_computed_rel,
    extract_bin_refs,
    extract_path_refs,
    find_in_installed,
    format_results,
    get_installed_versions,
    is_basename_grep_visible,
    main,
    run_bin_ref_integrity,
    run_computed_path_duality,
    run_orphan_risk_detection,
)

_HAS_PROJECT_PLUGIN_TREE = (Path(__file__).resolve().parent.parent.parent / "cc_foundry").is_dir()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_plugin_tree(tmp: Path, plugin: str = "foundry") -> tuple[Path, Path]:
    """Create a minimal plugins/cc_<plugin>/ tree and return (plugins_dir, plugin_dir).

    The on-disk source folder is cc_-prefixed after the folder rename, matching what ``_VAR_ROOTS`` resolves paths
    against; the plugin ``name`` stays bare.
    """
    plugins_dir = tmp / "plugins"
    plugin_dir = plugins_dir / f"cc_{plugin}"
    (plugin_dir / "skills" / "audit" / "templates").mkdir(parents=True)
    (plugin_dir / "skills" / "audit" / "modes").mkdir(parents=True)
    (plugin_dir / "skills" / "_shared").mkdir(parents=True)
    (plugin_dir / "agents").mkdir(parents=True)
    (plugin_dir / "bin").mkdir(parents=True)
    return plugins_dir, plugin_dir


def _make_cache(tmp: Path, plugin: str = "foundry", version: str = "0.17.0") -> Path:
    """Create a minimal plugin cache tree and return cache_dir (borda-ai-rig root)."""
    cache_dir = tmp / "cache"
    ver_dir = cache_dir / plugin / version
    (ver_dir / "skills" / "audit" / "templates").mkdir(parents=True)
    (ver_dir / "skills" / "audit" / "modes").mkdir(parents=True)
    (ver_dir / "bin").mkdir(parents=True)
    return cache_dir


# ---------------------------------------------------------------------------
# _resolve_computed_rel
# ---------------------------------------------------------------------------


class TestResolveComputedRel:
    def test_audit_tpl_modes(self, tmp_path: Path) -> None:
        plugins_dir = tmp_path / "plugins"
        result = _resolve_computed_rel("AUDIT_TPL", "modes/upgrade.md", plugins_dir)
        assert result == (plugins_dir / "cc_foundry" / "skills" / "audit" / "modes" / "upgrade.md").as_posix()

    def test_unknown_var_returns_none(self, tmp_path: Path) -> None:
        result = _resolve_computed_rel("MYSTERY_VAR", "foo.md", tmp_path)
        assert result is None

    def test_fs_shared_file(self, tmp_path: Path) -> None:
        plugins_dir = tmp_path / "plugins"
        result = _resolve_computed_rel("_FS", "../modes/x.md", plugins_dir)
        # _FS root is skills/_shared; parent is skills/; so result = skills/modes/x.md
        assert result is not None
        assert "modes/x.md" in result


# ---------------------------------------------------------------------------
# _resolve_computed_abs
# ---------------------------------------------------------------------------


class TestResolveComputedAbs:
    def test_fs_task_hygiene(self, tmp_path: Path) -> None:
        plugins_dir = tmp_path / "plugins"
        result = _resolve_computed_abs("_FS", "task-hygiene.md", plugins_dir)
        assert result == (plugins_dir / "cc_foundry" / "skills" / "_shared" / "task-hygiene.md").as_posix()

    def test_unknown_var_returns_none(self, tmp_path: Path) -> None:
        result = _resolve_computed_abs("NO_SUCH_VAR", "foo.md", tmp_path)
        assert result is None

    def test_foundry_shared(self, tmp_path: Path) -> None:
        plugins_dir = tmp_path / "plugins"
        result = _resolve_computed_abs("_FOUNDRY_SHARED", "bin-authoring-guide.md", plugins_dir)
        assert result is not None
        assert "bin-authoring-guide.md" in result


# ---------------------------------------------------------------------------
# extract_path_refs
# ---------------------------------------------------------------------------


class TestExtractPathRefs:
    def test_computed_rel_pattern(self, tmp_path: Path) -> None:
        plugins_dir, plugin_dir = _make_plugin_tree(tmp_path)
        skill_md = plugin_dir / "skills" / "audit" / "SKILL.md"
        skill_md.write_text(
            'UPGRADE_MD="$AUDIT_TPL/../modes/upgrade.md"\nADVERSARIAL_MD="$AUDIT_TPL/../modes/adversarial.md"\n'
        )
        refs = extract_path_refs(skill_md, "foundry", plugins_dir)
        basenames = {r.target_basename for r in refs}
        assert "upgrade.md" in basenames
        assert "adversarial.md" in basenames
        for r in refs:
            assert r.ref_type == "computed_rel"

    def test_computed_abs_read_pattern(self, tmp_path: Path) -> None:
        plugins_dir, plugin_dir = _make_plugin_tree(tmp_path)
        skill_md = plugin_dir / "skills" / "audit" / "SKILL.md"
        skill_md.write_text('Read "$_FS/task-hygiene.md"\nRead "$_FS/file-handoff-protocol.md"\n')
        refs = extract_path_refs(skill_md, "foundry", plugins_dir)
        basenames = {r.target_basename for r in refs}
        assert "task-hygiene.md" in basenames
        assert "file-handoff-protocol.md" in basenames

    def test_unknown_var_skipped(self, tmp_path: Path) -> None:
        plugins_dir, plugin_dir = _make_plugin_tree(tmp_path)
        skill_md = plugin_dir / "skills" / "audit" / "SKILL.md"
        skill_md.write_text('Read "$UNKNOWN_VAR/something.md"\n')
        refs = extract_path_refs(skill_md, "foundry", plugins_dir)
        assert refs == []

    def test_deduplication(self, tmp_path: Path) -> None:
        plugins_dir, plugin_dir = _make_plugin_tree(tmp_path)
        skill_md = plugin_dir / "skills" / "audit" / "SKILL.md"
        skill_md.write_text('Read "$_FS/task-hygiene.md"\nRead "$_FS/task-hygiene.md"\n')
        refs = extract_path_refs(skill_md, "foundry", plugins_dir)
        # Duplicate expressions → deduplicated
        task_refs = [r for r in refs if r.target_basename == "task-hygiene.md"]
        assert len(task_refs) == 1


# ---------------------------------------------------------------------------
# extract_bin_refs
# ---------------------------------------------------------------------------


class TestExtractBinRefs:
    def test_claude_plugin_root_pattern(self, tmp_path: Path) -> None:
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text(
            'python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_orphaned_bin.py" --plugins-dir plugins\n'
        )
        result = extract_bin_refs(skill_md, "foundry")
        assert len(result) == 1
        source_file, bin_plugin, script_name, explicit_plugin = result[0]
        assert bin_plugin == "foundry"
        assert script_name == "check_orphaned_bin.py"
        assert explicit_plugin is True

    def test_multiple_scripts(self, tmp_path: Path) -> None:
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text(
            '"${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/foo.py"\n"${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/bar.sh"\n'
        )
        result = extract_bin_refs(skill_md, "foundry")
        scripts = {r[2] for r in result}
        assert "foo.py" in scripts
        assert "bar.sh" in scripts

    def test_cross_plugin_ref(self, tmp_path: Path) -> None:
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text('"${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/codemap_scan.py"\n')
        result = extract_bin_refs(skill_md, "foundry")
        assert len(result) == 1
        _, bin_plugin, script, explicit_plugin = result[0]
        assert bin_plugin == "develop"
        assert script == "codemap_scan.py"
        assert explicit_plugin is True

    def test_deduplication(self, tmp_path: Path) -> None:
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text(
            '"${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/foo.py"\n"${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/foo.py"\n'
        )
        result = extract_bin_refs(skill_md, "foundry")
        assert len(result) == 1

    def test_no_fallback_form_infers_plugin(self, tmp_path: Path) -> None:
        """${CLAUDE_PLUGIN_ROOT}/bin/<script> (no :-fallback) resolves to the owning plugin."""
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text('python "${CLAUDE_PLUGIN_ROOT}/bin/health_sentinel.py" start foo\n')
        result = extract_bin_refs(skill_md, "foundry")
        assert len(result) == 1
        _, bin_plugin, script, explicit_plugin = result[0]
        assert bin_plugin == "foundry"
        assert script == "health_sentinel.py"
        assert explicit_plugin is False

    def test_mixed_forms_both_detected(self, tmp_path: Path) -> None:
        """Files using both the :- fallback and bare ${CLAUDE_PLUGIN_ROOT} form are each captured."""
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text(
            'python "${CLAUDE_PLUGIN_ROOT}/bin/a.py"\n"${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/b.sh"\n'
        )
        result = extract_bin_refs(skill_md, "foundry")
        scripts = {r[2] for r in result}
        assert scripts == {"a.py", "b.sh"}


# ---------------------------------------------------------------------------
# get_installed_versions + find_in_installed
# ---------------------------------------------------------------------------


class TestInstalledCache:
    def test_no_plugin_dir_returns_empty(self, tmp_path: Path) -> None:
        versions = get_installed_versions(tmp_path, "foundry")
        assert versions == []

    def test_finds_all_version_dirs(self, tmp_path: Path) -> None:
        (tmp_path / "foundry" / "0.15.0").mkdir(parents=True)
        (tmp_path / "foundry" / "0.16.0").mkdir(parents=True)
        (tmp_path / "foundry" / "0.17.0").mkdir(parents=True)
        versions = get_installed_versions(tmp_path, "foundry")
        names = [v.name for v in versions]
        assert "0.17.0" in names
        assert "0.16.0" in names
        # newest first
        assert names[0] == "0.17.0"

    def test_find_in_installed_found(self, tmp_path: Path) -> None:
        ver_dir = tmp_path / "foundry" / "0.17.0"
        (ver_dir / "skills" / "audit" / "modes").mkdir(parents=True)
        target = ver_dir / "skills" / "audit" / "modes" / "upgrade.md"
        target.write_text("content")
        local_path = "plugins/cc_foundry/skills/audit/modes/upgrade.md"
        found, checked = find_in_installed(local_path, "foundry", tmp_path)
        assert found is True

    def test_find_in_installed_not_found(self, tmp_path: Path) -> None:
        (tmp_path / "foundry" / "0.17.0" / "skills").mkdir(parents=True)
        local_path = "plugins/cc_foundry/skills/audit/modes/upgrade.md"
        found, checked = find_in_installed(local_path, "foundry", tmp_path)
        assert found is False
        assert len(checked) >= 1

    def test_find_in_installed_no_cache(self, tmp_path: Path) -> None:
        # No foundry/ under cache_dir at all
        cache_dir = tmp_path / "empty_cache"
        cache_dir.mkdir()
        found, checked = find_in_installed("plugins/cc_foundry/bin/foo.py", "foundry", cache_dir)
        assert found is False
        assert checked == []


# ---------------------------------------------------------------------------
# is_basename_grep_visible
# ---------------------------------------------------------------------------


class TestIsBasenameGrepVisible:
    def test_visible_in_skill_md(self, tmp_path: Path) -> None:
        (tmp_path / "skills" / "audit").mkdir(parents=True)
        skill = tmp_path / "skills" / "audit" / "SKILL.md"
        skill.write_text("loads: upgrade.md via $AUDIT_TPL/../modes/upgrade.md\n")
        assert is_basename_grep_visible("upgrade.md", tmp_path) is True

    def test_not_visible_anywhere(self, tmp_path: Path) -> None:
        (tmp_path / "skills" / "audit").mkdir(parents=True)
        skill = tmp_path / "skills" / "audit" / "SKILL.md"
        skill.write_text("$AUDIT_TPL/../modes/adversarial.md only via variable\n")
        # adversarial.md does NOT appear as a literal string — it appears embedded in path
        # but the basename "adversarial.md" IS present in the string above
        assert is_basename_grep_visible("adversarial.md", tmp_path) is True

    def test_truly_invisible(self, tmp_path: Path) -> None:
        (tmp_path / "skills" / "audit").mkdir(parents=True)
        skill = tmp_path / "skills" / "audit" / "SKILL.md"
        skill.write_text("completely unrelated content\n")
        assert is_basename_grep_visible("mystery.md", tmp_path) is False


# ---------------------------------------------------------------------------
# R1 integration
# ---------------------------------------------------------------------------


class TestRunR1:
    def test_pass_when_both_exist(self, tmp_path: Path) -> None:
        plugins_dir, plugin_dir = _make_plugin_tree(tmp_path)
        cache_dir = _make_cache(tmp_path)

        # Create file locally
        local_target = plugin_dir / "skills" / "audit" / "modes" / "upgrade.md"
        local_target.write_text("content")

        # Create same file in installed cache
        installed_target = tmp_path / "cache" / "foundry" / "0.17.0" / "skills" / "audit" / "modes" / "upgrade.md"
        installed_target.parent.mkdir(parents=True, exist_ok=True)
        installed_target.write_text("content")

        # SKILL.md referencing it via computed path
        skill_md = plugin_dir / "skills" / "audit" / "SKILL.md"
        skill_md.write_text('UPGRADE_MD="$AUDIT_TPL/../modes/upgrade.md"\n')

        findings = run_computed_path_duality(plugins_dir, cache_dir)
        fails = [f for f in findings if f.severity == Severity.FAIL]
        assert fails == [], f"Expected no FAIL findings, got: {fails}"

    def test_warn_not_fail_when_local_only(self, tmp_path: Path) -> None:
        """Local-only is advisory: the cache is machine state, so a not-yet-released file must never block the commit
        that releases it."""
        plugins_dir, plugin_dir = _make_plugin_tree(tmp_path)
        cache_dir = _make_cache(tmp_path)

        # File exists locally but NOT in installed cache
        local_target = plugin_dir / "skills" / "audit" / "modes" / "upgrade.md"
        local_target.write_text("content")

        skill_md = plugin_dir / "skills" / "audit" / "SKILL.md"
        skill_md.write_text('UPGRADE_MD="$AUDIT_TPL/../modes/upgrade.md"\n')

        findings = run_computed_path_duality(plugins_dir, cache_dir)
        assert [f for f in findings if f.severity == Severity.FAIL] == []
        warns = [f for f in findings if f.severity == Severity.WARN]
        assert len(warns) >= 1
        assert "upgrade.md" in warns[0].resolved_local

    def test_warn_when_installed_only(self, tmp_path: Path) -> None:
        plugins_dir, plugin_dir = _make_plugin_tree(tmp_path)
        cache_dir = _make_cache(tmp_path)

        # File does NOT exist locally but IS in installed cache
        installed_target = tmp_path / "cache" / "foundry" / "0.17.0" / "skills" / "audit" / "modes" / "upgrade.md"
        installed_target.parent.mkdir(parents=True, exist_ok=True)
        installed_target.write_text("content")

        skill_md = plugin_dir / "skills" / "audit" / "SKILL.md"
        skill_md.write_text('UPGRADE_MD="$AUDIT_TPL/../modes/upgrade.md"\n')

        findings = run_computed_path_duality(plugins_dir, cache_dir)
        warns = [f for f in findings if f.severity == Severity.WARN]
        assert len(warns) >= 1

    def test_info_when_not_installed(self, tmp_path: Path) -> None:
        plugins_dir, plugin_dir = _make_plugin_tree(tmp_path)
        # Use a cache dir that has no plugin installed
        cache_dir = tmp_path / "empty_cache"
        cache_dir.mkdir()

        local_target = plugin_dir / "skills" / "audit" / "modes" / "upgrade.md"
        local_target.write_text("content")

        skill_md = plugin_dir / "skills" / "audit" / "SKILL.md"
        skill_md.write_text('UPGRADE_MD="$AUDIT_TPL/../modes/upgrade.md"\n')

        findings = run_computed_path_duality(plugins_dir, cache_dir)
        infos = [f for f in findings if f.severity == "INFO"]
        assert len(infos) >= 1

    def test_cross_plugin_literal_ref_resolves_target_not_source_plugin(self, tmp_path: Path) -> None:
        """Regression: a literal `plugins/<other>/...md` ref must resolve against the REFERENCED plugin's cache, not the
        SOURCE file's own plugin — even when plugins_dir is absolute (as main() always passes it via .resolve()).

        Prior bug: target_folder was derived via
        `Path(ref.resolved_local).relative_to(plugins_dir).parts[0]`, which always
        raised ValueError when plugins_dir was absolute (resolved_local is stored
        relative to project root per the PathRef contract), silently falling back
        to `ref.plugin` — the file's OWN plugin, not the one actually referenced.
        Same-plugin refs never expose this (source == target by coincidence); a
        genuine cross-plugin literal reference does.
        """
        plugins_dir, research_dir = _make_plugin_tree(tmp_path, plugin="research")
        _, foundry_dir = _make_plugin_tree(tmp_path, plugin="foundry")
        cache_dir = _make_cache(tmp_path, plugin="foundry", version="0.17.0")
        # foundry is "installed"; research is not — isolates the assertion to
        # whether the foundry-side lookup used the right plugin identity.
        active_install_paths = {"foundry": cache_dir / "foundry" / "0.17.0"}

        # Referenced file exists locally (foundry) and in its own installed cache.
        target_local = foundry_dir / "agents" / "web-explorer.md"
        target_local.write_text("content")
        target_installed = cache_dir / "foundry" / "0.17.0" / "agents" / "web-explorer.md"
        target_installed.parent.mkdir(parents=True, exist_ok=True)
        target_installed.write_text("content")

        # research's own file has a literal cross-plugin reference into foundry.
        source_md = research_dir / "agents" / "data-steward.md"
        source_md.write_text("See `plugins/cc_foundry/agents/web-explorer.md` for details.\n")

        findings = run_computed_path_duality(plugins_dir.resolve(), cache_dir, active_install_paths)
        fails = [f for f in findings if f.severity == Severity.FAIL and "web-explorer.md" in f.raw_expr]
        assert fails == [], f"web-explorer.md exists in both local and its own installed cache: {fails}"


# ---------------------------------------------------------------------------
# R2 integration
# ---------------------------------------------------------------------------


class TestRunR2:
    def test_pass_when_basename_visible(self, tmp_path: Path) -> None:
        plugins_dir, plugin_dir = _make_plugin_tree(tmp_path)

        # Create a mode file
        mode_file = plugin_dir / "skills" / "audit" / "modes" / "upgrade.md"
        mode_file.write_text("# Upgrade mode")

        # Consumer SKILL.md mentions the basename explicitly
        skill_md = plugin_dir / "skills" / "audit" / "SKILL.md"
        skill_md.write_text('UPGRADE_MD="$AUDIT_TPL/../modes/upgrade.md"\n')

        findings = run_orphan_risk_detection(plugins_dir)
        assert findings == []

    def test_orphan_risk_when_basename_invisible(self, tmp_path: Path) -> None:
        plugins_dir, plugin_dir = _make_plugin_tree(tmp_path)

        # Create mode file
        mode_file = plugin_dir / "skills" / "audit" / "modes" / "secret.md"
        mode_file.write_text("# Secret mode — never referenced by basename")

        # SKILL.md does not contain "secret.md" anywhere
        skill_md = plugin_dir / "skills" / "audit" / "SKILL.md"
        skill_md.write_text("completely different content\n")

        findings = run_orphan_risk_detection(plugins_dir)
        assert len(findings) >= 1
        assert any("secret.md" in f.basename for f in findings)

    def test_shared_file_checked(self, tmp_path: Path) -> None:
        plugins_dir, plugin_dir = _make_plugin_tree(tmp_path)

        shared_file = plugin_dir / "skills" / "_shared" / "task-hygiene.md"
        shared_file.write_text("task hygiene content")

        # No reference to task-hygiene.md anywhere
        skill_md = plugin_dir / "skills" / "audit" / "SKILL.md"
        skill_md.write_text("nothing relevant\n")

        findings = run_orphan_risk_detection(plugins_dir)
        orphans = [f for f in findings if f.basename == "task-hygiene.md"]
        assert len(orphans) >= 1


# ---------------------------------------------------------------------------
# R3 integration
# ---------------------------------------------------------------------------


class TestRunR3:
    def test_pass_when_script_exists_both(self, tmp_path: Path) -> None:
        plugins_dir, plugin_dir = _make_plugin_tree(tmp_path)
        cache_dir = _make_cache(tmp_path)

        # Script exists locally
        script = plugin_dir / "bin" / "check_orphaned_bin.py"
        script.write_text("# script")

        # Script exists in installed cache
        cached_script = tmp_path / "cache" / "foundry" / "0.17.0" / "bin" / "check_orphaned_bin.py"
        cached_script.parent.mkdir(parents=True, exist_ok=True)
        cached_script.write_text("# script")

        skill_md = plugin_dir / "skills" / "audit" / "SKILL.md"
        skill_md.write_text(
            'python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_orphaned_bin.py" --plugins-dir plugins\n'
        )

        findings = run_bin_ref_integrity(plugins_dir, cache_dir)
        assert findings == []

    def test_fail_when_missing_locally(self, tmp_path: Path) -> None:
        plugins_dir, plugin_dir = _make_plugin_tree(tmp_path)
        cache_dir = _make_cache(tmp_path)

        # Script NOT created locally
        skill_md = plugin_dir / "skills" / "audit" / "SKILL.md"
        skill_md.write_text('python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/missing_script.py"\n')

        findings = run_bin_ref_integrity(plugins_dir, cache_dir)
        fails = [f for f in findings if f.severity == Severity.FAIL]
        assert len(fails) >= 1
        assert fails[0].script_name == "missing_script.py"

    def test_warn_when_missing_from_cache(self, tmp_path: Path) -> None:
        plugins_dir, plugin_dir = _make_plugin_tree(tmp_path)
        cache_dir = _make_cache(tmp_path)

        # Script exists locally but NOT in cache
        script = plugin_dir / "bin" / "new_script.py"
        script.write_text("# new script — not yet in installed cache")

        skill_md = plugin_dir / "skills" / "audit" / "SKILL.md"
        skill_md.write_text('python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/new_script.py"\n')

        findings = run_bin_ref_integrity(plugins_dir, cache_dir)
        warns = [f for f in findings if f.severity == Severity.WARN]
        assert len(warns) >= 1
        assert warns[0].script_name == "new_script.py"

    def test_skip_when_plugin_dir_absent(self, tmp_path: Path) -> None:
        """A ref into a non-existent plugin dir (placeholder like `myplugin`) is skipped, not FAILed."""
        plugins_dir, plugin_dir = _make_plugin_tree(tmp_path)
        cache_dir = _make_cache(tmp_path)

        # Illustrative placeholder plugin with no directory on disk (e.g. authoring-guide example).
        skill_md = plugin_dir / "skills" / "audit" / "SKILL.md"
        skill_md.write_text('python "${CLAUDE_PLUGIN_ROOT:-plugins/myplugin}/bin/resolve.py"\n')

        findings = run_bin_ref_integrity(plugins_dir, cache_dir)
        assert findings == []


# ---------------------------------------------------------------------------
# format_results
# ---------------------------------------------------------------------------


class TestFormatResults:
    def test_all_pass_message(self) -> None:
        results = CheckResults()
        report, exit_code = format_results(results, {"R1", "R2", "R3"})
        assert exit_code == 0
        assert "✓: Check R1" in report
        assert "✓: Check R2" in report
        assert "✓: Check R3" in report

    @pytest.mark.parametrize(
        ("severity", "expected_exit", "expected_fragment"),
        [
            pytest.param(Severity.FAIL, 1, "R1-FAIL", id="severity.fail"),
            pytest.param(Severity.WARN, 0, "R1-WARN", id="severity.warn"),
            pytest.param(Severity.INFO, 0, "reference(s) skipped", id="severity.info"),
        ],
    )
    def test_r1_exit_code_by_severity(self, severity: Severity, expected_exit: int, expected_fragment: str) -> None:
        results = CheckResults()
        results.r1 = [
            R1Finding(
                severity=severity,
                source_file="plugins/cc_foundry/skills/audit/SKILL.md",
                raw_expr="$AUDIT_TPL/../modes/upgrade.md",
                resolved_local="plugins/cc_foundry/skills/audit/modes/upgrade.md",
                exists_locally=severity != Severity.WARN,
                installed_versions=["~/.claude/plugins/cache/borda-ai-rig/foundry/0.17.0"],
                exists_installed=severity != Severity.FAIL,
                message=f"R1-{severity.value}: test",
            )
        ]
        report, exit_code = format_results(results, {"R1", "R2", "R3"})
        assert exit_code == expected_exit
        assert expected_fragment in report

    def test_r2_orphan_no_exit_1(self) -> None:
        # R2 findings are warnings, not hard failures
        results = CheckResults()
        results.r2 = [
            R2Finding(
                source_file="plugins/cc_foundry/skills/audit/modes/upgrade.md",
                plugin="foundry",
                basename="upgrade.md",
                message="R2-ORPHAN-RISK: test",
            )
        ]
        report, exit_code = format_results(results, {"R1", "R2", "R3"})
        # R2 alone does not set exit code to 1 (R3 FAIL does; R2 is informational)
        assert exit_code == 0
        assert "R2-ORPHAN-RISK" in report

    @pytest.mark.parametrize(
        ("severity", "expected_exit"),
        [pytest.param(Severity.FAIL, 1, id="severity.fail"), pytest.param(Severity.WARN, 0, id="severity.warn")],
    )
    def test_r3_exit_code_by_severity(self, severity: Severity, expected_exit: int) -> None:
        results = CheckResults()
        results.r3 = [
            R3Finding(
                severity=severity,
                source_file="plugins/cc_foundry/skills/audit/SKILL.md",
                script_name="missing.py",
                plugin="foundry",
                local_path="plugins/cc_foundry/bin/missing.py",
                exists_locally=severity != Severity.FAIL,
                exists_installed=severity != Severity.WARN,
                message=f"R3-{severity.value}: test",
            )
        ]
        report, exit_code = format_results(results, {"R1", "R2", "R3"})
        assert exit_code == expected_exit
        assert f"R3-{severity.value}" in report

    def test_selective_checks_respected(self) -> None:
        results = CheckResults()
        report, _ = format_results(results, {"R1"})
        assert "Check R1" in report
        assert "Check R2" not in report
        assert "Check R3" not in report


# ---------------------------------------------------------------------------
# CLI main
# ---------------------------------------------------------------------------


class TestMain:
    def test_invalid_plugins_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.chdir(tmp_path)
        exit_code = main(["--plugins-dir", str(tmp_path / "nonexistent")])
        assert exit_code == 2
        captured = capsys.readouterr()
        assert "not a directory" in captured.err

    def test_invalid_check_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.chdir(tmp_path)
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        exit_code = main(["--plugins-dir", str(plugins_dir), "--check", "R4"])
        assert exit_code == 2
        captured = capsys.readouterr()
        assert "unknown check" in captured.err

    def test_empty_plugins_dir_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.chdir(tmp_path)
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        exit_code = main(["--plugins-dir", str(plugins_dir), "--cache-dir", str(cache_dir)])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "✓" in captured.out

    @pytest.mark.skipif(not _HAS_PROJECT_PLUGIN_TREE, reason="requires project-root plugins/ tree")
    def test_real_plugins_dir_no_crash(self) -> None:
        """Smoke test against the actual plugins/ directory.

        Should not crash.
        """
        real_plugins = Path(__file__).resolve().parent.parent.parent  # plugins/
        # Use a nonexistent cache dir so no installed-state comparisons are made
        cache_dir = Path("/tmp/nonexistent_cache_dir_for_test")
        exit_code = main(
            [
                "--plugins-dir",
                str(real_plugins),
                "--cache-dir",
                str(cache_dir),
                "--check",
                "R2",  # R2 only — no filesystem-state dependency on installed cache
            ]
        )
        # Exit code may be 0 or 1 depending on current state; must not be 2 (arg error)
        assert exit_code in (0, 1)
