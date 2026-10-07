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
import symlink_with_guard
from symlink_with_guard import cleanup, create_link, main, scan

_MARKER = "borda-ai-rig/foundry/"
_SKILL_MD = Path(__file__).resolve().parent.parent.parent / "skills" / "setup" / "SKILL.md"


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

    @pytest.mark.parametrize(
        ("link_rel", "target", "message"),
        [
            # Stale same-lineage symlink whose source no longer exists.
            pytest.param(
                "rules/foundry-obsolete.md",
                lambda plugin, home: str(_stale_root(plugin) / "rules" / "obsolete.md"),
                "removed obsolete: foundry-obsolete.md",
                id="obsolete-foundry-rule-link",
            ),
            # Pre-namespace link into the current root is removed so Phase 4 can re-link namespaced.
            pytest.param(
                "rules/current.md",
                lambda plugin, home: str(plugin / "rules" / "current.md"),
                "removed obsolete: current.md",
                id="legacy-unprefixed-link-migrates",
            ),
            # A pre-namespace link from an older install shares the lineage, so it migrates too.
            pytest.param(
                "rules/current.md",
                lambda plugin, home: str(_stale_root(plugin) / "rules" / "current.md"),
                "removed obsolete: current.md",
                id="legacy-link-from-older-installed-version-migrates",
            ),
            # A link into the current root whose file was renamed away is owned, so it goes.
            pytest.param(
                "rules/testing.md",
                lambda plugin, home: str(plugin / "rules" / "testing.md"),
                "removed obsolete: testing.md",
                id="dangling-owned-link-after-source-rename",
            ),
            # Foundry-managed skill symlink under a non-current root.
            pytest.param(
                "skills/oldskill",
                lambda plugin, home: "/old/borda-ai-rig/foundry/0.10.0/skills/oldskill",
                "removed user-level skill link: oldskill",
                id="stale-skill-link",
            ),
            # Deliberately opposite to the current-version AGENT symlink case in ``test_keeps_link``: a current-root
            # skill link is not a signal to investigate, it is the defect itself — it registers the dir as a
            # user-level skill that shadows Claude Code's bundled skill of the same name. Do not align these two cases.
            pytest.param(
                "skills/curator",
                lambda plugin, home: str(plugin / "skills" / "curator"),
                "removed user-level skill link: curator",
                id="current-version-skill-link",
            ),
            # Deny exemptions for global shared-plugin paths.
            pytest.param(
                "skills/_shared",
                lambda plugin, home: str(plugin / "skills" / "_shared"),
                "removed user-level skill link: _shared",
                id="shared-support-dir-link",
            ),
            # Foundry-managed agent symlink under a non-current root is removed unconditionally.
            pytest.param(
                "agents/sw-engineer.md",
                lambda plugin, home: "/old/borda-ai-rig/foundry/0.10.0/agents/sw-engineer.md",
                "removed obsolete agent: sw-engineer.md",
                id="stale-foundry-agent-symlink",
            ),
        ],
    )
    def test_removes_link(self, env: tuple[Path, Path], link_rel: str, target, message: str) -> None:
        """A foundry-owned symlink whose source is gone or that must not exist user-level is removed and logged.

        Scenario: stale and dangling rule links in the owned lineage, pre-namespace links that Phase 4 re-links, and
        foundry-managed skill and agent links are all removed with a log line naming them.
        """
        plugin, home = env
        link = home / ".claude" / link_rel
        _ln(target(plugin, home), link)

        log = cleanup(plugin, home, _MARKER)

        assert not link.is_symlink()
        assert message in log

    @pytest.mark.parametrize(
        ("link_rel", "target"),
        [
            # Namespaced symlink already pointing into current plugin root is untouched.
            pytest.param(
                "rules/foundry-current.md",
                lambda plugin, home: str(plugin / "rules" / "current.md"),
                id="current-foundry-rule-link",
            ),
            # Symlink to a non-foundry path is left alone (user owns it).
            pytest.param(
                "rules/foundry-user.md", lambda plugin, home: "/somewhere/else/user.md", id="non-foundry-rule"
            ),
            pytest.param("skills/mine", lambda plugin, home: "/somewhere/else/mine", id="non-foundry-skill-link"),
            pytest.param(
                "agents/my-agent.md",
                lambda plugin, home: "/home/user/.claude/agents/my-agent.md",
                id="non-foundry-agent-symlink",
            ),
            # Init never re-creates these, so any link pointing at the current root was placed by something external;
            # leaving it intact gives the operator a clear signal to investigate without silently destroying state.
            pytest.param(
                "agents/sw-engineer.md",
                lambda plugin, home: str(plugin / "agents" / "sw-engineer.md"),
                id="current-version-agent-symlink",
            ),
            # Another plugin's namespace is never foundry's to prune.
            pytest.param(
                "rules/develop-quality-gates.md",
                lambda plugin, home: str(
                    home / ".claude/plugins/cache/borda-ai-rig/develop/0.19.0/rules/quality-gates.md"
                ),
                id="sibling-plugin-namespaced-link",
            ),
            # An unprefixed link failing the ownership proof survives migration untouched.
            pytest.param(
                "rules/current.md",
                lambda plugin, home: str(home / ".claude/plugins/cache/other-market/foundry/0.39.0/rules/current.md"),
                id="legacy-link-other-marketplace-target",
            ),
            pytest.param(
                "rules/current.md",
                lambda plugin, home: str(
                    home / ".claude/plugins/cache/borda-ai-rig/develop/0.19.0/rules/quality-gates.md"
                ),
                id="legacy-link-sibling-plugin-target",
            ),
            pytest.param(
                "rules/current.md",
                lambda plugin, home: str(home / "src/AI-Rig/plugins/cc_foundry/rules/current.md"),
                id="legacy-link-source-checkout-target",
            ),
            pytest.param(
                "rules/current.md",
                lambda plugin, home: str(home / "dotfiles/plugins/cc_foundry/rules/current.md"),
                id="legacy-link-dotfiles-target",
            ),
        ],
    )
    def test_keeps_link(self, env: tuple[Path, Path], link_rel: str, target) -> None:
        """A symlink the user or another plugin owns, or that is not provably foundry's, is left alone and unlogged.

        Scenario: a current namespaced rule link, non-foundry rule/skill/agent links, a current-root agent link, a
        sibling plugin's namespaced link and unprefixed links failing the ownership proof all survive cleanup.
        """
        plugin, home = env
        link = home / ".claude" / link_rel
        _ln(target(plugin, home), link)

        log = cleanup(plugin, home, _MARKER)

        assert link.is_symlink()
        assert log == []

    @pytest.mark.parametrize(
        ("real_rel", "make", "kind"),
        [
            pytest.param("rules/myown.md", lambda real: real.write_text("hand-written\n"), "is_file", id="rule-file"),
            pytest.param("skills/geo", lambda real: real.mkdir(), "is_dir", id="skill-dir"),
            pytest.param("agents/user.md", lambda real: real.write_text("user-authored\n"), "is_file", id="agent-file"),
        ],
    )
    def test_keeps_real_entry(self, env: tuple[Path, Path], real_rel: str, make, kind: str) -> None:
        """Real (non-symlink) files and dirs under ~/.claude/{rules,skills,agents}/ are never deleted by cleanup."""
        plugin, home = env
        real = home / ".claude" / real_rel
        make(real)

        cleanup(plugin, home, _MARKER)

        assert getattr(real, kind)()
        assert not real.is_symlink()

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

    def test_empty_when_no_obsolete_entries(self, env: tuple[Path, Path]) -> None:
        """Clean state → no removals, empty log."""
        plugin, home = env

        log = cleanup(plugin, home, _MARKER)

        assert log == []


