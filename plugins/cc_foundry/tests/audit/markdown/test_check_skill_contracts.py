"""Tests for check_skill_contracts bin script.

Covers Check 23b's three scans — including the multi-line subprocess call the line-based shell original could not see —
and Check 32f's shadow detection with its size and overlap thresholds. Everything runs against files built under
``tmp_path``; no test spawns a process.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import check_skill_contracts as csc
import pytest

_LINES = [f"line {i}" for i in range(30)]
_LOADS_TEAM = "loads: team.md\n"


def _write_files(root: Path, *relative: str) -> None:
    """Create each ``relative`` path under ``root`` as a one-line Markdown file, making parent directories."""
    for name in relative:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")


@pytest.fixture
def run_cli(monkeypatch: pytest.MonkeyPatch) -> Callable[..., int]:
    """Return a runner that sets ``sys.argv`` to the given arguments and returns ``main()``'s exit code."""

    def run(*args: str) -> int:
        monkeypatch.setattr("sys.argv", ["check_skill_contracts.py", *args])
        return csc.main()

    return run


@pytest.fixture
def shadow_pair(tmp_path: Path) -> Callable[..., tuple[Path, Path]]:
    """Return a factory building a skill dir with ``modes/team.md`` and a SKILL.md; yields ``(skill_md, mode_file)``."""

    def build(mode_body: list[str], inline: list[str], header: str = _LOADS_TEAM) -> tuple[Path, Path]:
        skill_dir = tmp_path / "cc_x" / "skills" / "demo"
        (skill_dir / "modes").mkdir(parents=True)
        mode_file = skill_dir / "modes" / "team.md"
        mode_file.write_text("\n".join(mode_body) + "\n", encoding="utf-8")
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text(header + "\n".join(inline) + "\n", encoding="utf-8")
        return skill_md, mode_file

    return build


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

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("subprocess.run(['ls'], timeout=5)", id="flag-never-declared"),
            # Every script under audit documents `[--timeout SECS]` in its docstring, so a substring test would exempt
            # the whole population from the check.
            pytest.param(
                '"""Usage: foo.py [--timeout SECS]"""\nimport subprocess\nsubprocess.run(["x"])\n',
                id="docstring-mention-does-not-exempt",
            ),
        ],
    )
    def test_subprocess_script_without_declared_flag_is_flagged(self, tmp_path: Path, text: str) -> None:
        """A subprocess-using script with no declared --timeout argument is reported.

        Mentioning the flag in prose, such as the module docstring, is not a declaration.
        """
        finding = csc.missing_timeout_flag(tmp_path / "a.py", text)
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
        """Declaring --timeout (even in a wrapped call) or never shelling out satisfies the check.

        The requirement only applies to scripts that spawn processes, and a declaration split across lines still counts.
        """
        assert csc.missing_timeout_flag(tmp_path / "a.py", text) is None


class TestShadowedMode:
    """Covers Check 32f."""

    def test_shadow_detected(self, shadow_pair: Callable[..., tuple[Path, Path]]) -> None:
        """A referenced mode file duplicated inline is reported with its overlap count.

        The SKILL.md names ``team.md`` and also repeats all 30 of its lines, so the mode file is dead weight.
        """
        skill_md, mode_file = shadow_pair(_LINES, _LINES)
        finding = csc.shadowed_mode(skill_md, mode_file)
        assert finding is not None
        assert "30 overlapping lines" in finding

    @pytest.mark.parametrize(
        ("mode_body", "inline", "header"),
        [
            # A mode file under the size threshold is too small to judge.
            pytest.param(_LINES[:5], _LINES[:5], _LOADS_TEAM, id="small-mode-file"),
            # A large mode file with only a few shared lines is not a shadow.
            pytest.param(_LINES, _LINES[:5], _LOADS_TEAM, id="low-overlap"),
            # Blanks and comments do not inflate the overlap count.
            pytest.param(["", "# note"] * 30, ["", "# note"] * 30, _LOADS_TEAM, id="blank-and-comment-lines-excluded"),
            # A mode file the SKILL.md never names is Check 32a's business, not 32f's.
            pytest.param(_LINES, _LINES, "", id="unreferenced-mode"),
        ],
    )
    def test_mode_not_reported_as_shadow(
        self, shadow_pair: Callable[..., tuple[Path, Path]], mode_body: list[str], inline: list[str], header: str
    ) -> None:
        """A mode file is not a shadow when it is too small, barely overlaps, overlaps only on filler, or is unnamed.

        Covers a mode file under the size threshold, a large one sharing only a few lines with the SKILL.md, one whose
        duplicated lines are all blanks or comments, and a fully duplicated one the SKILL.md never references.
        """
        skill_md, mode_file = shadow_pair(mode_body, inline, header)
        assert csc.shadowed_mode(skill_md, mode_file) is None


