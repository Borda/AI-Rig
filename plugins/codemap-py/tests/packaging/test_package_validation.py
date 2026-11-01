"""Package validator contract.

Black-box tests for ``validate_package``: a well-formed synthetic package passes,
and each closure/portability/hygiene/declared-component/exec rule fires a named
finding when violated. The fixtures are hand-built (not produced by the real-tree
builder) so the validator's contract is exercised in isolation.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from _host_skips import skip_executable_mode_unavailable

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _PLUGIN_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import validate_package as validator  # noqa: E402  (needs the scripts path insert above)

_CLAUDE_MANIFEST = (
    b'{"name": "codemap-py", "version": "0.25.0", "skills": "./claude-skills/", "hooks": "./hooks/claude-hooks.json"}\n'
)
_CODEX_MANIFEST = (
    b'{"name": "codemap-py", "version": "0.25.0", "skills": "./codex-skills/", "hooks": "./hooks/codex-hooks.json"}\n'
)
_HOOKS_WIRING = (
    b'{"hooks": {"SessionStart": [{"hooks": [{"type": "command", '
    b'"command": "python \\"${CLAUDE_PLUGIN_ROOT}/hooks/seed-session.py\\""}]}]}}\n'
)

# (relative path, bytes, executable) — a complete, closed, well-formed package.
_MEMBERS: dict[str, tuple[bytes, bool]] = {
    ".claude-plugin/plugin.json": (_CLAUDE_MANIFEST, False),
    ".codex-plugin/plugin.json": (_CODEX_MANIFEST, False),
    "README.md": (b"# codemap-py\n", False),
    "LICENSE": (b"Apache-2.0\n", False),
    "NOTICE": (b"codemap-py\n", False),
    "CHANGELOG.md": (b"# Changelog\n", False),
    "claude-skills/scan-codebase/SKILL.md": (b"---\nname: scan-codebase\n---\n", False),
    "claude-skills/_shared/codemap-context.md": (b"shared loader\n", False),
    "codex-skills/scan-codebase/SKILL.md": (b"---\nname: scan-codebase\n---\n", False),
    "hooks/claude-hooks.json": (_HOOKS_WIRING, False),
    "hooks/codex-hooks.json": (_HOOKS_WIRING, False),
    "hooks/seed-session.py": (b"# seed session\n", False),
    "hooks/_hookutil.py": (b"# shared hook helpers\n", False),
    "bin/scan-index": (b"#!/usr/bin/env python3\nprint('index')\n", True),
    "bin/_schema.py": (b"SCAN_VERSION = 11\n", False),
}


def _write_valid_package(package: Path) -> None:
    """Materialize a well-formed package with a self-consistent manifest."""
    records: list[dict[str, object]] = []
    for relative, (data, executable) in _MEMBERS.items():
        disk = package / relative
        disk.parent.mkdir(parents=True, exist_ok=True)
        disk.write_bytes(data)
        disk.chmod(0o755 if executable else 0o644)
        records.append({"path": relative, "sha256": hashlib.sha256(data).hexdigest(), "exec": executable})
    manifest = {
        "schema": 2,
        "name": "codemap-py",
        "version": "0.25.0",
        "skills": {"claude": ["scan-codebase"], "codex": ["scan-codebase"]},
        "files": sorted(records, key=lambda record: record["path"]),
        "exclusions": ["__pycache__/", "tests/"],
    }
    (package / "package-manifest.json").write_bytes(
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )


def _mutate_manifest(package: Path, mutate) -> None:
    """Rewrite ``package-manifest.json`` after applying ``mutate`` to its dict."""
    path = package / "package-manifest.json"
    manifest = json.loads(path.read_text())
    mutate(manifest)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def _sync_hash(manifest: dict, path: str, data: bytes) -> None:
    """Update the recorded SHA-256 for ``path`` so only the targeted rule fires."""
    for record in manifest["files"]:
        if record["path"] == path:
            record["sha256"] = hashlib.sha256(data).hexdigest()


def _rewrite_member(package: Path, relative: str, data: bytes) -> None:
    """Replace an existing payload file's bytes and keep its manifest hash consistent."""
    (package / relative).write_bytes(data)
    _mutate_manifest(package, lambda m: _sync_hash(m, relative, data))


def _drop_member(package: Path, relative: str) -> None:
    """Delete a file and its manifest entry, leaving the rest self-consistent."""
    (package / relative).unlink()
    _mutate_manifest(package, lambda m: m.__setitem__("files", [r for r in m["files"] if r["path"] != relative]))


