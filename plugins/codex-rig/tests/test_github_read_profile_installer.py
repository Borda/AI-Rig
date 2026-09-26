"""Exercise the GitHub evidence profile lifecycle against isolated Codex homes."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path, PureWindowsPath

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10/3.11 compatibility
    import tomli as tomllib


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install_github_read_rules.py"


def _installed_plugin(home: Path, version: str = "1.2.3") -> Path:
    """Create a verified installed package with the two GitHub evidence helpers."""
    root = home / "plugins" / "cache" / "borda-ai-rig" / "codex-rig" / version
    (root / ".codex-plugin").mkdir(parents=True)
    (root / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "codex-rig", "version": version}), encoding="utf-8"
    )
    (root / "shared").mkdir()
    (root / "shared" / "github_read.py").write_text('"""Fixture reader."""\n', encoding="utf-8")
    (root / "shared" / "collect_pr.py").write_text('"""Fixture collector."""\n', encoding="utf-8")
    files = [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "mode": f"{path.stat().st_mode & 0o777:04o}",
        }
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]
    reader = next(item for item in files if item["path"] == "shared/github_read.py")
    (root / "package-manifest.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "plugin": "codex-rig",
                "version": version,
                "files": files,
                "skills": [],
                "roles": [],
                "bootstrap": {"protocol": "fixture", "helper": reader["path"], "sha256": reader["sha256"]},
                "generator": {"version": "fixture", "path": reader["path"], "sha256": reader["sha256"]},
            }
        ),
        encoding="utf-8",
    )
    return root


def _run(home: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the public setup or clear CLI without touching the real Codex home."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments, "--codex-home", str(home)],
        capture_output=True,
        text=True,
        check=False,
    )


def _config(home: Path) -> dict[str, object]:
    """Parse installed configuration through the standard TOML reader."""
    return tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))


def test_windows_legacy_single_path_rules_are_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Accept owned legacy rules using the former single native Windows path."""
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("install_github_read_rules", SCRIPT)
    assert spec is not None and spec.loader is not None
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)

    home = PureWindowsPath("C:/fixture/.codex")
    root = home.joinpath("plugins", "cache", "borda-ai-rig", "codex-rig", "1.2.3", "shared")
    reader = root / "github_read.py"
    collector = root / "collect_pr.py"
    target = "https://github.com/example/project/pull/7"
    reader_body = (
        f'prefix_rule(pattern={json.dumps([["python", "python3"], str(reader)])}, decision="allow")\n'.encode()
    )
    reader_rule = (
        b"# codex-rig:github-read sha256=" + hashlib.sha256(reader_body).hexdigest().encode() + b"\n" + reader_body
    )
    collector_body = (
        f"prefix_rule(pattern={json.dumps([['python', 'python3'], str(collector), '--target', [target]])}, "
        'decision="allow")\n'
    ).encode()
    collector_rule = (
        b"# codex-rig:pr-collection sha256="
        + hashlib.sha256(collector_body).hexdigest().encode()
        + b"\n"
        + collector_body
    )

    installer._check_owned(reader_rule, home)
    assert installer._approved_prs(collector_rule, home) == [target]
    assert installer.strip_legacy_rules(collector_body + b"# unrelated\n", home) == b"# unrelated\n"
    installer._check_owned(installer.render_rules(reader), home)
    assert installer._approved_prs(installer.render_pr_rules(collector, [target]), home) == [target]


@pytest.mark.parametrize("line_ending", ["\n", "\r\n"])
def test_setup_repeat_and_clear_preserve_unrelated_config(tmp_path: Path, line_ending: str) -> None:
    """Own only the GitHub profile and restore prior root settings on clear."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    config = home / "config.toml"
    original = (
        "# preserve this user comment and ordering exactly\n"
        'model = "gpt-6-sol"\n'
        'default_permissions = "personal"\n'
        "\n[features]\n"
        "multi_agent = true\n"
        "network_proxy = false\n"
        "\n[profiles.personal]\n"
        'model_reasoning_effort = "high"\n'
    )
    original = original.replace("\n", line_ending)
    config.write_text(original, encoding="utf-8", newline="\n")

    result = _run(home, "--plugin-root", str(root))
    assert result.returncode == 0, result.stderr
    installed = _config(home)
    assert installed["model"] == "gpt-6-sol"
    assert installed["features"]["multi_agent"] is True
    assert installed["profiles"]["personal"] == {"model_reasoning_effort": "high"}
    assert installed["default_permissions"] == "personal"
    assert installed["features"]["network_proxy"] is True
    assert installed["permissions"]["github-read"] == {
        "extends": ":workspace",
        "filesystem": {":workspace_roots": {".git": "write"}},
        "network": {"enabled": True, "domains": {"api.github.com": "allow", "github.com": "allow"}},
    }
    first = config.read_bytes()

    assert _run(home, "--plugin-root", str(root)).returncode == 0
    assert config.read_bytes() == first
    assert _run(home, "--remove").returncode == 0
    assert config.read_bytes() == original.encode("utf-8")
    assert _config(home) == tomllib.loads(original)


def test_setup_rejects_proxy_change_that_revokes_selected_profile_network(tmp_path: Path) -> None:
    """Refuse setup when enabling the proxy would restrict an existing default profile."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    config = home / "config.toml"
    original = (
        b'default_permissions = "personal"\n'
        b"\n[features]\n"
        b"network_proxy = false\n"
        b"\n[permissions.personal.network]\n"
        b"enabled = true\n"
    )
    assert tomllib.loads(original.decode())["permissions"]["personal"]["network"] == {"enabled": True}
    config.write_bytes(original)
    rules = home / "rules"
    rules.mkdir()
    user_rule = rules / "default.rules"
    rule_bytes = b'prefix_rule(pattern=["git", "status"], decision="allow")\n'
    user_rule.write_bytes(rule_bytes)

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode != 0
    assert "network-enabled permission profile" in result.stderr
    assert config.read_bytes() == original
    assert user_rule.read_bytes() == rule_bytes
    assert not (home / "codex-rig-github-read-profile.json").exists()
    assert not (rules / "codex-rig-github-read.rules").exists()
    assert not (home / "backups" / "codex-rig").exists()


