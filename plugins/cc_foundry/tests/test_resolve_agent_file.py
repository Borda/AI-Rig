"""Tests for resolve_agent_file bin script.

Covers target splitting (agent vs skill, plugin prefix, default plugin), the three-tier resolution with its ambiguity
rules, and the CLI output contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import resolve_agent_file as raf


class TestSplitTarget:
    """Covers parsing a calibration target name."""

    def test_bare_agent_defaults_to_foundry(self) -> None:
        """A bare name is a foundry agent."""
        assert raf.split_target("curator") == ("foundry", "curator", "agents/curator.md")

    def test_plugin_prefixed_agent(self) -> None:
        """A `plugin:agent` target selects that plugin."""
        assert raf.split_target("oss:shepherd") == ("oss", "shepherd", "agents/shepherd.md")

    def test_leading_slash_selects_skill(self) -> None:
        """A leading slash makes the target a skill."""
        assert raf.split_target("/audit") == ("foundry", "audit", "skills/audit/SKILL.md")

    def test_plugin_prefixed_skill(self) -> None:
        """Both markers combine: `/plugin:skill`."""
        assert raf.split_target("/oss:review") == ("oss", "review", "skills/review/SKILL.md")


class TestResolve:
    """Covers the file-resolution tiers."""

    def test_cc_prefixed_directory_first(self, tmp_path: Path) -> None:
        """`plugins/cc_<plugin>/` wins over a bare `plugins/<plugin>/`."""
        for name in ("cc_foundry", "foundry"):
            target = tmp_path / name / "agents"
            target.mkdir(parents=True)
            (target / "a.md").write_text("x", encoding="utf-8")
        assert raf.resolve("foundry", "agents/a.md", root=tmp_path) == tmp_path / "cc_foundry/agents/a.md"

    def test_bare_plugin_directory_second(self, tmp_path: Path) -> None:
        """Without the cc_ form, the bare plugin directory resolves."""
        target = tmp_path / "foundry" / "agents"
        target.mkdir(parents=True)
        (target / "a.md").write_text("x", encoding="utf-8")
        assert raf.resolve("foundry", "agents/a.md", root=tmp_path) == tmp_path / "foundry/agents/a.md"

    def test_single_glob_match_accepted(self, tmp_path: Path) -> None:
        """One match anywhere under plugins/ resolves even under another name."""
        target = tmp_path / "cc_other" / "agents"
        target.mkdir(parents=True)
        (target / "a.md").write_text("x", encoding="utf-8")
        assert raf.resolve("foundry", "agents/a.md", root=tmp_path) == tmp_path / "cc_other/agents/a.md"

    def test_ambiguous_match_uses_prefix(self, tmp_path: Path) -> None:
        """With several matches, the plugin-prefixed directory is chosen."""
        for name in ("cc_alpha", "cc_beta"):
            target = tmp_path / name / "agents"
            target.mkdir(parents=True)
            (target / "a.md").write_text("x", encoding="utf-8")
        assert raf.resolve("beta", "agents/a.md", root=tmp_path) == tmp_path / "cc_beta/agents/a.md"

    def test_no_match_returns_none(self, tmp_path: Path) -> None:
        """Nothing on disk resolves to None rather than a guess."""
        assert raf.resolve("foundry", "agents/absent.md", root=tmp_path) is None


class TestCli:
    """Covers stdout contract and exit codes."""

    def test_resolved_prints_both_lines(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A hit prints agent-file and proposal-path and exits 0."""
        target = tmp_path / "plugins" / "cc_foundry" / "agents"
        target.mkdir(parents=True)
        (target / "curator.md").write_text("x", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("sys.argv", ["resolve_agent_file.py", "--name", "curator", "--timestamp", "TS"])
        assert raf.main() == 0
        out = capsys.readouterr().out
        assert "agent-file=plugins/cc_foundry/agents/curator.md" in out
        assert "proposal-path=.reports/calibrate/TS/curator/proposal.md" in out

    def test_unresolved_prints_empty_and_exits_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A miss prints the reason plus an empty agent-file and exits 1."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("sys.argv", ["resolve_agent_file.py", "--name", "nope"])
        assert raf.main() == 1
        out = capsys.readouterr().out
        assert "never falling back to installed cache" in out
        assert "agent-file=" in out

    def test_local_flag_labels_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--local changes only the failure label, not the resolution order."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("sys.argv", ["resolve_agent_file.py", "--name", "nope", "--local"])
        assert raf.main() == 1
        assert "⚠ --local:" in capsys.readouterr().out
