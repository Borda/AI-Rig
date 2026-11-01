"""Deterministic real-package builder contract.

Covers the builder CLI contract the install probes depend on, byte-for-byte determinism, single-source identity
(manifest version equals the tracked ``.claude-plugin/plugin.json`` version), and payload closure of the real tracked
tree — skills, hooks, bin, and scripts present; tests, default ``skills/``, and ``hooks/hooks.json`` absent; and no
references to the source tree, symlinks, or case collisions.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

import pytest
from _host_skips import skip_executable_mode_unavailable

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_BUILDER = _PLUGIN_ROOT / "scripts" / "build_package.py"
if str(_BUILDER.parent) not in sys.path:
    sys.path.insert(0, str(_BUILDER.parent))

import build_package as builder  # noqa: E402  (needs the scripts path insert above)
import validate_package as validator  # noqa: E402

_QUESTION_POLICY = "shared/codex-user-questions.md"
_QUESTION_ASSETS = (
    _QUESTION_POLICY,
    "shared/codex-user-questions-details.md",
    "shared/user_questions_mcp.py",
    ".codex-mcp.json",
)
_EXPECTED_SKILLS = ("scan-codebase", "query-code", "test-impact", "rename-refs", "integration", "debrief-coding")

_TEXT_LAUNCHERS = (
    "bin/check-index-currency",
    "bin/codemap-py",
    "bin/codemap-py.cmd",
    "bin/scan-index",
    "bin/scan-query",
)
_TEXT_PACKAGE_METADATA = ("LICENSE", "NOTICE")
_TEXT_INPUTS = (*_TEXT_LAUNCHERS, *_TEXT_PACKAGE_METADATA)


def _plugin_identity() -> tuple[str, str]:
    """Return ``(name, version)`` from the tracked Claude manifest."""
    manifest = json.loads((_PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    return manifest["name"], manifest["version"]


def _tracked_exec_modes() -> dict[str, bool]:
    """Return ``{tracked path: is executable}`` read straight from ``git ls-files --stage``."""
    tracked = subprocess.run(
        ["git", "ls-files", "--stage", "-z", "--", "."],
        cwd=_PLUGIN_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    records = [record.split("\t", 1) for record in tracked.stdout.split("\0") if record]
    return {relative: metadata.split()[0] == "100755" for metadata, relative in records}


def _run_builder(*args: str) -> subprocess.CompletedProcess[str]:
    """Build the declared candidate using tracked modes plus explicit new data assets."""
    modes = builder._git_exec_modes(_PLUGIN_ROOT)
    for relative in _QUESTION_ASSETS:
        modes.setdefault(relative, False)
    with tempfile.TemporaryDirectory() as directory:
        mode_map = Path(directory) / "candidate-modes.json"
        mode_map.write_text(json.dumps(modes), encoding="utf-8", newline="\n")
        return subprocess.run(
            [sys.executable, str(_BUILDER), *args, "--mode-map", str(mode_map)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )


def _git_porcelain() -> str:
    """Return repo-wide ``git status --porcelain`` output from the plugin tree."""
    return subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(_PLUGIN_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout


@pytest.fixture(name="package", scope="module")
def _package(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the package once via the CLI for read-only inspection."""
    out = tmp_path_factory.mktemp("package")
    result = _run_builder("--out", str(out))
    assert result.returncode == 0, result.stderr
    return out


@pytest.fixture(name="package_manifest")
def _package_manifest(package: Path) -> dict:
    """Return the parsed ``package-manifest.json`` of the shared build."""
    return json.loads((package / "package-manifest.json").read_text())


