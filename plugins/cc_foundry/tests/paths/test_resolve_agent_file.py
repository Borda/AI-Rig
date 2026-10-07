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

    @pytest.mark.parametrize(
        ("target", "expected"),
        [
            pytest.param("curator", ("foundry", "curator", "agents/curator.md"), id="bare-agent-defaults-to-foundry"),
            pytest.param("oss:shepherd", ("oss", "shepherd", "agents/shepherd.md"), id="plugin-prefixed-agent"),
            pytest.param("/audit", ("foundry", "audit", "skills/audit/SKILL.md"), id="leading-slash-selects-skill"),
            pytest.param("/oss:review", ("oss", "review", "skills/review/SKILL.md"), id="plugin-prefixed-skill"),
        ],
    )
    def test_target_is_split(self, target: str, expected: tuple[str, str, str]) -> None:
        """A target splits into plugin, name and relative file.

        Scenario: a bare name is a foundry agent; `plugin:agent` selects that plugin; a leading slash makes the target a
        skill; both markers combine as `/plugin:skill`.
        """
        assert raf.split_target(target) == expected


class TestResolve:
    """Covers the file-resolution tiers."""

    @pytest.mark.parametrize(
        ("dirs", "plugin", "expected"),
        [
            pytest.param(("cc_foundry", "foundry"), "foundry", "cc_foundry", id="cc-prefixed-directory-first"),
            pytest.param(("foundry",), "foundry", "foundry", id="bare-plugin-directory-second"),
            pytest.param(("cc_other",), "foundry", "cc_other", id="single-glob-match-accepted"),
            pytest.param(("cc_alpha", "cc_beta"), "beta", "cc_beta", id="ambiguous-match-uses-prefix"),
        ],
    )
    def test_file_resolution_tiers(self, tmp_path: Path, dirs: tuple[str, ...], plugin: str, expected: str) -> None:
        """The plugin file resolves through the cc_ directory, the bare directory, then a single glob match.

        Scenario: `plugins/cc_<plugin>/` wins over a bare `plugins/<plugin>/`; without the cc_ form the bare directory
        resolves; one match anywhere under plugins/ resolves even under another name; with several matches the
        plugin-prefixed directory is chosen.
        """
        for name in dirs:
            target = tmp_path / name / "agents"
            target.mkdir(parents=True)
            (target / "a.md").write_text("x", encoding="utf-8")
        assert raf.resolve(plugin, "agents/a.md", root=tmp_path) == tmp_path / expected / "agents/a.md"

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