def _add_member(package: Path, relative: str, data: bytes) -> None:
    """Add a file and a matching manifest entry."""
    disk = package / relative
    disk.parent.mkdir(parents=True, exist_ok=True)
    disk.write_bytes(data)
    record = {"path": relative, "sha256": hashlib.sha256(data).hexdigest(), "exec": False}
    _mutate_manifest(package, lambda m: m["files"].append(record))


def _validate_findings(package: Path) -> list[str]:
    """Return the validator's findings for ``package``."""
    return validator.validate_package(package)


# Package mutators: each breaks exactly one validator rule on a fresh valid package.


def _remove_schema_file(package: Path) -> None:
    """Delete a manifested file from disk, keeping its manifest entry."""
    (package / "bin" / "_schema.py").unlink()


def _tamper_readme(package: Path) -> None:
    """Change ``README.md`` bytes without updating the recorded hash."""
    (package / "README.md").write_bytes(b"# tampered\n")


def _add_unmanifested_file(package: Path) -> None:
    """Drop a file on disk that the manifest does not list."""
    (package / "bin" / "stowaway.py").write_bytes(b"x = 1\n")


def _leak_home_path(package: Path) -> None:
    """Bake the build host's real home path into ``bin/_schema.py`` with a consistent hash."""
    _rewrite_member(package, "bin/_schema.py", f"root = '{Path.home()}/checkout/codemap-py'\n".encode())


def _plant_private_key(package: Path) -> None:
    """Put private-key header bytes into ``NOTICE`` with a consistent hash.

    Assembled from fragments so no literal key header sits in this source file.
    """
    marker = b"-----BEGIN " + b"RSA PRIVATE" + b" KEY-----"
    _rewrite_member(package, "NOTICE", marker + b"\nabc\n" + marker.replace(b"BEGIN", b"END") + b"\n")


def _add_default_hooks_json(package: Path) -> None:
    """Add the forbidden default ``hooks/hooks.json`` as a manifested member."""
    _add_member(package, "hooks/hooks.json", b"{}\n")


def _add_alias_symlink(package: Path) -> None:
    """Add a symlink next to the launcher."""
    (package / "bin" / "alias-link").symlink_to(package / "bin" / "scan-index")


def _add_rogue_skill(package: Path) -> None:
    """Add a manifested skill dir that the manifest roster does not declare."""
    _add_member(package, "claude-skills/rogue/SKILL.md", b"---\nname: rogue\n---\n")


def _empty_codex_roster(package: Path) -> None:
    """Empty the Codex roster so it diverges from the Claude roster."""
    _mutate_manifest(package, lambda m: m["skills"].__setitem__("codex", []))


def _make_schema_executable(package: Path) -> None:
    """Make a data file executable on disk while its manifest exec flag stays false."""
    (package / "bin" / "_schema.py").chmod(0o755)


@pytest.fixture(name="valid_package")
def _valid_package(tmp_path: Path) -> Path:
    """Create a well-formed package directory."""
    package = tmp_path / "pkg"
    package.mkdir()
    _write_valid_package(package)
    return package


@pytest.fixture(name="native_question_package")
def _native_question_package(valid_package: Path) -> Path:
    """Declare and materialize native question assets in an otherwise valid legacy package."""
    codex_path = valid_package / ".codex-plugin" / "plugin.json"
    codex = json.loads(codex_path.read_text(encoding="utf-8"))
    codex["mcpServers"] = "./.codex-mcp.json"
    _rewrite_member(valid_package, ".codex-plugin/plugin.json", (json.dumps(codex) + "\n").encode("utf-8"))
    wiring = {
        "mcpServers": {
            "codemap-input": {
                "command": "python",
                "cwd": ".",
                "args": ["shared/user_questions_mcp.py", "--stdio"],
                "tool_timeout_sec": 3600,
            }
        }
    }
    _add_member(valid_package, ".codex-mcp.json", (json.dumps(wiring) + "\n").encode("utf-8"))
    _add_member(valid_package, "shared/user_questions_mcp.py", b'"""Native question provider fixture."""\n')
    return valid_package


class TestValidPackage:
    """A well-formed package passes through both the API and the CLI."""

    @pytest.mark.packaging
    def test_has_no_findings(self, valid_package: Path):
        """A well-formed package yields zero findings.

        Baseline for every violation test below: each of them breaks one rule on this same package.
        """
        assert _validate_findings(valid_package) == []

    @pytest.mark.packaging
    def test_cli_exits_zero(self, valid_package: Path):
        """The validator CLI exits zero on a clean package."""
        assert validator.main(["--package", str(valid_package)]) == 0


