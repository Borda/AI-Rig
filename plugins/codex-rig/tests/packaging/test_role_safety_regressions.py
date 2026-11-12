"""Exercise bounded role diagnosis and executable verification at runtime boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from _modules import load_module_scoped
from _platform import DIRECTORY_SYMLINKS_AVAILABLE, FILE_SYMLINKS_AVAILABLE, POSIX_DESCRIPTOR_PRIMITIVES_AVAILABLE

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
_requires_posix_descriptors = pytest.mark.skipif(
    not POSIX_DESCRIPTOR_PRIMITIVES_AVAILABLE, reason="host lacks descriptor-relative no-follow reads"
)


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load the manager with its installed sibling imports available."""
    monkeypatch.syspath_prepend(str(SCRIPTS))
    return load_module_scoped("role_safety_manager", SCRIPTS / "manage_role_agents.py", monkeypatch)


@pytest.fixture
def verifier(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load the linked-role verifier without invoking its command line."""
    return load_module_scoped("role_safety_verifier", SCRIPTS / "verify_role_link.py", monkeypatch)


def test_windows_discovery_excludes_working_directory(
    manager: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PATH entry naming the working directory cannot override the trusted Codex launcher."""
    current = tmp_path / "current"
    trusted = tmp_path / "trusted"
    current.mkdir()
    trusted.mkdir()
    planted = current / "codex.exe"
    selected = trusted / "codex.exe"
    planted.write_bytes(b"planted")
    selected.write_bytes(b"trusted")
    planted.chmod(0o700)
    selected.chmod(0o700)
    monkeypatch.chdir(current)
    monkeypatch.setenv("PATH", os.pathsep.join((str(current), str(trusted))))
    monkeypatch.setattr(manager.sys, "platform", "win32")
    monkeypatch.setattr(
        manager.shutil,
        "which",
        lambda command: str(Path(command).with_suffix(".exe")) if Path(command).is_absolute() else str(planted),
    )
    assert manager._codex_executable(None) == selected


def test_doctor_refuses_oversized_metadata_before_oracle(
    manager: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The active-package oracle refuses metadata exceeding the configured copy budget."""
    home = tmp_path / "home"
    plugin = home / "plugins" / "cache" / "borda-ai-rig" / "codex-rig" / "1.0.0"
    plugin.mkdir(parents=True)
    (home / "config.toml").write_bytes(b"x" * 32)
    monkeypatch.setattr(manager, "MAX_DOCTOR_METADATA_BYTES", 16, raising=False)
    result = manager._active_package_check(plugin, home, tmp_path / "missing-codex", "0" * 64, "1.0.0")
    assert result.status == "degraded"
    assert "metadata byte limit" in result.detail


def test_doctor_refuses_excess_metadata_entries(
    manager: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Directory-only metadata cannot evade the diagnosis copy entry limit."""
    home = tmp_path / "home"
    plugin = home / "plugins" / "cache" / "borda-ai-rig" / "codex-rig" / "1.0.0"
    plugin.mkdir(parents=True)
    monkeypatch.setattr(manager, "MAX_DOCTOR_METADATA_ENTRIES", 2, raising=False)
    result = manager._active_package_check(plugin, home, tmp_path / "missing-codex", "0" * 64, "1.0.0")
    assert result.status == "degraded"
    assert "metadata entry limit" in result.detail


@pytest.mark.parametrize("relative", ["config.toml", ".tmp/marketplaces/borda-ai-rig/.agents/plugins/marketplace.json"])
def test_doctor_refuses_nonregular_selected_metadata(manager: ModuleType, tmp_path: Path, relative: str) -> None:
    """A directory cannot masquerade as a selected listing metadata file."""
    home = tmp_path / "home"
    (home / relative).mkdir(parents=True)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    with pytest.raises(OSError, match="metadata file is not ordinary"):
        manager._copy_doctor_metadata(home, sandbox)


@pytest.mark.skipif(not FILE_SYMLINKS_AVAILABLE, reason="host cannot create file symlinks")
def test_doctor_refuses_linked_listing_metadata(manager: ModuleType, tmp_path: Path) -> None:
    """Copied configuration never links the disposable oracle back into the original home."""
    home = tmp_path / "home"
    home.mkdir()
    original = tmp_path / "original.toml"
    original.write_bytes(b"private configuration")
    (home / "config.toml").symlink_to(original)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    with pytest.raises(OSError, match="metadata file is not ordinary"):
        manager._copy_doctor_metadata(home, sandbox)
    assert original.read_bytes() == b"private configuration"


@pytest.mark.skipif(not DIRECTORY_SYMLINKS_AVAILABLE, reason="host cannot create directory symlinks")
def test_doctor_refuses_linked_cache_version(manager: ModuleType, tmp_path: Path) -> None:
    """A linked installed-version directory cannot expose an original runtime to the oracle."""
    home = tmp_path / "home"
    cache = home / "plugins" / "cache" / "borda-ai-rig" / "codex-rig"
    cache.mkdir(parents=True)
    original = tmp_path / "original-runtime"
    original.mkdir()
    (cache / "1.0.0").symlink_to(original, target_is_directory=True)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    with pytest.raises(OSError, match="metadata directory is not ordinary"):
        manager._copy_doctor_metadata(home, sandbox)


def test_doctor_preserves_manifestless_version_directories(manager: ModuleType, tmp_path: Path) -> None:
    """The native listing's version-directory selection survives missing version manifests."""
    home = tmp_path / "home"
    cache = Path("plugins/cache/borda-ai-rig/codex-rig")
    (home / cache / "1.0.0").mkdir(parents=True)
    (home / cache / "2.0.0").mkdir()
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    manager._copy_doctor_metadata(home, sandbox)
    assert sorted(path.name for path in (sandbox / cache).iterdir()) == ["1.0.0", "2.0.0"]


@_requires_posix_descriptors
def test_doctor_runs_oracle_with_bounded_disposable_metadata(
    manager: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only listing metadata reaches the oracle, even when installed runtimes exceed its copy budget."""
    home = tmp_path / "home"
    plugin = home / "plugins" / "cache" / "borda-ai-rig" / "codex-rig" / "1.0.0"
    manifest = plugin / ".codex-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"name": "codex-rig", "version": "1.0.0"}))
    marketplace = home / ".tmp" / "marketplaces" / "borda-ai-rig" / ".agents" / "plugins" / "marketplace.json"
    marketplace.parent.mkdir(parents=True)
    marketplace.write_text(json.dumps({"name": "borda-ai-rig", "plugins": [{"name": "codex-rig"}]}))
    (home / "config.toml").write_bytes(b"configuration")
    runtime = home / "plugins" / ".plugin-appserver" / "runtime"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"x" * 4096)
    (plugin / "runtime").write_bytes(b"x" * 4096)
    (marketplace.parents[2] / "runtime").write_bytes(b"x" * 4096)
    monkeypatch.setattr(manager, "MAX_DOCTOR_METADATA_BYTES", 1024)
    binary = tmp_path / "codex"
    code = (
        "import json,os;from pathlib import Path;home=Path(os.environ['CODEX_HOME']);"
        "assert (home/'config.toml').read_bytes()==b'configuration';"
        "manifest=json.loads((home/'plugins/cache/borda-ai-rig/codex-rig/1.0.0/.codex-plugin/plugin.json').read_text());"
        "market=json.loads((home/'.tmp/marketplaces/borda-ai-rig/.agents/plugins/marketplace.json').read_text());"
        "assert market['name']=='borda-ai-rig' and market['plugins'][0]['name']==manifest['name'];"
        "assert not list(home.rglob('runtime'));"
        "(home/'config.toml').write_text('oracle disposable write');"
        "print(json.dumps({'installed':[{'pluginId':manifest['name']+'@'+market['name'],"
        "'name':manifest['name'],'marketplaceName':market['name'],'installed':True,'enabled':True,"
        "'version':manifest['version']}]}))"
    )
    binary.write_text(f"#!{sys.executable}\n{code}\n")
    binary.chmod(0o700)
    result = manager._active_package_check(
        plugin, home, binary, hashlib.sha256(binary.read_bytes()).hexdigest(), "1.0.0"
    )
    assert result.status == "pass", result.detail
    assert (home / "config.toml").read_bytes() == b"configuration"
    assert runtime.stat().st_size == 4096
    assert manifest.read_text() == json.dumps({"name": "codex-rig", "version": "1.0.0"})


@_requires_posix_descriptors
def test_active_oracle_refuses_replaced_binary_before_execution(verifier: ModuleType, tmp_path: Path) -> None:
    """A replacement pathname is rejected before the active-package subprocess starts."""
    binary = tmp_path / "codex"
    binary.write_bytes(b"original")
    binary.chmod(0o700)
    path, descriptor, digest = verifier.open_absolute_executable(str(binary))
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"replacement")
    replacement.chmod(0o700)
    os.replace(replacement, binary)
    try:
        with pytest.raises(verifier.RoleUnavailable, match="codex-binary-transition"):
            verifier.active_plugin_root(path, tmp_path, "1.0.0", descriptor, digest)
    finally:
        os.close(descriptor)


@_requires_posix_descriptors
def test_active_oracle_refuses_in_place_binary_change(verifier: ModuleType, tmp_path: Path) -> None:
    """A same-inode rewrite between oracle calls cannot execute under the earlier digest."""
    binary = tmp_path / "codex"
    binary.write_bytes(b"original")
    binary.chmod(0o700)
    path, descriptor, digest = verifier.open_absolute_executable(str(binary))
    binary.write_bytes(b"modified")
    try:
        with pytest.raises(verifier.RoleUnavailable, match="codex-binary-transition"):
            verifier.active_plugin_root(path, tmp_path, "1.0.0", descriptor, digest)
    finally:
        os.close(descriptor)


@_requires_posix_descriptors
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="host cannot create a FIFO")
def test_linked_manifest_fifo_is_rejected_without_waiting(tmp_path: Path) -> None:
    """A FIFO masquerading as a manifest fails immediately instead of awaiting a writer."""
    os.mkfifo(tmp_path / "package-manifest.json")
    code = (
        "import os,sys;sys.path.insert(0,sys.argv[1]);import verify_role_link as v;"
        "fd=os.open(sys.argv[2],os.O_RDONLY|os.O_DIRECTORY);"
        "v.read_relative(fd,'package-manifest.json','package-manifest.json')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(SCRIPTS), str(tmp_path)],
        check=False,
        capture_output=True,
        timeout=3,
    )
    assert result.returncode != 0
    assert b"unsafe-file" in result.stderr


@_requires_posix_descriptors
def test_role_child_cannot_consume_parent_stdin() -> None:
    """The bounded subprocess receives EOF even when its parent has pending input."""
    code = (
        "import sys;sys.path.insert(0,sys.argv[1]);import verify_role_link as v;"
        "print(v.run_bounded([sys.executable,'-c','import sys;print(len(sys.stdin.read()))'])[1].decode().strip())"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(SCRIPTS)],
        input=b"private-input",
        check=False,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0
    assert result.stdout == b"0\n"