def test_setup_rejects_proxy_change_for_nondefault_network_profile(tmp_path: Path) -> None:
    """Protect a profile that may be selected after setup, even if it is not the default."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    config = home / "config.toml"
    original = b"[features]\nnetwork_proxy = false\n\n[permissions.research.network]\nenabled = true\n"
    config.write_bytes(original)

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode != 0
    assert "network-enabled permission profile" in result.stderr
    assert config.read_bytes() == original
    assert not (home / "codex-rig-github-read-profile.json").exists()


def test_setup_rejects_legacy_sandbox_override_before_write(tmp_path: Path) -> None:
    """Reject setup when a retained sandbox mode would override permission profiles."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    config = home / "config.toml"
    original = b'default_permissions = "personal"\nsandbox_mode = "workspace-write"\n'
    config.write_bytes(original)
    rules = home / "rules"
    rules.mkdir()
    user_rule = rules / "default.rules"
    rule_bytes = b'prefix_rule(pattern=["git", "status"], decision="allow")\n'
    user_rule.write_bytes(rule_bytes)

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode != 0
    assert "legacy sandbox_mode overrides permission profiles" in result.stderr
    assert config.read_bytes() == original
    assert user_rule.read_bytes() == rule_bytes
    assert not (home / "codex-rig-github-read-profile.json").exists()
    assert not (rules / "codex-rig-github-read.rules").exists()
    assert not (home / "backups" / "codex-rig").exists()


def test_setup_rejects_selectable_profile_sandbox_override_before_write(tmp_path: Path) -> None:
    """Reject a profile sandbox mode that would override a later opt-in selection."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    config = home / "config.toml"
    original = b'[profiles.personal]\nsandbox_mode = "workspace-write"\n'
    config.write_bytes(original)
    rules = home / "rules"
    rules.mkdir()
    user_rule = rules / "default.rules"
    rule_bytes = b'prefix_rule(pattern=["git", "status"], decision="allow")\n'
    user_rule.write_bytes(rule_bytes)

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode != 0
    assert "profile sandbox_mode overrides default_permissions" in result.stderr
    assert config.read_bytes() == original
    assert user_rule.read_bytes() == rule_bytes
    assert not (home / "codex-rig-github-read-profile.json").exists()
    assert not (rules / "codex-rig-github-read.rules").exists()
    assert not (home / "backups" / "codex-rig").exists()


@pytest.mark.parametrize(
    "initial_proxy,network_enabled",
    [
        pytest.param(True, True, id="proxy-already-enabled"),
        pytest.param(False, False, id="existing-profile-network-disabled"),
    ],
)
def test_setup_accepts_safe_existing_permission_profile(
    tmp_path: Path, initial_proxy: bool, network_enabled: bool
) -> None:
    """Keep setup available when it cannot newly restrict another profile's network."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    config = home / "config.toml"
    original = (
        f"[features]\nnetwork_proxy = {str(initial_proxy).lower()}\n"
        f"\n[permissions.personal.network]\nenabled = {str(network_enabled).lower()}\n"
    )
    config.write_text(original, encoding="utf-8", newline="\n")

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode == 0, result.stderr
    installed = _config(home)
    assert installed["features"]["network_proxy"] is True
    assert installed["permissions"]["personal"]["network"] == {"enabled": network_enabled}
    assert _run(home, "--remove").returncode == 0
    assert config.read_bytes() == original.encode()


@pytest.mark.parametrize(
    "selected",
    [pytest.param('"github-read"', id="literal"), pytest.param('"\\u0067ithub-read"', id="escaped")],
)
def test_setup_rejects_preexisting_global_github_read_default(tmp_path: Path, selected: str) -> None:
    """Keep an existing root selection from making the installed profile global."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    config = home / "config.toml"
    original = f'default_permissions = {selected}\nmodel = "gpt-6-sol"\n'
    config.write_text(original, encoding="utf-8", newline="\n")

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode != 0
    assert "github-read is already the global default" in result.stderr
    assert config.read_bytes() == original.encode()
    assert not (home / "codex-rig-github-read-profile.json").exists()
    assert _run(home, "--remove").returncode == 0
    assert config.read_bytes() == original.encode()


def test_setup_rejects_multiline_user_instructions_before_write(tmp_path: Path) -> None:
    """Unsafe line edits inside a TOML string must leave config unchanged."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    config = home / "config.toml"
    original = 'developer_instructions = """\nKeep this example:\ndefault_permissions = "private"\n"""\n'
    config.write_text(original, encoding="utf-8", newline="\n")

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode != 0
    assert config.read_bytes() == original.encode("utf-8")
    assert not (home / "codex-rig-github-read-profile.json").exists()