class TestCli:
    """Covers the command-line contract."""

    def test_check_required(self, run_cli: Callable[..., int]) -> None:
        """Omitting --check is an argparse error.

        The check selector has no default, so a bare invocation must stop with ``SystemExit`` rather than run nothing.
        """
        with pytest.raises(SystemExit):
            run_cli()

    @pytest.mark.parametrize(
        ("check", "expected_line"),
        [
            pytest.param("32f", "skipped in non-local mode", id="32f-skipped-without-source-tree"),
            pytest.param("23b", "✓: Check 23b scan complete", id="23b-clean-tree"),
        ],
    )
    def test_empty_root_exits_zero(
        self,
        tmp_path: Path,
        run_cli: Callable[..., int],
        capsys: pytest.CaptureFixture[str],
        check: str,
        expected_line: str,
    ) -> None:
        """An empty root exits 0: 32f reports a skip without a source tree, 23b reports a clean scan.

        With nothing under ``--root`` there are no findings to raise, but each check must still announce its outcome.
        """
        assert run_cli("--check", check, "--root", str(tmp_path)) == 0
        assert expected_line in capsys.readouterr().out


class TestConfigFileDiscovery:
    """Covers the layouts `_config_files` must reach.

    The default call site passes `.claude`, whose files sit one level shallower than the source tree's. A depth-fixed
    glob matched neither that layout nor the nested `rules/_full/`, so the whole check passed over zero files.
    """

    @pytest.mark.parametrize(
        ("root", "files", "expected"),
        [
            pytest.param(
                ".claude",
                (".claude/agents/a.md", ".claude/rules/r.md", ".claude/skills/foo/SKILL.md"),
                {"a.md", "r.md", "SKILL.md"},
                id="installed-layout",
            ),
            pytest.param("plugins", ("plugins/cc_x/agents/a.md",), {"a.md"}, id="source-tree-agents"),
            # The `find` original scanned `rules/_full/*.md`, so the nested directory stays in scope.
            pytest.param("plugins", ("plugins/cc_x/rules/_full/deep.md",), {"deep.md"}, id="nested-rules"),
            pytest.param("plugins", ("plugins/cc_x/README.md",), set(), id="unrelated-markdown-ignored"),
        ],
    )
    def test_markdown_in_known_layouts_is_in_scope(
        self, tmp_path: Path, root: str, files: tuple[str, ...], expected: set[str]
    ) -> None:
        """Agent, rule and skill Markdown is found in the installed and source layouts; other Markdown is not.

        `.claude/agents`, `.claude/rules`, `.claude/skills/*/SKILL.md`, `plugins/cc_x/agents` and the nested
        `plugins/cc_x/rules/_full` are config files, while a plugin README outside those shapes is not.
        """
        _write_files(tmp_path, *files)
        assert {p.name for p in csc._config_files(tmp_path / root)} == expected

    def test_finding_reported_through_installed_root(
        self, tmp_path: Path, run_cli: Callable[..., int], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """End to end: a bare `# timeout:` under `.claude/` reaches stdout.

        The agent file sits in the installed layout and holds a command with only a `# timeout:` comment; the scan must
        reach it and print its location.
        """
        claude = tmp_path / ".claude"
        (claude / "agents").mkdir(parents=True)
        (claude / "agents" / "a.md").write_text("gh pr view 1  # timeout: 5000\n", encoding="utf-8")
        assert run_cli("--check", "23b", "--root", str(claude)) == 0
        assert "a.md:1:" in capsys.readouterr().out
