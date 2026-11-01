"""Exercise the GitHub evidence profile lifecycle against isolated Codex homes."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from types import ModuleType
from typing import NamedTuple

import pytest
from _github_read_helpers import allow_rule, installed_plugin
from _modules import load_module

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10/3.11 compatibility
    import tomli as tomllib


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "install_github_read_rules.py"
PR_URL = "https://github.com/example/project/pull/7"
STATE_FILE = "codex-rig-github-read-profile.json"
PROFILE_END = "# codex-rig:github-read profile end\n"
USER_RULE_BYTES = b'prefix_rule(pattern=["git", "status"], decision="allow")\n'
WORKSPACE_DEFAULT_LINE = 'default_permissions = ":workspace" # codex-rig:github-read\n'
USER_PROFILE = '\n[permissions.personal]\nextends = ":read-only"\n'
PERSONAL_PROFILE = '\n[profiles.personal]\nmodel_reasoning_effort = "high"\n'
HOOK_TABLES = '\n[hooks.state]\n\n[hooks.state."example:session_start"]\ntrusted_hash = "sha256:example"\n'
PROFILE_BLOCK = (
    '[permissions.github-read]\nextends = ":workspace"\n\n'
    '[permissions.github-read.filesystem.":workspace_roots"]\n".git" = "write"\n\n'
    "[permissions.github-read.network]\nenabled = true\n\n"
    "[permissions.github-read.network.domains]\n"
    '"api.github.com" = "allow"\n"github.com" = "allow"\n'
)
# Config tail written by the historical schema-2 renderer, which selected no default profile.
SCHEMA2_SUFFIX = (
    "\n[features]\nnetwork_proxy = true # codex-rig:github-read\n\n"
    f"# codex-rig:github-read profile begin\n{PROFILE_BLOCK}{PROFILE_END}"
)
LEGACY_DEFAULT_LINE = 'default_permissions = "github-read" # codex-rig:github-read\n'

_CODEX = shutil.which("codex")
_requires_codex = pytest.mark.skipif(_CODEX is None, reason="Codex executable is unavailable")


class InterruptedWrite(NamedTuple):
    """Paths and state bytes left behind by an install whose config write failed."""

    config: Path
    state_path: Path
    state_bytes: bytes


@dataclass(frozen=True)
class InterruptedMigration:
    """A legacy profile whose migration state landed but whose config rewrite failed."""

    home: Path
    root: Path
    config: Path
    state_path: Path
    legacy: str
    migrated: dict[str, object]


def _run(home: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the public setup or clear CLI without touching the real Codex home."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments, "--codex-home", str(home)],
        capture_output=True,
        text=True,
        check=False,
    )


def _setup(home: Path, root: Path) -> subprocess.CompletedProcess[str]:
    """Run setup from the installed package."""
    return _run(home, "--plugin-root", str(root))


def _clear(home: Path) -> subprocess.CompletedProcess[str]:
    """Run removal of the managed profile."""
    return _run(home, "--remove")


def _run_mode(home: Path, root: Path, mode: str) -> subprocess.CompletedProcess[str]:
    """Run the CLI verb named by a lifecycle mode (setup or clear)."""
    return _run(home, *{"setup": ("--plugin-root", str(root)), "clear": ("--remove",)}[mode])


def _config(home: Path) -> dict[str, object]:
    """Parse installed configuration through the standard TOML reader."""
    return tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))


def _write_text(path: Path, text: str) -> None:
    """Write UTF-8 text with LF endings regardless of host."""
    path.write_text(text, encoding="utf-8", newline="\n")


def _read_state(home: Path) -> dict[str, object]:
    """Parse the authenticated profile state body, skipping its checksum header line."""
    return json.loads((home / STATE_FILE).read_bytes().split(b"\n", 1)[1])


def _state_dict(
    schema: int, original: str | None, installed: str, default_permissions: tuple[str, ...] = ()
) -> dict[str, object]:
    """Build a profile state record whose installed digest matches the given config text."""
    return {
        "schema": schema,
        "original": original,
        "installed_sha256": hashlib.sha256(installed.encode()).hexdigest(),
        "settings": {"default_permissions": list(default_permissions), "sandbox_mode": [], "network_proxy": []},
    }


def _write_state(home: Path, state: dict[str, object]) -> bytes:
    """Write a state record with its valid checksum header and return the bytes."""
    body = json.dumps(state, sort_keys=True, ensure_ascii=False).encode()
    payload = (
        b"# codex-rig:github-read-profile sha256=" + hashlib.sha256(body).hexdigest().encode() + b"\n" + body + b"\n"
    )
    (home / STATE_FILE).write_bytes(payload)
    return payload


def _retag_state(home: Path, schema: int, installed: str, default_permissions: tuple[str, ...] = ()) -> None:
    """Rewrite the state as an older schema bound to the given installed config text."""
    state = _read_state(home)
    state["schema"] = schema
    state["installed_sha256"] = hashlib.sha256(installed.encode()).hexdigest()
    state["settings"]["default_permissions"] = list(default_permissions)
    _write_state(home, state)


def _reader_grant(reader: Path | PureWindowsPath) -> bytes:
    """Render the legacy canonical reader grant for a helper path."""
    return allow_rule([["python", "python3"], str(reader)])


def _collector_grant(collector: Path | PureWindowsPath) -> bytes:
    """Render the canonical PR collector grant for a helper path."""
    return allow_rule([["python", "python3"], str(collector), "--target", [PR_URL]])


def _owned_rule(kind: str, body: bytes) -> bytes:
    """Prefix a rule body with the installer ownership marker and its checksum."""
    return f"# codex-rig:{kind} sha256=".encode() + hashlib.sha256(body).hexdigest().encode() + b"\n" + body


def _legacy_automatic_config(root_text: str) -> str:
    """Return the schema-1 config where github-read was an automatic default, after the user's root settings."""
    return root_text + LEGACY_DEFAULT_LINE + SCHEMA2_SUFFIX


def _write_schema2_profile(home: Path, *, original: str | None = None, edit: bool = False) -> str:
    """Reproduce the historical default-free renderer and its authenticated state."""
    installed = (original or "") + SCHEMA2_SUFFIX
    _write_state(home, _state_dict(2, original, installed))
    config_text = installed + (PERSONAL_PROFILE.lstrip("\n") if edit else "")
    _write_text(home / "config.toml", config_text)
    return config_text


def _write_schema3_profile(home: Path, *, original: str | None, automatic_default: str) -> str:
    """Reproduce the schema-3 profile state: the schema-2 body plus an optional automatic workspace default."""
    installed = automatic_default + _write_schema2_profile(home, original=original)
    _retag_state(home, 3, installed, (original,) if original else ())
    return installed


