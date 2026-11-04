"""Behavioral acceptance checks for the root Makefile's Claude- and Codex-side sync targets.

These exercise the actual `make <target>` invocation against stubbed CLIs and scratch registries — never the real
`claude`/`codex` CLIs or the real `$HOME` — so they can run safely in CI without touching a developer's or runner's real
plugin state. The real `make` run is the contract here (recipes, quoting, exit codes), so only the tools the recipes
call are stubbed.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import runpy
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = ROOT / "Makefile"
CONSENT_SCRIPT = ROOT / "plugins" / "bridge_cc-codex" / "bin" / "bridge_consent.py"
CONSENT_SKIP_NOTICE = "consent unchanged: needs operator terminal or BRIDGE_OPERATOR_TOKEN"
CONSENT_PROOF_ERROR = "APPROVE_CLAUDE_CONSENT=1 needs operator proof"
ORIGINAL_CONFIG = 'model = "retained"\n'


class FakeClaude(NamedTuple):
    """A stub `claude` CLI on disk, plus the log file it appends every invocation to."""

    bin_dir: Path
    log: Path


class FakeScript(NamedTuple):
    """A stub Python script on disk, plus the log file it writes its argv to."""

    path: Path
    log: Path


def _gnu_make() -> str | None:
    """Return the `make` binary path if it resolves to GNU make, else None."""
    make = shutil.which("make")
    if make is None:
        return None
    result = subprocess.run([make, "--version"], capture_output=True, text=True, encoding="utf-8", check=False)
    if "GNU Make" not in result.stdout:
        return None
    return make


GNU_MAKE = _gnu_make()
BASH = shutil.which("bash")
JQ = shutil.which("jq")
GIT = shutil.which("git")
_requires_gnu_make = pytest.mark.skipif(GNU_MAKE is None, reason="GNU make is not available on this host")
_requires_gnu_make_and_jq = pytest.mark.skipif(
    GNU_MAKE is None or JQ is None, reason="GNU make and jq are required on this host"
)
_requires_bash = pytest.mark.skipif(BASH is None, reason="Bash is required for the Makefile recipes")


@pytest.fixture(autouse=True)
def available_rtk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Give ordinary sync cases the required executable without depending on a host installation."""
    bin_dir = tmp_path / "rtk-bin"
    bin_dir.mkdir()
    rtk = bin_dir / "rtk"
    rtk.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    rtk.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join((str(bin_dir), os.environ.get("PATH", ""))))


