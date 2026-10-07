"""Tests for ``bin/check_agent.py`` plugin-agent installation probe.

The Python module checks whether a plugin agent is installed, either in the
installed cache (``~/.claude/plugins/cache/borda-ai-rig/<plugin>/*/agents/<agent>.md``)
or in the project-local ``.claude/agents/<agent>.md`` path. Prints ``"true"``
or ``"false"``; always exits 0. Invalid names trigger exit 2.
"""

from __future__ import annotations

from pathlib import Path

import check_agent  # type: ignore[import-not-found]
import pytest


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
def test_missing_or_invalid_args_exit_2(argv: list[str], needle: str, capsys: pytest.CaptureFixture[str]) -> None:
    """Missing args exit 2 with a usage message on stderr; an invalid plugin or agent name exits 2.

    The stderr message names the invalid plugin or agent.
    """
    rc = check_agent.main(argv)
    assert rc == 2
    assert needle in capsys.readouterr().err


def test_agent_not_found(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """No cache match and no local .claude/agents → prints ``false``, exits 0."""
    result = check_agent.check_agent("oss", "shepherd", home=tmp_path)
    assert result is False


@pytest.mark.parametrize(
    ("cache_plugin", "version", "content", "query_agent", "expected"),
    [
        pytest.param("oss", "0.1.0", "---\nname: shepherd\n---\n", "shepherd", True, id="found-in-cache"),
        pytest.param("oss", "1.2.3", "", "shepherd", True, id="found-in-different-version"),
        pytest.param("foundry", "0.1.0", "", "shepherd", False, id="cache-of-another-plugin-does-not-match"),
        pytest.param("oss", "0.1.0", "", "cicd-steward", False, id="cache-has-a-different-agent"),
    ],
)
def test_agent_lookup_in_plugin_cache(
    tmp_path: Path, cache_plugin: str, version: str, content: str, query_agent: str, expected: bool
) -> None:
    """The cache holds ``<plugin>/<version>/agents/shepherd.md``; only the requested plugin and agent match.

    The agent counts as installed in any version subdirectory (not just an exact version match). An agent under another
    plugin's cache does not satisfy the requested plugin, and a cached agent X does not satisfy a query for agent Y.
    """
    agents_dir = tmp_path / ".claude" / "plugins" / "cache" / "borda-ai-rig" / cache_plugin / version / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "shepherd.md").write_text(content)
    assert check_agent.check_agent("oss", query_agent, home=tmp_path) is expected


def test_agent_found_in_project_local_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Project-local .claude/agents fallback works when cache is absent."""
    agents_dir = tmp_path / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "shepherd.md").write_text("")
    monkeypatch.chdir(tmp_path)
    assert check_agent.check_agent("oss", "shepherd", home=tmp_path / "home") is True


def test_empty_version_dirs_return_false(tmp_path: Path) -> None:
    """Empty plugin version directories are ignored."""
    version_dir = tmp_path / ".claude" / "plugins" / "cache" / "borda-ai-rig" / "oss" / "0.1.0"
    version_dir.mkdir(parents=True)
    assert check_agent.check_agent("oss", "shepherd", home=tmp_path) is False


@pytest.mark.parametrize(
    ("plugin", "version", "agent"),
    [
        pytest.param("foundry", "0.5.0", "sw-engineer", id="foundry-sw-engineer"),
        pytest.param("oss", "0.1.0", "shepherd", id="documented-call-site-oss-shepherd"),
    ],
)
def test_main_prints_true(
    plugin: str, version: str, agent: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Print ``true`` to stdout and exit 0 when the agent is found.

    Covers the documented call site ``check_agent.py oss shepherd`` (2 positional arguments).
    """
    agents_dir = tmp_path / ".claude" / "plugins" / "cache" / "borda-ai-rig" / plugin / version / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / f"{agent}.md").write_text("")
    # Monkeypatch Path.home to return tmp_path
    import unittest.mock as mock

    with mock.patch.object(check_agent.Path, "home", return_value=tmp_path):
        rc = check_agent.main([plugin, agent])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "true"


def test_main_prints_false(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Print ``false`` to stdout when agent absent."""
    import unittest.mock as mock

    with mock.patch.object(check_agent.Path, "home", return_value=tmp_path):
        rc = check_agent.main(["oss", "missing-agent"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "false"