def test_setup_rejects_multiline_default_permissions_before_write(tmp_path: Path) -> None:
    """A valid multiline permission value must not be silently rewritten."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    config = home / "config.toml"
    original = 'default_permissions = """\n[features]\nnetwork_proxy = true\n"""\n'
    assert tomllib.loads(original)["default_permissions"] == "[features]\nnetwork_proxy = true\n"
    config.write_text(original, encoding="utf-8", newline="\n")

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode != 0
    assert config.read_bytes() == original.encode("utf-8")


@pytest.mark.parametrize(
    "original",
    [
        '"permissions"."github-read" = { extends = ":workspace" }\n',
        '"features".network_proxy = false\n',
        '["\\u0066eatures"]\nnetwork_proxy = false\n',
    ],
)
def test_setup_rejects_unsupported_equivalent_profile_keys_before_write(tmp_path: Path, original: str) -> None:
    """Valid quoted keys must not become duplicate managed TOML tables."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    config = home / "config.toml"
    assert tomllib.loads(original)
    config.write_text(original, encoding="utf-8", newline="\n")

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode != 0
    assert config.read_bytes() == original.encode("utf-8")
    assert _run(home, "--remove").returncode == 0
    assert _config(home) == tomllib.loads(original)


def test_clear_preserves_unrelated_edits_made_after_setup(tmp_path: Path) -> None:
    """A later user change outside the owned profile survives removal."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    config = home / "config.toml"
    config.write_text('model = "gpt-6-sol"\n', encoding="utf-8", newline="\n")
    assert _run(home, "--plugin-root", str(root)).returncode == 0
    installed = config.read_text(encoding="utf-8")
    assert installed.count('model = "gpt-6-sol"') == 1
    config.write_text(installed.replace('model = "gpt-6-sol"', 'model = "gpt-6-luna"'), encoding="utf-8", newline="\n")

    result = _run(home, "--remove")

    assert result.returncode == 0, result.stderr
    assert _config(home) == {"model": "gpt-6-luna"}


def test_comment_named_features_does_not_keep_created_table_on_clear(tmp_path: Path) -> None:
    """A commented table name must not count as an original TOML table."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    config = home / "config.toml"
    original = '# [features]\nmodel = "gpt-6-sol"\n'
    config.write_text(original, encoding="utf-8", newline="\n")

    setup = _run(home, "--plugin-root", str(root))
    assert setup.returncode == 0, setup.stderr
    installed = config.read_text(encoding="utf-8")
    assert tomllib.loads(installed)["features"]["network_proxy"] is True
    config.write_text(installed.replace('model = "gpt-6-sol"', 'model = "gpt-6-luna"'), encoding="utf-8", newline="\n")

    clear = _run(home, "--remove")
    assert clear.returncode == 0, clear.stderr
    assert config.read_text(encoding="utf-8") == original.replace("gpt-6-sol", "gpt-6-luna")


def test_root_permission_comment_and_order_survive_setup_and_edited_clear(tmp_path: Path) -> None:
    """The user's root setting keeps its neighboring comment through both CLI actions."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    config = home / "config.toml"
    original = 'model = "gpt-6-sol"\n# chosen permissions\ndefault_permissions = "personal"\nother = true\n'
    config.write_text(original, encoding="utf-8", newline="\n")

    setup = _run(home, "--plugin-root", str(root))
    assert setup.returncode == 0, setup.stderr
    installed = config.read_text(encoding="utf-8")
    assert original in installed
    assert tomllib.loads(installed)["default_permissions"] == "personal"
    config.write_text(installed.replace('model = "gpt-6-sol"', 'model = "gpt-6-luna"'), encoding="utf-8", newline="\n")

    clear = _run(home, "--remove")
    assert clear.returncode == 0, clear.stderr
    assert config.read_text(encoding="utf-8") == original.replace("gpt-6-sol", "gpt-6-luna")


def test_clear_preserves_later_root_permission_edit(tmp_path: Path) -> None:
    """Remove the owned profile while retaining a later valid user root choice."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    config = home / "config.toml"
    original = (
        'default_permissions = "personal"\n'
        "\n[permissions.personal]\n"
        'extends = ":workspace"\n'
        "\n[permissions.backup]\n"
        'extends = ":workspace"\n'
    )
    config.write_text(original, encoding="utf-8", newline="\n")
    assert _run(home, "--plugin-root", str(root)).returncode == 0
    before = 'default_permissions = "personal"'
    after = 'default_permissions = "backup"'
    installed = config.read_text(encoding="utf-8")
    assert installed.count(before) == 1
    config.write_text(installed.replace(before, after), encoding="utf-8", newline="\n")

    result = _run(home, "--remove")

    assert result.returncode == 0, result.stderr
    assert _config(home) == tomllib.loads(original.replace(before, after))
    assert after in config.read_text(encoding="utf-8")
    assert not (home / "codex-rig-github-read-profile.json").exists()


