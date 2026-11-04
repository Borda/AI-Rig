"""Acceptance checks for the native cross-platform Codex restore entrypoint."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from _modules import load_module

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10/3.11 compatibility
    import tomli as tomllib


PLUGIN_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
SYNC_SCRIPT = PLUGIN_ROOT / "scripts" / "sync_codex.py"
_PLUGIN_IDS = ("codex-rig@borda-ai-rig", "codemap-py@borda-ai-rig", "bridge@borda-ai-rig")
_ALL_INSTALLED = [
    {"pluginId": _PLUGIN_IDS[0], "enabled": True, "version": "0.3.0"},
    {"pluginId": _PLUGIN_IDS[1], "enabled": True, "version": "0.28.8"},
    {"pluginId": _PLUGIN_IDS[2], "enabled": True, "version": "0.1.0"},
]
_SPARSE_PATHS = (".agents/plugins", "plugins/codex-rig", "plugins/codemap-py", "plugins/bridge_cc-codex")
_SPARSE_ARGS = tuple(argument for path in _SPARSE_PATHS for argument in ("--sparse", path))
Calls = list[tuple[str, ...]]
Runner = Callable[..., subprocess.CompletedProcess[str]]


def _load_sync() -> ModuleType:
    """Load the packaged sync entrypoint without package imports."""
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    return load_module("codex_rig_sync_codex", SYNC_SCRIPT)


@pytest.fixture
def sync() -> ModuleType:
    """Return a fresh copy of the packaged sync module."""
    return _load_sync()


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
    for directory in ("assets", "scripts", ".codex-plugin", "shared"):
        (plugin / directory).mkdir(parents=True)
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
    calls: Calls,
    *,
    configured_ref: str = "",
    source_type: str = "git",
    sparse_paths: tuple[str, ...] = _SPARSE_PATHS,
    installed: list[dict[str, object]] | None = None,
    python_probe: tuple[str, str] = ("Python 3.12.0\n", ""),
    first_listing_empty: bool = False,
) -> Runner:
    """Return deterministic Codex/Git command results for one restore.

    `python_probe` is the stdout and stderr of the MCP interpreter version probe, `installed` the managed plugins the
    marketplace reports, and `first_listing_empty` hides the marketplace until it has been registered once.
    """
    (marketplace_root / ".codex-marketplace-install.json").write_text(
        json.dumps({"ref_name": configured_ref}), encoding="utf-8"
    )
    listings = 0

    def _run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        """Record a sync command and return the configured marketplace response."""
        nonlocal listings
        rendered = tuple(str(item) for item in command)
        calls.append(rendered)
        if rendered[1:5] == ("plugin", "marketplace", "list", "--json"):
            listings += 1
            marketplace = {
                "name": "borda-ai-rig",
                "root": str(marketplace_root),
                "marketplaceSource": {"sourceType": source_type},
            }
            payload = {"marketplaces": [] if first_listing_empty and listings == 1 else [marketplace]}
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        if rendered[1:5] == ("plugin", "list", "--marketplace", "borda-ai-rig"):
            payload = {"installed": _ALL_INSTALLED if installed is None else installed}
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        if rendered == ("git", "-C", str(marketplace_root), "sparse-checkout", "list"):
            return subprocess.CompletedProcess(command, 0, "\n".join(sparse_paths), "")
        if rendered[:3] == ("git", "-C", str(marketplace_root)):
            return subprocess.CompletedProcess(command, 0, "0123456789abcdef\n", "")
        if rendered == ("python", "--version"):
            return subprocess.CompletedProcess(command, 0, *python_probe)
        if rendered[0] == "python" and rendered[-2:] == ("--direction", "claude"):
            return subprocess.CompletedProcess(command, 0, json.dumps({"ok": True, "live": False}), "")
        return subprocess.CompletedProcess(command, 0, "", "")

    return _run


def _intercept(
    base_run: Runner,
    calls: Calls,
    matches: Callable[[list[str]], bool],
    respond: Callable[..., subprocess.CompletedProcess[str]],
) -> Runner:
    """Answer commands selected by `matches` with `respond`, recording them, and delegate the rest to `base_run`."""

    def _run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Route one sync command to the replacement or the base runner."""
        if matches(command):
            calls.append(tuple(str(item) for item in command))
            return respond(command, **kwargs)
        return base_run(command, **kwargs)

    return _run


def _is_script(command: list[str], name: str) -> bool:
    """Report whether `command` runs the helper script `name`."""
    return len(command) > 1 and Path(command[1]).name == name


def _is_instruction_check(command: list[str]) -> bool:
    """Report whether `command` is the read-only global-instruction health check."""
    return _is_script(command, "install_global_agents.py") and "--check" in command


def _is_instruction_installer(command: list[str]) -> bool:
    """Report whether `command` runs the global-instruction installer in any mode."""
    return _is_script(command, "install_global_agents.py")


def _is_rules_helper(command: list[str]) -> bool:
    """Report whether `command` runs the GitHub-read rules helper."""
    return _is_script(command, "install_github_read_rules.py")