class TestScan:
    """Scan: surfaces only conflicts requiring user confirmation."""

    @pytest.mark.parametrize(
        ("link_rel", "target"),
        [
            pytest.param(
                "rules/foundry-current.md",
                lambda plugin: str(plugin / "rules" / "current.md"),
                id="current-foundry-link",
            ),
            # Same-lineage stale symlink is auto-replaced in Phase 4.
            pytest.param(
                "rules/foundry-current.md",
                lambda plugin: str(_stale_root(plugin) / "rules" / "current.md"),
                id="stale-foundry-link",
            ),
            # A pre-namespace link is not at any current destination.
            pytest.param(
                "rules/current.md", lambda plugin: str(plugin / "rules" / "current.md"), id="legacy-unprefixed-link"
            ),
        ],
    )
    def test_link_is_not_a_conflict(self, env: tuple[Path, Path], link_rel: str, target) -> None:
        """A current, stale same-lineage or pre-namespace link raises no conflict for the user to confirm."""
        plugin, home = env
        _ln(target(plugin), home / ".claude" / link_rel)

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

    def test_main_create_mode_rejects_dotdot_traversal(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """CLI ``create`` rejects ``--dest "$HOME/.claude/.."`` instead of silently accepting it.

        Reproduces the CRITICAL PoC: this invocation previously returned exit 0 and
        would have copied plugin content directly into ``$HOME``.
        """
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        plugin_root = tmp_path / "plugin_root"
        plugin_root.mkdir()
        src = plugin_root / "src.md"
        src.write_text("body\n")

        rc = main(
            [
                "create",
                "--src",
                str(src),
                "--dest",
                str(home / ".claude" / ".."),
                "--home",
                str(home),
                "--plugin-root",
                str(plugin_root),
            ]
        )

        assert rc == 2
        assert "dest must be under" in capsys.readouterr().err


class TestAssertDestUnderHomeClaude:
    """_assert_dest_under_home_claude: containment must survive a `..` traversal leaf."""

    def test_rejects_dotdot_leaf_above_home_claude(self, tmp_path: Path) -> None:
        """``home/.claude/..`` resolves to ``home`` itself — one level outside the boundary.

        Reproduces the CRITICAL PoC: the un-normalised join ``resolved_parent / dest.name``
        keeps the literal ``..`` component, so ``resolved.parents`` reported ``home/.claude``
        as containing it even though the true location is ``home``.
        """
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        dest = Path(str(home) + "/.claude/..")

        with pytest.raises(ValueError, match="dest must be under"):
            symlink_with_guard._assert_dest_under_home_claude(dest, home)

    def test_accepts_normal_dest_under_rules(self, tmp_path: Path) -> None:
        """A normal, non-traversal dest under ``home/.claude/rules/`` still resolves and passes."""
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        dest = home / ".claude" / "rules" / "foundry-x.md"

        resolved = symlink_with_guard._assert_dest_under_home_claude(dest, home)

        assert resolved == dest.resolve()

    def test_rejects_dest_equal_to_home_claude_itself(self, tmp_path: Path) -> None:
        """``dest == home/.claude`` itself is rejected — no caller wants to replace the whole dir.

        Accepting exact equality (fixed by dropping the ``resolved == home_claude`` disjunct) let a fresh, not-yet-
        existing ``~/.claude`` be materialised as a symlink into the plugin tree via ``--dest "$HOME/.claude"``, wiping
        the directory boundary this function exists to enforce.
        """
        home = tmp_path / "home"
        home.mkdir()  # note: .claude does NOT exist yet — the vulnerable fresh-home case
        dest = home / ".claude"

        with pytest.raises(ValueError, match="dest must be under"):
            symlink_with_guard._assert_dest_under_home_claude(dest, home)


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

    def test_rejects_pre_existing_real_file_dest(self, tmp_path: Path) -> None:
        """A real file already at dest is never silently destroyed by the copy fallback.

        Reproduces the CRITICAL PoC: Tier 1 fails with ``FileExistsError`` against an
        occupied dest, and without the up-front guard Tier 3's ``shutil.copy2`` would
        overwrite it unconditionally.
        """
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        src = tmp_path / "src.md"
        src.write_text("new content\n")
        dest = home / ".claude" / "rules" / "foundry-b.md"
        dest.parent.mkdir(parents=True)
        dest.write_text("original user content\n")

        with pytest.raises(OSError, match="refusing to overwrite existing dest"):
            create_link(src, dest, home)

        assert dest.read_text() == "original user content\n"

    def test_rejects_pre_existing_live_symlink_outside_tree(self, tmp_path: Path) -> None:
        """A live symlink at dest pointing outside ``~/.claude`` is never followed and written through.

        Reproduces the CRITICAL PoC: dest is a legal ``--dest`` location whose existing
        entry is a symlink to ``outside/important.txt``. Without the guard, Tier 3's
        ``shutil.copy2`` follows the symlink and overwrites the external file, nullifying
        the ``--dest`` containment boundary this script otherwise enforces everywhere else.
        """
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        outside = tmp_path / "outside" / "important.txt"
        outside.parent.mkdir(parents=True)
        outside.write_text("do not touch\n")
        src = tmp_path / "src.md"
        src.write_text("new content\n")
        dest = home / ".claude" / "rules" / "foundry-c.md"
        dest.parent.mkdir(parents=True)
        dest.symlink_to(outside)

        with pytest.raises(OSError, match="refusing to overwrite existing dest"):
            create_link(src, dest, home)

        assert outside.read_text() == "do not touch\n"
        assert dest.is_symlink()

    def test_rejects_pre_existing_dangling_symlink(self, tmp_path: Path) -> None:
        """A dangling symlink at dest is rejected too — ``Path.exists()`` alone misses it.

        ``dest.exists()`` follows symlinks and is False for a dangling target, so the guard also checks
        ``dest.is_symlink()`` to catch this case before Tier 1 runs.
        """
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        src = tmp_path / "src.md"
        src.write_text("new content\n")
        dest = home / ".claude" / "rules" / "foundry-d.md"
        dest.parent.mkdir(parents=True)
        dest.symlink_to(tmp_path / "outside" / "gone.md")  # target never created

        with pytest.raises(OSError, match="refusing to overwrite existing dest"):
            create_link(src, dest, home)

        assert dest.is_symlink()
        assert not dest.exists()  # still dangling — untouched

    def test_race_created_dest_is_not_overwritten_by_copy_fallback(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A dest appearing between the guard check and Tier 1 still fails closed (TOCTOU).

        Simulates the race window: the up-front guard passes because dest is absent, but
        `symlink_to` itself raises ``FileExistsError`` (as it does when an entry appears in
        that window) — the exception must propagate, never fall through to Tier 3's overwrite.
        """
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        src = tmp_path / "src.md"
        src.write_text("new content\n")
        dest = home / ".claude" / "rules" / "foundry-e.md"
        dest.parent.mkdir(parents=True)

        monkeypatch.setattr(
            symlink_with_guard.Path,
            "symlink_to",
            lambda self, target: (_ for _ in ()).throw(FileExistsError("simulated race")),
        )

        with pytest.raises(FileExistsError):
            create_link(src, dest, home)

        assert not dest.exists()
        assert not dest.is_symlink()

    def test_rejects_pre_planted_symlink_at_sidecar_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A symlink planted at the deterministic sidecar path is refused, not followed.

        ``sidecar_path`` (``.{dest.name}.sourced_from``) is derived from ``dest`` before ``create_link`` is called, so
        an attacker (or a stale artifact) can plant a symlink there ahead of time pointing anywhere on disk. Without a
        guard, ``write_text`` follows it and truncates the target — the same escape class the ``dest`` guard above
        closes, just for the sidecar name instead of ``dest`` itself.
        """
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        src = tmp_path / "src_dir"
        src.mkdir()
        (src / "inner.txt").write_text("payload\n")
        dest = home / ".claude" / "rules" / "foundry-f.md"
        dest.parent.mkdir(parents=True)

        outside = tmp_path / "outside" / "do_not_touch.txt"
        outside.parent.mkdir(parents=True)
        outside.write_text("precious\n")

        sidecar_path = dest.parent / f".{dest.name}.sourced_from"
        sidecar_path.symlink_to(outside)

        # Force Tier 1 to fail and pin platform to non-Windows so Tier 2 (copy) runs.
        monkeypatch.setattr(
            symlink_with_guard.Path,
            "symlink_to",
            lambda self, target: (_ for _ in ()).throw(OSError("simulated symlink failure")),
        )
        monkeypatch.setattr(symlink_with_guard.sys, "platform", "linux")

        with pytest.raises(OSError, match="refusing to overwrite existing sidecar"):
            create_link(src, dest, home)

        assert outside.read_text() == "precious\n"  # untouched — not followed

    @pytest.mark.parametrize(
        ("given", "missing"),
        [
            pytest.param("--dest", "--src", id="missing-src"),
            pytest.param("--src", "--dest", id="missing-dest"),
        ],
    )
    def test_main_create_mode_missing_path(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        given: str,
        missing: str,
    ) -> None:
        """Reject create mode without a source path or without a destination path, naming the missing flag."""
        home = tmp_path / "home"
        home.mkdir()
        rc = main(["create", given, str(tmp_path / "x"), "--home", str(home)])
        assert rc == 2
        assert missing in capsys.readouterr().err


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
