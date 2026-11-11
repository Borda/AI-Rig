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
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import symlink_with_guard
from _hook_env import _bash_runs_posix_script
from symlink_with_guard import cleanup, create_link, main, scan

_MARKER = "borda-ai-rig/foundry/"
_SKILL_MD = Path(__file__).resolve().parents[3] / "skills" / "setup" / "SKILL.md"


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
        """``_readlink`` returns the target without the extended-length prefix.

        Windows reports link targets as ``\\\\?\\C:\\...``; stripping at the read boundary means no downstream ownership
        comparison ever sees the prefixed spelling.
        """
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
        """Both spellings are foundry-managed — the skills/agents purge must fire on each.

        A POSIX target and the same cache path spelled with backslashes on native Windows both carry the marker.
        """
        assert symlink_with_guard._is_foundry_managed(target, _MARKER)

    def test_foreign_simulated_windows_target_is_not_managed(self) -> None:
        """Separator normalisation must not widen the match to unrelated paths.

        A Windows-style dotfiles path has no marker, so normalising ``\\`` to ``/`` must still report it unmanaged.
        """
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
        """A same-lineage TEAM_PROTOCOL.md link is removed once the plugin no longer ships the file.

        The plugin's own copy is deleted, leaving the home link pointing at an older version of it; cleanup drops it.
        """
        plugin, home = env
        (plugin / "TEAM_PROTOCOL.md").unlink()
        link = home / ".claude" / "TEAM_PROTOCOL.md"
        _ln(str(_stale_root(plugin) / "TEAM_PROTOCOL.md"), link)

        log = cleanup(plugin, home, _MARKER)

        assert not link.is_symlink()
        assert "removed obsolete: TEAM_PROTOCOL.md" in log

    def test_keeps_foreign_team_protocol_when_source_gone(self, env: tuple[Path, Path]) -> None:
        """An unowned TEAM_PROTOCOL.md link survives even when foundry stops shipping the file.

        The link points into the user's dotfiles, which fail the ownership proof, so cleanup must leave it alone.
        """
        plugin, home = env
        (plugin / "TEAM_PROTOCOL.md").unlink()
        link = home / ".claude" / "TEAM_PROTOCOL.md"
        _ln(str(home / "dotfiles" / "TEAM_PROTOCOL.md"), link)

        log = cleanup(plugin, home, _MARKER)

        assert link.is_symlink()
        assert log == []

    def test_keeps_team_protocol_when_source_still_present(self, env: tuple[Path, Path]) -> None:
        """A stale foundry TEAM_PROTOCOL.md link stays while the plugin still ships the file.

        The link is not obsolete, only old, so removing it is left to the Phase 4 refresh.
        """
        plugin, home = env
        link = home / ".claude" / "TEAM_PROTOCOL.md"
        _ln(str(_stale_root(plugin) / "TEAM_PROTOCOL.md"), link)

        cleanup(plugin, home, _MARKER)

        assert link.is_symlink()

    def test_empty_when_no_obsolete_entries(self, env: tuple[Path, Path]) -> None:
        """A clean home removes nothing and logs nothing.

        The fixture home holds no links at all, so any log line would mean cleanup invented work.
        """
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
        """A symlink failing the ownership proof surfaces as a conflict descriptor.

        The foundry-namespaced destination already holds a link into the user's dotfiles, so Phase 4 must ask first.
        """
        plugin, home = env
        _ln("/home/user/dotfiles/another.md", home / ".claude" / "rules" / "foundry-another.md")

        conflicts = scan(plugin, home, _MARKER)

        assert conflicts == ["rules/foundry-another.md → /home/user/dotfiles/another.md"]

    def test_reports_other_marketplace_symlink(self, env: tuple[Path, Path]) -> None:
        """Another marketplace's cache path is a different lineage — conflict, not a silent refresh.

        A same-named plugin cached under a different marketplace is not an older foundry version, so it is reported.
        """
        plugin, home = env
        foreign = home / ".claude/plugins/cache/other-market/foundry/0.39.0/rules/current.md"
        _ln(str(foreign), home / ".claude" / "rules" / "foundry-current.md")

        conflicts = scan(plugin, home, _MARKER)

        assert conflicts == [f"rules/foundry-current.md → {foreign}"]

    def test_reports_real_file_conflict(self, env: tuple[Path, Path]) -> None:
        """A real file at a destination path surfaces with the ``(real file)`` suffix.

        A hand-written rule occupying a foundry destination would be overwritten by linking, so the user must confirm.
        """
        plugin, home = env
        (home / ".claude" / "rules" / "foundry-current.md").write_text("hand-written\n")

        conflicts = scan(plugin, home, _MARKER)

        assert "rules/foundry-current.md  (real file)" in conflicts

    def test_reports_team_protocol_conflict(self, env: tuple[Path, Path]) -> None:
        """A TEAM_PROTOCOL.md conflict is labelled without the ``rules/`` or ``skills/`` prefix.

        The file sits directly under ``~/.claude``, so its descriptor is the bare file name.
        """
        plugin, home = env
        _ln("/somewhere/else/team.md", home / ".claude" / "TEAM_PROTOCOL.md")

        conflicts = scan(plugin, home, _MARKER)

        assert "TEAM_PROTOCOL.md → /somewhere/else/team.md" in conflicts

    def test_plugin_skill_real_dir_is_not_a_conflict(self, env: tuple[Path, Path]) -> None:
        """A directory named after a plugin skill is no conflict — skills are never linked into $HOME.

        The user's own ``skills/curator`` directory shares a name with a shipped skill but is never a link target.
        """
        plugin, home = env
        (home / ".claude" / "skills" / "curator").mkdir()

        conflicts = scan(plugin, home, _MARKER)

        assert conflicts == []

    def test_no_conflicts_on_empty_dest(self, env: tuple[Path, Path]) -> None:
        """Absent destination entries are not conflicts.

        Nothing occupies the destinations on a fresh home, so there is nothing to confirm; Phase 4 creates them.
        """
        plugin, home = env

        assert scan(plugin, home, _MARKER) == []


@pytest.fixture(name="fake_home")
def _fake_home(tmp_path: Path) -> Path:
    """Fake ``$HOME`` holding an empty ``.claude`` directory."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    return home


@pytest.fixture(name="src_file")
def _src_file(tmp_path: Path) -> Path:
    """Create a small source file outside the fake home, standing in for a shipped rule."""
    src = tmp_path / "src.md"
    src.write_text("new content\n")
    return src


def _raise_on_symlink(error: OSError):
    """Build a ``Path.symlink_to`` replacement that always raises ``error``."""

    def symlink_to(self: Path, target: object) -> None:
        raise error

    return symlink_to


@pytest.fixture(name="symlink_fails")
def _symlink_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make tier-1 symlinking raise ``OSError`` on a non-Windows platform, so ``create_link`` reaches the copy tier."""
    monkeypatch.setattr(symlink_with_guard.Path, "symlink_to", _raise_on_symlink(OSError("simulated symlink failure")))
    monkeypatch.setattr(symlink_with_guard.sys, "platform", "linux")


class TestMain:
    """Main: CLI surface — stdout, stderr, exit codes."""

    def test_cleanup_prints_log_lines_indented(
        self, env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Cleanup mode emits ``  removed obsolete: ...`` lines (two-space indent).

        A stale rule link whose source was deleted is removed, and the log line is printed for the setup skill to show.
        """
        plugin, home = env
        _ln(str(_stale_root(plugin) / "rules" / "another.md"), home / ".claude" / "rules" / "foundry-another.md")
        (plugin / "rules" / "another.md").unlink()

        rc = main(["cleanup", "--plugin-root", str(plugin), "--home", str(home)])

        assert rc == 0
        assert "  removed obsolete: foundry-another.md" in capsys.readouterr().out

    def test_scan_prints_one_conflict_per_line(
        self, env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Scan mode emits the bash-array-ready format on stdout.

        One conflict descriptor per line lets the setup skill read the output straight into a shell array.
        """
        plugin, home = env
        _ln("/elsewhere/foo.md", home / ".claude" / "rules" / "foundry-current.md")

        rc = main(["scan", "--plugin-root", str(plugin), "--home", str(home)])

        assert rc == 0
        assert capsys.readouterr().out.strip().splitlines() == ["rules/foundry-current.md → /elsewhere/foo.md"]

    def test_missing_plugin_root_exits_1(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A non-existent plugin root path is a hard error.

        Pointing the tool at a directory that is not there must fail loudly instead of reporting a clean home.
        """
        rc = main(["scan", "--plugin-root", str(tmp_path / "nope")])
        assert rc == 1
        assert "is not a directory" in capsys.readouterr().err

    def test_custom_marker_respected(self, env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
        """A custom ownership marker is used when purging skill links.

        The rules and TEAM_PROTOCOL scopes ignore the marker entirely — they are destinations foundry writes, so they
        demand the stricter path-lineage proof instead.
        """
        plugin, home = env
        _ln("/x/custom-marker/0.1/skills/curator", home / ".claude" / "skills" / "curator")

        rc = main(["cleanup", "--plugin-root", str(plugin), "--home", str(home), "--marker", "custom-marker/"])

        assert rc == 0
        assert "  removed user-level skill link: curator" in capsys.readouterr().out

    def test_marker_does_not_authorise_rule_deletion(
        self, env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A marker-matching but unowned rules link is never deleted.

        The marker only proves ownership of skill links; a rules link needs the path-lineage proof it lacks.
        """
        plugin, home = env
        link = home / ".claude" / "rules" / "current.md"
        _ln("/x/custom-marker/0.1/rules/current.md", link)

        rc = main(["cleanup", "--plugin-root", str(plugin), "--home", str(home), "--marker", "custom-marker/"])

        assert rc == 0
        assert link.is_symlink()
        assert "removed obsolete" not in capsys.readouterr().out

    def test_create_rejects_dotdot_traversal(
        self, tmp_path: Path, fake_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """CLI ``create`` rejects ``--dest "$HOME/.claude/.."`` instead of silently accepting it.

        Reproduces the CRITICAL PoC: this invocation previously returned exit 0 and would have copied plugin content
        directly into ``$HOME``.
        """
        plugin_root = tmp_path / "plugin_root"
        plugin_root.mkdir()
        src = plugin_root / "src.md"
        src.write_text("body\n")
        dest = fake_home / ".claude" / ".."

        rc = main(
            [
                "create",
                "--src",
                str(src),
                "--dest",
                str(dest),
                "--home",
                str(fake_home),
                "--plugin-root",
                str(plugin_root),
            ]
        )

        assert rc == 2
        assert "dest must be under" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("given", "missing"),
        [
            pytest.param("--dest", "--src", id="missing-src"),
            pytest.param("--src", "--dest", id="missing-dest"),
        ],
    )
    def test_create_rejects_missing_path(
        self, tmp_path: Path, fake_home: Path, capsys: pytest.CaptureFixture[str], given: str, missing: str
    ) -> None:
        """Create mode without a source path or without a destination path is rejected, naming the missing flag.

        Each case passes one of the two required flags and expects exit 2 with the other flag named on stderr.
        """
        rc = main(["create", given, str(tmp_path / "x"), "--home", str(fake_home)])
        assert rc == 2
        assert missing in capsys.readouterr().err


class TestAssertDestUnderHomeClaude:
    """_assert_dest_under_home_claude: containment must survive a `..` traversal leaf."""

    def test_rejects_dotdot_leaf_above_home_claude(self, fake_home: Path) -> None:
        """``home/.claude/..`` resolves to ``home`` itself — one level outside the boundary.

        Reproduces the CRITICAL PoC: the un-normalised join ``resolved_parent / dest.name`` keeps the literal ``..``
        component, so ``resolved.parents`` reported ``home/.claude`` as containing it even though the true location is
        ``home``.
        """
        dest = Path(str(fake_home) + "/.claude/..")

        with pytest.raises(ValueError, match="dest must be under"):
            symlink_with_guard._assert_dest_under_home_claude(dest, fake_home)

    def test_accepts_normal_dest_under_rules(self, fake_home: Path) -> None:
        """A normal, non-traversal dest under ``home/.claude/rules/`` still resolves and passes.

        Guards against the traversal fix over-rejecting the ordinary destination every rule link uses.
        """
        dest = fake_home / ".claude" / "rules" / "foundry-x.md"

        resolved = symlink_with_guard._assert_dest_under_home_claude(dest, fake_home)

        assert resolved == dest.resolve()

    def test_rejects_dest_equal_to_home_claude_itself(self, tmp_path: Path) -> None:
        """``dest == home/.claude`` itself is rejected — no caller wants to replace the whole dir.

        Accepting exact equality (fixed by dropping the ``resolved == home_claude`` disjunct) let a fresh, not-yet-
        existing ``~/.claude`` be materialised as a symlink into the plugin tree via ``--dest "$HOME/.claude"``, wiping
        the directory boundary this function exists to enforce.
        """
        home = tmp_path / "home"
        home.mkdir()  # .claude does NOT exist yet — the vulnerable fresh-home case
        dest = home / ".claude"

        with pytest.raises(ValueError, match="dest must be under"):
            symlink_with_guard._assert_dest_under_home_claude(dest, home)


class TestCreateLink:
    """create_link: 3-tier cascade (symlink → junction → copy + sidecar)."""

    def test_links_directory(self, tmp_path: Path, fake_home: Path) -> None:
        """A directory source and a non-existent destination give a real symlink on POSIX.

        Tier 1 succeeds, so the destination is a link and the source contents are reachable through it.
        """
        src = tmp_path / "src_dir"
        src.mkdir()
        (src / "file.txt").write_text("hello\n")
        dest = tmp_path / "dest_link"

        tier = create_link(src, dest, fake_home)

        assert tier == "symlink"
        assert dest.is_symlink()
        assert (dest / "file.txt").read_text() == "hello\n"

    def test_links_file(self, tmp_path: Path, fake_home: Path, src_file: Path) -> None:
        """A file source and a non-existent destination give a real symlink on POSIX.

        Rule files are linked individually, so the file form of tier 1 is the everyday path.
        """
        dest = tmp_path / "dest_file.md"

        tier = create_link(src_file, dest, fake_home)

        assert tier == "symlink"
        assert dest.is_symlink()
        assert dest.read_text() == "new content\n"

    @pytest.mark.usefixtures("symlink_fails")
    def test_falls_to_copy_when_symlink_fails(self, tmp_path: Path, fake_home: Path) -> None:
        """Tier 1 raises ``OSError`` and tier 2 is skipped (non-Windows), so tier 3 copies and writes a sidecar.

        Hosts that cannot create symlinks still get the content, plus a sidecar recording where it came from.
        """
        src = tmp_path / "src_dir"
        src.mkdir()
        (src / "inner.txt").write_text("payload\n")
        dest = tmp_path / "out" / "dest_dir"

        tier = create_link(src, dest, fake_home)

        sidecar = dest.parent / f".{dest.name}.sourced_from"
        assert tier == "copy"
        assert dest.is_dir()
        assert not dest.is_symlink()
        assert (dest / "inner.txt").read_text() == "payload\n"
        assert sidecar.read_text()

    @pytest.mark.usefixtures("symlink_fails")
    def test_sidecar_uses_relative_path(self, tmp_path: Path, fake_home: Path) -> None:
        """A source under ``home/.claude`` is recorded in the sidecar as a relative posix path (no leading ``/``).

        A relative record stays valid when the home directory moves between machines.
        """
        src = fake_home / ".claude" / "plugins" / "foundry" / "rules" / "x.md"
        src.parent.mkdir(parents=True)
        src.write_text("rule body\n")
        dest = tmp_path / "out" / "x.md"

        tier = create_link(src, dest, fake_home)

        assert tier == "copy"
        assert (dest.parent / f".{dest.name}.sourced_from").read_text() == "plugins/foundry/rules/x.md\n"

    @pytest.mark.usefixtures("symlink_fails")
    def test_sidecar_falls_back_to_absolute(self, tmp_path: Path, fake_home: Path) -> None:
        """A source outside ``home/.claude`` makes ``relative_to`` fail, so the sidecar stores its absolute path.

        Sources from a plugin checkout or other tree cannot be made relative to the home, and must still be traceable.
        """
        src = tmp_path / "elsewhere" / "external.md"
        src.parent.mkdir(parents=True)
        src.write_text("external content\n")
        dest = tmp_path / "out" / "external.md"

        tier = create_link(src, dest, fake_home)

        assert tier == "copy"
        assert (dest.parent / f".{dest.name}.sourced_from").read_text() == src.as_posix() + "\n"

    def test_rejects_pre_existing_real_file_dest(self, fake_home: Path, src_file: Path) -> None:
        """A real file already at the destination is never silently destroyed by the copy fallback.

        Reproduces the CRITICAL PoC: tier 1 fails with ``FileExistsError`` against an occupied destination, and without
        the up-front guard tier 3's ``shutil.copy2`` would overwrite it unconditionally.
        """
        dest = fake_home / ".claude" / "rules" / "foundry-b.md"
        dest.parent.mkdir(parents=True)
        dest.write_text("original user content\n")

        with pytest.raises(OSError, match="refusing to overwrite existing dest"):
            create_link(src_file, dest, fake_home)

        assert dest.read_text() == "original user content\n"

    def test_rejects_pre_existing_live_symlink_outside_tree(
        self, tmp_path: Path, fake_home: Path, src_file: Path
    ) -> None:
        """A live symlink at the destination pointing outside ``~/.claude`` is never followed and written through.

        Reproduces the CRITICAL PoC: the destination is a legal ``--dest`` location whose existing entry is a symlink to
        ``outside/important.txt``. Without the guard, tier 3's ``shutil.copy2`` follows the symlink and overwrites the
        external file, nullifying the ``--dest`` containment boundary this script otherwise enforces everywhere else.
        """
        outside = tmp_path / "outside" / "important.txt"
        outside.parent.mkdir(parents=True)
        outside.write_text("do not touch\n")
        dest = fake_home / ".claude" / "rules" / "foundry-c.md"
        dest.parent.mkdir(parents=True)
        dest.symlink_to(outside)

        with pytest.raises(OSError, match="refusing to overwrite existing dest"):
            create_link(src_file, dest, fake_home)

        assert outside.read_text() == "do not touch\n"
        assert dest.is_symlink()

    def test_rejects_pre_existing_dangling_symlink(self, tmp_path: Path, fake_home: Path, src_file: Path) -> None:
        """A dangling symlink at the destination is rejected too — ``Path.exists()`` alone misses it.

        ``dest.exists()`` follows symlinks and is False for a dangling target, so the guard also checks
        ``dest.is_symlink()`` to catch this case before tier 1 runs.
        """
        dest = fake_home / ".claude" / "rules" / "foundry-d.md"
        dest.parent.mkdir(parents=True)
        dest.symlink_to(tmp_path / "outside" / "gone.md")

        with pytest.raises(OSError, match="refusing to overwrite existing dest"):
            create_link(src_file, dest, fake_home)

        assert dest.is_symlink()
        assert not dest.exists()

    def test_race_created_dest_is_not_overwritten_by_copy_fallback(
        self, fake_home: Path, src_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A destination appearing between the guard check and tier 1 still fails closed (TOCTOU).

        Simulates the race window: the up-front guard passes because the destination is absent, but ``symlink_to``
        itself raises ``FileExistsError`` (as it does when an entry appears in that window) — the exception must
        propagate, never fall through to tier 3's overwrite.
        """
        dest = fake_home / ".claude" / "rules" / "foundry-e.md"
        dest.parent.mkdir(parents=True)
        monkeypatch.setattr(symlink_with_guard.Path, "symlink_to", _raise_on_symlink(FileExistsError("simulated race")))

        with pytest.raises(FileExistsError):
            create_link(src_file, dest, fake_home)

        assert not dest.exists()
        assert not dest.is_symlink()

    @pytest.mark.usefixtures("symlink_fails")
    def test_rejects_pre_planted_symlink_at_sidecar_path(self, tmp_path: Path, fake_home: Path) -> None:
        """A symlink planted at the deterministic sidecar path is refused, not followed.

        ``sidecar_path`` (``.{dest.name}.sourced_from``) is derived from ``dest`` before ``create_link`` is called, so
        an attacker (or a stale artifact) can plant a symlink there ahead of time pointing anywhere on disk. Without a
        guard, ``write_text`` follows it and truncates the target — the same escape class the ``dest`` guard above
        closes, just for the sidecar name instead of ``dest`` itself.
        """
        src = tmp_path / "src_dir"
        src.mkdir()
        (src / "inner.txt").write_text("payload\n")
        dest = fake_home / ".claude" / "rules" / "foundry-f.md"
        dest.parent.mkdir(parents=True)
        outside = tmp_path / "outside" / "do_not_touch.txt"
        outside.parent.mkdir(parents=True)
        outside.write_text("precious\n")
        os.symlink(outside, dest.parent / f".{dest.name}.sourced_from")

        with pytest.raises(OSError, match="refusing to overwrite existing sidecar"):
            create_link(src, dest, fake_home)

        assert outside.read_text() == "precious\n"


def _fenced_bash_blocks(text: str) -> list[str]:
    """Split Markdown into the bodies of its fenced ``bash`` blocks, reading line by line."""
    blocks: list[str] = []
    current: list[str] | None = None
    for line in text.splitlines(keepends=True):
        if current is None:
            current = [] if line.rstrip() == "```bash" else None
        elif line.startswith("```"):
            blocks.append("".join(current))
            current = None
        else:
            current.append(line)
    return blocks


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
    blocks = _fenced_bash_blocks(_SKILL_MD.read_text(encoding="utf-8"))
    matches = [b for b in blocks if 'for src in "$PLUGIN_ROOT/rules/"*.md' in b]
    assert len(matches) == 1, f"expected exactly one Phase 4 link loop in {_SKILL_MD}, found {len(matches)}"
    return matches[0]


def _run_phase4(plugin: Path, home: Path, tmpdir: Path) -> subprocess.CompletedProcess[str]:
    """Run the Phase 4 block in real ``bash`` against a fake plugin root and home, with ``tmpdir`` as ``TMPDIR``."""
    run_env = {
        **os.environ,
        "HOME": str(home),
        "PLUGIN_ROOT": str(plugin),
        "TMPDIR": str(tmpdir),
        "CLAUDE_CODE_SESSION_ID": "test-session",
    }
    return subprocess.run(["bash", "-c", _phase4_block()], env=run_env, capture_output=True, text=True, check=False)


class TestSkillPhase4Block:
    """The executable SKILL.md block must agree with this module's destinations."""

    @pytest.mark.skipif(
        not _BASH_SYMLINKS,
        reason="`bash` on PATH cannot create symlinks (WSL launcher stub, or Git Bash without winsymlinks)",
    )
    def test_block_creates_namespaced_links(self, env: tuple[Path, Path], tmp_path: Path) -> None:
        """Running the real block produces exactly the destinations ``_build_entries`` expects.

        Capability-gated, not platform-gated: the block uses the guarded native link helper, so a host whose ``bash``
        cannot link has nothing to assert about. The real shell run is the contract here, so it is not mocked.
        """
        plugin, home = env
        (plugin / "bin").mkdir()
        shutil.copy2(Path(symlink_with_guard.__file__), plugin / "bin/symlink_with_guard.py")
        # Phase 2 marks a completed conflict scan; Phase 4 refuses to link without it
        (tmp_path / "foundry-setup-scan-ok-test-session").write_text("true\n", encoding="utf-8")

        proc = _run_phase4(plugin, home, tmp_path)

        rules_dest = home / ".claude" / "rules"
        assert proc.returncode == 0, proc.stderr
        assert sorted(p.name for p in rules_dest.iterdir()) == ["foundry-another.md", "foundry-current.md"]
        assert (rules_dest / "foundry-current.md").readlink() == plugin / "rules" / "current.md"
        assert (home / ".claude" / "TEAM_PROTOCOL.md").is_symlink()

    @pytest.mark.skipif(not _bash_runs_posix_script(), reason="bash cannot execute a POSIX script")
    def test_block_refuses_without_completed_scan(self, env: tuple[Path, Path], tmp_path: Path) -> None:
        """A missing Phase 2 scan marker blocks linking, so a failed scan never reads as zero conflicts.

        No marker file exists in the private ``TMPDIR``, so the real block must print ``BLOCKED`` and create no links.
        """
        plugin, home = env

        proc = _run_phase4(plugin, home, tmp_path)

        assert proc.returncode == 1
        assert "BLOCKED" in proc.stdout
        assert not (home / ".claude" / "TEAM_PROTOCOL.md").is_symlink()
