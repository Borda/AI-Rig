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
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_BUILD = _load_script("build_package")
_VALIDATE = _load_script("validate_package")


@pytest.fixture
def candidate(tmp_path: Path) -> Path:
    """Build an isolated package candidate from the checked-out plugin tree.

    ``build_package`` clears its output directory by contract, so the candidate lives in its own ``tmp_path`` child.
    """
    out = tmp_path / "candidate"
    mode_map = _BUILD._git_exec_modes(_PLUGIN_ROOT)
    # Include the owned new payload before commit without staging the caller's index.
    for relative in (
        "shared/codex-user-questions.md",
        "shared/codex-user-questions-details.md",
        "shared/user_questions_mcp.py",
        ".codex-mcp.json",
    ):
        mode_map.setdefault(relative, False)
    _BUILD.build_package(_PLUGIN_ROOT, out, mode_map)
    return out


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
def test_built_candidate_contains_codex_hook_config(candidate: Path) -> None:
    """The packaging inventory ships the hook configuration the Codex manifest declares.

    A freshly built candidate must carry both the manifest pointer and the file it points at, otherwise Codex would
    register hooks that do not exist in the installed package.
    """
    manifest = json.loads((candidate / ".codex-plugin" / "plugin.json").read_text())

    assert manifest["hooks"] == "./hooks/codex-hooks.json"
    assert (candidate / "hooks" / "codex-hooks.json").is_file()


@pytest.mark.integration
@pytest.mark.packaging
def test_content_identity_matches_built_payload(candidate: Path) -> None:
    """Source and built payload content identities agree when their shipped files agree.

    The audit compares the provider's source tree with the built package; a faithful build must therefore report the
    same observed identity on both sides, or every audit would claim drift.
    """
    source_identity = integration._provider_content_identity(_PLUGIN_ROOT, unreadable_reason="source_unreadable")
    candidate_identity = integration._provider_content_identity(candidate, unreadable_reason="candidate_unreadable")

    assert source_identity["state"] == "observed"
    assert candidate_identity["state"] == "observed"
    assert candidate_identity == source_identity


@pytest.mark.integration
@pytest.mark.packaging
def test_validator_rejects_missing_codex_hook_pointer(candidate: Path) -> None:
    """A candidate fails package closure when Codex points at a missing hook config.

    The built manifest is rewritten to name a hook file that is not in the package (with its inventory hash kept
    accurate), so only the pointer check can catch the break.
    """
    _replace_codex_manifest(candidate, "./hooks/missing-codex-hooks.json")

    findings = _VALIDATE.validate_package(candidate)

    assert "codex hooks pointer file missing: hooks/missing-codex-hooks.json" in findings