@pytest.fixture(name="question_package", scope="module")
def _question_package(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the candidate from a foreign working directory with modes taken directly from git; read-only afterwards."""
    base = tmp_path_factory.mktemp("candidate")
    modes = _tracked_exec_modes()
    for relative in _QUESTION_ASSETS:
        modes.setdefault(relative, False)
    mode_map = base / "candidate-modes.json"
    mode_map.write_text(json.dumps(modes), encoding="utf-8")
    output = base / "package"
    built = subprocess.run(
        [sys.executable, str(_BUILDER), "--out", str(output), "--mode-map", str(mode_map)],
        cwd=base,
        capture_output=True,
        text=True,
        check=False,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    return output


@pytest.mark.packaging
@pytest.mark.integration
class TestQuestionPolicyCandidate:
    """Installed skills and native MCP wiring resolve byte-exact declared assets outside the checkout."""

    @pytest.mark.parametrize("asset", _QUESTION_ASSETS)
    def test_asset_ships_byte_exact(self, question_package: Path, asset: str) -> None:
        """Each declared question-policy asset in the package is byte-identical to its tracked source.

        The new data files are not yet in git's index when a change is staged, so the builder takes them from the
        explicit mode map; a dropped or altered asset would leave installed skills pointing at stale instructions.
        """
        assert (question_package / asset).read_bytes() == (_PLUGIN_ROOT / asset).read_bytes()

    def test_codex_mcp_wiring_points_at_shipped_server(self, question_package: Path) -> None:
        """The Codex manifest's MCP config launches the shipped stdio server from the plugin root.

        The wiring must name the packaged ``user_questions_mcp.py`` relative to the plugin, and that file must exist,
        otherwise the native question tool is declared but cannot start after install.
        """
        codex = json.loads((question_package / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
        config = question_package / codex["mcpServers"].removeprefix("./")

        wiring = json.loads(config.read_text(encoding="utf-8"))["mcpServers"]["codemap-input"]

        assert wiring["command"] == "python"
        assert wiring["args"] == ["shared/user_questions_mcp.py", "--stdio"]
        assert wiring["cwd"] == "."
        assert (question_package / wiring["args"][0].removeprefix("${PLUGIN_ROOT}/")).is_file()

    def test_package_validates_clean(self, question_package: Path) -> None:
        """The shipped package passes ``validate_package`` with no findings.

        The validator is the install-time gate; the candidate built with the question assets must satisfy it.
        """
        assert validator.validate_package(question_package) == []

    def test_ships_the_six_codex_skills(self, question_package: Path) -> None:
        """The package carries exactly the six Codex skills, each with a ``SKILL.md``."""
        skills = {path.parent.name for path in (question_package / "codex-skills").glob("*/SKILL.md")}

        assert skills == set(_EXPECTED_SKILLS)

    @pytest.mark.parametrize("skill_name", _EXPECTED_SKILLS)
    def test_skill_links_policy_document(self, question_package: Path, skill_name: str) -> None:
        """Each Codex skill references the question policy by a relative path that resolves inside the package.

        The skill is read from the installed bytes, so a broken ``../../shared/...`` link would leave the skill without
        its approval and question rules.
        """
        skill = question_package / "codex-skills" / skill_name / "SKILL.md"

        assert "../../" + _QUESTION_POLICY in skill.read_text(encoding="utf-8")
        assert (skill.parent / "../../" / _QUESTION_POLICY).resolve() == question_package / _QUESTION_POLICY


class TestManifest:
    """The package and plugin manifests carry the tracked identity and skill roster."""

    @pytest.mark.packaging
    def test_carries_tracked_plugin_identity(self, package_manifest: dict) -> None:
        """The package manifest has the tracked plugin name, the version read from ``plugin.json``, and files.

        The version comes from the single tracked manifest rather than a literal in the builder, so a version bump never
        needs a second edit and a stale hard-coded value shows up here.
        """
        name, version = _plugin_identity()

        assert (package_manifest["name"], package_manifest["version"]) == (name, version)
        assert len(package_manifest["files"]) >= 1

    @pytest.mark.parametrize("manifest_rel", [".claude-plugin/plugin.json", ".codex-plugin/plugin.json"])
    @pytest.mark.packaging
    def test_runtime_manifest_ships(self, package: Path, manifest_rel: str) -> None:
        """Both the Claude and Codex plugin manifests ship in the package."""
        assert (package / manifest_rel).is_file()

    @pytest.mark.packaging
    def test_runtime_manifests_share_identity(self, package: Path) -> None:
        """Claude and Codex manifests agree on name and version.

        The two runtimes install the same package, so a divergence would advertise two different plugin versions.
        """
        claude = json.loads((package / ".claude-plugin" / "plugin.json").read_text())
        codex = json.loads((package / ".codex-plugin" / "plugin.json").read_text())

        assert (claude["name"], claude["version"]) == (codex["name"], codex["version"])

    @pytest.mark.packaging
    def test_codex_manifest_advertises_skills_and_hooks(self, package: Path) -> None:
        """The Codex manifest advertises its skill roster and runtime-scoped hook configuration."""
        codex = json.loads((package / ".codex-plugin" / "plugin.json").read_text())

        assert (codex["skills"], codex["hooks"]) == ("./codex-skills/", "./hooks/codex-hooks.json")

    @pytest.mark.packaging
    def test_lists_same_six_skills_for_both_runtimes(self, package_manifest: dict) -> None:
        """The package manifest lists the same six skills for both Claude and Codex."""
        rosters = package_manifest["skills"]

        assert (set(rosters["claude"]), set(rosters["codex"])) == (set(_EXPECTED_SKILLS), set(_EXPECTED_SKILLS))

    @pytest.mark.parametrize(
        ("member", "expected_exec"),
        [
            pytest.param("bin/scan-index", True, id="executable-cli"),
            pytest.param("bin/scan-query", True, id="executable-query"),
            pytest.param("bin/codemap-py", True, id="executable-launcher"),
            pytest.param("bin/_schema.py", False, id="library-module"),
            pytest.param("README.md", False, id="document"),
        ],
    )
    @pytest.mark.packaging
    def test_exec_flag_is_platform_neutral(self, package_manifest: dict, member: str, expected_exec: bool) -> None:
        """The manifest ``exec`` flag mirrors git's tracked mode, not host st_mode."""
        record = next(entry for entry in package_manifest["files"] if entry["path"] == member)

        assert record["exec"] is expected_exec

    @pytest.mark.packaging
    def test_sha256_matches_payload_bytes(self, package: Path, package_manifest: dict) -> None:
        """Every manifest SHA-256 matches its on-disk payload bytes."""
        mismatched = [
            record["path"]
            for record in package_manifest["files"]
            if hashlib.sha256((package / record["path"]).read_bytes()).hexdigest() != record["sha256"]
        ]

        assert mismatched == []


class TestBuild:
    """The builder CLI is deterministic, self-checking, and leaves the working tree untouched."""

    @pytest.mark.packaging
    def test_check_flag_accepts_matching_reference(self, package: Path) -> None:
        """``--check`` accepts a candidate matching a populated reference build."""
        result = _run_builder("--out", str(package), "--check")

        assert result.returncode == 0, result.stderr

    @pytest.mark.integration
    @pytest.mark.packaging
    def test_two_builds_are_byte_identical(self, package: Path, tmp_path: Path) -> None:
        """Two independent CLI builds produce byte-identical trees.

        The shared module build is the first build; a second build into a fresh directory must match it byte for byte.
        """
        second = tmp_path / "b"
        assert _run_builder("--out", str(second)).returncode == 0

        assert builder._tree_bytes(package) == builder._tree_bytes(second)

    @pytest.mark.packaging
    def test_mutates_nothing_tracked(self, tmp_path: Path) -> None:
        """A build to an external dir leaves the tracked working tree byte-identical."""
        before = _git_porcelain()
        assert _run_builder("--out", str(tmp_path / "pkg")).returncode == 0

        assert _git_porcelain() == before

    @skip_executable_mode_unavailable
    @pytest.mark.packaging
    def test_check_flags_tampered_reference_exec_mode(self, tmp_path: Path) -> None:
        """Reject a reference file whose mode differs from its manifest entry."""
        out = tmp_path / "pkg"
        assert _run_builder("--out", str(out)).returncode == 0
        (out / "README.md").chmod(0o755)

        assert _run_builder("--out", str(out), "--check").returncode == 1

    @pytest.mark.parametrize(
        ("relative", "excluded"),
        [
            pytest.param("bin/__pycache__/x.pyc", True, id="pycache"),
            pytest.param("bin/foo.pyc", True, id="pyc-suffix"),
            pytest.param(".coverage.host", True, id="coverage"),
            pytest.param(".claude/state/x", True, id="dot-claude"),
            pytest.param("tests/test_x.py", True, id="tests-dir"),
            pytest.param("bin/scan-index", False, id="kept-cli"),
            pytest.param(".claude-plugin/plugin.json", False, id="kept-manifest"),
        ],
    )
    @pytest.mark.packaging
    def test_is_excluded(self, relative: str, excluded: bool) -> None:
        """The exclusion predicate drops caches/state/tests but keeps real payload."""
        assert builder._is_excluded(relative) is excluded

    @pytest.mark.packaging
    def test_admit_rejects_case_collision(self) -> None:
        """Two payload paths differing only by case are rejected as a collision."""
        folded: set[str] = set()
        pairs: list[tuple[Path, str]] = []
        builder._admit(Path("Bin/Scan"), "Bin/Scan", folded, pairs)

        with pytest.raises(ValueError, match="case-colliding"):
            builder._admit(Path("bin/scan"), "bin/scan", folded, pairs)


#: Every Python file the shipped ``hooks/`` tree is expected to contain. Pinned as a
#: named set compared by difference — NOT by an exact count — because the count form
#: ("exactly six") failed the moment a shared helper module was factored out of the
#: hooks, which is a change this test has no business blocking. Set difference keeps the
#: real guarantees (nothing silently disappears, nothing unexpected sneaks in) while
#: leaving the roster free to grow through a one-line edit here.
_EXPECTED_HOOK_PY = frozenset(
    {
        "_hookutil.py",
        "guard-redundant-scan.py",
        "inject-preamble.py",
        "log-skill-start.py",
        "log-tool-use.py",
        "record-exhausted.py",
        "seed-session.py",
    }
)


class TestPayload:
    """The package holds the load-bearing tracked members and nothing that must not ship."""

    @pytest.mark.parametrize("absent_rel", ["skills", "hooks/hooks.json", "tests"])
    @pytest.mark.packaging
    def test_omits_default_and_source_only_paths(self, package: Path, absent_rel: str) -> None:
        """Default and source-only paths are excluded from the shipped package."""
        assert not (package / absent_rel).exists()

    @pytest.mark.parametrize(
        "present_rel",
        [
            "claude-skills/query-code/SKILL.md",
            "claude-skills/_shared/codemap-context.md",
            "codex-skills/query-code/SKILL.md",
            "hooks/claude-hooks.json",
            "hooks/codex-hooks.json",
            "hooks/inject-preamble.py",
            "bin/scan-index",
            "bin/codemap-py",
            "scripts/codemap_py_cli.py",
            "README.md",
            "LICENSE",
            "NOTICE",
        ],
    )
    @pytest.mark.packaging
    def test_includes_expected_members(self, package: Path, present_rel: str) -> None:
        """The real tracked tree's load-bearing members ship in the package."""
        assert (package / present_rel).is_file()

    @pytest.mark.packaging
    def test_ships_expected_python_hooks_and_no_javascript(self, package: Path) -> None:
        """The shipped ``hooks/`` tree holds exactly the expected Python roster, no JavaScript."""
        shipped = {p.name for p in (package / "hooks").glob("*.py")}

        assert sorted(_EXPECTED_HOOK_PY - shipped) == [], "expected hook helpers missing from the package"
        assert sorted(shipped - _EXPECTED_HOOK_PY) == [], "unexpected hook helpers in the package"
        assert list((package / "hooks").glob("*.js")) == []

    @pytest.mark.packaging
    def test_git_attributes_force_lf_for_hashed_text_inputs(self) -> None:
        """Every hashed launcher and metadata file is marked ``text`` with ``eol=lf`` in git attributes.

        Exact package hashes stay identical across platforms only when git never rewrites these files' line endings.
        """
        result = subprocess.run(
            ["git", "check-attr", "text", "eol", "--", *_TEXT_INPUTS],
            cwd=str(_PLUGIN_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )

        expected = [line for member in _TEXT_INPUTS for line in (f"{member}: text: set", f"{member}: eol: lf")]
        assert result.stdout.splitlines() == expected

    @pytest.mark.packaging
    def test_hashed_text_inputs_have_no_carriage_returns(self, package: Path) -> None:
        """No packaged launcher or metadata file contains a carriage return."""
        with_cr = [member for member in _TEXT_INPUTS if b"\r" in (package / member).read_bytes()]

        assert with_cr == []

    @pytest.mark.packaging
    def test_has_no_symlinks(self, package: Path) -> None:
        """No payload member is a symlink."""
        assert [p for p in package.rglob("*") if p.is_symlink()] == []

    @pytest.mark.packaging
    def test_has_no_case_collisions(self, package: Path) -> None:
        """No two payload members collide under case folding."""
        folded = [p.relative_to(package).as_posix().casefold() for p in package.rglob("*") if p.is_file()]

        assert [key for key, count in Counter(folded).items() if count > 1] == []

    @pytest.mark.packaging
    def test_has_no_source_tree_references(self, package: Path) -> None:
        """No payload member embeds the absolute source-root path."""
        needle = str(builder.SOURCE_ROOT).encode("utf-8")
        offenders = [
            p.relative_to(package).as_posix() for p in package.rglob("*") if p.is_file() and needle in p.read_bytes()
        ]

        assert offenders == []
