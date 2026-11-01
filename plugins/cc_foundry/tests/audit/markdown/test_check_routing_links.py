"""Tests for check_routing_links.py — R1/R2/R3 path-duality checks.

R1 compares a computed or literal path reference against the local tree and the installed plugin cache, R2 flags mode
and shared files whose basename no SKILL.md mentions, and R3 verifies ``bin/`` script references. All tests build
throwaway plugin and cache trees under ``tmp_path``; only the git-root tests run real ``git``, because resolving the
project root through git is the behaviour they cover.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest

# conftest.py registers bin/ scripts as importable modules
from check_routing_links import (
    CheckResults,
    R1Finding,
    R2Finding,
    R3Finding,
    Severity,
    _project_root,
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

_HAS_PROJECT_PLUGIN_TREE = (Path(__file__).resolve().parents[4] / "cc_foundry").is_dir()

_skip_git_unavailable = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

_UPGRADE_REF = 'UPGRADE_MD="$AUDIT_TPL/../modes/upgrade.md"\n'
_LOCAL_UPGRADE = "plugins/cc_foundry/skills/audit/modes/upgrade.md"
_CACHED_UPGRADE = "cache/foundry/0.17.0/skills/audit/modes/upgrade.md"
_FOUNDRY_BIN = '"${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/%s"\n'


def _init_repo(path: Path) -> Path:
    """Create ``path`` as an empty git repository and return it."""
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


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


def _touch(root: Path, *relative: str, content: str = "content") -> None:
    """Write ``content`` to each ``relative`` path under ``root``, creating parent directories."""
    for name in relative:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


@pytest.fixture
def plugin_tree(tmp_path: Path) -> tuple[Path, Path]:
    """Minimal ``plugins/cc_foundry`` tree under ``tmp_path``; yields ``(plugins_dir, plugin_dir)``."""
    return _make_plugin_tree(tmp_path)


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    """Plugin cache under ``tmp_path`` holding an installed foundry 0.17.0 skeleton."""
    return _make_cache(tmp_path)


@pytest.fixture
def skill_md(plugin_tree: tuple[Path, Path]) -> Path:
    """Path of the audit SKILL.md inside the minimal plugin tree (not yet written)."""
    return plugin_tree[1] / "skills" / "audit" / "SKILL.md"


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
        """A known variable plus a relative path resolves to the exact file under its plugin root.

        ``$AUDIT_TPL`` and ``$_FS`` each map to a fixed directory inside the plugin source tree; resolution appends the
        reference to that directory.
        """
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
        """A known variable resolves to some path that names the requested file.

        These roots depend on the environment (a parent escape, a shared directory), so only the requested file name is
        pinned.
        """
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
        """A variable with no known plugin root cannot be resolved.

        Guessing a root for an unknown variable would report false drift, so the resolvers return ``None``.
        """
        assert resolver(var, "foo.md", tmp_path) is None


class TestExtractPathRefs:
    """``extract_path_refs`` finds computed-path references in a SKILL.md and drops what it cannot resolve."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param(
                'UPGRADE_MD="$AUDIT_TPL/../modes/upgrade.md"\nADVERSARIAL_MD="$AUDIT_TPL/../modes/adversarial.md"\n',
                [("adversarial.md", "computed_rel"), ("upgrade.md", "computed_rel")],
                id="computed-relative",
            ),
            pytest.param(
                'Read "$_FS/task-hygiene.md"\nRead "$_FS/file-handoff-protocol.md"\n',
                [("file-handoff-protocol.md", "computed_abs"), ("task-hygiene.md", "computed_abs")],
                id="computed-absolute-read",
            ),
            pytest.param('Read "$UNKNOWN_VAR/something.md"\n', [], id="unknown-var-skipped"),
            pytest.param(
                'Read "$_FS/task-hygiene.md"\nRead "$_FS/task-hygiene.md"\n',
                [("task-hygiene.md", "computed_abs")],
                id="duplicate-expression-deduplicated",
            ),
        ],
    )
    def test_references_found_by_basename_and_type(
        self, plugin_tree: tuple[Path, Path], skill_md: Path, text: str, expected: list[tuple[str, str]]
    ) -> None:
        """Each resolvable computed reference is returned once, with its basename and relative/absolute type.

        Covers variable-relative references, ``Read``-style absolute ones, a variable with no known root (skipped) and
        the same expression repeated (deduplicated).
        """
        skill_md.write_text(text)
        refs = extract_path_refs(skill_md, "foundry", plugin_tree[0])
        assert sorted((r.target_basename, r.ref_type) for r in refs) == expected


class TestExtractBinRefs:
    """``extract_bin_refs`` finds ``bin/`` script references written with ``${CLAUDE_PLUGIN_ROOT}``."""

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
                ["bar.sh", "foo.py"],
                id="fallback-form-only",
            ),
            pytest.param(
                'python "${CLAUDE_PLUGIN_ROOT}/bin/a.py"\n"${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/b.sh"\n',
                ["a.py", "b.sh"],
                id="bare-and-fallback-forms-mixed",
            ),
            pytest.param(
                '"${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/foo.py"\n'
                '"${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/foo.py"\n',
                ["foo.py"],
                id="repeated-reference-deduplicated",
            ),
        ],
    )
    def test_every_script_reference_is_captured_once(self, tmp_path: Path, text: str, expected: list[str]) -> None:
        """Each bin/ script reference is captured once, whichever of the two forms it uses.

        Covers the fallback form alone, the bare and fallback forms mixed in one file, and the same reference repeated.
        """
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text(text)
        assert sorted(r[2] for r in extract_bin_refs(skill_md, "foundry")) == expected


class TestInstalledCache:
    """The installed-cache helpers list plugin versions newest first and look files up in them."""

    @pytest.mark.parametrize(
        ("versions", "expected"),
        [
            pytest.param((), [], id="no-plugin-dir"),
            pytest.param(("0.15.0", "0.16.0", "0.17.0"), ["0.17.0", "0.16.0", "0.15.0"], id="newest-first"),
        ],
    )
    def test_installed_versions_newest_first(
        self, tmp_path: Path, versions: tuple[str, ...], expected: list[str]
    ) -> None:
        """Every version directory is listed, newest first; a plugin with no cache directory lists none.

        Callers take the first entry as the current install, so ordering is part of the contract.
        """
        _touch(tmp_path, *(f"foundry/{version}/marker" for version in versions))
        assert [v.name for v in get_installed_versions(tmp_path, "foundry")] == expected

    @pytest.mark.parametrize(
        ("files", "found", "checked_any"),
        [
            pytest.param(("foundry/0.17.0/skills/audit/modes/upgrade.md",), True, True, id="file-in-version-dir"),
            pytest.param(("foundry/0.17.0/skills/placeholder",), False, True, id="version-dir-without-file"),
            pytest.param((), False, False, id="no-cache-for-plugin"),
        ],
    )
    def test_find_in_installed_reports_found_and_checked(
        self, tmp_path: Path, files: tuple[str, ...], found: bool, checked_any: bool
    ) -> None:
        """A local path is found when any installed version holds it, and the versions searched are reported.

        With no installed versions nothing is searched, so nothing is reported as checked.
        """
        _touch(tmp_path, *files)
        result_found, checked = find_in_installed(_LOCAL_UPGRADE, "foundry", tmp_path)
        assert result_found is found
        assert bool(checked) is checked_any


class TestIsBasenameGrepVisible:
    """A basename is visible to the grep-based orphan check when its text appears in a SKILL.md."""

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
        """A basename is grep-visible when its literal text appears in a SKILL.md, even inside a built path.

        A grep for the basename is how the orphan check finds consumers, so a variable-built path still counts as long
        as the file name itself appears.
        """
        _touch(tmp_path, "skills/audit/SKILL.md", content=content)
        assert is_basename_grep_visible(basename, tmp_path) is expected


