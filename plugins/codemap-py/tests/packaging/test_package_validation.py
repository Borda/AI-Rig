"""Package validator contract.

Black-box tests for ``validate_package``: a well-formed synthetic package passes,
and each closure/portability/hygiene/declared-component/exec rule fires a named
finding when violated. The fixtures are hand-built (not produced by the real-tree
builder) so the validator's contract is exercised in isolation.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest


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


def _executable_mode_is_preserved() -> bool:
    """Return whether this host preserves an executable mode set by ``chmod``."""
    with tempfile.TemporaryDirectory() as directory:
        candidate = Path(directory) / "candidate"
        candidate.write_text("candidate\n", encoding="utf-8")
        try:
            candidate.chmod(0o755)
        except OSError:
            return False
        return os.stat(candidate).st_mode & 0o777 == 0o755


_skip_executable_mode_unavailable = pytest.mark.skipif(
    not _executable_mode_is_preserved(), reason="chmod cannot preserve executable mode on this host"
)


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


@pytest.fixture(name="valid_package")
def _valid_package(tmp_path: Path) -> Path:
    """Create a well-formed package directory."""
    package = tmp_path / "pkg"
    package.mkdir()
    _write_valid_package(package)
    return package


def _validate_findings(package: Path) -> list[str]:
    """Return the validator's findings for ``package``."""
    return validator.validate_package(package)


# --- happy path ------------------------------------------------------------


@pytest.mark.packaging
def test_valid_package_has_no_findings(valid_package: Path) -> None:
    """A well-formed package yields zero findings."""
    assert _validate_findings(valid_package) == []


@pytest.mark.packaging
def test_cli_exits_zero_on_valid_package(valid_package: Path) -> None:
    """The validator CLI exits zero on a clean package."""
    assert validator.main(["--package", str(valid_package)]) == 0


# --- inventory violations --------------------------------------------------


@pytest.mark.packaging
def test_missing_payload_flagged(valid_package: Path) -> None:
    """A manifest entry with no on-disk file is flagged missing."""
    (valid_package / "bin" / "_schema.py").unlink()
    assert any("missing payload file: bin/_schema.py" == item for item in _validate_findings(valid_package))


@pytest.mark.packaging
def test_modified_payload_flagged(valid_package: Path) -> None:
    """A payload file whose bytes drift from its recorded hash is flagged."""
    (valid_package / "README.md").write_bytes(b"# tampered\n")
    assert any("modified payload file: README.md" == item for item in _validate_findings(valid_package))


@pytest.mark.packaging
def test_extra_unmanifested_file_flagged(valid_package: Path) -> None:
    """A file absent from the manifest is flagged extra."""
    (valid_package / "bin" / "stowaway.py").write_bytes(b"x = 1\n")
    assert any("extra un-manifested file: bin/stowaway.py" == item for item in _validate_findings(valid_package))


# --- path violations -------------------------------------------------------


@pytest.mark.parametrize("bad_path", ["/etc/passwd", "../escape.py", "C:\\Windows\\x"])
@pytest.mark.packaging
def test_non_relative_manifest_path_flagged(valid_package: Path, bad_path: str) -> None:
    """Absolute, parent-escaping, or drive-qualified manifest paths are flagged."""
    _mutate_manifest(valid_package, lambda m: m["files"].append({"path": bad_path, "sha256": "0" * 64, "exec": False}))
    assert any(item.startswith(f"non-relative manifest path: {bad_path}") for item in _validate_findings(valid_package))


# --- hygiene violations ----------------------------------------------------


@pytest.mark.packaging
def test_personal_path_reference_flagged(valid_package: Path) -> None:
    """The build host's real home path baked into a payload file is flagged."""
    leaked = f"root = '{Path.home()}/checkout/codemap-py'\n".encode()
    (valid_package / "bin" / "_schema.py").write_bytes(leaked)
    _mutate_manifest(valid_package, lambda m: _sync_hash(m, "bin/_schema.py", leaked))
    assert any(
        "personal-path reference in payload: bin/_schema.py" == item for item in _validate_findings(valid_package)
    )


@pytest.mark.packaging
def test_secret_material_flagged(valid_package: Path) -> None:
    """Private-key header bytes inside a payload file are flagged."""
    # Assembled from fragments so no literal key header sits in this source file.
    marker = b"-----BEGIN " + b"RSA PRIVATE" + b" KEY-----"
    secret = marker + b"\nabc\n" + marker.replace(b"BEGIN", b"END") + b"\n"
    (valid_package / "NOTICE").write_bytes(secret)
    _mutate_manifest(valid_package, lambda m: _sync_hash(m, "NOTICE", secret))
    assert any("secret-like material in payload: NOTICE" == item for item in _validate_findings(valid_package))


# --- closure violations ----------------------------------------------------


@pytest.mark.packaging
def test_missing_required_document_flagged(valid_package: Path) -> None:
    """Dropping a required document (and its manifest entry) is flagged as missing."""
    _drop_member(valid_package, "CHANGELOG.md")
    assert any("missing required member: CHANGELOG.md" == item for item in _validate_findings(valid_package))


