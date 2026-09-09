"""Behavioral acceptance checks for the root Makefile's Claude- and Codex-side sync targets.

These exercise the actual `make <target>` invocation against stubbed CLIs and scratch registries — never the real
`claude`/`codex` CLIs or the real `$HOME` — so they can run safely in CI without touching a developer's or runner's real
plugin state.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest


ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = ROOT / "Makefile"


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
    result = subprocess.run([make, "--version"], capture_output=True, text=True, check=False)
    if "GNU Make" not in result.stdout:
        return None
    return make


GNU_MAKE = _gnu_make()
JQ = shutil.which("jq")


def _run_make(
    target: str, *, env: dict[str, str], extra_vars: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Invoke one Makefile target with the given environment and Make-variable overrides."""
    args = [GNU_MAKE, "-f", str(MAKEFILE), target]
    for key, value in (extra_vars or {}).items():
        args.append(f"{key}={value}")
    return subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True, check=False)


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
    path = tmp_path / "fake_sync_codex.py"
    log = tmp_path / "codex-sync.log"
    path.write_text(
        "import sys\n"
        f"from pathlib import Path\n"
        f"Path({str(log)!r}).write_text(' '.join(sys.argv[1:]), encoding='utf-8')\n",
        encoding="utf-8",
    )
    return FakeScript(path=path, log=log)


@pytest.fixture(name="fake_codex_home_sync_script")
def _fake_codex_home_sync_script(tmp_path: Path) -> FakeScript:
    """Write a session-policy sync stub whose future log stays inside the fixture directory."""
    path = tmp_path / "fake_sync_codex_session_policy.py"
    log = tmp_path / "codex-home-sync.log"
    path.write_text(
        "import sys\n"
        f"from pathlib import Path\n"
        f"Path({str(log)!r}).write_text(' '.join(sys.argv[1:]), encoding='utf-8')\n",
        encoding="utf-8",
    )
    return FakeScript(path=path, log=log)


@pytest.mark.integration
@pytest.mark.skipif(GNU_MAKE is None, reason="GNU make is not available on this host")
class TestInstallClaudePlugins:
    """Bridge-purge guard and try-all-6-then-report contract for install-claude-plugins."""

    def test_purges_legacy_codex_plugin_when_bridge_install_succeeds(
        self, fake_claude: FakeClaude, sandbox_home: Path
    ) -> None:
        """A successful bridge install must still let the unconditional purge run.

        ponytail@ponytail is always purged; codex@openai-codex is purged only when this
        run's bridge install succeeded, per plugins/codex-rig/tests/test_sync_setup_dispatch.py's
        retired contract — this is the Claude-side half of that guard,
        now verified against the Makefile instead of the retired sync.sh.
        """
        env = os.environ.copy()
        env["PATH"] = f"{fake_claude.bin_dir}{os.pathsep}{env['PATH']}"
        env["HOME"] = str(sandbox_home)
        env["CLAUDE_STUB_LOG"] = str(fake_claude.log)
        env["FAIL_BRIDGE"] = "false"
        installed_plugins = sandbox_home / ".claude" / "plugins" / "installed_plugins.json"

        result = _run_make("install-claude-plugins", env=env, extra_vars={"INSTALLED_PLUGINS": str(installed_plugins)})

        assert result.returncode == 0, result.stdout + result.stderr
        calls = fake_claude.log.read_text(encoding="utf-8")
        assert "claude plugin uninstall ponytail@ponytail" in calls
        assert "claude plugin uninstall codex@openai-codex" in calls

    def test_preserves_legacy_codex_plugin_when_bridge_install_fails(
        self, fake_claude: FakeClaude, sandbox_home: Path
    ) -> None:
        """A failed bridge install must skip only the conditional purge entry, not the unconditional one.

        Also proves try-all-6-then-report: the run must still complete purge and setup-skills
        after the bridge failure, then exit nonzero for the accumulated failure count.
        """
        env = os.environ.copy()
        env["PATH"] = f"{fake_claude.bin_dir}{os.pathsep}{env['PATH']}"
        env["HOME"] = str(sandbox_home)
        env["CLAUDE_STUB_LOG"] = str(fake_claude.log)
        env["FAIL_BRIDGE"] = "true"
        installed_plugins = sandbox_home / ".claude" / "plugins" / "installed_plugins.json"

        result = _run_make("install-claude-plugins", env=env, extra_vars={"INSTALLED_PLUGINS": str(installed_plugins)})

        assert result.returncode != 0
        calls = fake_claude.log.read_text(encoding="utf-8")
        assert "claude plugin uninstall ponytail@ponytail" in calls
        assert "claude plugin uninstall codex@openai-codex" not in calls
        assert "codemap-py@" in calls  # plugins after the failed one still got installed


