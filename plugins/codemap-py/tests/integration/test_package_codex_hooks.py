"""Built-package closure contracts for Codex hook registration."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from codemap_py import integration


_PLUGIN_ROOT = Path(__file__).parents[2]


def _load_script(name: str):
    """Import one packaging script without treating ``scripts`` as a package."""
    spec = importlib.util.spec_from_file_location(name, _PLUGIN_ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_BUILD = _load_script("build_package")
_VALIDATE = _load_script("validate_package")


def _build_candidate(tmp_path: Path) -> Path:
    """Build an isolated candidate from the checked-out plugin tree."""
    candidate = tmp_path / "candidate"
    mode_map = _BUILD._git_exec_modes(_PLUGIN_ROOT)
    _BUILD.build_package(_PLUGIN_ROOT, candidate, mode_map)
    return candidate


def _replace_codex_manifest(candidate: Path, hooks: str) -> None:
    """Set the Codex hooks pointer and keep the candidate inventory byte-accurate."""
    path = candidate / ".codex-plugin" / "plugin.json"
    manifest = json.loads(path.read_text())
    manifest["hooks"] = hooks
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    package_manifest_path = candidate / "package-manifest.json"
    package_manifest = json.loads(package_manifest_path.read_text())
    record = next(item for item in package_manifest["files"] if item["path"] == ".codex-plugin/plugin.json")
    record["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    package_manifest_path.write_text(json.dumps(package_manifest, sort_keys=True, separators=(",", ":")) + "\n")


@pytest.mark.packaging
def test_built_candidate_contains_codex_hook_config() -> None:
    """The packaging inventory must ship the configuration the Codex manifest declares."""
    # Kept local because build_package clears its output directory by contract.
    import tempfile

    with tempfile.TemporaryDirectory() as temp:
        candidate = _build_candidate(Path(temp))
        manifest = json.loads((candidate / ".codex-plugin" / "plugin.json").read_text())

        assert manifest["hooks"] == "./hooks/codex-hooks.json"
        assert (candidate / "hooks" / "codex-hooks.json").is_file()


@pytest.mark.integration
@pytest.mark.packaging
def test_content_identity_matches_built_payload(tmp_path: Path) -> None:
    """Source and built payload hashes must agree when their shipped files agree."""
    candidate = _build_candidate(tmp_path)

    source_identity = integration._provider_content_identity(_PLUGIN_ROOT, unreadable_reason="source_unreadable")
    candidate_identity = integration._provider_content_identity(candidate, unreadable_reason="candidate_unreadable")

    assert source_identity["state"] == "observed"
    assert candidate_identity["state"] == "observed"
    assert candidate_identity == source_identity


@pytest.mark.integration
@pytest.mark.packaging
def test_validator_rejects_missing_or_mismatched_codex_hook_pointer(tmp_path: Path) -> None:
    """A candidate must fail closure when Codex points at a missing hook config."""
    candidate = _build_candidate(tmp_path)
    _replace_codex_manifest(candidate, "./hooks/missing-codex-hooks.json")

    findings = _VALIDATE.validate_package(candidate)

    assert "codex hooks pointer file missing: hooks/missing-codex-hooks.json" in findings
