"""Tests for ``bin/check_agent.py`` plugin-agent installation probe.

The Python module checks whether a plugin agent is installed in the installed
cache (``~/.claude/plugins/cache/borda-ai-rig/<plugin>/*/agents/<agent>.md``)
only; a project-local ``.claude/agents/<agent>.md`` never counts. Prints
``"true"`` or ``"false"``; always exits 0. Invalid names trigger exit 2.
"""

from __future__ import annotations

from pathlib import Path

import check_agent  # type: ignore[import-not-found]
import pytest


@pytest.fixture(name="home")
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Give ``Path.home()`` an empty fake home directory so the real plugin cache is never consulted."""
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setattr(check_agent.Path, "home", classmethod(lambda cls: fake))
    return fake


def _agents_dir(home: Path, plugin: str, version: str) -> Path:
    """Create and return ``<home>/.claude/plugins/cache/borda-ai-rig/<plugin>/<version>/agents``."""
    agents = home / ".claude" / "plugins" / "cache" / "borda-ai-rig" / plugin / version / "agents"
    agents.mkdir(parents=True)
    return agents


class TestMainArguments:
    """``main`` argument validation."""

    @pytest.mark.parametrize(
        ("argv", "needle"),
        [
            pytest.param([], "Usage", id="missing-both-args"),
            pytest.param(["foundry"], "Usage", id="missing-second-arg"),
            pytest.param(["bad name", "shepherd"], "invalid plugin name", id="plugin-with-space"),
            pytest.param(["oss", "bad/agent"], "invalid agent name", id="agent-with-slash"),
            pytest.param(["", "shepherd"], "invalid plugin name", id="empty-plugin"),
            pytest.param(["bad/name", "shepherd"], "invalid plugin name", id="plugin-slash"),
            pytest.param(["oss", ""], "invalid agent name", id="empty-agent"),
            pytest.param(["oss", "../agent"], "invalid agent name", id="agent-traversal"),
        ],
    )
    def test_missing_or_invalid_args_exit_2(
        self, argv: list[str], needle: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Missing arguments or an unsafe plugin/agent name exit 2 with the reason on stderr.

        A path-shaped name (slash, ``..``, space, empty) must be refused before it is ever joined into a cache glob; the
        stderr message names which of the two names was invalid, or shows usage when arguments are missing.
        """
        rc = check_agent.main(argv)
        assert rc == 2
        assert needle in capsys.readouterr().err


class TestCheckAgent:
    """``check_agent`` lookup against a fake home directory."""

    def test_missing_cache_is_not_installed(self, home: Path) -> None:
        """With no plugin cache at all the agent is reported as not installed.

        A fresh machine has no ``~/.claude/plugins/cache`` tree; the probe must answer ``False`` rather than raise.
        """
        assert check_agent.check_agent("oss", "shepherd", home=home) is False

    def test_empty_version_dir_is_not_installed(self, home: Path) -> None:
        """A plugin version directory without an ``agents`` folder does not count as installed.

        The cache keeps one directory per installed version; one that has no agent files must be ignored.
        """
        (home / ".claude" / "plugins" / "cache" / "borda-ai-rig" / "oss" / "0.1.0").mkdir(parents=True)
        assert check_agent.check_agent("oss", "shepherd", home=home) is False

    @pytest.mark.parametrize(
        ("cache_plugin", "version", "content", "query_agent", "expected"),
        [
            pytest.param("oss", "0.1.0", "---\nname: shepherd\n---\n", "shepherd", True, id="found-in-cache"),
            pytest.param("oss", "1.2.3", "", "shepherd", True, id="found-in-different-version"),
            pytest.param("foundry", "0.1.0", "", "shepherd", False, id="cache-of-another-plugin-does-not-match"),
            pytest.param("oss", "0.1.0", "", "cicd-steward", False, id="cache-has-a-different-agent"),
        ],
    )
    def test_lookup_in_plugin_cache(
        self, home: Path, cache_plugin: str, version: str, content: str, query_agent: str, expected: bool
    ) -> None:
        """Only the requested plugin's cache and the requested agent file satisfy the lookup.

        The cache holds ``<plugin>/<version>/agents/shepherd.md``. The agent counts as installed under any version
        subdirectory, an agent under another plugin's cache does not satisfy the requested plugin, and a cached agent X
        does not satisfy a query for agent Y.
        """
        (_agents_dir(home, cache_plugin, version) / "shepherd.md").write_text(content)
        assert check_agent.check_agent("oss", query_agent, home=home) is expected

    def test_project_local_agent_is_not_an_installed_plugin_agent(
        self, tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A repository-planted ``.claude/agents`` file never stands in for the installed plugin agent.

        An untrusted checkout can ship its own ``.claude/agents/shepherd.md``; the probe, both as a function and through
        the command line, must keep answering ``false`` so a planted prompt is never treated as the vetted plugin agent.
        """
        agents_dir = tmp_path / ".claude" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "shepherd.md").write_text("planted prompt")
        monkeypatch.chdir(tmp_path)
        assert check_agent.check_agent("oss", "shepherd", home=home) is False
        assert check_agent.main(["oss", "shepherd"]) == 0
        assert capsys.readouterr().out.strip() == "false"


class TestMain:
    """``main`` prints the installed state on stdout and exits 0."""

    @pytest.mark.parametrize(
        ("plugin", "version", "agent"),
        [
            pytest.param("foundry", "0.5.0", "sw-engineer", id="foundry-sw-engineer"),
            pytest.param("oss", "0.1.0", "shepherd", id="documented-call-site-oss-shepherd"),
        ],
    )
    def test_prints_true_when_installed(
        self, home: Path, capsys: pytest.CaptureFixture[str], plugin: str, version: str, agent: str
    ) -> None:
        """An installed agent prints ``true`` and exits 0, including the documented ``oss shepherd`` call site.

        The shell guards in the skills consume this stdout verbatim, so it must be exactly the word ``true``.
        """
        (_agents_dir(home, plugin, version) / f"{agent}.md").write_text("")
        rc = check_agent.main([plugin, agent])
        assert rc == 0
        assert capsys.readouterr().out.strip() == "true"

    def test_prints_false_when_absent(self, home: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """An agent missing from the cache prints ``false`` and still exits 0.

        Absence is an answer, not an error: the exit code stays 0 so skills branch on stdout instead of aborting.
        """
        rc = check_agent.main(["oss", "missing-agent"])
        assert rc == 0
        assert capsys.readouterr().out.strip() == "false"
