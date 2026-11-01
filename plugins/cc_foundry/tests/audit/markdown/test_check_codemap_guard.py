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
from check_codemap_guard import Guard, _scannable, check, guard_lines, invariant_findings, manifest_paths
from resolve_centrality import match_module

_FOUNDRY = Path(__file__).resolve().parents[3]
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
_UNANCHORED_LINE = '_IDX="${CODEMAP_INDEX_DIR:-.cache/codemap}"'
_NEW_SKILL = "plugins/cc_new/skills/thing/SKILL.md"
_QUALITY_STACK = _FOUNDRY / "skills" / "_shared" / "quality-stack.md"
_ROOT_ANCHORED_FILES = [pytest.param(_FOUNDRY / "agents" / agent, id=agent) for agent in _GUARD_AGENTS] + [
    pytest.param(_QUALITY_STACK, id="quality-stack.md")
]


def _tree(root: Path, rel: str, body: str) -> Path:
    """Create ``root/rel`` containing *body*, making parents as needed."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _agent_text(agent: str) -> str:
    """Return the text of one real foundry agent definition."""
    return (_FOUNDRY / "agents" / agent).read_text(encoding="utf-8")


def _preamble(agent: str) -> str:
    """Return the four-line guard preamble starting at the agent's ``_ROOT=`` line."""
    lines = _agent_text(agent).splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("_ROOT="))
    return "\n".join(lines[start : start + 4])


@pytest.fixture
def manifest_tree(tmp_path: Path) -> Path:
    """Synthetic repo seeded with a real ``propagate_shared.py`` so MANIFEST reads work; returns its root."""
    bin_dir = tmp_path / "plugins" / "cc_foundry" / "bin"
    bin_dir.mkdir(parents=True)
    shutil.copyfile(_FOUNDRY / "bin" / "propagate_shared.py", bin_dir / "propagate_shared.py")
    return tmp_path


class TestInvariants:
    """The two invariants that actually drifted in production."""

    def test_anchored_line_passes(self) -> None:
        """A bash-preamble line anchored to the project root yields no finding."""
        assert invariant_findings("f.md", Guard("bash-preamble", ""), [(1, _ANCHORED_LINE)]) == []

    @pytest.mark.parametrize(
        ("line_no", "text", "message"),
        [
            # A bare `.cache/codemap` resolves against the CWD, so a subdirectory run reports a false no_index.
            pytest.param(
                7,
                _UNANCHORED_LINE,
                "f.md:7 index dir not anchored to a project-root variable",
                id="cwd-anchored-index-dir",
            ),
            # The scanner writes the raw basename; a filtered name seeks a file that was never written.
            pytest.param(
                3,
                'PROJ=$(basename "$_ROOT" | tr -cd "[:alnum:]")',
                "f.md:3 project name is sanitized; scanner writes the raw basename",
                id="sanitized-project-name",
            ),
        ],
    )
    def test_drifted_line_is_flagged(self, line_no: int, text: str, message: str) -> None:
        """A bash-preamble line that drifted from either production invariant yields exactly one finding."""
        findings = invariant_findings("f.md", Guard("bash-preamble", ""), [(line_no, text)])

        assert findings == [message]

    @pytest.mark.parametrize("shape", ["python-local", "index-copy", "doc-prose"])
    def test_non_bash_shapes_exempt(self, shape: str) -> None:
        """A Python copy resolves with pathlib and prose is not executable, so neither is checked as bash."""
        hits = [(1, 'index_dir = root / ".cache/codemap"')]

        assert invariant_findings("f.py", Guard(shape, ""), hits) == []


class TestScanScope:
    """Which files the guard scan covers."""

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            # Codemap-py defines the layout consumers mirror; it is not a copy of itself.
            pytest.param(_REPO / "plugins" / "codemap-py" / "src" / "x.py", False, id="provider-tree-excluded"),
            pytest.param(_FOUNDRY / "bin" / "check_codemap_guard.py", False, id="checker-excludes-itself"),
            pytest.param(
                _FOUNDRY / "tests" / "audit" / "markdown" / "test_check_codemap_guard.py", False, id="tests-excluded"
            ),
            pytest.param(_FOUNDRY / "agents" / "sw-engineer.md", True, id="consumer-markdown-scanned"),
        ],
    )
    def test_scannable_path(self, path: Path, expected: bool) -> None:
        """Consumer files are scanned; the provider's own tree, the checker and test files are not."""
        assert bool(_scannable(path, _REPO)) is expected

    def test_guard_lines_empty_for_provider_cli_consumer(self, tmp_path: Path) -> None:
        """The preferred shape — calling the CLI — never spells a path, so it never trips."""
        path = _tree(tmp_path, "x.md", "codemap-py query central --top 5\n")

        assert guard_lines(path) == []


class TestRegistryEnforcement:
    """Every hand-written guard copy must be registered, and registration keeps the invariants."""

    def test_unregistered_copy_fails(self, manifest_tree: Path) -> None:
        """A new hand-written guard in an unknown file cannot be added silently."""
        _tree(manifest_tree, _NEW_SKILL, f"```bash\n{_ANCHORED_LINE}\n```\n")

        findings = check(manifest_tree)

        assert any("cc_new/skills/thing/SKILL.md" in f and "unmanaged codemap guard" in f for f in findings)

    def test_registered_copy_passes(self, manifest_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A guard copy listed in the registry with a reason produces no finding."""
        _tree(manifest_tree, _NEW_SKILL, f"```bash\n{_ANCHORED_LINE}\n```\n")
        monkeypatch.setattr("check_codemap_guard.REGISTRY", {_NEW_SKILL: Guard("bash-preamble", "test")})

        assert check(manifest_tree) == []

    def test_registered_copy_still_invariant_checked(
        self, manifest_tree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Registration buys a reason, not an exemption from the invariants.

        The registered file uses the CWD-relative index dir, so the anchoring finding still appears.
        """
        _tree(manifest_tree, _NEW_SKILL, f"```bash\n{_UNANCHORED_LINE}\n```\n")
        monkeypatch.setattr("check_codemap_guard.REGISTRY", {_NEW_SKILL: Guard("bash-preamble", "test")})

        assert any("not anchored" in f for f in check(manifest_tree))

    def test_stale_registry_entry_reported(self, manifest_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An entry whose file lost its guard must not linger as a phantom inventory row."""
        monkeypatch.setattr("check_codemap_guard.REGISTRY", {"plugins/cc_gone/x.md": Guard("bash-preamble", "test")})

        assert any("holds no codemap guard" in f for f in check(manifest_tree))

    def test_manifested_copy_is_exempt(self, manifest_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """MANIFEST membership is read from propagate_shared.py, never restated here."""
        _tree(manifest_tree, "plugins/cc_develop/bin/codemap_resolve.py", '# ".cache/codemap" and CODEMAP_INDEX_DIR\n')
        monkeypatch.setattr("check_codemap_guard.REGISTRY", {})

        assert check(manifest_tree) == []


class TestManifestRead:
    """MANIFEST is read as data: ``--root`` may point at a tree this checker audits."""

    def test_propagate_shared_is_never_imported(self, tmp_path: Path) -> None:
        """A ``propagate_shared.py`` that writes a marker on import must not run while its literal is read.

        The audited tree's file is untrusted input; the marker proves nothing executed, while the entries prove the
        literal was still read.
        """
        marker = tmp_path / "ran"
        (tmp_path / "propagate_shared.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n"
            "MANIFEST = [{'canonical': 'a.py', 'copies': ['b.py']}]\n",
            encoding="utf-8",
        )

        assert manifest_paths(tmp_path) == {"a.py", "b.py"}
        assert not marker.exists()

    def test_missing_propagate_shared_yields_no_exemptions(self, tmp_path: Path) -> None:
        """No readable ``propagate_shared.py`` means an empty set, so every guard copy then needs its registry entry."""
        assert manifest_paths(tmp_path) == set()


class TestManifestCoverage:
    """Pins the MANIFEST entries this work added and the byte-identical resolver copy."""

    @pytest.mark.parametrize(
        "path",
        [
            "plugins/cc_develop/bin/codemap_resolve.py",
            "plugins/cc_research/bin/codemap_resolve.py",
            "plugins/cc_foundry/bin/resolve_centrality.py",
        ],
    )
    def test_path_is_manifested(self, path: str) -> None:
        """Each propagated guard-related script is listed in foundry's MANIFEST, so the checker exempts it."""
        assert path in manifest_paths(_FOUNDRY / "bin")


class TestAgentGuardContracts:
    """Contracts over the real foundry agent pre-flight blocks."""

    @pytest.mark.parametrize("agent", _GUARD_AGENTS)
    def test_no_sed_module_derivation(self, agent: str) -> None:
        """No agent derives module names with ``sed``, which maps ``pkg/__init__.py`` to a name the index never had."""
        assert "sed 's|^src/" not in _agent_text(agent)

    @pytest.mark.parametrize("agent", _GUARD_AGENTS)
    def test_modules_derived_from_index(self, agent: str) -> None:
        """Changed-file module names come from the shared script, which resolves them through the index.

        The agent no longer re-derives them itself; the script's own tests pin the index-name behaviour
        (``pkg/__init__.py`` resolves to ``pkg``).
        """
        text = _agent_text(agent)

        assert text.count("bin/codemap_changed_modules.py") == 1
        assert "resolve_centrality.py" not in text

    def test_guard_preamble_identical_across_agents(self) -> None:
        """All six copies must stay one text; drift here is the whole finding."""
        assert len({_preamble(agent) for agent in _GUARD_AGENTS}) == 1


class TestRootAnchoring:
    """The real guard blocks anchor the index directory to the project root."""

    @pytest.mark.parametrize("path", _ROOT_ANCHORED_FILES)
    def test_guard_is_root_anchored(self, path: Path) -> None:
        """Every agent pre-flight and the quality-stack snippet pass the production invariants."""
        assert invariant_findings(path.name, Guard("bash-preamble", ""), guard_lines(path)) == []

    def test_quality_stack_requires_index_not_just_binary(self) -> None:
        """The blast-radius snippet gates on the index file, so command discovery works without an index.

        Gating on the binary alone would allow later queries to run against a missing index.
        """
        assert '[ -f "${_IDX}/${PROJ}.json" ]' in _QUALITY_STACK.read_text(encoding="utf-8")


class TestModuleNameResolution:
    """The semantic bug the sed transform caused, pinned on foundry's own copy."""

    @pytest.mark.parametrize(
        ("file", "modules", "expected"),
        [
            pytest.param(
                "src/pkg/__init__.py",
                [("pkg", "src/pkg/__init__.py"), ("pkg.auth", "src/pkg/auth.py")],
                "pkg",
                id="package-init-maps-to-package",
            ),
            pytest.param(
                "src/pkg/brand_new.py", [("pkg", "src/pkg/__init__.py")], None, id="unknown-file-yields-no-guessed-name"
            ),
        ],
    )
    def test_match_module(self, file: str, modules: list[tuple[str, str]], expected: str | None) -> None:
        """A file resolves to the index's module name, and an unindexed file resolves to nothing."""
        assert match_module(file, modules) == expected