def test_clear_rejects_later_selection_of_managed_profile(tmp_path: Path) -> None:
    """Avoid leaving a default that names the profile being removed."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    assert _run(home, "--plugin-root", str(root)).returncode == 0
    config = home / "config.toml"
    selected = 'default_permissions = "github-read"\n' + config.read_text(encoding="utf-8")
    config.write_text(selected, encoding="utf-8", newline="\n")
    state_path = home / "codex-rig-github-read-profile.json"
    state = state_path.read_bytes()

    result = _run(home, "--remove")

    assert result.returncode != 0
    assert config.read_text(encoding="utf-8") == selected
    assert state_path.read_bytes() == state


def test_clear_preserves_prior_network_proxy_after_unrelated_edit(tmp_path: Path) -> None:
    """Restore a disabled proxy when another setting changes after setup."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    config = home / "config.toml"
    original = 'model = "gpt-6-sol"\n\n[features]\nnetwork_proxy = false\n'
    config.write_text(original, encoding="utf-8", newline="\n")
    assert _run(home, "--plugin-root", str(root)).returncode == 0
    installed = config.read_text(encoding="utf-8")
    config.write_text(installed.replace('model = "gpt-6-sol"', 'model = "gpt-6-luna"'), encoding="utf-8", newline="\n")

    result = _run(home, "--remove")

    assert result.returncode == 0, result.stderr
    assert _config(home) == {"model": "gpt-6-luna", "features": {"network_proxy": False}}


def test_clear_rejects_new_network_profile_before_write(tmp_path: Path) -> None:
    """Do not disable the global proxy after a user adds a network-enabled profile."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    config = home / "config.toml"
    config.write_bytes(b"[features]\nnetwork_proxy = false\n")
    rules = home / "rules"
    rules.mkdir()
    user_rule = rules / "default.rules"
    rule_bytes = b'prefix_rule(pattern=["git", "status"], decision="allow")\n'
    user_rule.write_bytes(rule_bytes)
    assert _run(home, "--plugin-root", str(root)).returncode == 0
    state_path = home / "codex-rig-github-read-profile.json"
    state_bytes = state_path.read_bytes()
    config.write_bytes(config.read_bytes() + b"\n[permissions.personal.network]\nenabled = true\n")
    original = config.read_bytes()
    assert tomllib.loads(original.decode())["permissions"]["personal"]["network"] == {"enabled": True}
    backups = home / "backups" / "codex-rig"
    prior_backups = set(backups.iterdir())

    result = _run(home, "--remove")

    assert result.returncode != 0
    assert "disabling network_proxy" in result.stderr
    assert config.read_bytes() == original
    assert user_rule.read_bytes() == rule_bytes
    assert state_path.read_bytes() == state_bytes
    assert set(backups.iterdir()) == prior_backups


def test_setup_and_clear_without_original_config(tmp_path: Path) -> None:
    """Remove created profile state when the Codex home had no configuration."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    assert _run(home, "--plugin-root", str(root)).returncode == 0
    installed = _config(home)
    assert "default_permissions" not in installed
    assert installed["features"]["network_proxy"] is True
    assert installed["permissions"]["github-read"]["network"]["domains"] == {
        "api.github.com": "allow",
        "github.com": "allow",
    }
    assert _run(home, "--remove").returncode == 0
    assert not (home / "config.toml").exists()


def test_fresh_setup_retries_after_state_write_without_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Resume a first install interrupted between its state and config writes."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    config = home / "config.toml"
    state_path = home / "codex-rig-github-read-profile.json"

    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("install_github_read_rules", SCRIPT)
    assert spec is not None and spec.loader is not None
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    apply_change = installer._apply_change

    def fail_config_write(target: Path, payload: bytes | None, existing: bytes | None) -> str:
        """Interrupt the first config write after the real installer persists state."""
        if target == config:
            raise OSError("injected config write failure")
        return apply_change(target, payload, existing)

    with monkeypatch.context() as patch:
        patch.setattr(installer, "_apply_change", fail_config_write)
        with pytest.raises(OSError, match="injected config write failure"):
            list(installer.sync_github_read_profile(home, root))

    assert not config.exists()
    state_bytes = state_path.read_bytes()
    state = json.loads(state_bytes.split(b"\n", 1)[1])
    assert state["schema"] == 2
    assert state["original"] is None

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode == 0, result.stderr
    assert state_path.read_bytes() == state_bytes
    installed = _config(home)
    assert "default_permissions" not in installed
    assert installed["features"]["network_proxy"] is True
    assert installed["permissions"]["github-read"]["extends"] == ":workspace"
    assert _run(home, "--remove").returncode == 0
    assert not config.exists()
    assert not state_path.exists()


def test_fresh_setup_rejects_foreign_state_without_config(tmp_path: Path) -> None:
    """A real state from a configured home cannot authorize a blank home's recovery."""
    foreign_home = tmp_path / "foreign-home"
    foreign_root = _installed_plugin(foreign_home)
    (foreign_home / "config.toml").write_text('model = "other"\n', encoding="utf-8", newline="\n")
    installed = _run(foreign_home, "--plugin-root", str(foreign_root))
    assert installed.returncode == 0, installed.stderr
    state_bytes = (foreign_home / "codex-rig-github-read-profile.json").read_bytes()
    assert json.loads(state_bytes.split(b"\n", 1)[1])["original"] == 'model = "other"\n'

    home = tmp_path / "home"
    root = _installed_plugin(home)
    state_path = home / "codex-rig-github-read-profile.json"
    state_path.write_bytes(state_bytes)

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode != 0
    assert not (home / "config.toml").exists()
    assert state_path.read_bytes() == state_bytes


