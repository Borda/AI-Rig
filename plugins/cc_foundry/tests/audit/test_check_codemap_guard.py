"""Tests for ``bin/check_codemap_guard.py`` and the codemap guard contracts it enforces.

Two layers:

* unit tests for the checker, run against synthetic trees so they assert the *rule* and
  not the repository's current contents;
* contract tests over the real foundry agent files, pinning the two failure modes this
  work closed — hand-copied guards drifting apart, and module names derived by
  path-guessing instead of from the index.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from check_codemap_guard import (
    Guard,
    _scannable,
    check,
    guard_lines,
    invariant_findings,
    manifest_paths,
)
from resolve_centrality import match_module

_FOUNDRY = Path(__file__).resolve().parent.parent.parent
_PLUGINS = _FOUNDRY.parent
_REPO = _PLUGINS.parent

_GUARD_AGENTS = (
    "challenger.md",
    "doc-scribe.md",
    "perf-optimizer.md",
    "qa-specialist.md",
    "solution-architect.md",
    "sw-engineer.md",
)

_ANCHORED_LINE = '_IDX="${CODEMAP_INDEX_DIR:-$_ROOT/.cache/codemap}"'


def _tree(root: Path, rel: str, body: str) -> Path:
    """Create ``root/rel`` containing *body*, making parents as needed."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _tree_with_manifest(root: Path) -> None:
    """Seed a synthetic repo with a real ``propagate_shared.py`` so MANIFEST reads work."""
    bin_dir = root / "plugins" / "cc_foundry" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_FOUNDRY / "bin" / "propagate_shared.py", bin_dir / "propagate_shared.py")


class TestInvariants:
    """The two invariants that actually drifted in production."""

    def test_anchored_line_passes(self) -> None:
        assert invariant_findings("f.md", Guard("bash-preamble", ""), [(1, _ANCHORED_LINE)]) == []

    def test_cwd_anchored_line_flagged(self) -> None:
        """A bare ``.cache/codemap`` resolves against the CWD — false no_index from a subdir."""
        hits = [(7, '_IDX="${CODEMAP_INDEX_DIR:-.cache/codemap}"')]
        findings = invariant_findings("f.md", Guard("bash-preamble", ""), hits)
        assert findings == ["f.md:7 index dir not anchored to a project-root variable"]

    def test_sanitized_project_name_flagged(self) -> None:
        """The scanner writes the raw basename; a filtered name seeks a file never written."""
        hits = [(3, 'PROJ=$(basename "$_ROOT" | tr -cd "[:alnum:]")')]
        findings = invariant_findings("f.md", Guard("bash-preamble", ""), hits)
        assert findings == ["f.md:3 project name is sanitized; scanner writes the raw basename"]

    @pytest.mark.parametrize("shape", ["python-local", "index-copy", "doc-prose"])
    def test_non_bash_shapes_exempt(self, shape: str) -> None:
        """A Python copy resolves with pathlib and prose is not executable."""
        hits = [(1, 'index_dir = root / ".cache/codemap"')]
        assert invariant_findings("f.py", Guard(shape, ""), hits) == []


class TestScanScope:
    def test_provider_tree_excluded(self) -> None:
        """Codemap-py defines the layout consumers mirror; it is not a copy of itself."""
        assert not _scannable(_REPO / "plugins" / "codemap-py" / "src" / "x.py", _REPO)

    def test_checker_excludes_itself(self) -> None:
        assert not _scannable(_FOUNDRY / "bin" / "check_codemap_guard.py", _REPO)

    def test_tests_excluded(self) -> None:
        assert not _scannable(_FOUNDRY / "tests" / "audit" / "test_check_codemap_guard.py", _REPO)

    def test_consumer_markdown_scanned(self) -> None:
        assert _scannable(_FOUNDRY / "agents" / "sw-engineer.md", _REPO)

    def test_guard_lines_empty_for_provider_cli_consumer(self, tmp_path: Path) -> None:
        """The preferred shape — calling the CLI — never spells a path, so it never trips."""
        path = _tree(tmp_path, "x.md", "codemap-py query central --top 5\n")
        assert guard_lines(path) == []


