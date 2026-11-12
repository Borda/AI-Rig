"""Check bounded reads, private backups, and permission-profile reporting."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from _github_read_helpers import installed_plugin
from _modules import load_module
from _platform import DIRECTORY_SYMLINKS_AVAILABLE, POSIX_FILE_MODES_AVAILABLE

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"


@pytest.fixture
def global_installer() -> ModuleType:
    """Load the global instruction installer without running its CLI."""
    return load_module("safety_global_installer", SCRIPTS / "install_global_agents.py")


@pytest.fixture
def profile_installer(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load the profile installer with its packaged imports available."""
    monkeypatch.syspath_prepend(str(SCRIPTS))
    return load_module("safety_profile_installer", SCRIPTS / "install_github_read_rules.py")


def test_profile_cli_discloses_effective_permissions(tmp_path: Path) -> None:
    """An initial setup explicitly reports the default and its write and network effects."""
    home = tmp_path / "home"
    plugin = installed_plugin(home)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "install_github_read_rules.py"),
            "--codex-home",
            str(home),
            "--plugin-root",
            str(plugin),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "default_permissions=local-workflow" in result.stdout
    assert ".git write access on every workspace root" in result.stdout
    assert "network_proxy=true" in result.stdout
    assert "local-workflow network disabled" in result.stdout


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("value = [\n" + '  "item",\n' * 1000 + "]\n", id="large-array"),
        pytest.param('value = """\n' + "text\n" * 1000 + '"""\n', id="large-string"),
    ],
)
def test_statement_splitter_bounds_parser_work(
    profile_installer: ModuleType, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    """A complete multiline statement is validated without repeatedly parsing its growing prefix."""
    loads = profile_installer.tomllib.loads
    parsed_sizes: list[int] = []

    def counted_loads(value: str) -> object:
        """Record parser input volume while retaining real TOML validation."""
        parsed_sizes.append(len(value))
        return loads(value)

    monkeypatch.setattr(profile_installer.tomllib, "loads", counted_loads)

    assert profile_installer._config_lines(content) == [content]
    assert sum(parsed_sizes) <= 2 * len(content)


@pytest.mark.parametrize(
    "statement",
    [
        'value = [\n "[permissions.github-read]", # ]\n { inner = "quoted \\" # [" },\n]\n',
        "value = '''\n[permissions.github-read]\nvalue = '''''\n",
        'value = """\n[permissions.github-read]\nvalue = """""\n',
        '"quoted.key" = "escaped \\" # ["\n',
        'value = """line \\\n  continuation\n"""\n',
    ],
)
def test_statement_splitter_preserves_opaque_values(profile_installer: ModuleType, statement: str) -> None:
    """Quotes, comments, arrays and multiline strings preserve exact statement boundaries."""
    suffix = "# untouched\n[custom]\nflag = true\n"

    assert profile_installer._config_lines(statement + suffix) == [
        statement,
        "# untouched\n",
        "[custom]\n",
        "flag = true\n",
    ]


@pytest.mark.parametrize("oversized", ["template", "target"])
def test_global_install_refuses_oversized_bytes(
    global_installer: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, oversized: str
) -> None:
    """Oversized source or target bytes prevent installation and backup creation."""
    source = tmp_path / "template.md"
    source.write_bytes(b"# Policy\n" if oversized == "target" else b"x" * 65)
    home = tmp_path / "home"
    home.mkdir()
    target = home / "AGENTS.md"
    target.write_bytes(b"x" * 65 if oversized == "target" else b"custom\n")
    original = target.read_bytes()
    monkeypatch.setattr(global_installer, "MAX_BYTES", 64, raising=False)

    with pytest.raises(global_installer.UnsafeGlobalAgentsState, match="bounded"):
        global_installer.install_global_agents(source, home)

    assert target.read_bytes() == original
    assert not (home / "backups").exists()


@pytest.mark.skipif(not DIRECTORY_SYMLINKS_AVAILABLE, reason="filesystem cannot create directory symlinks")
def test_global_backup_refuses_linked_directory(global_installer: ModuleType, tmp_path: Path) -> None:
    """A linked backup directory cannot redirect instruction bytes into another directory."""
    home = tmp_path / "home"
    home.mkdir()
    target = home / "AGENTS.md"
    target.write_bytes(b"private policy\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "backups").symlink_to(outside, target_is_directory=True)

    with pytest.raises(global_installer.UnsafeGlobalAgentsState, match="linked"):
        global_installer.backup_target(target, home, target.read_bytes())

    assert list(outside.iterdir()) == []


@pytest.mark.skipif(not POSIX_FILE_MODES_AVAILABLE, reason="filesystem does not preserve POSIX file modes")
def test_global_backup_is_private(global_installer: ModuleType, tmp_path: Path) -> None:
    """A world-readable target still produces a backup readable only by its owner."""
    home = tmp_path / "home"
    home.mkdir()
    target = home / "AGENTS.md"
    target.write_bytes(b"private policy\n")
    target.chmod(0o644)

    backup = global_installer.backup_target(target, home, target.read_bytes())

    assert backup.read_bytes() == target.read_bytes()
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


@pytest.mark.skipif(not POSIX_FILE_MODES_AVAILABLE, reason="filesystem does not preserve POSIX file modes")
def test_global_home_creation_is_private(global_installer: ModuleType, tmp_path: Path) -> None:
    """A new Codex home remains private under a permissive process umask."""
    source = tmp_path / "template.md"
    source.write_bytes(b"# Policy\n")
    home = tmp_path / "home"
    prior = os.umask(0)
    try:
        global_installer.install_global_agents(source, home)
    finally:
        os.umask(prior)

    assert stat.S_IMODE(home.stat().st_mode) == 0o700
