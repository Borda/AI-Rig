"""Tests for check_skill_contracts bin script.

Covers Check 23b's three scans — including the multi-line subprocess call the line-based shell original could not see —
and Check 32f's shadow detection with its size and overlap thresholds.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import check_skill_contracts as csc


class TestUnenforcedTimeouts:
    """Covers the `# timeout:` comment scan."""

    def test_bare_comment_flagged(self, tmp_path: Path) -> None:
        """A command with only a timeout comment beside it is flagged."""
        findings = csc.unenforced_timeouts(tmp_path / "a.md", ["gh pr view 1  # timeout: 5000"])
        assert len(findings) == 1

    def test_shell_timeout_exempts(self, tmp_path: Path) -> None:
        """A real `timeout S` prefix satisfies the check."""
        findings = csc.unenforced_timeouts(tmp_path / "a.md", ["timeout 5 gh pr view 1  # timeout: 5000"])
        assert findings == []

    def test_python_line_exempt(self, tmp_path: Path) -> None:
        """Python scripts enforce internally via their own --timeout default."""
        findings = csc.unenforced_timeouts(tmp_path / "a.md", ['python "x/bin/y.py"  # timeout: 5000'])
        assert findings == []

    def test_comment_only_line_ignored(self, tmp_path: Path) -> None:
        """A line that is entirely a comment is prose, not a command."""
        findings = csc.unenforced_timeouts(tmp_path / "a.md", ["# timeout: 5000 is the convention"])
        assert findings == []


class TestUntimedSubprocess:
    """Covers the subprocess timeout= scan."""

    def test_single_line_without_timeout_flagged(self, tmp_path: Path) -> None:
        """A one-line call with no timeout= is flagged."""
        findings = csc.untimed_subprocess(tmp_path / "a.py", ["subprocess.run(['ls'])"])
        assert len(findings) == 1

    def test_single_line_with_timeout_passes(self, tmp_path: Path) -> None:
        """A one-line call carrying timeout= passes."""
        findings = csc.untimed_subprocess(tmp_path / "a.py", ["subprocess.run(['ls'], timeout=5)"])
        assert findings == []

    def test_multiline_call_with_timeout_passes(self, tmp_path: Path) -> None:
        """Timeout= two lines below the call still counts — the shell original missed this."""
        lines = [
            "    result = subprocess.run(",
            "        ['ls'],",
            "        timeout=30,",
            "    )",
        ]
        assert csc.untimed_subprocess(tmp_path / "a.py", lines) == []

    def test_multiline_call_without_timeout_flagged(self, tmp_path: Path) -> None:
        """A multi-line call genuinely missing timeout= is still reported."""
        lines = ["    result = subprocess.run(", "        ['ls'],", "    )"]
        assert len(csc.untimed_subprocess(tmp_path / "a.py", lines)) == 1

    def test_commented_call_ignored(self, tmp_path: Path) -> None:
        """A commented-out call is not a live call site."""
        assert csc.untimed_subprocess(tmp_path / "a.py", ["# subprocess.run(['ls'])"]) == []


class TestMissingTimeoutFlag:
    """Covers the --timeout argparse requirement."""

    def test_script_without_flag_flagged(self, tmp_path: Path) -> None:
        """A subprocess-using script with no --timeout argument is reported."""
        finding = csc.missing_timeout_flag(tmp_path / "a.py", "subprocess.run(['ls'], timeout=5)")
        assert finding is not None
        assert "--timeout argparse argument absent" in finding

    def test_script_with_flag_passes(self, tmp_path: Path) -> None:
        """Declaring --timeout satisfies the check."""
        text = 'parser.add_argument("--timeout")\nsubprocess.run(["ls"], timeout=5)'
        assert csc.missing_timeout_flag(tmp_path / "a.py", text) is None

    def test_script_without_subprocess_exempt(self, tmp_path: Path) -> None:
        """A script that never shells out needs no --timeout."""
        assert csc.missing_timeout_flag(tmp_path / "a.py", "x = 1\n") is None

    def test_docstring_mention_does_not_exempt(self, tmp_path: Path) -> None:
        """`--timeout` in the module docstring is not a declared flag.

        Every script under audit documents `[--timeout SECS]` in its docstring, so a substring test would exempt the
        whole population from the check.
        """
        text = '"""Usage: foo.py [--timeout SECS]"""\nimport subprocess\nsubprocess.run(["x"])\n'
        finding = csc.missing_timeout_flag(tmp_path / "a.py", text)
        assert finding is not None

    def test_multiline_add_argument_exempts(self, tmp_path: Path) -> None:
        """A wrapped `add_argument(` call still counts as declaring the flag."""
        text = 'parser.add_argument(\n    "--timeout",\n    type=float,\n)\nsubprocess.run(["x"], timeout=1)'
        assert csc.missing_timeout_flag(tmp_path / "a.py", text) is None


class TestShadowedMode:
    """Covers Check 32f."""

    def _pair(self, tmp_path: Path, mode_body: list[str], inline: list[str]) -> tuple[Path, Path]:
        skill_dir = tmp_path / "cc_x" / "skills" / "demo"
        (skill_dir / "modes").mkdir(parents=True)
        mode_file = skill_dir / "modes" / "team.md"
        mode_file.write_text("\n".join(mode_body) + "\n", encoding="utf-8")
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("loads: team.md\n" + "\n".join(inline) + "\n", encoding="utf-8")
        return skill_md, mode_file

    def test_shadow_detected(self, tmp_path: Path) -> None:
        """A referenced mode file duplicated inline is reported with its overlap count."""
        body = [f"line {i}" for i in range(30)]
        skill_md, mode_file = self._pair(tmp_path, body, body)
        finding = csc.shadowed_mode(skill_md, mode_file)
        assert finding is not None
        assert "30 overlapping lines" in finding

    def test_unreferenced_mode_ignored(self, tmp_path: Path) -> None:
        """A mode file the SKILL.md never names is Check 32a's business, not 32f's."""
        body = [f"line {i}" for i in range(30)]
        skill_dir = tmp_path / "cc_x" / "skills" / "demo"
        (skill_dir / "modes").mkdir(parents=True)
        mode_file = skill_dir / "modes" / "team.md"
        mode_file.write_text("\n".join(body), encoding="utf-8")
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("\n".join(body), encoding="utf-8")
        assert csc.shadowed_mode(skill_md, mode_file) is None

    def test_small_mode_file_skipped(self, tmp_path: Path) -> None:
        """A mode file under the size threshold is too small to judge."""
        body = [f"line {i}" for i in range(5)]
        skill_md, mode_file = self._pair(tmp_path, body, body)
        assert csc.shadowed_mode(skill_md, mode_file) is None

    def test_low_overlap_not_reported(self, tmp_path: Path) -> None:
        """A large mode file with only a few shared lines is not a shadow."""
        body = [f"line {i}" for i in range(30)]
        skill_md, mode_file = self._pair(tmp_path, body, body[:5])
        assert csc.shadowed_mode(skill_md, mode_file) is None

    def test_blank_and_comment_lines_excluded(self, tmp_path: Path) -> None:
        """Blanks and comments do not inflate the overlap count."""
        body = ["", "# note"] * 30
        skill_md, mode_file = self._pair(tmp_path, body, body)
        assert csc.shadowed_mode(skill_md, mode_file) is None


class TestCli:
    """Covers the command-line contract."""

    def test_check_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Omitting --check is an argparse error."""
        monkeypatch.setattr("sys.argv", ["check_skill_contracts.py"])
        with pytest.raises(SystemExit):
            csc.main()

    def test_32f_skipped_without_local(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Without a source tree, 32f reports a skip and exits 0."""
        monkeypatch.setattr("sys.argv", ["check_skill_contracts.py", "--check", "32f", "--root", str(tmp_path)])
        assert csc.main() == 0
        assert "skipped in non-local mode" in capsys.readouterr().out

    def test_23b_clean_tree_exits_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An empty scope produces the three banners and exits 0."""
        monkeypatch.setattr("sys.argv", ["check_skill_contracts.py", "--check", "23b", "--root", str(tmp_path)])
        assert csc.main() == 0
        assert "✓: Check 23b scan complete" in capsys.readouterr().out


class TestConfigFileDiscovery:
    """Covers the layouts `_config_files` must reach.

    The default call site passes `.claude`, whose files sit one level shallower than the source tree's. A depth-fixed
    glob matched neither that layout nor the nested `rules/_full/`, so the whole check passed over zero files.
    """

    def test_installed_layout_matched(self, tmp_path: Path) -> None:
        """`.claude/agents/a.md` and `.claude/skills/x/SKILL.md` are in scope."""
        claude = tmp_path / ".claude"
        (claude / "agents").mkdir(parents=True)
        (claude / "agents" / "a.md").write_text("x\n", encoding="utf-8")
        (claude / "rules").mkdir()
        (claude / "rules" / "r.md").write_text("x\n", encoding="utf-8")
        (claude / "skills" / "foo").mkdir(parents=True)
        (claude / "skills" / "foo" / "SKILL.md").write_text("x\n", encoding="utf-8")
        found = {p.name for p in csc._config_files(claude)}
        assert found == {"a.md", "r.md", "SKILL.md"}

    def test_source_tree_layout_matched(self, tmp_path: Path) -> None:
        """`plugins/cc_x/agents/a.md` is still in scope."""
        agents = tmp_path / "plugins" / "cc_x" / "agents"
        agents.mkdir(parents=True)
        (agents / "a.md").write_text("x\n", encoding="utf-8")
        assert [p.name for p in csc._config_files(tmp_path / "plugins")] == ["a.md"]

    def test_nested_rules_matched(self, tmp_path: Path) -> None:
        """`rules/_full/*.md` is scanned, as the `find` original scanned it."""
        full = tmp_path / "plugins" / "cc_x" / "rules" / "_full"
        full.mkdir(parents=True)
        (full / "deep.md").write_text("x\n", encoding="utf-8")
        assert [p.name for p in csc._config_files(tmp_path / "plugins")] == ["deep.md"]

    def test_unrelated_markdown_ignored(self, tmp_path: Path) -> None:
        """A README outside the three shapes is not a config file."""
        (tmp_path / "plugins" / "cc_x").mkdir(parents=True)
        (tmp_path / "plugins" / "cc_x" / "README.md").write_text("x\n", encoding="utf-8")
        assert csc._config_files(tmp_path / "plugins") == []

    def test_finding_reported_through_installed_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """End to end: a bare `# timeout:` under `.claude/` reaches stdout."""
        claude = tmp_path / ".claude"
        (claude / "agents").mkdir(parents=True)
        (claude / "agents" / "a.md").write_text("gh pr view 1  # timeout: 5000\n", encoding="utf-8")
        monkeypatch.setattr("sys.argv", ["check_skill_contracts.py", "--check", "23b", "--root", str(claude)])
        assert csc.main() == 0
        assert "a.md:1:" in capsys.readouterr().out
