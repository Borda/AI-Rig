"""Filesystem regression tests for ``bin/sync_rules.py``.

Every mutation the helper can perform has a case here, because the reverted first attempt at this feature shipped two
unsafe ones: a foreign marketplace link was refreshed, and a ``dotfiles/`` link was deleted, both because ownership was
a path substring match. The ownership cases below are the guard against that.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path, PureWindowsPath
from unittest import mock

import pytest
import sync_rules
from sync_rules import VARIANTS, SourceError, cache_lineage, dest_name, main, owns, read_link_target, sync

MARKETPLACE = "borda-ai-rig"
PLUGIN = "develop"
VARIANT = VARIANTS[0]
BOTH_VARIANTS = {VARIANT.full: "# full gates\n", VARIANT.delta: "# delta gates\n"}
_DEST_NAME = "develop-quality-gates.md"
_WIN_ROOT = PureWindowsPath(r"C:\h\.claude\plugins\cache\borda-ai-rig\develop\0.19.0")


def _make_home(tmp_path: Path) -> Path:
    """Create a disposable home with an empty ``~/.claude/rules``.

    Examples:
        >>> home = _make_home(getfixture("tmp_path"))
        >>> home.joinpath(".claude", "rules").is_dir()
        True
    """
    home = tmp_path / "home"
    (home / ".claude" / "rules").mkdir(parents=True)
    return home


def _make_plugin(root: Path, name: str = PLUGIN, rules: dict[str, str] | None = None) -> Path:
    """Create a minimal plugin tree with a manifest and rule files.

    Examples:
        >>> root = _make_plugin(getfixture("tmp_path") / "plugin")
        >>> json.loads(root.joinpath(".claude-plugin", "plugin.json").read_text())["name"]
        'develop'
    """
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": "0.1.0"}), encoding="utf-8"
    )
    (root / "rules").mkdir(parents=True)
    for rule_name, body in (rules or {"quality-gates.md": "# gates\n"}).items():
        (root / "rules" / rule_name).write_text(body, encoding="utf-8")
    return root


def _installed_root(
    home: Path,
    version: str = "0.19.0",
    plugin: str = PLUGIN,
    marketplace: str = MARKETPLACE,
    rules: dict[str, str] | None = None,
) -> Path:
    """Create an installed-cache plugin root under ``home``.

    Examples:
        >>> home = _make_home(getfixture("tmp_path"))
        >>> _installed_root(home).relative_to(home).as_posix()
        '.claude/plugins/cache/borda-ai-rig/develop/0.19.0'
    """
    root = home / ".claude" / "plugins" / "cache" / marketplace / plugin / version
    _make_plugin(root, name=plugin, rules=rules)
    _record_install(home, root, plugin, marketplace)
    return root


def _record_install(home: Path, root: Path, plugin: str, marketplace: str = MARKETPLACE) -> None:
    """Record a plugin's first fixture installation without activating later stale roots."""
    registry = home / ".claude" / "plugins" / "installed_plugins.json"
    data = json.loads(registry.read_text(encoding="utf-8")) if registry.exists() else {"plugins": {}}
    data["plugins"].setdefault(f"{plugin}@{marketplace}", [{"installPath": str(root)}])
    registry.write_text(json.dumps(data), encoding="utf-8")


def _dest(home: Path, source_name: str = "quality-gates.md", plugin: str = PLUGIN) -> Path:
    """Return the namespaced destination path for a source rule.

    Examples:
        >>> _dest(getfixture("tmp_path")).name
        'develop-quality-gates.md'
    """
    return home / ".claude" / "rules" / dest_name(plugin, source_name)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """Disposable home directory with an empty ``~/.claude/rules``."""
    return _make_home(tmp_path)


@pytest.fixture
def root(home: Path) -> Path:
    """Installed-cache root of the develop plugin (version 0.19.0) shipping one ``quality-gates.md`` rule."""
    return _installed_root(home)


