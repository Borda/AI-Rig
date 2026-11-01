"""Tests for ``bin/resolve_skill_subdir.py``.

Doctests in the source cover ``_validate_token`` and ``resolve``'s miss-path. This file covers the four-tier cascade
against a real filesystem, plus the CLI surface (stdout, stderr, exit codes).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from resolve_skill_subdir import main, resolve

_LOCAL_AUDIT_TEMPLATES = Path("plugins") / "cc_foundry" / "skills" / "audit" / "templates"


def _make_cached_modes(cache: Path, versions: tuple[str, ...]) -> None:
    """Create ``<cache>/<version>/skills/calibrate/modes`` for every version in ``versions``."""
    for version in versions:
        (cache / version / "skills" / "calibrate" / "modes").mkdir(parents=True)


@pytest.fixture(name="fake_home")
def _fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provide a fake $HOME with an empty plugin cache subtree and no ``CLAUDE_PLUGIN_ROOT`` in the environment.

    Tests that need a plugin root set it themselves after this fixture has run.
    """
    home = tmp_path / "home"
    (home / ".claude" / "plugins" / "cache").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    return home


@pytest.fixture(name="cwd_with_local_tree")
def _cwd_with_local_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Chdir to a fresh project root; tests create the ``plugins/…`` or ``.claude/skills/…`` trees they need."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestResolveCascade:
    """Resolve: tier-1 (local) → tier-2 (plugin root) → tier-3 → tier-4 ordering."""

    def test_tier2_claude_plugin_root_wins_when_local_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_home: Path
    ) -> None:
        """When CLAUDE_PLUGIN_ROOT is set, the dir exists, and --local absent, that's the answer.

        Scenario: an installed plugin root contains ``skills/calibrate/modes``; with no ``--local`` the exported root is
        the first tier that matches.
        """
        plugin_root = tmp_path / "installed"
        target = plugin_root / "skills" / "calibrate" / "modes"
        target.mkdir(parents=True)
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

        result = resolve("calibrate", "modes", local=False, home=fake_home)

        assert result == target

    @pytest.mark.parametrize(
        ("local", "expected"),
        [
            pytest.param(False, None, id="bypassed-without-local-flag"),
            pytest.param(True, _LOCAL_AUDIT_TEMPLATES, id="used-with-local-flag"),
        ],
    )
    def test_tier1_local_source_requires_local_flag(
        self, cwd_with_local_tree: Path, fake_home: Path, local: bool, expected: Path | None
    ) -> None:
        """The local source tree is used only when ``--local`` is passed.

        Scenario: ``plugins/cc_foundry/skills/audit/templates`` exists under the working directory and nothing else
        matches. Without ``--local`` the tier is bypassed and the result is None; with it the source tree enters the
        cascade ahead of ``CLAUDE_PLUGIN_ROOT``.
        """
        (cwd_with_local_tree / _LOCAL_AUDIT_TEMPLATES).mkdir(parents=True)

        result = resolve("audit", "templates", local=local, home=fake_home)

        assert result == expected

    def test_local_true_wins_over_claude_plugin_root_when_both_present(
        self, cwd_with_local_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_home: Path
    ) -> None:
        """Regression: --local must override CLAUDE_PLUGIN_ROOT, not lose to it.

        Prior tier order checked CLAUDE_PLUGIN_ROOT before the local-source tier, so --local was a silent no-op whenever
        CLAUDE_PLUGIN_ROOT happened to be set (the common case inside any installed-plugin run). Both candidates exist
        here; the local source tree must win.
        """
        installed = tmp_path / "installed"
        (installed / "skills" / "audit" / "templates").mkdir(parents=True)
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(installed))
        (cwd_with_local_tree / _LOCAL_AUDIT_TEMPLATES).mkdir(parents=True)

        result = resolve("audit", "templates", local=True, home=fake_home)

        assert result == _LOCAL_AUDIT_TEMPLATES

    def test_tier3_project_local_override(
        self, cwd_with_local_tree: Path, fake_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Use the project skill override under --local when no higher resolution tier matches, and say so on stderr.

        The override is repository-supplied text that load_mode.py streams as skill instructions, so a hit is never
        silent: the warning names the resolved absolute path so the operator can see which tree supplied it.
        """
        override = cwd_with_local_tree / ".claude" / "skills" / "manage" / "templates"
        override.mkdir(parents=True)

        result = resolve("manage", "templates", local=True, home=fake_home)

        assert result == Path(".claude") / "skills" / "manage" / "templates"
        err = capsys.readouterr().err
        assert "! WARNING: project-local override in use" in err
        assert override.resolve().as_posix() in err

    def test_tier3_project_local_ignored_without_local(
        self, cwd_with_local_tree: Path, fake_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Without --local a cwd-relative .claude/skills override never outranks the installed plugin cache.

        A repository under review can ship ``.claude/skills/<skill>/modes``; honouring it by default would let that
        repository replace the instructions the installed plugin loads. The cached copy must win and nothing is printed.
        """
        (cwd_with_local_tree / ".claude" / "skills" / "audit" / "modes").mkdir(parents=True)
        cached = fake_home / ".claude" / "plugins" / "cache" / "borda-ai-rig" / "foundry" / "0.1.0" / "skills"
        (cached / "audit" / "modes").mkdir(parents=True)

        result = resolve("audit", "modes", local=False, home=fake_home)

        assert result == cached / "audit" / "modes"
        assert capsys.readouterr().err == ""

    def test_tier4_cache_scan_picks_highest(self, fake_home: Path) -> None:
        """Cache fallback returns the highest semver version, not the lexically greatest path.

        Scenario: ``0.10.0``, ``0.18.0`` and ``0.9.0`` are cached. A lexical sort would pick ``0.9.0``; the scan must
        pick ``0.18.0``.
        """
        cache = fake_home / ".claude" / "plugins" / "cache" / "borda-ai-rig" / "foundry"
        _make_cached_modes(cache, ("0.10.0", "0.18.0", "0.9.0"))

        result = resolve("calibrate", "modes", local=False, home=fake_home)

        assert result == cache / "0.18.0" / "skills" / "calibrate" / "modes"

    def test_all_tiers_miss_returns_none(self, fake_home: Path) -> None:
        """Nothing exists → None (CLI surface translates this to exit 1).

        Scenario: an empty home cache, no plugin root and no local trees.
        """
        assert resolve("nope", "missing", local=False, home=fake_home) is None


class TestMain:
    """Main: CLI surface — exit codes, stdout, stderr."""

    def test_success_prints_path_exit_0(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Resolved path printed to stdout, exit 0.

        Scenario: ``CLAUDE_PLUGIN_ROOT`` names an install holding ``skills/audit/templates``.
        """
        plugin_root = tmp_path / "installed"
        (plugin_root / "skills" / "audit" / "templates").mkdir(parents=True)
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

        rc = main(["audit", "templates", "--home", str(fake_home)])

        assert rc == 0
        assert Path(capsys.readouterr().out.strip()) == plugin_root / "skills" / "audit" / "templates"

    def test_missing_prints_breaking_exit_1(self, fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Report an unresolvable directory on stderr, starting with ``! BREAKING``, and exit 1.

        Scenario: neither plugin root, cache nor local tree holds ``nope/missing``; the diagnostic names the pair.
        """
        rc = main(["nope", "missing", "--home", str(fake_home)])

        assert rc == 1
        err = capsys.readouterr().err
        assert "! BREAKING" in err
        assert "nope/missing not found" in err

    @pytest.mark.parametrize(
        ("skill", "subdir"),
        [
            pytest.param("", "templates", id="empty"),
            pytest.param("audit", "", id="audit-empty"),
            pytest.param("../etc", "templates", id="..-etc"),
            pytest.param("audit", "../sneaky", id="audit-..-sneaky"),
            pytest.param("audit/sub", "templates", id="audit-sub"),
        ],
    )
    def test_invalid_token_exits_2(self, skill: str, subdir: str, capsys: pytest.CaptureFixture[str]) -> None:
        """Bad characters in skill or subdir → exit 2, stderr explains.

        Scenario: empty, path-traversing or separator-carrying names are refused before any lookup so they cannot steer
        the search outside the skills tree.
        """
        rc = main([skill, subdir])
        assert rc == 2
        assert "error:" in capsys.readouterr().err

    def test_local_flag_propagates(
        self, cwd_with_local_tree: Path, fake_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The ``--local`` flag enables local-source resolution at the CLI surface.

        Scenario: ``plugins/cc_foundry/skills/manage/templates`` exists under the working directory; with ``--local``
        the CLI prints that relative path and exits 0.
        """
        (cwd_with_local_tree / "plugins" / "cc_foundry" / "skills" / "manage" / "templates").mkdir(parents=True)

        rc = main(["manage", "templates", "--local", "--home", str(fake_home)])

        assert rc == 0
        assert Path(capsys.readouterr().out.strip()) == Path("plugins/cc_foundry/skills/manage/templates")
