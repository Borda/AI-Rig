"""Tests for ``bin/symlink_with_guard.py``.

Doctests cover the pure helpers (``_is_foundry_managed``, ``_is_current``, ``_owns``, ``_cache_lineage``,
``_conflict_label``). This file exercises the end-to-end behaviours of ``cleanup`` and ``scan`` against a real temporary
filesystem so the symlink-handling logic is verified without mocks.

Rules install as ``~/.claude/rules/foundry-<source>.md``. Every mutation there is gated on the ownership proof in
``_owns`` — the target must resolve under the current plugin root or the same installed-cache lineage. The "foreign
target" cases below exist because an earlier implementation used a path substring instead and deleted a user's
``dotfiles/plugins/cc_foundry/rules/…`` link.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

import pytest


import symlink_with_guard  # noqa: E402
from symlink_with_guard import cleanup, create_link, main, scan  # noqa: E402

_MARKER = "borda-ai-rig/foundry/"
_SKILL_MD = Path(__file__).resolve().parent.parent / "skills" / "setup" / "SKILL.md"


@pytest.fixture(name="env")
def _env(tmp_path: Path) -> tuple[Path, Path]:
    """Build a current installed plugin tree + a fake $HOME, return both.

    The plugin root sits where a real install puts it — under the fake home's
    plugin cache — so both ownership regimes are exercised faithfully: the
    ``borda-ai-rig/foundry/`` marker matches the path (skills/agents scopes) and
    ``_cache_lineage`` resolves (rules/TEAM_PROTOCOL scopes). A root outside the
    cache would make several assertions pass vacuously.

    Layout::

        tmp/home/.claude/plugins/cache/borda-ai-rig/foundry/0.40.0/
            {rules/{current,another}.md,skills/{curator,shepherd,_shared},TEAM_PROTOCOL.md}
        tmp/home/.claude/{rules,skills,agents}/

    The ``skills/`` dirs exist to prove they are NOT linked into ``$HOME`` —
    they never enter ``_build_entries``.
    """
    home = tmp_path / "home"
    plugin = home / ".claude" / "plugins" / "cache" / "borda-ai-rig" / "foundry" / "0.40.0"
    (plugin / "rules").mkdir(parents=True)
    (plugin / "rules" / "current.md").write_text("current\n")
    (plugin / "rules" / "another.md").write_text("another\n")
    (plugin / "TEAM_PROTOCOL.md").write_text("team\n")
    (plugin / "skills" / "curator").mkdir(parents=True)
    (plugin / "skills" / "shepherd").mkdir(parents=True)
    (plugin / "skills" / "_shared").mkdir(parents=True)

    (home / ".claude" / "rules").mkdir(parents=True)
    (home / ".claude" / "skills").mkdir(parents=True)
    (home / ".claude" / "agents").mkdir(parents=True)
    return plugin, home


@pytest.fixture(name="marked_env")
def _marked_env(env: tuple[Path, Path]) -> tuple[Path, Path]:
    """Alias of :func:`env`, kept for tests that assert on current-version links.

    The distinction the two fixtures used to draw disappeared when ``env`` moved
    its plugin root into the install cache: that root now carries the
    ``borda-ai-rig/foundry/`` marker on its own.
    """
    return env


def _stale_root(plugin: Path, version: str = "0.39.0") -> Path:
    """Path of an older version of this plugin — same install-cache lineage.

    Examples:
        >>> _stale_root(Path("cache/foundry/0.40.0"), "0.39.0").as_posix()
        'cache/foundry/0.39.0'
    """
    return plugin.parent / version


def _ln(target: str, link: Path) -> None:
    """Create a symlink with an arbitrary text target (no exists-check)."""
    link.symlink_to(target)


def _bash_can_symlink() -> bool:
    """Probe whether the ``bash`` on PATH can create a symlink Python recognises.

    A capability probe rather than a platform test: on a Windows host ``bash``
    may be the WSL launcher stub (prints a UTF-16 install notice, exits 1) or a
    Git Bash whose ``ln -s`` copies instead of linking, but a Git Bash with
    ``MSYS=winsymlinks:nativestrict`` and Developer Mode satisfies it and must
    keep running the test.

    Returns:
        True when ``bash -c 'ln -s ...'`` produced a real symlink.
    """
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "probe_src"
        src.write_text("probe\n", encoding="utf-8")
        link = Path(tmp) / "probe_link"
        try:
            proc = subprocess.run(
                ["bash", "-c", f'ln -s "{src}" "{link}"'],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0 and link.is_symlink()


_BASH_SYMLINKS = _bash_can_symlink()


class TestExtendedPathPrefix:
    """Windows hands back ``\\\\?\\``-prefixed link targets; every comparison must see them stripped."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param(r"\\?\C:\Users\x\rules\a.md", r"C:\Users\x\rules\a.md", id="drive"),
            pytest.param(r"\\?\UNC\server\share\a.md", r"\\server\share\a.md", id="unc"),
            pytest.param("/home/x/rules/a.md", "/home/x/rules/a.md", id="posix-untouched"),
            pytest.param(r"C:\Users\x\a.md", r"C:\Users\x\a.md", id="plain-windows-untouched"),
        ],
    )
    def test_strip_extended_prefix(self, raw: str, expected: str) -> None:
        """The prefix is removed for both drive and UNC forms; other spellings pass through."""
        assert symlink_with_guard._strip_extended_prefix(raw) == expected

    def test_readlink_strips_prefix_at_the_boundary(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Normalize, so no downstream consumer ever sees the prefixed spelling."""
        link = tmp_path / "link.md"
        _ln("target.md", link)
        monkeypatch.setattr(symlink_with_guard.os, "readlink", lambda _p: r"\\?\C:\cache\foundry\rules\a.md")

        assert symlink_with_guard._readlink(link) == r"C:\cache\foundry\rules\a.md"


class TestMarkerSeparators:
    """The marker is spelled with ``/``; a native Windows target spells the same path with ``\\``."""

    @pytest.mark.parametrize(
        "target",
        [
            "/h/.claude/plugins/cache/borda-ai-rig/foundry/0.40.0/skills/curator",
            r"C:\h\.claude\plugins\cache\borda-ai-rig\foundry\0.40.0\skills\curator",
        ],
    )
    def test_marker_matches_either_separator(self, target: str) -> None:
        """Both spellings are foundry-managed — the skills/agents purge must fire on each."""
        assert symlink_with_guard._is_foundry_managed(target, _MARKER)

    def test_foreign_simulated_windows_target_is_not_managed(self) -> None:
        """Separator normalisation must not widen the match to unrelated paths."""
        assert not symlink_with_guard._is_foundry_managed(r"C:\h\dotfiles\rules\a.md", _MARKER)


class TestCleanup:
    """Cleanup: removes only foundry-managed symlinks whose source vanished."""

    def test_removes_obsolete_foundry_rule_link(self, env: tuple[Path, Path]) -> None:
        """Stale same-lineage symlink whose source no longer exists is removed."""
        plugin, home = env
        link = home / ".claude" / "rules" / "foundry-obsolete.md"
        _ln(str(_stale_root(plugin) / "rules" / "obsolete.md"), link)

        log = cleanup(plugin, home, _MARKER)

        assert not link.is_symlink()
        assert "removed obsolete: foundry-obsolete.md" in log

    def test_keeps_current_foundry_rule_link(self, env: tuple[Path, Path]) -> None:
        """Namespaced symlink already pointing into current plugin root is untouched."""
        plugin, home = env
        link = home / ".claude" / "rules" / "foundry-current.md"
        _ln(str(plugin / "rules" / "current.md"), link)

        log = cleanup(plugin, home, _MARKER)

        assert link.is_symlink()
        assert log == []

    def test_keeps_non_foundry_rule_link(self, env: tuple[Path, Path]) -> None:
        """Symlink to a non-foundry path is left alone (user owns it)."""
        plugin, home = env
        link = home / ".claude" / "rules" / "foundry-user.md"
        _ln("/somewhere/else/user.md", link)

        cleanup(plugin, home, _MARKER)

        assert link.is_symlink()

    def test_migrates_legacy_unprefixed_link(self, env: tuple[Path, Path]) -> None:
        """Pre-namespace link into the current root is removed so Phase 4 can re-link namespaced."""
        plugin, home = env
        legacy = home / ".claude" / "rules" / "current.md"
        _ln(str(plugin / "rules" / "current.md"), legacy)

        log = cleanup(plugin, home, _MARKER)

        assert not legacy.is_symlink()
        assert "removed obsolete: current.md" in log

    def test_migrates_legacy_link_from_older_installed_version(self, env: tuple[Path, Path]) -> None:
        """A pre-namespace link from an older install shares the lineage, so it migrates too."""
        plugin, home = env
        legacy = home / ".claude" / "rules" / "current.md"
        _ln(str(_stale_root(plugin) / "rules" / "current.md"), legacy)

        cleanup(plugin, home, _MARKER)

        assert not legacy.is_symlink()

    def test_removes_dangling_owned_link_after_source_rename(self, env: tuple[Path, Path]) -> None:
        """A link into the current root whose file was renamed away is owned, so it goes."""
        plugin, home = env
        dangling = home / ".claude" / "rules" / "testing.md"
        _ln(str(plugin / "rules" / "testing.md"), dangling)

        log = cleanup(plugin, home, _MARKER)

        assert not dangling.is_symlink()
        assert "removed obsolete: testing.md" in log

    @pytest.mark.parametrize(
        "target_rel",
        [
            ".claude/plugins/cache/other-market/foundry/0.39.0/rules/current.md",
            ".claude/plugins/cache/borda-ai-rig/develop/0.19.0/rules/quality-gates.md",
            "src/AI-Rig/plugins/cc_foundry/rules/current.md",
            "dotfiles/plugins/cc_foundry/rules/current.md",
        ],
    )
    def test_keeps_legacy_link_with_foreign_target(self, env: tuple[Path, Path], target_rel: str) -> None:
        """An unprefixed link failing the ownership proof survives migration untouched."""
        plugin, home = env
        legacy = home / ".claude" / "rules" / "current.md"
        _ln(str(home / target_rel), legacy)

        log = cleanup(plugin, home, _MARKER)

        assert legacy.is_symlink()
        assert log == []

    def test_keeps_sibling_plugin_namespaced_link(self, env: tuple[Path, Path]) -> None:
        """Another plugin's namespace is never foundry's to prune."""
        plugin, home = env
        sibling = home / ".claude" / "rules" / "develop-quality-gates.md"
        _ln(str(home / ".claude/plugins/cache/borda-ai-rig/develop/0.19.0/rules/quality-gates.md"), sibling)

        cleanup(plugin, home, _MARKER)

        assert sibling.is_symlink()

    def test_keeps_real_file(self, env: tuple[Path, Path]) -> None:
        """Real files are never deleted by cleanup."""
        plugin, home = env
        real = home / ".claude" / "rules" / "myown.md"
        real.write_text("hand-written\n")

        cleanup(plugin, home, _MARKER)

        assert real.is_file()

    def test_removes_obsolete_team_protocol(self, env: tuple[Path, Path]) -> None:
        """Stale foundry TEAM_PROTOCOL.md symlink is removed when source absent."""
        plugin, home = env
        # source file removed from current plugin → cleanup should drop the stale link
        (plugin / "TEAM_PROTOCOL.md").unlink()
        link = home / ".claude" / "TEAM_PROTOCOL.md"
        _ln(str(_stale_root(plugin) / "TEAM_PROTOCOL.md"), link)

        log = cleanup(plugin, home, _MARKER)

        assert not link.is_symlink()
        assert "removed obsolete: TEAM_PROTOCOL.md" in log

    def test_keeps_foreign_team_protocol_when_source_gone(self, env: tuple[Path, Path]) -> None:
        """An unowned TEAM_PROTOCOL.md link survives even when foundry stops shipping the file."""
        plugin, home = env
        (plugin / "TEAM_PROTOCOL.md").unlink()
        link = home / ".claude" / "TEAM_PROTOCOL.md"
        _ln(str(home / "dotfiles" / "TEAM_PROTOCOL.md"), link)

        log = cleanup(plugin, home, _MARKER)

        assert link.is_symlink()
        assert log == []

    def test_keeps_team_protocol_when_source_still_present(self, env: tuple[Path, Path]) -> None:
        """Even a stale foundry TEAM_PROTOCOL.md link stays when source exists (Phase 4 will refresh)."""
        plugin, home = env
        link = home / ".claude" / "TEAM_PROTOCOL.md"
        _ln(str(_stale_root(plugin) / "TEAM_PROTOCOL.md"), link)

        cleanup(plugin, home, _MARKER)

        assert link.is_symlink()  # not obsolete — Phase 4 handles refresh

    def test_removes_stale_skill_link(self, env: tuple[Path, Path]) -> None:
        """Foundry-managed skill symlink under a non-current root is removed."""
        plugin, home = env
        link = home / ".claude" / "skills" / "oldskill"
        _ln("/old/borda-ai-rig/foundry/0.10.0/skills/oldskill", link)

        log = cleanup(plugin, home, _MARKER)

        assert not link.is_symlink()
        assert "removed user-level skill link: oldskill" in log

    def test_removes_current_version_skill_link(self, marked_env: tuple[Path, Path]) -> None:
        """Skill symlink pointing into the CURRENT plugin root is removed too.

        Deliberately opposite to ``test_keeps_current_version_agent_symlink``: a current-root skill link is not a signal
        to investigate, it is the defect itself — it registers the dir as a user-level skill that shadows Claude Code's
        bundled skill of the same name. Do not align these two tests.
        """
        plugin, home = marked_env
        link = home / ".claude" / "skills" / "curator"
        _ln(str(plugin / "skills" / "curator"), link)

        log = cleanup(plugin, home, _MARKER)

        assert not link.is_symlink()
        assert "removed user-level skill link: curator" in log

    def test_removes_shared_support_dir_link(self, marked_env: tuple[Path, Path]) -> None:
        """Deny exemptions for global shared-plugin paths."""
        plugin, home = marked_env
        link = home / ".claude" / "skills" / "_shared"
        _ln(str(plugin / "skills" / "_shared"), link)

        log = cleanup(plugin, home, _MARKER)

        assert not link.is_symlink()
        assert "removed user-level skill link: _shared" in log

    def test_keeps_non_foundry_skill_link(self, env: tuple[Path, Path]) -> None:
        """Skill symlink pointing outside foundry is left alone (user owns it)."""
        plugin, home = env
        link = home / ".claude" / "skills" / "mine"
        _ln("/somewhere/else/mine", link)

        cleanup(plugin, home, _MARKER)

        assert link.is_symlink()

    def test_keeps_real_skill_dir(self, env: tuple[Path, Path]) -> None:
        """Real (non-symlink) dirs under ~/.claude/skills/ are never deleted."""
        plugin, home = env
        real = home / ".claude" / "skills" / "geo"
        real.mkdir()

        cleanup(plugin, home, _MARKER)

        assert real.is_dir()
        assert not real.is_symlink()

    def test_empty_when_no_obsolete_entries(self, env: tuple[Path, Path]) -> None:
        """Clean state → no removals, empty log."""
        plugin, home = env

        log = cleanup(plugin, home, _MARKER)

        assert log == []

    def test_removes_stale_foundry_agent_symlink(self, env: tuple[Path, Path]) -> None:
        """Foundry-managed agent symlink under a non-current root is removed unconditionally."""
        plugin, home = env
        link = home / ".claude" / "agents" / "sw-engineer.md"
        _ln("/old/borda-ai-rig/foundry/0.10.0/agents/sw-engineer.md", link)

        log = cleanup(plugin, home, _MARKER)

        assert not link.is_symlink()
        assert "removed obsolete agent: sw-engineer.md" in log

    def test_keeps_non_foundry_agent_symlink(self, env: tuple[Path, Path]) -> None:
        """Agent symlink pointing outside foundry is left alone (user owns it)."""
        plugin, home = env
        link = home / ".claude" / "agents" / "my-agent.md"
        _ln("/home/user/.claude/agents/my-agent.md", link)

        cleanup(plugin, home, _MARKER)

        assert link.is_symlink()

    def test_keeps_current_version_agent_symlink(self, env: tuple[Path, Path]) -> None:
        """Agent symlink pointing into the current plugin root is left alone.

        Init never re-creates these, so any link pointing at the current root was placed by something external; leaving
        it intact gives the operator a clear signal to investigate without silently destroying state.
        """
        plugin, home = env
        link = home / ".claude" / "agents" / "sw-engineer.md"
        _ln(str(plugin / "agents" / "sw-engineer.md"), link)

        cleanup(plugin, home, _MARKER)

        assert link.is_symlink()

    def test_keeps_real_agent_file(self, env: tuple[Path, Path]) -> None:
        """Real (non-symlink) files under ~/.claude/agents/ are never deleted."""
        plugin, home = env
        real = home / ".claude" / "agents" / "user.md"
        real.write_text("user-authored\n")

        cleanup(plugin, home, _MARKER)

        assert real.is_file()
        assert not real.is_symlink()


class TestScan:
    """Scan: surfaces only conflicts requiring user confirmation."""

    def test_skips_current_foundry_link(self, env: tuple[Path, Path]) -> None:
        """Current namespaced symlink is not a conflict."""
        plugin, home = env
        _ln(
            str(plugin / "rules" / "current.md"),
            home / ".claude" / "rules" / "foundry-current.md",
        )

        assert scan(plugin, home, _MARKER) == []

    def test_skips_stale_foundry_link(self, env: tuple[Path, Path]) -> None:
        """Same-lineage stale symlink is auto-replaced in Phase 4 — not a conflict."""
        plugin, home = env
        _ln(
            str(_stale_root(plugin) / "rules" / "current.md"),
            home / ".claude" / "rules" / "foundry-current.md",
        )

        assert scan(plugin, home, _MARKER) == []

    def test_ignores_legacy_unprefixed_link(self, env: tuple[Path, Path]) -> None:
        """A pre-namespace link is not at any current destination, so it raises no conflict."""
        plugin, home = env
        _ln(str(plugin / "rules" / "current.md"), home / ".claude" / "rules" / "current.md")

        assert scan(plugin, home, _MARKER) == []

    def test_reports_non_foundry_symlink(self, env: tuple[Path, Path]) -> None:
        """Symlink failing the ownership proof surfaces as a conflict descriptor."""
        plugin, home = env
        _ln("/home/user/dotfiles/another.md", home / ".claude" / "rules" / "foundry-another.md")

        conflicts = scan(plugin, home, _MARKER)

        assert conflicts == ["rules/foundry-another.md → /home/user/dotfiles/another.md"]

    def test_reports_other_marketplace_symlink(self, env: tuple[Path, Path]) -> None:
        """Another marketplace's cache path is a different lineage — conflict, not a silent refresh."""
        plugin, home = env
        foreign = home / ".claude/plugins/cache/other-market/foundry/0.39.0/rules/current.md"
        _ln(str(foreign), home / ".claude" / "rules" / "foundry-current.md")

        conflicts = scan(plugin, home, _MARKER)

        assert conflicts == [f"rules/foundry-current.md → {foreign}"]

    def test_reports_real_file_conflict(self, env: tuple[Path, Path]) -> None:
        """Real file at dest path surfaces with the (real file) suffix."""
        plugin, home = env
        (home / ".claude" / "rules" / "foundry-current.md").write_text("hand-written\n")

        conflicts = scan(plugin, home, _MARKER)

        assert "rules/foundry-current.md  (real file)" in conflicts

    def test_reports_team_protocol_conflict(self, env: tuple[Path, Path]) -> None:
        """TEAM_PROTOCOL.md conflict is labeled without the rules/ or skills/ prefix."""
        plugin, home = env
        _ln("/somewhere/else/team.md", home / ".claude" / "TEAM_PROTOCOL.md")

        conflicts = scan(plugin, home, _MARKER)

        assert "TEAM_PROTOCOL.md → /somewhere/else/team.md" in conflicts

    def test_plugin_skill_real_dir_is_not_a_conflict(self, env: tuple[Path, Path]) -> None:
        """A dir named after a plugin skill is no conflict — skills are never linked into $HOME."""
        plugin, home = env
        (home / ".claude" / "skills" / "curator").mkdir()

        conflicts = scan(plugin, home, _MARKER)

        assert conflicts == []

    def test_no_conflicts_on_empty_dest(self, env: tuple[Path, Path]) -> None:
        """Absent dest entries are not conflicts — Phase 4 will create them."""
        plugin, home = env

        assert scan(plugin, home, _MARKER) == []


class TestMain:
    """Main: CLI surface — stdout, stderr, exit codes."""

    def test_cleanup_prints_log_lines_indented(
        self,
        env: tuple[Path, Path],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Cleanup mode emits ``  removed obsolete: ...`` lines (two-space indent)."""
        plugin, home = env
        _ln(
            str(_stale_root(plugin) / "rules" / "another.md"),
            home / ".claude" / "rules" / "foundry-another.md",
        )
        # required: source file deleted to mark it obsolete
        (plugin / "rules" / "another.md").unlink()

        rc = main(["cleanup", "--plugin-root", str(plugin), "--home", str(home)])

        assert rc == 0
        out = capsys.readouterr().out
        assert "  removed obsolete: foundry-another.md" in out

    def test_scan_prints_one_conflict_per_line(
        self,
        env: tuple[Path, Path],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Scan mode emits the bash-array-ready format on stdout."""
        plugin, home = env
        _ln("/elsewhere/foo.md", home / ".claude" / "rules" / "foundry-current.md")

        rc = main(["scan", "--plugin-root", str(plugin), "--home", str(home)])

        assert rc == 0
        out = capsys.readouterr().out.strip().splitlines()
        assert out == ["rules/foundry-current.md → /elsewhere/foo.md"]

    def test_missing_plugin_root_exits_1(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Non-existent plugin root path is a hard error."""
        rc = main(["scan", "--plugin-root", str(tmp_path / "nope")])
        assert rc == 1
        assert "is not a directory" in capsys.readouterr().err

    def test_custom_marker_respected(
        self,
        env: tuple[Path, Path],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Use a custom ownership marker when purging skill links.

        The rules and TEAM_PROTOCOL scopes ignore the marker entirely — they are destinations foundry writes, so they
        demand the stricter path-lineage proof instead.
        """
        plugin, home = env
        _ln(
            "/x/custom-marker/0.1/skills/curator",
            home / ".claude" / "skills" / "curator",
        )

        rc = main(
            ["cleanup", "--plugin-root", str(plugin), "--home", str(home), "--marker", "custom-marker/"],
        )

        assert rc == 0
        out = capsys.readouterr().out
        assert "  removed user-level skill link: curator" in out

    def test_marker_does_not_authorise_rule_deletion(
        self,
        env: tuple[Path, Path],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A marker-matching but unowned rules link is never deleted."""
        plugin, home = env
        link = home / ".claude" / "rules" / "current.md"
        _ln("/x/custom-marker/0.1/rules/current.md", link)

        rc = main(
            ["cleanup", "--plugin-root", str(plugin), "--home", str(home), "--marker", "custom-marker/"],
        )

        assert rc == 0
        assert link.is_symlink()
        assert "removed obsolete" not in capsys.readouterr().out


class TestCreateLink:
    """create_link: 3-tier cascade (symlink → junction → copy + sidecar)."""

    def test_create_link_makes_symlink(self, tmp_path: Path) -> None:
        """Directory src + non-existent dest on POSIX → real symlink created."""
        home = tmp_path / "home"
        home.mkdir()
        src = tmp_path / "src_dir"
        src.mkdir()
        (src / "file.txt").write_text("hello\n")
        dest = tmp_path / "dest_link"

        tier = create_link(src, dest, home)

        assert tier == "symlink"
        assert dest.is_symlink()
        assert (dest / "file.txt").read_text() == "hello\n"

    def test_create_link_file_makes_symlink(self, tmp_path: Path) -> None:
        """File src + non-existent dest on POSIX → real symlink created."""
        home = tmp_path / "home"
        home.mkdir()
        src = tmp_path / "src_file.md"
        src.write_text("body\n")
        dest = tmp_path / "dest_file.md"

        tier = create_link(src, dest, home)

        assert tier == "symlink"
        assert dest.is_symlink()
        assert dest.read_text() == "body\n"

    def test_create_link_falls_to_copy_when_symlink_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Tier 1 raises OSError, Tier 2 skipped (non-Windows) → Tier 3 copy + sidecar."""
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        src = tmp_path / "src_dir"
        src.mkdir()
        (src / "inner.txt").write_text("payload\n")
        dest = tmp_path / "out" / "dest_dir"

        # Force Tier 1 to fail and pin platform to non-Windows so Tier 2 is skipped.
        monkeypatch.setattr(
            symlink_with_guard.Path,
            "symlink_to",
            lambda self, target: (_ for _ in ()).throw(OSError("simulated symlink failure")),
        )
        monkeypatch.setattr(symlink_with_guard.sys, "platform", "linux")

        tier = create_link(src, dest, home)

        assert tier == "copy"
        assert dest.is_dir()
        assert not dest.is_symlink()
        assert (dest / "inner.txt").read_text() == "payload\n"
        sidecar = dest.parent / f".{dest.name}.sourced_from"
        assert sidecar.is_file()
        assert sidecar.read_text()  # non-empty

    def test_create_link_sidecar_uses_relative_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Src under ``home/.claude`` → sidecar holds relative posix path (no leading /)."""
        home = tmp_path / "home"
        claude_dir = home / ".claude"
        claude_dir.mkdir(parents=True)
        src = claude_dir / "plugins" / "foundry" / "rules" / "x.md"
        src.parent.mkdir(parents=True)
        src.write_text("rule body\n")
        dest = tmp_path / "out" / "x.md"

        monkeypatch.setattr(
            symlink_with_guard.Path,
            "symlink_to",
            lambda self, target: (_ for _ in ()).throw(OSError("forced copy path")),
        )
        monkeypatch.setattr(symlink_with_guard.sys, "platform", "linux")

        tier = create_link(src, dest, home)

        assert tier == "copy"
        sidecar = dest.parent / f".{dest.name}.sourced_from"
        content = sidecar.read_text()
        # Relative path: does NOT start with '/' and matches expected posix-relative form.
        assert not content.startswith("/")
        assert content == "plugins/foundry/rules/x.md\n"

    def test_create_link_sidecar_falls_back_to_absolute(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Src outside ``home/.claude`` → ValueError on relative_to → sidecar stores absolute posix path."""
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        # src lives outside home/.claude entirely
        src = tmp_path / "elsewhere" / "external.md"
        src.parent.mkdir(parents=True)
        src.write_text("external content\n")
        dest = tmp_path / "out" / "external.md"

        monkeypatch.setattr(
            symlink_with_guard.Path,
            "symlink_to",
            lambda self, target: (_ for _ in ()).throw(OSError("forced copy path")),
        )
        monkeypatch.setattr(symlink_with_guard.sys, "platform", "linux")

        tier = create_link(src, dest, home)

        assert tier == "copy"
        sidecar = dest.parent / f".{dest.name}.sourced_from"
        content = sidecar.read_text()
        assert content == src.as_posix() + "\n"
        assert Path(content.strip()).is_absolute()  # absolute fallback

    def test_main_create_mode_missing_src(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Reject create mode without a source path."""
        home = tmp_path / "home"
        home.mkdir()
        rc = main(["create", "--dest", str(tmp_path / "x"), "--home", str(home)])
        assert rc == 2
        assert "--src" in capsys.readouterr().err

    def test_main_create_mode_missing_dest(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Reject create mode without a destination path."""
        home = tmp_path / "home"
        home.mkdir()
        rc = main(["create", "--src", str(tmp_path / "x"), "--home", str(home)])
        assert rc == 2
        assert "--dest" in capsys.readouterr().err


def _phase4_block() -> str:
    """Return the setup skill's Phase 4 linking block, verbatim from SKILL.md.

    Two destination calculations exist for the same files — this Python module's
    ``_build_entries`` and the shell loop the skill actually executes. The first
    attempt at namespacing changed only the Python side and still created
    unprefixed links at runtime, so the shell block is executed here rather than
    trusted.

    Returns:
        The fenced block's contents.

    Raises:
        AssertionError: When the block can no longer be located.
    """
    text = _SKILL_MD.read_text(encoding="utf-8")
    blocks = re.findall(r"^```bash\n(.*?)^```", text, flags=re.DOTALL | re.MULTILINE)
    matches = [b for b in blocks if 'for src in "$PLUGIN_ROOT/rules/"*.md' in b]
    assert len(matches) == 1, f"expected exactly one Phase 4 link loop in {_SKILL_MD}, found {len(matches)}"
    return matches[0]


class TestSkillPhase4Block:
    """The executable SKILL.md block must agree with this module's destinations."""

    @pytest.mark.skipif(
        not _BASH_SYMLINKS,
        reason="`bash` on PATH cannot create symlinks (WSL launcher stub, or Git Bash without winsymlinks)",
    )
    def test_block_creates_namespaced_links(self, env: tuple[Path, Path], tmp_path: Path) -> None:
        """Running the real block produces exactly the destinations ``_build_entries`` expects.

        Capability-gated, not platform-gated: the block is the skill's own ``ln -sf`` loop, so a host whose ``bash``
        cannot link has nothing to assert about.
        """
        plugin, home = env
        run_env = {
            **os.environ,
            "HOME": str(home),
            "PLUGIN_ROOT": str(plugin),
            "TMPDIR": str(tmp_path),
            "CLAUDE_CODE_SESSION_ID": "test-session",
        }

        proc = subprocess.run(
            ["bash", "-c", _phase4_block()],
            env=run_env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert proc.returncode == 0, proc.stderr
        rules_dest = home / ".claude" / "rules"
        created = sorted(p.name for p in rules_dest.iterdir())
        assert created == ["foundry-another.md", "foundry-current.md"]
        assert (rules_dest / "foundry-current.md").readlink() == plugin / "rules" / "current.md"
        assert (home / ".claude" / "TEAM_PROTOCOL.md").is_symlink()