class TestCacheLineage:
    """A plugin root has a cache lineage only when it lives in the installed-plugin cache."""

    def test_only_for_installed_roots(self, tmp_path: Path, home: Path, root: Path) -> None:
        """An installed root reports its version-parent directory; a source checkout reports none.

        The lineage is what ownership is judged against, so a checkout must never be mistaken for an installed copy.
        """
        assert cache_lineage(root, home) == root.parent
        assert cache_lineage(tmp_path / "checkout" / "plugins" / "cc_develop", home) is None


class TestOwnership:
    """``owns`` decides whether an existing link may be refreshed or pruned; it must never claim a foreign target."""

    @pytest.mark.parametrize(
        "version",
        [pytest.param("0.19.0", id="current-root"), pytest.param("0.18.5", id="same-lineage-stale-version")],
    )
    def test_owns_same_lineage_targets(self, home: Path, root: Path, version: str) -> None:
        """A link into the current root, or into an older version of the same plugin and marketplace, is owned."""
        target = home / ".claude" / "plugins" / "cache" / MARKETPLACE / PLUGIN / version / "rules" / "quality-gates.md"
        assert owns(_dest(home), str(target), root, cache_lineage(root, home))

    @pytest.mark.parametrize(
        "target_rel",
        [
            # another marketplace, same plugin name — different lineage
            pytest.param(
                ".claude/plugins/cache/other-marketplace/develop/0.18.5/rules/quality-gates.md", id="marketplace"
            ),
            # same marketplace, another plugin — different lineage
            pytest.param(".claude/plugins/cache/borda-ai-rig/oss/0.24.5/rules/quality-gates.md", id="other-plugin"),
            # arbitrary source checkout
            pytest.param("src/AI-Rig/plugins/cc_develop/rules/quality-gates.md", id="source-checkout"),
            # dotfiles path — the exact shape the reverted attempt deleted
            pytest.param("dotfiles/plugins/cc_develop/rules/quality-gates.md", id="dotfiles"),
            # a similarly named plugin must not enter the Develop lineage through a shared name prefix
            pytest.param(
                ".claude/plugins/cache/borda-ai-rig/develop-extra/0.1.0/rules/quality-gates.md", id="name-prefix"
            ),
        ],
    )
    def test_does_not_own_foreign_targets(self, home: Path, root: Path, target_rel: str) -> None:
        """Targets outside this plugin's cache lineage are never owned, whatever their path resembles."""
        assert not owns(_dest(home), str(home / target_rel), root, cache_lineage(root, home))