def test_fresh_setup_rejects_noncanonical_empty_origin_state(tmp_path: Path) -> None:
    """A checksum-valid state with extra fields does not prove an interrupted first install."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    assert _run(home, "--plugin-root", str(root)).returncode == 0
    state_path = home / "codex-rig-github-read-profile.json"
    state = json.loads(state_path.read_bytes().split(b"\n", 1)[1])
    assert state["original"] is None
    state["foreign_field"] = "unverified"
    body = json.dumps(state, sort_keys=True, ensure_ascii=False).encode()
    state_bytes = (
        b"# codex-rig:github-read-profile sha256=" + hashlib.sha256(body).hexdigest().encode() + b"\n" + body + b"\n"
    )
    state_path.write_bytes(state_bytes)
    (home / "config.toml").unlink()

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode != 0
    assert not (home / "config.toml").exists()
    assert state_path.read_bytes() == state_bytes


def test_setup_migrates_verified_automatic_profile_to_opt_in(tmp_path: Path) -> None:
    """Upgrade a previously owned default profile without retaining its automatic selection."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    original = 'model = "gpt-6-sol"\n'
    legacy = (
        original
        + 'default_permissions = "github-read" # codex-rig:github-read\n'
        + "\n[features]\n"
        + "network_proxy = true # codex-rig:github-read\n"
        + "\n# codex-rig:github-read profile begin\n"
        + "[permissions.github-read]\n"
        + 'extends = ":workspace"\n\n'
        + '[permissions.github-read.filesystem.":workspace_roots"]\n'
        + '".git" = "write"\n\n'
        + "[permissions.github-read.network]\n"
        + "enabled = true\n\n"
        + "[permissions.github-read.network.domains]\n"
        + '"api.github.com" = "allow"\n'
        + '"github.com" = "allow"\n'
        + "# codex-rig:github-read profile end\n"
    )
    (home / "config.toml").write_text(legacy, encoding="utf-8", newline="\n")
    state = {
        "schema": 1,
        "original": original,
        "installed_sha256": hashlib.sha256(legacy.encode()).hexdigest(),
        "settings": {"default_permissions": [], "sandbox_mode": [], "network_proxy": []},
    }
    body = json.dumps(state, sort_keys=True, ensure_ascii=False).encode()
    (home / "codex-rig-github-read-profile.json").write_bytes(
        b"# codex-rig:github-read-profile sha256=" + hashlib.sha256(body).hexdigest().encode() + b"\n" + body + b"\n"
    )
    (home / "config.toml").write_text(legacy.replace("gpt-6-sol", "gpt-6-luna"), encoding="utf-8", newline="\n")

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode == 0, result.stderr
    assert "default_permissions" not in _config(home)
    assert _config(home)["model"] == "gpt-6-luna"
    assert "github-read" in _config(home)["permissions"]
    assert _run(home, "--plugin-root", str(root)).returncode == 0
    assert _run(home, "--remove").returncode == 0
    assert (home / "config.toml").read_text(encoding="utf-8") == original.replace("gpt-6-sol", "gpt-6-luna")


@pytest.mark.parametrize(
    "original",
    [
        pytest.param(
            'model = "old"\n# chosen permissions\ndefault_permissions = "personal"\nother = true\n',
            id="comment-before-permission",
        ),
        pytest.param(
            'default_permissions = "personal"\nmodel = "old"\nother = true\n',
            id="permission-first",
        ),
    ],
)
def test_clear_edited_legacy_profile_restores_root_position(tmp_path: Path, original: str) -> None:
    """Clear an owned schema-1 profile without relocating the user's root choice."""
    home = tmp_path / "home"
    config = home / "config.toml"
    home.mkdir()
    legacy_root = original.replace('default_permissions = "personal"\n', "")
    legacy = (
        legacy_root
        + 'default_permissions = "github-read" # codex-rig:github-read\n'
        + "\n[features]\nnetwork_proxy = true # codex-rig:github-read\n"
        + "\n# codex-rig:github-read profile begin\n"
        + '[permissions.github-read]\nextends = ":workspace"\n\n'
        + '[permissions.github-read.filesystem.":workspace_roots"]\n".git" = "write"\n\n'
        + "[permissions.github-read.network]\nenabled = true\n\n"
        + "[permissions.github-read.network.domains]\n"
        + '"api.github.com" = "allow"\n"github.com" = "allow"\n'
        + "# codex-rig:github-read profile end\n"
    )
    config.write_text(legacy.replace('model = "old"', 'model = "new"'), encoding="utf-8", newline="\n")
    state = {
        "schema": 1,
        "original": original,
        "installed_sha256": hashlib.sha256(legacy.encode()).hexdigest(),
        "settings": {
            "default_permissions": ['default_permissions = "personal"\n'],
            "sandbox_mode": [],
            "network_proxy": [],
        },
    }
    body = json.dumps(state, sort_keys=True, ensure_ascii=False).encode()
    (home / "codex-rig-github-read-profile.json").write_bytes(
        b"# codex-rig:github-read-profile sha256=" + hashlib.sha256(body).hexdigest().encode() + b"\n" + body + b"\n"
    )

    result = _run(home, "--remove")

    assert result.returncode == 0, result.stderr
    assert config.read_text(encoding="utf-8") == original.replace('model = "old"', 'model = "new"')