def _fixed(returncode: int, stdout: str = "", stderr: str = "") -> Callable[..., subprocess.CompletedProcess[str]]:
    """Return a responder that always answers with the given exit status and streams."""

    def respond(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        """Answer one intercepted command with the fixed result."""
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    return respond


def _run_real_instruction_installer(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    """Run the packaged global-instruction installer for real, against whatever home the arguments name."""
    return subprocess.run([sys.executable, str(SCRIPTS_DIR / "install_global_agents.py"), *command[2:]], **kwargs)


def _run_sync(
    sync: ModuleType,
    arguments: list[str],
    run: Runner,
    *,
    environ: dict[str, str] | None = None,
    stdout: io.StringIO | None = None,
    sleeps: list[float] | None = None,
) -> int:
    """Run `sync_codex` with parsed `arguments`, an empty environment unless given, and discarded output.

    Retry pauses are recorded in `sleeps` instead of waited out, so a test never spends real time on a backoff.
    """
    return sync.sync_codex(
        sync.parse_args(arguments),
        run=run,
        environ={} if environ is None else environ,
        stdout=io.StringIO() if stdout is None else stdout,
        sleep=(sleeps if sleeps is not None else []).append,
    )


def _plugin_calls(verb: str) -> set[tuple[str, ...]]:
    """Return the `codex plugin <verb>` command for each managed plugin."""
    return {("codex", "plugin", verb, plugin_id) for plugin_id in _PLUGIN_IDS}


def _changes_installation(calls: Calls) -> Calls:
    """Return the calls that refresh, remove, or add marketplace or plugin state."""
    return [
        call
        for call in calls
        if call[1:4] in {("plugin", "marketplace", "upgrade"), ("plugin", "marketplace", "remove")}
        or call[1:3] in {("plugin", "remove"), ("plugin", "add")}
    ]


def _removes_global_agents(calls: Calls, home: Path) -> bool:
    """Report whether the managed global-instruction block was removed from `home`."""
    installer = str(SCRIPTS_DIR / "install_global_agents.py")
    return any(call[1:] == (installer, "--remove", "--codex-home", str(home)) for call in calls)


def _copy_packaged_scripts(destination: Path, *names: str) -> None:
    """Replace fixture helper scripts in `destination` with the real packaged ones."""
    for name in names:
        (destination / name).write_bytes((SCRIPTS_DIR / name).read_bytes())


def _forbidden_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
    """Fail if sync reaches a local mutation or marketplace inspection."""
    raise AssertionError("sync command ran before the prerequisite check")


def test_check_only_workflow_reaches_instruction_health_before_refresh_boundary() -> None:
    """A read-only sync check must diagnose composed instructions before its mandatory stop.

    The skill text orders the steps; a check-only request that stopped before instruction health would report a clean
    sync while the active global instructions were degraded.
    """
    skill = (PLUGIN_ROOT / "skills" / "sync" / "SKILL.md").read_text(encoding="utf-8")
    health = skill.index("### 02b: Global instruction health")
    boundary = skill.index("### 04: Stop after dry run")
    refresh = skill.index("### 05: Refresh through the Codex CLI")

    assert health < boundary < refresh
    assert "install_global_agents.py --check" in skill[health:boundary]
    assert "A nonzero result is degraded instruction composition" in skill[health:boundary]
    assert "A check-only request" in skill[boundary:refresh]


@pytest.mark.parametrize("action", ["install", "clear"])
class TestPythonPrerequisite:
    """Both sync actions check the interpreter before running any command."""

    def test_missing_tomli_stops_python310(
        self, sync: ModuleType, monkeypatch: pytest.MonkeyPatch, action: str
    ) -> None:
        """A missing parser must stop either sync action before invoking a command.

        On Python 3.10 the TOML parser is the `tomli` backport; without it the profile cannot be read or written.
        """
        monkeypatch.setattr(sync.sys, "version_info", (3, 10, 0))
        monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)

        with pytest.raises(sync.SyncError, match="tomli"):
            _run_sync(sync, [action], _forbidden_run)

    def test_unsupported_python_is_named_before_tomli(
        self, sync: ModuleType, monkeypatch: pytest.MonkeyPatch, action: str
    ) -> None:
        """An unsupported interpreter is named as such, not misreported as a missing tomli on Python 3.10.

        The interpreter reports 3.9.6 and no `tomli` is importable. Sync must name the version floor and never mention
        `tomli`, because installing the backport would not make Python 3.9 usable and would send the user the wrong way.
        """
        monkeypatch.setattr(sync.sys, "version_info", (3, 9, 6))
        monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)

        with pytest.raises(sync.SyncError, match=r"Python 3\.10 or newer; found Python 3\.9\.6") as raised:
            _run_sync(sync, [action], _forbidden_run)

        assert "tomli" not in str(raised.value)


class TestMcpInterpreter:
    """The `python` launcher the MCP servers use must be Python 3.10 or newer before anything changes."""

    @pytest.mark.parametrize(
        ("version", "returncode"),
        [
            pytest.param("", 127, id="python-unavailable"),
            pytest.param("unrecognized version", 0, id="python-version-invalid"),
            pytest.param("Python 3.9.19", 0, id="python-too-old"),
        ],
    )
    def test_prerequisite_precedes_marketplace_mutations(
        self, sync: ModuleType, tmp_path: Path, version: str, returncode: int
    ) -> None:
        """Fail install before marketplace writes without creating a home interpreter.

        The `python --version` probe reports the launcher missing, unparsable, or too old, and every other command is
        forbidden. Sync must stop at the probe with the 3.10 message and leave the home directory uncreated.
        """
        calls: Calls = []
        probe = _intercept(
            _forbidden_run, calls, lambda command: command == ["python", "--version"], _fixed(returncode, version)
        )
        home = tmp_path / "home"

        with pytest.raises(sync.SyncError, match=r"Python 3.10 or newer"):
            _run_sync(sync, [], probe, environ={"HOME": str(home)})

        assert calls == [("python", "--version")]
        assert not home.exists()

    def test_missing_executable_stops_before_marketplace(self, sync: ModuleType) -> None:
        """Treat an absent MCP launcher as a prerequisite failure without a fallback.

        Running `python` raises `FileNotFoundError`, as on a host with no such executable. Sync must report the 3.10
        requirement after exactly one probe attempt, never try another interpreter name, and run nothing else.
        """
        calls: list[list[str]] = []

        def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            """Simulate an unavailable python command at the real process boundary."""
            calls.append(command)
            raise FileNotFoundError("python")

        with pytest.raises(sync.SyncError, match=r"Python 3.10 or newer"):
            _run_sync(sync, [], run)

        assert calls == [["python", "--version"]]

    def test_version_on_stderr_satisfies_prerequisite(self, sync: ModuleType, tmp_path: Path) -> None:
        """Allow the minimum supported launcher, which prints its version on stderr, without creating a home shim.

        A complete marketplace fixture is synced while the probe prints `Python 3.10.0` on stderr only, as older
        interpreters do. The probe must be read from either stream, run once, and succeed without writing under home.
        """
        root = _marketplace_fixture(tmp_path)
        calls: Calls = []
        home = tmp_path / "home"

        result = _run_sync(
            sync, [], _fake_runner(root, calls, python_probe=("", "Python 3.10.0\n")), environ={"HOME": str(home)}
        )

        assert result == 0
        assert calls[0] == ("python", "--version")
        assert calls.count(("python", "--version")) == 1
        assert not home.exists()

    def test_bridge_python_below_minimum_stops_before_diagnosis(self, sync: ModuleType, tmp_path: Path) -> None:
        """Prevent a green sync when the MCP launcher cannot run Bridge.

        The launcher reports Python 3.9.19 against an otherwise complete fixture. Sync must fail naming that version and
        never reach the Bridge static diagnosis, since Bridge cannot start under that interpreter.
        """
        root = _marketplace_fixture(tmp_path)
        calls: Calls = []
        run = _fake_runner(root, calls, python_probe=("Python 3.9.19\n", ""))

        with pytest.raises(sync.SyncError, match=r"found Python 3.9.19"):
            _run_sync(sync, ["--no-codex-global-agents"], run)

        assert not any(call[-2:] == ("--direction", "claude") for call in calls)
        assert calls == [("python", "--version")]