class TestInstall:
    """Linking rules into ``~/.claude/rules`` under the plugin-prefixed name."""

    def test_creates_link_when_absent(self, home: Path, root: Path) -> None:
        """A first sync links the plugin-prefixed destination at the installed rule and reports it as linked."""
        result = sync(PLUGIN, root, home)
        assert result.linked == [_DEST_NAME]
        assert _dest(home).is_symlink()
        assert _dest(home).name == _DEST_NAME
        assert Path(read_link_target(_dest(home))) == root / "rules" / "quality-gates.md"

    def test_sibling_plugins_do_not_collide(self, home: Path, root: Path) -> None:
        """Two plugins shipping the same rule name each get their own prefixed link.

        Without the plugin prefix the second sync would overwrite the first plugin's rule.
        """
        oss = _make_plugin(home / ".claude" / "plugins" / "cache" / MARKETPLACE / "oss" / "0.25.0", name="oss")
        _record_install(home, oss, "oss")
        sync(PLUGIN, root, home)
        sync("oss", oss, home)
        assert Path(read_link_target(_dest(home))).parent.parent == root
        assert Path(read_link_target(_dest(home, plugin="oss"))).parent.parent == oss

    def test_creates_rules_dir_when_missing(self, tmp_path: Path) -> None:
        """A home without ``~/.claude/rules`` gets the directory created before linking."""
        home = tmp_path / "home"
        home.mkdir()
        root = _installed_root(home)
        sync(PLUGIN, root, home)
        assert _dest(home).is_symlink()

    def test_relative_plugin_root_still_links_absolutely(
        self, home: Path, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A relative ``--plugin-root`` must not become a dangling relative symlink target."""
        monkeypatch.chdir(root.parent)
        sync(PLUGIN, Path(root.name), home)
        assert Path(read_link_target(_dest(home))).is_absolute()
        assert _dest(home).resolve().is_file()

    def test_idempotent_rerun_reports_unchanged(self, home: Path, root: Path) -> None:
        """Running sync twice links once; the rerun reports the link as unchanged."""
        sync(PLUGIN, root, home)
        second = sync(PLUGIN, root, home)
        assert (second.linked, second.unchanged) == ([], [_DEST_NAME])

    def test_refreshes_same_lineage_stale_link(self, home: Path, root: Path) -> None:
        """A link into an older version of the same plugin is repointed at the current root."""
        old = _installed_root(home, version="0.18.5")
        _dest(home).symlink_to(old / "rules" / "quality-gates.md")
        result = sync(PLUGIN, root, home)
        assert result.linked == [_DEST_NAME]
        assert Path(read_link_target(_dest(home))) == root / "rules" / "quality-gates.md"

    def test_refreshes_broken_owned_link(self, home: Path, root: Path) -> None:
        """A dangling link into the current root is owned, so it is repaired."""
        _dest(home).symlink_to(root / "rules" / "renamed-away.md")
        result = sync(PLUGIN, root, home)
        assert result.linked == [_DEST_NAME]
        assert Path(read_link_target(_dest(home))) == root / "rules" / "quality-gates.md"


class TestConflicts:
    """Anything at the destination that the plugin does not own is preserved and reported, never overwritten."""

    def test_real_file_is_preserved(self, home: Path, root: Path) -> None:
        """A user's own file at the destination name is left intact and reported as a conflict."""
        _dest(home).write_text("user content\n", encoding="utf-8")
        result = sync(PLUGIN, root, home)
        assert result.conflicts == [f"{_DEST_NAME}  (real file)"]
        assert _dest(home).read_text(encoding="utf-8") == "user content\n"
        assert not _dest(home).is_symlink()

    def test_foreign_link_is_preserved(self, home: Path, root: Path) -> None:
        """A link into the user's own dotfiles is reported as a conflict, kept, and its target file survives."""
        foreign = home / "dotfiles" / "rules" / "quality-gates.md"
        foreign.parent.mkdir(parents=True)
        foreign.write_text("mine\n", encoding="utf-8")
        _dest(home).symlink_to(foreign)
        result = sync(PLUGIN, root, home)
        assert result.conflicts == [f"{_DEST_NAME} → {foreign}"]
        assert Path(read_link_target(_dest(home))) == foreign
        assert foreign.is_file()

    @pytest.mark.parametrize(
        "target_for",
        [
            pytest.param(lambda home: Path("../../dotfiles/quality-gates.md"), id="relative"),
            pytest.param(
                lambda home: home / "src" / "AI-Rig" / "plugins" / "cc_develop" / "rules" / "q.md", id="dangling"
            ),
        ],
    )
    def test_unresolvable_foreign_link_is_preserved(
        self, home: Path, root: Path, target_for: Callable[[Path], Path]
    ) -> None:
        """A relative or dangling link that leaves the plugin's cache is foreign: reported and left pointing as
        before."""
        target = target_for(home)
        _dest(home).symlink_to(target)
        result = sync(PLUGIN, root, home)
        assert result.conflicts == [f"{_DEST_NAME} → {target}"]
        assert Path(read_link_target(_dest(home))) == target

    def test_relative_owned_target_is_recognised(self, home: Path, root: Path) -> None:
        """A relative link that resolves into the current root is owned, so it is no conflict."""
        _dest(home).symlink_to(Path("..") / ".." / root.relative_to(home) / "rules" / "quality-gates.md")
        result = sync(PLUGIN, root, home)
        assert (result.conflicts, result.unchanged) == ([], [_DEST_NAME])
        assert _dest(home).resolve() == root / "rules" / "quality-gates.md"

    def test_explicit_choice_replaces_real_file(self, home: Path, root: Path) -> None:
        """An explicit user-file replacement backs up the real file before installing the plugin link."""
        _dest(home).write_text("user content\n", encoding="utf-8")
        result = sync(PLUGIN, root, home, replace_user_files=True)
        assert result.replaced == [_DEST_NAME]
        assert Path(read_link_target(_dest(home))) == root / "rules" / "quality-gates.md"
        backups = list(_dest(home).parent.glob(f"{_DEST_NAME}.bak-*"))
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == "user content\n"


class TestPruning:
    """Obsolete entries carrying this plugin's prefix are removed only when the plugin owns them."""

    def test_removes_obsolete_owned_link(self, home: Path, root: Path) -> None:
        """A prefixed link into the current root for a rule that no longer ships is removed."""
        obsolete = home / ".claude" / "rules" / "develop-retired.md"
        obsolete.symlink_to(root / "rules" / "retired.md")
        result = sync(PLUGIN, root, home)
        assert result.removed == ["develop-retired.md"]
        assert not obsolete.is_symlink()

    def test_keeps_obsolete_foreign_link(self, home: Path, root: Path) -> None:
        """A prefixed link that points outside the plugin's cache is the user's and is kept."""
        foreign = home / ".claude" / "rules" / "develop-retired.md"
        foreign.symlink_to(home / "dotfiles" / "retired.md")
        result = sync(PLUGIN, root, home)
        assert result.removed == []
        assert foreign.is_symlink()

    def test_keeps_obsolete_real_file(self, home: Path, root: Path) -> None:
        """A prefixed real file is the user's notes and is kept untouched."""
        user_file = home / ".claude" / "rules" / "develop-notes.md"
        user_file.write_text("mine\n", encoding="utf-8")
        result = sync(PLUGIN, root, home)
        assert result.removed == []
        assert user_file.read_text(encoding="utf-8") == "mine\n"

    def test_never_touches_other_plugins_namespace(self, home: Path, root: Path) -> None:
        """Another plugin's prefixed link is outside this plugin's namespace and survives the sync."""
        other = home / ".claude" / "rules" / "foundry-quality-gates.md"
        other.symlink_to(
            home / ".claude" / "plugins" / "cache" / MARKETPLACE / "foundry" / "0.40.0" / "rules" / "quality-gates.md"
        )
        sync(PLUGIN, root, home)
        assert other.is_symlink()


# --- variant selection -------------------------------------------------------
#
# A plugin may ship the same rule twice: the full body, and a delta stating only its own
# additions. Delivering the delta is correct exactly when the plugin owning the shared body has
# already delivered it, which is why every case below is about *proving* that, not detecting a
# filename. Anything unproven must fall back to the full rule — redundant beats incomplete.


def _canonical_source(home: Path, owner: str = VARIANT.owner, version: str = "0.60.0") -> Path:
    """Install the plugin that owns the canonical shared body and return its rule file."""
    root = home / ".claude" / "plugins" / "cache" / MARKETPLACE / owner / version
    _make_plugin(root, name=owner, rules={VARIANT.full: "# shared body\n"})
    return root / "rules" / VARIANT.full


def _deliver_canonical(home: Path, target: Path) -> Path:
    """Link the canonical destination name at ``target``, as the owner's own setup would."""
    link = home / ".claude" / "rules" / VARIANT.canonical
    link.symlink_to(target)
    return link


def _plugin_rule_links(home: Path) -> list[str]:
    """Names of every delivered entry carrying this plugin's prefix."""
    return sorted(entry.name for entry in (home / ".claude" / "rules").iterdir() if entry.name.startswith("develop-"))


def _canonical_real_file(home: Path) -> None:
    """Write a user's own file under the canonical name — no provenance at all."""
    (home / ".claude" / "rules" / VARIANT.canonical).write_text("mine\n", encoding="utf-8")


def _canonical_unmanaged_link(home: Path) -> None:
    """Link the canonical name into a tree that ships no plugin manifest."""
    target = home / "dotfiles" / "rules" / VARIANT.full
    target.parent.mkdir(parents=True)
    target.write_text("# mine\n", encoding="utf-8")
    _deliver_canonical(home, target)


def _canonical_other_plugin_link(home: Path) -> None:
    """Link the canonical name at another plugin's rule of the same basename."""
    _deliver_canonical(home, _canonical_source(home, owner="oss", version="0.25.0"))


def _canonical_dangling_link(home: Path) -> None:
    """Link the canonical name into a version directory purged after an upgrade."""
    _deliver_canonical(home, _canonical_source(home, version="0.59.0"))
    shutil.rmtree(home / ".claude" / "plugins" / "cache" / MARKETPLACE / VARIANT.owner / "0.59.0")


def _canonical_outside_rules_dir(home: Path) -> None:
    """Link the canonical name at an owner file that is not the rule the delta abbreviates."""
    owner_root = _canonical_source(home).parent.parent
    stray = owner_root / "skills" / VARIANT.full
    stray.parent.mkdir(parents=True)
    stray.write_text("# not a rule\n", encoding="utf-8")
    _deliver_canonical(home, stray)


def _canonical_absent(home: Path) -> None:
    """Leave the canonical name undelivered, as in a standalone install."""


def _canonical_owner_provided(home: Path) -> None:
    """Deliver the canonical name from the owning plugin, as the owner's own setup would."""
    _deliver_canonical(home, _canonical_source(home))


class TestVariantSelection:
    """The full rule or its delta is delivered under one destination name, chosen by proven canonical provenance."""

    @pytest.mark.parametrize(
        ("plant", "delivered_variant"),
        [
            # A standalone install has no shared body to defer to, so it must ship every obligation.
            pytest.param(_canonical_absent, VARIANT.full, id="full-rule-when-no-canonical-is-present"),
            # With the shared body already delivered, only the plugin's own additions are installed.
            pytest.param(_canonical_owner_provided, VARIANT.delta, id="delta-when-the-canonical-is-owner-provided"),
        ],
    )
    def test_delivers_the_variant_matching_canonical_provenance(
        self, home: Path, plant: Callable[[Path], None], delivered_variant: str
    ) -> None:
        """The full rule is delivered without a canonical, the delta when the owner already delivered the shared body.

        Either way the plugin's single destination name is linked at the chosen variant.
        """
        root = _installed_root(home, rules=BOTH_VARIANTS)
        plant(home)
        result = sync(PLUGIN, root, home)
        assert result.linked == [_DEST_NAME]
        assert Path(read_link_target(_dest(home))) == root / "rules" / delivered_variant

    def test_delta_shares_the_full_rules_destination(self, home: Path) -> None:
        """Both forms share one destination, so no run can leave two copies of the rule loaded."""
        root = _installed_root(home, rules=BOTH_VARIANTS)
        _deliver_canonical(home, _canonical_source(home))
        sync(PLUGIN, root, home)
        assert _plugin_rule_links(home) == [_DEST_NAME]

    def test_canonical_is_delivered_accepts_an_owner_declared_manifest(self, home: Path) -> None:
        """Provenance comes from the target's own manifest, so a checkout-installed owner counts too."""
        _deliver_canonical(home, _canonical_source(home))
        assert sync_rules.canonical_is_delivered(home / ".claude" / "rules", VARIANT)

    @pytest.mark.parametrize(
        "plant",
        [
            pytest.param(_canonical_real_file, id="real-file"),
            pytest.param(_canonical_unmanaged_link, id="no-manifest"),
            pytest.param(_canonical_other_plugin_link, id="other-plugin"),
            pytest.param(_canonical_dangling_link, id="purged-version"),
            pytest.param(_canonical_outside_rules_dir, id="outside-rules-dir"),
        ],
    )
    def test_canonical_without_owner_provenance_keeps_full_rule(
        self, home: Path, plant: Callable[[Path], None]
    ) -> None:
        """The canonical filename alone proves nothing; each shape here leaves the rule set incomplete."""
        root = _installed_root(home, rules=BOTH_VARIANTS)
        plant(home)
        sync(PLUGIN, root, home)
        assert not sync_rules.canonical_is_delivered(home / ".claude" / "rules", VARIANT)
        assert Path(read_link_target(_dest(home))) == root / "rules" / VARIANT.full

    def test_installing_the_owner_later_switches_to_the_delta(self, home: Path) -> None:
        """Rerunning after the owner appears must swap forms, leaving exactly one link behind."""
        root = _installed_root(home, rules=BOTH_VARIANTS)
        sync(PLUGIN, root, home)
        _deliver_canonical(home, _canonical_source(home))
        result = sync(PLUGIN, root, home)
        assert result.linked == [_DEST_NAME]
        assert _plugin_rule_links(home) == [_DEST_NAME]
        assert Path(read_link_target(_dest(home))) == root / "rules" / VARIANT.delta

    def test_removing_the_owner_switches_back_to_the_full_rule(self, home: Path) -> None:
        """Uninstalling the owner must restore the complete rule rather than leave the delta serving alone."""
        root = _installed_root(home, rules=BOTH_VARIANTS)
        canonical = _deliver_canonical(home, _canonical_source(home))
        sync(PLUGIN, root, home)
        canonical.unlink()
        result = sync(PLUGIN, root, home)
        assert result.linked == [_DEST_NAME]
        assert _plugin_rule_links(home) == [_DEST_NAME]
        assert Path(read_link_target(_dest(home))) == root / "rules" / VARIANT.full

    def test_stray_per_variant_link_is_pruned(self, home: Path) -> None:
        """An earlier attempt that gave the delta its own destination must not keep serving it."""
        root = _installed_root(home, rules=BOTH_VARIANTS)
        stray = home / ".claude" / "rules" / dest_name(PLUGIN, VARIANT.delta)
        stray.symlink_to(root / "rules" / VARIANT.delta)
        result = sync(PLUGIN, root, home)
        assert result.removed == ["develop-quality-gates-delta.md"]
        assert _plugin_rule_links(home) == [_DEST_NAME]

    def test_delta_without_full_counterpart_aborts(self, home: Path) -> None:
        """A lone delta would be delivered as the whole rule whenever the canonical is absent."""
        root = _installed_root(home, rules={VARIANT.delta: "# delta gates\n"})
        with pytest.raises(SourceError, match="ships without its full counterpart"):
            sync(PLUGIN, root, home)
        assert list((home / ".claude" / "rules").iterdir()) == []


# --- source validation -------------------------------------------------------


def _delete_manifest(root: Path) -> None:
    """Remove the plugin manifest."""
    (root / ".claude-plugin" / "plugin.json").unlink()


def _write_invalid_manifest(root: Path) -> None:
    """Replace the plugin manifest with text that is not JSON."""
    (root / ".claude-plugin" / "plugin.json").write_text("{not json", encoding="utf-8")


def _write_mismatched_manifest(root: Path) -> None:
    """Replace the plugin manifest with one declaring another plugin's name."""
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "oss"}), encoding="utf-8")


