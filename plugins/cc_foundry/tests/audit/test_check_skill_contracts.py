"""Tests for check_skill_contracts bin script.

Covers Check 23b's three scans — including the multi-line subprocess call the line-based shell original could not see —
and Check 32f's shadow detection with its size and overlap thresholds.
"""

from __future__ import annotations

from pathlib import Path

import check_skill_contracts as csc
import pytest


class TestUnenforcedTimeouts:
    """Covers the `# timeout:` comment scan."""

    @pytest.mark.parametrize(
        ("lines", "expected"),
        [
            pytest.param(["gh pr view 1  # timeout: 5000"], 1, id="bare-comment-flagged"),
            pytest.param(["timeout 5 gh pr view 1  # timeout: 5000"], 0, id="shell-timeout-prefix-exempts"),
            # Python scripts enforce internally via their own --timeout default.
            pytest.param(['python "x/bin/y.py"  # timeout: 5000'], 0, id="python-line-exempt"),
            pytest.param(["# timeout: 5000 is the convention"], 0, id="comment-only-line-ignored"),
        ],
    )
    def test_timeout_comment_needs_real_enforcement(self, tmp_path: Path, lines: list[str], expected: int) -> None:
        """A `# timeout:` comment is flagged unless a real `timeout S` prefix or a Python script enforces it.

        A command with only the comment beside it is reported; a ``timeout`` prefix and a Python invocation satisfy the
        check, and a line that is entirely a comment is prose rather than a command.
        """
        assert len(csc.unenforced_timeouts(tmp_path / "a.md", lines)) == expected


class TestUntimedSubprocess:
    """Covers the subprocess timeout= scan."""

    @pytest.mark.parametrize(
        ("lines", "expected"),
        [
            pytest.param(["subprocess.run(['ls'])"], 1, id="single-line-without-timeout"),
            pytest.param(["subprocess.run(['ls'], timeout=5)"], 0, id="single-line-with-timeout"),
            # The shell original missed a timeout= two lines below the call.
            pytest.param(
                ["    result = subprocess.run(", "        ['ls'],", "        timeout=30,", "    )"],
                0,
                id="multiline-with-timeout",
            ),
            pytest.param(
                ["    result = subprocess.run(", "        ['ls'],", "    )"], 1, id="multiline-without-timeout"
            ),
            pytest.param(["# subprocess.run(['ls'])"], 0, id="commented-out-call-ignored"),
        ],
    )
    def test_subprocess_call_needs_timeout(self, tmp_path: Path, lines: list[str], expected: int) -> None:
        """A live subprocess call is flagged exactly when no ``timeout=`` appears within the call.

        Covers one-line and multi-line calls with and without ``timeout=``, and a commented-out call that is not a live
        call site.
        """
        assert len(csc.untimed_subprocess(tmp_path / "a.py", lines)) == expected


class TestMissingTimeoutFlag:
    """Covers the --timeout argparse requirement."""

    def test_script_without_flag_flagged(self, tmp_path: Path) -> None:
        """A subprocess-using script with no --timeout argument is reported."""
        finding = csc.missing_timeout_flag(tmp_path / "a.py", "subprocess.run(['ls'], timeout=5)")
        assert finding is not None
        assert "--timeout argparse argument absent" in finding

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param('parser.add_argument("--timeout")\nsubprocess.run(["ls"], timeout=5)', id="declared-flag"),
            pytest.param("x = 1\n", id="script-never-shells-out"),
            pytest.param(
                'parser.add_argument(\n    "--timeout",\n    type=float,\n)\nsubprocess.run(["x"], timeout=1)',
                id="wrapped-add-argument-call",
            ),
        ],
    )
    def test_script_with_flag_or_without_subprocess_passes(self, tmp_path: Path, text: str) -> None:
        """Declaring --timeout (even in a wrapped call) or never shelling out satisfies the check."""
        assert csc.missing_timeout_flag(tmp_path / "a.py", text) is None

    def test_docstring_mention_does_not_exempt(self, tmp_path: Path) -> None:
        """`--timeout` in the module docstring is not a declared flag.

        Every script under audit documents `[--timeout SECS]` in its docstring, so a substring test would exempt the
        whole population from the check.
        """
        text = '"""Usage: foo.py [--timeout SECS]"""\nimport subprocess\nsubprocess.run(["x"])\n'
        finding = csc.missing_timeout_flag(tmp_path / "a.py", text)
        assert finding is not None


_LINES = [f"line {i}" for i in range(30)]


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

    @pytest.mark.parametrize(
        ("mode_body", "inline"),
        [
            # A mode file under the size threshold is too small to judge.
            pytest.param(_LINES[:5], _LINES[:5], id="small-mode-file"),
            # A large mode file with only a few shared lines is not a shadow.
            pytest.param(_LINES, _LINES[:5], id="low-overlap"),
            # Blanks and comments do not inflate the overlap count.
            pytest.param(["", "# note"] * 30, ["", "# note"] * 30, id="blank-and-comment-lines-excluded"),
        ],
    )
    def test_mode_not_reported_as_shadow(self, tmp_path: Path, mode_body: list[str], inline: list[str]) -> None:
        """A referenced mode file is not a shadow when it is too small, barely overlaps, or overlaps only on filler.

        Covers a mode file under the size threshold, a large one sharing only a few lines with the SKILL.md, and one
        whose duplicated lines are all blanks or comments.
        """
        skill_md, mode_file = self._pair(tmp_path, mode_body, inline)
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

    @pytest.mark.parametrize(
        ("subdir", "name"),
        [
            pytest.param("agents", "a.md", id="source-tree-agents"),
            # The `find` original scanned `rules/_full/*.md`, so the nested directory stays in scope.
            pytest.param("rules/_full", "deep.md", id="nested-rules"),
        ],
    )
    def test_source_tree_layout_matched(self, tmp_path: Path, subdir: str, name: str) -> None:
        """Markdown under `plugins/cc_x/agents` and the nested `plugins/cc_x/rules/_full` stays in scope."""
        directory = tmp_path / "plugins" / "cc_x" / subdir
        directory.mkdir(parents=True)
        (directory / name).write_text("x\n", encoding="utf-8")
        assert [p.name for p in csc._config_files(tmp_path / "plugins")] == [name]

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