class TestRunR1:
    """R1 compares computed path references against the local tree and the installed cache."""

    @pytest.mark.parametrize(
        ("files", "expected"),
        [
            pytest.param((_LOCAL_UPGRADE, _CACHED_UPGRADE), [], id="in-both-is-clean"),
            # Local-only is advisory: the cache is machine state, so a not-yet-released file must never block the
            # commit that releases it.
            pytest.param((_LOCAL_UPGRADE,), [Severity.WARN], id="local-only-warns-not-fails"),
            pytest.param((_CACHED_UPGRADE,), [Severity.WARN], id="installed-only-warns"),
        ],
    )
    def test_local_and_installed_presence_sets_severity(
        self,
        tmp_path: Path,
        plugin_tree: tuple[Path, Path],
        cache_dir: Path,
        skill_md: Path,
        files: tuple[str, ...],
        expected: list[Severity],
    ) -> None:
        """A referenced file present in both trees is clean; present in only one is a WARN, never a FAIL.

        The SKILL.md references ``upgrade.md`` through a computed path, and the file is created locally, in the
        installed 0.17.0 cache, or both.
        """
        _touch(tmp_path, *files)
        skill_md.write_text(_UPGRADE_REF)
        findings = run_computed_path_duality(plugin_tree[0], cache_dir)
        assert [f.severity for f in findings] == expected
        assert all("upgrade.md" in f.resolved_local for f in findings)

    def test_info_when_not_installed(self, tmp_path: Path, plugin_tree: tuple[Path, Path], skill_md: Path) -> None:
        """A plugin with no installed cache entry yields INFO, not a failure.

        There is nothing to compare against when the plugin was never installed, so the reference is only noted.
        """
        empty_cache = tmp_path / "empty_cache"
        empty_cache.mkdir()
        _touch(tmp_path, _LOCAL_UPGRADE)
        skill_md.write_text(_UPGRADE_REF)
        findings = run_computed_path_duality(plugin_tree[0], empty_cache)
        assert [f.severity for f in findings] == [Severity.INFO]

    def test_cross_plugin_literal_ref_resolves_target_not_source_plugin(self, tmp_path: Path) -> None:
        """A literal `plugins/<other>/...md` ref resolves against the REFERENCED plugin's cache, not the source's.

        Regression: it must hold even when plugins_dir is absolute (as main() always passes it via .resolve()). The
        prior bug derived the target folder via `Path(ref.resolved_local).relative_to(plugins_dir).parts[0]`, which
        always raised ValueError for an absolute plugins_dir (resolved_local is stored relative to the project root per
        the PathRef contract) and silently fell back to `ref.plugin`, the file's OWN plugin. Same-plugin refs never
        expose this; a genuine cross-plugin literal reference does.
        """
        plugins_dir, research_dir = _make_plugin_tree(tmp_path, plugin="research")
        _, foundry_dir = _make_plugin_tree(tmp_path, plugin="foundry")
        cache_dir = _make_cache(tmp_path, plugin="foundry", version="0.17.0")
        # foundry is "installed"; research is not — isolates the assertion to whether the foundry-side lookup used the
        # right plugin identity.
        active_install_paths = {"foundry": cache_dir / "foundry" / "0.17.0"}
        # The referenced file exists locally (foundry) and in its own installed cache.
        (foundry_dir / "agents" / "web-explorer.md").write_text("content")
        _touch(cache_dir, "foundry/0.17.0/agents/web-explorer.md")
        # research's own file holds a literal cross-plugin reference into foundry.
        source_md = research_dir / "agents" / "data-steward.md"
        source_md.write_text("See `plugins/cc_foundry/agents/web-explorer.md` for details.\n")

        findings = run_computed_path_duality(plugins_dir.resolve(), cache_dir, active_install_paths)

        fails = [f for f in findings if f.severity == Severity.FAIL and "web-explorer.md" in f.raw_expr]
        assert fails == [], f"web-explorer.md exists in both local and its own installed cache: {fails}"


