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

import pytest
import sync_rules
from sync_rules import VARIANTS, SourceError, cache_lineage, dest_name, main, owns, read_link_target, sync

MARKETPLACE = "borda-ai-rig"
PLUGIN = "develop"
VARIANT = VARIANTS[0]
BOTH_VARIANTS = {VARIANT.full: "# full gates\n", VARIANT.delta: "# delta gates\n"}


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
    return _make_plugin(root, name=plugin, rules=rules)


def _dest(home: Path, source_name: str = "quality-gates.md", plugin: str = PLUGIN) -> Path:
    """Return the namespaced destination path for a source rule.

    Examples:
        >>> _dest(getfixture("tmp_path")).name
        'develop-quality-gates.md'
    """
    return home / ".claude" / "rules" / dest_name(plugin, source_name)


# --- naming and lineage ------------------------------------------------------


def test_destination_is_plugin_prefixed(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    root = _installed_root(home)

    sync(PLUGIN, root, home)

    assert _dest(home).is_symlink()
    assert _dest(home).name == "develop-quality-gates.md"


def test_sibling_plugins_do_not_collide(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    develop = _installed_root(home)
    oss = _make_plugin(home / ".claude" / "plugins" / "cache" / MARKETPLACE / "oss" / "0.25.0", name="oss")

    sync(PLUGIN, develop, home)
    sync("oss", oss, home)

    assert _dest(home).is_symlink()
    assert _dest(home, plugin="oss").is_symlink()
    assert Path(read_link_target(_dest(home))).parent.parent == develop
    assert Path(read_link_target(_dest(home, plugin="oss"))).parent.parent == oss


def test_cache_lineage_only_for_installed_roots(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    installed = _installed_root(home)

    assert cache_lineage(installed, home) == installed.parent
    assert cache_lineage(tmp_path / "checkout" / "plugins" / "cc_develop", home) is None


# --- ownership ---------------------------------------------------------------


def test_owns_current_root(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    root = _installed_root(home)
    dest = _dest(home)

    assert owns(dest, str(root / "rules" / "quality-gates.md"), root, cache_lineage(root, home))


def test_owns_same_lineage_stale_version(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    root = _installed_root(home, version="0.19.0")
    stale = home / ".claude" / "plugins" / "cache" / MARKETPLACE / PLUGIN / "0.18.5" / "rules" / "quality-gates.md"

    assert owns(_dest(home), str(stale), root, cache_lineage(root, home))


@pytest.mark.parametrize(
    "target_rel",
    [
        # another marketplace, same plugin name — different lineage
        ".claude/plugins/cache/other-marketplace/develop/0.18.5/rules/quality-gates.md",
        # same marketplace, another plugin — different lineage
        ".claude/plugins/cache/borda-ai-rig/oss/0.24.5/rules/quality-gates.md",
        # arbitrary source checkout
        "src/AI-Rig/plugins/cc_develop/rules/quality-gates.md",
        # dotfiles path — the exact shape the reverted attempt deleted
        "dotfiles/plugins/cc_develop/rules/quality-gates.md",
        # a similarly named plugin must not enter the Develop lineage through a shared name prefix
        ".claude/plugins/cache/borda-ai-rig/develop-extra/0.1.0/rules/quality-gates.md",
    ],
)
def test_does_not_own_foreign_targets(tmp_path: Path, target_rel: str) -> None:
    home = _make_home(tmp_path)
    root = _installed_root(home)

    assert not owns(_dest(home), str(home / target_rel), root, cache_lineage(root, home))


# --- install behaviour -------------------------------------------------------


def test_creates_link_when_absent(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    root = _installed_root(home)

    result = sync(PLUGIN, root, home)

    assert result.linked == ["develop-quality-gates.md"]
    assert Path(read_link_target(_dest(home))) == root / "rules" / "quality-gates.md"


def test_creates_rules_dir_when_missing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    root = _installed_root(home)

    sync(PLUGIN, root, home)

    assert _dest(home).is_symlink()


def test_relative_plugin_root_still_links_absolutely(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A relative ``--plugin-root`` must not become a dangling relative symlink target."""
    home = _make_home(tmp_path)
    root = _installed_root(home)
    monkeypatch.chdir(root.parent)

    sync(PLUGIN, Path(root.name), home)

    assert Path(read_link_target(_dest(home))).is_absolute()
    assert _dest(home).resolve().is_file()


def test_idempotent_rerun_reports_unchanged(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    root = _installed_root(home)

    sync(PLUGIN, root, home)
    second = sync(PLUGIN, root, home)

    assert second.linked == []
    assert second.unchanged == ["develop-quality-gates.md"]


def test_refreshes_same_lineage_stale_link(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    old = _installed_root(home, version="0.18.5")
    new = _installed_root(home, version="0.19.0")
    _dest(home).symlink_to(old / "rules" / "quality-gates.md")

    result = sync(PLUGIN, new, home)

    assert result.linked == ["develop-quality-gates.md"]
    assert Path(read_link_target(_dest(home))) == new / "rules" / "quality-gates.md"


def test_refreshes_broken_owned_link(tmp_path: Path) -> None:
    """A dangling link into the current root is owned, so it is repaired."""
    home = _make_home(tmp_path)
    root = _installed_root(home)
    _dest(home).symlink_to(root / "rules" / "renamed-away.md")

    result = sync(PLUGIN, root, home)

    assert result.linked == ["develop-quality-gates.md"]
    assert Path(read_link_target(_dest(home))) == root / "rules" / "quality-gates.md"


# --- conflicts ---------------------------------------------------------------


def test_real_file_is_preserved_as_conflict(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    root = _installed_root(home)
    _dest(home).write_text("user content\n", encoding="utf-8")

    result = sync(PLUGIN, root, home)

    assert result.conflicts == ["develop-quality-gates.md  (real file)"]
    assert _dest(home).read_text(encoding="utf-8") == "user content\n"
    assert not _dest(home).is_symlink()


def test_foreign_link_is_preserved_as_conflict(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    root = _installed_root(home)
    foreign = home / "dotfiles" / "rules" / "quality-gates.md"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("mine\n", encoding="utf-8")
    _dest(home).symlink_to(foreign)

    result = sync(PLUGIN, root, home)

    assert result.conflicts == [f"develop-quality-gates.md → {foreign}"]
    assert Path(read_link_target(_dest(home))) == foreign
    assert foreign.is_file()


def test_relative_foreign_target_is_preserved(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    root = _installed_root(home)
    _dest(home).symlink_to(Path("../../dotfiles/quality-gates.md"))

    result = sync(PLUGIN, root, home)

    assert result.conflicts
    assert Path(read_link_target(_dest(home))) == Path("../../dotfiles/quality-gates.md")


def test_relative_owned_target_is_recognised(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    root = _installed_root(home)
    relative = Path("..") / ".." / root.relative_to(home) / "rules" / "quality-gates.md"
    _dest(home).symlink_to(relative)

    result = sync(PLUGIN, root, home)

    assert result.conflicts == []
    assert result.unchanged == ["develop-quality-gates.md"]
    assert _dest(home).resolve() == root / "rules" / "quality-gates.md"


def test_broken_foreign_link_is_preserved(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    root = _installed_root(home)
    dangling = home / "src" / "AI-Rig" / "plugins" / "cc_develop" / "rules" / "quality-gates.md"
    _dest(home).symlink_to(dangling)

    result = sync(PLUGIN, root, home)

    assert result.conflicts == [f"develop-quality-gates.md → {dangling}"]
    assert Path(read_link_target(_dest(home))) == dangling


def test_approve_replaces_conflicts(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    root = _installed_root(home)
    _dest(home).write_text("user content\n", encoding="utf-8")

    result = sync(PLUGIN, root, home, approve=True)

    assert result.replaced == ["develop-quality-gates.md"]
    assert Path(read_link_target(_dest(home))) == root / "rules" / "quality-gates.md"


# --- obsolete pruning --------------------------------------------------------


def test_removes_obsolete_owned_link(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    root = _installed_root(home)
    obsolete = home / ".claude" / "rules" / "develop-retired.md"
    obsolete.symlink_to(root / "rules" / "retired.md")

    result = sync(PLUGIN, root, home)

    assert result.removed == ["develop-retired.md"]
    assert not obsolete.is_symlink()


def test_keeps_obsolete_foreign_link(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    root = _installed_root(home)
    foreign = home / ".claude" / "rules" / "develop-retired.md"
    foreign.symlink_to(home / "dotfiles" / "retired.md")

    result = sync(PLUGIN, root, home)

    assert result.removed == []
    assert foreign.is_symlink()


def test_keeps_obsolete_real_file(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    root = _installed_root(home)
    user_file = home / ".claude" / "rules" / "develop-notes.md"
    user_file.write_text("mine\n", encoding="utf-8")

    result = sync(PLUGIN, root, home)

    assert result.removed == []
    assert user_file.read_text(encoding="utf-8") == "mine\n"


def test_never_touches_other_plugins_namespace(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    root = _installed_root(home)
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
    root = _canonical_source(home).parent.parent
    stray = root / "skills" / VARIANT.full
    stray.parent.mkdir(parents=True)
    stray.write_text("# not a rule\n", encoding="utf-8")
    _deliver_canonical(home, stray)


def _canonical_absent(home: Path) -> None:
    """Leave the canonical name undelivered, as in a standalone install."""


def _canonical_owner_provided(home: Path) -> None:
    """Deliver the canonical name from the owning plugin, as the owner's own setup would."""
    _deliver_canonical(home, _canonical_source(home))


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
    tmp_path: Path, plant: Callable[[Path], None], delivered_variant: str
) -> None:
    """The full rule is delivered without a canonical, the delta when the owner already delivered the shared body.

    Either way the plugin's single destination name is linked at the chosen variant.
    """
    home = _make_home(tmp_path)
    root = _installed_root(home, rules=BOTH_VARIANTS)
    plant(home)

    result = sync(PLUGIN, root, home)

    assert result.linked == ["develop-quality-gates.md"]
    assert Path(read_link_target(_dest(home))) == root / "rules" / delivered_variant


def test_the_delta_is_never_delivered_under_a_name_of_its_own(tmp_path: Path) -> None:
    """Both forms share one destination, so no run can leave two copies of the rule loaded."""
    home = _make_home(tmp_path)
    root = _installed_root(home, rules=BOTH_VARIANTS)
    _deliver_canonical(home, _canonical_source(home))

    sync(PLUGIN, root, home)

    assert _plugin_rule_links(home) == ["develop-quality-gates.md"]


def test_canonical_is_delivered_accepts_an_owner_declared_manifest(tmp_path: Path) -> None:
    """Provenance comes from the target's own manifest, so a checkout-installed owner counts too."""
    home = _make_home(tmp_path)
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
def test_a_canonical_without_owner_provenance_keeps_the_full_rule(
    tmp_path: Path, plant: Callable[[Path], None]
) -> None:
    """The canonical filename alone proves nothing; each shape here leaves the rule set incomplete."""
    home = _make_home(tmp_path)
    root = _installed_root(home, rules=BOTH_VARIANTS)
    plant(home)

    sync(PLUGIN, root, home)

    assert not sync_rules.canonical_is_delivered(home / ".claude" / "rules", VARIANT)
    assert Path(read_link_target(_dest(home))) == root / "rules" / VARIANT.full


def test_installing_the_owner_later_switches_to_the_delta(tmp_path: Path) -> None:
    """Rerunning after the owner appears must swap forms, leaving exactly one link behind."""
    home = _make_home(tmp_path)
    root = _installed_root(home, rules=BOTH_VARIANTS)
    sync(PLUGIN, root, home)
    _deliver_canonical(home, _canonical_source(home))

    result = sync(PLUGIN, root, home)

    assert result.linked == ["develop-quality-gates.md"]
    assert _plugin_rule_links(home) == ["develop-quality-gates.md"]
    assert Path(read_link_target(_dest(home))) == root / "rules" / VARIANT.delta


def test_removing_the_owner_switches_back_to_the_full_rule(tmp_path: Path) -> None:
    """Uninstalling the owner must restore the complete rule rather than leave the delta serving alone."""
    home = _make_home(tmp_path)
    root = _installed_root(home, rules=BOTH_VARIANTS)
    canonical = _deliver_canonical(home, _canonical_source(home))
    sync(PLUGIN, root, home)
    canonical.unlink()

    result = sync(PLUGIN, root, home)

    assert result.linked == ["develop-quality-gates.md"]
    assert _plugin_rule_links(home) == ["develop-quality-gates.md"]
    assert Path(read_link_target(_dest(home))) == root / "rules" / VARIANT.full


def test_a_stray_per_variant_link_is_pruned(tmp_path: Path) -> None:
    """An earlier attempt that gave the delta its own destination must not keep serving it."""
    home = _make_home(tmp_path)
    root = _installed_root(home, rules=BOTH_VARIANTS)
    stray = home / ".claude" / "rules" / dest_name(PLUGIN, VARIANT.delta)
    stray.symlink_to(root / "rules" / VARIANT.delta)

    result = sync(PLUGIN, root, home)

    assert result.removed == ["develop-quality-gates-delta.md"]
    assert _plugin_rule_links(home) == ["develop-quality-gates.md"]


def test_a_delta_without_its_full_counterpart_aborts(tmp_path: Path) -> None:
    """A lone delta would be delivered as the whole rule whenever the canonical is absent."""
    home = _make_home(tmp_path)
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
    tmp_path: Path, corrupt: Callable[[Path], None], message: str
) -> None:
    """A plugin source with a bad manifest or rules directory aborts with a named reason and delivers nothing.

    The manifest may be missing, unreadable or declare another plugin; the rules directory may be missing or empty; a
    rule file may be empty. Validation runs before any link is created in the user's rules directory.
    """
    home = _make_home(tmp_path)
    root = _installed_root(home)
    corrupt(root)

    with pytest.raises(SourceError, match=message):
        sync(PLUGIN, root, home)
    assert list((home / ".claude" / "rules").iterdir()) == []


def test_symlinked_rules_dir_aborts(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    root = _installed_root(home)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "quality-gates.md").write_text("# gates\n", encoding="utf-8")
    (root / "rules" / "quality-gates.md").unlink()
    (root / "rules").rmdir()
    (root / "rules").symlink_to(elsewhere)

    with pytest.raises(SourceError, match="not a real directory"):
        sync(PLUGIN, root, home)


def test_symlinked_rule_escaping_root_aborts(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    root = _installed_root(home)
    outside = tmp_path / "outside.md"
    outside.write_text("# elsewhere\n", encoding="utf-8")
    (root / "rules" / "escape.md").symlink_to(outside)

    with pytest.raises(SourceError, match="rule is a symlink"):
        sync(PLUGIN, root, home)
    assert not _dest(home, "escape.md").exists()


def test_validation_failure_leaves_existing_links_untouched(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    root = _installed_root(home)
    sync(PLUGIN, root, home)
    (root / "rules" / "broken.md").write_text("", encoding="utf-8")

    with pytest.raises(SourceError):
        sync(PLUGIN, root, home)
    assert Path(read_link_target(_dest(home))) == root / "rules" / "quality-gates.md"


# --- containment -------------------------------------------------------------


def test_writes_nothing_outside_claude_rules(tmp_path: Path) -> None:
    home = _make_home(tmp_path)
    codex = home / ".codex"
    codex.mkdir()
    (codex / "config.toml").write_text("keep\n", encoding="utf-8")
    root = _installed_root(home)

    before = sorted(p.relative_to(home) for p in home.rglob("*") if ".claude/rules" not in p.as_posix())
    sync(PLUGIN, root, home, approve=True)
    after = sorted(p.relative_to(home) for p in home.rglob("*") if ".claude/rules" not in p.as_posix())

    assert before == after
    assert (codex / "config.toml").read_text(encoding="utf-8") == "keep\n"


# --- Windows link-target spelling --------------------------------------------
#
# These run everywhere. ``os.readlink`` only produces the extended-length spelling on
# Windows, and ``pathlib`` picks its flavour from the host, so the Windows semantics are
# exercised through ``PureWindowsPath``/``ntpath`` directly rather than by pretending to
# be win32 — monkeypatching ``sys.platform`` does not change how ``pathlib`` parses paths.


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(r"\\?\C:\p\rules\quality-gates.md", r"C:\p\rules\quality-gates.md", id="drive"),
        pytest.param(r"\\?\UNC\server\share\gates.md", r"\\server\share\gates.md", id="unc"),
        pytest.param(r"C:\p\rules\quality-gates.md", r"C:\p\rules\quality-gates.md", id="already-plain"),
        pytest.param("/h/.claude/rules/quality-gates.md", "/h/.claude/rules/quality-gates.md", id="posix-untouched"),
        pytest.param("../../dotfiles/quality-gates.md", "../../dotfiles/quality-gates.md", id="relative-untouched"),
    ],
)
def test_extended_length_prefix_is_stripped(raw: str, expected: str) -> None:
    """Windows spells absolute link targets in the ``\\\\?\\`` namespace; POSIX targets never do."""
    assert sync_rules.strip_extended_prefix(raw) == expected


def test_extended_length_prefix_defeats_the_ownership_comparison() -> None:
    """The prefix is why ownership failed: same file, unequal path — so stripping is the fix."""
    root = PureWindowsPath(r"C:\h\.claude\plugins\cache\borda-ai-rig\develop\0.19.0")
    raw = rf"\\?\{root}\rules\quality-gates.md"

    assert not PureWindowsPath(raw).is_relative_to(root)
    assert PureWindowsPath(sync_rules.strip_extended_prefix(raw)).is_relative_to(root)


def test_stripping_does_not_adopt_a_foreign_simulated_windows_target() -> None:
    """Normalising the spelling must not widen ownership — a dotfiles link stays foreign."""
    root = PureWindowsPath(r"C:\h\.claude\plugins\cache\borda-ai-rig\develop\0.19.0")
    foreign = sync_rules.strip_extended_prefix(r"\\?\C:\h\dotfiles\rules\quality-gates.md")

    assert not PureWindowsPath(foreign).is_relative_to(root)


def test_read_link_target_normalises_what_the_link_reports(tmp_path: Path) -> None:
    """The public reader is what every consumer uses, so the whole module sees one spelling."""
    source = tmp_path / "quality-gates.md"
    source.write_text("# gates\n", encoding="utf-8")
    link = tmp_path / "develop-quality-gates.md"
    link.symlink_to(source)

    assert read_link_target(link) == sync_rules.strip_extended_prefix(str(link.readlink()))
    assert Path(read_link_target(link)) == source


# --- CLI ---------------------------------------------------------------------


def test_main_reports_and_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = _make_home(tmp_path)
    root = _installed_root(home)

    code = main(["--plugin-name", PLUGIN, "--plugin-root", str(root), "--home", str(home)])

    assert code == 0
    assert "linked: develop-quality-gates.md" in capsys.readouterr().out


def test_main_exits_one_on_source_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = _make_home(tmp_path)

    code = main(["--plugin-name", PLUGIN, "--plugin-root", str(tmp_path / "nope"), "--home", str(home)])

    assert code == 1
    assert "not a directory" in capsys.readouterr().err


def test_main_dry_run_changes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = _make_home(tmp_path)
    root = _installed_root(home)

    code = main(["--plugin-name", PLUGIN, "--plugin-root", str(root), "--home", str(home), "--dry-run"])

    assert code == 0
    assert "linked: develop-quality-gates.md" in capsys.readouterr().out
    assert list((home / ".claude" / "rules").iterdir()) == []


def test_main_reports_failure_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = _make_home(tmp_path)
    root = _installed_root(home)

    def _boom(link: sync_rules.RuleLink) -> None:
        """Simulate a platform that refuses to create the destination symlink."""
        raise OSError("symlinks unsupported")

    monkeypatch.setattr(sync_rules, "_replace_link", _boom)
    code = main(["--plugin-name", PLUGIN, "--plugin-root", str(root), "--home", str(home)])

    assert code == 1
    assert "FAILED: develop-quality-gates.md: symlinks unsupported" in capsys.readouterr().out