def _run_make(
    target: str, *, env: dict[str, str], extra_vars: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Invoke one Makefile target with the given environment and Make-variable overrides."""
    args = [GNU_MAKE, "-f", str(MAKEFILE), target]
    for key, value in (extra_vars or {}).items():
        args.append(f"{key}={value}")
    return subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", check=False)


def _write_argv_logger(path: Path, log: Path) -> FakeScript:
    """Write a stub script that records its argv to ``log`` when run; neither is executed or created here."""
    path.write_text(
        f"import sys\nfrom pathlib import Path\nPath({str(log)!r}).write_text(' '.join(sys.argv[1:]), encoding='utf-8')\n",
        encoding="utf-8",
    )
    return FakeScript(path=path, log=log)


@pytest.fixture(name="fake_claude")
def _fake_claude(tmp_path: Path) -> FakeClaude:
    """Write a Claude CLI stub and empty invocation log without executing the stub."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "claude-invocations.log"
    log.write_text("", encoding="utf-8")
    script = bin_dir / "claude"
    script.write_text(
        "#!/usr/bin/env bash\n"
        'echo "claude $*" >> "$CLAUDE_STUB_LOG"\n'
        'if [[ "$1" == "--print" ]]; then echo "setup-root=$CLAUDE_PLUGIN_ROOT" >> "$CLAUDE_STUB_LOG"; fi\n'
        'if [[ "$CLAUDE_STUB_UTF8_STDOUT" == "true" ]]; then printf "\\340\\240\\235\\n"; fi\n'
        'if [[ "$1 $2" == "plugin install" && "$3" == bridge@* && "$FAIL_BRIDGE" == "true" ]]; then\n'
        "    exit 1\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return FakeClaude(bin_dir=bin_dir, log=log)


@pytest.fixture(name="sandbox_home")
def _sandbox_home(tmp_path: Path) -> Path:
    """Create an isolated home with empty plugin registries, leaving the actual user home untouched."""
    home = tmp_path / "home"
    plugins_dir = home / ".claude" / "plugins"
    (plugins_dir / "cache").mkdir(parents=True)
    (plugins_dir / "installed_plugins.json").write_text('{"plugins": {}}', encoding="utf-8")
    (plugins_dir / "known_marketplaces.json").write_text("{}", encoding="utf-8")
    (plugins_dir / "settings.json").write_text("{}", encoding="utf-8")
    return home


@pytest.fixture(name="fake_codex_sync_script")
def _fake_codex_sync_script(tmp_path: Path) -> FakeScript:
    """Write an argument-recording sync stub without executing it or creating its future log."""
    return _write_argv_logger(tmp_path / "fake_sync_codex.py", tmp_path / "codex-sync.log")


@pytest.fixture(name="fake_codex_home_sync_script")
def _fake_codex_home_sync_script(tmp_path: Path) -> FakeScript:
    """Write a session-policy sync stub whose future log stays inside the fixture directory."""
    return _write_argv_logger(tmp_path / "fake_sync_codex_session_policy.py", tmp_path / "codex-home-sync.log")


def _claude_env(fake_claude: FakeClaude, home: Path, **extra: str) -> dict[str, str]:
    """Return the environment that puts the Claude stub first on PATH and confines HOME to the sandbox."""
    env = os.environ.copy()
    env.update(
        PATH=f"{fake_claude.bin_dir}{os.pathsep}{env['PATH']}", HOME=str(home), CLAUDE_STUB_LOG=str(fake_claude.log)
    )
    env.update(extra)
    return env


@_requires_gnu_make
@pytest.mark.integration
class TestSyncRtkPreflight:
    """Every sync entrypoint requires RTK before any mutation, including GNU make's keep-going mode."""

    _STAGES = (
        "migrate-marketplace uninstall-claude-plugins refresh-ext-marketplace update-ext-plugins "
        "register-marketplace install-claude-plugins verify-claude-plugins prune-claude-cache "
        "warn-stale-claude-sessions install-codex-plugins sync-codex-home-policy sync-codex-bridge-consent "
        "register-opencode-rig install-opencode-agents setup-opencode"
    )

    def _run_sync(self, tmp_path: Path, target: str, keep_going: bool, rtk_available: bool):
        """Run a sync entrypoint with harmless stage recipes and an RTK lookup path, with or without ``rtk``."""
        bin_dir = tmp_path / "isolated-bin"
        bin_dir.mkdir()
        if rtk_available:
            rtk = bin_dir / "rtk"
            # Preflight only discovers this sentinel; tests must never need a working RTK executable.
            rtk.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
            rtk.chmod(0o755)
        # Isolate RTK lookup only: native make needs the host toolchain for direct printf/echo execution.
        # Retain real recursive sync recipes and require their harmless stage replacements to retain Bash discovery.
        (tmp_path / "Makefile").write_text(
            MAKEFILE.read_text(encoding="utf-8")
            + f"\npreflight-sync: export PATH := {bin_dir.as_posix()}\n"
            + f"\n{self._STAGES}:\n\t@command -v bash >/dev/null && echo $@ >> stages.log\n",
            encoding="utf-8",
        )
        return subprocess.run(
            [
                GNU_MAKE,
                *(["-k"] if keep_going else []),
                *([] if target == "default" else [target]),
                # Resolve the recipe shell before preflight restricts PATH; recursive make inherits this override.
                f"SHELL={Path(BASH).as_posix()}",
                "MARKETPLACE=fixture",
                f"PROJECT_DIR={tmp_path.as_posix()}",
                "MARKETPLACE_REMOTE=unused",
            ],
            cwd=tmp_path,
            env={**os.environ, "MAKEFLAGS": "", "MAKEFILES": ""},
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

    @_requires_bash
    @pytest.mark.parametrize("keep_going", [False, True])
    @pytest.mark.parametrize("target", ["sync-all", "sync-claude", "sync-codex", "sync-opencode", "default"])
    def test_stops_before_any_stage_without_rtk(self, tmp_path: Path, target: str, keep_going: bool) -> None:
        """Without RTK the sync fails with its diagnostic before running a single stage.

        The RTK lookup path holds no ``rtk``. Neither plain nor ``-k`` make may reach a mutating stage or claim the sync
        completed.
        """
        result = self._run_sync(tmp_path, target, keep_going, rtk_available=False)

        assert result.returncode != 0, result.stdout + result.stderr
        assert not (tmp_path / "stages.log").exists()
        assert "RTK is required for sync" in result.stderr
        assert "sync complete" not in result.stdout

    @_requires_bash
    @pytest.mark.parametrize("keep_going", [False, True])
    @pytest.mark.parametrize(
        ("target", "runs_opencode"),
        [
            pytest.param("sync-all", True, id="sync-all"),
            pytest.param("sync-claude", False, id="sync-claude"),
            pytest.param("sync-codex", False, id="sync-codex"),
            pytest.param("sync-opencode", True, id="sync-opencode"),
            pytest.param("default", True, id="default"),
        ],
    )
    def test_runs_stages_with_rtk(self, tmp_path: Path, target: str, runs_opencode: bool, keep_going: bool) -> None:
        """With RTK discoverable the entrypoint runs its stages, and the OpenCode ones only where documented.

        A sentinel ``rtk`` satisfies preflight; the full-sync, OpenCode and default entrypoints must register and
        install OpenCode Rig, the Claude and Codex entrypoints must not.
        """
        result = self._run_sync(tmp_path, target, keep_going, rtk_available=True)

        stage_log = (tmp_path / "stages.log").read_text(encoding="utf-8").splitlines()
        assert result.returncode == 0, result.stdout + result.stderr
        assert ("register-opencode-rig" in stage_log) == runs_opencode
        assert ("install-opencode-agents" in stage_log) == runs_opencode
        assert ("setup-opencode" in stage_log) == runs_opencode


@_requires_gnu_make
@pytest.mark.integration
def test_run_make_decodes_utf8_output_when_parent_default_is_cp1252(
    fake_claude: FakeClaude, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Make output must remain readable when the host default decoder is cp1252.

    The real CLI stub emits U+081D as raw UTF-8 bytes, including 0x9D which is undefined in cp1252. ASCII arguments
    avoid MinGW's Unicode command-line conversion; the wrapper supplies a non-UTF-8 default only when unspecified.
    """
    real_run = subprocess.run

    def run_with_cp1252_default(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Supply the simulated host default while preserving the real subprocess call."""
        if kwargs.get("text") and kwargs.get("encoding") is None:
            kwargs["encoding"] = "cp1252"
        return real_run(*args, **kwargs)

    env = os.environ.copy()
    env["PATH"] = f"{fake_claude.bin_dir}{os.pathsep}{env['PATH']}"
    env["CLAUDE_STUB_LOG"] = str(fake_claude.log)
    env["CLAUDE_STUB_UTF8_STDOUT"] = "true"
    monkeypatch.setattr(subprocess, "run", run_with_cp1252_default)

    result = _run_make("clear-claude", env=env, extra_vars={"PLUGINS": "example"})

    assert result.returncode == 0, result.stdout + result.stderr
    assert "\u081d" in result.stdout


def _register_plugin_roots(home: Path, plugins: tuple[str, ...]) -> tuple[Path, list[str]]:
    """Install a ``setup`` skill per plugin under a path with spaces and register each in ``installed_plugins.json``.

    Returns:
        The registry path and the POSIX install roots in registration order.
    """
    registry = {}
    roots = []
    for plugin in plugins:
        root = home / ".claude" / "plugins" / "cache" / plugin / "version with spaces"
        skill = root / "skills" / "setup" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("# Setup\n", encoding="utf-8")
        roots.append(root.as_posix())
        registry[f"{plugin}@borda-ai-rig"] = [{"installPath": root.as_posix()}]
    installed = home / ".claude" / "plugins" / "installed_plugins.json"
    installed.write_text(json.dumps({"plugins": registry}), encoding="utf-8")
    return installed, roots


@_requires_gnu_make
@pytest.mark.integration
class TestInstallClaudePlugins:
    """Bridge-purge guard and try-all-then-report contract for install-claude-plugins."""

    @pytest.mark.skipif(JQ is None, reason="jq is required on this host")
    def test_setup_receives_each_installed_plugin_root(self, fake_claude: FakeClaude, sandbox_home: Path) -> None:
        """Setup uses each plugin's installed version even when the caller exports another plugin root.

        Two plugins are registered under install roots containing spaces; the environment exports an unrelated
        ``CLAUDE_PLUGIN_ROOT``. Each ``/<plugin>:setup`` run must see its own root, exactly once.
        """
        installed, roots = _register_plugin_roots(sandbox_home, ("foundry", "oss"))
        env = _claude_env(fake_claude, sandbox_home, CLAUDE_PLUGIN_ROOT="unrelated-plugin")

        result = _run_make(
            "install-claude-plugins",
            env=env,
            extra_vars={"PLUGINS": "foundry oss", "INSTALLED_PLUGINS": installed.as_posix()},
        )

        calls = fake_claude.log.read_text(encoding="utf-8").splitlines()
        assert result.returncode == 0, result.stdout + result.stderr
        assert [line for line in calls if line.startswith("setup-root=")] == [f"setup-root={root}" for root in roots]
        assert sum("/foundry:setup --approve" in line for line in calls) == 1
        assert sum("/oss:setup --approve" in line for line in calls) == 1

    @pytest.mark.parametrize(
        ("fail_bridge", "bridge_installed"),
        [
            pytest.param("false", True, id="bridge-install-succeeds"),
            pytest.param("true", False, id="bridge-install-fails"),
        ],
    )
    def test_legacy_codex_plugin_purge_follows_bridge_install(
        self, fake_claude: FakeClaude, sandbox_home: Path, fail_bridge: str, bridge_installed: bool
    ) -> None:
        """The unconditional purge always runs; the legacy Codex plugin is purged only after a bridge install succeeds.

        ``ponytail@ponytail`` is always purged; ``codex@openai-codex`` only when this run's bridge install succeeded,
        per ``plugins/codex-rig/tests/test_sync_setup_dispatch.py``'s retired contract (the Claude-side half).
        A failed bridge install also proves try-all-then-report: later plugins still install and the run exits nonzero.
        """
        env = _claude_env(fake_claude, sandbox_home, FAIL_BRIDGE=fail_bridge)
        installed_plugins = sandbox_home / ".claude" / "plugins" / "installed_plugins.json"

        result = _run_make("install-claude-plugins", env=env, extra_vars={"INSTALLED_PLUGINS": str(installed_plugins)})

        calls = fake_claude.log.read_text(encoding="utf-8")
        assert (result.returncode == 0) == bridge_installed, result.stdout + result.stderr
        assert "claude plugin uninstall ponytail@ponytail" in calls
        assert ("claude plugin uninstall codex@openai-codex" in calls) == bridge_installed
        assert "codemap-py@" in calls  # plugins after the failed one still got installed


def _write_crlf_jq_wrapper(directory: Path) -> None:
    """Write a ``jq`` wrapper that re-ends the selector query's output with ``$JQ_SELECTOR_EOL``.

    The wrapper delegates to ``$REAL_JQ``; only the output of the ``to_entries | map`` selector is re-ended, to prove
    the recipe tolerates a carriage return before the newline that Bash ``read -r`` preserves.
    """
    program = (
        "import os\n"
        "import subprocess\n"
        "import sys\n"
        "result = subprocess.run([os.environ['REAL_JQ'], *sys.argv[1:]], capture_output=True, check=False)\n"
        "if 'to_entries | map' in ' '.join(sys.argv[1:]):\n"
        "    output = result.stdout.replace(b'\\r\\n', b'\\n').replace(b'\\n', os.environ['JQ_SELECTOR_EOL'].encode())\n"
        "    sys.stdout.buffer.write(output)\n"
        "else:\n"
        "    sys.stdout.buffer.write(result.stdout)\n"
        "sys.stderr.buffer.write(result.stderr)\n"
        "raise SystemExit(result.returncode)\n"
    )
    directory.mkdir()
    wrapper = directory / "jq"
    wrapper.write_text(
        f'#!/usr/bin/env bash\nexec {shlex.quote(Path(sys.executable).as_posix())} -c {shlex.quote(program)} "$@"\n',
        encoding="utf-8",
        newline="\n",
    )
    wrapper.chmod(0o755)


@_requires_gnu_make_and_jq
@pytest.mark.integration
class TestMigrateMarketplace:
    """Jq-driven registry rewrites for a stale marketplace registration."""

    @pytest.mark.parametrize("jq_selector_line_ending", ["\n", "\r\n"])
    def test_renames_stale_marketplace_across_all_three_registries(
        self, tmp_path: Path, jq_selector_line_ending: str
    ) -> None:
        """A stale marketplace name is renamed everywhere, including nested string values.

        Covers the cache directory rename plus all three jq mutations (known_marketplaces.json key rename,
        installed_plugins.json key + nested-string rename via `walk`, and settings.json's extraKnownMarketplaces
        deletion + nested-string rename). The selector's line ending is varied because Bash `read -r` preserves a
        carriage return before its newline.
        """
        cache_dir = tmp_path / "cache"
        (cache_dir / "old-name").mkdir(parents=True)
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        # GNU Make executes recipes through Bash.  Use its slash-delimited spelling
        # on every host so the registry value and shell argument stay comparable.
        project_path = project_dir.as_posix()
        known_marketplaces = tmp_path / "known_marketplaces.json"
        known_marketplaces.write_text(json.dumps({"old-name": {"source": {"path": project_path}}}), encoding="utf-8")
        installed_plugins = tmp_path / "installed_plugins.json"
        installed_plugins.write_text(
            json.dumps({"plugins": {"foundry@old-name": [{"installPath": "old-name/foundry/1.0.0"}]}}),
            encoding="utf-8",
        )
        settings = tmp_path / "settings.json"
        settings.write_text(
            json.dumps({"extraKnownMarketplaces": {"old-name": {}}, "enabledPlugins": {"foundry@old-name": True}}),
            encoding="utf-8",
        )
        jq_bin_dir = tmp_path / "bin"
        _write_crlf_jq_wrapper(jq_bin_dir)
        env = os.environ.copy()
        env["PATH"] = f"{jq_bin_dir}{os.pathsep}{env['PATH']}"
        env["REAL_JQ"] = JQ
        env["JQ_SELECTOR_EOL"] = jq_selector_line_ending

        result = _run_make(
            "migrate-marketplace",
            env=env,
            extra_vars={
                "PROJECT_DIR": project_path,
                "MARKETPLACE": "new-name",
                "CACHE_DIR": str(cache_dir),
                "KNOWN_MARKETPLACES": str(known_marketplaces),
                "INSTALLED_PLUGINS": str(installed_plugins),
                "SETTINGS": str(settings),
            },
        )

        rewritten_settings = json.loads(settings.read_text(encoding="utf-8"))
        assert result.returncode == 0, result.stdout + result.stderr
        assert (cache_dir / "new-name").is_dir()
        assert not (cache_dir / "old-name").exists()
        assert json.loads(known_marketplaces.read_text(encoding="utf-8")) == {
            "new-name": {"source": {"path": project_path}}
        }
        assert "foundry@new-name" in json.loads(installed_plugins.read_text(encoding="utf-8"))["plugins"]
        assert rewritten_settings["extraKnownMarketplaces"] == {}
        assert "foundry@new-name" in rewritten_settings["enabledPlugins"]


class _Terminal(io.StringIO):
    """In-memory stream that reports itself as an operator terminal."""

    def isatty(self) -> bool:
        """Report an interactive terminal so the consent helper agrees to mint a token."""
        return True


def _mint_operator_token(home: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Mint a one-time operator token through the real helper, as an operator terminal would, and return it."""
    output = _Terminal()
    with monkeypatch.context() as patch:
        patch.setenv("CODEX_HOME", str(home))
        patch.setattr(sys, "stdout", output)
        patch.setattr(sys, "stdin", _Terminal())
        assert runpy.run_path(str(CONSENT_SCRIPT))["main"](["issue-token"]) == 0
    return output.getvalue().splitlines()[1]


class ConsentRun(NamedTuple):
    """Outcome of one captured Codex sync: the make result and what it left behind."""

    result: subprocess.CompletedProcess[str]
    dispatched: list[str]
    config: str
    token_left: bool


@pytest.fixture(name="consent_home")
def _consent_home(tmp_path: Path) -> Path:
    """Create a Codex home holding a ``config.toml`` the consent grant would amend."""
    home = tmp_path / "consent home"
    home.mkdir()
    (home / "config.toml").write_text(ORIGINAL_CONFIG, encoding="utf-8")
    return home


@pytest.fixture(name="run_consent_sync")
def _run_consent_sync(
    consent_home: Path, tmp_path: Path, fake_codex_sync_script: FakeScript, fake_codex_home_sync_script
):
    """Return a runner that executes one captured ``make`` sync with stubbed installers and a recording dispatcher.

    The dispatcher replaces ``$(MAKE)``: it keeps the recursive Codex recipes real, but fails closed on any unrelated
    sync target that is not explicitly stubbed.
    """
    dispatcher = tmp_path / "make_dispatch.py"
    dispatched_log = tmp_path / "dispatched.log"
    dispatcher.write_text(
        "import subprocess, sys\n"
        f"with open({str(dispatched_log)!r}, 'a', encoding='utf-8') as log: log.write(sys.argv[1] + '\\n')\n"
        "if sys.argv[1:] in (['sync-claude'], ['sync-opencode']): raise SystemExit(0)\n"
        "if sys.argv[1] not in ('sync-codex', 'install-codex-plugins', "
        "'sync-codex-home-policy', 'sync-codex-bridge-consent'): raise SystemExit(2)\n"
        f"raise SystemExit(subprocess.call([{GNU_MAKE!r}, *sys.argv[1:]]))\n",
        encoding="utf-8",
    )
    ambient = ("APPROVE_CLAUDE_CONSENT", "BRIDGE_OPERATOR_TOKEN")

    def run(
        target: str, make_vars: dict[str, str], env_vars: dict[str, str], operator_env: dict[str, str]
    ) -> ConsentRun:
        """Run ``target`` with consent given through make variables and/or the environment, with or without a token."""
        env = {k: v for k, v in os.environ.items() if k not in ambient}
        env.update(HOME=str(consent_home), CODEX_HOME=str(consent_home), **env_vars, **operator_env)
        variables = {
            "CODEX_SYNC_SCRIPT": str(fake_codex_sync_script.path),
            "CODEX_HOME_SYNC_SCRIPT": str(fake_codex_home_sync_script.path),
            "MAKE": f"{shlex.quote(Path(sys.executable).as_posix())} {shlex.quote(dispatcher.as_posix())}",
            **make_vars,
        }
        result = _run_make(target, env=env, extra_vars=variables)
        return ConsentRun(
            result=result,
            dispatched=dispatched_log.read_text(encoding="utf-8").splitlines(),
            config=(consent_home / "config.toml").read_text(encoding="utf-8"),
            token_left=(consent_home / ".bridge-consent-operator-token").exists(),
        )

    return run


_CONSENT_ON = {"APPROVE_CLAUDE_CONSENT": "1"}
_CONSENT_OFF = {"APPROVE_CLAUDE_CONSENT": "0"}


@_requires_gnu_make
@pytest.mark.integration
class TestCodexSyncConsent:
    """Consent is recorded only with operator proof; the implicit full-sync default degrades to a notice without it.

    Make runs with piped output, so it has no operator terminal. Without a one-time token the implicit full-sync default
    skips with a notice and exits zero rather than failing the whole sync, while an explicit opt-in (command line or
    environment) fails so a requested grant never silently does nothing. With a token minted by the real helper it
    grants once and consumes the token. Opting out with ``APPROVE_CLAUDE_CONSENT=0`` changes nothing.
    """

    @staticmethod
    def _assert_installers_ran(
        run: ConsentRun, target: str, fake_codex_sync_script, fake_codex_home_sync_script
    ) -> None:
        """Assert the installer and policy stubs ran, OpenCode ran only for the full sync, and no prompt leaked."""
        assert fake_codex_sync_script.log.read_text(encoding="utf-8") == "install"
        assert fake_codex_home_sync_script.log.exists()
        assert ("sync-opencode" in run.dispatched) == (target == "sync-all")
        assert "Type Approve" not in run.result.stdout

    @pytest.mark.parametrize(
        ("target", "make_vars", "env_vars", "outcome"),
        [
            pytest.param("sync-codex", _CONSENT_OFF, {}, "unchanged", id="codex-opt-out-flag"),
            pytest.param("sync-codex", _CONSENT_ON, {}, "fails", id="codex-opt-in-flag"),
            pytest.param("sync-codex", {}, {}, "unchanged", id="codex-default"),
            pytest.param("sync-codex", {}, _CONSENT_ON, "fails", id="codex-opt-in-environment"),
            pytest.param("sync-all", _CONSENT_OFF, {}, "unchanged", id="all-opt-out-flag"),
            pytest.param("sync-all", _CONSENT_ON, {}, "fails", id="all-opt-in-flag"),
            pytest.param("sync-all", {}, {}, "skip-notice", id="all-default"),
            pytest.param("sync-all", {}, _CONSENT_OFF, "unchanged", id="all-opt-out-environment"),
        ],
    )
    def test_without_operator_proof(
        self,
        run_consent_sync,
        fake_codex_sync_script: FakeScript,
        fake_codex_home_sync_script: FakeScript,
        target: str,
        make_vars: dict[str, str],
        env_vars: dict[str, str],
        outcome: str,
    ) -> None:
        """No grant is written without proof: an explicit opt-in fails, the implicit default skips with a notice.

        The sync is captured with no token and no terminal. Installers still run in every case, and the Codex config and
        the (never minted) token file stay untouched.
        """
        run = run_consent_sync(target, make_vars, env_vars, {})

        self._assert_installers_ran(run, target, fake_codex_sync_script, fake_codex_home_sync_script)
        assert (run.result.returncode != 0) == (outcome == "fails"), run.result.stdout + run.result.stderr
        assert (CONSENT_PROOF_ERROR in run.result.stderr) == (outcome == "fails")
        assert (CONSENT_SKIP_NOTICE in run.result.stdout) == (outcome == "skip-notice")
        assert run.config == ORIGINAL_CONFIG
        assert not run.token_left

    @pytest.mark.parametrize(
        ("target", "make_vars", "env_vars", "outcome"),
        [
            pytest.param("sync-codex", _CONSENT_OFF, {}, "unchanged", id="codex-opt-out-flag"),
            pytest.param("sync-codex", _CONSENT_ON, {}, "granted", id="codex-opt-in-flag"),
            pytest.param("sync-codex", {}, {}, "unchanged", id="codex-default"),
            pytest.param("sync-codex", {}, _CONSENT_ON, "granted", id="codex-opt-in-environment"),
            pytest.param("sync-all", _CONSENT_OFF, {}, "unchanged", id="all-opt-out-flag"),
            pytest.param("sync-all", _CONSENT_ON, {}, "granted", id="all-opt-in-flag"),
            pytest.param("sync-all", {}, {}, "granted", id="all-default"),
            pytest.param("sync-all", {}, _CONSENT_OFF, "unchanged", id="all-opt-out-environment"),
        ],
    )
    def test_with_operator_proof(
        self,
        run_consent_sync,
        consent_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_codex_sync_script: FakeScript,
        fake_codex_home_sync_script: FakeScript,
        target: str,
        make_vars: dict[str, str],
        env_vars: dict[str, str],
        outcome: str,
    ) -> None:
        """A one-time operator token grants a requested consent once and is consumed; an opt-out leaves it unused.

        The token is minted by the real helper as an operator terminal would. The grant marker lands in the Codex config
        only when consent was requested (explicitly or by the full-sync default); otherwise both stay as found.
        """
        operator_env = {"BRIDGE_OPERATOR_TOKEN": _mint_operator_token(consent_home, monkeypatch)}

        run = run_consent_sync(target, make_vars, env_vars, operator_env)

        granted = outcome == "granted"
        self._assert_installers_ran(run, target, fake_codex_sync_script, fake_codex_home_sync_script)
        assert run.result.returncode == 0, run.result.stdout + run.result.stderr
        assert ("bridge:claude-transfer-consent:v1 begin" in run.config) == granted
        assert ('"consent": "configured"' in run.result.stdout) == granted
        assert ("with a one-time operator token" in run.result.stdout) == granted
        assert (run.config == ORIGINAL_CONFIG) == (not granted)
        assert run.token_left == (not granted)


@_requires_gnu_make
@pytest.mark.integration
class TestCodexSyncTargets:
    """Argument wiring and failure ordering of the Codex-side install, policy and consent targets."""

    def test_install_passes_no_flags(self, fake_codex_sync_script: FakeScript) -> None:
        """Dropped CLI flags must not silently resurface as passed args.

        ``install-codex-plugins`` runs the sync script with the single ``install`` word and nothing else.
        """
        result = _run_make(
            "install-codex-plugins",
            env=os.environ.copy(),
            extra_vars={"CODEX_SYNC_SCRIPT": str(fake_codex_sync_script.path)},
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert fake_codex_sync_script.log.read_text(encoding="utf-8") == "install"

    def test_home_policy_forwards_source_and_codex_home(self, fake_codex_home_sync_script: FakeScript) -> None:
        """The policy mirror target forwards config/policy source paths and ``$CODEX_HOME``, always including policy.

        The stub records its argv; each documented option must appear with the repository's own source file.
        """
        codex_home = fake_codex_home_sync_script.path.parent / "codex-home"
        codex_home.mkdir()
        env = {**os.environ, "CODEX_HOME": codex_home.as_posix()}

        result = _run_make(
            "sync-codex-home-policy",
            env=env,
            extra_vars={"CODEX_HOME_SYNC_SCRIPT": str(fake_codex_home_sync_script.path)},
        )

        logged_args = fake_codex_home_sync_script.log.read_text(encoding="utf-8")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "--source-config" in logged_args
        assert (ROOT / ".codex" / "config.toml").as_posix() in logged_args
        assert "--source-policy" in logged_args
        assert (ROOT / ".codex" / "global-session-policy.md").as_posix() in logged_args
        assert f"--codex-home {codex_home.as_posix()}" in logged_args

    @pytest.mark.parametrize("approve", ["yes", "", "2"])
    def test_rejects_invalid_consent_opt_in_before_any_sync(
        self, fake_codex_sync_script: FakeScript, fake_codex_home_sync_script: FakeScript, tmp_path: Path, approve: str
    ) -> None:
        """Reject a mistyped opt-in before invoking even the installer.

        ``APPROVE_CLAUDE_CONSENT`` accepts only ``0`` or ``1``; anything else must fail validation with a message and
        never reach the install stage.
        """
        result = _run_make(
            "sync-codex",
            env={**os.environ, "CODEX_HOME": str(tmp_path / "unused-home")},
            extra_vars={
                "CODEX_SYNC_SCRIPT": str(fake_codex_sync_script.path),
                "CODEX_HOME_SYNC_SCRIPT": str(fake_codex_home_sync_script.path),
                "APPROVE_CLAUDE_CONSENT": approve,
            },
        )

        assert result.returncode != 0
        assert "APPROVE_CLAUDE_CONSENT must be 0 or 1" in result.stderr
        assert not fake_codex_sync_script.log.exists()

    @pytest.mark.parametrize("keep_going", [False, True])
    @pytest.mark.parametrize(
        ("failed_stage", "expected_calls", "grant_reached"),
        [
            pytest.param("install", 1, False, id="install-fails"),
            pytest.param("policy", 2, False, id="policy-fails"),
            pytest.param("grant", 3, True, id="grant-fails"),
        ],
    )
    def test_failed_stage_stops_later_stages(
        self, tmp_path: Path, failed_stage: str, expected_calls: int, grant_reached: bool, keep_going: bool
    ) -> None:
        """A failed install, policy sync or grant never reaches a later successful stage, even under ``make -k``.

        One stub stands in for all three scripts and fails only for ``FAILED_STAGE``. Each stage is a prerequisite of
        the next, so the call count stops at the failing stage and the sync never reports completion.
        """
        script = tmp_path / "stage.py"
        log = tmp_path / "stages.jsonl"
        script.write_text(
            "import json, os, sys\n"
            f"with open({str(log)!r}, 'a', encoding='utf-8') as stream:\n"
            "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "stage = sys.argv[1] if sys.argv[1] in ('install', 'grant', 'check') else 'policy'\n"
            "raise SystemExit(7 if stage == os.environ['FAILED_STAGE'] else 0)\n",
            encoding="utf-8",
        )

        result = _run_make(
            "sync-codex",
            # A token stands in for operator proof so the stub grant is reached instead of the no-proof skip.
            env={
                **os.environ,
                "FAILED_STAGE": failed_stage,
                "MAKEFLAGS": "-k" if keep_going else "",
                "BRIDGE_OPERATOR_TOKEN": "stub-operator-token",
            },
            extra_vars={
                "CODEX_SYNC_SCRIPT": str(script),
                "CODEX_HOME_SYNC_SCRIPT": str(script),
                "BRIDGE_CONSENT_SCRIPT": str(script),
                "APPROVE_CLAUDE_CONSENT": "1",
            },
        )

        calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        assert result.returncode != 0
        assert len(calls) == expected_calls
        assert calls[0] == ["install"]
        assert (calls[-1] == ["grant", "--approve"]) == grant_reached
        assert "Codex sync complete" not in result.stdout


@_requires_gnu_make
@pytest.mark.integration
class TestMakeDryRun:
    """`make -n` traces recipes without executing installers or touching native state."""

    @pytest.mark.parametrize("target", ["sync-all", "default"])
    def test_full_sync_entrypoints_approve_consent_by_default(self, target: str) -> None:
        """Both full-sync entrypoints trace the consent grant on by default, with the no-proof notice as fallback.

        With ``APPROVE_CLAUDE_CONSENT`` unset the traced recipe must not take the opt-out branch and must call the
        consent helper's ``grant --approve``.
        """
        env = {key: value for key, value in os.environ.items() if key != "APPROVE_CLAUDE_CONSENT"}

        result = subprocess.run(
            [GNU_MAKE, "-n", "-f", str(MAKEFILE), *([] if target == "default" else [target])],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert 'if [ "1" = "0" ]; then' in result.stdout
        assert 'bridge_consent.py" grant --approve &&' in result.stdout
        assert CONSENT_SKIP_NOTICE in result.stdout

    @pytest.mark.parametrize(
        "target",
        [
            "uninstall-claude-plugins",
            "refresh-ext-marketplace",
            "update-ext-plugins",
            "register-marketplace",
            "prune-claude-cache",
            "verify-claude-plugins",
            "warn-stale-claude-sessions",
            "clear-claude",
            "clear-codex",
        ],
    )
    def test_target_parses_without_error(self, target: str) -> None:
        """Every remaining target dry-runs cleanly (catches syntax/escaping regressions).

        A `$$`-escaping mistake or a missing `;` in one of these simpler targets would show up here as a nonzero `make
        -n` exit even before any real command runs.
        """
        result = subprocess.run(
            [GNU_MAKE, "-f", str(MAKEFILE), "-n", target],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

        assert result.returncode == 0, result.stderr


def _orphan(cache: Path, plugin: str, version: str, stamp: str | None) -> Path:
    """Create one cached plugin version dir, with an `.orphaned_at` marker when `stamp` is given."""
    root = cache / "borda-ai-rig" / plugin / version
    root.mkdir(parents=True)
    if stamp is not None:
        (root / ".orphaned_at").write_text(stamp, encoding="utf-8")
    return root


def _prune_claude_cache(cache: Path, installed_plugins: Path) -> subprocess.CompletedProcess[str]:
    """Run ``prune-claude-cache`` against a scratch cache and install record."""
    return _run_make(
        "prune-claude-cache",
        env=os.environ.copy(),
        extra_vars={
            "CACHE_DIR": cache.as_posix(),
            "INSTALLED_PLUGINS": installed_plugins.as_posix(),
            "MARKETPLACE": "borda-ai-rig",
        },
    )


@_requires_gnu_make_and_jq
@pytest.mark.integration
class TestPruneClaudeCache:
    """Pruning of replaced plugin versions that `claude plugin uninstall` marks but never deletes."""

    def test_removes_only_aged_uninstalled_orphans(self, tmp_path: Path) -> None:
        """Only orphaned version dirs past the age floor and absent from the install record are deleted.

        `claude plugin uninstall` marks a replaced version with `.orphaned_at` but never deletes it, so old versions
        pile up across syncs. A session started before the replacement may still read skill files from that dir, which
        is why a young marker, a still-installed dir, an unreadable marker, and an unmarked dir must all survive.
        """
        cache = tmp_path / "cache"
        day_ago_ms = str(int((time.time() - 2 * 86400) * 1000))
        hour_ago_ms = str(int((time.time() - 3600) * 1000))
        aged = _orphan(cache, "oss", "0.40.3", day_ago_ms)
        aged_seconds = _orphan(cache, "oss", "0.41.0", str(int(time.time() - 2 * 86400)))
        young = _orphan(cache, "foundry", "0.62.0", hour_ago_ms)
        still_installed = _orphan(cache, "research", "0.26.0", day_ago_ms)
        unreadable = _orphan(cache, "develop", "0.34.0", "not-a-stamp")
        current = _orphan(cache, "oss", "0.41.1", None)
        installed_plugins = tmp_path / "installed_plugins.json"
        installed_plugins.write_text(
            json.dumps(
                {
                    "plugins": {
                        "oss@borda-ai-rig": [{"installPath": current.as_posix()}],
                        "research@borda-ai-rig": [{"installPath": still_installed.as_posix()}],
                    }
                }
            ),
            encoding="utf-8",
        )

        result = _prune_claude_cache(cache, installed_plugins)

        assert result.returncode == 0, result.stdout + result.stderr
        assert not aged.exists()
        assert not aged_seconds.exists()
        assert young.is_dir()
        assert still_installed.is_dir()
        assert unreadable.is_dir()
        assert current.is_dir()
        assert "2 orphaned version dir(s) removed" in result.stdout

    def test_skips_when_registry_is_unreadable(self, tmp_path: Path) -> None:
        """An unreadable install record stops pruning instead of reading as "nothing installed".

        The registry is the only guard keeping a still-installed version dir that carries an old marker; treating a
        corrupt file as empty would let the prune delete the copy a plugin currently loads from.
        """
        cache = tmp_path / "cache"
        aged = _orphan(cache, "oss", "0.40.3", str(int((time.time() - 2 * 86400) * 1000)))
        installed_plugins = tmp_path / "installed_plugins.json"
        installed_plugins.write_text("{not json", encoding="utf-8")

        result = _prune_claude_cache(cache, installed_plugins)

        assert result.returncode == 0, result.stdout + result.stderr
        assert aged.is_dir()
        assert "pruning skipped" in result.stdout


VERIFY_SCRIPT = ROOT / "scripts" / "verify_claude_plugins.py"
#: Version the scratch marketplace declares for each plugin: alpha from its manifest, beta from its catalog entry.
DECLARED = {"alpha": "1.2.0", "beta": "2.0.0"}


class ScratchRegistry(NamedTuple):
    """A scratch marketplace clone plus the registries and cache that point at it."""

    clone: Path
    cache: Path
    known: Path
    installed: Path


def _write_json(path: Path, data: object) -> None:
    """Write one JSON file, creating its parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _record(registry: ScratchRegistry, plugin: str, version: str, **extra: object) -> dict:
    """Return one install record for ``plugin`` at ``version``, creating its cache dir unless told it is missing."""
    install_dir = registry.cache / "demo" / plugin / version
    if not extra.pop("missing_dir", False):
        install_dir.mkdir(parents=True, exist_ok=True)
    if extra.pop("orphaned", False):
        (install_dir / ".orphaned_at").write_text("1", encoding="utf-8")
    return {"scope": "user", "version": version, "installPath": install_dir.as_posix(), **extra}


def _install(registry: ScratchRegistry, alpha: list[dict]) -> None:
    """Write installed_plugins.json with one alpha record per spec (``_record`` keywords) and a current beta install."""
    alpha_records = [_record(registry, "alpha", **spec) for spec in alpha]
    records = {"alpha@demo": alpha_records, "beta@demo": [_record(registry, "beta", DECLARED["beta"])]}
    _write_json(registry.installed, {"version": 2, "plugins": {key: value for key, value in records.items() if value}})


def _verify(registry: ScratchRegistry, **extra_vars: str) -> subprocess.CompletedProcess[str]:
    """Run the verify-claude-plugins target against the scratch registry."""
    variables = {
        "INSTALLED_PLUGINS": registry.installed.as_posix(),
        "KNOWN_MARKETPLACES": registry.known.as_posix(),
        "MARKETPLACE": "demo",
        "PLUGINS": "alpha beta",
        "EXTERNAL_PLUGINS": "",
        "MARKETPLACE_REMOTE": "",
        **extra_vars,
    }
    return _run_make("verify-claude-plugins", env=os.environ.copy(), extra_vars=variables)


def _git_commit(repo: Path, message: str) -> None:
    """Commit everything in ``repo`` with a config isolated from the host's global and system git settings.

    The global config points at an empty file beside the repository rather than ``os.devnull``, whose ``nul`` spelling
    Git for Windows does not reliably accept as a config path.
    """
    isolated = repo.parent / "isolated.gitconfig"
    isolated.write_text("", encoding="utf-8")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(isolated), "GIT_CONFIG_NOSYSTEM": "1"}
    identity = ["-c", "user.name=test", "-c", "user.email=test@example.invalid"]
    for args in (["init", "-q"], ["add", "-A"], [*identity, "commit", "-q", "-m", message]):
        subprocess.run([GIT, "-C", str(repo), *args], env=env, check=True, capture_output=True)


@pytest.fixture(name="registry")
def _registry(tmp_path: Path) -> ScratchRegistry:
    """Build a marketplace clone declaring alpha 1.2.0 (manifest) and beta 2.0.0 (catalog entry), with no installs."""
    clone = tmp_path / "marketplaces" / "demo"
    catalog = {
        "name": "demo",
        "plugins": [
            {"name": "alpha", "source": "./plugins/alpha", "version": "0.0.1"},
            {"name": "beta", "source": "./plugins/beta", "version": DECLARED["beta"]},
        ],
    }
    _write_json(clone / ".claude-plugin" / "marketplace.json", catalog)
    _write_json(clone / "plugins" / "alpha" / ".claude-plugin" / "plugin.json", {"name": "alpha", "version": "1.2.0"})
    _write_json(clone / "plugins" / "beta" / ".claude-plugin" / "plugin.json", {"name": "beta"})
    known = tmp_path / "known_marketplaces.json"
    _write_json(known, {"demo": {"installLocation": clone.as_posix()}})
    return ScratchRegistry(clone, tmp_path / "cache", known, tmp_path / "installed_plugins.json")


@_requires_gnu_make
@pytest.mark.integration
class TestVerifyClaudePlugins:
    """The post-install check fails the sync whenever a plugin could load an older version than its marketplace."""

    def test_passes_when_every_install_matches_the_marketplace(self, registry: ScratchRegistry) -> None:
        """Current user-scope installs pass, with the manifest version taking precedence over the catalog entry.

        Alpha declares 1.2.0 in its manifest (the catalog says 0.0.1) and beta declares 2.0.0 in the catalog only.
        """
        _install(registry, [{"version": "1.2.0"}])

        result = _verify(registry)

        assert result.returncode == 0, result.stdout + result.stderr
        assert "✓ alpha@demo [user]: 1.2.0" in result.stdout
        assert "✓ beta@demo [user]: 2.0.0" in result.stdout

    @pytest.mark.parametrize(
        ("alpha", "expected"),
        [
            pytest.param(
                [{"version": "1.1.0"}],
                "✗ alpha@demo [user]: installed 1.1.0, marketplace has 1.2.0 → claude plugin update alpha@demo",
                id="older-user-install",
            ),
            pytest.param([], "✗ alpha@demo: not installed → claude plugin install alpha@demo", id="missing-install"),
            pytest.param([{"version": "1.2.0", "orphaned": True}], "install dir is marked orphaned", id="orphaned-dir"),
            pytest.param([{"version": "1.2.0", "missing_dir": True}], "install dir missing", id="missing-dir"),
            pytest.param(
                [{"version": "1.2.0"}, {"version": "1.1.0", "scope": "project", "projectPath": "/work/app"}],
                "✗ alpha@demo [project /work/app]: installed 1.1.0, marketplace has 1.2.0 → in /work/app: "
                "claude plugin update alpha@demo --scope project",
                id="older-project-install",
            ),
            pytest.param(
                [{"version": "1.2.0", "scope": "project", "projectPath": "/work/app"}],
                "✗ alpha@demo: no user-scope install",
                id="project-scope-only",
            ),
        ],
    )
    def test_fails_when_a_plugin_could_load_an_older_copy(
        self, registry: ScratchRegistry, alpha: list[dict], expected: str
    ) -> None:
        """Each way a sync can leave a plugin behind its marketplace exits nonzero and names the plugin and the fix.

        These are the silent paths: an uninstall failure reported as "not installed", an install that kept the old
        record, a project-scope install the sync never touches, or a record pointing at a replaced directory.
        """
        _install(registry, alpha)

        result = _verify(registry)

        assert result.returncode != 0
        assert expected in result.stdout
        assert "install problem(s)" in result.stdout

    def test_report_only_plugin_warns_without_failing(self, registry: ScratchRegistry) -> None:
        """An external plugin missing after an offline run is reported as a warning, never as a sync failure."""
        _install(registry, [{"version": "1.2.0"}])

        result = _verify(registry, EXTERNAL_PLUGINS="gamma@demo")

        assert result.returncode == 0, result.stdout + result.stderr
        assert "⚠ gamma@demo: not installed" in result.stdout

    def test_fails_when_marketplace_is_not_registered(self, registry: ScratchRegistry) -> None:
        """Without the registered clone there is nothing to compare against, so the check fails rather than passes."""
        _install(registry, [{"version": "1.2.0"}])
        _write_json(registry.known, {})

        result = _verify(registry)

        assert result.returncode != 0
        assert "marketplace demo is not registered" in result.stdout

    @pytest.mark.skipif(GIT is None, reason="git is not available on this host")
    def test_passes_when_clone_is_at_remote_head(self, registry: ScratchRegistry) -> None:
        """A clone whose HEAD equals the remote's confirms the versions were read from the current catalog."""
        _git_commit(registry.clone, "catalog")
        _install(registry, [{"version": "1.2.0"}])

        result = _verify(registry, MARKETPLACE_REMOTE=registry.clone.as_posix())

        assert result.returncode == 0, result.stdout + result.stderr
        assert "✓ marketplace clone matches" in result.stdout

    @pytest.mark.skipif(GIT is None, reason="git is not available on this host")
    def test_fails_when_clone_is_behind_remote(self, registry: ScratchRegistry, tmp_path: Path) -> None:
        """A stale clone would vouch for stale installs, so a clone HEAD different from the remote's fails the sync.

        Version equality alone passes when the clone itself is old; comparing it with the remote catches that.
        """
        _git_commit(registry.clone, "catalog")
        remote = tmp_path / "remote"
        remote.mkdir()
        (remote / "change.txt").write_text("newer", encoding="utf-8")
        _git_commit(remote, "newer catalog")
        _install(registry, [{"version": "1.2.0"}])

        result = _verify(registry, MARKETPLACE_REMOTE=remote.as_posix())

        assert result.returncode != 0
        assert "✗ marketplace clone is at" in result.stdout


@_requires_gnu_make
@pytest.mark.integration
def test_warn_stale_claude_sessions_passes_installed_registry(tmp_path: Path) -> None:
    """The session warning reads the registry the sync just wrote, via the shared helper's ``sessions`` subcommand.

    A stub replaces the verification helper and records its argv; the target must call ``sessions --installed <path>``.
    """
    script = _write_argv_logger(tmp_path / "fake_verify.py", tmp_path / "argv.log")
    installed = tmp_path / "installed_plugins.json"

    result = _run_make(
        "warn-stale-claude-sessions",
        env=os.environ.copy(),
        extra_vars={"VERIFY_PLUGINS_SCRIPT": script.path.as_posix(), "INSTALLED_PLUGINS": installed.as_posix()},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert script.log.read_text(encoding="utf-8") == f"sessions --installed {installed.as_posix()}"


@pytest.fixture(scope="module", name="verify_helpers")
def _verify_helpers() -> dict[str, object]:
    """Load the verification helper's definitions once, without running its command line; the tests only read them."""
    return runpy.run_path(str(VERIFY_SCRIPT))


def _lstart(epoch: float) -> str:
    """Format ``epoch`` the way ``ps -o lstart=`` prints a start time in the C locale."""
    return time.strftime("%a %b %d %H:%M:%S %Y", time.localtime(epoch))


class TestStaleSessionListing:
    """Claude Code processes older than the last install are listed; everything else is ignored."""

    def test_parse_ps_keeps_only_claude_code_processes(self, verify_helpers: dict[str, object]) -> None:
        """The native CLI and the Node entry point count; the desktop app and unrelated tools do not.

        A ``ps`` listing mixes the two Claude Code launch forms with the desktop app, an editor and a malformed line.
        """
        stamp = _lstart(1_700_000_000)
        ps_output = "\n".join(
            [
                f"  101 {stamp} /Users/me/.local/bin/claude --resume",
                f"  102 {stamp} node /usr/lib/node_modules/@anthropic-ai/claude-code/cli.js",
                f"  103 {stamp} /Applications/Claude.app/Contents/MacOS/Claude",
                f"  104 {stamp} /usr/bin/vim notes.md",
                "  garbage line",
            ]
        )

        rows = verify_helpers["parse_ps"](ps_output)

        assert [pid for pid, _ in rows] == [101, 102]
        assert rows[0][1] == pytest.approx(1_700_000_000)

    def test_lists_only_processes_started_before_the_install(
        self, verify_helpers: dict[str, object], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A process started before installed_plugins.json was written is named with its cwd and a restart step.

        Two processes straddle the registry's modification time; only the older one may be reported.
        """
        installed = tmp_path / "installed_plugins.json"
        installed.write_text("{}", encoding="utf-8")
        os.utime(installed, (1_700_000_000, 1_700_000_000))
        ps_output = f"  201 {_lstart(1_699_990_000)} claude\n  202 {_lstart(1_700_010_000)} claude\n"
        outputs = {"ps": ps_output, "lsof": "p201\nfcwd\nn/work/old-session\n"}

        def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            """Answer ps and lsof with canned output instead of inspecting real processes."""
            return subprocess.CompletedProcess(argv, 0, stdout=outputs[argv[0]], stderr="")

        probe = verify_helpers["ProcessProbe"](run=fake_run, platform="darwin", which=lambda name: f"/usr/bin/{name}")

        status = verify_helpers["run_sessions"](argparse.Namespace(installed=installed), probe)

        out = capsys.readouterr().out
        assert status == 0
        assert "pid 201" in out
        assert "/work/old-session" in out
        assert "pid 202" not in out
        assert "claude --resume <session-id>" in out

    def test_unsupported_host_is_skipped_quietly(
        self, verify_helpers: dict[str, object], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Native Windows has no ``ps``; the warning step prints nothing and still succeeds.

        The probe is told it runs on ``win32`` and any attempt to spawn a process listing fails the test.
        """
        installed = tmp_path / "installed_plugins.json"
        installed.write_text("{}", encoding="utf-8")

        def refuse_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            """Fail the test if the unsupported-host path tries to run a process listing."""
            raise AssertionError(f"unexpected process probe: {argv}")

        probe = verify_helpers["ProcessProbe"](run=refuse_run, platform="win32", which=lambda name: None)

        status = verify_helpers["run_sessions"](argparse.Namespace(installed=installed), probe)

        assert (status, capsys.readouterr().out) == (0, "")
