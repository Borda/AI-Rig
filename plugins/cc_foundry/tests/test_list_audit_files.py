"""Tests for list_audit_files bin script.

Covers the local and installed sweep patterns, the deliberate exclusions (references/, rules/_full/), fenced-block
counting, and the report layout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import list_audit_files as inventory


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

    def test_local_sweep_excludes_nested_agent_sidecars(self, tmp_path: Path) -> None:
        """References-style nesting under agents/ stays out of the sweep."""
        _touch(tmp_path / "cc_x" / "agents" / "curator.md")
        _touch(tmp_path / "cc_x" / "agents" / "curator" / "sidecar.md")
        found = inventory.collect(tmp_path, inventory.LOCAL_PATTERNS)
        assert [p.name for p in found] == ["curator.md"]

    def test_local_sweep_excludes_rules_full(self, tmp_path: Path) -> None:
        """rules/_full/ long-form bodies are not part of the sweep."""
        _touch(tmp_path / "cc_x" / "rules" / "a.md")
        _touch(tmp_path / "cc_x" / "rules" / "_full" / "a.md")
        found = inventory.collect(tmp_path, inventory.LOCAL_PATTERNS)
        assert [p.parent.name for p in found] == ["rules"]

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

    def test_single_block(self, tmp_path: Path) -> None:
        """One open/close pair is one block."""
        path = _touch(tmp_path / "a.md", "```bash\necho hi\n```\n")
        assert inventory.count_blocks(path) == 1

    def test_two_blocks(self, tmp_path: Path) -> None:
        """Two pairs are two blocks."""
        path = _touch(tmp_path / "a.md", "```bash\nx\n```\n\n```python\ny\n```\n")
        assert inventory.count_blocks(path) == 2

    def test_no_blocks(self, tmp_path: Path) -> None:
        """Prose with no fences counts zero."""
        assert inventory.count_blocks(_touch(tmp_path / "a.md", "# Title\ntext\n")) == 0

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