@pytest.mark.packaging
def test_forbidden_default_path_flagged(valid_package: Path) -> None:
    """A leaked default ``hooks/hooks.json`` is flagged forbidden."""
    _add_member(valid_package, "hooks/hooks.json", b"{}\n")
    assert any("forbidden default path present: hooks/hooks.json" == item for item in _validate_findings(valid_package))


@pytest.mark.packaging
def test_symlink_flagged(valid_package: Path) -> None:
    """A symlink anywhere in the package is flagged."""
    link = valid_package / "bin" / "alias-link"
    link.symlink_to(valid_package / "bin" / "scan-index")
    assert any(item.startswith("symlink in package:") for item in _validate_findings(valid_package))


# --- declared-component closure ---------------------------------------


@pytest.mark.packaging
def test_missing_referenced_hook_helper_flagged(valid_package: Path) -> None:
    """A hook helper named by the wiring but absent from the package is flagged."""
    _drop_member(valid_package, "hooks/seed-session.py")
    assert any(
        "referenced hook helper missing: hooks/seed-session.py" == item for item in _validate_findings(valid_package)
    )


@pytest.mark.packaging
def test_missing_hooks_pointer_file_flagged(valid_package: Path) -> None:
    """A Claude ``hooks`` pointer whose target file is absent is flagged."""
    _drop_member(valid_package, "hooks/claude-hooks.json")
    assert any(
        "claude hooks pointer file missing: hooks/claude-hooks.json" == item
        for item in _validate_findings(valid_package)
    )


@pytest.mark.packaging
def test_undeclared_extra_roster_dir_flagged(valid_package: Path) -> None:
    """An on-disk skill dir absent from the manifest roster is flagged as a mismatch."""
    _add_member(valid_package, "claude-skills/rogue/SKILL.md", b"---\nname: rogue\n---\n")
    assert any(item.startswith("claude roster ") and "rogue" in item for item in _validate_findings(valid_package))


@pytest.mark.packaging
def test_rostered_skill_missing_skillmd_flagged(valid_package: Path) -> None:
    """A rostered skill whose SKILL.md is absent is flagged."""
    _drop_member(valid_package, "claude-skills/scan-codebase/SKILL.md")
    assert any(
        "rostered skill missing SKILL.md: claude-skills/scan-codebase/SKILL.md" == item
        for item in _validate_findings(valid_package)
    )


@pytest.mark.packaging
def test_codex_manifest_missing_skills_key_flagged(valid_package: Path) -> None:
    """A Codex manifest that omits the ``skills`` pointer violates the six-skill-parity contract."""
    codex = valid_package / ".codex-plugin" / "plugin.json"
    payload = b'{"name": "codemap-py", "version": "0.25.0"}\n'
    codex.write_bytes(payload)
    _mutate_manifest(valid_package, lambda m: _sync_hash(m, ".codex-plugin/plugin.json", payload))
    assert any(
        "codex manifest must declare skills: ./codex-skills/" in item for item in _validate_findings(valid_package)
    )


@pytest.mark.packaging
def test_codex_manifest_missing_hooks_key_flagged(valid_package: Path) -> None:
    """A Codex package without its runtime hook pointer is not integration-complete."""
    codex = valid_package / ".codex-plugin" / "plugin.json"
    payload = b'{"name": "codemap-py", "version": "0.25.0", "skills": "./codex-skills/"}\n'
    codex.write_bytes(payload)
    _mutate_manifest(valid_package, lambda m: _sync_hash(m, ".codex-plugin/plugin.json", payload))

    assert any(
        "codex manifest must declare hooks: ./hooks/codex-hooks.json" in item
        for item in _validate_findings(valid_package)
    )


@pytest.mark.packaging
def test_rostered_codex_skill_missing_skillmd_flagged(valid_package: Path) -> None:
    """A rostered Codex skill whose ``SKILL.md`` is absent violates the six-skill-parity contract."""
    _drop_member(valid_package, "codex-skills/scan-codebase/SKILL.md")
    assert any(
        "rostered codex skill missing SKILL.md: codex-skills/scan-codebase/SKILL.md" == item
        for item in _validate_findings(valid_package)
    )


@pytest.mark.packaging
def test_codex_roster_mismatch_flagged(valid_package: Path) -> None:
    """Flag a Codex roster that diverges from the corresponding Claude roster."""
    _mutate_manifest(valid_package, lambda m: m["skills"].__setitem__("codex", []))
    assert any(item.startswith("codex roster [] != claude roster") for item in _validate_findings(valid_package))


# --- executable-mode drift --------------------------------------------


@_skip_executable_mode_unavailable
@pytest.mark.packaging
def test_exec_flag_mismatch_flagged(valid_package: Path) -> None:
    """A data file made executable on disk disagrees with its manifest exec flag."""
    (valid_package / "bin" / "_schema.py").chmod(0o755)
    assert any(
        item.startswith("exec flag mismatch") and "bin/_schema.py" in item for item in _validate_findings(valid_package)
    )


def _sync_hash(manifest: dict, path: str, data: bytes) -> None:
    """Update the recorded SHA-256 for ``path`` so only the targeted rule fires."""
    for record in manifest["files"]:
        if record["path"] == path:
            record["sha256"] = hashlib.sha256(data).hexdigest()
