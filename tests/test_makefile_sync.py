"""Behavioral acceptance checks for the root Makefile's Claude- and Codex-side sync targets.

These exercise the actual `make <target>` invocation against stubbed CLIs and scratch registries — never the real
`claude`/`codex` CLIs or the real `$HOME` — so they can run safely in CI without touching a developer's or runner's real
plugin state.
"""

from __future__ import annotations

import argparse
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
JQ = shutil.which("jq")


def _run_make(
    target: str, *, env: dict[str, str], extra_vars: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Invoke one Makefile target with the given environment and Make-variable overrides."""
    args = [GNU_MAKE, "-f", str(MAKEFILE), target]
    for key, value in (extra_vars or {}).items():
        args.append(f"{key}={value}")
    return subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", check=False)


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


@pytest.mark.integration
@pytest.mark.skipif(GNU_MAKE is None, reason="GNU make is not available on this host")
class TestInstallClaudePlugins:
    """Bridge-purge guard and try-all-6-then-report contract for install-claude-plugins."""

    @pytest.mark.skipif(JQ is None, reason="jq is required on this host")
    def test_setup_receives_each_installed_plugin_root(self, fake_claude: FakeClaude, sandbox_home: Path) -> None:
        """Setup must use its installed version even when the caller exports another plugin root."""
        registry = {}
        roots = []
        for plugin in ("foundry", "oss"):
            root = sandbox_home / ".claude" / "plugins" / "cache" / plugin / "version with spaces"
            skill = root / "skills" / "setup" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("# Setup\n", encoding="utf-8")
            roots.append(root.as_posix())
            registry[f"{plugin}@borda-ai-rig"] = [{"installPath": root.as_posix()}]
        installed = sandbox_home / ".claude" / "plugins" / "installed_plugins.json"
        installed.write_text(json.dumps({"plugins": registry}), encoding="utf-8")
        env = os.environ.copy()
        env.update(
            PATH=f"{fake_claude.bin_dir}{os.pathsep}{env['PATH']}",
            HOME=str(sandbox_home),
            CLAUDE_STUB_LOG=str(fake_claude.log),
            CLAUDE_PLUGIN_ROOT="unrelated-plugin",
        )

        result = _run_make(
            "install-claude-plugins",
            env=env,
            extra_vars={"PLUGINS": "foundry oss", "INSTALLED_PLUGINS": installed.as_posix()},
        )

        assert result.returncode == 0, result.stdout + result.stderr
        calls = fake_claude.log.read_text(encoding="utf-8").splitlines()
        assert [line for line in calls if line.startswith("setup-root=")] == [f"setup-root={root}" for root in roots]
        assert sum("/foundry:setup --approve" in line for line in calls) == 1
        assert sum("/oss:setup --approve" in line for line in calls) == 1

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
        "prune-claude-cache",
        "verify-claude-plugins",
        "warn-stale-claude-sessions",
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


@pytest.mark.integration
@pytest.mark.skipif(GNU_MAKE is None or JQ is None, reason="GNU make and jq are required on this host")
def test_prune_claude_cache_removes_only_aged_uninstalled_orphans(tmp_path: Path) -> None:
    """Only orphaned version dirs past the age floor and absent from the install record are deleted.

    `claude plugin uninstall` marks a replaced version with `.orphaned_at` but never deletes it, so old versions pile up
    across syncs. A session started before the replacement may still read skill files from that dir, which is why a
    young marker, a still-installed dir, an unreadable marker, and an unmarked dir must all survive.
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

    result = _run_make(
        "prune-claude-cache",
        env=os.environ.copy(),
        extra_vars={
            "CACHE_DIR": cache.as_posix(),
            "INSTALLED_PLUGINS": installed_plugins.as_posix(),
            "MARKETPLACE": "borda-ai-rig",
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not aged.exists()
    assert not aged_seconds.exists()
    assert young.is_dir()
    assert still_installed.is_dir()
    assert unreadable.is_dir()
    assert current.is_dir()
    assert "2 orphaned version dir(s) removed" in result.stdout


@pytest.mark.integration
@pytest.mark.skipif(GNU_MAKE is None or JQ is None, reason="GNU make and jq are required on this host")
def test_prune_claude_cache_skips_when_registry_is_unreadable(tmp_path: Path) -> None:
    """An unreadable install record must stop pruning instead of reading as "nothing installed".

    The registry is the only guard keeping a still-installed version dir that carries an old marker; treating a corrupt
    file as empty would let the prune delete the copy a plugin currently loads from.
    """
    cache = tmp_path / "cache"
    aged = _orphan(cache, "oss", "0.40.3", str(int((time.time() - 2 * 86400) * 1000)))
    installed_plugins = tmp_path / "installed_plugins.json"
    installed_plugins.write_text("{not json", encoding="utf-8")

    result = _run_make(
        "prune-claude-cache",
        env=os.environ.copy(),
        extra_vars={
            "CACHE_DIR": cache.as_posix(),
            "INSTALLED_PLUGINS": installed_plugins.as_posix(),
            "MARKETPLACE": "borda-ai-rig",
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert aged.is_dir()
    assert "pruning skipped" in result.stdout


VERIFY_SCRIPT = ROOT / "scripts" / "verify_claude_plugins.py"
GIT = shutil.which("git")
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


@pytest.mark.integration
@pytest.mark.skipif(GNU_MAKE is None, reason="GNU make is not available on this host")
class TestVerifyClaudePlugins:
    """The post-install check fails the sync whenever a plugin could load an older version than its marketplace."""

    def test_passes_when_every_install_matches_the_marketplace(self, registry: ScratchRegistry) -> None:
        """Current user-scope installs pass, with the manifest version taking precedence over the catalog entry."""
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


@pytest.mark.integration
@pytest.mark.skipif(GNU_MAKE is None, reason="GNU make is not available on this host")
def test_warn_stale_claude_sessions_passes_installed_registry(tmp_path: Path) -> None:
    """The session warning reads the registry the sync just wrote, via the shared helper's ``sessions`` subcommand."""
    log = tmp_path / "argv.log"
    script = tmp_path / "fake_verify.py"
    script.write_text(
        f"import sys\nfrom pathlib import Path\nPath({str(log)!r}).write_text(' '.join(sys.argv[1:]), encoding='utf-8')\n",
        encoding="utf-8",
    )
    installed = tmp_path / "installed_plugins.json"

    result = _run_make(
        "warn-stale-claude-sessions",
        env=os.environ.copy(),
        extra_vars={"VERIFY_PLUGINS_SCRIPT": script.as_posix(), "INSTALLED_PLUGINS": installed.as_posix()},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert log.read_text(encoding="utf-8") == f"sessions --installed {installed.as_posix()}"


def _verify_namespace() -> dict[str, object]:
    """Load the verification helper's definitions without running its command line."""
    return runpy.run_path(str(VERIFY_SCRIPT))


def _lstart(epoch: float) -> str:
    """Format ``epoch`` the way ``ps -o lstart=`` prints a start time in the C locale."""
    return time.strftime("%a %b %d %H:%M:%S %Y", time.localtime(epoch))


class TestStaleSessionListing:
    """Claude Code processes older than the last install are listed; everything else is ignored."""

    def test_parse_ps_keeps_only_claude_code_processes(self) -> None:
        """The native CLI and the Node entry point count; the desktop app and unrelated tools do not."""
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

        rows = _verify_namespace()["parse_ps"](ps_output)

        assert [pid for pid, _ in rows] == [101, 102]
        assert rows[0][1] == pytest.approx(1_700_000_000)

    def test_lists_only_processes_started_before_the_install(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A process started before installed_plugins.json was written is named with its cwd and a restart step."""
        namespace = _verify_namespace()
        installed = tmp_path / "installed_plugins.json"
        installed.write_text("{}", encoding="utf-8")
        os.utime(installed, (1_700_000_000, 1_700_000_000))
        ps_output = f"  201 {_lstart(1_699_990_000)} claude\n  202 {_lstart(1_700_010_000)} claude\n"
        outputs = {"ps": ps_output, "lsof": "p201\nfcwd\nn/work/old-session\n"}

        def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            """Answer ps and lsof with canned output instead of inspecting real processes."""
            return subprocess.CompletedProcess(argv, 0, stdout=outputs[argv[0]], stderr="")

        probe = namespace["ProcessProbe"](run=fake_run, platform="darwin", which=lambda name: f"/usr/bin/{name}")

        status = namespace["run_sessions"](argparse.Namespace(installed=installed), probe)

        out = capsys.readouterr().out
        assert status == 0
        assert "pid 201" in out
        assert "/work/old-session" in out
        assert "pid 202" not in out
        assert "claude --resume <session-id>" in out

    def test_unsupported_host_is_skipped_quietly(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Native Windows has no ``ps``; the warning step prints nothing and still succeeds."""
        namespace = _verify_namespace()
        installed = tmp_path / "installed_plugins.json"
        installed.write_text("{}", encoding="utf-8")

        def refuse_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            """Fail the test if the unsupported-host path tries to run a process listing."""
            raise AssertionError(f"unexpected process probe: {argv}")

        probe = namespace["ProcessProbe"](run=refuse_run, platform="win32", which=lambda name: None)

        status = namespace["run_sessions"](argparse.Namespace(installed=installed), probe)

        assert (status, capsys.readouterr().out) == (0, "")