def _load_installer(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Import the installer script as a module with its sibling imports resolvable."""
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    return load_module("install_github_read_rules", SCRIPT)


def _fail_config_write(monkeypatch: pytest.MonkeyPatch, home: Path, root: Path) -> None:
    """Run the real installer in-process but fail its config write right after the state write lands."""
    installer = _load_installer(monkeypatch)
    apply_change = installer._apply_change
    config = home / "config.toml"

    def fail_config_write(target: Path, payload: bytes | None, existing: bytes | None) -> str:
        """Fail only the config update; every other write goes through the real installer."""
        if target == config:
            raise OSError("injected config write failure")
        return apply_change(target, payload, existing)

    monkeypatch.setattr(installer, "_apply_change", fail_config_write)
    with pytest.raises(OSError, match="injected config write failure"):
        list(installer.sync_github_read_profile(home, root))


def _interrupted_first_write(home: Path, root: Path, monkeypatch: pytest.MonkeyPatch) -> InterruptedWrite:
    """Run an install that fails on its config write after the state write lands."""
    _fail_config_write(monkeypatch, home, root)
    state_path = home / STATE_FILE
    return InterruptedWrite(home / "config.toml", state_path, state_path.read_bytes())


def _no_interruption(home: Path, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Leave the prepared profile as it is, so setup runs uninterrupted."""


def _codex_features(home: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Load the Codex configuration in an isolated home by listing features."""
    return subprocess.run(
        [_CODEX, "features", "list"],
        cwd=cwd,
        env={**os.environ, "CODEX_HOME": str(home)},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def config(home: Path) -> Path:
    """Path of the Codex config file (not yet created)."""
    return home / "config.toml"


@pytest.fixture
def state_path(home: Path) -> Path:
    """Path of the installer's authenticated profile state file."""
    return home / STATE_FILE


@pytest.fixture
def user_rule(home: Path) -> Path:
    """User-owned execpolicy rule file that no lifecycle step may modify."""
    path = home / "rules" / "default.rules"
    path.parent.mkdir(parents=True)
    path.write_bytes(USER_RULE_BYTES)
    return path


def _append_user_profile(home: Path) -> None:
    """Append a user permission profile to the installed config."""
    config = home / "config.toml"
    _write_text(config, config.read_text(encoding="utf-8") + USER_PROFILE)


def _origin_fresh(home: Path, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Install on a blank home, then add a user profile."""
    assert _setup(home, root).returncode == 0
    _append_user_profile(home)


def _origin_schema1(home: Path, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Start from the historical schema-1 automatic-default profile beside a user profile."""
    installed = LEGACY_DEFAULT_LINE + _write_schema2_profile(home)
    _retag_state(home, 1, installed)
    _write_text(home / "config.toml", installed + USER_PROFILE)


def _origin_schema2(home: Path, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Start from the historical schema-2 default-free profile beside a user profile."""
    _write_text(home / "config.toml", _write_schema2_profile(home) + USER_PROFILE)


def _origin_interrupted(home: Path, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Start from a schema-2 profile whose upgrade write was interrupted, beside a user profile."""
    _write_text(home / "config.toml", _write_schema2_profile(home) + USER_PROFILE)
    _interrupted_first_write(home, root, monkeypatch)


def _origin_workspace(home: Path, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Install over a legacy workspace sandbox mode, then add a user profile."""
    _write_text(home / "config.toml", 'sandbox_mode = "workspace-write" # retained choice\n')
    assert _setup(home, root).returncode == 0
    _append_user_profile(home)


def _origin_existing_profiles(home: Path, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Install beside a profile the user already had."""
    _write_text(home / "config.toml", USER_PROFILE)
    assert _setup(home, root).returncode == 0


def _origin_explicit(home: Path, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Install beside an explicit read-only default, then add a user profile."""
    _write_text(home / "config.toml", 'default_permissions = ":read-only"\n')
    assert _setup(home, root).returncode == 0
    _append_user_profile(home)


def _install_schema3_hooked(home: Path, root: Path) -> str:
    """Write the schema-3 profile (automatic workspace default) and return its config text."""
    return _write_schema3_profile(home, original=None, automatic_default=WORKSPACE_DEFAULT_LINE)


def _install_schema4(home: Path, root: Path) -> str:
    """Install the current profile and return the written config text."""
    assert _setup(home, root).returncode == 0
    return (home / "config.toml").read_text(encoding="utf-8")


@pytest.fixture
def interrupted_migration(
    home: Path, plugin_root: Path, config: Path, state_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[[bool], InterruptedMigration]:
    """Build a legacy profile whose migration state was written but whose config rewrite failed."""

    def build(trailing_table: bool) -> InterruptedMigration:
        """Create the interrupted state, optionally with a user table after the managed block."""
        legacy = _legacy_automatic_config('model = "gpt-6-sol"\n')
        _write_state(home, _state_dict(1, 'model = "gpt-6-sol"\n', legacy))
        current = legacy + (PERSONAL_PROFILE if trailing_table else "")
        _write_text(config, current)
        _fail_config_write(monkeypatch, home, plugin_root)
        migrated = json.loads(state_path.read_text(encoding="utf-8").split("\n", 1)[1])
        return InterruptedMigration(home, plugin_root, config, state_path, current, migrated)

    return build


class TestFreshHome:
    """Setup and clear on a Codex home that has no configuration."""

    def test_supplies_local_workflow_without_network_or_execpolicy(
        self, home: Path, plugin_root: Path, config: Path
    ) -> None:
        """Setup allows routine workspace Git writes without granting network or unsandboxed commands.

        A home with no config receives the local-workflow default profile with network off; repeating setup is a byte-
        for-byte no-op and clear removes the file again. No execpolicy rules directory is ever created.
        """
        result = _setup(home, plugin_root)
        assert result.returncode == 0, result.stderr
        installed = _config(home)
        assert installed["default_permissions"] == "local-workflow"
        assert installed["permissions"]["local-workflow"] == {
            "extends": ":workspace",
            "filesystem": {":workspace_roots": {".git": "write"}},
            "network": {"enabled": False},
        }
        assert not (home / "rules").exists()
        before = config.read_bytes()

        assert _setup(home, plugin_root).returncode == 0
        assert config.read_bytes() == before
        assert _clear(home).returncode == 0
        assert not config.exists()

    def test_setup_and_clear_leave_no_state_without_original_config(
        self, home: Path, plugin_root: Path, config: Path
    ) -> None:
        """Clear removes the created config entirely when the Codex home had no configuration to restore.

        Setup on a blank home enables the network proxy and the GitHub domains profile; clear must leave no config.
        """
        assert _setup(home, plugin_root).returncode == 0
        installed = _config(home)
        assert installed["default_permissions"] == "local-workflow"
        assert installed["features"]["network_proxy"] is True
        assert installed["permissions"]["github-read"]["network"]["domains"] == {
            "api.github.com": "allow",
            "github.com": "allow",
        }

        assert _clear(home).returncode == 0
        assert not config.exists()


class TestConfigPreservation:
    """Setup and clear keep every user setting and refuse configs they cannot adopt unchanged."""

    @pytest.mark.parametrize("line_ending", ["\n", "\r\n"])
    def test_repeat_and_clear_preserve_unrelated_config(
        self, home: Path, plugin_root: Path, config: Path, line_ending: str
    ) -> None:
        """Setup owns only the GitHub profile; clear restores prior root settings byte for byte.

        The user's config has a comment, a personal default profile, feature flags, and a profile table, in LF or CRLF
        endings. Setup flips only the proxy flag and adds profile tables; repeat is a no-op; clear restores all.
        """
        original = (
            "# preserve this user comment and ordering exactly\n"
            'model = "gpt-6-sol"\n'
            'default_permissions = "personal"\n'
            "\n[features]\n"
            "multi_agent = true\n"
            "network_proxy = false\n"
            "\n[profiles.personal]\n"
            'model_reasoning_effort = "high"\n'
        ).replace("\n", line_ending)
        config.write_text(original, encoding="utf-8", newline="\n")

        result = _setup(home, plugin_root)
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

        assert _setup(home, plugin_root).returncode == 0
        assert config.read_bytes() == first
        assert _clear(home).returncode == 0
        assert config.read_bytes() == original.encode("utf-8")
        assert _config(home) == tomllib.loads(original)

    @pytest.mark.parametrize(
        ("original", "message"),
        [
            pytest.param(
                b'default_permissions = "personal"\n\n[features]\nnetwork_proxy = false\n'
                b"\n[permissions.personal.network]\nenabled = true\n",
                "network-enabled permission profile",
                id="proxy-change-for-default-network-profile",
            ),
            pytest.param(
                b"[features]\nnetwork_proxy = false\n\n[permissions.research.network]\nenabled = true\n",
                "network-enabled permission profile",
                id="proxy-change-for-nondefault-network-profile",
            ),
            pytest.param(
                b'sandbox_mode = "workspace-write"\n[sandbox_workspace_write]\nnetwork_access = true\n',
                "legacy sandbox_mode overrides permission profiles",
                id="legacy-workspace-table-with-root-mode",
            ),
            pytest.param(
                b"[sandbox_workspace_write]\nnetwork_access = true\n",
                "legacy sandbox_workspace_write conflicts",
                id="legacy-workspace-table-without-root-mode",
            ),
            pytest.param(
                b'sandbox_mode = "read-only"\n',
                "legacy sandbox_mode overrides permission profiles",
                id="legacy-read-only-sandbox-mode",
            ),
            pytest.param(
                b'sandbox_mode = "danger-full-access"\n',
                "legacy sandbox_mode overrides permission profiles",
                id="legacy-danger-full-access-sandbox-mode",
            ),
            pytest.param(
                b'default_permissions = "personal"\nsandbox_mode = "workspace-write"\n',
                "legacy sandbox_mode overrides permission profiles",
                id="ambiguous-legacy-sandbox-override",
            ),
            pytest.param(
                b'[profiles.personal]\nsandbox_mode = "workspace-write"\n',
                "profile sandbox_mode overrides default_permissions",
                id="selectable-profile-sandbox-override",
            ),
            pytest.param(
                b"features = { network_proxy = false, multi_agent = true }\n",
                "manual migration",
                id="inline-features-table",
            ),
        ],
    )
    def test_setup_rejects_conflicting_config_before_write(
        self, home: Path, plugin_root: Path, config: Path, user_rule: Path, original: bytes, message: str
    ) -> None:
        """Setup refuses with a named reason and leaves config, user rules, and state untouched.

        Each starting config holds a setting setup cannot adopt without silently changing meaning: a proxy change that
        would restrict a network-enabled profile (selected now or later), a legacy sandbox mode or workspace table
        needing explicit migration, a profile sandbox mode that would override a later opt-in selection, or valid TOML
        (an inline features table) that would otherwise gain a duplicate table. A user rule file must survive, and no
        profile state, managed rule file, or backup may appear.
        """
        assert tomllib.loads(original.decode())
        config.write_bytes(original)

        result = _setup(home, plugin_root)

        assert result.returncode != 0
        assert message in result.stderr
        assert config.read_bytes() == original
        assert user_rule.read_bytes() == USER_RULE_BYTES
        assert not (home / STATE_FILE).exists()
        assert not (home / "rules" / "codex-rig-github-read.rules").exists()
        assert not (home / "backups" / "codex-rig").exists()

    @pytest.mark.parametrize(
        ("initial_proxy", "network_enabled"),
        [
            pytest.param(True, True, id="proxy-already-enabled"),
            pytest.param(False, False, id="existing-profile-network-disabled"),
        ],
    )
    def test_setup_accepts_safe_existing_permission_profile(
        self, home: Path, plugin_root: Path, config: Path, initial_proxy: bool, network_enabled: bool
    ) -> None:
        """Setup stays available when it cannot newly restrict another profile's network.

        Either the proxy is already on, or the user's profile has network off, so enabling the proxy changes nothing for
        them; clear then restores the file with only the generated root default prepended.
        """
        original = (
            f"[features]\nnetwork_proxy = {str(initial_proxy).lower()}\n"
            f"\n[permissions.personal.network]\nenabled = {str(network_enabled).lower()}\n"
        )
        _write_text(config, original)

        result = _setup(home, plugin_root)

        assert result.returncode == 0, result.stderr
        installed = _config(home)
        assert installed["features"]["network_proxy"] is True
        assert installed["permissions"]["personal"]["network"] == {"enabled": network_enabled}
        assert _clear(home).returncode == 0
        assert config.read_bytes() == b'default_permissions = ":workspace"\n' + original.encode()

    @pytest.mark.parametrize(
        "selected", [pytest.param('"github-read"', id="literal"), pytest.param('"\\u0067ithub-read"', id="escaped")]
    )
    def test_setup_rejects_preexisting_global_github_read_default(
        self, home: Path, plugin_root: Path, config: Path, selected: str
    ) -> None:
        """An existing root selection of github-read is refused so the profile never becomes global.

        The selection arrives as a literal or a unicode-escaped string; both must be recognized. Setup and the following
        clear leave the config byte-identical and create no state.
        """
        original = f'default_permissions = {selected}\nmodel = "gpt-6-sol"\n'
        _write_text(config, original)

        result = _setup(home, plugin_root)

        assert result.returncode != 0
        assert "github-read is already the global default" in result.stderr
        assert config.read_bytes() == original.encode()
        assert not (home / STATE_FILE).exists()
        assert _clear(home).returncode == 0
        assert config.read_bytes() == original.encode()

    def test_setup_preserves_multiline_user_instructions(self, home: Path, plugin_root: Path, config: Path) -> None:
        """Table-looking text inside a multi-line string survives setup, re-entry, and removal.

        The instructions contain a `default_permissions = ...` line that a line-based editor could mistake for a real
        root setting.
        """
        original = 'developer_instructions = """\nKeep this example:\ndefault_permissions = "private"\n"""\n'
        _write_text(config, original)

        result = _setup(home, plugin_root)

        assert result.returncode == 0, result.stderr
        assert _config(home)["developer_instructions"] == tomllib.loads(original)["developer_instructions"]
        installed = config.read_bytes()
        assert original.encode() in installed
        assert _setup(home, plugin_root).returncode == 0
        assert config.read_bytes() == installed
        assert _clear(home).returncode == 0
        assert config.read_bytes() == original.encode("utf-8")

    def test_setup_rejects_multiline_default_permissions_before_write(
        self, home: Path, plugin_root: Path, config: Path
    ) -> None:
        """A valid multi-line `default_permissions` value is refused rather than silently rewritten."""
        original = 'default_permissions = """\n[features]\nnetwork_proxy = true\n"""\n'
        assert tomllib.loads(original)["default_permissions"] == "[features]\nnetwork_proxy = true\n"
        _write_text(config, original)

        result = _setup(home, plugin_root)

        assert result.returncode != 0
        assert config.read_bytes() == original.encode("utf-8")

    @pytest.mark.parametrize(
        "original",
        [
            pytest.param('"permissions"."github-read" = { extends = ":workspace" }\n', id="quoted-dotted-profile"),
            pytest.param('"features".network_proxy = false\n', id="quoted-dotted-features"),
            pytest.param('["\\u0066eatures"]\nnetwork_proxy = false\n', id="escaped-features-header"),
        ],
    )
    def test_setup_rejects_unsupported_equivalent_profile_keys_before_write(
        self, home: Path, plugin_root: Path, config: Path, original: str
    ) -> None:
        """Valid quoted or escaped key spellings are refused instead of becoming duplicate managed TOML tables.

        Setup fails without touching the file, and a following clear leaves the user's parsed config unchanged.
        """
        assert tomllib.loads(original)
        _write_text(config, original)

        result = _setup(home, plugin_root)

        assert result.returncode != 0
        assert config.read_bytes() == original.encode("utf-8")
        assert _clear(home).returncode == 0
        assert _config(home) == tomllib.loads(original)

    @pytest.mark.parametrize(
        "original",
        [
            pytest.param('[permissions]\n"github-read" = { extends = ":workspace" }\n', id="assignment-quoted"),
            pytest.param('[permissions]\n"\\u0067ithub-read" = { extends = ":workspace" }\n', id="assignment-escaped"),
            pytest.param("[permissions.'github-read']\nextends = \":workspace\"\n", id="single-quoted-profile"),
            pytest.param("[ permissions . 'github-read' ]\nextends = \":workspace\"\n", id="spaced-profile"),
            pytest.param('["permissions"."github-read"]\nextends = ":workspace"\n', id="quoted-parent"),
            pytest.param("[permissions.'github-read'.network]\nextends = \":workspace\"\n", id="nested-profile"),
        ],
    )
    def test_setup_rejects_existing_reserved_profile_spellings_before_writing(
        self, home: Path, plugin_root: Path, config: Path, original: str
    ) -> None:
        """Every valid TOML spelling of an existing reserved profile is recognized and never duplicated.

        The reserved profile may arrive as a value assigned in the parent permissions table (quoted or with a unicode
        escape) or as a table header written with single quotes, spacing, a quoted parent, or a nested network table.
        """
        tomllib.loads(original)
        config.write_bytes(original.encode())

        result = _setup(home, plugin_root)

        assert result.returncode != 0
        assert config.read_bytes() == original.encode()
        assert not (home / STATE_FILE).exists()

    @pytest.mark.parametrize("managed_name", ["github-read", "local-workflow"])
    def test_setup_rejects_existing_user_profile_collision(
        self, home: Path, plugin_root: Path, config: Path, managed_name: str
    ) -> None:
        """A user profile carrying a name reserved by the installer is never overwritten.

        The profile appears after a clean setup and clear, so only the name collision can cause the refusal.
        """
        assert _setup(home, plugin_root).returncode == 0
        assert _clear(home).returncode == 0
        original = f'[permissions."{managed_name}"]\nextends = ":workspace"\n'.encode()
        config.write_bytes(original)

        result = _setup(home, plugin_root)

        assert result.returncode != 0
        assert config.read_bytes() == original

    def test_setup_reuses_features_table_with_comment(self, home: Path, plugin_root: Path, config: Path) -> None:
        """An annotated `[features]` header is reused so no duplicate TOML table appears."""
        config.write_text("[features] # user note\nfoo = true\n", encoding="utf-8")

        result = _setup(home, plugin_root)

        assert result.returncode == 0, result.stderr
        assert _config(home)["features"] == {"foo": True, "network_proxy": True}
        assert config.read_text(encoding="utf-8").count("[features]") == 1

    def test_setup_accepts_config_without_final_newline(self, home: Path, plugin_root: Path, config: Path) -> None:
        """Profile tables appended to a config lacking a final newline are not joined onto its last value."""
        config.write_text('model = "gpt-6-sol"', encoding="utf-8")

        result = _setup(home, plugin_root)

        assert result.returncode == 0, result.stderr
        assert _config(home)["model"] == "gpt-6-sol"
        assert _config(home)["default_permissions"] == "local-workflow"
        assert _clear(home).returncode == 0
        assert config.read_text(encoding="utf-8") == 'model = "gpt-6-sol"'

    def test_setup_rejects_state_larger_than_later_reader_limit(
        self, home: Path, plugin_root: Path, config: Path
    ) -> None:
        """Setup refuses to create a state file that the installer's own repeat and clear could not read back.

        The config is just under the 4 MiB file limit, but the state embeds it again with extra framing.
        """
        original = ("# long user comment\n" * 200_000).encode()
        assert len(original) < 4 * 1024 * 1024
        config.write_bytes(original)

        result = _setup(home, plugin_root)

        assert result.returncode != 0
        assert "size limit" in result.stderr
        assert config.read_bytes() == original
        assert not (home / STATE_FILE).exists()

    def test_comment_named_features_is_not_an_original_table(self, home: Path, plugin_root: Path, config: Path) -> None:
        """A commented-out `[features]` line does not count as an original table, so clear removes the created one."""
        original = '# [features]\nmodel = "gpt-6-sol"\n'
        _write_text(config, original)
        assert _setup(home, plugin_root).returncode == 0
        installed = config.read_text(encoding="utf-8")
        assert tomllib.loads(installed)["features"]["network_proxy"] is True
        _write_text(config, installed.replace('model = "gpt-6-sol"', 'model = "gpt-6-luna"'))

        clear = _clear(home)

        assert clear.returncode == 0, clear.stderr
        assert config.read_text(encoding="utf-8") == original.replace("gpt-6-sol", "gpt-6-luna")

    def test_root_permission_comment_and_order_survive_setup_and_edited_clear(
        self, home: Path, plugin_root: Path, config: Path
    ) -> None:
        """The user's root permission setting keeps its neighbouring comment and position through setup and clear."""
        original = 'model = "gpt-6-sol"\n# chosen permissions\ndefault_permissions = "personal"\nother = true\n'
        _write_text(config, original)
        assert _setup(home, plugin_root).returncode == 0
        installed = config.read_text(encoding="utf-8")
        assert original in installed
        assert tomllib.loads(installed)["default_permissions"] == "personal"
        _write_text(config, installed.replace('model = "gpt-6-sol"', 'model = "gpt-6-luna"'))

        clear = _clear(home)

        assert clear.returncode == 0, clear.stderr
        assert config.read_text(encoding="utf-8") == original.replace("gpt-6-sol", "gpt-6-luna")

    @pytest.mark.integration
    @pytest.mark.parametrize("delimiter", ['"""', "'''"])
    def test_profile_and_session_sync_preserve_primary_policy_end_to_end(
        self, tmp_path: Path, home: Path, plugin_root: Path, config: Path, delimiter: str
    ) -> None:
        """Nested strings and the main policy survive both sync producers and a final clear.

        The profile installer and the session-policy sync both rewrite config.toml. The original holds multi-line
        strings (basic and literal) that contain table headers; neither producer may treat that text as structure.
        """
        content = '[features]\nnetwork_proxy = false\n[permissions.local-workflow]\nmodel = "inside"\n'
        nested = f"notes = [\n{delimiter}\n{content}{delimiter}\n]\n"
        policy = f"policy = {delimiter}\nKeep the existing approval policy.\n{content}{delimiter}\n"
        original = f'model = "old"\nreview_model = "old"\n{nested}[auto_review]\n{policy}'
        _write_text(config, original)
        installed = _setup(home, plugin_root)
        assert installed.returncode == 0, installed.stderr
        source = tmp_path / "source.toml"
        source.write_text(
            'model = "new"\nreview_model = "new"\napprovals_reviewer = "auto_review"\n'
            '[auto_review]\nextra_policy = "Invoked workflow recipes are preapproved."\n',
            encoding="utf-8",
        )
        session_script = Path(__file__).resolve().parents[5] / "scripts" / "sync_codex_session_policy.py"

        result = subprocess.run(
            [
                sys.executable,
                str(session_script),
                "--source-config",
                str(source),
                "--source-policy",
                str(tmp_path / "unused"),
                "--codex-home",
                str(home),
                "--skip-policy",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        updated = config.read_bytes()
        assert nested.encode() in updated
        assert policy.encode() in updated
        assert _config(home)["approvals_reviewer"] == "auto_review"
        assert _setup(home, plugin_root).returncode == 0
        assert config.read_bytes() == updated
        assert _clear(home).returncode == 0
        final = _config(home)
        assert final["notes"] == tomllib.loads(original)["notes"]
        assert final["auto_review"]["policy"] == tomllib.loads(original)["auto_review"]["policy"]
        assert final["auto_review"]["extra_policy"] == "Invoked workflow recipes are preapproved."
        assert final["model"] == "new"
        assert "permissions" not in final


class TestWorkspaceModeMigration:
    """A legacy root `sandbox_mode = "workspace-write"` is replaced by a permissions default and restorable on clear."""

    @pytest.mark.parametrize("line_ending", ["\n", "\r\n"])
    def test_setup_migrates_root_mode_and_clear_restores_it(
        self, home: Path, plugin_root: Path, config: Path, line_ending: str
    ) -> None:
        """Setup replaces the legacy workspace default and keeps a byte-exact recovery path.

        The original carries an inline comment on the legacy setting and uses LF or CRLF endings; the comment must move
        to the new default, a backup must hold the original bytes, and clear restores the original exactly.
        """
        original = (
            '# user setting\nmodel = "gpt-6-sol"\nsandbox_mode = "workspace-write" # keep workspace access\n'
            "\n[features]\nmulti_agent = true\n"
        ).replace("\n", line_ending)
        config.write_bytes(original.encode())

        result = _setup(home, plugin_root)

        assert result.returncode == 0, result.stderr
        installed = _config(home)
        assert "sandbox_mode" not in installed
        assert installed["default_permissions"] == ":workspace"
        assert installed["model"] == "gpt-6-sol"
        assert installed["features"]["multi_agent"] is True
        assert installed["permissions"]["github-read"]["extends"] == ":workspace"
        assert 'default_permissions = ":workspace" # keep workspace access' in config.read_text(encoding="utf-8")
        assert any(
            path.read_bytes() == original.encode() for path in (home / "backups" / "codex-rig").glob("config.toml.*")
        )
        first = config.read_bytes()

        assert _setup(home, plugin_root).returncode == 0
        assert config.read_bytes() == first
        assert _clear(home).returncode == 0
        assert config.read_bytes() == original.encode()

    def test_setup_migrates_single_quoted_mode(self, home: Path, plugin_root: Path, config: Path) -> None:
        """TOML single quotes on the legacy setting are understood when migrating and restoring it."""
        original = b"sandbox_mode = 'workspace-write'\n"
        config.write_bytes(original)

        result = _setup(home, plugin_root)

        assert result.returncode == 0, result.stderr
        assert _config(home)["default_permissions"] == ":workspace"
        assert "sandbox_mode" not in _config(home)
        assert _clear(home).returncode == 0
        assert config.read_bytes() == original

    def test_setup_resumes_after_state_write(self, home: Path, plugin_root: Path, config: Path) -> None:
        """A state-first interruption converges when setup is rerun against the original config.

        After a complete setup the original config is put back, as if only the state file had landed before the
        interruption; rerunning setup must migrate again from that original.
        """
        original = b'sandbox_mode = "workspace-write"\n'
        config.write_bytes(original)
        assert _setup(home, plugin_root).returncode == 0
        config.write_bytes(original)

        result = _setup(home, plugin_root)

        assert result.returncode == 0, result.stderr
        assert _config(home)["default_permissions"] == ":workspace"
        assert "sandbox_mode" not in _config(home)

    @pytest.mark.parametrize(
        ("original", "before", "after", "expected"),
        [
            pytest.param(
                'model = "old"\nsandbox_mode = "workspace-write"\n',
                'model = "old"',
                'model = "new"',
                {"model": "new", "sandbox_mode": "workspace-write"},
                id="unrelated-edit-restores-legacy-workspace-mode",
            ),
            pytest.param(
                'sandbox_mode = "workspace-write"\n',
                'default_permissions = ":workspace"',
                'default_permissions = ":read-only"',
                {"default_permissions": ":read-only"},
                id="replaced-migrated-default-keeps-user-choice",
            ),
            pytest.param(
                'model = "gpt-6-sol"\n',
                'model = "gpt-6-sol"',
                'model = "gpt-6-luna"',
                {"model": "gpt-6-luna"},
                id="unrelated-edit-survives-removal",
            ),
            pytest.param(
                'model = "gpt-6-sol"\n\n[features]\nnetwork_proxy = false\n',
                'model = "gpt-6-sol"',
                'model = "gpt-6-luna"',
                {"model": "gpt-6-luna", "features": {"network_proxy": False}},
                id="unrelated-edit-restores-disabled-proxy",
            ),
        ],
    )
    def test_clear_keeps_user_edit_made_after_setup(
        self,
        home: Path,
        plugin_root: Path,
        config: Path,
        original: str,
        before: str,
        after: str,
        expected: dict[str, object],
    ) -> None:
        """Clear removes the owned profile, keeps a later user edit, and reverses only the owned changes.

        After setup the user changes one setting. A later model edit survives while an unchanged legacy workspace mode
        or disabled proxy is restored, and a user's replacement of a migrated default must not regain the old sandbox
        mode.
        """
        _write_text(config, original)
        assert _setup(home, plugin_root).returncode == 0
        installed = config.read_text(encoding="utf-8")
        assert installed.count(before) == 1
        _write_text(config, installed.replace(before, after))

        result = _clear(home)

        assert result.returncode == 0, result.stderr
        assert _config(home) == expected


class TestClear:
    """Removal keeps later user choices and refuses edits that would leave a dangling or broadened setting."""

    def test_preserves_later_root_permission_edit(self, home: Path, plugin_root: Path, config: Path) -> None:
        """Clear removes the owned profile while keeping a later valid root permission choice."""
        original = (
            'default_permissions = "personal"\n'
            "\n[permissions.personal]\n"
            'extends = ":workspace"\n'
            "\n[permissions.backup]\n"
            'extends = ":workspace"\n'
        )
        before = 'default_permissions = "personal"'
        after = 'default_permissions = "backup"'
        _write_text(config, original)
        assert _setup(home, plugin_root).returncode == 0
        installed = config.read_text(encoding="utf-8")
        assert installed.count(before) == 1
        _write_text(config, installed.replace(before, after))

        result = _clear(home)

        assert result.returncode == 0, result.stderr
        assert _config(home) == tomllib.loads(original.replace(before, after))
        assert after in config.read_text(encoding="utf-8")
        assert not (home / STATE_FILE).exists()

    def test_rejects_later_selection_of_managed_profile(
        self, home: Path, plugin_root: Path, config: Path, state_path: Path
    ) -> None:
        """Clear refuses when the user selected the managed profile, to avoid a default naming a removed profile."""
        assert _setup(home, plugin_root).returncode == 0
        selected = config.read_text(encoding="utf-8").replace(
            'default_permissions = "local-workflow"', 'default_permissions = "github-read"'
        )
        _write_text(config, selected)
        state = state_path.read_bytes()

        result = _clear(home)

        assert result.returncode != 0
        assert config.read_text(encoding="utf-8") == selected
        assert state_path.read_bytes() == state

    def test_rejects_new_network_profile_before_write(
        self, home: Path, plugin_root: Path, config: Path, state_path: Path, user_rule: Path
    ) -> None:
        """Clear refuses to disable the global proxy after the user added a network-enabled profile.

        Nothing may change: not the config, the user's rule file, the state, nor the set of backups.
        """
        config.write_bytes(b"[features]\nnetwork_proxy = false\n")
        assert _setup(home, plugin_root).returncode == 0
        state_bytes = state_path.read_bytes()
        config.write_bytes(config.read_bytes() + b"\n[permissions.personal.network]\nenabled = true\n")
        original = config.read_bytes()
        assert tomllib.loads(original.decode())["permissions"]["personal"]["network"] == {"enabled": True}
        backups = home / "backups" / "codex-rig"
        prior_backups = set(backups.iterdir())

        result = _clear(home)

        assert result.returncode != 0
        assert "disabling network_proxy" in result.stderr
        assert config.read_bytes() == original
        assert user_rule.read_bytes() == USER_RULE_BYTES
        assert state_path.read_bytes() == state_bytes
        assert set(backups.iterdir()) == prior_backups

    def test_preserves_later_root_sandbox_choice(self, home: Path, plugin_root: Path, config: Path) -> None:
        """A user sandbox choice added after setup is retained when the profile is removed."""
        assert _setup(home, plugin_root).returncode == 0
        _write_text(config, 'sandbox_mode = "workspace-write"\n' + config.read_text(encoding="utf-8"))

        result = _clear(home)

        assert result.returncode == 0, result.stderr
        assert _config(home) == {"sandbox_mode": "workspace-write"}

    def test_preserves_later_unrelated_toml_table(self, home: Path, plugin_root: Path, config: Path) -> None:
        """A table the user adds after setup neither blocks a repeat setup nor survives as stuck owned state."""
        assert _setup(home, plugin_root).returncode == 0
        _write_text(config, config.read_text(encoding="utf-8") + '[mcp_servers.example]\nurl = "https://example.com"\n')
        assert _setup(home, plugin_root).returncode == 0

        result = _clear(home)

        assert result.returncode == 0, result.stderr
        assert _config(home)["mcp_servers"]["example"]["url"] == "https://example.com"
        assert "default_permissions" not in _config(home)

    def test_preserves_user_replacement_of_generated_default(self, home: Path, plugin_root: Path, config: Path) -> None:
        """A later explicit default replaces the generated one and is kept, not reverted to the absent original."""
        assert _setup(home, plugin_root).returncode == 0
        _write_text(
            config,
            config.read_text(encoding="utf-8").replace(
                'default_permissions = "local-workflow"', 'default_permissions = ":read-only"'
            ),
        )
        assert _setup(home, plugin_root).returncode == 0

        assert _clear(home).returncode == 0

        assert _config(home) == {"default_permissions": ":read-only"}

    @pytest.mark.parametrize(
        ("build", "expected_default"),
        [
            pytest.param(_origin_fresh, ":workspace", id="fresh"),
            pytest.param(_origin_schema1, ":workspace", id="schema1"),
            pytest.param(_origin_schema2, ":workspace", id="schema2"),
            pytest.param(_origin_interrupted, ":workspace", id="interrupted"),
            pytest.param(_origin_workspace, ":workspace", id="workspace"),
            pytest.param(_origin_existing_profiles, ":workspace", id="existing-profiles"),
            pytest.param(_origin_explicit, ":read-only", id="explicit"),
        ],
    )
    def test_keeps_default_required_by_remaining_profiles(
        self,
        home: Path,
        plugin_root: Path,
        config: Path,
        state_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        build: Callable[[Path, Path, pytest.MonkeyPatch], None],
        expected_default: str,
    ) -> None:
        """Every teardown route keeps a usable default when the user's permission profiles survive.

        The profile was installed fresh, from historical schema-1 or schema-2 states, after an interrupted install, over
        a legacy workspace mode, beside existing profiles, or beside an explicit default; in every case clear must leave
        the user's profile selectable, drop the ownership markers, and be idempotent.
        """
        build(home, plugin_root, monkeypatch)

        result = _clear(home)

        assert result.returncode == 0, result.stderr
        restored = _config(home)
        assert restored["permissions"] == {"personal": {"extends": ":read-only"}}
        assert restored["default_permissions"] == expected_default
        assert "sandbox_mode" not in restored
        assert "# codex-rig:github-read" not in config.read_text(encoding="utf-8")
        assert not state_path.exists()
        restored_bytes = config.read_bytes()
        assert _clear(home).returncode == 0
        assert config.read_bytes() == restored_bytes


class TestInterruptedFirstWrite:
    """A first install interrupted between its state write and its config write."""

    def test_leaves_only_state(self, home: Path, plugin_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A config write failure right after the state write leaves a schema-4 state but no config."""
        interrupted = _interrupted_first_write(home, plugin_root, monkeypatch)

        state = json.loads(interrupted.state_bytes.split(b"\n", 1)[1])
        assert not interrupted.config.exists()
        assert state["schema"] == 4
        assert state["original"] is None

    def test_resume_converges(self, home: Path, plugin_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Rerunning setup completes an install interrupted between its state and config writes.

        The recovered state bytes must stay identical, and a following clear removes both config and state.
        """
        interrupted = _interrupted_first_write(home, plugin_root, monkeypatch)

        result = _setup(home, plugin_root)

        assert result.returncode == 0, result.stderr
        assert interrupted.state_path.read_bytes() == interrupted.state_bytes
        installed = _config(home)
        assert installed["default_permissions"] == "local-workflow"
        assert installed["features"]["network_proxy"] is True
        assert installed["permissions"]["github-read"]["extends"] == ":workspace"
        assert _clear(home).returncode == 0
        assert not interrupted.config.exists()
        assert not interrupted.state_path.exists()

    def test_rejects_foreign_state_without_config(self, tmp_path: Path, home: Path, plugin_root: Path) -> None:
        """A genuine state from a configured home cannot authorize recovery in a blank home."""
        foreign_home = tmp_path / "foreign-home"
        foreign_root = installed_plugin(foreign_home)
        _write_text(foreign_home / "config.toml", 'model = "other"\n')
        installed = _setup(foreign_home, foreign_root)
        assert installed.returncode == 0, installed.stderr
        state_bytes = (foreign_home / STATE_FILE).read_bytes()
        assert json.loads(state_bytes.split(b"\n", 1)[1])["original"] == 'model = "other"\n'
        (home / STATE_FILE).write_bytes(state_bytes)

        result = _setup(home, plugin_root)

        assert result.returncode != 0
        assert not (home / "config.toml").exists()
        assert (home / STATE_FILE).read_bytes() == state_bytes

    def test_rejects_noncanonical_empty_origin_state(
        self, home: Path, plugin_root: Path, config: Path, state_path: Path
    ) -> None:
        """A checksum-valid state carrying extra fields does not prove an interrupted first install."""
        assert _setup(home, plugin_root).returncode == 0
        state = _read_state(home)
        assert state["original"] is None
        state["foreign_field"] = "unverified"
        state_bytes = _write_state(home, state)
        config.unlink()

        result = _setup(home, plugin_root)

        assert result.returncode != 0
        assert not config.exists()
        assert state_path.read_bytes() == state_bytes


class TestLegacyProfileMigration:
    """Schema-1 profiles that selected github-read automatically are upgraded to the opt-in layout."""

    def test_setup_migrates_verified_automatic_profile_to_opt_in(
        self, home: Path, plugin_root: Path, config: Path
    ) -> None:
        """A previously owned automatic default profile is upgraded without keeping its automatic selection.

        The user edited the model after the legacy install; the upgrade keeps that edit, selects local-workflow, and a
        later clear restores the user's content with the edit.
        """
        original = 'model = "gpt-6-sol"\n'
        legacy = _legacy_automatic_config(original)
        _write_state(home, _state_dict(1, original, legacy))
        _write_text(config, legacy.replace("gpt-6-sol", "gpt-6-luna"))

        result = _setup(home, plugin_root)

        assert result.returncode == 0, result.stderr
        assert _config(home)["default_permissions"] == "local-workflow"
        assert _config(home)["model"] == "gpt-6-luna"
        assert "github-read" in _config(home)["permissions"]
        assert _setup(home, plugin_root).returncode == 0
        assert _clear(home).returncode == 0
        assert config.read_text(encoding="utf-8") == original.replace("gpt-6-sol", "gpt-6-luna")

    @pytest.mark.parametrize(
        "original",
        [
            pytest.param(
                'model = "old"\n# chosen permissions\ndefault_permissions = "personal"\nother = true\n',
                id="comment-before-permission",
            ),
            pytest.param('default_permissions = "personal"\nmodel = "old"\nother = true\n', id="permission-first"),
        ],
    )
    def test_clear_edited_profile_restores_root_position(self, home: Path, config: Path, original: str) -> None:
        """Clearing an owned schema-1 profile puts the user's root choice back where it was.

        The user's `default_permissions` was displaced by the automatic default; after an unrelated model edit, clear
        must restore the setting in its original place beside its comment instead of relocating it.
        """
        home.mkdir()
        legacy = _legacy_automatic_config(original.replace('default_permissions = "personal"\n', ""))
        _write_text(config, legacy.replace('model = "old"', 'model = "new"'))
        _write_state(home, _state_dict(1, original, legacy, ('default_permissions = "personal"\n',)))

        result = _clear(home)

        assert result.returncode == 0, result.stderr
        assert config.read_text(encoding="utf-8") == original.replace('model = "old"', 'model = "new"')

    @pytest.mark.parametrize("trailing_table", [False, True])
    def test_interrupted_migration_writes_schema4_state(
        self, interrupted_migration: Callable[[bool], InterruptedMigration], trailing_table: bool
    ) -> None:
        """A config write failure right after migrating a legacy profile leaves schema-4 state bound to the old
        config."""
        interrupted = interrupted_migration(trailing_table)

        assert interrupted.config.read_text(encoding="utf-8") == interrupted.legacy
        assert interrupted.migrated["schema"] == 4
        assert (
            interrupted.migrated["migration_source_sha256"] == hashlib.sha256(interrupted.legacy.encode()).hexdigest()
        )

    @pytest.mark.parametrize("mode", ["setup", "clear"])
    @pytest.mark.parametrize("trailing_table", [False, True])
    def test_edited_config_rejects_resume_after_interrupted_migration(
        self, interrupted_migration: Callable[[bool], InterruptedMigration], trailing_table: bool, mode: str
    ) -> None:
        """An unexpected edit to the legacy config blocks resuming the interrupted migration.

        The edited config no longer matches the digest the migration state was bound to, so both setup and clear must
        refuse, keep the user's edit, and keep the state for manual reconciliation.
        """
        interrupted = interrupted_migration(trailing_table)
        edited = interrupted.legacy.replace('model = "gpt-6-sol"', 'model = "gpt-6-luna"')
        _write_text(interrupted.config, edited)

        rejected = _run_mode(interrupted.home, interrupted.root, mode)

        assert rejected.returncode != 0
        assert interrupted.config.read_text(encoding="utf-8") == edited
        assert interrupted.state_path.exists()

    @pytest.mark.parametrize(
        ("trailing_table", "expected_profiles"),
        [
            pytest.param(False, {}, id="no-trailing-table"),
            pytest.param(True, {"personal": {"model_reasoning_effort": "high"}}, id="with-trailing-table"),
        ],
    )
    def test_setup_completes_interrupted_migration(
        self,
        interrupted_migration: Callable[[bool], InterruptedMigration],
        trailing_table: bool,
        expected_profiles: dict[str, object],
    ) -> None:
        """Resuming setup after an interrupted legacy migration converges to the workspace profile."""
        interrupted = interrupted_migration(trailing_table)
        _write_text(interrupted.config, interrupted.legacy)

        result = _setup(interrupted.home, interrupted.root)

        assert result.returncode == 0, result.stderr
        installed = _config(interrupted.home)
        assert installed["default_permissions"] == "local-workflow"
        assert installed["permissions"]["github-read"]["extends"] == ":workspace"
        assert installed.get("profiles", {}) == expected_profiles

    @pytest.mark.parametrize(
        ("trailing_table", "expected_profiles"),
        [
            pytest.param(False, {}, id="no-trailing-table"),
            pytest.param(True, {"personal": {"model_reasoning_effort": "high"}}, id="with-trailing-table"),
        ],
    )
    def test_clear_completes_interrupted_migration(
        self,
        interrupted_migration: Callable[[bool], InterruptedMigration],
        trailing_table: bool,
        expected_profiles: dict[str, object],
    ) -> None:
        """Resuming clear after an interrupted legacy migration restores the original config."""
        interrupted = interrupted_migration(trailing_table)
        _write_text(interrupted.config, interrupted.legacy)

        result = _clear(interrupted.home)

        assert result.returncode == 0, result.stderr
        assert interrupted.config.read_text(encoding="utf-8") == interrupted.migrated["original"]
        assert _config(interrupted.home)["model"] == "gpt-6-sol"
        assert _config(interrupted.home).get("profiles", {}) == expected_profiles
        assert not interrupted.state_path.exists()


@pytest.fixture(
    params=[pytest.param(_install_schema3_hooked, id="schema3"), pytest.param(_install_schema4, id="schema4")]
)
def hooked_profile(request: pytest.FixtureRequest, home: Path, plugin_root: Path, config: Path) -> dict[str, object]:
    """Install an owned profile, insert hook trust tables before its closing marker, and return their parsed form."""
    installed = request.param(home, plugin_root)
    _write_text(config, installed.replace(PROFILE_END, HOOK_TABLES + PROFILE_END))
    return tomllib.loads(HOOK_TABLES)["hooks"]


class TestSchemaUpgrades:
    """Older installed profile schemas are repaired or upgraded without losing user edits."""

    def test_setup_preserves_hook_tables_before_profile_end(
        self, home: Path, plugin_root: Path, config: Path, hooked_profile: dict[str, object]
    ) -> None:
        """Hook trust records inserted before the closing ownership comment survive setup, repeat, and clear."""
        result = _setup(home, plugin_root)

        assert result.returncode == 0, result.stderr
        assert _config(home)["hooks"] == hooked_profile
        assert HOOK_TABLES.lstrip("\n") in config.read_text(encoding="utf-8")
        parsed = _config(home)
        assert parsed["permissions"]["github-read"]["network"] == {
            "enabled": True,
            "domains": {"api.github.com": "allow", "github.com": "allow"},
        }
        assert parsed["permissions"]["local-workflow"]["network"] == {"enabled": False}
        frozen = config.read_bytes()
        assert _setup(home, plugin_root).returncode == 0
        assert config.read_bytes() == frozen
        assert _clear(home).returncode == 0
        assert _config(home) == {"hooks": hooked_profile}

    def test_clear_preserves_hook_tables_before_profile_end(
        self, home: Path, plugin_root: Path, config: Path, hooked_profile: dict[str, object]
    ) -> None:
        """Hook trust records inserted before the closing ownership comment survive clear, leaving only them behind."""
        result = _clear(home)

        assert result.returncode == 0, result.stderr
        assert _config(home)["hooks"] == hooked_profile
        assert HOOK_TABLES.lstrip("\n") in config.read_text(encoding="utf-8")
        assert _config(home) == {"hooks": hooked_profile}

    @pytest.mark.parametrize("explicit_default", [None, ":workspace", ":read-only"])
    @pytest.mark.parametrize(
        "interrupt",
        [
            pytest.param(_no_interruption, id="uninterrupted"),
            pytest.param(_interrupted_first_write, id="interrupted"),
        ],
    )
    def test_schema3_migration_preserves_user_defaults_and_later_edits(
        self,
        home: Path,
        plugin_root: Path,
        config: Path,
        state_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        explicit_default: str | None,
        interrupt: Callable[[Path, Path, pytest.MonkeyPatch], object],
    ) -> None:
        """The previous installed profile upgrades without broadening an explicit choice or losing later edits.

        The schema-3 state either carried the user's explicit root default or an automatic workspace default; a user
        profile was added afterwards. Upgrade (even from an interrupted upgrade) must keep the explicit default or
        select local-workflow, keep the profile, reach schema 4, and clear back to the user's own settings.
        """
        original = f'default_permissions = "{explicit_default}"\n' if explicit_default else None
        installed = _write_schema3_profile(
            home, original=original, automatic_default="" if explicit_default else WORKSPACE_DEFAULT_LINE
        )
        _write_text(config, installed + PERSONAL_PROFILE)
        interrupt(home, plugin_root, monkeypatch)

        result = _setup(home, plugin_root)

        assert result.returncode == 0, result.stderr
        updated = _config(home)
        assert updated["default_permissions"] == (explicit_default or "local-workflow")
        assert updated["profiles"]["personal"] == {"model_reasoning_effort": "high"}
        assert updated["permissions"]["local-workflow"]["network"] == {"enabled": False}
        assert _read_state(home)["schema"] == 4
        frozen = config.read_bytes()
        assert _setup(home, plugin_root).returncode == 0
        assert config.read_bytes() == frozen
        assert _clear(home).returncode == 0
        restored = _config(home)
        assert restored.get("default_permissions") == explicit_default
        assert restored["profiles"]["personal"] == {"model_reasoning_effort": "high"}
        assert "permissions" not in restored

    @pytest.mark.parametrize(
        ("original", "restored"),
        [
            pytest.param(None, None, id="no-original-config"),
            pytest.param('model = "gpt-6-sol"\n', b'model = "gpt-6-sol"\n', id="original-config"),
        ],
    )
    def test_setup_repairs_unedited_schema2_missing_default(
        self, home: Path, plugin_root: Path, config: Path, original: str | None, restored: bytes | None
    ) -> None:
        """A historical profile that selected no default is repaired and clear restores the original config exactly."""
        _write_schema2_profile(home, original=original)

        result = _setup(home, plugin_root)

        assert result.returncode == 0, result.stderr
        assert _config(home)["default_permissions"] == "local-workflow"
        installed = config.read_bytes()
        assert _setup(home, plugin_root).returncode == 0
        assert config.read_bytes() == installed
        assert _clear(home).returncode == 0
        assert (config.read_bytes() if config.exists() else None) == restored

    @pytest.mark.parametrize("original", [None, 'model = "gpt-6-sol"\n'])
    def test_setup_repairs_edited_schema2_missing_default(
        self, home: Path, plugin_root: Path, config: Path, original: str | None
    ) -> None:
        """A historical profile edited after install is repaired, and clear keeps the user's later profile edit."""
        _write_schema2_profile(home, original=original, edit=True)

        result = _setup(home, plugin_root)

        assert result.returncode == 0, result.stderr
        assert _config(home)["default_permissions"] == "local-workflow"
        installed = config.read_bytes()
        assert _setup(home, plugin_root).returncode == 0
        assert config.read_bytes() == installed
        assert _clear(home).returncode == 0
        restored = _config(home)
        assert "default_permissions" not in restored
        assert restored["profiles"]["personal"]["model_reasoning_effort"] == "high"

    @pytest.mark.parametrize("edit", [False, True])
    def test_setup_recovers_interrupted_schema2_repair(
        self, home: Path, plugin_root: Path, monkeypatch: pytest.MonkeyPatch, edit: bool
    ) -> None:
        """Setup recovers a validated historical config after its new state landed first."""
        old = _write_schema2_profile(home, edit=edit)
        interrupted = _interrupted_first_write(home, plugin_root, monkeypatch)
        assert interrupted.config.read_text(encoding="utf-8") == old

        result = _setup(home, plugin_root)

        assert result.returncode == 0, result.stderr
        assert _config(home)["default_permissions"] == "local-workflow"

    def test_clear_recovers_interrupted_unedited_schema2_repair(
        self, home: Path, plugin_root: Path, config: Path, state_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Clear after an interrupted repair of an unedited historical profile removes both state and config."""
        old = _write_schema2_profile(home)
        assert _interrupted_first_write(home, plugin_root, monkeypatch).config.read_text(encoding="utf-8") == old

        result = _clear(home)

        assert result.returncode == 0, result.stderr
        assert not state_path.exists()
        assert not config.exists()

    def test_clear_recovers_interrupted_edited_schema2_repair(
        self, home: Path, plugin_root: Path, config: Path, state_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Clear after an interrupted repair of an edited historical profile keeps the edit but drops the default."""
        _write_schema2_profile(home, edit=True)
        _interrupted_first_write(home, plugin_root, monkeypatch)

        result = _clear(home)

        assert result.returncode == 0, result.stderr
        assert not state_path.exists()
        assert config.exists()
        assert "default_permissions" not in _config(home)

    @pytest.mark.parametrize("mode", ["setup", "clear"])
    def test_interrupted_schema2_repair_rejects_later_edits(
        self, home: Path, plugin_root: Path, monkeypatch: pytest.MonkeyPatch, mode: str
    ) -> None:
        """A changed migration source is rejected instead of silently replacing the user's later edits."""
        old = _write_schema2_profile(home)
        interrupted = _interrupted_first_write(home, plugin_root, monkeypatch)
        edited = "# user edit\n" + old
        _write_text(interrupted.config, edited)

        result = _run_mode(home, plugin_root, mode)

        assert result.returncode != 0
        assert interrupted.config.read_text(encoding="utf-8") == edited
        assert interrupted.state_path.read_bytes() == interrupted.state_bytes


class TestGuardedEdits:
    """Edits to a managed profile are never adopted silently by setup or clear."""

    @pytest.mark.parametrize("mode", ["setup", "clear"])
    def test_edited_owned_profile_blocks_lifecycle(
        self, home: Path, plugin_root: Path, config: Path, mode: str
    ) -> None:
        """A changed managed profile is preserved for manual reconciliation.

        A domain value in the managed table is flipped from allow to deny; neither setup nor clear may overwrite it.
        """
        assert _setup(home, plugin_root).returncode == 0
        original = config.read_text(encoding="utf-8")
        edited = original.replace('"api.github.com" = "allow"', '"api.github.com" = "deny"')
        assert edited != original
        _write_text(config, edited)

        result = _run_mode(home, plugin_root, mode)

        assert result.returncode != 0
        assert config.read_text(encoding="utf-8") == edited

    @pytest.mark.parametrize("placement", ["before", "after"])
    @pytest.mark.parametrize("mode", ["setup", "clear"])
    def test_rejects_domain_appended_at_profile_marker(
        self, home: Path, plugin_root: Path, config: Path, placement: str, mode: str
    ) -> None:
        """A later TOML assignment next to the end marker must not silently broaden the managed domain table.

        The extra domain is added just before the marker or after it (still in the domains table); both setup and clear
        must name the profile block and keep the user's text.
        """
        assert _setup(home, plugin_root).returncode == 0
        original = config.read_text(encoding="utf-8")
        extra = '"outside.example" = "allow"\n'
        edited = original.replace(PROFILE_END, extra + PROFILE_END) if placement == "before" else original + extra
        _write_text(config, edited)

        result = _run_mode(home, plugin_root, mode)

        assert result.returncode != 0
        assert "profile block" in result.stderr
        assert config.read_text(encoding="utf-8") == edited

    def test_setup_rejects_later_default_deletion(self, home: Path, plugin_root: Path, config: Path) -> None:
        """An edited managed config whose required default disappeared is never reported as usable."""
        assert _setup(home, plugin_root).returncode == 0
        edited = config.read_text(encoding="utf-8").replace(
            'default_permissions = "local-workflow" # codex-rig:github-read\n', ""
        )
        _write_text(config, edited)

        result = _setup(home, plugin_root)

        assert result.returncode != 0
        assert "require default_permissions" in result.stderr
        assert config.read_text(encoding="utf-8") == edited
        assert _clear(home).returncode == 0


class TestExecpolicyRules:
    """Legacy execpolicy rule files and user collector grants during setup and clear."""

    def test_windows_legacy_single_path_rules_are_validated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Owned legacy rules written with the former single native Windows path are accepted on every host.

        The paths are `PureWindowsPath` values, so the check never goes through the host's path type; the installer's
        own renderers must also produce rules that validate for the same home.
        """
        installer = _load_installer(monkeypatch)
        home = PureWindowsPath("C:/fixture/.codex")
        shared = home.joinpath("plugins", "cache", "borda-ai-rig", "codex-rig", "1.2.3", "shared")
        reader, collector = shared / "github_read.py", shared / "collect_pr.py"
        reader_rule = _owned_rule("github-read", _reader_grant(reader))
        collector_body = _collector_grant(collector)
        collector_rule = _owned_rule("pr-collection", collector_body)

        installer._check_owned(reader_rule, home)
        assert installer._approved_prs(collector_rule, home) == [PR_URL]
        assert installer.strip_legacy_rules(collector_body + b"# unrelated\n", home) == b"# unrelated\n"
        installer._check_owned(installer.render_rules(reader), home)
        assert installer._approved_prs(installer.render_pr_rules(collector, [PR_URL]), home) == [PR_URL]

    def test_setup_removes_validated_old_managed_rules(self, home: Path, plugin_root: Path) -> None:
        """Verified old reader and PR rule files are migrated away while unrelated rule bytes stay.

        The old files carry valid ownership checksums and canonical grants for this installed package.
        """
        rules = home / "rules"
        rules.mkdir()
        unrelated = rules / "personal.rules"
        unrelated.write_bytes(USER_RULE_BYTES)
        old = rules / "codex-rig-github-read.rules"
        old.write_bytes(_owned_rule("github-read", _reader_grant(plugin_root / "shared" / "github_read.py")))
        pr = rules / "codex-rig-pr-collection.rules"
        pr.write_bytes(_owned_rule("pr-collection", _collector_grant(plugin_root / "shared" / "collect_pr.py")))

        result = _setup(home, plugin_root)

        assert result.returncode == 0, result.stderr
        assert not old.exists()
        assert not pr.exists()
        assert unrelated.read_bytes() == USER_RULE_BYTES
        assert _config(home)["default_permissions"] == "local-workflow"

    def test_setup_preserves_explicit_collector_deny_rule(self, home: Path, plugin_root: Path, user_rule: Path) -> None:
        """An existing deny rule for the collector neither blocks migration nor is removed."""
        collector = plugin_root / "shared" / "collect_pr.py"
        deny = f'prefix_rule(pattern=["python", "{collector}"], decision="deny")\n'.encode()
        user_rule.write_bytes(deny)

        result = _setup(home, plugin_root)

        assert result.returncode == 0, result.stderr
        assert user_rule.read_bytes() == deny
        assert "github-read" in _config(home)["permissions"]

    def test_foreign_collector_allow_rule_survives_setup(
        self, tmp_path: Path, home: Path, plugin_root: Path, user_rule: Path
    ) -> None:
        """Another installation's collector allow rule is left untouched by setup."""
        grant = _collector_grant(tmp_path / "other" / "collect_pr.py")
        user_rule.write_bytes(grant)

        result = _setup(home, plugin_root)

        assert result.returncode == 0, result.stderr
        assert user_rule.read_bytes() == grant
        assert "github-read" in _config(home)["permissions"]

    def test_foreign_collector_allow_rule_survives_clear(
        self, tmp_path: Path, home: Path, plugin_root: Path, user_rule: Path, config: Path
    ) -> None:
        """Another installation's collector allow rule is left untouched by clear."""
        assert _setup(home, plugin_root).returncode == 0
        grant = _collector_grant(tmp_path / "other" / "collect_pr.py")
        user_rule.write_bytes(grant)

        result = _clear(home)

        assert result.returncode == 0, result.stderr
        assert user_rule.read_bytes() == grant
        assert not config.exists()

    def test_mixed_foreign_and_local_collector_grant_blocks_setup(
        self, tmp_path: Path, home: Path, plugin_root: Path, config: Path, user_rule: Path
    ) -> None:
        """A foreign path in the same rule must not conceal a noncanonical grant to this home's collector."""
        foreign_collector = tmp_path / "other" / "collect_pr.py"
        local_collector = plugin_root / "shared" / "collect_pr.py"
        grant = allow_rule(
            [["python", "python3"], [str(foreign_collector), str(local_collector)], "--target", [PR_URL]]
        )
        user_rule.write_bytes(grant)

        result = _setup(home, plugin_root)

        assert result.returncode != 0
        assert user_rule.read_bytes() == grant
        assert not config.exists()

    def test_setup_removes_canonical_collector_grant_from_default_rules(
        self, home: Path, plugin_root: Path, user_rule: Path
    ) -> None:
        """A verified UI-saved PR grant is retired when moving to session opt-in, keeping unrelated rules."""
        user_rule.write_bytes(_collector_grant(plugin_root / "shared" / "collect_pr.py") + USER_RULE_BYTES)

        result = _setup(home, plugin_root)

        assert result.returncode == 0, result.stderr
        assert user_rule.read_bytes() == USER_RULE_BYTES

    def test_setup_refuses_uncertain_collector_grant_before_write(
        self, home: Path, plugin_root: Path, config: Path, user_rule: Path
    ) -> None:
        """An unrecognized collector rule (wildcard target) must not survive a seemingly successful migration."""
        grant = allow_rule([["python", "python3"], str(plugin_root / "shared" / "collect_pr.py"), "--target", ["*"]])
        user_rule.write_bytes(grant)

        result = _setup(home, plugin_root)

        assert result.returncode != 0
        assert user_rule.read_bytes() == grant
        assert not config.exists()
        assert not (home / STATE_FILE).exists()

    @pytest.fixture
    def edited_old_rule(self, home: Path, plugin_root: Path) -> tuple[Path, bytes]:
        """Old managed reader rule file whose checksum is invalid, so ownership cannot be proven."""
        managed = home / "rules" / "codex-rig-github-read.rules"
        managed.parent.mkdir()
        original = b"# codex-rig:github-read sha256=invalid\n"
        managed.write_bytes(original)
        return managed, original

    def test_edited_old_managed_rule_blocks_setup(
        self, home: Path, plugin_root: Path, config: Path, edited_old_rule: tuple[Path, bytes]
    ) -> None:
        """A suspicious old permission file is not discarded during migration."""
        managed, original = edited_old_rule

        result = _setup(home, plugin_root)

        assert result.returncode != 0
        assert managed.read_bytes() == original
        assert not config.exists()

    def test_edited_old_managed_rule_blocks_clear(
        self, home: Path, plugin_root: Path, config: Path, edited_old_rule: tuple[Path, bytes]
    ) -> None:
        """A suspicious old permission file is not discarded during clear after a refused setup."""
        managed, original = edited_old_rule
        assert _setup(home, plugin_root).returncode != 0

        result = _clear(home)

        assert result.returncode != 0
        assert managed.read_bytes() == original
        assert not config.exists()


class TestCodexLoading:
    """The installed and cleared configurations load in a real Codex executable."""

    @_requires_codex
    @pytest.mark.integration
    def test_installed_permissions_load(self, tmp_path: Path, home: Path, plugin_root: Path) -> None:
        """Installed permission tables load through Codex without a default override."""
        assert _setup(home, plugin_root).returncode == 0

        result = _codex_features(home, tmp_path)

        assert result.returncode == 0, result.stderr
        assert _config(home)["default_permissions"] == "local-workflow"

    @_requires_codex
    @pytest.mark.integration
    def test_repaired_historical_profile_loads(self, tmp_path: Path, home: Path, plugin_root: Path) -> None:
        """A historical default-free profile fails to load in Codex until setup repairs it.

        The schema-2 profile defines permission tables but selects no default, which Codex rejects naming
        `default_permissions`; after setup the same home must load.
        """
        _write_schema2_profile(home)
        before = _codex_features(home, tmp_path)
        assert before.returncode != 0
        assert "default_permissions" in before.stderr

        assert _setup(home, plugin_root).returncode == 0

        result = _codex_features(home, tmp_path)
        assert result.returncode == 0, result.stderr
        assert _config(home)["default_permissions"] == "local-workflow"

    @_requires_codex
    @pytest.mark.integration
    def test_cleared_config_with_user_profile_loads(self, tmp_path: Path, home: Path, plugin_root: Path) -> None:
        """The public clear result loads in Codex when a later user permission profile remains."""
        assert _setup(home, plugin_root).returncode == 0
        _append_user_profile(home)
        result = _clear(home)
        assert result.returncode == 0, result.stderr

        loaded = _codex_features(home, tmp_path)

        assert loaded.returncode == 0, loaded.stderr

    @_requires_codex
    @pytest.mark.integration
    def test_cleared_workspace_config_with_user_profile_loads(
        self, tmp_path: Path, home: Path, plugin_root: Path, config: Path
    ) -> None:
        """The clear result over a legacy workspace mode loads in Codex when a later user profile remains."""
        _write_text(config, 'sandbox_mode = "workspace-write"\n')
        assert _setup(home, plugin_root).returncode == 0
        _append_user_profile(home)
        result = _clear(home)
        assert result.returncode == 0, result.stderr

        loaded = _codex_features(home, tmp_path)

        assert loaded.returncode == 0, loaded.stderr