def _remove_rules_dir(root: Path) -> None:
    """Delete the rules directory together with its only rule."""
    (root / "rules" / "quality-gates.md").unlink()
    (root / "rules").rmdir()


def _empty_rules_dir(root: Path) -> None:
    """Delete the only rule, leaving an empty rules directory."""
    (root / "rules" / "quality-gates.md").unlink()


def _blank_rule_file(root: Path) -> None:
    """Truncate the only rule to an empty file."""
    (root / "rules" / "quality-gates.md").write_text("", encoding="utf-8")


class TestSourceValidation:
    """A bad plugin source aborts with a named reason before any link is created or changed."""

    @pytest.mark.parametrize(
        ("corrupt", "message"),
        [
            pytest.param(_delete_manifest, "missing plugin manifest", id="missing-manifest"),
            pytest.param(_write_invalid_manifest, "unreadable plugin manifest", id="invalid-manifest"),
            pytest.param(_write_mismatched_manifest, "declares 'oss'", id="mismatched-manifest-name"),
            pytest.param(_remove_rules_dir, "rules directory missing", id="missing-rules-dir"),
            pytest.param(_empty_rules_dir, r"no \*\.md rules found", id="empty-rules-dir"),
            pytest.param(_blank_rule_file, "rule is empty", id="empty-rule-file"),
        ],
    )
    def test_invalid_plugin_source_aborts_before_mutation(
        self, home: Path, root: Path, corrupt: Callable[[Path], None], message: str
    ) -> None:
        """A plugin source with a bad manifest or rules directory aborts with a named reason and delivers nothing.

        The manifest may be missing, unreadable or declare another plugin; the rules directory may be missing or empty;
        a rule file may be empty. Validation runs before any link is created in the user's rules directory.
        """
        corrupt(root)
        with pytest.raises(SourceError, match=message):
            sync(PLUGIN, root, home)
        assert list((home / ".claude" / "rules").iterdir()) == []

    @pytest.mark.parametrize(
        "name",
        [
            pytest.param("../x", id="parent-traversal"),
            pytest.param("a/b", id="path-separator"),
            pytest.param("..", id="dot-dot"),
            pytest.param("Develop", id="uppercase"),
            pytest.param("-develop", id="leading-hyphen"),
            pytest.param("", id="empty"),
        ],
    )
    def test_unsafe_plugin_name_aborts_before_any_link(self, tmp_path: Path, home: Path, name: str) -> None:
        """A plugin name that could leave ``rules/`` aborts even when its manifest declares the same name.

        The destination is ``rules/<name>-<rule>``. A manifest declaring ``../x`` and a matching ``--plugin-name``
        passed the old equality check, so the link landed in ``~/.claude/`` itself; the name itself is now the gate.
        """
        unsafe = _make_plugin(tmp_path / "plugin", name=name)
        with pytest.raises(SourceError, match="invalid plugin name"):
            sync(name, unsafe, home)
        assert [p.name for p in (home / ".claude").iterdir()] == ["rules"]
        assert list((home / ".claude" / "rules").iterdir()) == []

    def test_symlinked_rules_dir_aborts(self, tmp_path: Path, home: Path, root: Path) -> None:
        """A ``rules`` directory that is a symlink is refused, since it could point anywhere on disk."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "quality-gates.md").write_text("# gates\n", encoding="utf-8")
        (root / "rules" / "quality-gates.md").unlink()
        (root / "rules").rmdir()
        (root / "rules").symlink_to(elsewhere)
        with pytest.raises(SourceError, match="not a real directory"):
            sync(PLUGIN, root, home)

    def test_symlinked_rule_escaping_root_aborts(self, tmp_path: Path, home: Path, root: Path) -> None:
        """A rule file that is a symlink out of the plugin root aborts and links nothing under its name."""
        outside = tmp_path / "outside.md"
        outside.write_text("# elsewhere\n", encoding="utf-8")
        (root / "rules" / "escape.md").symlink_to(outside)
        with pytest.raises(SourceError, match="rule is a symlink"):
            sync(PLUGIN, root, home)
        assert not _dest(home, "escape.md").exists()

    def test_validation_failure_leaves_existing_links_untouched(self, home: Path, root: Path) -> None:
        """A later sync that fails validation must not disturb links an earlier good sync created."""
        sync(PLUGIN, root, home)
        (root / "rules" / "broken.md").write_text("", encoding="utf-8")
        with pytest.raises(SourceError):
            sync(PLUGIN, root, home)
        assert Path(read_link_target(_dest(home))) == root / "rules" / "quality-gates.md"


class TestContainment:
    """The helper writes only inside ``~/.claude/rules``."""

    def test_writes_nothing_outside_claude_rules(self, home: Path, root: Path) -> None:
        """Even with ``approve=True`` nothing outside ``.claude/rules`` is created, changed or removed.

        A neighbouring ``~/.codex/config.toml`` stands in for any unrelated user file the sync must not touch.
        """
        codex = home / ".codex"
        codex.mkdir()
        (codex / "config.toml").write_text("keep\n", encoding="utf-8")
        before = sorted(p.relative_to(home) for p in home.rglob("*") if ".claude/rules" not in p.as_posix())
        sync(PLUGIN, root, home, approve=True)
        after = sorted(p.relative_to(home) for p in home.rglob("*") if ".claude/rules" not in p.as_posix())
        assert before == after
        assert (codex / "config.toml").read_text(encoding="utf-8") == "keep\n"


class TestWindowsLinkTargets:
    """Windows link-target spelling.

    These run everywhere. ``os.readlink`` only produces the extended-length spelling on Windows, and ``pathlib`` picks
    its flavour from the host, so the Windows semantics are exercised through ``PureWindowsPath``/``ntpath`` directly
    rather than by pretending to be win32 — monkeypatching ``sys.platform`` does not change how ``pathlib`` parses
    paths.
    """

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param(r"\\?\C:\p\rules\quality-gates.md", r"C:\p\rules\quality-gates.md", id="drive"),
            pytest.param(r"\\?\UNC\server\share\gates.md", r"\\server\share\gates.md", id="unc"),
            pytest.param(r"C:\p\rules\quality-gates.md", r"C:\p\rules\quality-gates.md", id="already-plain"),
            pytest.param(
                "/h/.claude/rules/quality-gates.md", "/h/.claude/rules/quality-gates.md", id="posix-untouched"
            ),
            pytest.param("../../dotfiles/quality-gates.md", "../../dotfiles/quality-gates.md", id="relative-untouched"),
        ],
    )
    def test_extended_length_prefix_is_stripped(self, raw: str, expected: str) -> None:
        """Windows spells absolute link targets in the ``\\\\?\\`` namespace; POSIX targets never do."""
        assert sync_rules.strip_extended_prefix(raw) == expected

    def test_extended_length_prefix_defeats_the_ownership_comparison(self) -> None:
        """The prefix is why ownership failed: same file, unequal path — so stripping is the fix."""
        raw = rf"\\?\{_WIN_ROOT}\rules\quality-gates.md"
        assert not PureWindowsPath(raw).is_relative_to(_WIN_ROOT)
        assert PureWindowsPath(sync_rules.strip_extended_prefix(raw)).is_relative_to(_WIN_ROOT)

    def test_stripping_does_not_adopt_a_foreign_target(self) -> None:
        """Normalising the spelling must not widen ownership — a dotfiles link stays foreign."""
        foreign = sync_rules.strip_extended_prefix(r"\\?\C:\h\dotfiles\rules\quality-gates.md")
        assert not PureWindowsPath(foreign).is_relative_to(_WIN_ROOT)

    def test_read_link_target_normalises_what_the_link_reports(self, tmp_path: Path) -> None:
        """The public reader is what every consumer uses, so the whole module sees one spelling."""
        source = tmp_path / "quality-gates.md"
        source.write_text("# gates\n", encoding="utf-8")
        link = tmp_path / _DEST_NAME
        link.symlink_to(source)
        assert read_link_target(link) == sync_rules.strip_extended_prefix(str(link.readlink()))
        assert Path(read_link_target(link)) == source


class TestCli:
    """The ``main`` entry point: report on stdout, exit 0 on success and 1 on any failure."""

    @staticmethod
    def _argv(home: Path, root: Path | str, *extra: str) -> list[str]:
        """Build the CLI argv for ``PLUGIN`` rooted at ``root`` with ``home`` as the user home."""
        return ["--plugin-name", PLUGIN, "--plugin-root", str(root), "--home", str(home), *extra]

    def test_reports_and_exits_zero(self, home: Path, root: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A successful sync prints one ``linked:`` line per rule and exits 0."""
        assert main(self._argv(home, root)) == 0
        assert f"linked: {_DEST_NAME}" in capsys.readouterr().out

    def test_exits_one_on_source_error(self, tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A plugin root that is not a directory exits 1 with the reason on stderr."""
        assert main(self._argv(home, tmp_path / "nope")) == 1
        assert "not a directory" in capsys.readouterr().err

    def test_dry_run_changes_nothing(self, home: Path, root: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """``--dry-run`` reports what would be linked but creates no link."""
        assert main(self._argv(home, root, "--dry-run")) == 0
        assert f"linked: {_DEST_NAME}" in capsys.readouterr().out
        assert list((home / ".claude" / "rules").iterdir()) == []

    @mock.patch.object(sync_rules, "_replace_link", autospec=True, side_effect=OSError("symlinks unsupported"))
    def test_reports_failure_exit_code(
        self, replace_link: mock.MagicMock, home: Path, root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A platform that refuses to create the symlink yields a ``FAILED:`` line and exit code 1."""
        assert main(self._argv(home, root)) == 1
        replace_link.assert_called_once()
        assert f"FAILED: {_DEST_NAME}: symlinks unsupported" in capsys.readouterr().out
