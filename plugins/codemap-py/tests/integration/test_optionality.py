"""Symmetric optionality — provider-only lane.

``-k provider_only``: with every declared consumer hidden/absent, ``codemap-py``'s own six skills stay discoverable, the
shared-index/logging surface resolves fine, and the non-mutating ``integrate audit`` inspection path names absent
consumers from bytes on disk alone — never by importing, locating, or installing a ``cc_*``/``codex-rig`` package.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from codemap_py import integration

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_CANONICAL_SKILLS = {"scan-codebase", "query-code", "test-impact", "rename-refs", "integration", "debrief-coding"}


def _write_manifest(plugin_dir: Path, runtime: integration.Runtime, name: str, version: str) -> None:
    """Write the minimal runtime manifest used by the provider-only fixture."""
    manifest_dir = plugin_dir / (".claude-plugin" if runtime == integration.Runtime.CLAUDE else ".codex-plugin")
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "plugin.json").write_text(json.dumps({"name": name, "version": version}))


def _provider_only_root(base: Path) -> Path:
    """Create a disposable tree containing only the Codemap provider."""
    root = base / "provider-only"
    root.mkdir()
    _write_manifest(root / integration.PROVIDER_DIR, integration.Runtime.CLAUDE, integration.PROVIDER_NAME, "1.0.0")
    _write_manifest(root / integration.PROVIDER_DIR, integration.Runtime.CODEX, integration.PROVIDER_NAME, "1.0.0")
    return root


def _tree_snapshot(root: Path) -> dict[str, bytes]:
    """Capture fixture-file bytes keyed by root-relative POSIX paths.

    >>> from tempfile import TemporaryDirectory
    >>> with TemporaryDirectory() as directory:
    ...     root = Path(directory); _ = (root / "a.txt").write_bytes(b"x")
    ...     sorted(_tree_snapshot(root))
    ['a.txt']
    """
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.fixture(name="provider_only_repo")
def _provider_only_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provide a provider-only repository with native CLI discovery disabled."""
    root = _provider_only_root(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setattr(integration.native, "_native_json_probe", lambda argv: None)
    return root


# --------------------------------------------------------------------------------------
# No cc_* / codex_rig import anywhere in the shipped package source.
# --------------------------------------------------------------------------------------


def test_provider_only_no_cc_star_import_in_package_source() -> None:
    """No module under ``codemap_py`` imports a ``cc_*`` consumer package or ``codex_rig``."""
    pkg_root = Path(integration.__file__).resolve().parent
    offenders: list[tuple[Path, list[str]]] = []
    for path in pkg_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            bad = [name for name in names if name.startswith("cc_") or name.startswith("codex_rig")]
            if bad:
                offenders.append((path, bad))
    assert offenders == []


def test_provider_only_six_skills_discoverable_regardless_of_consumers() -> None:
    """The six-skill rosters are static package content — discoverable with zero consumers installed."""
    claude_names = {p.name for p in (_PLUGIN_ROOT / "claude-skills").iterdir() if (p / "SKILL.md").is_file()}
    codex_names = {p.name for p in (_PLUGIN_ROOT / "codex-skills").iterdir() if (p / "SKILL.md").is_file()}
    assert claude_names == _CANONICAL_SKILLS
    assert codex_names == _CANONICAL_SKILLS


# --------------------------------------------------------------------------------------
# `integrate audit` names absent consumers from bytes only — zero-write, no import.
# --------------------------------------------------------------------------------------


def test_provider_only_audit_names_absent_consumers_from_bytes(provider_only_repo: Path) -> None:
    """Report hidden consumers as named absent states rather than errors."""
    report = integration.build_audit_report("both", provider_only_repo / integration.PROVIDER_DIR)
    for runtime_name in ("claude", "codex"):
        block = report["provider"]["runtimes"][runtime_name]
        assert block["probe_available"] is False
        for consumer_status in block["consumers"].values():
            assert {
                key: consumer_status[key]
                for key in ("manifest_present", "name_matches", "source_version", "installed_version")
            } == {
                "manifest_present": False,
                "name_matches": False,
                "source_version": None,
                "installed_version": None,
            }
            assert consumer_status["managed_block"]["status"] == "absent"


def test_provider_only_audit_is_zero_write(provider_only_repo: Path) -> None:
    """Keep provider-only audits free of fixture-tree mutations."""
    before = _tree_snapshot(provider_only_repo)
    integration.build_audit_report("both", provider_only_repo / integration.PROVIDER_DIR)
    assert _tree_snapshot(provider_only_repo) == before


def test_provider_only_audit_cli_exits_zero(provider_only_repo: Path) -> None:
    """Exit 0 with absent optional consumers and evidence warnings."""
    code = integration.run(["audit", "--runtime", "both", "--json"], provider_only_repo / integration.PROVIDER_DIR)
    assert code == 0


def test_provider_only_plan_rejects_unknown_but_reports_known_absent(provider_only_repo: Path) -> None:
    """A known closed-set consumer name still resolves (as absent-on-disk), never a discovery lookup."""
    targets = integration.resolve_targets("claude", ["oss"])
    assert [t.consumer for t in targets] == ["oss"]  # known name resolves even though unwired on disk
    with pytest.raises(integration.IntegrationError) as exc:
        integration.resolve_targets("claude", ["not-a-real-consumer"])
    assert exc.value.code == "unknown_target"


# --------------------------------------------------------------------------------------
# Shared-index / logging resolve fine with zero consumers installed.
# --------------------------------------------------------------------------------------


def test_provider_only_shared_index_and_logging_resolve(provider_only_repo: Path) -> None:
    """Shared-index identity and runtime-log isolation resolve with no consumer installed."""
    report = integration.build_audit_report("claude", provider_only_repo / integration.PROVIDER_DIR)
    assert report["shared_index"]["root"] == str(provider_only_repo.resolve())
    assert set(report["runtime_logs"]["selected"]) == {"claude"}
