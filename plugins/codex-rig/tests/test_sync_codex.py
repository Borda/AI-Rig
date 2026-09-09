"""Acceptance checks for the native cross-platform Codex restore entrypoint."""

from __future__ import annotations

import importlib.util
import io
import hashlib
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
SYNC_SCRIPT = PLUGIN_ROOT / "scripts" / "sync_codex.py"


def _load_sync() -> ModuleType:
    """Load the packaged sync entrypoint without package imports."""
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    specification = importlib.util.spec_from_file_location("codex_rig_sync_codex", SYNC_SCRIPT)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _write_fixture_package_manifest(root: Path, version: str) -> None:
    """Write a complete package manifest for one test-only Codex Rig payload."""
    records = [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "mode": f"{path.stat().st_mode & 0o777:04o}",
        }
        for path in sorted(root.rglob("*"))
        if path.is_file() and path != root / "package-manifest.json"
    ]
    reader = next(record for record in records if record["path"] == "shared/github_read.py")
    (root / "package-manifest.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "plugin": "codex-rig",
                "version": version,
                "files": records,
                "skills": [],
                "roles": [],
                "bootstrap": {"protocol": "fixture", "helper": reader["path"], "sha256": reader["sha256"]},
                "generator": {"version": "fixture", "path": reader["path"], "sha256": reader["sha256"]},
            }
        ),
        encoding="utf-8",
    )


def _marketplace_fixture(tmp_path: Path) -> Path:
    """Create a complete selected marketplace payload for sync acceptance checks."""
    root = tmp_path / "marketplace"
    plugin = root / "plugins" / "codex-rig"
    bridge_doctor = root / "plugins" / "bridge_cc-codex" / "bin" / "bridge_diagnose.py"
    (plugin / "assets").mkdir(parents=True)
    (plugin / "scripts").mkdir()
    (plugin / ".codex-plugin").mkdir()
    (plugin / "shared").mkdir()
    bridge_doctor.parent.mkdir(parents=True)
    (plugin / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "codex-rig", "version": "0.3.0"}), encoding="utf-8"
    )
    (plugin / "assets" / "AGENTS.md").write_text("# fixture\n", encoding="utf-8")
    for filename in (
        "_package_identity.py",
        "_safe_package_io.py",
        "install_global_agents.py",
        "install_github_read_rules.py",
    ):
        (plugin / "scripts" / filename).write_text("# fixture\n", encoding="utf-8")
    (plugin / "shared" / "github_read.py").write_text('"""Fixture reader."""\n', encoding="utf-8")
    _write_fixture_package_manifest(plugin, "0.3.0")
    bridge_doctor.write_text("# fixture\n", encoding="utf-8")
    return root


