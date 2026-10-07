"""Tests for check_routing_links.py — R1/R2/R3 path-duality checks."""

from __future__ import annotations

from pathlib import Path

import pytest

# conftest.py registers bin/ scripts as importable modules
from check_routing_links import (
    CheckResults,
    R1Finding,
    R2Finding,
    R3Finding,
    Severity,
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

_HAS_PROJECT_PLUGIN_TREE = (Path(__file__).resolve().parent.parent.parent.parent / "cc_foundry").is_dir()


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
# _resolve_computed_rel / _resolve_computed_abs
# ---------------------------------------------------------------------------


class TestResolveComputed:
    """The relative and absolute computed-path resolvers share one contract over their variable roots."""

    @pytest.mark.parametrize(
        ("resolver", "var", "relative", "parts"),
        [
            pytest.param(
                _resolve_computed_rel,
                "AUDIT_TPL",
                "modes/upgrade.md",
                ("cc_foundry", "skills", "audit", "modes", "upgrade.md"),
                id="relative-audit-templates",
            ),
            pytest.param(
                _resolve_computed_abs,
                "_FS",
                "task-hygiene.md",
                ("cc_foundry", "skills", "_shared", "task-hygiene.md"),
                id="absolute-shared-file",
            ),
        ],
    )
    def test_known_var_resolves_under_its_root(
        self, tmp_path: Path, resolver, var: str, relative: str, parts: tuple[str, ...]
    ) -> None:
        """A known variable plus a relative path resolves to the exact file under its plugin root."""
        plugins_dir = tmp_path / "plugins"
        assert resolver(var, relative, plugins_dir) == plugins_dir.joinpath(*parts).as_posix()

    @pytest.mark.parametrize(
        ("resolver", "var", "relative", "fragment"),
        [
            # The _FS root is skills/_shared; its parent is skills/, so the result is skills/modes/x.md.
            pytest.param(_resolve_computed_rel, "_FS", "../modes/x.md", "modes/x.md", id="relative-parent-escape"),
            pytest.param(
                _resolve_computed_abs,
                "_FOUNDRY_SHARED",
                "bin-authoring-guide.md",
                "bin-authoring-guide.md",
                id="foundry-shared",
            ),
        ],
    )
    def test_known_var_resolves_to_path_naming_the_file(
        self, tmp_path: Path, resolver, var: str, relative: str, fragment: str
    ) -> None:
        """A known variable resolves to some path that names the requested file."""
        result = resolver(var, relative, tmp_path / "plugins")
        assert result is not None
        assert fragment in result

    @pytest.mark.parametrize(
        ("resolver", "var"),
        [
            pytest.param(_resolve_computed_rel, "MYSTERY_VAR", id="relative"),
            pytest.param(_resolve_computed_abs, "NO_SUCH_VAR", id="absolute"),
        ],
    )
    def test_unknown_var_returns_none(self, tmp_path: Path, resolver, var: str) -> None:
        """A variable with no known plugin root cannot be resolved."""
        assert resolver(var, "foo.md", tmp_path) is None


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
    @pytest.mark.parametrize(
        ("text", "bin_plugin", "script", "explicit_plugin"),
        [
            pytest.param(
                'python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_orphaned_bin.py" --plugins-dir plugins\n',
                "foundry",
                "check_orphaned_bin.py",
                True,
                id="fallback-form-own-plugin",
            ),
            pytest.param(
                '"${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/codemap_scan.py"\n',
                "develop",
                "codemap_scan.py",
                True,
                id="fallback-form-cross-plugin",
            ),
            # ${CLAUDE_PLUGIN_ROOT}/bin/<script> with no :-fallback resolves to the owning plugin.
            pytest.param(
                'python "${CLAUDE_PLUGIN_ROOT}/bin/health_sentinel.py" start foo\n',
                "foundry",
                "health_sentinel.py",
                False,
                id="no-fallback-infers-owning-plugin",
            ),
        ],
    )
    def test_single_reference_names_plugin_and_script(
        self, tmp_path: Path, text: str, bin_plugin: str, script: str, explicit_plugin: bool
    ) -> None:
        """One bin/ reference yields the plugin it targets, the script, and whether the plugin was spelled out.

        Covers the ``:-plugins/cc_<name>`` fallback form for the owning and for another plugin, and the bare
        ``${CLAUDE_PLUGIN_ROOT}`` form, whose plugin is inferred rather than explicit.
        """
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text(text)
        result = extract_bin_refs(skill_md, "foundry")
        assert len(result) == 1
        assert result[0][1:] == (bin_plugin, script, explicit_plugin)

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param(
                '"${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/foo.py"\n'
                '"${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/bar.sh"\n',
                {"foo.py", "bar.sh"},
                id="fallback-form-only",
            ),
            pytest.param(
                'python "${CLAUDE_PLUGIN_ROOT}/bin/a.py"\n"${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/b.sh"\n',
                {"a.py", "b.sh"},
                id="bare-and-fallback-forms-mixed",
            ),
        ],
    )
    def test_every_script_reference_is_captured(self, tmp_path: Path, text: str, expected: set[str]) -> None:
        """Each bin/ script reference is captured, whichever of the two forms it uses."""
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text(text)
        assert {r[2] for r in extract_bin_refs(skill_md, "foundry")} == expected

    def test_deduplication(self, tmp_path: Path) -> None:
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text(
            '"${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/foo.py"\n"${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/foo.py"\n'
        )
        result = extract_bin_refs(skill_md, "foundry")
        assert len(result) == 1


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
        found, _checked = find_in_installed(local_path, "foundry", tmp_path)
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
    @pytest.mark.parametrize(
        ("content", "basename", "expected"),
        [
            pytest.param(
                "loads: upgrade.md via $AUDIT_TPL/../modes/upgrade.md\n", "upgrade.md", True, id="literal-in-skill"
            ),
            # The basename is embedded in a variable-built path, yet the literal string is still present.
            pytest.param(
                "$AUDIT_TPL/../modes/adversarial.md only via variable\n", "adversarial.md", True, id="embedded-in-path"
            ),
            pytest.param("completely unrelated content\n", "mystery.md", False, id="absent"),
        ],
    )
    def test_basename_visible_only_when_literally_present(
        self, tmp_path: Path, content: str, basename: str, expected: bool
    ) -> None:
        """A basename is grep-visible when its literal text appears in a SKILL.md, even inside a built path."""
        (tmp_path / "skills" / "audit").mkdir(parents=True)
        (tmp_path / "skills" / "audit" / "SKILL.md").write_text(content)
        assert is_basename_grep_visible(basename, tmp_path) is expected


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
        real_plugins = Path(__file__).resolve().parent.parent.parent.parent  # plugins/
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
