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


def test_missing_both_args_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    """No args → exit 2 with usage message on stderr."""
    rc = check_agent.main([])
    assert rc == 2
    assert "Usage" in capsys.readouterr().err


def test_missing_second_arg_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    """One arg → exit 2 with usage message."""
    rc = check_agent.main(["foundry"])
    assert rc == 2
    assert "Usage" in capsys.readouterr().err


def test_invalid_plugin_name_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    """Plugin name with spaces → exit 2."""
    rc = check_agent.main(["bad name", "shepherd"])
    assert rc == 2
    assert "invalid plugin name" in capsys.readouterr().err


def test_invalid_agent_name_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    """Agent name with slashes → exit 2."""
    rc = check_agent.main(["oss", "bad/agent"])
    assert rc == 2
    assert "invalid agent name" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("plugin", "agent", "needle"),
    [
        pytest.param("", "shepherd", "invalid plugin name", id="empty-plugin"),
        pytest.param("bad/name", "shepherd", "invalid plugin name", id="plugin-slash"),
        pytest.param("oss", "", "invalid agent name", id="empty-agent"),
        pytest.param("oss", "../agent", "invalid agent name", id="agent-traversal"),
    ],
)
def test_invalid_names_exit_2(plugin: str, agent: str, needle: str, capsys: pytest.CaptureFixture[str]) -> None:
    rc = check_agent.main([plugin, agent])
    assert rc == 2
    assert needle in capsys.readouterr().err


def test_agent_not_found(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """No cache match and no local .claude/agents → prints ``false``, exits 0."""
    result = check_agent.check_agent("oss", "shepherd", home=tmp_path)
    assert result is False


def test_agent_found_in_cache(tmp_path: Path) -> None:
    """Cache contains ``<plugin>/<version>/agents/<agent>.md`` → True."""
    agents_dir = tmp_path / ".claude" / "plugins" / "cache" / "borda-ai-rig" / "oss" / "0.1.0" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "shepherd.md").write_text("---\nname: shepherd\n---\n")
    assert check_agent.check_agent("oss", "shepherd", home=tmp_path) is True


def test_agent_found_in_different_version(tmp_path: Path) -> None:
    """Agent in any version subdir → True (not just exact version match)."""
    agents_dir = tmp_path / ".claude" / "plugins" / "cache" / "borda-ai-rig" / "oss" / "1.2.3" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "shepherd.md").write_text("")
    assert check_agent.check_agent("oss", "shepherd", home=tmp_path) is True


def test_agent_found_in_project_local_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Project-local .claude/agents fallback works when cache is absent."""
    agents_dir = tmp_path / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "shepherd.md").write_text("")
    monkeypatch.chdir(tmp_path)
    assert check_agent.check_agent("oss", "shepherd", home=tmp_path / "home") is True


def test_cache_plugin_mismatch_returns_false(tmp_path: Path) -> None:
    """Agent under another plugin cache does not satisfy the requested plugin."""
    agents_dir = tmp_path / ".claude" / "plugins" / "cache" / "borda-ai-rig" / "foundry" / "0.1.0" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "shepherd.md").write_text("")
    assert check_agent.check_agent("oss", "shepherd", home=tmp_path) is False


def test_empty_version_dirs_return_false(tmp_path: Path) -> None:
    """Empty plugin version directories are ignored."""
    version_dir = tmp_path / ".claude" / "plugins" / "cache" / "borda-ai-rig" / "oss" / "0.1.0"
    version_dir.mkdir(parents=True)
    assert check_agent.check_agent("oss", "shepherd", home=tmp_path) is False


def test_cache_different_agent_returns_false(tmp_path: Path) -> None:
    """Cache has agent X; querying agent Y → False."""
    agents_dir = tmp_path / ".claude" / "plugins" / "cache" / "borda-ai-rig" / "oss" / "0.1.0" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "shepherd.md").write_text("")
    assert check_agent.check_agent("oss", "cicd-steward", home=tmp_path) is False


def test_main_prints_true(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Print ``true`` to stdout when agent found."""
    agents_dir = tmp_path / ".claude" / "plugins" / "cache" / "borda-ai-rig" / "foundry" / "0.5.0" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "sw-engineer.md").write_text("")
    # Monkeypatch Path.home to return tmp_path
    from unittest import mock

    with mock.patch.object(check_agent.Path, "home", return_value=tmp_path):
        rc = check_agent.main(["foundry", "sw-engineer"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "true"


def test_main_prints_false(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Print ``false`` to stdout when agent absent."""
    from unittest import mock

    with mock.patch.object(check_agent.Path, "home", return_value=tmp_path):
        rc = check_agent.main(["oss", "missing-agent"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "false"


def test_golden_invocation_two_positionals(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Documented call site ``check_agent.py oss shepherd`` (2 positional) prints true/false, exits 0."""
    from unittest import mock

    agents_dir = tmp_path / ".claude" / "plugins" / "cache" / "borda-ai-rig" / "oss" / "0.1.0" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "shepherd.md").write_text("")
    with mock.patch.object(check_agent.Path, "home", return_value=tmp_path):
        rc = check_agent.main(["oss", "shepherd"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "true"