class TestNamedFindings:
    """Each inventory, hygiene and closure violation yields its exact named finding."""

    @pytest.mark.parametrize(
        ("mutate", "finding"),
        [
            pytest.param(_remove_schema_file, "missing payload file: bin/_schema.py", id="missing-payload"),
            pytest.param(_tamper_readme, "modified payload file: README.md", id="modified-payload"),
            pytest.param(_add_unmanifested_file, "extra un-manifested file: bin/stowaway.py", id="extra-file"),
            pytest.param(
                _leak_home_path, "personal-path reference in payload: bin/_schema.py", id="personal-path-reference"
            ),
            pytest.param(_plant_private_key, "secret-like material in payload: NOTICE", id="secret-material"),
            pytest.param(
                _add_default_hooks_json, "forbidden default path present: hooks/hooks.json", id="forbidden-default-path"
            ),
        ],
    )
    @pytest.mark.packaging
    def test_reports_exact_finding(self, valid_package: Path, mutate: Callable[[Path], None], finding: str):
        """Each single-rule violation on a valid package is reported verbatim.

        Covers inventory drift (missing, modified, extra file), hygiene (home path, private key) and closure (a leaked
        default ``hooks/hooks.json``). The mutator keeps the rest of the package self-consistent.
        """
        mutate(valid_package)

        assert finding in _validate_findings(valid_package)

    @pytest.mark.parametrize(
        ("mutate", "prefix", "detail"),
        [
            pytest.param(_add_alias_symlink, "symlink in package:", "", id="symlink"),
            pytest.param(_add_rogue_skill, "claude roster ", "rogue", id="undeclared-roster-dir"),
            pytest.param(_empty_codex_roster, "codex roster [] != claude roster", "", id="codex-roster-mismatch"),
            pytest.param(
                _make_schema_executable,
                "exec flag mismatch",
                "bin/_schema.py",
                id="exec-flag-mismatch",
                marks=skip_executable_mode_unavailable,
            ),
        ],
    )
    @pytest.mark.packaging
    def test_reports_finding_with_prefix(
        self, valid_package: Path, mutate: Callable[[Path], None], prefix: str, detail: str
    ):
        """Findings whose full text carries variable detail are matched by prefix plus the named subject.

        Covers a symlink anywhere in the package, an on-disk skill dir missing from the roster, a Codex roster that
        diverges from the Claude roster, and an on-disk exec bit that disagrees with the manifest exec flag.
        """
        mutate(valid_package)

        assert any(item.startswith(prefix) and detail in item for item in _validate_findings(valid_package))

    @pytest.mark.parametrize("bad_path", ["/etc/passwd", "../escape.py", "C:\\Windows\\x"])
    @pytest.mark.packaging
    def test_non_relative_manifest_path(self, valid_package: Path, bad_path: str):
        """Absolute, parent-escaping, or drive-qualified manifest paths are flagged.

        The bogus entry is appended to a valid manifest, so the finding must name the offending path.
        """
        entry = {"path": bad_path, "sha256": "0" * 64, "exec": False}
        _mutate_manifest(valid_package, lambda m: m["files"].append(entry))

        findings = _validate_findings(valid_package)

        assert any(item.startswith(f"non-relative manifest path: {bad_path}") for item in findings)