class TestRunR2:
    """R2 flags mode and shared files whose basename no SKILL.md mentions."""

    @pytest.mark.parametrize(
        ("relative_file", "skill_text", "expected_orphans"),
        [
            pytest.param("skills/audit/modes/upgrade.md", _UPGRADE_REF, [], id="basename-visible"),
            pytest.param(
                "skills/audit/modes/secret.md",
                "completely different content\n",
                ["secret.md"],
                id="mode-file-basename-invisible",
            ),
            pytest.param(
                "skills/_shared/task-hygiene.md", "nothing relevant\n", ["task-hygiene.md"], id="shared-file-checked"
            ),
        ],
    )
    def test_orphan_risk_when_basename_invisible(
        self,
        plugin_tree: tuple[Path, Path],
        skill_md: Path,
        relative_file: str,
        skill_text: str,
        expected_orphans: list[str],
    ) -> None:
        """A mode or shared file is an orphan risk exactly when no SKILL.md mentions its basename.

        Covers a mode file named by the SKILL.md, a mode file the SKILL.md never names, and an unreferenced file under
        ``_shared``, which is checked the same way.
        """
        plugins_dir, plugin_dir = plugin_tree
        _touch(plugin_dir, relative_file)
        skill_md.write_text(skill_text)
        assert [f.basename for f in run_orphan_risk_detection(plugins_dir)] == expected_orphans


class TestRunR3:
    """R3 verifies that referenced ``bin/`` scripts exist locally and in the installed cache."""

    @pytest.mark.parametrize(
        ("files", "reference", "expected"),
        [
            pytest.param(
                ("plugins/cc_foundry/bin/check_orphaned_bin.py", "cache/foundry/0.17.0/bin/check_orphaned_bin.py"),
                _FOUNDRY_BIN % "check_orphaned_bin.py",
                [],
                id="in-both-is-clean",
            ),
            pytest.param(
                (),
                _FOUNDRY_BIN % "missing_script.py",
                [(Severity.FAIL, "missing_script.py")],
                id="missing-locally-fails",
            ),
            # Present locally but not yet in the installed cache: advisory only.
            pytest.param(
                ("plugins/cc_foundry/bin/new_script.py",),
                _FOUNDRY_BIN % "new_script.py",
                [(Severity.WARN, "new_script.py")],
                id="missing-from-cache-warns",
            ),
            # An illustrative placeholder plugin with no directory on disk (e.g. an authoring-guide example).
            pytest.param(
                (),
                '"${CLAUDE_PLUGIN_ROOT:-plugins/myplugin}/bin/resolve.py"\n',
                [],
                id="absent-plugin-dir-skipped",
            ),
        ],
    )
    def test_script_reference_severity(
        self,
        tmp_path: Path,
        plugin_tree: tuple[Path, Path],
        cache_dir: Path,
        skill_md: Path,
        files: tuple[str, ...],
        reference: str,
        expected: list[tuple[Severity, str]],
    ) -> None:
        """A referenced bin/ script is clean when present in both trees, WARN when only local, FAIL when absent locally.

        A reference into a plugin that has no directory at all is a placeholder and is skipped rather than failed.
        """
        _touch(tmp_path, *files, content="# script")
        skill_md.write_text(f"python {reference}")
        findings = run_bin_ref_integrity(plugin_tree[0], cache_dir)
        assert [(f.severity, f.script_name) for f in findings] == expected


class TestFormatResults:
    """``format_results`` renders the per-check report and derives the exit code."""

    def test_all_pass_message(self) -> None:
        """Findings-free results print a pass line for every selected check and exit 0.

        Each of R1, R2 and R3 must announce itself so a silent skip cannot pass as clean.
        """
        report, exit_code = format_results(CheckResults(), {"R1", "R2", "R3"})
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
        """An R1 FAIL exits 1; WARN and INFO exit 0 and still appear in the report.

        Only a hard failure may block the commit; advisory findings are printed but never change the exit code.
        """
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
        """An R2 orphan-risk finding is reported but never sets exit code 1.

        R2 findings are warnings, not hard failures: R3 FAIL drives the exit code and R2 is informational.
        """
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
        assert exit_code == 0
        assert "R2-ORPHAN-RISK" in report

    @pytest.mark.parametrize(
        ("severity", "expected_exit"),
        [pytest.param(Severity.FAIL, 1, id="severity.fail"), pytest.param(Severity.WARN, 0, id="severity.warn")],
    )
    def test_r3_exit_code_by_severity(self, severity: Severity, expected_exit: int) -> None:
        """An R3 FAIL exits 1; a WARN exits 0 and still appears in the report.

        A script missing locally blocks the commit; one merely absent from the installed cache does not.
        """
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
        """Only the selected checks appear in the report.

        Selecting R1 alone must not print R2 or R3 headings.
        """
        report, _ = format_results(CheckResults(), {"R1"})
        assert "Check R1" in report
        assert "Check R2" not in report
        assert "Check R3" not in report