@pytest.mark.integration
@pytest.mark.skipif(GNU_MAKE is None or JQ is None, reason="GNU make and jq are required on this host")
class TestMigrateMarketplace:
    """Jq-driven registry rewrites for a stale marketplace registration."""

    @pytest.mark.parametrize("jq_selector_line_ending", ["\n", "\r\n"])
    def test_renames_stale_marketplace_across_all_three_registries(
        self, tmp_path: Path, jq_selector_line_ending: str
    ) -> None:
        """A stale marketplace name must be renamed everywhere, including nested string values.

        Covers the cache directory rename plus all three jq mutations (known_marketplaces.json key rename,
        installed_plugins.json key + nested-string rename via `walk`, and settings.json's extraKnownMarketplaces
        deletion + nested-string rename). The selector's line ending is varied because Bash `read -r` preserves a
        carriage return before its newline.
        """
        assert JQ is not None
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
        jq_bin_dir.mkdir()
        jq_wrapper = jq_bin_dir / "jq"
        jq_wrapper_program = (
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
        jq_wrapper.write_text(
            "#!/usr/bin/env bash\n"
            f'exec {shlex.quote(Path(sys.executable).as_posix())} -c {shlex.quote(jq_wrapper_program)} "$@"\n',
            encoding="utf-8",
            newline="\n",
        )
        jq_wrapper.chmod(0o755)
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

        assert result.returncode == 0, result.stdout + result.stderr
        assert (cache_dir / "new-name").is_dir()
        assert not (cache_dir / "old-name").exists()
        assert json.loads(known_marketplaces.read_text(encoding="utf-8")) == {
            "new-name": {"source": {"path": project_path}}
        }
        assert "foundry@new-name" in json.loads(installed_plugins.read_text(encoding="utf-8"))["plugins"]
        rewritten_settings = json.loads(settings.read_text(encoding="utf-8"))
        assert rewritten_settings["extraKnownMarketplaces"] == {}
        assert "foundry@new-name" in rewritten_settings["enabledPlugins"]


@pytest.mark.integration
@pytest.mark.skipif(GNU_MAKE is None, reason="GNU make is not available on this host")
class TestInstallCodexPlugins:
    """No-flag invocation contract for the Codex-side install target."""

    def test_invokes_sync_codex_install_with_no_flags(self, fake_codex_sync_script: FakeScript) -> None:
        """Dropped CLI flags must not silently resurface as passed args."""
        env = os.environ.copy()

        result = _run_make(
            "install-codex-plugins", env=env, extra_vars={"CODEX_SYNC_SCRIPT": str(fake_codex_sync_script.path)}
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert fake_codex_sync_script.log.read_text(encoding="utf-8") == "install"


@pytest.mark.integration
@pytest.mark.skipif(GNU_MAKE is None, reason="GNU make is not available on this host")
class TestSyncCodexHomePolicy:
    """Correct argument wiring for the Codex-home policy-mirror target."""

    def test_invokes_with_source_and_codex_home_arguments(self, fake_codex_home_sync_script: FakeScript) -> None:
        """The Makefile must forward config/policy source paths and $CODEX_HOME, always including policy."""
        env = os.environ.copy()
        codex_home = fake_codex_home_sync_script.path.parent / "codex-home"
        codex_home.mkdir()
        env["CODEX_HOME"] = codex_home.as_posix()

        result = _run_make(
            "sync-codex-home-policy",
            env=env,
            extra_vars={"CODEX_HOME_SYNC_SCRIPT": str(fake_codex_home_sync_script.path)},
        )

        assert result.returncode == 0, result.stdout + result.stderr
        logged_args = fake_codex_home_sync_script.log.read_text(encoding="utf-8")
        assert "--source-config" in logged_args
        assert (ROOT / ".codex" / "config.toml").as_posix() in logged_args
        assert "--source-policy" in logged_args
        assert (ROOT / ".codex" / "global-session-policy.md").as_posix() in logged_args
        assert f"--codex-home {codex_home.as_posix()}" in logged_args


@pytest.mark.integration
@pytest.mark.skipif(GNU_MAKE is None, reason="GNU make is not available on this host")
@pytest.mark.parametrize(
    "target",
    [
        "uninstall-claude-plugins",
        "refresh-ext-marketplace",
        "update-ext-plugins",
        "register-marketplace",
        "clear-claude",
        "clear-codex",
    ],
)
def test_target_dry_runs_without_a_make_parse_error(target: str) -> None:
    """Every remaining target must at least dry-run cleanly (catches syntax/escaping regressions).

    A `$$`-escaping mistake or a missing `;` in one of these simpler targets would show up here as a nonzero `make -n`
    exit even before any real command runs.
    """
    result = subprocess.run(
        [GNU_MAKE, "-f", str(MAKEFILE), "-n", target], cwd=ROOT, capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr
