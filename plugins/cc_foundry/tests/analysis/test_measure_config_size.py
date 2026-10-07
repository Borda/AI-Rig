"""Tests for measure_config_size bin script.

Covers the inventory table, the always-loaded overhead totals, per-kind and per-file thresholds, and the CLI mode
switch.
"""

from __future__ import annotations

from pathlib import Path

import measure_config_size as mcs
import pytest


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

    @pytest.mark.parametrize(
        ("size", "expected"),
        [
            pytest.param(100, "✓ OK Check 34", id="small-config-ok"),
            pytest.param(mcs.OVERHEAD_WARN_BYTES + 10, "⚠ WARN Check 34a", id="warn-threshold"),
            pytest.param(mcs.OVERHEAD_FAIL_BYTES + 10, "! FAIL Check 34a", id="fail-threshold"),
        ],
    )
    def test_total_size_verdict(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], size: int, expected: str
    ) -> None:
        """The total overhead is graded by size.

        Scenario: a tiny config reports OK and exits 0; between 50 KB and 100 KB warns without failing; over 100 KB
        fails.
        """
        project = tmp_path / "CLAUDE.md"
        _write(project, size)
        mcs.report_overhead(tmp_path / ".claude", project, tmp_path / "global")
        assert expected in capsys.readouterr().out

    def test_global_claude_counted_once(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Global CLAUDE.md contributes its own size, not twice."""
        global_dir = tmp_path / "global"
        _write(global_dir / "CLAUDE.md", 3000)
        mcs.report_overhead(tmp_path / ".claude", tmp_path / "absent.md", global_dir)
        assert "Global ~/.claude/:  3000 bytes" in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("size", "expected"),
        [
            pytest.param(mcs.RULES_FAIL_BYTES + 10, "! FAIL Check 34b", id="over-10kb-fails"),
            pytest.param(mcs.RULES_WARN_BYTES + 10, "⚠ WARN Check 34b", id="between-thresholds-warns"),
        ],
    )
    def test_rules_file_size_verdict(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], size: int, expected: str
    ) -> None:
        """A single rules file is graded by size even when the total is small.

        Scenario: a rules file over 10 KB fails; one between 5 KB and 10 KB warns without failing.
        """
        claude = tmp_path / ".claude"
        _write(claude / "rules" / "file.md", size)
        mcs.report_overhead(claude, tmp_path / "absent.md", tmp_path / "global")
        assert expected in capsys.readouterr().out

    def test_user_rules_dir_counted(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Rules delivered to the user directory count toward the total.

        Every plugin's setup skill symlinks its rules into `~/.claude/rules/`, and a project `.claude/` usually has no
        `rules/` at all. Counting only the project directory reported `Rules dir total: 0 bytes` on a normal machine, so
        the bulk of the always-loaded set never reached the Check 34a total.
        """
        global_dir = tmp_path / "global"
        _write(global_dir / "rules" / "delivered.md", 4000)
        mcs.report_overhead(tmp_path / ".claude", tmp_path / "absent.md", global_dir)
        assert "Rules dir total:    4000 bytes" in capsys.readouterr().out

    def test_user_rules_file_flagged_by_size(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A per-file threshold applies to a delivered rule, not only a project one.

        The oversized rules this check exists to surface are the delivered ones; flagging only project rules left every
        real offender unreported.
        """
        global_dir = tmp_path / "global"
        _write(global_dir / "rules" / "big.md", mcs.RULES_FAIL_BYTES + 10)
        mcs.report_overhead(tmp_path / ".claude", tmp_path / "absent.md", global_dir)
        assert "! FAIL Check 34b" in capsys.readouterr().out

    def test_symlinked_rule_counted_once(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A project rule symlinked to a delivered one contributes its bytes once.

        Counting both directories naively would double-count exactly the files the setup skills link, inflating the
        figure the gate compares against 100 KB.
        """
        global_dir = tmp_path / "global"
        target = global_dir / "rules" / "shared.md"
        _write(target, 3000)
        link_dir = tmp_path / ".claude" / "rules"
        link_dir.mkdir(parents=True, exist_ok=True)
        try:
            (link_dir / "shared.md").symlink_to(target)
        except OSError:
            pytest.skip("symlinks unavailable on this host")
        mcs.report_overhead(tmp_path / ".claude", tmp_path / "absent.md", global_dir)
        assert "Rules dir total:    3000 bytes" in capsys.readouterr().out


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