class TestBridgeDiagnosis:
    """Sync verifies Bridge prerequisites after install without any paid model call."""

    def test_runs_free_static_diagnosis(self, sync: ModuleType, tmp_path: Path) -> None:
        """Verify machine-wide Bridge prerequisites without paid model inference.

        A healthy fixture is synced with global instructions skipped. After install sync must run the packaged Bridge
        doctor in static `--direction claude` mode and print that no provider call was made.
        """
        root = _marketplace_fixture(tmp_path)
        doctor = root / "plugins" / "bridge_cc-codex" / "bin" / "bridge_diagnose.py"
        calls: Calls = []
        output = io.StringIO()

        result = _run_sync(sync, ["--no-codex-global-agents"], _fake_runner(root, calls), stdout=output)

        assert result == 0
        assert ("python", "--version") in calls
        assert ("python", str(doctor), "--direction", "claude") in calls
        assert "Bridge static diagnosis passed; no provider call made" in output.getvalue()

    def test_failed_diagnosis_is_reported(self, sync: ModuleType, tmp_path: Path) -> None:
        """Keep missing Claude CLI compatibility visible after installation.

        The Bridge doctor exits 2 with a failing Claude finding after an otherwise clean install. Sync must raise
        "Bridge static diagnosis failed" instead of reporting success with a plugin that cannot reach Claude.
        """
        root = _marketplace_fixture(tmp_path)
        calls: Calls = []
        failure = {"ok": False, "live": False, "findings": [{"target": "claude", "ok": False}]}
        run = _intercept(
            _fake_runner(root, calls),
            calls,
            lambda command: command[-2:] == ["--direction", "claude"],
            _fixed(2, json.dumps(failure)),
        )

        with pytest.raises(sync.SyncError, match="Bridge static diagnosis failed"):
            _run_sync(sync, ["--no-codex-global-agents"], run)

        assert any(call[-2:] == ("--direction", "claude") for call in calls)


