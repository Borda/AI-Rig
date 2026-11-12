"""Tests for resolve_agent_file bin script.

Covers target splitting (agent vs skill, plugin prefix, default plugin), the three-tier resolution with its ambiguity
rules, and the CLI output contract.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
import resolve_agent_file as raf
from _hook_env import _symlinks_supported


def _write_agent_files(root: Path, dirs: tuple[str, ...], filename: str = "a.md") -> None:
    """Create ``root/<dir>/agents/<filename>`` for every directory name in ``dirs``."""
    for name in dirs:
        agents = root / name / "agents"
        agents.mkdir(parents=True)
        (agents / filename).write_text("x", encoding="utf-8")


@pytest.fixture(name="run_cli")
def _run_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Callable[..., tuple[int, str]]:
    """Return a runner that executes ``raf.main()`` from ``tmp_path`` with the given argv tail; yields (rc, stdout)."""
    monkeypatch.chdir(tmp_path)

    def run(*args: str) -> tuple[int, str]:
        """Run the CLI once with ``args`` after the script name and return its exit code and stdout."""
        monkeypatch.setattr("sys.argv", ["resolve_agent_file.py", *args])
        return raf.main(), capsys.readouterr().out

    return run


class TestSplitTarget:
    """Covers parsing a calibration target name."""

    @pytest.mark.parametrize(
        ("target", "expected"),
        [
            pytest.param("curator", ("foundry", "curator", "agents/curator.md"), id="bare-agent-defaults-to-foundry"),
            pytest.param("oss:shepherd", ("oss", "shepherd", "agents/shepherd.md"), id="plugin-prefixed-agent"),
            pytest.param("/audit", ("foundry", "audit", "skills/audit/SKILL.md"), id="leading-slash-selects-skill"),
            pytest.param("/oss:review", ("oss", "review", "skills/review/SKILL.md"), id="plugin-prefixed-skill"),
        ],
    )
    def test_target_is_split(self, target: str, expected: tuple[str, str, str]) -> None:
        """A target splits into plugin, name and relative file.

        Scenario: a bare name is a foundry agent; `plugin:agent` selects that plugin; a leading slash makes the target a
        skill; both markers combine as `/plugin:skill`.
        """
        assert raf.split_target(target) == expected


class TestResolve:
    """Covers the file-resolution tiers."""

    @pytest.mark.parametrize(
        ("dirs", "plugin", "expected"),
        [
            pytest.param(("cc_foundry", "foundry"), "foundry", "cc_foundry", id="cc-prefixed-directory-first"),
            pytest.param(("foundry",), "foundry", "foundry", id="bare-plugin-directory-second"),
            pytest.param(("cc_other",), "foundry", "cc_other", id="single-glob-match-accepted"),
            pytest.param(("cc_alpha", "cc_beta"), "beta", "cc_beta", id="ambiguous-match-uses-prefix"),
        ],
    )
    def test_file_resolution_tiers(self, tmp_path: Path, dirs: tuple[str, ...], plugin: str, expected: str) -> None:
        """The plugin file resolves through the cc_ directory, the bare directory, then a single glob match.

        Scenario: `plugins/cc_<plugin>/` wins over a bare `plugins/<plugin>/`; without the cc_ form the bare directory
        resolves; one match anywhere under plugins/ resolves even under another name; with several matches the
        plugin-prefixed directory is chosen.
        """
        _write_agent_files(tmp_path, dirs)
        assert raf.resolve(plugin, "agents/a.md", root=tmp_path) == tmp_path / expected / "agents/a.md"

    def test_no_match_returns_none(self, tmp_path: Path) -> None:
        """Nothing on disk resolves to None rather than a guess.

        Scenario: an empty plugins root. Returning a made-up path would let a calibration run proceed on a file that
        does not exist.
        """
        assert raf.resolve("foundry", "agents/absent.md", root=tmp_path) is None

    @pytest.mark.parametrize(
        ("host", "rel", "declared", "suffix"),
        [
            pytest.param(".claude-plugin", "agents/a.md", "custom-agents", "a.md", id="claude-agent-alias"),
            pytest.param(".claude-plugin", "skills/a/SKILL.md", "custom-skills", "a/SKILL.md", id="claude-skill-alias"),
            pytest.param(".codex-plugin", "agents/a.md", "custom-agents", "a.md", id="codex-agent-alias"),
        ],
    )
    def test_manifest_alias_and_declared_directory(
        self, tmp_path: Path, host: str, rel: str, declared: str, suffix: str
    ) -> None:
        """Manifest plugin names and relative directories determine calibration destinations."""
        plugin = tmp_path / "unrelated-folder"
        manifest = plugin / host / "plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"name": "foundry", rel.split("/")[0]: f"./{declared}/"}), encoding="utf-8")
        target = plugin / declared / suffix
        target.parent.mkdir(parents=True)
        target.write_text("target", encoding="utf-8")
        assert raf.resolve("foundry", rel, root=tmp_path) == target

    def test_manifest_name_prevents_wrong_qualified_match(self, tmp_path: Path) -> None:
        """A folder resembling the requested plugin cannot override its different manifest identity."""
        _write_agent_files(tmp_path, ("cc_foundry",))
        manifest = tmp_path / "cc_foundry/.claude-plugin/plugin.json"
        manifest.parent.mkdir()
        manifest.write_text('{"name": "other"}', encoding="utf-8")
        assert raf.resolve("foundry", "agents/a.md", root=tmp_path) is None

    def test_claude_manifest_takes_precedence(self, tmp_path: Path) -> None:
        """A Codex manifest never overrides an existing Claude manifest."""
        _write_agent_files(tmp_path, ("cc_foundry",))
        claude = tmp_path / "cc_foundry/.claude-plugin/plugin.json"
        claude.parent.mkdir()
        claude.write_text('{"name": "other"}', encoding="utf-8")
        codex = tmp_path / "cc_foundry/.codex-plugin/plugin.json"
        codex.parent.mkdir()
        codex.write_text('{"name": "foundry"}', encoding="utf-8")
        assert raf.resolve("foundry", "agents/a.md", root=tmp_path) is None

    @pytest.mark.parametrize(
        "declared",
        ["../outside", "/agents", "C:/agents", ".", "", pytest.param(["agents"], id="list-directory"), None],
    )
    def test_invalid_declared_directory_does_not_fall_back(self, tmp_path: Path, declared: object) -> None:
        """An invalid declared directory cannot silently select the conventional agents directory."""
        _write_agent_files(tmp_path, ("cc_foundry",))
        manifest = tmp_path / "cc_foundry/.claude-plugin/plugin.json"
        manifest.parent.mkdir()
        manifest.write_text(json.dumps({"name": "foundry", "agents": declared}), encoding="utf-8")
        assert raf.resolve("foundry", "agents/a.md", root=tmp_path) is None

    @pytest.mark.skipif(not _symlinks_supported(), reason="This process cannot create source target symlinks")
    def test_resolved_destination_stays_inside_plugin(self, tmp_path: Path) -> None:
        """A source target reached through an escaping symlink is never returned for editing."""
        _write_agent_files(tmp_path, ("outside",))
        plugin = tmp_path / "plugins/cc_foundry"
        (plugin / "agents").mkdir(parents=True)
        (plugin / "agents/a.md").symlink_to(tmp_path / "outside/agents/a.md")
        assert raf.resolve("foundry", "agents/a.md", root=tmp_path / "plugins") is None


@pytest.mark.parametrize("declared", ["agents", "../outside"])
def test_known_manifest_without_target_cannot_select_other_plugin(tmp_path: Path, declared: str) -> None:
    """A known plugin's missing or invalid target cannot redirect edits to a legacy namesake."""
    manifest = tmp_path / "own/.claude-plugin/plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"name": "foundry", "agents": declared}), encoding="utf-8")
    _write_agent_files(tmp_path, ("other",))

    assert raf.resolve("foundry", "agents/a.md", root=tmp_path) is None


