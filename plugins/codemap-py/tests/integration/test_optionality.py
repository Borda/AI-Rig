"""Symmetric optionality — provider-only lane.

``-k ProviderOnly``: with every declared consumer hidden/absent, ``codemap-py``'s own six skills stay discoverable, the
shared-index/logging surface resolves fine, and the non-mutating ``integrate audit`` inspection path names absent
consumers from bytes on disk alone — never by importing, locating, or installing a ``cc_*``/``codex-rig`` package.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from _scan_helpers import tree_snapshot
from codemap_py import integration

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_CANONICAL_SKILLS = {"scan-codebase", "query-code", "test-impact", "rename-refs", "integration", "debrief-coding"}
_ABSENT_FIELDS = {"manifest_present": False, "name_matches": False, "source_version": None, "installed_version": None}


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


def _consumer_imports(pkg_root: Path) -> list[tuple[Path, list[str]]]:
    """Return ``(file, names)`` for every ``cc_*``/``codex_rig`` import found in Python files under *pkg_root*."""
    offenders: list[tuple[Path, list[str]]] = []
    for path in pkg_root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            bad = [name for name in names if name.startswith(("cc_", "codex_rig"))]
            if bad:
                offenders.append((path, bad))
    return offenders


@pytest.fixture(name="provider_only_repo")
def _provider_only_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provide a provider-only repository with native CLI discovery disabled."""
    root = _provider_only_root(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setattr(integration.native, "_native_json_probe", lambda argv: None)
    return root


class TestProviderOnly:
    """Provider-only lane: every declared consumer is absent from the tree."""

    def test_package_source_has_no_consumer_imports(self) -> None:
        """No module under ``codemap_py`` imports a ``cc_*`` consumer package or ``codex_rig``.

        The provider must stay installable and importable alone, so any import of a consumer package in its shipped
        source would turn an absent optional consumer into a hard failure.
        """
        assert _consumer_imports(Path(integration.__file__).resolve().parent) == []

    def test_six_skills_discoverable_without_consumers(self) -> None:
        """The six-skill rosters are static package content, discoverable with zero consumers installed.

        Both the Claude and the Codex skill trees are listed straight from disk; neither may depend on a consumer plugin
        to contribute or hide a skill.
        """
        claude_names = {p.name for p in (_PLUGIN_ROOT / "claude-skills").iterdir() if (p / "SKILL.md").is_file()}
        codex_names = {p.name for p in (_PLUGIN_ROOT / "codex-skills").iterdir() if (p / "SKILL.md").is_file()}

        assert claude_names == _CANONICAL_SKILLS
        assert codex_names == _CANONICAL_SKILLS

    @pytest.mark.parametrize("runtime_name", ["claude", "codex"])
    def test_audit_names_absent_consumers_from_bytes(self, provider_only_repo: Path, runtime_name: str) -> None:
        """Hidden consumers are reported as named absent states rather than errors.

        With only the provider manifests on disk and native discovery disabled, ``integrate audit`` must still list each
        declared consumer, with no manifest, no version and an absent managed block, derived from bytes alone.
        """
        report = integration.build_audit_report("both", provider_only_repo / integration.PROVIDER_DIR)

        block = report["provider"]["runtimes"][runtime_name]
        observed = [{key: status[key] for key in _ABSENT_FIELDS} for status in block["consumers"].values()]
        assert block["probe_available"] is False
        assert observed
        assert all(item == _ABSENT_FIELDS for item in observed)
        assert all(status["managed_block"]["status"] == "absent" for status in block["consumers"].values())

    def test_audit_is_zero_write(self, provider_only_repo: Path) -> None:
        """A provider-only audit leaves the fixture tree byte-identical.

        ``audit`` is the non-mutating inspection path, so it must not create manifests, blocks or logs even when every
        consumer is missing.
        """
        before = tree_snapshot(provider_only_repo)

        integration.build_audit_report("both", provider_only_repo / integration.PROVIDER_DIR)

        assert tree_snapshot(provider_only_repo) == before

    def test_audit_cli_exits_zero(self, provider_only_repo: Path) -> None:
        """The audit CLI exits 0 with absent optional consumers and evidence warnings.

        Absent consumers are an expected state of a provider-only install, not a failure a caller should branch on.
        """
        code = integration.run(["audit", "--runtime", "both", "--json"], provider_only_repo / integration.PROVIDER_DIR)

        assert code == 0

    def test_known_consumer_resolves_though_absent_on_disk(self, provider_only_repo: Path) -> None:
        """A known closed-set consumer name still resolves (as absent-on-disk), never through a discovery lookup.

        The consumer set is a closed list in the provider, so naming ``oss`` yields a target even though nothing for it
        is wired in the fixture tree.
        """
        targets = integration.resolve_targets("claude", ["oss"])

        assert [t.consumer for t in targets] == ["oss"]

    def test_unknown_consumer_is_rejected(self, provider_only_repo: Path) -> None:
        """A name outside the closed consumer set is rejected with ``unknown_target``.

        Rejecting by the closed list, not by looking on disk, is what keeps an absent known consumer distinct from a
        typo.
        """
        with pytest.raises(integration.IntegrationError) as exc:
            integration.resolve_targets("claude", ["not-a-real-consumer"])

        assert exc.value.code == "unknown_target"

    def test_shared_index_and_logging_resolve(self, provider_only_repo: Path) -> None:
        """Shared-index identity and runtime-log isolation resolve with no consumer installed.

        Both come from the provider alone, so the audit can still report the project root and the selected runtime's log
        directory when every consumer is missing.
        """
        report = integration.build_audit_report("claude", provider_only_repo / integration.PROVIDER_DIR)

        assert report["shared_index"]["root"] == str(provider_only_repo.resolve())
        assert set(report["runtime_logs"]["selected"]) == {"claude"}