class TestMain:
    """The CLI validates its arguments, guards the project root, and reports through exit codes."""

    @pytest.mark.parametrize(
        ("args", "stderr_fragment"),
        [
            pytest.param(["--plugins-dir", "nonexistent"], "not a directory", id="missing-plugins-dir"),
            pytest.param(["--plugins-dir", "plugins", "--check", "R4"], "unknown check", id="unknown-check-name"),
        ],
    )
    def test_invalid_arguments_exit_two(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
        args: list[str],
        stderr_fragment: str,
    ) -> None:
        """A non-directory --plugins-dir or an unknown --check name exits 2 with the reason on stderr.

        Argument errors must be distinguishable from check failures (exit 1) so pre-commit output points at the cause.
        """
        (tmp_path / "plugins").mkdir()
        monkeypatch.chdir(tmp_path)
        assert main(args) == 2
        assert stderr_fragment in capsys.readouterr().err

    def test_empty_plugins_dir_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """An empty plugins directory has nothing to report: exit 0 with the pass lines printed."""
        (tmp_path / "plugins").mkdir()
        (tmp_path / "cache").mkdir()
        monkeypatch.chdir(tmp_path)
        assert main(["--plugins-dir", str(tmp_path / "plugins"), "--cache-dir", str(tmp_path / "cache")]) == 0
        assert "✓" in capsys.readouterr().out

    @_skip_git_unavailable
    def test_plugins_dir_in_another_git_repo_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """A --plugins-dir that lives in a different git repository than the cwd is outside the project root.

        The root used to be derived from --plugins-dir itself, so any directory inside some repository counted as
        contained by that repository's own root and the guard never fired; it must be measured from the cwd.
        """
        project = _init_repo(tmp_path / "project")
        other_plugins = _init_repo(tmp_path / "other") / "plugins"
        other_plugins.mkdir()
        monkeypatch.chdir(project)
        assert main(["--plugins-dir", str(other_plugins)]) == 2
        assert "SECURITY" in capsys.readouterr().err

    @_skip_git_unavailable
    def test_plugins_dir_in_project_subdirectory_is_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Running from a subdirectory still accepts a --plugins-dir that sits under the repository root.

        The guard measures against the git top-level of the cwd rather than the cwd itself, so ``../plugins`` from a
        subdirectory of the project stays valid.
        """
        project = _init_repo(tmp_path / "project")
        (project / "plugins").mkdir()
        (project / "sub").mkdir()
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        monkeypatch.chdir(project / "sub")
        assert main(["--plugins-dir", str(project / "plugins"), "--cache-dir", str(cache_dir)]) == 0
        assert "SECURITY" not in capsys.readouterr().err

    @mock.patch("check_routing_links.subprocess.run", autospec=True, side_effect=FileNotFoundError("git"))
    def test_project_root_falls_back_to_cwd_when_git_is_missing(
        self, mock_run: mock.MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing git binary degrades the root to the current directory instead of crashing the check.

        The git lookup is simulated as absent; every other test of the root guard keeps git real.
        """
        monkeypatch.chdir(tmp_path)
        assert _project_root(30) == tmp_path.resolve()
        mock_run.assert_called()

    @pytest.mark.skipif(not _HAS_PROJECT_PLUGIN_TREE, reason="requires project-root plugins/ tree")
    def test_real_plugins_dir_no_crash(self, tmp_path: Path) -> None:
        """Smoke test against the actual plugins/ directory: the check may pass or fail but never errors out.

        The cache directory does not exist, so no installed-state comparison is made, and only R2 runs, so the outcome
        has no dependency on installed-cache state. Exit code may be 0 or 1 depending on the current tree; it must not
        be 2 (argument error).
        """
        real_plugins = Path(__file__).resolve().parents[4]
        exit_code = main(
            ["--plugins-dir", str(real_plugins), "--cache-dir", str(tmp_path / "no_cache"), "--check", "R2"]
        )
        assert exit_code in (0, 1)
