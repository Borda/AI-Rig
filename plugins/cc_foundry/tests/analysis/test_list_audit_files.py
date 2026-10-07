"""Tests for list_audit_files bin script.

Covers the local and installed sweep patterns, the deliberate exclusions (references/, rules/_full/), fenced-block
counting, and the report layout.
"""

from __future__ import annotations

from pathlib import Path

import list_audit_files as inventory
import pytest


def _touch(path: Path, text: str = "x\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestCollect:
    """Covers pattern matching."""

    def test_local_sweep_finds_skill_entrypoint(self, tmp_path: Path) -> None:
        """A skills/<name>/SKILL.md is in the local sweep."""
        _touch(tmp_path / "cc_x" / "skills" / "audit" / "SKILL.md")
        found = inventory.collect(tmp_path, inventory.LOCAL_PATTERNS)
        assert [p.name for p in found] == ["SKILL.md"]

    def test_local_sweep_includes_modes_templates_shared(self, tmp_path: Path) -> None:
        """Modes/, templates/ and skills/_shared/ files are swept."""
        _touch(tmp_path / "cc_x" / "skills" / "audit" / "modes" / "fix.md")
        _touch(tmp_path / "cc_x" / "skills" / "audit" / "templates" / "t.md")
        _touch(tmp_path / "cc_x" / "skills" / "_shared" / "s.md")
        assert len(inventory.collect(tmp_path, inventory.LOCAL_PATTERNS)) == 3

    @pytest.mark.parametrize(
        ("files", "expected"),
        [
            pytest.param(
                ["cc_x/agents/curator.md", "cc_x/agents/curator/sidecar.md"],
                ["cc_x/agents/curator.md"],
                id="nested-agent-sidecars",
            ),
            pytest.param(["cc_x/rules/a.md", "cc_x/rules/_full/a.md"], ["cc_x/rules/a.md"], id="rules-full-long-form"),
        ],
    )
    def test_local_sweep_excludes(self, tmp_path: Path, files: list[str], expected: list[str]) -> None:
        """Nested agent sidecars and rules/_full/ long-form bodies stay out of the local sweep.

        Scenario: references-style nesting under agents/ and the rules/_full/ copy of a rule are both skipped, leaving
        only the flat file.
        """
        for rel in files:
            _touch(tmp_path / rel)
        found = inventory.collect(tmp_path, inventory.LOCAL_PATTERNS)
        assert [p.relative_to(tmp_path).as_posix() for p in found] == expected

    def test_installed_sweep_is_narrower(self, tmp_path: Path) -> None:
        """The installed sweep covers only skill entrypoints and flat agents."""
        _touch(tmp_path / "skills" / "audit" / "SKILL.md")
        _touch(tmp_path / "skills" / "audit" / "modes" / "fix.md")
        _touch(tmp_path / "agents" / "a.md")
        found = {p.name for p in inventory.collect(tmp_path, inventory.INSTALLED_PATTERNS)}
        assert found == {"SKILL.md", "a.md"}

    def test_results_deduplicated_and_sorted(self, tmp_path: Path) -> None:
        """A file matching two patterns appears once, and output is ordered."""
        _touch(tmp_path / "cc_x" / "skills" / "b" / "SKILL.md")
        _touch(tmp_path / "cc_x" / "skills" / "a" / "SKILL.md")
        found = inventory.collect(tmp_path, inventory.LOCAL_PATTERNS)
        assert found == sorted(found)
        assert len(found) == len(set(found))


class TestCountBlocks:
    """Covers fenced-block counting."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param("```bash\necho hi\n```\n", 1, id="one-pair-one-block"),
            pytest.param("```bash\nx\n```\n\n```python\ny\n```\n", 2, id="two-pairs-two-blocks"),
            pytest.param("# Title\ntext\n", 0, id="prose-without-fences"),
        ],
    )
    def test_counts_fenced_pairs(self, tmp_path: Path, text: str, expected: int) -> None:
        """Each open/close fence pair is one block; prose with no fences counts zero."""
        assert inventory.count_blocks(_touch(tmp_path / "a.md", text)) == expected

    def test_unreadable_file_counts_zero(self, tmp_path: Path) -> None:
        """A missing file counts zero rather than raising."""
        assert inventory.count_blocks(tmp_path / "absent.md") == 0


class TestReport:
    """Covers the printed table."""

    def test_header_and_relative_labels(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Rows are labelled relative to the sweep root."""
        path = _touch(tmp_path / "cc_x" / "agents" / "a.md", "```bash\nx\n```\n")
        inventory.report([path], tmp_path)
        out = capsys.readouterr().out
        assert out.splitlines()[0].startswith("FILE")
        assert "cc_x/agents/a.md" in out
        assert out.rstrip().endswith("1")

    def test_path_outside_root_falls_back_to_full_path(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A file outside the root is labelled by its own path, not crashed on."""
        outside = _touch(tmp_path / "elsewhere" / "a.md")
        inventory.report([outside], tmp_path / "root")
        assert outside.as_posix() in capsys.readouterr().out


class TestCli:
    """Covers argument handling."""

    def test_local_mode_uses_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--local sweeps --root with the source-tree patterns."""
        _touch(tmp_path / "cc_x" / "agents" / "a.md")
        monkeypatch.setattr("sys.argv", ["list_audit_files.py", "--local", "--root", str(tmp_path)])
        assert inventory.main() == 0
        assert "cc_x/agents/a.md" in capsys.readouterr().out

    def test_installed_mode_uses_claude_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Without --local the sweep targets --claude-dir."""
        _touch(tmp_path / "agents" / "a.md")
        monkeypatch.setattr("sys.argv", ["list_audit_files.py", "--claude-dir", str(tmp_path)])
        assert inventory.main() == 0
        assert "agents/a.md" in capsys.readouterr().out