@pytest.mark.parametrize("mode", ["setup", "clear"])
@pytest.mark.parametrize("trailing_table", [False, True])
def test_interrupted_legacy_migration_can_retry_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, trailing_table: bool
) -> None:
    """Recover setup or clear after a migrated state write precedes a failed config write."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    original = 'model = "gpt-6-sol"\n'
    legacy = (
        original
        + 'default_permissions = "github-read" # codex-rig:github-read\n'
        + "\n[features]\n"
        + "network_proxy = true # codex-rig:github-read\n"
        + "\n# codex-rig:github-read profile begin\n"
        + "[permissions.github-read]\n"
        + 'extends = ":workspace"\n\n'
        + '[permissions.github-read.filesystem.":workspace_roots"]\n'
        + '".git" = "write"\n\n'
        + "[permissions.github-read.network]\n"
        + "enabled = true\n\n"
        + "[permissions.github-read.network.domains]\n"
        + '"api.github.com" = "allow"\n'
        + '"github.com" = "allow"\n'
        + "# codex-rig:github-read profile end\n"
    )
    config = home / "config.toml"
    state = {
        "schema": 1,
        "original": original,
        "installed_sha256": hashlib.sha256(legacy.encode()).hexdigest(),
        "settings": {"default_permissions": [], "sandbox_mode": [], "network_proxy": []},
    }
    body = json.dumps(state, sort_keys=True, ensure_ascii=False).encode()
    state_path = home / "codex-rig-github-read-profile.json"
    state_path.write_bytes(
        b"# codex-rig:github-read-profile sha256=" + hashlib.sha256(body).hexdigest().encode() + b"\n" + body + b"\n"
    )
    extra = '\n[profiles.personal]\nmodel_reasoning_effort = "high"\n' if trailing_table else ""
    current_legacy = legacy + extra
    config.write_text(current_legacy, encoding="utf-8", newline="\n")

    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("install_github_read_rules", SCRIPT)
    assert spec is not None and spec.loader is not None
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    apply_change = installer._apply_change

    def fail_config_write(target: Path, payload: bytes | None, existing: bytes | None) -> str:
        """Fail only the config update after the migration state has been written."""
        if target == config:
            raise OSError("injected config write failure")
        return apply_change(target, payload, existing)

    with monkeypatch.context() as patch:
        patch.setattr(installer, "_apply_change", fail_config_write)
        with pytest.raises(OSError, match="injected config write failure"):
            list(installer.sync_github_read_profile(home, root))

    assert config.read_text(encoding="utf-8") == current_legacy
    migrated = json.loads(state_path.read_text(encoding="utf-8").split("\n", 1)[1])
    assert migrated["schema"] == 2
    assert migrated["migration_source_sha256"] == hashlib.sha256(current_legacy.encode()).hexdigest()

    edited = current_legacy.replace('model = "gpt-6-sol"', 'model = "gpt-6-luna"')
    config.write_text(edited, encoding="utf-8", newline="\n")
    rejected = _run(home, "--plugin-root", str(root)) if mode == "setup" else _run(home, "--remove")
    assert rejected.returncode != 0
    assert config.read_text(encoding="utf-8") == edited
    assert state_path.exists()
    config.write_text(current_legacy, encoding="utf-8", newline="\n")

    result = _run(home, "--plugin-root", str(root)) if mode == "setup" else _run(home, "--remove")

    assert result.returncode == 0, result.stderr
    if mode == "setup":
        assert "default_permissions" not in _config(home)
        assert _config(home)["permissions"]["github-read"]["extends"] == ":workspace"
        if trailing_table:
            assert _config(home)["profiles"]["personal"] == {"model_reasoning_effort": "high"}
    else:
        assert config.read_text(encoding="utf-8") == migrated["original"]
        assert _config(home)["model"] == "gpt-6-sol"
        if trailing_table:
            assert _config(home)["profiles"]["personal"] == {"model_reasoning_effort": "high"}
        assert not state_path.exists()


@pytest.mark.parametrize("mode", ["setup", "clear"])
def test_edited_owned_profile_blocks_lifecycle(tmp_path: Path, mode: str) -> None:
    """Preserve a changed managed profile for manual reconciliation."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    assert _run(home, "--plugin-root", str(root)).returncode == 0
    config = home / "config.toml"
    original = config.read_text(encoding="utf-8")
    edited = original.replace('"api.github.com" = "allow"', '"api.github.com" = "deny"')
    assert edited != original
    config.write_text(edited, encoding="utf-8", newline="\n")

    result = _run(home, "--plugin-root", str(root)) if mode == "setup" else _run(home, "--remove")

    assert result.returncode != 0
    assert config.read_text(encoding="utf-8") == edited


def test_clear_rejects_edited_owned_network_grant(tmp_path: Path) -> None:
    """Keep a locally edited managed domain grant for manual reconciliation."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    assert _run(home, "--plugin-root", str(root)).returncode == 0
    config = home / "config.toml"
    original = config.read_text(encoding="utf-8")
    edited = original.replace('"api.github.com" = "allow"', '"api.github.com" = "deny"')
    assert edited != original
    config.write_text(edited, encoding="utf-8", newline="\n")

    result = _run(home, "--remove")

    assert result.returncode != 0
    assert config.read_text(encoding="utf-8") == edited


def test_clear_preserves_later_root_sandbox_choice(tmp_path: Path) -> None:
    """Removal retains a user sandbox choice added after profile setup."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    assert _run(home, "--plugin-root", str(root)).returncode == 0
    config = home / "config.toml"
    config.write_text(
        'sandbox_mode = "workspace-write"\n' + config.read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
    )

    result = _run(home, "--remove")

    assert result.returncode == 0, result.stderr
    assert _config(home) == {"sandbox_mode": "workspace-write"}


