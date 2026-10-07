"""Tests for scan_efficiency_signals bin script.

Covers frontmatter scoping, unbounded-spawn detection and its batch-guard exemption, the model-declaration scan, and the
counted boilerplate sections.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import scan_efficiency_signals as ses


class TestFrontmatter:
    """Covers the frontmatter slice."""

    def test_extracts_block_between_delimiters(self) -> None:
        """Only the text between the first two `---` lines is returned."""
        text = "---\nname: x\nmodel: opus\n---\nbody model: sonnet\n"
        assert "model: opus" in ses.frontmatter(text)
        assert "body" not in ses.frontmatter(text)

    def test_no_frontmatter_returns_empty(self) -> None:
        """A file that does not open with `---` has no frontmatter."""
        assert ses.frontmatter("# Heading\nmodel: opus\n") == ""


class TestUnboundedSpawns:
    """Covers the Agent()-in-loop detector."""

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("for f in $FILES; do\n  echo x\n  Agent(subagent_type='x')\ndone\n", id="agent-in-for-loop"),
            pytest.param("while read -r f; do\n  Agent(subagent_type='x')\ndone\n", id="agent-in-while-loop"),
        ],
    )
    def test_agent_in_loop_flagged(self, tmp_path: Path, text: str) -> None:
        """An Agent( call a few lines below a `for` or `while` is flagged; both loop kinds count the same."""
        finding = ses.unbounded_spawns(tmp_path / "s.md", text)
        assert finding is not None
        assert "UNBOUNDED_SPAWN" in finding

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param(
                "BATCH_SIZE=5\nfor f in $FILES; do\n  Agent(subagent_type='x')\ndone\n", id="batch-size-guard-exempts"
            ),
            pytest.param(
                "files=$(ls | head -n 5)\nfor f in $files; do\n  Agent(x)\ndone\n", id="head-limit-counts-as-guard"
            ),
            pytest.param("echo hi\nAgent(subagent_type='x')\n", id="agent-outside-loop"),
            pytest.param(
                "for f in x; do\n" + "  echo y\n" * 10 + "  Agent(x)\ndone\n", id="loop-beyond-lookbehind-window"
            ),
        ],
    )
    def test_spawn_not_flagged(self, tmp_path: Path, text: str) -> None:
        """No finding when a batch guard exempts the file, no loop sits above the call, or the loop is out of range.

        Scenario: a BATCH_SIZE guard anywhere in the file exempts it; a `head -n 5` cap counts as a guard; an Agent(
        call with no loop above it is not a finding; a loop more than the lookbehind window away does not count.
        """
        assert ses.unbounded_spawns(tmp_path / "s.md", text) is None


class TestMissingModel:
    """Covers the model-declaration scan."""

    def test_declared_model_passes(self, tmp_path: Path) -> None:
        """Frontmatter with a model line yields no finding."""
        assert ses.missing_model(tmp_path / "a.md", "---\nname: x\nmodel: opus\n---\n") is None

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("---\nname: x\n---\n", id="absent-model"),
            pytest.param("---\nname: x\n---\nThe agent uses\nmodel: opus\n", id="model-in-body-does-not-count"),
        ],
    )
    def test_undeclared_model_flagged(self, tmp_path: Path, text: str) -> None:
        """Frontmatter without a model line is flagged, and a `model:` line in prose below it is not a declaration."""
        finding = ses.missing_model(tmp_path / "a.md", text)
        assert finding is not None
        assert finding.startswith("NO_MODEL:")


class TestDeclaresModel:
    """Covers which files are required to declare a tier."""

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            pytest.param("plugins/x/skills/audit/SKILL.md", True, id="skill-md-included"),
            pytest.param("plugins/x/agents/curator.md", True, id="agent-file-included"),
            pytest.param("plugins/x/skills/audit/modes/fix.md", False, id="mode-file-excluded"),
        ],
    )
    def test_tier_declaration_scope(self, path: str, expected: bool) -> None:
        """A SKILL.md and anything under agents/ must declare a tier; a modes/ file carries none and is out of scope."""
        assert ses._declares_model(Path(path)) is expected


class TestScan:
    """Covers the whole-directory report."""

    def test_sections_printed_in_order(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """All four section banners appear, in the consolidator's expected order."""
        (tmp_path / "a.md").write_text("---\nname: x\n---\n", encoding="utf-8")
        ses.scan(tmp_path)
        out = capsys.readouterr().out
        order = [
            out.index("=== Unbounded spawn patterns ==="),
            out.index("=== Missing model declarations ==="),
            out.index("=== Boilerplate duplication ==="),
            out.index("=== Bin/ extraction candidates ==="),
        ]
        assert order == sorted(order)

    def test_boilerplate_counts_files_not_hits(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Two occurrences in one file count as one file."""
        (tmp_path / "a.md").write_text("Unknown flag\nUnknown flag\n", encoding="utf-8")
        ses.scan(tmp_path)
        assert "unsupported-flag-check boilerplate: 1 files" in capsys.readouterr().out

    def test_missing_model_reported_for_skill(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A SKILL.md without a model line appears in the report."""
        skill = tmp_path / "skills" / "demo"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
        ses.scan(tmp_path)
        assert "NO_MODEL:" in capsys.readouterr().out

    def test_cli_exit_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The scan is a report, never a gate — it always exits 0."""
        monkeypatch.setattr("sys.argv", ["scan_efficiency_signals.py", "--scan-dir", str(tmp_path)])
        assert ses.main() == 0
        assert "Boilerplate duplication" in capsys.readouterr().out