class TestRegistryEnforcement:
    def test_unregistered_copy_fails(self, tmp_path: Path) -> None:
        """A new hand-written guard in an unknown file cannot be added silently."""
        _tree_with_manifest(tmp_path)
        _tree(tmp_path, "plugins/cc_new/skills/thing/SKILL.md", f"```bash\n{_ANCHORED_LINE}\n```\n")
        findings = check(tmp_path)
        assert any("cc_new/skills/thing/SKILL.md" in f and "unmanaged codemap guard" in f for f in findings)

    def test_registered_copy_passes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _tree_with_manifest(tmp_path)
        rel = "plugins/cc_new/skills/thing/SKILL.md"
        _tree(tmp_path, rel, f"```bash\n{_ANCHORED_LINE}\n```\n")
        monkeypatch.setattr("check_codemap_guard.REGISTRY", {rel: Guard("bash-preamble", "test")})
        assert check(tmp_path) == []

    def test_registered_copy_still_invariant_checked(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Registration buys a reason, not an exemption from the invariants."""
        _tree_with_manifest(tmp_path)
        rel = "plugins/cc_new/skills/thing/SKILL.md"
        _tree(tmp_path, rel, '```bash\n_IDX="${CODEMAP_INDEX_DIR:-.cache/codemap}"\n```\n')
        monkeypatch.setattr("check_codemap_guard.REGISTRY", {rel: Guard("bash-preamble", "test")})
        assert any("not anchored" in f for f in check(tmp_path))

    def test_stale_registry_entry_reported(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An entry whose file lost its guard must not linger as a phantom inventory row."""
        _tree_with_manifest(tmp_path)
        monkeypatch.setattr("check_codemap_guard.REGISTRY", {"plugins/cc_gone/x.md": Guard("bash-preamble", "test")})
        assert any("holds no codemap guard" in f for f in check(tmp_path))

    def test_manifested_copy_is_exempt(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """MANIFEST membership is read from propagate_shared.py, never restated here."""
        _tree_with_manifest(tmp_path)
        rel = "plugins/cc_develop/bin/codemap_resolve.py"
        _tree(tmp_path, rel, '# ".cache/codemap" and CODEMAP_INDEX_DIR\n')
        monkeypatch.setattr("check_codemap_guard.REGISTRY", {})
        assert check(tmp_path) == []


class TestManifestCoverage:
    """Pins the two MANIFEST entries this work added."""

    def test_codemap_resolve_pair_manifested(self) -> None:
        paths = manifest_paths(_FOUNDRY / "bin")
        assert "plugins/cc_develop/bin/codemap_resolve.py" in paths
        assert "plugins/cc_research/bin/codemap_resolve.py" in paths

    def test_resolve_centrality_copy_manifested(self) -> None:
        assert "plugins/cc_foundry/bin/resolve_centrality.py" in manifest_paths(_FOUNDRY / "bin")

    def test_foundry_resolve_centrality_matches_oss_canonical(self) -> None:
        foundry = (_FOUNDRY / "bin" / "resolve_centrality.py").read_bytes()
        canonical = (_PLUGINS / "cc_oss" / "bin" / "resolve_centrality.py").read_bytes()
        assert foundry == canonical


class TestAgentGuardContracts:
    """Contracts over the real foundry agent pre-flight blocks."""

    @pytest.mark.parametrize("agent", _GUARD_AGENTS)
    def test_no_sed_module_derivation(self, agent: str) -> None:
        """Path-guessing maps ``pkg/__init__.py`` to a module the index never had."""
        text = (_FOUNDRY / "agents" / agent).read_text(encoding="utf-8")
        assert "sed 's|^src/" not in text

    @pytest.mark.parametrize("agent", _GUARD_AGENTS)
    def test_modules_derived_from_index(self, agent: str) -> None:
        text = (_FOUNDRY / "agents" / agent).read_text(encoding="utf-8")
        assert "resolve_centrality.py" in text
        assert "--modules-only" in text

    def test_guard_preamble_identical_across_agents(self) -> None:
        """All six copies must stay one text; drift here is the whole finding."""
        preambles = set()
        for agent in _GUARD_AGENTS:
            lines = (_FOUNDRY / "agents" / agent).read_text(encoding="utf-8").splitlines()
            start = next(i for i, ln in enumerate(lines) if ln.startswith("_ROOT="))
            preambles.add("\n".join(lines[start : start + 4]))
        assert len(preambles) == 1

    @pytest.mark.parametrize("agent", _GUARD_AGENTS)
    def test_guard_is_root_anchored(self, agent: str) -> None:
        path = _FOUNDRY / "agents" / agent
        assert invariant_findings(agent, Guard("bash-preamble", ""), guard_lines(path)) == []


class TestQualityStackGuard:
    """The blast-radius snippet must gate on the index, not the binary alone."""

    def test_guard_requires_index_not_just_binary(self) -> None:
        """Allow command discovery without an index while rejecting later queries."""
        text = (_FOUNDRY / "skills" / "_shared" / "quality-stack.md").read_text(encoding="utf-8")
        assert '[ -f "${_IDX}/${PROJ}.json" ]' in text

    def test_guard_is_root_anchored(self) -> None:
        path = _FOUNDRY / "skills" / "_shared" / "quality-stack.md"
        assert invariant_findings("quality-stack.md", Guard("bash-preamble", ""), guard_lines(path)) == []


class TestModuleNameResolution:
    """The semantic bug the sed transform caused, pinned on foundry's own copy."""

    def test_package_init_maps_to_package(self) -> None:
        mods = [("pkg", "src/pkg/__init__.py"), ("pkg.auth", "src/pkg/auth.py")]
        assert match_module("src/pkg/__init__.py", mods) == "pkg"

    def test_unknown_file_yields_no_guessed_name(self) -> None:
        assert match_module("src/pkg/brand_new.py", [("pkg", "src/pkg/__init__.py")]) is None