def test_setup_rejects_domain_appended_after_profile_marker(tmp_path: Path) -> None:
    """A later TOML assignment must not silently broaden the managed domain table."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    assert _run(home, "--plugin-root", str(root)).returncode == 0
    config = home / "config.toml"
    config.write_text(
        config.read_text(encoding="utf-8") + '"outside.example" = "allow"\n', encoding="utf-8", newline="\n"
    )

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode != 0
    assert "profile block was extended" in result.stderr


def test_clear_preserves_later_unrelated_toml_table(tmp_path: Path) -> None:
    """A new table after setup cannot keep the owned profile stuck on clear."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    assert _run(home, "--plugin-root", str(root)).returncode == 0
    config = home / "config.toml"
    config.write_text(
        config.read_text(encoding="utf-8") + '[mcp_servers.example]\nurl = "https://example.com"\n',
        encoding="utf-8",
        newline="\n",
    )

    assert _run(home, "--plugin-root", str(root)).returncode == 0
    result = _run(home, "--remove")

    assert result.returncode == 0, result.stderr
    assert _config(home)["mcp_servers"]["example"]["url"] == "https://example.com"
    assert "default_permissions" not in _config(home)


def test_setup_rejects_state_larger_than_later_reader_limit(tmp_path: Path) -> None:
    """Setup must not create a state file that prevents its own repeat and clear."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    config = home / "config.toml"
    original = ("# long user comment\n" * 200_000).encode()
    assert len(original) < 4 * 1024 * 1024
    config.write_bytes(original)

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode != 0
    assert "size limit" in result.stderr
    assert config.read_bytes() == original
    assert not (home / "codex-rig-github-read-profile.json").exists()


@pytest.mark.parametrize(
    "key",
    [pytest.param('"github-read"', id="quoted"), pytest.param('"\\u0067ithub-read"', id="escaped")],
)
def test_setup_rejects_profile_defined_with_permission_assignment(tmp_path: Path, key: str) -> None:
    """A profile value in the parent permissions table cannot be duplicated."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    config = home / "config.toml"
    original = f'[permissions]\n{key} = {{ extends = ":workspace" }}\n'.encode()
    tomllib.loads(original.decode())
    config.write_bytes(original)

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode != 0
    assert config.read_bytes() == original
    assert not (home / "codex-rig-github-read-profile.json").exists()


def test_setup_reuses_features_table_with_comment(tmp_path: Path) -> None:
    """Preserve a valid annotated table header without creating duplicate TOML tables."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    config = home / "config.toml"
    config.write_text("[features] # user note\nfoo = true\n", encoding="utf-8")

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode == 0, result.stderr
    assert _config(home)["features"] == {"foo": True, "network_proxy": True}
    assert config.read_text(encoding="utf-8").count("[features]") == 1


def test_setup_accepts_config_without_final_newline(tmp_path: Path) -> None:
    """Adding profile tables must not join them to an existing last value."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    config = home / "config.toml"
    config.write_text('model = "gpt-6-sol"', encoding="utf-8")

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode == 0, result.stderr
    assert _config(home)["model"] == "gpt-6-sol"
    assert "default_permissions" not in _config(home)
    assert _run(home, "--remove").returncode == 0
    assert config.read_text(encoding="utf-8") == 'model = "gpt-6-sol"'


def test_setup_rejects_inline_features_before_writing(tmp_path: Path) -> None:
    """Unsupported valid TOML forms must fail before creating duplicate feature tables."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    config = home / "config.toml"
    original = "features = { network_proxy = false, multi_agent = true }\n"
    config.write_text(original, encoding="utf-8")

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode != 0
    assert "manual migration" in result.stderr
    assert config.read_text(encoding="utf-8") == original
    assert not (home / "codex-rig-github-read-profile.json").exists()


def test_setup_rejects_existing_user_profile_collision(tmp_path: Path) -> None:
    """Never overwrite a profile with the name reserved for GitHub evidence."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    first = _run(home, "--plugin-root", str(root))
    assert first.returncode == 0, first.stderr
    managed_name = "github-read"
    assert _run(home, "--remove").returncode == 0
    config = home / "config.toml"
    original = f'[permissions."{managed_name}"]\nextends = ":workspace"\n'.encode()
    config.write_bytes(original)

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode != 0
    assert config.read_bytes() == original


@pytest.mark.parametrize(
    "header",
    [
        pytest.param("[permissions.'github-read']", id="single-quoted-profile"),
        pytest.param("[ permissions . 'github-read' ]", id="spaced-profile"),
        pytest.param('["permissions"."github-read"]', id="quoted-parent"),
        pytest.param("[permissions.'github-read'.network]", id="nested-profile"),
    ],
)
def test_setup_rejects_equivalent_user_profile_headers_before_writing(tmp_path: Path, header: str) -> None:
    """Recognize valid TOML spellings of an existing reserved profile."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    config = home / "config.toml"
    original = f'{header}\nextends = ":workspace"\n'.encode()
    tomllib.loads(original.decode())
    config.write_bytes(original)

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode != 0
    assert config.read_bytes() == original
    assert not (home / "codex-rig-github-read-profile.json").exists()


def test_setup_removes_validated_old_managed_rules(tmp_path: Path) -> None:
    """Migrate a verified old reader grant while keeping unrelated rule bytes."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    rules = home / "rules"
    rules.mkdir()
    unrelated = rules / "personal.rules"
    original = b'prefix_rule(pattern=["git", "status"], decision="allow")\n'
    unrelated.write_bytes(original)
    old = rules / "codex-rig-github-read.rules"
    reader = root / "shared" / "github_read.py"
    body = f'prefix_rule(pattern={json.dumps([["python", "python3"], str(reader)])}, decision="allow")\n'.encode()
    old.write_bytes(b"# codex-rig:github-read sha256=" + hashlib.sha256(body).hexdigest().encode() + b"\n" + body)
    pr = rules / "codex-rig-pr-collection.rules"
    collector = root / "shared" / "collect_pr.py"
    target = "https://github.com/example/project/pull/7"
    pr_body = (
        f"prefix_rule(pattern={json.dumps([['python', 'python3'], str(collector), '--target', [target]])}, "
        'decision="allow")\n'
    ).encode()
    pr.write_bytes(
        b"# codex-rig:pr-collection sha256=" + hashlib.sha256(pr_body).hexdigest().encode() + b"\n" + pr_body
    )

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode == 0, result.stderr
    assert not old.exists()
    assert not pr.exists()
    assert unrelated.read_bytes() == original
    assert "default_permissions" not in _config(home)


