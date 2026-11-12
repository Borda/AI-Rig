"""Acceptance checks for the representative installed-cache scaffold."""

from __future__ import annotations

import functools
import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from _fixture_files import file_sha256
from _modules import load_module
from _platform import (
    POSIX_DESCRIPTOR_PRIMITIVES_AVAILABLE,
    POSIX_EXECUTABLE_SCRIPTS_AVAILABLE,
    POSIX_FILE_MODES_AVAILABLE,
)

_requires_posix_descriptors = pytest.mark.skipif(
    not POSIX_DESCRIPTOR_PRIMITIVES_AVAILABLE, reason="host lacks descriptor-relative no-follow reads"
)
_requires_posix_executable_scripts = pytest.mark.skipif(
    not POSIX_EXECUTABLE_SCRIPTS_AVAILABLE, reason="host cannot execute POSIX shell scripts"
)
_requires_posix_file_modes = pytest.mark.skipif(
    not POSIX_FILE_MODES_AVAILABLE, reason="filesystem does not preserve POSIX file modes"
)
PLUGIN_ROOT = Path(__file__).resolve().parents[3]
REPO_ROOT = PLUGIN_ROOT.parents[1]
CARD_SEPARATOR = b"--- codex-rig-role-card ---\n"
MARKETPLACE_PATH = REPO_ROOT / ".agents" / "plugins" / "marketplace.json"
ROLE_CARD_PATH = Path("roles") / "challenger" / "ROLE.md"
ROLE_CONTRACT_FRAGMENTS = (
    "role_id: challenger",
    "model_reasoning_effort: high",
    "approval_policy: on-request",
    "sandbox_mode: read-only",
    "fallback_modes:",
    "## Trigger and skip boundaries",
    "## Evidence ownership",
    "## Execution constraints",
    "## Handover contract",
    "## Confidence contract",
)


def _package_manifest() -> dict[str, Any]:
    """Return the committed package manifest of the plugin under test."""
    return json.loads((PLUGIN_ROOT / "package-manifest.json").read_text(encoding="utf-8"))


def _rewrite_json(path: Path, edit: Callable[[dict[str, Any]], object]) -> None:
    """Apply ``edit`` in place to the JSON object stored at ``path`` and write it back."""
    document = json.loads(path.read_text(encoding="utf-8"))
    edit(document)
    path.write_text(json.dumps(document), encoding="utf-8")


def _load_package_builder() -> Any:
    """Load the package builder that owns publication file discovery."""
    return load_module("codex_rig_cache_package_builder", PLUGIN_ROOT / "scripts" / "build_package.py")


def _write_fake_codex(path: Path, version: str, *, installed: bool = True, enabled: bool = True) -> None:
    """Write an absolute executable that returns one bounded plugin-list fixture."""
    entries = []
    if installed:
        entries.append(
            {
                "pluginId": "codex-rig@borda-ai-rig",
                "name": "codex-rig",
                "marketplaceName": "borda-ai-rig",
                "version": version,
                "installed": True,
                "enabled": enabled,
            }
        )
    payload = json.dumps({"installed": entries}, separators=(",", ":"))
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{payload}'\n", encoding="utf-8")
    path.chmod(0o755)


def _installed_fixture(
    tmp_path: Path,
    *,
    installed: bool = True,
    enabled: bool = True,
    stale_cache: bool = False,
    missing_card: bool = False,
) -> tuple[Path, Path, Path]:
    """Copy a source-independent package snapshot into an isolated cache; return (home, cache root, fake codex)."""
    version = _package_manifest()["version"]
    cache_version = "0.0.0-retained" if stale_cache else version
    home = tmp_path / "codex-home"
    installed_root = home / "plugins" / "cache" / "borda-ai-rig" / "codex-rig" / cache_version
    shutil.copytree(
        PLUGIN_ROOT, installed_root, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".coverage")
    )
    if missing_card:
        (installed_root / ROLE_CARD_PATH).unlink()
    codex_binary = tmp_path / "codex-runtime"
    _write_fake_codex(codex_binary, version, installed=installed, enabled=enabled)
    return home, installed_root, codex_binary


