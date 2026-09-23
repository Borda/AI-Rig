#!/usr/bin/env python
"""sync_rules.py — install a plugin's ``rules/*.md`` into ``~/.claude/rules/``.

Claude Code loads user-level rules from the flat ``~/.claude/rules/`` namespace
(verified against Claude Code 2.1.220: filenames are inert — only the ``paths:``
frontmatter field scopes a rule). Several plugins ship a source file with the
same basename (``quality-gates.md``), so source basenames cannot be installed
directly. Every rule is installed as ``<plugin>-<source-name>.md`` instead.

Design constraints, in order of importance:

1. **Validate before mutating.** Every source check completes before the first
   ``symlink``/``unlink``. A malformed plugin tree never leaves ``~/.claude/``
   half-updated.
2. **Conservative ownership.** A destination is adopted only when its link
   target provably belongs to this plugin: under the current plugin root, or
   under the same ``<home>/.claude/plugins/cache/<marketplace>/<plugin>/``
   lineage as the current installed root. Path *substrings* are never used —
   a foreign marketplace, an arbitrary source checkout, or a dotfiles path is
   preserved as a conflict, never silently replaced or deleted.
3. **Symlinks only.** Rules must track the installed plugin version; a copy
   silently serves stale content after an upgrade. When the platform refuses
   to create a symlink the entry is reported as a failure, not copied.

Nothing outside ``<home>/.claude/rules/`` is ever created, replaced, or removed.
In particular this tool never writes under ``~/.codex/``.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/sync_rules.py" --plugin-name <name> --plugin-root <path>
        [--home <path>] [--approve] [--dry-run]

Exit codes:
    0   success — links are in place (unresolved conflicts are reported, not fatal)
    1   irrecoverable error (invalid plugin source, write failure)
    2   argument error
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

_MANIFEST_REL = Path(".claude-plugin") / "plugin.json"


class SourceError(Exception):
    """Raised when the plugin source tree fails validation.

    Every instance aborts the run before any filesystem mutation.
    """


@dataclass(frozen=True)
class RuleLink:
    """One ``source rule`` → ``destination link`` pairing.

    Attributes:
        source: Absolute path of the rule file inside the plugin tree.
        dest: Absolute path of the namespaced link under ``~/.claude/rules/``.
    """

    source: Path
    dest: Path


@dataclass
class SyncResult:
    """Outcome of a :func:`sync` run, one entry per affected destination.

    Attributes:
        linked: Names newly created or refreshed to the current plugin root.
        unchanged: Names already pointing at the current source.
        removed: Obsolete owned links deleted because the source is gone.
        conflicts: ``"<name> → <state>"`` descriptors left untouched.
        replaced: Conflicting names overwritten because ``--approve`` was given.
        failed: ``"<name>: <reason>"`` for destinations that could not be written.
    """

    linked: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def dest_name(plugin_name: str, source_name: str) -> str:
    """Namespaced destination filename for a source rule.

    Args:
        plugin_name: Owning plugin's manifest name (e.g. ``"develop"``).
        source_name: Source basename (e.g. ``"quality-gates.md"``).

    Returns:
        ``"<plugin>-<source-name>"`` — collision-free in the flat namespace.

    Examples:
        >>> dest_name("develop", "quality-gates.md")
        'develop-quality-gates.md'
        >>> dest_name("foundry", "foundry-config.md")
        'foundry-foundry-config.md'
    """
    return f"{plugin_name}-{source_name}"


def _manifest_name(plugin_root: Path) -> str:
    """Read the plugin name declared by ``<plugin_root>/.claude-plugin/plugin.json``.

    Args:
        plugin_root: Plugin install root.

    Returns:
        The manifest's ``name`` value.

    Raises:
        SourceError: When the manifest is missing, unreadable, not JSON, or
            declares no string ``name``.
    """
    manifest = plugin_root / _MANIFEST_REL
    if not manifest.is_file():
        raise SourceError(f"missing plugin manifest: {manifest}")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SourceError(f"unreadable plugin manifest {manifest}: {exc}") from exc
    name = data.get("name") if isinstance(data, dict) else None
    if not isinstance(name, str) or not name:
        raise SourceError(f"plugin manifest {manifest} declares no 'name'")
    return name


def _check_rule_file(rule: Path, rules_dir: Path) -> None:
    """Assert one candidate rule file is safe to link.

    Args:
        rule: Candidate ``*.md`` path.
        rules_dir: The plugin's ``rules/`` directory.

    Raises:
        SourceError: When the entry is a symlink, is not a regular file, is
            empty, or escapes ``rules_dir``.
    """
    if rule.is_symlink():
        raise SourceError(f"rule is a symlink, refusing to link it: {rule}")
    if not rule.is_file():
        raise SourceError(f"rule is not a regular file: {rule}")
    if rule.stat().st_size == 0:
        raise SourceError(f"rule is empty: {rule}")
    if not Path(os.path.normpath(str(rule))).is_relative_to(rules_dir):
        raise SourceError(f"rule escapes the plugin rules directory: {rule}")


def validate_source(plugin_root: Path, plugin_name: str) -> list[Path]:
    """Run every source-tree check and return the rule files to install.

    Completes fully before any caller mutates ``~/.claude/``.

    Args:
        plugin_root: Plugin install root.
        plugin_name: Name the caller claims this root provides.

    Returns:
        Sorted list of validated rule paths.

    Raises:
        SourceError: On any validation failure.
    """
    if not plugin_root.is_dir():
        raise SourceError(f"plugin root is not a directory: {plugin_root}")
    declared = _manifest_name(plugin_root)
    if declared != plugin_name:
        raise SourceError(f"plugin manifest declares {declared!r}, expected {plugin_name!r}")

    rules_dir = Path(os.path.normpath(str(plugin_root / "rules")))
    if rules_dir.is_symlink() or not rules_dir.is_dir():
        raise SourceError(f"plugin rules directory missing or not a real directory: {rules_dir}")
    if not rules_dir.is_relative_to(Path(os.path.normpath(str(plugin_root)))):
        raise SourceError(f"plugin rules directory escapes the plugin root: {rules_dir}")

    rules = sorted(p for p in rules_dir.iterdir() if p.suffix == ".md" and not p.is_dir())
    if not rules:
        raise SourceError(f"no *.md rules found in {rules_dir}")
    for rule in rules:
        _check_rule_file(rule, rules_dir)
    return rules


def cache_lineage(plugin_root: Path, home: Path) -> Path | None:
    """Return the ``cache/<marketplace>/<plugin>/`` prefix of an installed root.

    A stale link from a previous version of *this* plugin, installed from *this*
    marketplace, sits under the same two-segment prefix. Anything else — another
    marketplace, another plugin, a source checkout — has no lineage in common and
    is never adopted.

    Args:
        plugin_root: Plugin install root.
        home: User home directory.

    Returns:
        The lineage directory, or ``None`` when ``plugin_root`` is not an
        installed cache root (e.g. a local source checkout).

    Examples:
        >>> home = Path("/h")
        >>> root = home / ".claude/plugins/cache/mkt/develop/0.19.0"
        >>> cache_lineage(root, home).as_posix()
        '/h/.claude/plugins/cache/mkt/develop'
        >>> cache_lineage(Path("/src/plugins/cc_develop"), home) is None
        True
    """
    cache = Path(os.path.normpath(str(home / ".claude" / "plugins" / "cache")))
    root = Path(os.path.normpath(str(plugin_root)))
    if not root.is_relative_to(cache):
        return None
    parts = root.relative_to(cache).parts
    if len(parts) < 3:
        return None
    return cache / parts[0] / parts[1]


def _base_variants(base: Path) -> set[str]:
    """Normalised and symlink-resolved spellings of an ownership base path.

    Both are compared because the caller may pass a root reached through a
    symlinked prefix (``/tmp`` → ``/private/tmp`` on macOS) while previously
    written links recorded the other spelling.

    Args:
        base: Directory to expand.

    Returns:
        The distinct absolute path strings identifying ``base``.
    """
    return {os.path.normpath(str(base)), os.path.realpath(str(base))}


def resolve_target(dest: Path, target: str) -> Path:
    """Absolutise a raw ``readlink`` target without following symlinks.

    ``realpath`` is deliberately avoided: resolving a foreign path could walk it
    into a directory this plugin owns and turn a user's link into an adoption.
    Relative targets are anchored at the link's own directory, matching how the
    kernel reads them.

    Args:
        dest: The symlink whose target this is.
        target: Raw ``readlink`` output.

    Returns:
        Normalised absolute path of the target.

    Examples:
        >>> resolve_target(Path("/h/.claude/rules/x.md"), "../../elsewhere/x.md").as_posix()
        '/h/elsewhere/x.md'
        >>> resolve_target(Path("/h/.claude/rules/x.md"), "/abs/x.md").as_posix()
        '/abs/x.md'
    """
    if os.path.isabs(target):
        return Path(os.path.normpath(target))
    return Path(os.path.normpath(str(dest.parent / target)))


def owns(dest: Path, target: str, plugin_root: Path, lineage: Path | None) -> bool:
    """Check whether this plugin may replace or delete a destination.

    Args:
        dest: Existing symlink under ``~/.claude/rules/``.
        target: Raw ``readlink`` output for ``dest``.
        plugin_root: Current plugin install root.
        lineage: Result of :func:`cache_lineage`, or ``None``.

    Returns:
        True iff the target lies under the current plugin root or the same
        installed-cache lineage. Broken targets are judged on path alone, so a
        dangling link this plugin created is still owned (and therefore
        cleanable), while a dangling foreign link stays untouched.

    Examples:
        >>> root = Path("/h/.claude/plugins/cache/mkt/develop/0.19.0")
        >>> dest = Path("/h/.claude/rules/develop-quality-gates.md")
        >>> owns(dest, str(root / "rules/quality-gates.md"), root, None)
        True
        >>> owns(dest, "/h/dotfiles/plugins/cc_develop/rules/quality-gates.md", root, None)
        False
    """
    resolved = resolve_target(dest, target)
    bases = _base_variants(plugin_root)
    if lineage is not None:
        bases |= _base_variants(lineage)
    return any(resolved.is_relative_to(base) for base in bases)


def strip_extended_prefix(target: str) -> str:
    r"""Drop Windows' extended-length (``\\?\``) prefix from a raw link target.

    ``os.readlink`` on Windows returns the reparse point's substitute name, which for an
    absolute target is always spelled in the extended-length namespace: ``C:\p\rules`` comes
    back as ``\\?\C:\p\rules``. That spelling denotes the same file but compares unequal to
    every path this module derives from ``plugin_root``, so :func:`owns` rejected links this
    plugin had just written — every rerun reported its own links as foreign conflicts,
    refreshed nothing, and pruned nothing.

    A POSIX target can never begin with this prefix, so the transform is inert off Windows
    and needs no platform branch.

    Args:
        target: Raw ``readlink`` output.

    Returns:
        The same target spelled without the extended-length prefix.

    Examples:
        >>> strip_extended_prefix(r"\\?\C:\plugins\rules\gates.md")
        'C:\\plugins\\rules\\gates.md'
        >>> strip_extended_prefix(r"\\?\UNC\server\share\gates.md")
        '\\\\server\\share\\gates.md'
        >>> strip_extended_prefix("/h/.claude/rules/gates.md")
        '/h/.claude/rules/gates.md'
    """
    if target.startswith("\\\\?\\UNC\\"):
        return "\\\\" + target[len("\\\\?\\UNC\\") :]
    if target.startswith("\\\\?\\"):
        return target[len("\\\\?\\") :]
    return target


def read_link_target(path: Path) -> str | None:
    """Link target of ``path`` in its plain spelling, or ``None`` when it is not a symlink.

    Every consumer — ownership proof, conflict descriptor, idempotence check — must see the
    same spelling the rest of this module derives from ``plugin_root``, so the raw value is
    normalised here rather than at each call site.

    Args:
        path: Filesystem path to inspect.

    Returns:
        ``readlink`` output with any extended-length prefix removed, or ``None``.

    Examples:
        >>> read_link_target(Path("/nonexistent/path")) is None
        True
    """
    if not path.is_symlink():
        return None
    try:
        return strip_extended_prefix(os.readlink(path))
    except OSError:
        return None


def _describe(dest: Path, target: str | None) -> str:
    """One-line conflict descriptor for a destination left untouched.

    Args:
        dest: Destination path.
        target: ``readlink`` output, or ``None`` for a real file or directory.

    Returns:
        ``"<name> → <target>"`` or ``"<name>  (real file)"``.

    Examples:
        >>> _describe(Path("/h/.claude/rules/develop-x.md"), "/elsewhere/x.md")
        'develop-x.md → /elsewhere/x.md'
        >>> _describe(Path("/h/.claude/rules/develop-x.md"), None)
        'develop-x.md  (real file)'
    """
    if target is None:
        return f"{dest.name}  (real file)"
    return f"{dest.name} → {target}"


def _replace_link(link: RuleLink) -> None:
    """Point ``link.dest`` at ``link.source`` via an atomic rename.

    A fresh symlink is created at a sibling temp path and swapped into place with
    ``os.replace``, atomic on POSIX and Windows. This closes the TOCTOU window between the
    ownership check in :func:`_install_one` and the write: the old
    ``unlink()``-then-``symlink_to()`` sequence left ``link.dest`` briefly absent between the
    two calls; the temp-and-rename swap never leaves it in that state.

    Args:
        link: Pairing to materialise.

    Raises:
        OSError: When the temporary symlink cannot be created or the atomic replace fails.
            Copies are never attempted — a copied rule silently serves stale content after a
            plugin upgrade.
    """
    tmp = link.dest.with_name(f".{link.dest.name}.{os.getpid()}.tmp")
    try:
        tmp.symlink_to(link.source)
        os.replace(tmp, link.dest)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _install_one(
    link: RuleLink, plugin_root: Path, lineage: Path | None, approve: bool, dry_run: bool, result: SyncResult
) -> None:
    """Create, refresh, preserve, or replace a single destination.

    Args:
        link: Pairing to install.
        plugin_root: Current plugin install root.
        lineage: Result of :func:`cache_lineage`, or ``None``.
        approve: Whether the caller authorised replacing foreign entries.
        dry_run: Report only; make no filesystem change.
        result: Accumulator mutated in place.
    """
    target = read_link_target(link.dest)
    exists = link.dest.is_symlink() or link.dest.exists()
    foreign = exists and not (target is not None and owns(link.dest, target, plugin_root, lineage))

    if foreign and not approve:
        result.conflicts.append(_describe(link.dest, target))
        return
    if target is not None and resolve_target(link.dest, target) == Path(os.path.normpath(str(link.source))):
        result.unchanged.append(link.dest.name)
        return
    if dry_run:
        (result.replaced if foreign else result.linked).append(link.dest.name)
        return
    try:
        _replace_link(link)
    except OSError as exc:
        result.failed.append(f"{link.dest.name}: {exc}")
        return
    (result.replaced if foreign else result.linked).append(link.dest.name)


def _prune_obsolete(
    rules_dest: Path,
    keep: set[str],
    plugin_name: str,
    plugin_root: Path,
    lineage: Path | None,
    dry_run: bool,
    result: SyncResult,
) -> None:
    """Delete owned namespaced links whose source left the plugin.

    Only symlinks carrying this plugin's ``<plugin>-`` prefix are considered,
    and each still has to pass the same ownership proof used for replacement —
    a real file or a foreign link with a matching name is never removed.

    Args:
        rules_dest: ``<home>/.claude/rules``.
        keep: Destination basenames the current version still provides.
        plugin_name: Owning plugin name (prefix filter).
        plugin_root: Current plugin install root.
        lineage: Result of :func:`cache_lineage`, or ``None``.
        dry_run: Report only; make no filesystem change.
        result: Accumulator mutated in place.
    """
    if not rules_dest.is_dir():
        return
    prefix = f"{plugin_name}-"
    for entry in sorted(rules_dest.iterdir()):
        if entry.name in keep or not entry.name.startswith(prefix) or entry.suffix != ".md":
            continue
        target = read_link_target(entry)
        if target is None or not owns(entry, target, plugin_root, lineage):
            continue
        if not dry_run:
            try:
                entry.unlink()
            except OSError as exc:
                result.failed.append(f"{entry.name}: {exc}")
                continue
        result.removed.append(entry.name)


def sync(plugin_name: str, plugin_root: Path, home: Path, approve: bool = False, dry_run: bool = False) -> SyncResult:
    """Install this plugin's rules into ``<home>/.claude/rules/``.

    Args:
        plugin_name: Name the plugin manifest must declare.
        plugin_root: Plugin install root.
        home: User home directory.
        approve: Replace foreign entries instead of preserving them.
        dry_run: Report the plan without touching the filesystem.

    Returns:
        A :class:`SyncResult` describing every destination considered.

    Raises:
        SourceError: When source validation fails — nothing has been mutated.
    """
    # A relative root would be written verbatim into the symlink and resolved
    # against ~/.claude/rules/, producing a dangling link that looks installed.
    plugin_root = Path(os.path.abspath(plugin_root))
    rules = validate_source(plugin_root, plugin_name)
    rules_dest = home / ".claude" / "rules"
    links = [RuleLink(source=rule, dest=rules_dest / dest_name(plugin_name, rule.name)) for rule in rules]
    lineage = cache_lineage(plugin_root, home)

    result = SyncResult()
    if not dry_run:
        rules_dest.mkdir(parents=True, exist_ok=True)
    for link in links:
        _install_one(link, plugin_root, lineage, approve, dry_run, result)
    _prune_obsolete(rules_dest, {link.dest.name for link in links}, plugin_name, plugin_root, lineage, dry_run, result)
    return result


def _report(result: SyncResult) -> str:
    """Render a run's outcome as indented stdout lines.

    Args:
        result: Outcome to render.

    Returns:
        Newline-joined report (empty string when nothing happened).

    Examples:
        >>> print(_report(SyncResult(linked=["develop-quality-gates.md"])))
          linked: develop-quality-gates.md
    """
    labels = (
        ("linked", result.linked),
        ("unchanged", result.unchanged),
        ("replaced (--approve)", result.replaced),
        ("removed obsolete", result.removed),
        ("conflict, kept as-is", result.conflicts),
        ("FAILED", result.failed),
    )
    return "\n".join(f"  {label}: {item}" for label, items in labels for item in items)


def main(argv: list[str] | None = None) -> int:
    """Run rule synchronization from command-line arguments."""
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    parser = argparse.ArgumentParser(
        prog="sync_rules",
        description="Install a plugin's rules/*.md into ~/.claude/rules/ as <plugin>-<name>.md symlinks.",
    )
    parser.add_argument("--plugin-name", required=True, metavar="NAME", help="Name the plugin manifest must declare.")
    parser.add_argument(
        "--plugin-root", required=True, metavar="PATH", help="Absolute path of the installed plugin version."
    )
    parser.add_argument(
        "--home", default=None, metavar="PATH", help="Override $HOME (default: value from environment)."
    )
    parser.add_argument(
        "--approve", action="store_true", help="Replace conflicting destinations instead of preserving them."
    )
    parser.add_argument("--dry-run", action="store_true", help="Report the plan without touching the filesystem.")
    args = parser.parse_args(argv)

    home = Path(args.home) if args.home else Path(os.path.expanduser("~"))
    if not home.is_dir():
        print(f"sync_rules: home directory {str(home)!r} is not a directory", file=sys.stderr)
        return 1

    try:
        result = sync(args.plugin_name, Path(args.plugin_root), home, approve=args.approve, dry_run=args.dry_run)
    except SourceError as exc:
        print(f"sync_rules: {exc}", file=sys.stderr)
        return 1

    report = _report(result)
    if report:
        print(report)
    if result.failed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