def test_setup_preserves_explicit_collector_deny_rule(tmp_path: Path) -> None:
    """An existing deny rule must not block migration or be removed."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    rules = home / "rules"
    rules.mkdir()
    default = rules / "default.rules"
    collector = root / "shared" / "collect_pr.py"
    deny = f'prefix_rule(pattern=["python", "{collector}"], decision="deny")\n'.encode()
    default.write_bytes(deny)

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode == 0, result.stderr
    assert default.read_bytes() == deny
    assert "github-read" in _config(home)["permissions"]


@pytest.mark.parametrize("phase", ["setup", "clear"])
def test_foreign_collector_allow_rule_survives_profile_lifecycle(tmp_path: Path, phase: str) -> None:
    """Leave another collector's allow rule untouched during setup and clear."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    rules = home / "rules"
    rules.mkdir()
    default = rules / "default.rules"
    foreign_collector = tmp_path / "other" / "collect_pr.py"
    target = "https://github.com/example/project/pull/7"
    grant = (
        f"prefix_rule(pattern={json.dumps([['python', 'python3'], str(foreign_collector), '--target', [target]])}, "
        'decision="allow")\n'
    ).encode()
    if phase == "clear":
        assert _run(home, "--plugin-root", str(root)).returncode == 0
    default.write_bytes(grant)

    result = _run(home, "--plugin-root", str(root)) if phase == "setup" else _run(home, "--remove")

    assert result.returncode == 0, result.stderr
    assert default.read_bytes() == grant
    if phase == "setup":
        assert "github-read" in _config(home)["permissions"]
    else:
        assert not (home / "config.toml").exists()


def test_mixed_foreign_and_local_collector_grant_blocks_setup(tmp_path: Path) -> None:
    """A foreign path must not conceal a noncanonical grant to this home's collector."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    rules = home / "rules"
    rules.mkdir()
    default = rules / "default.rules"
    foreign_collector = tmp_path / "other" / "collect_pr.py"
    local_collector = root / "shared" / "collect_pr.py"
    target = "https://github.com/example/project/pull/7"
    grant = (
        f"prefix_rule(pattern={json.dumps([['python', 'python3'], [str(foreign_collector), str(local_collector)], '--target', [target]])}, "
        'decision="allow")\n'
    ).encode()
    default.write_bytes(grant)

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode != 0
    assert default.read_bytes() == grant
    assert not (home / "config.toml").exists()


def test_setup_removes_canonical_collector_grant_from_default_rules(tmp_path: Path) -> None:
    """Retire a verified UI-saved PR grant when moving to session opt-in."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    rules = home / "rules"
    rules.mkdir()
    default = rules / "default.rules"
    collector = root / "shared" / "collect_pr.py"
    target = "https://github.com/example/project/pull/7"
    grant = (
        f"prefix_rule(pattern={json.dumps([['python', 'python3'], str(collector), '--target', [target]])}, "
        'decision="allow")\n'
    ).encode()
    unrelated = b'prefix_rule(pattern=["git", "status"], decision="allow")\n'
    default.write_bytes(grant + unrelated)

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode == 0, result.stderr
    assert default.read_bytes() == unrelated


def test_setup_refuses_uncertain_collector_grant_before_write(tmp_path: Path) -> None:
    """An unrecognized collector rule must not survive a seemingly successful migration."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    rules = home / "rules"
    rules.mkdir()
    default = rules / "default.rules"
    collector = root / "shared" / "collect_pr.py"
    grant = (
        f"prefix_rule(pattern={json.dumps([['python', 'python3'], str(collector), '--target', ['*']])}, "
        'decision="allow")\n'
    ).encode()
    default.write_bytes(grant)

    result = _run(home, "--plugin-root", str(root))

    assert result.returncode != 0
    assert default.read_bytes() == grant
    assert not (home / "config.toml").exists()
    assert not (home / "codex-rig-github-read-profile.json").exists()


@pytest.mark.parametrize("mode", ["setup", "clear"])
def test_edited_old_managed_rule_blocks_profile_change(tmp_path: Path, mode: str) -> None:
    """Do not discard a suspicious old permission file during migration or clear."""
    home = tmp_path / "home"
    root = _installed_plugin(home)
    managed = home / "rules" / "codex-rig-github-read.rules"
    managed.parent.mkdir()
    original = b"# codex-rig:github-read sha256=invalid\n"
    managed.write_bytes(original)
    if mode == "clear":
        assert _run(home, "--plugin-root", str(root)).returncode != 0
        arguments = ("--remove",)
    else:
        arguments = ("--plugin-root", str(root))

    result = _run(home, *arguments)

    assert result.returncode != 0
    assert managed.read_bytes() == original
    assert not (home / "config.toml").exists()
