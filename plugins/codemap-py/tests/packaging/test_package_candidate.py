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
from pathlib import Path

import pytest


_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_BUILDER = _PLUGIN_ROOT / "scripts" / "build_package.py"
if str(_BUILDER.parent) not in sys.path:
    sys.path.insert(0, str(_BUILDER.parent))

import build_package as builder  # noqa: E402  (needs the scripts path insert above)

_TEXT_LAUNCHERS = (
    "bin/check-index-currency",
    "bin/codemap-py",
    "bin/codemap-py.cmd",
    "bin/scan-index",
    "bin/scan-query",
)
_TEXT_PACKAGE_METADATA = ("LICENSE", "NOTICE")


def _plugin_identity() -> tuple[str, str]:
    """Return ``(name, version)`` from the tracked Claude manifest."""
    manifest = json.loads((_PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    return manifest["name"], manifest["version"]


def _run_builder(*args: str) -> subprocess.CompletedProcess[str]:
    """Invoke the builder CLI under the current interpreter."""
    return subprocess.run(
        [sys.executable, str(_BUILDER), *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.fixture(name="package", scope="module")
def _package(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the package once via the CLI for read-only inspection."""
    out = tmp_path_factory.mktemp("package")
    result = _run_builder("--out", str(out))
    assert result.returncode == 0, result.stderr
    return out


# --- CLI + single-source identity ------------------------------------------


@pytest.mark.packaging
def test_build_exits_zero_and_writes_manifest(package: Path) -> None:
    """Manifest carries the tracked plugin identity and a non-empty file list."""
    name, version = _plugin_identity()
    manifest = json.loads((package / "package-manifest.json").read_text())
    assert manifest["name"] == name
    assert manifest["version"] == version
    assert len(manifest["files"]) >= 1


@pytest.mark.packaging
def test_manifest_version_is_not_hardcoded(package: Path) -> None:
    """The builder reads version from plugin.json rather than a literal."""
    _, version = _plugin_identity()
    assert json.loads((package / "package-manifest.json").read_text())["version"] == version


@pytest.mark.packaging
def test_check_flag_reports_deterministic(package: Path) -> None:
    """Accept a candidate matching a populated reference build."""
    result = _run_builder("--out", str(package), "--check")
    assert result.returncode == 0, result.stderr


# --- determinism -----------------------------------------------------------


@pytest.mark.integration
@pytest.mark.packaging
def test_two_builds_are_byte_identical(tmp_path: Path) -> None:
    """Two independent CLI builds produce byte-identical trees."""
    first, second = tmp_path / "a", tmp_path / "b"
    assert _run_builder("--out", str(first)).returncode == 0
    assert _run_builder("--out", str(second)).returncode == 0
    assert builder._tree_bytes(first) == builder._tree_bytes(second)


# --- manifest shape --------------------------------------------------------


@pytest.mark.packaging
def test_both_runtime_manifests_present(package: Path) -> None:
    """Both the Claude and Codex plugin manifests ship in the package."""
    assert (package / ".claude-plugin" / "plugin.json").is_file()
    assert (package / ".codex-plugin" / "plugin.json").is_file()


@pytest.mark.packaging
def test_manifest_pair_shares_identity(package: Path) -> None:
    """Claude and Codex manifests agree on name and version."""
    claude = json.loads((package / ".claude-plugin" / "plugin.json").read_text())
    codex = json.loads((package / ".codex-plugin" / "plugin.json").read_text())
    assert claude["name"] == codex["name"]
    assert claude["version"] == codex["version"]


@pytest.mark.packaging
def test_codex_manifest_ships_skill_roster_and_runtime_hooks(package: Path) -> None:
    """The Codex manifest advertises its skill roster and runtime-scoped hook configuration."""
    codex = json.loads((package / ".codex-plugin" / "plugin.json").read_text())
    assert codex["skills"] == "./codex-skills/"
    assert codex["hooks"] == "./hooks/codex-hooks.json"


@pytest.mark.packaging
def test_manifest_skill_rosters(package: Path) -> None:
    """The package manifest lists the same six skills for both Claude and Codex."""
    rosters = json.loads((package / "package-manifest.json").read_text())["skills"]
    expected = {
        "scan-codebase",
        "query-code",
        "test-impact",
        "rename-refs",
        "integration",
        "debrief-coding",
    }
    assert set(rosters["claude"]) == expected
    assert set(rosters["codex"]) == expected


@pytest.mark.parametrize("absent_rel", ["skills", "hooks/hooks.json", "tests"])
@pytest.mark.packaging
def test_package_omits_paths(package: Path, absent_rel: str) -> None:
    """Default/source-only paths are excluded from the shipped package."""
    assert not (package / absent_rel).exists()


# --- payload closure -------------------------------------------------------


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
def test_payload_includes_expected_members(package: Path, present_rel: str) -> None:
    """The real tracked tree's load-bearing members ship in the package."""
    assert (package / present_rel).is_file()


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


@pytest.mark.packaging
def test_expected_python_hooks_ship(package: Path) -> None:
    """The shipped ``hooks/`` tree holds exactly the expected Python roster, no JavaScript."""
    shipped = {p.name for p in (package / "hooks").glob("*.py")}
    assert sorted(_EXPECTED_HOOK_PY - shipped) == [], "expected hook helpers missing from the package"
    assert sorted(shipped - _EXPECTED_HOOK_PY) == [], "unexpected hook helpers in the package"
    assert list((package / "hooks").glob("*.js")) == []


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
def test_manifest_exec_flag_is_platform_neutral(package: Path, member: str, expected_exec: bool) -> None:
    """The manifest ``exec`` flag mirrors git's tracked mode, not host st_mode."""
    manifest = json.loads((package / "package-manifest.json").read_text())
    record = next(entry for entry in manifest["files"] if entry["path"] == member)
    assert record["exec"] is expected_exec


@pytest.mark.packaging
def test_hashed_text_inputs_have_platform_neutral_line_endings(package: Path) -> None:
    """Git attributes and packaged bytes must keep exact package hashes cross-platform."""
    text_inputs = (*_TEXT_LAUNCHERS, *_TEXT_PACKAGE_METADATA)
    result = subprocess.run(
        ["git", "check-attr", "text", "eol", "--", *text_inputs],
        cwd=str(_PLUGIN_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    expected_attributes = [line for member in text_inputs for line in (f"{member}: text: set", f"{member}: eol: lf")]
    assert result.stdout.splitlines() == expected_attributes
    for member in text_inputs:
        assert b"\r" not in (package / member).read_bytes(), member


@pytest.mark.packaging
def test_no_symlinks_in_payload(package: Path) -> None:
    """No payload member is a symlink."""
    assert [p for p in package.rglob("*") if p.is_symlink()] == []


@pytest.mark.packaging
def test_no_case_collisions(package: Path) -> None:
    """No two payload members collide under case folding."""
    folded: dict[str, str] = {}
    for path in package.rglob("*"):
        if not path.is_file():
            continue
        key = path.relative_to(package).as_posix().casefold()
        assert key not in folded, f"case collision: {key}"
        folded[key] = path.name


@pytest.mark.packaging
def test_no_source_tree_references(package: Path) -> None:
    """No payload member embeds the absolute source-root path."""
    needle = str(builder.SOURCE_ROOT).encode("utf-8")
    offenders = [
        p.relative_to(package).as_posix() for p in package.rglob("*") if p.is_file() and needle in p.read_bytes()
    ]
    assert offenders == []


@pytest.mark.packaging
def test_package_manifest_hashes_match(package: Path) -> None:
    """Every manifest SHA-256 matches its on-disk payload bytes."""
    manifest = json.loads((package / "package-manifest.json").read_text())
    for record in manifest["files"]:
        payload = (package / record["path"]).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == record["sha256"], record["path"]


# --- pure builder helpers (filesystem-independent) -------------------------


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
def test_is_excluded(relative: str, excluded: bool) -> None:
    """The exclusion predicate drops caches/state/tests but keeps real payload."""
    assert builder._is_excluded(relative) is excluded


@pytest.mark.packaging
def test_admit_rejects_case_collision() -> None:
    """Two payload paths differing only by case are rejected as a collision."""
    folded: set[str] = set()
    pairs: list[tuple[Path, str]] = []
    builder._admit(Path("Bin/Scan"), "Bin/Scan", folded, pairs)
    with pytest.raises(ValueError, match="case-colliding"):
        builder._admit(Path("bin/scan"), "bin/scan", folded, pairs)


# --- build hygiene gates (residuals #3, #5) --------------------------------


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


@pytest.mark.packaging
def test_builder_mutates_nothing_tracked(tmp_path: Path) -> None:
    """A build to an external dir leaves the tracked working tree byte-identical."""
    before = _git_porcelain()
    assert _run_builder("--out", str(tmp_path / "pkg")).returncode == 0
    assert _git_porcelain() == before


@pytest.mark.skipif(sys.platform.startswith("win"), reason="executable bit is unreliable off POSIX")
@pytest.mark.packaging
def test_check_flags_tampered_reference_exec_mode(tmp_path: Path) -> None:
    """Reject a reference file whose mode differs from its manifest entry."""
    out = tmp_path / "pkg"
    assert _run_builder("--out", str(out)).returncode == 0
    (out / "README.md").chmod(0o755)
    assert _run_builder("--out", str(out), "--check").returncode == 1
