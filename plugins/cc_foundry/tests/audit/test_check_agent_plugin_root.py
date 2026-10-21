"""Tests for ``bin/check_agent_plugin_root.py`` and the foundry agents' zero-finding contract.

A plugin agent's Bash has ``CLAUDE_PLUGIN_ROOT`` unset, and only the exact token ``${CLAUDE_PLUGIN_ROOT}`` is text-
substituted in its prompt. The defaulted form falls back to a repo-relative path that does not exist in an installed
plugin, so the checker keeps it out of every agent file. The last test runs it over foundry's own agents.
"""

from __future__ import annotations

from pathlib import Path

import check_agent_plugin_root as checker
import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
FOUNDRY_AGENTS = REPO_ROOT / "plugins" / "cc_foundry" / "agents"


class TestDefaultsPluginRoot:
    """Line classification flags the defaulted spelling only."""

    @pytest.mark.parametrize(
        ("line", "flagged"),
        [
            pytest.param('cat "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/refs/a.md"', True, id="defaulted-root"),
            pytest.param('cat "${CLAUDE_PLUGIN_ROOT}/refs/a.md"', False, id="exact-token"),
            pytest.param('cat "$CLAUDE_PLUGIN_ROOT/refs/a.md"', False, id="bare-variable"),
            pytest.param('cat "${CLAUDE_PLUGIN_ROOT:-x}/a.md"  plugin-root-check: allow', False, id="exempted"),
            pytest.param("plain prose about plugins", False, id="unrelated-line"),
        ],
    )
    def test_flags_only_the_defaulted_form(self, line: str, flagged: bool) -> None:
        """Only ``${CLAUDE_PLUGIN_ROOT:-...}`` without an exemption marker is a finding.

        The exact token is what the host substitutes, so it must stay clean; the bare variable is unset in an agent's
        Bash and is a different defect this checker does not claim.
        """
        assert checker.defaults_plugin_root(line) is flagged


class TestMain:
    """CLI contract over scanned directories and explicit files."""

    def test_scan_dir_reports_each_offending_agent_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A defaulted line in any ``agents/*.md`` is reported with its path and line number, exit 1.

        Skills are excluded: they run in the main session where the variable is exported, so the default is correct
        there and must not be flagged.
        """
        agents = tmp_path / "p" / "agents"
        skills = tmp_path / "p" / "skills" / "s"
        agents.mkdir(parents=True)
        skills.mkdir(parents=True)
        (agents / "a.md").write_text('ok\ncat "${CLAUDE_PLUGIN_ROOT:-plugins/p}/x.md"\n', encoding="utf-8")
        (skills / "SKILL.md").write_text('cat "${CLAUDE_PLUGIN_ROOT:-plugins/p}/x.md"\n', encoding="utf-8")

        exit_code = checker.main(["--scan-dir", str(tmp_path)])

        out = capsys.readouterr().out
        assert (exit_code, out.count("plugin-root-default:"), "a.md:2:" in out.replace("\\", "/")) == (1, 1, True)

    def test_clean_scan_prints_pass_line(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Agents using the exact token produce a single pass line and exit 0."""
        agents = tmp_path / "p" / "agents"
        agents.mkdir(parents=True)
        (agents / "a.md").write_text('cat "${CLAUDE_PLUGIN_ROOT}/x.md"\n', encoding="utf-8")

        exit_code = checker.main(["--scan-dir", str(tmp_path)])

        assert (exit_code, capsys.readouterr().out.startswith("✓ agent-plugin-root")) == (0, True)

    def test_explicit_non_markdown_file_is_ignored(self, tmp_path: Path) -> None:
        """Only ``.md`` paths are collected, so a stray argument cannot make the scan read arbitrary files."""
        script = tmp_path / "x.py"
        script.write_text('"${CLAUDE_PLUGIN_ROOT:-p}"\n', encoding="utf-8")
        assert checker.collect(None, [str(script)]) == []


def test_foundry_agents_never_default_the_plugin_root() -> None:
    """Every foundry agent file uses the exact ``${CLAUDE_PLUGIN_ROOT}`` token wherever it names the plugin root.

    A reintroduced default silently degrades sidecar and ``_shared`` loads on every installed copy, while still working
    inside this repository where the relative path exists, so nothing else catches it.
    """
    findings = [finding for path in checker.collect(FOUNDRY_AGENTS.parent, []) for finding in checker.scan_file(path)]
    assert findings == []