def _fake_runner(
    marketplace_root: Path,
    calls: list[tuple[str, ...]],
    *,
    configured_ref: str = "",
    source_type: str = "git",
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Return deterministic Codex/Git command results for one restore."""
    metadata = marketplace_root / ".codex-marketplace-install.json"
    metadata.write_text(json.dumps({"ref_name": configured_ref}), encoding="utf-8")

    def _run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        """Record a sync command and return the configured marketplace response."""
        rendered = tuple(str(item) for item in command)
        calls.append(rendered)
        if rendered[1:5] == ("plugin", "marketplace", "list", "--json"):
            payload = {
                "marketplaces": [
                    {
                        "name": "borda-ai-rig",
                        "root": str(marketplace_root),
                        "marketplaceSource": {"sourceType": source_type},
                    }
                ]
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        if rendered[1:5] == ("plugin", "list", "--marketplace", "borda-ai-rig"):
            payload = {
                "installed": [
                    {
                        "pluginId": "codex-rig@borda-ai-rig",
                        "enabled": True,
                        "version": "0.3.0",
                    },
                    {
                        "pluginId": "codemap-py@borda-ai-rig",
                        "enabled": True,
                        "version": "0.28.8",
                    },
                    {
                        "pluginId": "bridge@borda-ai-rig",
                        "enabled": True,
                        "version": "0.1.0",
                    },
                ]
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        if rendered[:3] == ("git", "-C", str(marketplace_root)):
            return subprocess.CompletedProcess(command, 0, "0123456789abcdef\n", "")
        if rendered == ("python", "--version"):
            return subprocess.CompletedProcess(command, 0, "Python 3.12.0\n", "")
        if rendered[0] == "python" and rendered[-2:] == ("--direction", "claude"):
            return subprocess.CompletedProcess(command, 0, json.dumps({"ok": True, "live": False}), "")
        return subprocess.CompletedProcess(command, 0, "", "")

    return _run


def test_native_sync_refreshes_latest_and_installs_global_instructions(tmp_path: Path) -> None:
    """Refresh and clean-install the latest plugins without shell interpolation."""
    module = _load_sync()
    root = _marketplace_fixture(tmp_path)
    calls: list[tuple[str, ...]] = []
    output = io.StringIO()

    result = module.sync_codex(
        module.parse_args([]),
        run=_fake_runner(root, calls),
        environ={"CODEX_HOME": str(tmp_path / "home")},
        stdout=output,
    )

    assert result == 0
    for _display_name, plugin_id in module.MANAGED_PLUGINS:
        remove_call = ("codex", "plugin", "remove", plugin_id)
        add_call = ("codex", "plugin", "add", plugin_id)
        assert remove_call in calls
        assert add_call in calls
        assert calls.index(remove_call) < calls.index(add_call)
    upgrade_call = ("codex", "plugin", "marketplace", "upgrade", "borda-ai-rig")
    assert upgrade_call in calls
    assert all(
        calls.index(upgrade_call) < calls.index(("codex", "plugin", "remove", plugin_id))
        for _display_name, plugin_id in module.MANAGED_PLUGINS
    )
    assert ("codex", "plugin", "add", "codex-rig@borda-ai-rig") in calls
    assert ("codex", "plugin", "add", "codemap-py@borda-ai-rig") in calls
    assert ("codex", "plugin", "add", "bridge@borda-ai-rig") in calls
    installer = root / "plugins" / "codex-rig" / "scripts" / "install_global_agents.py"
    template = root / "plugins" / "codex-rig" / "assets" / "AGENTS.md"
    assert any(
        call[1:] == (str(installer), "--source", str(template), "--codex-home", str(tmp_path / "home"))
        for call in calls
    )
    assert "Codex Rig 0.3.0 installed" in output.getvalue()
    assert "Codemap 0.28.8 installed" in output.getvalue()
    assert "Claude Code and Codex Bridge 0.1.0 installed" in output.getvalue()
    rules_installer = root / "plugins" / "codex-rig" / "scripts" / "install_github_read_rules.py"
    installed_root = tmp_path / "home" / "plugins" / "cache" / "borda-ai-rig" / "codex-rig" / "0.3.0"
    assert any(
        call[1:] == (str(rules_installer), "--plugin-root", str(installed_root), "--codex-home", str(tmp_path / "home"))
        for call in calls
    )


def test_native_sync_runs_free_installed_bridge_diagnosis(tmp_path: Path) -> None:
    """Verify machine-wide Bridge prerequisites without paid model inference."""
    module = _load_sync()
    root = _marketplace_fixture(tmp_path)
    doctor = root / "plugins" / "bridge_cc-codex" / "bin" / "bridge_diagnose.py"
    calls: list[tuple[str, ...]] = []
    base_run = _fake_runner(root, calls)

    def _run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Return configured Bridge version/diagnosis responses and delegate the rest."""
        rendered = tuple(str(item) for item in command)
        if rendered == ("python", "--version"):
            calls.append(rendered)
            return subprocess.CompletedProcess(command, 0, "Python 3.12.0\n", "")
        if rendered == ("python", str(doctor), "--direction", "claude"):
            calls.append(rendered)
            return subprocess.CompletedProcess(command, 0, json.dumps({"ok": True, "live": False}), "")
        return base_run(command, **kwargs)

    output = io.StringIO()
    result = module.sync_codex(
        module.parse_args(["--no-codex-global-agents"]),
        run=_run,
        environ={},
        stdout=output,
    )

    assert result == 0
    assert ("python", "--version") in calls
    assert ("python", str(doctor), "--direction", "claude") in calls
    assert "Bridge static diagnosis passed; no provider call made" in output.getvalue()


def test_native_sync_rejects_bridge_python_below_minimum(tmp_path: Path) -> None:
    """Prevent a green sync when the MCP launcher cannot run Bridge."""
    module = _load_sync()
    root = _marketplace_fixture(tmp_path)
    calls: list[tuple[str, ...]] = []
    base_run = _fake_runner(root, calls)

    def _run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Return an old Bridge Python version and delegate unrelated commands."""
        if command == ["python", "--version"]:
            calls.append(tuple(command))
            return subprocess.CompletedProcess(command, 0, "Python 3.9.19\n", "")
        return base_run(command, **kwargs)

    with pytest.raises(module.SyncError, match="found Python 3.9.19"):
        module.sync_codex(
            module.parse_args(["--no-codex-global-agents"]),
            run=_run,
            environ={},
            stdout=io.StringIO(),
        )

    assert not any(call[-2:] == ("--direction", "claude") for call in calls)


def test_native_sync_rejects_failed_bridge_static_diagnosis(tmp_path: Path) -> None:
    """Keep missing Claude CLI compatibility visible after installation."""
    module = _load_sync()
    root = _marketplace_fixture(tmp_path)
    doctor = root / "plugins" / "bridge_cc-codex" / "bin" / "bridge_diagnose.py"
    calls: list[tuple[str, ...]] = []
    base_run = _fake_runner(root, calls)

    def _run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Return a failed Bridge diagnosis and delegate unrelated sync commands."""
        rendered = tuple(str(item) for item in command)
        if rendered == ("python", str(doctor), "--direction", "claude"):
            calls.append(rendered)
            payload = {"ok": False, "live": False, "findings": [{"target": "claude", "ok": False}]}
            return subprocess.CompletedProcess(command, 2, json.dumps(payload), "")
        return base_run(command, **kwargs)

    with pytest.raises(module.SyncError, match="Bridge static diagnosis failed"):
        module.sync_codex(
            module.parse_args(["--no-codex-global-agents"]),
            run=_run,
            environ={},
            stdout=io.StringIO(),
        )


def test_native_sync_replaces_local_marketplace_with_git_source(tmp_path: Path) -> None:
    """Restore canonical refresh semantics when a local marketplace is configured."""
    module = _load_sync()
    root = _marketplace_fixture(tmp_path)
    calls: list[tuple[str, ...]] = []
    output = io.StringIO()

    result = module.sync_codex(
        module.parse_args(["--no-codex-global-agents"]),
        run=_fake_runner(root, calls, source_type="local"),
        environ={},
        stdout=output,
    )

    assert result == 0
    assert ("codex", "plugin", "marketplace", "upgrade", "borda-ai-rig") not in calls
    assert ("codex", "plugin", "marketplace", "remove", "borda-ai-rig") in calls
    assert ("codex", "plugin", "marketplace", "add", "Borda/AI-Rig") in calls
    marketplace_remove_index = calls.index(("codex", "plugin", "marketplace", "remove", "borda-ai-rig"))
    assert all(
        marketplace_remove_index < calls.index(("codex", "plugin", "remove", plugin_id))
        for _display_name, plugin_id in module.MANAGED_PLUGINS
    )
    assert ("codex", "plugin", "add", "codex-rig@borda-ai-rig") in calls
    assert ("codex", "plugin", "add", "codemap-py@borda-ai-rig") in calls
    assert ("codex", "plugin", "add", "bridge@borda-ai-rig") in calls
    assert "marketplace re-registered from Git source" in output.getvalue()
    assert "[skip] marketplace refresh" not in output.getvalue()


def test_native_sync_no_clean_refreshes_without_removing_plugins(tmp_path: Path) -> None:
    """Keep no-clean limited to plugin removal rather than marketplace freshness."""
    module = _load_sync()
    root = _marketplace_fixture(tmp_path)
    calls: list[tuple[str, ...]] = []

    result = module.sync_codex(
        module.parse_args(["--no-clean", "--no-codex-global-agents"]),
        run=_fake_runner(root, calls, source_type="local"),
        environ={},
        stdout=io.StringIO(),
    )

    assert result == 0
    assert not any(call[1:3] == ("plugin", "remove") for call in calls)
    assert ("codex", "plugin", "marketplace", "remove", "borda-ai-rig") in calls
    assert ("codex", "plugin", "marketplace", "add", "Borda/AI-Rig") in calls
    assert ("codex", "plugin", "add", "codex-rig@borda-ai-rig") in calls


def test_native_sync_adds_pinned_marketplace_when_absent(tmp_path: Path) -> None:
    """Forward one explicit ref without relying on shell quoting."""
    module = _load_sync()
    root = _marketplace_fixture(tmp_path)
    calls: list[tuple[str, ...]] = []
    listings = 0

    def _run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        """Expose the first empty marketplace listing before the configured retry listing."""
        nonlocal listings
        rendered = tuple(str(item) for item in command)
        calls.append(rendered)
        if rendered[1:5] == ("plugin", "marketplace", "list", "--json"):
            listings += 1
            payload = {"marketplaces": [] if listings == 1 else [{"name": "borda-ai-rig", "root": str(root)}]}
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        if rendered[1:5] == ("plugin", "list", "--marketplace", "borda-ai-rig"):
            payload = {
                "installed": [
                    {"pluginId": "codex-rig@borda-ai-rig", "enabled": True, "version": "0.3.0"},
                    {"pluginId": "codemap-py@borda-ai-rig", "enabled": True, "version": "0.28.8"},
                    {"pluginId": "bridge@borda-ai-rig", "enabled": True, "version": "0.1.0"},
                ]
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        if rendered[:3] == ("git", "-C", str(root)):
            return subprocess.CompletedProcess(command, 0, "0123456789abcdef\n", "")
        if rendered == ("python", "--version"):
            return subprocess.CompletedProcess(command, 0, "Python 3.12.0\n", "")
        if rendered[0] == "python" and rendered[-2:] == ("--direction", "claude"):
            return subprocess.CompletedProcess(command, 0, json.dumps({"ok": True, "live": False}), "")
        return subprocess.CompletedProcess(command, 0, "", "")

    result = module.sync_codex(
        module.parse_args(["--codex-ref", "codex-rig-v0.3.0", "--no-codex-global-agents"]),
        run=_run,
        environ={},
        stdout=io.StringIO(),
    )

    assert result == 0
    assert (
        "codex",
        "plugin",
        "marketplace",
        "add",
        "Borda/AI-Rig",
        "--ref",
        "codex-rig-v0.3.0",
    ) in calls
    assert not any("install_global_agents.py" in item for call in calls for item in call)


def test_native_sync_rejects_unavailable_pinned_rules_before_plugin_removal(tmp_path: Path) -> None:
    """Keep a newly selected unsupported revision from removing managed plugins."""
    module = _load_sync()
    root = _marketplace_fixture(tmp_path)
    (root / "plugins" / "codex-rig" / "scripts" / "install_github_read_rules.py").unlink()
    calls: list[tuple[str, ...]] = []
    listings = 0
    base_run = _fake_runner(root, calls)

    def _run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Expose an absent marketplace until the selected source is registered."""
        nonlocal listings
        rendered = tuple(str(item) for item in command)
        if rendered[1:5] == ("plugin", "marketplace", "list", "--json"):
            calls.append(rendered)
            listings += 1
            payload = {"marketplaces": [] if listings == 1 else [{"name": "borda-ai-rig", "root": str(root)}]}
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        return base_run(command, **kwargs)

    with pytest.raises(module.SyncError, match="selected Codex Rig package failed verification"):
        module.sync_codex(
            module.parse_args(["--codex-ref", "codex-rig-v0.2.4", "--no-codex-global-agents"]),
            run=_run,
            environ={},
            stdout=io.StringIO(),
        )

    assert ("codex", "plugin", "marketplace", "add", "Borda/AI-Rig", "--ref", "codex-rig-v0.2.4") in calls
    assert not any(call[1:3] in {("plugin", "remove"), ("plugin", "add")} for call in calls)


def test_native_sync_rejects_existing_ref_mismatch_without_changes(tmp_path: Path) -> None:
    """Fail before refresh when the configured marketplace tracks another ref."""
    module = _load_sync()
    root = _marketplace_fixture(tmp_path)
    calls: list[tuple[str, ...]] = []

    with pytest.raises(module.SyncError, match="marketplace tracks"):
        module.sync_codex(
            module.parse_args(["--codex-ref", "codex-rig-v0.3.0"]),
            run=_fake_runner(root, calls, configured_ref="codex-rig-v0.2.4"),
            environ={},
            stdout=io.StringIO(),
        )

    assert not any(
        call[1:4] == ("plugin", "marketplace", "upgrade") or call[1:3] == ("plugin", "add") for call in calls
    )


def test_native_sync_rejects_pinned_payload_missing_rules_before_changes(tmp_path: Path) -> None:
    """Keep an unsupported selected revision from removing a working install."""
    module = _load_sync()
    root = _marketplace_fixture(tmp_path)
    rules_installer = root / "plugins" / "codex-rig" / "scripts" / "install_github_read_rules.py"
    rules_installer.unlink()
    calls: list[tuple[str, ...]] = []

    with pytest.raises(module.SyncError, match="selected Codex Rig package failed verification"):
        module.sync_codex(
            module.parse_args(["--codex-ref", "codex-rig-v0.2.4", "--no-codex-global-agents"]),
            run=_fake_runner(root, calls, configured_ref="codex-rig-v0.2.4"),
            environ={},
            stdout=io.StringIO(),
        )

    assert all(
        call[1:4] != ("plugin", "marketplace", "upgrade")
        and call[1:4] != ("plugin", "marketplace", "remove")
        and call[1:3] != ("plugin", "remove")
        and call[1:3] != ("plugin", "add")
        for call in calls
    )


@pytest.mark.parametrize("tamper", ("malformed-manifest", "hash-mismatch", "missing-package-dependency"))
def test_native_sync_rejects_invalid_pinned_payload_before_changes(tmp_path: Path, tamper: str) -> None:
    """Keep a corrupt selected package from mutating the configured plugin state."""
    module = _load_sync()
    root = _marketplace_fixture(tmp_path)
    plugin = root / "plugins" / "codex-rig"
    if tamper == "malformed-manifest":
        (plugin / "package-manifest.json").write_text("{", encoding="utf-8")
    elif tamper == "hash-mismatch":
        (plugin / "shared" / "github_read.py").write_text('"""Modified fixture reader."""\n', encoding="utf-8")
    else:
        (plugin / "scripts" / "_safe_package_io.py").unlink()
    calls: list[tuple[str, ...]] = []

    with pytest.raises(module.SyncError, match="selected Codex Rig package failed verification"):
        module.sync_codex(
            module.parse_args(["--codex-ref", "codex-rig-v0.2.4", "--no-codex-global-agents"]),
            run=_fake_runner(root, calls, configured_ref="codex-rig-v0.2.4"),
            environ={},
            stdout=io.StringIO(),
        )

    assert not any(
        call[1:4] in {("plugin", "marketplace", "upgrade"), ("plugin", "marketplace", "remove")}
        or call[1:3] in {("plugin", "remove"), ("plugin", "add")}
        for call in calls
    )


def test_native_sync_requires_both_managed_plugins_after_install(tmp_path: Path) -> None:
    """Reject a partial restore that enables Codex Rig without Codemap."""
    module = _load_sync()
    root = _marketplace_fixture(tmp_path)
    calls: list[tuple[str, ...]] = []

    def _run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        """Return deterministic marketplace and plugin-list responses for restore checks."""
        rendered = tuple(str(item) for item in command)
        calls.append(rendered)
        if rendered[1:5] == ("plugin", "marketplace", "list", "--json"):
            payload = {"marketplaces": [{"name": "borda-ai-rig", "root": str(root)}]}
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        if rendered[1:5] == ("plugin", "list", "--marketplace", "borda-ai-rig"):
            payload = {"installed": [{"pluginId": "codex-rig@borda-ai-rig", "enabled": True, "version": "0.3.0"}]}
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        if rendered[:3] == ("git", "-C", str(root)):
            return subprocess.CompletedProcess(command, 0, "0123456789abcdef\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    with pytest.raises(module.SyncError, match="Codemap is not uniquely enabled"):
        module.sync_codex(
            module.parse_args(["--no-codex-global-agents"]),
            run=_run,
            environ={},
            stdout=io.StringIO(),
        )


@pytest.mark.parametrize(
    "arguments",
    (
        pytest.param(["install", "--no-codex-global-agents"], id="install"),
        pytest.param(["clear"], id="clear"),
    ),
)
def test_native_sync_preserves_rule_helper_status_before_partial_failure(tmp_path: Path, arguments: list[str]) -> None:
    """Show completed rule changes when the helper reports a later partial failure."""
    module = _load_sync()
    root = _marketplace_fixture(tmp_path)
    calls: list[tuple[str, ...]] = []
    base_run = _fake_runner(root, calls)

    def _run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Return a rule-helper partial failure and delegate unrelated commands."""
        if len(command) > 1 and Path(command[1]).name == "install_github_read_rules.py":
            calls.append(tuple(command))
            return subprocess.CompletedProcess(command, 2, "rules created\n", "github-read-rules-partial\n")
        return base_run(command, **kwargs)

    output = io.StringIO()
    with pytest.raises(module.SyncError, match="github-read-rules-partial"):
        module.sync_codex(module.parse_args(arguments), run=_run, environ={}, stdout=output)

    assert "rules created" in output.getvalue()


def test_native_sync_clear_removes_plugin_and_managed_block(tmp_path: Path) -> None:
    """Route teardown through the same portable Python entrypoint."""
    module = _load_sync()
    calls: list[tuple[str, ...]] = []

    def _run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        """Record clear commands and return successful empty process results."""
        calls.append(tuple(str(item) for item in command))
        return subprocess.CompletedProcess(command, 0, "", "")

    result = module.sync_codex(
        module.parse_args(["clear"]),
        run=_run,
        environ={"CODEX_HOME": str(tmp_path / "home")},
        stdout=io.StringIO(),
    )

    assert result == 0
    assert ("codex", "plugin", "remove", "codex-rig@borda-ai-rig") in calls
    assert ("codex", "plugin", "remove", "codemap-py@borda-ai-rig") in calls
    assert ("codex", "plugin", "remove", "bridge@borda-ai-rig") in calls
    assert any(
        call[1:]
        == (
            str(PLUGIN_ROOT / "scripts" / "install_global_agents.py"),
            "--remove",
            "--codex-home",
            str(tmp_path / "home"),
        )
        for call in calls
    )
    rules_removal = next(call for call in calls if any("install_github_read_rules.py" in item for item in call))
    assert rules_removal[2:] == ("--remove", "--codex-home", str(tmp_path / "home"))
    assert calls.index(rules_removal) < calls.index(("codex", "plugin", "remove", "codex-rig@borda-ai-rig"))


@pytest.mark.integration
def test_sync_executes_rule_setup_and_clear_in_isolated_home(tmp_path: Path) -> None:
    """Exercise real rule lifecycle helpers while replacing only external marketplace commands."""
    module = _load_sync()
    root = _marketplace_fixture(tmp_path)
    helper = root / "plugins" / "codex-rig" / "scripts" / "install_github_read_rules.py"
    helper.write_bytes((PLUGIN_ROOT / "scripts" / helper.name).read_bytes())
    for filename in ("_package_identity.py", "_safe_package_io.py"):
        (helper.parent / filename).write_bytes((PLUGIN_ROOT / "scripts" / filename).read_bytes())
    _write_fixture_package_manifest(helper.parents[1], "0.3.0")
    home = tmp_path / "home"
    installed = home / "plugins" / "cache" / "borda-ai-rig" / "codex-rig" / "0.3.0"
    (installed / ".codex-plugin").mkdir(parents=True)
    (installed / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "codex-rig", "version": "0.3.0"}), encoding="utf-8"
    )
    (installed / "shared").mkdir()
    (installed / "shared" / "github_read.py").write_text('"""Fixture reader."""\n', encoding="utf-8")
    _write_fixture_package_manifest(installed, "0.3.0")
    calls: list[tuple[str, ...]] = []
    external = _fake_runner(root, calls)

    def _run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Execute the permission helper and replace networked platform commands only."""
        if len(command) > 1 and Path(command[1]).name == "install_github_read_rules.py":
            calls.append(tuple(command))
            return subprocess.run(command, **kwargs)
        return external(command, **kwargs)

    # Skipping global instructions must not skip the separately managed reader rules.
    assert (
        module.sync_codex(
            module.parse_args(["install", "--no-codex-global-agents"]),
            run=_run,
            environ={"CODEX_HOME": str(home)},
            stdout=io.StringIO(),
        )
        == 0
    )
    managed = home / "rules" / "codex-rig-github-read.rules"
    payload = managed.read_text(encoding="utf-8")
    assert json.dumps(str(installed / "shared" / "github_read.py")) in payload
    assert (
        module.sync_codex(
            module.parse_args(["clear"]),
            run=_run,
            environ={"CODEX_HOME": str(home)},
            stdout=io.StringIO(),
        )
        == 0
    )
    assert not managed.exists()


def test_native_sync_clear_removes_managed_block_when_codex_is_missing(tmp_path: Path) -> None:
    """Keep local teardown usable after the Codex executable was removed."""
    module = _load_sync()
    calls: list[tuple[str, ...]] = []

    def _run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        """Model a missing Codex executable while allowing local teardown to succeed."""
        calls.append(tuple(str(item) for item in command))
        if command[0] == "codex":
            raise FileNotFoundError("codex")
        return subprocess.CompletedProcess(command, 0, "managed block removed\n", "")

    output = io.StringIO()
    result = module.sync_codex(
        module.parse_args(["clear"]),
        run=_run,
        environ={"CODEX_HOME": str(tmp_path / "home")},
        stdout=output,
    )

    assert result == 0
    assert "not installed" in output.getvalue()
    assert "managed block removed" in output.getvalue()
    assert ("codex", "plugin", "remove", "codex-rig@borda-ai-rig") in calls
    assert ("codex", "plugin", "remove", "codemap-py@borda-ai-rig") in calls
    assert ("codex", "plugin", "remove", "bridge@borda-ai-rig") in calls
    assert any(
        call[1:]
        == (
            str(PLUGIN_ROOT / "scripts" / "install_global_agents.py"),
            "--remove",
            "--codex-home",
            str(tmp_path / "home"),
        )
        for call in calls
    )


def test_system_runner_resolves_simulated_windows_batch_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """Launch a resolved Codex batch shim through the Windows command processor."""
    module = _load_sync()

    def _which(command: str) -> str | None:
        """Resolve only the simulated Windows batch launcher."""
        return {"codex": r"C:\Program Files\Codex\codex.cmd"}.get(command)

    monkeypatch.setattr(module.shutil, "which", _which)

    resolved, shell = module._resolve_system_command(["codex", "plugin", "list"], windows=True)

    assert resolved == r'"C:\Program Files\Codex\codex.cmd" plugin list'
    assert shell is True


@pytest.mark.parametrize(
    "argument",
    (
        "main&whoami",
        "main|whoami",
        "main<in",
        "main>out",
        "main^x",
        "main(x)",
        "main%x%",
        "main!x!",
        'main"x',
        "main x",
    ),
)
def test_system_runner_rejects_simulated_windows_batch_shell_syntax(
    monkeypatch: pytest.MonkeyPatch, argument: str
) -> None:
    """Reject user-controlled values that cmd.exe could reinterpret."""
    module = _load_sync()
    monkeypatch.setattr(module.shutil, "which", lambda _command: r"C:\fixture\codex.cmd")

    with pytest.raises(OSError, match="unsafe Windows batch command"):
        module._resolve_system_command(["codex", "plugin", "add", argument], windows=True)