def _run_verifier(
    home: Path,
    installed_root: Path,
    codex_binary: Path,
    *,
    role_sha256: str | None = None,
    helper_sha256: str | None = None,
    manifest_sha256: str | None = None,
    path_override: str | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run the installed verifier with exact bound identity arguments, optionally overriding one of them."""
    role_path = installed_root / ROLE_CARD_PATH
    verifier = installed_root / "scripts" / "verify_role_link.py"
    manifest_path = installed_root / "package-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    recorded_role_digest = next(item["sha256"] for item in manifest["roles"] if item["id"] == "challenger")
    env = os.environ.copy()
    env["CODEX_HOME"] = str(home)
    if path_override is not None:
        env["PATH"] = path_override
    return subprocess.run(
        [
            sys.executable,
            str(verifier),
            "--plugin-root",
            str(installed_root),
            "--role",
            "challenger",
            "--role-sha256",
            role_sha256 or (file_sha256(role_path) if role_path.exists() else recorded_role_digest),
            "--manifest-sha256",
            manifest_sha256 or file_sha256(manifest_path),
            "--helper-sha256",
            helper_sha256 or file_sha256(verifier),
            "--codex-binary",
            str(codex_binary),
            "--codex-sha256",
            file_sha256(codex_binary),
        ],
        check=False,
        capture_output=True,
        env=env,
    )


def _failure_payload(result: subprocess.CompletedProcess[bytes]) -> dict[str, object]:
    """Decode one exact unavailable bootstrap envelope."""
    assert result.returncode == 4
    assert result.stderr == b""
    assert result.stdout.count(b"\n") == 1
    payload = json.loads(result.stdout)
    assert payload["protocol"] == 1
    assert payload["role_id"] == "challenger"
    assert payload["status"] == "codex-rig-role-unavailable"
    return payload


def _runtime_payload_paths() -> tuple[str, ...]:
    """Return every manifested path that is shipped runtime payload rather than a test."""
    return tuple(record["path"] for record in _package_manifest()["files"] if "tests" not in Path(record["path"]).parts)


@pytest.fixture
def installed(tmp_path: Path) -> Callable[..., tuple[Path, Path, Path]]:
    """Return a factory that copies the package into an isolated cache; keywords select a degraded variant."""
    return functools.partial(_installed_fixture, tmp_path)


@pytest.fixture
def payload_files() -> list[Path]:
    """Return the publication payload files exactly as the package builder discovers them."""
    return list(_load_package_builder().iter_payload_files())


class TestPackageIdentity:
    """Identity and boundary metadata of the committed package."""

    @pytest.mark.packaging
    def test_scaffold_has_stable_role_card_release_identity(self) -> None:
        """The plugin manifest keeps its name, author, MCP wiring and prompt boundaries in step with the package.

        A pinned version literal here once broke on every bump without catching real manifest drift, so the version is
        only required to be numeric and equal to the package manifest's.
        """
        manifest = json.loads((PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        prompts = manifest["interface"]["defaultPrompt"]

        assert manifest["name"] == "codex-rig"
        assert all(part.isdigit() for part in manifest["version"].split(".")), manifest["version"]
        assert manifest["version"] == _package_manifest()["version"]
        assert manifest["author"]["name"] == "Jiri Borovec"
        assert "hooks" not in manifest
        assert manifest["mcpServers"] == "./.codex-mcp.json"
        assert len(prompts) <= 3
        assert all("agent-shims" not in prompt for prompt in prompts)
        assert {"codex-rig:implement", "codex-rig:code-review", "codex-rig:research"} == {
            prompt.split()[2] for prompt in prompts
        }

    @pytest.mark.skipif(
        not MARKETPLACE_PATH.exists(), reason="repository marketplace is outside installed plugin cache"
    )
    @pytest.mark.packaging
    def test_repository_marketplace_contract(self) -> None:
        """The repository marketplace lists Codex Rig at its plugin directory, checked only when the catalog exists."""
        marketplace = json.loads(MARKETPLACE_PATH.read_text(encoding="utf-8"))

        codex_entry = next(plugin for plugin in marketplace["plugins"] if plugin["name"] == "codex-rig")
        assert marketplace["name"] == "borda-ai-rig"
        assert codex_entry["source"]["path"] == "./plugins/codex-rig"

    @pytest.mark.packaging
    def test_committed_runtime_payload_manifest_has_non_test_files(self) -> None:
        """The runtime-payload parametrization below is non-empty, so its privacy check cannot pass vacuously."""
        assert _runtime_payload_paths()

    @pytest.mark.packaging
    @pytest.mark.parametrize("relative", _runtime_payload_paths())
    def test_committed_runtime_payload_has_no_private_machine_paths(self, relative: str) -> None:
        """Each published runtime file carries no home-directory path or private key material.

        Every manifested non-test file is checked on its own, so a leak names the exact offending file.
        """
        payload = (PLUGIN_ROOT / relative).read_bytes()

        assert b"/Us" + b"ers/" not in payload
        assert b"/ho" + b"me/" not in payload
        assert b"BEGIN " + b"PRIVATE KEY" not in payload


class TestRepresentativePayloads:
    """The representative skill and role card are portable to an installed cache."""

    @pytest.fixture
    def skill(self) -> str:
        """Return the representative ``assess`` skill text."""
        return (PLUGIN_ROOT / "skills" / "assess" / "SKILL.md").read_text(encoding="utf-8")

    @pytest.fixture
    def role(self) -> str:
        """Return the representative ``challenger`` role-card text."""
        return (PLUGIN_ROOT / ROLE_CARD_PATH).read_text(encoding="utf-8")

    @pytest.mark.packaging
    def test_skill_links_shared_docs_relative_to_the_package(self, skill: str) -> None:
        """The skill reaches shared docs through the package-relative ``../../shared/`` path.

        The pre-move ``../_shared/`` spelling resolves only inside the source checkout, not an installed cache.
        """
        assert "../../shared/" in skill
        assert "../_shared/" not in skill

    @pytest.mark.packaging
    @pytest.mark.parametrize("fragment", ROLE_CONTRACT_FRAGMENTS)
    def test_role_card_declares_contract_fragment(self, role: str, fragment: str) -> None:
        """The role card declares each runtime setting and contract section the bootstrap relies on."""
        assert fragment in role

    @pytest.mark.packaging
    def test_role_card_selects_no_model(self, role: str) -> None:
        """The role card pins no ``model:`` line, leaving model choice to the host."""
        assert not any(line.startswith("model:") for line in role.splitlines())

    @pytest.mark.packaging
    @pytest.mark.parametrize("checkout_path", ["/Us" + "ers/", "/ho" + "me/", "plugins/codex-rig"])
    def test_payloads_carry_no_checkout_path(self, skill: str, role: str, checkout_path: str) -> None:
        """Neither payload names a user home or the source-checkout plugin directory.

        Such a path resolves on the author's machine only, so an installed copy would silently depend on it.
        """
        assert checkout_path not in skill + role

    @pytest.mark.packaging
    def test_package_manifest_records_role_runtime(self) -> None:
        """The package manifest repeats the role card's reviewed runtime settings for the first role."""
        assert _package_manifest()["roles"][0]["runtime"] == {
            "model_reasoning_effort": "high",
            "approval_policy": "on-request",
            "sandbox_mode": "read-only",
        }


class TestPackageManifest:
    """The package manifest matches the files the builder would publish."""

    @pytest.mark.packaging
    def test_records_unique_paths_even_case_insensitively(self) -> None:
        """No manifest path repeats, including on case-insensitive filesystems."""
        recorded_paths = [item["path"] for item in _package_manifest()["files"]]

        assert len(recorded_paths) == len(set(recorded_paths))
        assert len(recorded_paths) == len({path.casefold() for path in recorded_paths})

    @pytest.mark.packaging
    def test_payload_files_are_regular_and_contained(self, payload_files: list[Path]) -> None:
        """Every publishable file is a regular file whose manifest path stays inside the plugin root."""
        relative_parts = [path.relative_to(PLUGIN_ROOT).parts for path in payload_files]

        assert not any(path.is_symlink() for path in payload_files)
        assert all(".." not in parts for parts in relative_parts)

    @_requires_posix_file_modes
    @pytest.mark.packaging
    def test_covers_payload_hashes_and_modes(self, payload_files: list[Path]) -> None:
        """Recorded hashes and POSIX modes equal what the builder discovers on disk, with nothing extra either side.

        This catches a stale hash, a mode drift, and a file added or removed without regenerating the manifest.
        """
        recorded = {item["path"]: (item["sha256"], int(item["mode"], 8)) for item in _package_manifest()["files"]}

        discovered = {
            path.relative_to(PLUGIN_ROOT).as_posix(): (file_sha256(path), stat.S_IMODE(path.stat().st_mode))
            for path in payload_files
        }

        assert recorded == discovered


class TestRoleLinkVerifier:
    """The installed bootstrap verifier emits the role card only for a fully consistent active package."""

    @_requires_posix_descriptors
    @_requires_posix_executable_scripts
    @pytest.mark.packaging
    def test_emits_exact_installed_card_bytes(self, installed: Callable[..., tuple[Path, Path, Path]]) -> None:
        """The active cache copy emits its own verified card bytes, with no fallback to the source checkout.

        A healthy copy of the package in an isolated Codex home is verified against a fake runtime that reports it as
        installed and enabled.
        """
        home, installed_root, codex_binary = installed()

        result = _run_verifier(home, installed_root, codex_binary)

        card = (installed_root / ROLE_CARD_PATH).read_bytes()
        first_line, separator, emitted_card = result.stdout.partition(CARD_SEPARATOR)
        assert installed_root != PLUGIN_ROOT
        assert result.returncode == 0
        assert result.stderr == b""
        assert separator == CARD_SEPARATOR
        assert json.loads(first_line) == {
            "protocol": 1,
            "role_id": "challenger",
            "role_sha256": file_sha256(installed_root / ROLE_CARD_PATH),
            "status": "ok",
        }
        assert emitted_card == card

    @pytest.mark.parametrize("hooks", [False, True])
    @_requires_posix_descriptors
    @_requires_posix_executable_scripts
    @pytest.mark.packaging
    def test_accepts_exact_manager_profile(
        self, installed: Callable[..., tuple[Path, Path, Path]], hooks: bool
    ) -> None:
        """Linked bootstrap stays valid for both declared manager package variants (with and without hooks).

        The manifest is rewritten to the shim-enabled profile; its hash is then bound on the command line, so the
        verifier must accept the profile on its merits.
        """
        home, installed_root, codex_binary = installed()
        features = {"manager": True, "hooks": hooks, "mcp": False, "generated_shims": True}
        _rewrite_json(
            installed_root / "package-manifest.json",
            lambda manifest: manifest.update(release_profile="shim-enabled", features=features),
        )

        result = _run_verifier(home, installed_root, codex_binary)

        assert result.returncode == 0, result.stdout
        assert CARD_SEPARATOR in result.stdout

    @_requires_posix_descriptors
    @pytest.mark.packaging
    def test_rejects_plugin_manifest_content_not_bound_by_package_manifest(
        self, installed: Callable[..., tuple[Path, Path, Path]]
    ) -> None:
        """Same-version plugin metadata tampering cannot emit a role card.

        The plugin manifest keeps its name and version but changes its description, which the package manifest hashes.
        """
        home, installed_root, codex_binary = installed()
        _rewrite_json(
            installed_root / ".codex-plugin" / "plugin.json",
            lambda manifest: manifest.update(description="tampered but same identity"),
        )

        result = _run_verifier(home, installed_root, codex_binary)

        assert _failure_payload(result)["reason"] == "package-identity-mismatch"
        assert CARD_SEPARATOR not in result.stdout

    @pytest.mark.parametrize(
        "edit",
        [
            pytest.param(lambda manifest: manifest.pop("schema"), id="missing"),
            pytest.param(lambda manifest: manifest.update(schema=3), id="future"),
        ],
    )
    @_requires_posix_descriptors
    @pytest.mark.packaging
    def test_rejects_unsupported_package_schema(
        self, installed: Callable[..., tuple[Path, Path, Path]], edit: Callable[[dict[str, Any]], object]
    ) -> None:
        """A missing or future package schema never enters the trust chain.

        Only schema 1 is understood; the manifest is edited to drop the key or claim schema 3.
        """
        home, installed_root, codex_binary = installed()
        _rewrite_json(installed_root / "package-manifest.json", edit)

        result = _run_verifier(home, installed_root, codex_binary)

        assert _failure_payload(result)["reason"] == "manifest-invalid"
        assert CARD_SEPARATOR not in result.stdout

    @pytest.mark.packaging
    def test_bounds_invalid_role_envelope(self, installed: Callable[..., tuple[Path, Path, Path]]) -> None:
        """A malformed role argument yields a short fixed envelope, never an expanded or injected diagnostic.

        The role value carries a newline and a thousand characters; the reply must stay under 160 bytes and name no
        role.
        """
        home, installed_root, _codex_binary = installed()
        verifier = installed_root / "scripts" / "verify_role_link.py"

        result = subprocess.run(
            [sys.executable, str(verifier), "--role", "bad\n" + "x" * 1000],
            check=False,
            capture_output=True,
            env={**os.environ, "CODEX_HOME": str(home)},
        )

        assert result.returncode == 4
        assert result.stderr == b""
        assert json.loads(result.stdout) == {
            "protocol": 1,
            "reason": "invalid-arguments",
            "role_id": "unknown",
            "status": "codex-rig-role-unavailable",
        }
        assert len(result.stdout) < 160

    @_requires_posix_descriptors
    @_requires_posix_executable_scripts
    @pytest.mark.packaging
    def test_stops_oversized_oracle_output(self, installed: Callable[..., tuple[Path, Path, Path]]) -> None:
        """An oversized runtime response is neither buffered nor trusted.

        The fake Codex runtime streams output forever (``yes``); the verifier must cut it off and report the bound.
        """
        home, installed_root, codex_binary = installed()
        codex_binary.write_text("#!/bin/sh\nyes x\n", encoding="utf-8")
        codex_binary.chmod(0o755)

        result = _run_verifier(home, installed_root, codex_binary)

        assert _failure_payload(result)["reason"] == "active-package-oracle-oversized"
        assert CARD_SEPARATOR not in result.stdout

    @pytest.mark.parametrize(
        ("fixture_options", "overrides", "reason"),
        [
            pytest.param({"enabled": False}, {}, "active-package-mismatch", id="disabled"),
            pytest.param({"installed": False}, {}, "active-package-mismatch", id="removed"),
            pytest.param({"missing_card": True}, {}, "verification-error", id="missing-card"),
            pytest.param({"stale_cache": True}, {}, "active-package-mismatch", id="retained-old-cache"),
            pytest.param({}, {"role_sha256": "0" * 64}, "role-manifest-mismatch", id="role-hash"),
            pytest.param({}, {"helper_sha256": "0" * 64}, "helper-hash-mismatch", id="helper-hash"),
            pytest.param({}, {"manifest_sha256": "0" * 64}, "manifest-hash-mismatch", id="manifest-hash"),
        ],
    )
    @_requires_posix_descriptors
    @_requires_posix_executable_scripts
    @pytest.mark.packaging
    def test_rejects_negative_link_states(
        self,
        installed: Callable[..., tuple[Path, Path, Path]],
        fixture_options: dict[str, bool],
        overrides: dict[str, str],
        reason: str,
    ) -> None:
        """Inactive or inconsistent links never emit role instructions and report a specific reason.

        Seven states are covered: plugin disabled, removed, card missing, an old retained cache, and a wrong role,
        helper or manifest hash bound on the command line.
        """
        home, installed_root, codex_binary = installed(**fixture_options)

        result = _run_verifier(home, installed_root, codex_binary, **overrides)

        assert _failure_payload(result)["reason"] == reason
        assert b"Treat every important claim" not in result.stdout

    @_requires_posix_descriptors
    @_requires_posix_executable_scripts
    @pytest.mark.packaging
    def test_ignores_hostile_path_lookup(
        self, tmp_path: Path, installed: Callable[..., tuple[Path, Path, Path]]
    ) -> None:
        """The inherited PATH cannot substitute the active-package oracle.

        A hostile ``codex`` that leaves a marker sits alone on PATH; the verifier must use the absolute binary it was
        given and never run the impostor.
        """
        home, installed_root, codex_binary = installed()
        hostile_bin = tmp_path / "hostile-bin"
        hostile_bin.mkdir()
        marker = tmp_path / "hostile-codex-ran"
        hostile_codex = hostile_bin / "codex"
        hostile_codex.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
        hostile_codex.chmod(0o755)

        result = _run_verifier(home, installed_root, codex_binary, path_override=str(hostile_bin))

        assert result.returncode == 0
        assert not marker.exists()