class TestDeclaredComponentClosure:
    """Components the package declares (documents, hooks, skills, Codex pointers) must all be present."""

    @pytest.mark.packaging
    @pytest.mark.parametrize(
        ("member", "expected_finding"),
        [
            # Dropping a required document (and its manifest entry) is flagged as missing.
            pytest.param("CHANGELOG.md", "missing required member: CHANGELOG.md", id="required-document"),
            # A hook helper named by the wiring but absent from the package is flagged.
            pytest.param(
                "hooks/seed-session.py",
                "referenced hook helper missing: hooks/seed-session.py",
                id="referenced-hook-helper",
            ),
            # A Claude ``hooks`` pointer whose target file is absent is flagged.
            pytest.param(
                "hooks/claude-hooks.json",
                "claude hooks pointer file missing: hooks/claude-hooks.json",
                id="hooks-pointer-file",
            ),
            # A rostered skill whose SKILL.md is absent is flagged.
            pytest.param(
                "claude-skills/scan-codebase/SKILL.md",
                "rostered skill missing SKILL.md: claude-skills/scan-codebase/SKILL.md",
                id="rostered-skill-skillmd",
            ),
            # A rostered Codex skill whose ``SKILL.md`` is absent violates the six-skill-parity contract.
            pytest.param(
                "codex-skills/scan-codebase/SKILL.md",
                "rostered codex skill missing SKILL.md: codex-skills/scan-codebase/SKILL.md",
                id="rostered-codex-skill-skillmd",
            ),
        ],
    )
    def test_dropped_member_flagged(self, valid_package: Path, member: str, expected_finding: str):
        """Dropping a required or referenced member (and its manifest entry) is flagged with its closure finding.

        Covers the required documents, the hook helper and Claude hooks pointer file named by the wiring, and the
        ``SKILL.md`` of a rostered Claude or Codex skill.
        """
        _drop_member(valid_package, member)

        assert expected_finding in _validate_findings(valid_package)

    @pytest.mark.parametrize(
        ("payload", "expected_fragment"),
        [
            pytest.param(
                b'{"name": "codemap-py", "version": "0.25.0"}\n',
                "codex manifest must declare skills: ./codex-skills/",
                id="missing-skills-key",
            ),
            pytest.param(
                b'{"name": "codemap-py", "version": "0.25.0", "skills": "./codex-skills/"}\n',
                "codex manifest must declare hooks: ./hooks/codex-hooks.json",
                id="missing-hooks-key",
            ),
        ],
    )
    def test_codex_manifest_missing_key_flagged(self, valid_package: Path, payload: bytes, expected_fragment: str):
        """A Codex manifest without its ``skills`` or ``hooks`` pointer is not integration-complete.

        A manifest omitting ``skills`` violates the six-skill-parity contract, and a Codex package without its runtime
        hook pointer is not integration-complete either.
        """
        _rewrite_member(valid_package, ".codex-plugin/plugin.json", payload)

        assert any(expected_fragment in item for item in _validate_findings(valid_package))


class TestNativeQuestionAssets:
    """A declared native question server must ship its exact configuration and local provider."""

    @pytest.mark.packaging
    def test_declared_package_validates(self, native_question_package: Path):
        """A declared native server ships its exact configuration and inventoried provider.

        Control for the failure cases below: the same package with nothing removed or altered is clean.
        """
        assert _validate_findings(native_question_package) == []

    @pytest.mark.parametrize(
        ("member", "finding"),
        [
            pytest.param(
                ".codex-mcp.json", "native question configuration missing from inventory", id="missing-config"
            ),
            pytest.param(
                "shared/user_questions_mcp.py", "native question provider missing from inventory", id="missing-provider"
            ),
        ],
    )
    @pytest.mark.packaging
    def test_missing_asset_fails_closed(self, native_question_package: Path, member: str, finding: str):
        """Removing an asset and its matching inventory row still violates declared component closure.

        The finding must be reported and the CLI must exit 1, so automation cannot ship the incomplete package.
        """
        _drop_member(native_question_package, member)

        assert finding in _validate_findings(native_question_package)
        assert validator.main(["--package", str(native_question_package)]) == 1

    @pytest.mark.packaging
    def test_changed_provider_wiring_fails_closed(self, native_question_package: Path):
        """Hash-consistent wiring pointing outside the declared local provider fails validation.

        The rewritten configuration keeps a valid hash, so only the provider-path rule can catch it.
        """
        config = native_question_package / ".codex-mcp.json"
        wiring = json.loads(config.read_text(encoding="utf-8"))
        wiring["mcpServers"]["codemap-input"]["args"] = ["other-provider.py", "--stdio"]
        _rewrite_member(native_question_package, ".codex-mcp.json", (json.dumps(wiring) + "\n").encode("utf-8"))

        findings = _validate_findings(native_question_package)

        assert "native question configuration differs from the packaged local provider" in findings

    @pytest.mark.parametrize(
        "pointer", [pytest.param(None, id="null"), pytest.param("./other-mcp.json", id="other-path")]
    )
    @pytest.mark.packaging
    def test_invalid_declaration_fails_closed(self, native_question_package: Path, pointer: str | None):
        """An explicitly invalid or null MCP declaration cannot bypass native component validation.

        A null pointer must not be treated as "no native server declared" while native assets ship in the package.
        """
        path = native_question_package / ".codex-plugin" / "plugin.json"
        codex = json.loads(path.read_text(encoding="utf-8"))
        codex["mcpServers"] = pointer
        _rewrite_member(
            native_question_package, ".codex-plugin/plugin.json", (json.dumps(codex) + "\n").encode("utf-8")
        )

        findings = _validate_findings(native_question_package)

        assert "codex manifest must declare native questions: ./.codex-mcp.json" in findings
