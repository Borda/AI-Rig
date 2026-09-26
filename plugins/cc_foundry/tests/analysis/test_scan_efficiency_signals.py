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

    def test_agent_in_for_loop_flagged(self, tmp_path: Path) -> None:
        """An Agent( call a few lines below a `for` is flagged."""
        text = "for f in $FILES; do\n  echo x\n  Agent(subagent_type='x')\ndone\n"
        finding = ses.unbounded_spawns(tmp_path / "s.md", text)
        assert finding is not None
        assert "UNBOUNDED_SPAWN" in finding

    def test_agent_in_while_loop_flagged(self, tmp_path: Path) -> None:
        """A `while` loop counts the same as `for`."""
        text = "while read -r f; do\n  Agent(subagent_type='x')\ndone\n"
        assert ses.unbounded_spawns(tmp_path / "s.md", text) is not None

    def test_batch_guard_exempts(self, tmp_path: Path) -> None:
        """A BATCH_SIZE guard anywhere in the file exempts the file."""
        text = "BATCH_SIZE=5\nfor f in $FILES; do\n  Agent(subagent_type='x')\ndone\n"
        assert ses.unbounded_spawns(tmp_path / "s.md", text) is None

    def test_head_limit_exempts(self, tmp_path: Path) -> None:
        """A `head -n 5` cap counts as a batch guard."""
        text = "files=$(ls | head -n 5)\nfor f in $files; do\n  Agent(x)\ndone\n"
        assert ses.unbounded_spawns(tmp_path / "s.md", text) is None

    def test_agent_outside_loop_not_flagged(self, tmp_path: Path) -> None:
        """An Agent( call with no loop above it is not a finding."""
        text = "echo hi\nAgent(subagent_type='x')\n"
        assert ses.unbounded_spawns(tmp_path / "s.md", text) is None

    def test_loop_far_above_not_flagged(self, tmp_path: Path) -> None:
        """A loop more than the lookbehind window away does not count."""
        text = "for f in x; do\n" + "  echo y\n" * 10 + "  Agent(x)\ndone\n"
        assert ses.unbounded_spawns(tmp_path / "s.md", text) is None


class TestMissingModel:
    """Covers the model-declaration scan."""

    def test_declared_model_passes(self, tmp_path: Path) -> None:
        """Frontmatter with a model line yields no finding."""
        assert ses.missing_model(tmp_path / "a.md", "---\nname: x\nmodel: opus\n---\n") is None

    def test_absent_model_flagged(self, tmp_path: Path) -> None:
        """Frontmatter without a model line is flagged."""
        finding = ses.missing_model(tmp_path / "a.md", "---\nname: x\n---\n")
        assert finding is not None
        assert finding.startswith("NO_MODEL:")

    def test_model_in_body_does_not_count(self, tmp_path: Path) -> None:
        """A `model:` line in prose below the frontmatter is not a declaration."""
        text = "---\nname: x\n---\nThe agent uses\nmodel: opus\n"
        assert ses.missing_model(tmp_path / "a.md", text) is not None


class TestDeclaresModel:
    """Covers which files are required to declare a tier."""

    def test_skill_md_included(self) -> None:
        """A skills/<name>/SKILL.md is in scope."""
        assert ses._declares_model(Path("plugins/x/skills/audit/SKILL.md")) is True

    def test_agent_file_included(self) -> None:
        """Anything under an agents/ directory is in scope."""
        assert ses._declares_model(Path("plugins/x/agents/curator.md")) is True

    def test_mode_file_excluded(self) -> None:
        """A modes/ file carries no frontmatter tier and is out of scope."""
        assert ses._declares_model(Path("plugins/x/skills/audit/modes/fix.md")) is False


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