class TestRefreshAndInstall:
    """A normal sync refreshes the marketplace, reinstalls each managed plugin, and installs instructions."""

    def test_refreshes_marketplace_then_reinstalls_each_plugin(self, sync: ModuleType, tmp_path: Path) -> None:
        """Refresh and clean-install the latest plugins without shell interpolation.

        The marketplace is upgraded first, then each managed plugin is removed and added again, in that order, so a
        stale cached copy can never survive a sync.
        """
        root = _marketplace_fixture(tmp_path)
        calls: Calls = []
        output = io.StringIO()
        upgrade = ("codex", "plugin", "marketplace", "upgrade", "borda-ai-rig")

        result = _run_sync(
            sync, [], _fake_runner(root, calls), environ={"CODEX_HOME": str(tmp_path / "home")}, stdout=output
        )

        assert result == 0
        assert calls[0] == ("python", "--version")
        assert calls.count(("python", "--version")) == 1
        assert _plugin_calls("add") <= set(calls)
        assert all(
            calls.index(upgrade)
            < calls.index(("codex", "plugin", "remove", plugin_id))
            < calls.index(("codex", "plugin", "add", plugin_id))
            for _display_name, plugin_id in sync.MANAGED_PLUGINS
        )
        assert "Codex Rig 0.3.0 installed" in output.getvalue()
        assert "Codemap 0.28.8 installed" in output.getvalue()
        assert "Claude Code and Codex Bridge 0.1.0 installed" in output.getvalue()

    def test_retries_failed_marketplace_refresh_until_it_succeeds(self, sync: ModuleType, tmp_path: Path) -> None:
        """A refresh that times out twice and then completes still finishes the sync.

        Codex clones the marketplace with a fixed 30 s limit, so a slow link can lose an attempt that the next one wins.
        The sync must pause between attempts and carry on into the plugin reinstall.
        """
        root = _marketplace_fixture(tmp_path)
        calls: Calls = []
        sleeps: list[float] = []
        output = io.StringIO()
        refreshes = iter([(1, "clone timed out after 30s"), (1, "early EOF"), (0, "")])

        def respond(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            """Fail the first two refreshes, then succeed."""
            returncode, stderr = next(refreshes)
            return subprocess.CompletedProcess(command, returncode, "", stderr)

        run = _intercept(
            _fake_runner(root, calls),
            calls,
            lambda command: command[1:4] == ["plugin", "marketplace", "upgrade"],
            respond,
        )

        result = _run_sync(sync, ["--no-codex-global-agents"], run, stdout=output, sleeps=sleeps)

        assert result == 0
        assert calls.count(("codex", "plugin", "marketplace", "upgrade", "borda-ai-rig")) == 3
        assert sleeps == [5.0, 15.0]
        assert "retrying in 5s (attempt 2 of 3)" in output.getvalue()
        assert _plugin_calls("add") <= set(calls)

    def test_stops_after_last_failed_refresh_without_touching_plugins(self, sync: ModuleType, tmp_path: Path) -> None:
        """Three failed refreshes abort the sync before any plugin is removed or added.

        With the marketplace stale, reinstalling would silently put old plugin versions back. The error must name the
        attempt count and the fixed Codex clone limit without removing installed plugins.
        """
        root = _marketplace_fixture(tmp_path)
        calls: Calls = []
        sleeps: list[float] = []
        run = _intercept(
            _fake_runner(root, calls),
            calls,
            lambda command: command[1:4] == ["plugin", "marketplace", "upgrade"],
            _fixed(1, stderr="git clone marketplace source timed out after 30s"),
        )

        with pytest.raises(sync.SyncError, match=r"after 3 attempts.*fixed 30 s limit"):
            _run_sync(sync, ["--no-codex-global-agents"], run, sleeps=sleeps)

        assert sleeps == [5.0, 15.0]
        assert not any(call[1:3] in {("plugin", "remove"), ("plugin", "add")} for call in calls)

    def test_installs_global_instructions_and_rules(self, sync: ModuleType, tmp_path: Path) -> None:
        """Install the global instruction block and the GitHub-read rules from the selected payload.

        Both helpers run from the marketplace checkout; the rules helper is pointed at the installed cache root.
        """
        root = _marketplace_fixture(tmp_path)
        calls: Calls = []
        home = tmp_path / "home"
        installer = root / "plugins" / "codex-rig" / "scripts" / "install_global_agents.py"
        template = root / "plugins" / "codex-rig" / "assets" / "AGENTS.md"
        rules_installer = root / "plugins" / "codex-rig" / "scripts" / "install_github_read_rules.py"
        installed_root = home / "plugins" / "cache" / "borda-ai-rig" / "codex-rig" / "0.3.0"

        _run_sync(sync, [], _fake_runner(root, calls), environ={"CODEX_HOME": str(home)})

        assert any(call[1:] == (str(installer), "--source", str(template), "--codex-home", str(home)) for call in calls)
        assert any(
            call[1:] == (str(rules_installer), "--plugin-root", str(installed_root), "--codex-home", str(home))
            for call in calls
        )

    def test_replaces_local_marketplace_with_git_source(self, sync: ModuleType, tmp_path: Path) -> None:
        """Restore canonical refresh semantics when a local marketplace is configured.

        The registered marketplace has a `local` source type. Sync must remove it, re-add it from the Git source, skip
        the in-place upgrade, and remove each plugin only after the marketplace itself was removed.
        """
        root = _marketplace_fixture(tmp_path)
        calls: Calls = []
        output = io.StringIO()
        removal = ("codex", "plugin", "marketplace", "remove", "borda-ai-rig")

        result = _run_sync(
            sync, ["--no-codex-global-agents"], _fake_runner(root, calls, source_type="local"), stdout=output
        )

        assert result == 0
        assert ("codex", "plugin", "marketplace", "upgrade", "borda-ai-rig") not in calls
        assert ("codex", "plugin", "marketplace", "add", "Borda/AI-Rig", *_SPARSE_ARGS) in calls
        assert all(
            calls.index(removal) < calls.index(("codex", "plugin", "remove", plugin_id))
            for _display_name, plugin_id in sync.MANAGED_PLUGINS
        )
        assert _plugin_calls("add") <= set(calls)
        assert "marketplace re-registered from Git source" in output.getvalue()
        assert "[skip] marketplace refresh" not in output.getvalue()

    @pytest.mark.parametrize(
        "sparse_paths",
        [pytest.param((), id="full-clone"), pytest.param(("plugins/codex-rig",), id="incomplete-sparse")],
    )
    def test_migrates_existing_checkout_to_filtered_clone(
        self, sync: ModuleType, tmp_path: Path, sparse_paths: tuple[str, ...]
    ) -> None:
        """Replace full or incomplete checkouts with every managed plugin before reinstalling."""
        root = _marketplace_fixture(tmp_path)
        calls: Calls = []
        run = _fake_runner(root, calls, sparse_paths=sparse_paths)
        addition = ("codex", "plugin", "marketplace", "add", "Borda/AI-Rig", *_SPARSE_ARGS)

        assert _run_sync(sync, ["--no-codex-global-agents"], run) == 0

        assert calls.index(("codex", "plugin", "marketplace", "remove", "borda-ai-rig")) < calls.index(addition)
        assert calls.index(addition) < calls.index(("codex", "plugin", "remove", _PLUGIN_IDS[0]))
        assert ("codex", "plugin", "marketplace", "upgrade", "borda-ai-rig") not in calls

    def test_failed_sparse_registration_skips_plugin_reinstall(self, sync: ModuleType, tmp_path: Path) -> None:
        """Skip plugin reinstall on registration failure; cached plugins await successful registration."""
        root = _marketplace_fixture(tmp_path)
        calls: Calls = []
        run = _intercept(
            _fake_runner(root, calls, sparse_paths=()),
            calls,
            lambda command: command[1:4] == ["plugin", "marketplace", "add"],
            _fixed(1, stderr="clone failed"),
        )

        with pytest.raises(sync.SyncError, match="clone failed"):
            _run_sync(sync, ["--no-codex-global-agents"], run)

        assert not any(call[1:3] in {("plugin", "remove"), ("plugin", "add")} for call in calls)

    def test_no_clean_keeps_plugins_but_still_refreshes_marketplace(self, sync: ModuleType, tmp_path: Path) -> None:
        """Keep no-clean limited to plugin removal rather than marketplace freshness.

        A local-source marketplace is synced with `--no-clean`. No plugin is removed, yet the marketplace is still re-
        registered from Git and plugins are added, so skipping the clean step never leaves a stale local source.
        """
        root = _marketplace_fixture(tmp_path)
        calls: Calls = []

        result = _run_sync(
            sync, ["--no-clean", "--no-codex-global-agents"], _fake_runner(root, calls, source_type="local")
        )

        assert result == 0
        assert not any(call[1:3] == ("plugin", "remove") for call in calls)
        assert ("codex", "plugin", "marketplace", "remove", "borda-ai-rig") in calls
        assert ("codex", "plugin", "marketplace", "add", "Borda/AI-Rig", *_SPARSE_ARGS) in calls
        assert ("codex", "plugin", "add", "codex-rig@borda-ai-rig") in calls

    def test_requires_every_managed_plugin_after_install(self, sync: ModuleType, tmp_path: Path) -> None:
        """Reject a partial restore that enables Codex Rig without Codemap.

        After install the marketplace lists only the first managed plugin. Sync must fail naming Codemap as not uniquely
        enabled, because the three managed plugins are meant to be installed together.
        """
        root = _marketplace_fixture(tmp_path)
        run = _fake_runner(root, [], installed=_ALL_INSTALLED[:1])

        with pytest.raises(sync.SyncError, match="Codemap is not uniquely enabled"):
            _run_sync(sync, ["--no-codex-global-agents"], run)


class TestPinnedRef:
    """A pinned `--codex-ref` is verified against the selected payload before anything is removed."""

    def test_adds_marketplace_at_ref_when_absent(self, sync: ModuleType, tmp_path: Path) -> None:
        """Forward one explicit ref without relying on shell quoting.

        No marketplace is registered yet and `--codex-ref codex-rig-v0.3.0` is given. Sync must add the marketplace with
        that ref as separate arguments and, with global instructions skipped, never call the instruction installer.
        """
        root = _marketplace_fixture(tmp_path)
        calls: Calls = []

        result = _run_sync(
            sync,
            ["--codex-ref", "codex-rig-v0.3.0", "--no-codex-global-agents"],
            _fake_runner(root, calls, first_listing_empty=True),
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
            *_SPARSE_ARGS,
        ) in calls
        assert not any("install_global_agents.py" in item for call in calls for item in call)

    def test_rejects_ref_mismatch_without_changes(self, sync: ModuleType, tmp_path: Path) -> None:
        """Fail before refresh when the configured marketplace tracks another ref.

        The marketplace is configured at `codex-rig-v0.2.4` while `--codex-ref codex-rig-v0.3.0` is requested. Sync must
        raise "marketplace tracks" and run neither an upgrade nor any plugin add, so nothing changes silently.
        """
        root = _marketplace_fixture(tmp_path)
        calls: Calls = []
        run = _fake_runner(root, calls, configured_ref="codex-rig-v0.2.4")

        with pytest.raises(sync.SyncError, match="marketplace tracks"):
            _run_sync(sync, ["--codex-ref", "codex-rig-v0.3.0"], run)

        assert not any(
            call[1:4] == ("plugin", "marketplace", "upgrade") or call[1:3] == ("plugin", "add") for call in calls
        )

    def test_rejects_unavailable_rules_helper_before_plugin_removal(self, sync: ModuleType, tmp_path: Path) -> None:
        """Keep a newly selected unsupported revision from removing managed plugins.

        The marketplace is absent until the pinned ref is registered; only then does verification find the rules helper
        missing from the payload.
        """
        root = _marketplace_fixture(tmp_path)
        (root / "plugins" / "codex-rig" / "scripts" / "install_github_read_rules.py").unlink()
        calls: Calls = []
        run = _fake_runner(root, calls, first_listing_empty=True)

        with pytest.raises(sync.SyncError, match="selected Codex Rig package failed verification"):
            _run_sync(sync, ["--codex-ref", "codex-rig-v0.2.4", "--no-codex-global-agents"], run)

        assert (
            "codex",
            "plugin",
            "marketplace",
            "add",
            "Borda/AI-Rig",
            "--ref",
            "codex-rig-v0.2.4",
            *_SPARSE_ARGS,
        ) in calls
        assert not any(call[1:3] in {("plugin", "remove"), ("plugin", "add")} for call in calls)

    @pytest.mark.parametrize(
        "corrupt",
        [
            pytest.param(
                lambda plugin: (plugin / "package-manifest.json").write_text("{", encoding="utf-8"),
                id="malformed-manifest",
            ),
            pytest.param(
                lambda plugin: (plugin / "shared" / "github_read.py").write_text(
                    '"""Modified fixture reader."""\n', encoding="utf-8"
                ),
                id="hash-mismatch",
            ),
            pytest.param(
                lambda plugin: (plugin / "scripts" / "_safe_package_io.py").unlink(), id="missing-package-dependency"
            ),
            pytest.param(
                lambda plugin: (plugin / "scripts" / "install_github_read_rules.py").unlink(), id="missing-rules-helper"
            ),
        ],
    )
    def test_rejects_invalid_payload_before_changes(
        self, sync: ModuleType, tmp_path: Path, corrupt: Callable[[Path], object]
    ) -> None:
        """Keep a corrupt or unsupported selected package from mutating the configured plugin state.

        The pinned payload is damaged in one of four ways: malformed manifest, hash mismatch, a missing package
        dependency, or a missing rules helper. Sync must fail verification before any upgrade, removal, or add runs.
        """
        root = _marketplace_fixture(tmp_path)
        corrupt(root / "plugins" / "codex-rig")
        calls: Calls = []
        run = _fake_runner(root, calls, configured_ref="codex-rig-v0.2.4")

        with pytest.raises(sync.SyncError, match="selected Codex Rig package failed verification"):
            _run_sync(sync, ["--codex-ref", "codex-rig-v0.2.4", "--no-codex-global-agents"], run)

        assert _changes_installation(calls) == []


class TestRulesHelperStatus:
    """The GitHub-read rules helper's output and failures reach the caller."""

    @pytest.mark.parametrize(
        "arguments",
        [
            pytest.param(["install", "--no-codex-global-agents"], id="install"),
            pytest.param(["clear"], id="clear"),
        ],
    )
    def test_preserves_status_before_partial_failure(
        self, sync: ModuleType, tmp_path: Path, arguments: list[str]
    ) -> None:
        """Show completed rule changes when the helper reports a later partial failure.

        The rules helper exits 2 after printing "rules created" and a partial-failure code on stderr, for install and
        for clear. Sync must raise with that code and still have written the helper's stdout, so work done stays
        visible.
        """
        root = _marketplace_fixture(tmp_path)
        calls: Calls = []
        run = _intercept(
            _fake_runner(root, calls),
            calls,
            _is_rules_helper,
            _fixed(2, "rules created\n", "github-read-rules-partial\n"),
        )
        output = io.StringIO()

        with pytest.raises(sync.SyncError, match="github-read-rules-partial"):
            _run_sync(sync, arguments, run, stdout=output)

        assert "rules created" in output.getvalue()

    @pytest.mark.integration
    def test_executes_profile_setup_and_clear_in_isolated_home(self, sync: ModuleType, tmp_path: Path) -> None:
        """Exercise the real profile lifecycle while replacing marketplace commands.

        The rules helper runs for real against an isolated home: install writes the managed profile, clear removes it.
        Skipping global instructions must not skip the separately managed profile.
        """
        root = _marketplace_fixture(tmp_path)
        scripts = root / "plugins" / "codex-rig" / "scripts"
        _copy_packaged_scripts(scripts, "install_github_read_rules.py", "_package_identity.py", "_safe_package_io.py")
        _write_fixture_package_manifest(scripts.parent, "0.3.0")
        home = tmp_path / "home"
        installed = home / "plugins" / "cache" / "borda-ai-rig" / "codex-rig" / "0.3.0"
        (installed / ".codex-plugin").mkdir(parents=True)
        (installed / ".codex-plugin" / "plugin.json").write_text(
            json.dumps({"name": "codex-rig", "version": "0.3.0"}), encoding="utf-8"
        )
        (installed / "shared").mkdir()
        (installed / "shared" / "github_read.py").write_text('"""Fixture reader."""\n', encoding="utf-8")
        (installed / "shared" / "collect_pr.py").write_text('"""Fixture collector."""\n', encoding="utf-8")
        _write_fixture_package_manifest(installed, "0.3.0")
        calls: Calls = []
        run = _intercept(
            _fake_runner(root, calls),
            calls,
            _is_rules_helper,
            lambda command, **kwargs: subprocess.run(command, **kwargs),
        )
        environ = {"CODEX_HOME": str(home)}

        installed_status = _run_sync(sync, ["install", "--no-codex-global-agents"], run, environ=environ)
        payload = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
        has_rules_file = (home / "rules" / "codex-rig-github-read.rules").exists()
        cleared_status = _run_sync(sync, ["clear"], run, environ=environ)

        assert installed_status == 0
        assert payload["default_permissions"] == "local-workflow"
        assert payload["permissions"]["github-read"]["extends"] == ":workspace"
        assert payload["features"]["network_proxy"] is True
        assert not has_rules_file
        assert cleared_status == 0
        assert not (home / "config.toml").exists()


class TestClear:
    """`clear` removes plugins and the managed instruction block through the same entrypoint."""

    def test_removes_plugins_instructions_and_rules(self, sync: ModuleType, tmp_path: Path) -> None:
        """Route teardown through the same portable Python entrypoint.

        The rules are removed before Codex Rig itself, so the profile is never left pointing at a plugin that is gone.
        """
        calls: Calls = []
        home = tmp_path / "home"
        run = _intercept(_forbidden_run, calls, lambda command: True, _fixed(0))

        result = _run_sync(sync, ["clear"], run, environ={"CODEX_HOME": str(home)})

        rules_removal = next(call for call in calls if any("install_github_read_rules.py" in item for item in call))
        assert result == 0
        assert _plugin_calls("remove") <= set(calls)
        assert _removes_global_agents(calls, home)
        assert rules_removal[2:] == ("--remove", "--codex-home", str(home))
        assert calls.index(rules_removal) < calls.index(("codex", "plugin", "remove", "codex-rig@borda-ai-rig"))

    def test_removes_managed_block_when_codex_is_missing(self, sync: ModuleType, tmp_path: Path) -> None:
        """Keep local teardown usable after the Codex executable was removed.

        Every `codex` command raises `FileNotFoundError` while local helper commands succeed. `clear` must still exit 0,
        say Codex is not installed, remove the managed global block, and attempt each plugin removal.
        """
        calls: Calls = []
        home = tmp_path / "home"

        def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            """Model a missing Codex executable while allowing local teardown to succeed."""
            calls.append(tuple(str(item) for item in command))
            if command[0] == "codex":
                raise FileNotFoundError("codex")
            return subprocess.CompletedProcess(command, 0, "managed block removed\n", "")

        output = io.StringIO()

        result = _run_sync(sync, ["clear"], run, environ={"CODEX_HOME": str(home)}, stdout=output)

        assert result == 0
        assert "not installed" in output.getvalue()
        assert "managed block removed" in output.getvalue()
        assert _plugin_calls("remove") <= set(calls)
        assert _removes_global_agents(calls, home)


class TestWindowsBatchLauncher:
    """A Codex `.cmd` shim is launched through the Windows command processor without shell injection."""

    def test_resolves_batch_launcher(self, sync: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        """Launch a resolved Codex batch shim through the Windows command processor.

        `which` resolves `codex` to a `.cmd` path containing a space. The command must come back as one quoted string
        flagged for shell execution, since a batch file cannot be started directly by Windows process creation.
        """
        monkeypatch.setattr(
            sync.shutil, "which", lambda command: {"codex": r"C:\Program Files\Codex\codex.cmd"}.get(command)
        )

        resolved, shell = sync._resolve_system_command(["codex", "plugin", "list"], windows=True)

        assert resolved == r'"C:\Program Files\Codex\codex.cmd" plugin list'
        assert shell is True

    @pytest.mark.parametrize(
        "argument",
        [
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
        ],
    )
    def test_rejects_shell_syntax_in_arguments(
        self, sync: ModuleType, monkeypatch: pytest.MonkeyPatch, argument: str
    ) -> None:
        """Reject user-controlled values that cmd.exe could reinterpret.

        Each argument carries a command separator, redirection, escape, expansion, quote, or space. Resolving a batch
        shim with it must raise "unsafe Windows batch command", because the shell would otherwise run injected text.
        """
        monkeypatch.setattr(sync.shutil, "which", lambda _command: r"C:\fixture\codex.cmd")

        with pytest.raises(OSError, match="unsafe Windows batch command"):
            sync._resolve_system_command(["codex", "plugin", "add", argument], windows=True)


class TestInstructionPreflight:
    """The global-instruction health check gates plugin changes and re-checks after install."""

    def test_degraded_composition_after_install_is_reported(self, sync: ModuleType, tmp_path: Path) -> None:
        """Package installation success cannot conceal a degraded active instruction check.

        The instruction health check reports a legacy skill route after plugins were installed. Sync must raise with
        that diagnostic and withhold the "Start a fresh Codex session" success hint.
        """
        root = _marketplace_fixture(tmp_path)
        calls: Calls = []
        degraded = _fixed(4, "", "global-agents-check: legacy-skill-route:develop")
        run = _intercept(_fake_runner(root, calls), calls, _is_instruction_check, degraded)
        output = io.StringIO()

        with pytest.raises(sync.SyncError, match="legacy-skill-route:develop"):
            _run_sync(sync, [], run, environ={"CODEX_HOME": str(tmp_path / "home")}, stdout=output)

        assert "Start a fresh Codex session" not in output.getvalue()

    @pytest.mark.parametrize(
        ("status", "diagnostic"),
        [
            pytest.param(4, "global-agents-check: global-agents-overlap", id="overlapping-policy"),
            pytest.param(4, "global-agents-check: invalid-managed-instructions", id="invalid-managed-block"),
            pytest.param(4, "global-agents-check: legacy-skill-route:develop", id="legacy-route"),
            pytest.param(4, "unexpected diagnostic", id="malformed-diagnostic"),
            pytest.param(4, "", id="missing-diagnostic"),
            pytest.param(2, "global-agents-check: stale-managed-template", id="unexpected-exit-status"),
            pytest.param(0, "global-agents-check: global-agents-overlap", id="contradictory-success"),
            pytest.param(
                4,
                "global-agents-check: stale-managed-template\nglobal-agents-check: global-agents-overlap",
                id="stale-and-overlapping",
            ),
        ],
    )
    def test_blocked_composition_stops_plugin_and_profile_mutation(
        self, sync: ModuleType, tmp_path: Path, status: int, diagnostic: str
    ) -> None:
        """Known blocked instruction composition must stop before plugin changes or profile writes.

        The first health check returns an overlap, invalid block, legacy route, malformed or missing diagnostic, an
        unexpected exit status, or a contradictory success. Sync must raise and run no plugin change, no rules helper,
        and no installer beyond the read-only check.
        """
        root = _marketplace_fixture(tmp_path)
        calls: Calls = []
        run = _intercept(_fake_runner(root, calls), calls, _is_instruction_check, _fixed(status, "", diagnostic))

        with pytest.raises(sync.SyncError):
            _run_sync(sync, [], run, environ={"CODEX_HOME": str(tmp_path / "home")})

        assert not any(call[:3] in {("codex", "plugin", "remove"), ("codex", "plugin", "add")} for call in calls)
        assert not any(len(call) > 1 and Path(call[1]).name == "install_github_read_rules.py" for call in calls)
        assert not any(
            len(call) > 1 and Path(call[1]).name == "install_global_agents.py" and "--check" not in call
            for call in calls
        )

    @pytest.mark.integration
    def test_real_overlap_check_runs_before_plugin_changes(self, sync: ModuleType, tmp_path: Path) -> None:
        """Run the real bounded installer check while keeping marketplace and plugin commands external fakes.

        The home holds unmanaged custom policy; the real check must flag the overlap, leave the file byte-identical, and
        create no backup.
        """
        root = _marketplace_fixture(tmp_path)
        plugin = root / "plugins" / "codex-rig"
        (plugin / "assets/AGENTS.md").write_bytes(b"# Global Agent Instructions\nCurrent managed policy.\n")
        _write_fixture_package_manifest(plugin, "0.3.0")
        home = tmp_path / "home"
        home.mkdir()
        original = b"# Global Agent Instructions\nUnmanaged custom policy.\n"
        (home / "AGENTS.md").write_bytes(original)
        calls: Calls = []
        run = _intercept(_fake_runner(root, calls), calls, _is_instruction_check, _run_real_instruction_installer)

        with pytest.raises(sync.SyncError, match="global-agents-overlap"):
            _run_sync(sync, [], run, environ={"CODEX_HOME": str(home)})

        assert not any(call[:3] in {("codex", "plugin", "remove"), ("codex", "plugin", "add")} for call in calls)
        assert not any(len(call) > 1 and Path(call[1]).name == "install_github_read_rules.py" for call in calls)
        assert (home / "AGENTS.md").read_bytes() == original
        assert not (home / "backups").exists()

    @staticmethod
    def _stale_then_final(root: Path, calls: Calls, final_status: int) -> tuple[Runner, list[list[str]]]:
        """Return a runner whose first health check reports an upgradeable stale template, then `final_status`."""
        checks: list[list[str]] = []

        def respond(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            """Report only the upgradeable preflight mismatch first, then the actual installed health."""
            checks.append(command)
            first = len(checks) == 1
            stale = "global-agents-check: stale-managed-template" if first or final_status else ""
            return subprocess.CompletedProcess(command, 4 if first else final_status, "", stale)

        return _intercept(_fake_runner(root, calls), calls, _is_instruction_check, respond), checks

    @staticmethod
    def _checks_bracket_install(calls: Calls) -> tuple[bool, bool]:
        """Report whether the first check precedes the first plugin removal and the last check follows the install."""
        checks = [i for i, call in enumerate(calls) if _is_instruction_check(list(call))]
        removal = next(i for i, call in enumerate(calls) if call[:3] == ("codex", "plugin", "remove"))
        install = next(
            i for i, call in enumerate(calls) if _is_instruction_installer(list(call)) and "--check" not in call
        )
        return checks[0] < removal, checks[-1] > install

    def test_stale_template_is_accepted_when_final_health_passes(self, sync: ModuleType, tmp_path: Path) -> None:
        """A normal template upgrade may proceed, and sync succeeds when the final instruction health passes.

        The first check reports an upgradeable stale template and the second passes. Sync must continue, run exactly two
        checks, with the first before plugin removal and the last after the instruction install.
        """
        calls: Calls = []
        run, checks = self._stale_then_final(_marketplace_fixture(tmp_path), calls, final_status=0)

        result = _run_sync(sync, [], run, environ={"CODEX_HOME": str(tmp_path / "home")})

        assert result == 0
        assert len(checks) == 2
        assert self._checks_bracket_install(calls) == (True, True)

    def test_stale_template_is_rejected_when_final_health_fails(self, sync: ModuleType, tmp_path: Path) -> None:
        """A normal template upgrade may proceed, but final instruction health remains mandatory.

        The first check reports a stale template and the post-install check still reports it. Sync must raise with the
        stale-template diagnostic after both checks ran, so an upgrade that did not take cannot pass as success.
        """
        calls: Calls = []
        run, checks = self._stale_then_final(_marketplace_fixture(tmp_path), calls, final_status=4)

        with pytest.raises(sync.SyncError, match="stale-managed-template"):
            _run_sync(sync, [], run, environ={"CODEX_HOME": str(tmp_path / "home")})

        assert len(checks) == 2
        assert self._checks_bracket_install(calls) == (True, True)


def _sync_with_real_instruction_installer(
    sync: ModuleType, tmp_path: Path, template: bytes
) -> tuple[Calls, Path, Path, io.StringIO]:
    """Run sync with fake Codex commands but the real global-instruction installer and an installed cache layout."""
    root = _marketplace_fixture(tmp_path)
    plugin = root / "plugins" / "codex-rig"
    (plugin / "assets" / "AGENTS.md").write_bytes(template)
    _write_fixture_package_manifest(plugin, "0.3.0")
    home = tmp_path / "home"
    installed = home / "plugins" / "cache" / "borda-ai-rig" / "codex-rig" / "0.3.0"
    shutil.copytree(PLUGIN_ROOT / "shared", installed / "shared", ignore=shutil.ignore_patterns("__pycache__"))
    calls: Calls = []
    run = _intercept(_fake_runner(root, calls), calls, _is_instruction_installer, _run_real_instruction_installer)
    output = io.StringIO()
    assert _run_sync(sync, [], run, environ={"CODEX_HOME": str(home)}, stdout=output) == 0
    return calls, home, installed, output


@pytest.mark.integration
def test_sync_writes_installed_root_so_global_pointers_resolve(sync: ModuleType, tmp_path: Path) -> None:
    """Sync renders the installed cache root into the global block, not the marketplace checkout it read it from.

    The template is read from the marketplace checkout, but a session in any project must open the shared docs of the
    installed cache. Sync's own post-install health check must also accept the block it just wrote.
    """
    calls, home, installed, output = _sync_with_real_instruction_installer(
        sync, tmp_path, (PLUGIN_ROOT / "assets" / "AGENTS.md").read_bytes()
    )

    body = (home / "AGENTS.md").read_text(encoding="utf-8")
    assert re.findall(r"^`PLUGIN_ROOT` = `([^`]+)`", body, re.MULTILINE) == [str(installed)]
    pointers = set(re.findall(r"`(shared/[\w.-]+\.(?:md|py))`", body))
    assert "shared/global-baseline-details.md" in pointers
    assert sorted(pointer for pointer in pointers if not (installed / pointer).is_file()) == []
    installer_calls = [call for call in calls if Path(call[1]).name == "install_global_agents.py"]
    assert [call[-2:] == ("--plugin-root", str(installed)) for call in installer_calls] == [False, True, True]
    assert "global instructions created" in output.getvalue()


@pytest.mark.integration
def test_sync_calls_older_payload_installer_without_plugin_root(sync: ModuleType, tmp_path: Path) -> None:
    """A selected payload whose template predates the root placeholder gets the installer arguments it understands.

    Repository sync can run against an older marketplace ref whose installer rejects unknown ``--plugin-root``.
    """
    template = b"# Global Agent Instructions\nOlder managed policy.\n"

    calls, home, _installed, _output = _sync_with_real_instruction_installer(sync, tmp_path, template)

    assert template in (home / "AGENTS.md").read_bytes()
    installer_calls = [call for call in calls if Path(call[1]).name == "install_global_agents.py"]
    assert len(installer_calls) == 3
