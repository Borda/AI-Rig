"""Tests for measure_config_size bin script.

Covers the inventory table, the always-loaded overhead totals, per-kind and per-file thresholds, and the CLI mode
switch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import measure_config_size as mcs


def _write(path: Path, size: int) -> None:
    """Create a file of exactly `size` bytes, newline-terminated."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * (size - 1) + b"\n")


class TestInventory:
    """Covers --mode inventory."""

    def test_empty_claude_dir_prints_header_only(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """No agents, skills or rules means header row and nothing else."""
        mcs.report_inventory(tmp_path / ".claude")
        out = capsys.readouterr().out.strip().splitlines()
        assert len(out) == 1
        assert out[0].startswith("FILE")

    def test_small_agent_listed_not_flagged(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """An agent under budget appears as a plain row."""
        claude = tmp_path / ".claude"
        _write(claude / "agents" / "small.md", 300)
        mcs.report_inventory(claude)
        out = capsys.readouterr().out
        assert "agents/small.md" in out
        assert "OVER BUDGET" not in out

    def test_oversized_agent_flagged_and_fails(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """An agent over the 4 k token budget is flagged and exits non-zero."""
        claude = tmp_path / ".claude"
        _write(claude / "agents" / "big.md", mcs.BUDGETS["agents"] * 3 + 10)
        mcs.report_inventory(claude)
        assert "⚠ OVER BUDGET: agents/big.md" in capsys.readouterr().out

    def test_skill_path_label_uses_parent_dir(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A SKILL.md is labelled by its containing skill directory."""
        claude = tmp_path / ".claude"
        _write(claude / "skills" / "audit" / "SKILL.md", 100)
        mcs.report_inventory(claude)
        assert "skills/audit/SKILL.md" in capsys.readouterr().out

    def test_rules_budget_is_tighter_than_agents(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A file that passes as an agent fails as a rule."""
        claude = tmp_path / ".claude"
        size = mcs.BUDGETS["rules"] * 3 + 10
        _write(claude / "rules" / "r.md", size)
        _write(claude / "agents" / "a.md", size)
        mcs.report_inventory(claude)
        out = capsys.readouterr().out
        assert "OVER BUDGET: rules/r.md" in out
        assert "OVER BUDGET: agents/a.md" not in out


class TestOverhead:
    """Covers --mode overhead."""

    def test_small_config_passes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A tiny config reports OK and exits 0."""
        project = tmp_path / "CLAUDE.md"
        _write(project, 100)
        mcs.report_overhead(tmp_path / ".claude", project, tmp_path / "global")
        assert "✓ OK Check 34" in capsys.readouterr().out

    def test_warn_threshold(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Between 50 KB and 100 KB warns without failing."""
        project = tmp_path / "CLAUDE.md"
        _write(project, mcs.OVERHEAD_WARN_BYTES + 10)
        mcs.report_overhead(tmp_path / ".claude", project, tmp_path / "global")
        assert "⚠ WARN Check 34a" in capsys.readouterr().out

    def test_fail_threshold(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Over 100 KB fails."""
        project = tmp_path / "CLAUDE.md"
        _write(project, mcs.OVERHEAD_FAIL_BYTES + 10)
        mcs.report_overhead(tmp_path / ".claude", project, tmp_path / "global")
        assert "! FAIL Check 34a" in capsys.readouterr().out

    def test_global_claude_counted_once(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Global CLAUDE.md contributes its own size, not twice."""
        global_dir = tmp_path / "global"
        _write(global_dir / "CLAUDE.md", 3000)
        mcs.report_overhead(tmp_path / ".claude", tmp_path / "absent.md", global_dir)
        assert "Global ~/.claude/:  3000 bytes" in capsys.readouterr().out

    def test_oversized_rules_file_fails(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A rules file over 10 KB fails even when the total is small."""
        claude = tmp_path / ".claude"
        _write(claude / "rules" / "big.md", mcs.RULES_FAIL_BYTES + 10)
        mcs.report_overhead(claude, tmp_path / "absent.md", tmp_path / "global")
        assert "! FAIL Check 34b" in capsys.readouterr().out

    def test_rules_file_between_thresholds_warns(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A rules file between 5 KB and 10 KB warns without failing."""
        claude = tmp_path / ".claude"
        _write(claude / "rules" / "mid.md", mcs.RULES_WARN_BYTES + 10)
        mcs.report_overhead(claude, tmp_path / "absent.md", tmp_path / "global")
        assert "⚠ WARN Check 34b" in capsys.readouterr().out


class TestCli:
    """Covers argument handling."""

    def test_mode_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Omitting --mode is an argparse error, not a silent default."""
        monkeypatch.setattr("sys.argv", ["measure_config_size.py"])
        with pytest.raises(SystemExit):
            mcs.main()

    def test_inventory_mode_dispatches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--mode inventory prints the table header."""
        monkeypatch.setattr(
            "sys.argv",
            ["measure_config_size.py", "--mode", "inventory", "--claude-dir", str(tmp_path)],
        )
        assert mcs.main() == 0
        assert "~TOKENS" in capsys.readouterr().out

    def test_overhead_mode_dispatches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--mode overhead prints the Check 34 banner."""
        monkeypatch.setattr(
            "sys.argv",
            [
                "measure_config_size.py",
                "--mode",
                "overhead",
                "--claude-dir",
                str(tmp_path),
                "--project-claude",
                str(tmp_path / "absent.md"),
                "--global-dir",
                str(tmp_path / "global"),
            ],
        )
        assert mcs.main() == 0
        assert "Check 34: Config token overhead" in capsys.readouterr().out