@pytest.mark.skipif(not _symlinks_supported(), reason="This process cannot create plugin directory symlinks")
def test_plugin_directory_cannot_escape_source_root(tmp_path: Path) -> None:
    """A linked plugin directory outside the source tree is never an apply destination."""
    _write_agent_files(tmp_path, ("outside",))
    root = tmp_path / "plugins"
    root.mkdir()
    (root / "cc_foundry").symlink_to(tmp_path / "outside", target_is_directory=True)

    assert raf.resolve("foundry", "agents/a.md", root=root) is None


class TestCli:
    """Covers stdout contract and exit codes."""

    def test_resolved_prints_both_lines(self, tmp_path: Path, run_cli: Callable[..., tuple[int, str]]) -> None:
        """A hit prints agent-file and proposal-path and exits 0.

        Scenario: ``plugins/cc_foundry/agents/curator.md`` exists under the working directory and the caller passes a
        fixed timestamp, so both output lines are fully determined.
        """
        _write_agent_files(tmp_path / "plugins", ("cc_foundry",), "curator.md")

        rc, out = run_cli("--name", "curator", "--timestamp", "TS")

        assert rc == 0
        assert "agent-file=plugins/cc_foundry/agents/curator.md" in out
        assert "proposal-path=.reports/calibrate/TS/curator/proposal.md" in out

    @pytest.mark.parametrize(
        ("run_slug", "expected"),
        [
            pytest.param(
                "", "proposal-path=.reports/calibrate/TS/oss:analyse/proposal.md", id="empty-slug-keeps-target"
            ),
            pytest.param(
                "oss:analyse-vitality",
                "proposal-path=.reports/calibrate/TS/oss:analyse-vitality/proposal.md",
                id="mode-slug-own-dir",
            ),
        ],
    )
    def test_run_slug_names_proposal_dir(
        self, tmp_path: Path, run_cli: Callable[..., tuple[int, str]], run_slug: str, expected: str
    ) -> None:
        """``--run-slug`` moves only the proposal directory; the skill file still resolves from ``--name``.

        Scenario: a mode-level calibration domain (vitality of ``/oss:analyse``) keeps its results apart from the
        default thread domain, yet its proposals edit the same skill. An empty slug, which the apply recipe passes
        for ordinary targets, must keep today's path.
        """
        skill = tmp_path / "plugins" / "cc_oss" / "skills" / "analyse"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("x", encoding="utf-8")

        rc, out = run_cli("--name", "/oss:analyse", "--timestamp", "TS", "--run-slug", run_slug)

        assert rc == 0
        assert "agent-file=plugins/cc_oss/skills/analyse/SKILL.md" in out
        assert expected in out

    def test_run_slug_with_separator_rejected(self, run_cli: Callable[..., tuple[int, str]]) -> None:
        """A slug carrying a path separator exits 2 instead of writing outside the run directory."""
        rc, _ = run_cli("--name", "curator", "--timestamp", "TS", "--run-slug", "../escape")

        assert rc == 2

    @pytest.mark.parametrize(
        ("args", "fragments"),
        [
            pytest.param(
                ["--name", "nope"],
                ("never falling back to installed cache", "agent-file="),
                id="miss-explains-and-blanks",
            ),
            pytest.param(["--name", "nope", "--local"], ("⚠ --local:",), id="local-flag-labels-failure"),
        ],
    )
    def test_unresolved_exits_one(
        self, run_cli: Callable[..., tuple[int, str]], args: list[str], fragments: tuple[str, ...]
    ) -> None:
        """A miss exits 1 and prints the reason; ``--local`` changes only the failure label, not the resolution order.

        Scenario: no agent file exists. The plain run prints why it never falls back to the installed cache plus an
        empty ``agent-file=``; with ``--local`` the failure line carries the ``--local`` label instead.
        """
        rc, out = run_cli(*args)

        assert rc == 1
        assert all(fragment in out for fragment in fragments)
