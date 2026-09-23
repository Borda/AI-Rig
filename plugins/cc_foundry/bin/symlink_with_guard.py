#!/usr/bin/env python
"""symlink_with_guard.py — foundry-init symlink cleanup and conflict scan.

Encapsulates Phase 1 (obsolete-cleanup) and Phase 2 (conflict-scan) from the
init/SKILL.md symlinking flow. Phase 4 (the actual replacement that depends on
interactive user choices) stays inline in SKILL.md.

Two modes, both consume the same input:

* ``cleanup`` — remove foundry-managed symlinks whose targets no longer exist
  in the current plugin version. Side-effect: deletes stale symlinks. Prints
  ``removed obsolete: <name>`` lines to stdout for each removal. Also purges
  every foundry-managed symlink lingering under ``~/.claude/skills/`` and
  ``~/.claude/agents/`` — both are dispatched directly from the plugin
  namespace and never need an entry there, so any such symlink is obsolete.
* ``scan`` — identify symlink/file entries needing user confirmation. Prints
  one conflict descriptor per line to stdout (ready for bash array consumption
  via ``mapfile -t LINK_CONFLICTS < <(... scan ...)``).

Two iteration patterns are evaluated by both modes; cleanup adds two
unconditional purge scopes:

1. ``<plugin>/rules/<name>.md`` ↔ ``$HOME/.claude/rules/foundry-<name>.md``
2. ``<plugin>/TEAM_PROTOCOL.md`` ↔ ``$HOME/.claude/TEAM_PROTOCOL.md`` (single file)
3. (cleanup only) ``$HOME/.claude/skills/`` — purge any foundry-managed symlink
4. (cleanup only) ``$HOME/.claude/agents/`` — purge any foundry-managed symlink

Rules are namespaced because ``~/.claude/rules/`` is one flat directory shared by
every plugin and four of them ship a ``rules/quality-gates.md``. Cleanup migrates
a pre-namespace unprefixed link to the new name — but only when it passes the
ownership proof below.

Two ownership regimes, deliberately different. Scopes 1–2 are destinations
foundry legitimately writes, so mutating one requires :func:`_owns`: the target
must resolve under the current plugin root or the same
``$HOME/.claude/plugins/cache/<marketplace>/foundry/`` lineage. Path substrings
are not accepted — an earlier implementation used one and deleted a user's
``dotfiles/plugins/cc_foundry/rules/…`` link. Scopes 3–4 are destinations foundry
never writes at all, so any symlink there matching the ``borda-ai-rig/foundry/``
marker substring is obsolete by construction and purged on that cheaper test.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/symlink_with_guard.py" cleanup --plugin-root <path> \
        [--home <path>] [--marker <str>]
    python "${CLAUDE_PLUGIN_ROOT}/bin/symlink_with_guard.py" scan --plugin-root <path> \
        [--home <path>] [--marker <str>]
    python "${CLAUDE_PLUGIN_ROOT}/bin/symlink_with_guard.py" create --plugin-root <path> \
        --src <path> --dest <path> [--home <path>]

Exit codes (same 0/1/2 contract for all modes — including ``create``):
    0   success (no failures)
    1   irrecoverable error (missing plugin root, write failure)
    2   argument error
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class GuardMode(str, Enum):
    """Operation this invocation performs.

    Subclasses ``str`` (not ``enum.StrEnum``) because ``requires-python`` is ``>=3.10``.

    Examples:
        >>> GuardMode.CREATE == "create"
        True
    """

    CLEANUP = "cleanup"
    SCAN = "scan"
    CREATE = "create"


_DEFAULT_MARKER = "borda-ai-rig/foundry/"

# Rules land in Claude's flat ``~/.claude/rules/`` namespace, which every plugin
# shares. Four plugins ship a ``rules/quality-gates.md``, so source basenames
# would collide; each rule installs as ``foundry-<source-name>.md`` instead. The
# prefix is inert — verified against Claude Code 2.1.220 that it changes neither
# unconditional loading nor ``paths:`` frontmatter matching.
_RULE_PREFIX = "foundry-"

# Marker is used in substring matches against `readlink` output (see
# `_is_foundry_managed`). Restricting to filesystem-safe characters prevents a
# caller from sneaking shell metacharacters or path traversal sequences past
# downstream consumers that may interpret matches loosely.
_MARKER_ALLOWED = re.compile(r"^[A-Za-z0-9_/.-]+$")


def _validate_marker(marker: str) -> None:
    """Validate the marker against the allowlist pattern.

    Args:
        marker: User-supplied substring used to identify foundry-managed links.

    Raises:
        ValueError: If ``marker`` is empty or contains disallowed characters.

    Examples:
        >>> _validate_marker("borda-ai-rig/foundry/")  # no raise
        >>> try:
        ...     _validate_marker("evil; rm -rf /")
        ... except ValueError:
        ...     print("rejected")
        rejected
    """
    if not marker:
        raise ValueError("marker cannot be empty")
    if not _MARKER_ALLOWED.match(marker):
        raise ValueError(
            f"marker contains disallowed characters: {marker!r} — allowed: [A-Za-z0-9_/.-]",
        )


def _assert_dest_under_home_claude(dest: Path, home: Path) -> Path:
    """Resolve ``dest`` and assert it lives under ``home/.claude``.

    `create` mode writes a real symlink at ``dest`` — without containment
    checks a caller could materialise a link anywhere on the filesystem (e.g.
    ``/etc/passwd``). All foundry-managed user state lives under
    ``~/.claude/``; reject anything outside.

    Args:
        dest: Destination path supplied by the caller.
        home: User's home directory (typically ``~``).

    Returns:
        The resolved (absolute, symlink-free) destination path.

    Raises:
        ValueError: If the resolved destination is not under ``home/.claude``.
    """
    home_claude = (home / ".claude").resolve()
    # Resolve the parent because ``dest`` itself may not yet exist (we are
    # about to create it) and, if it does exist as a symlink, this check must
    # judge the dest LOCATION rather than follow the symlink to its target.
    resolved_parent = dest.parent.resolve()
    # normpath collapses a literal `..`/`.` leaf before the containment test:
    # without it, a dest like `home/.claude/..` joins to a path whose OWN
    # lexical parent is `home/.claude`, so `resolved.parents` reports it as
    # "contained" even though the true location — one level up, `home` itself
    # — sits outside the boundary. `os.path.normpath` is already this
    # module's pattern for the same class of problem (see `_resolve_target`).
    resolved = Path(os.path.normpath(str(resolved_parent / dest.name)))
    # No caller legitimately wants dest to BE ~/.claude itself — every real
    # target is a path under it (e.g. rules/x.md). Accepting equality let
    # `--dest "$HOME/.claude"` on a not-yet-existing .claude replace the whole
    # directory with a symlink into the plugin tree; `home_claude in
    # resolved.parents` alone requires strictly-under, closing that case.
    if home_claude not in resolved.parents:
        raise ValueError(
            f"dest must be under ~/.claude/, got: {resolved}",
        )
    return resolved


@dataclass(frozen=True)
class _Entry:
    """One (source, dest, kind) triple to evaluate.

    Attributes:
        source: Absolute path inside the plugin tree (file or directory).
        dest: Absolute path inside ``$HOME/.claude/`` (symlink target on disk).
        kind: ``"file"`` for files, ``"dir"`` for directories — drives the
            stat predicate used for real-entry detection.
    """

    source: Path
    dest: Path
    kind: str


def _strip_extended_prefix(path_str: str) -> str:
    """Drop a Windows extended-length (``\\\\?\\``) prefix from a path string.

    Windows stores an absolute symlink target in the reparse point with this
    prefix, so ``os.readlink`` hands it back verbatim. Every comparison in this
    module — ``Path.is_relative_to`` against a plugin root, the ``marker``
    substring test, the conflict descriptor printed for the user — treats the
    prefixed form as a *different* path, which silently disables cleanup and
    turns owned links into conflicts. Strip it once at the boundary instead.

    The UNC variant maps back to its double-backslash form rather than losing
    four characters, which would corrupt a network path into a relative one.
    Non-Windows targets pass through untouched.

    Args:
        path_str: Raw path string, typically ``os.readlink`` output.

    Returns:
        The same path without the extended-length prefix.

    Examples:
        >>> _strip_extended_prefix("\\\\\\\\?\\\\C:\\\\Users\\\\x\\\\rules\\\\a.md")
        'C:\\\\Users\\\\x\\\\rules\\\\a.md'
        >>> _strip_extended_prefix("\\\\\\\\?\\\\UNC\\\\server\\\\share\\\\a.md")
        '\\\\\\\\server\\\\share\\\\a.md'
        >>> _strip_extended_prefix("/home/x/rules/a.md")
        '/home/x/rules/a.md'
    """
    if path_str.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path_str[len("\\\\?\\UNC\\") :]
    if path_str.startswith("\\\\?\\"):
        return path_str[len("\\\\?\\") :]
    return path_str


def _readlink(path: Path) -> str | None:
    """Return ``readlink(path)`` as a string, or ``None`` if not a symlink.

    The result is normalised through :func:`_strip_extended_prefix` so every
    downstream consumer — ownership proof, current-root test, marker match,
    conflict descriptor — sees one canonical spelling of the target.

    Args:
        path: Filesystem path to inspect.

    Returns:
        The link target text (not resolved) when ``path`` is a symlink,
        otherwise ``None``.

    Examples:
        >>> _readlink(Path("/nonexistent/path"))
    """
    if not path.is_symlink():
        return None
    try:
        return _strip_extended_prefix(os.readlink(path))
    except OSError:
        return None


def _is_foundry_managed(target: str, marker: str) -> bool:
    """Check whether a link target contains the Foundry ownership marker.

    The marker is written with forward slashes (``borda-ai-rig/foundry/``), so a
    native Windows target spelled with backslashes never matched it and both
    unconditional purge scopes silently did nothing. The separator-normalised
    form is tested as well; the raw test is kept first so a POSIX path
    containing a literal backslash cannot change meaning.

    Args:
        target: ``readlink`` output (link's stored target, not resolved).
        marker: Substring identifying foundry-owned paths.

    Returns:
        True iff ``marker`` appears in ``target``, with either separator style.

    Examples:
        >>> target = "/home/x/.claude/plugins/cache/borda-ai-rig/foundry/0.17.0/rules/x.md"
        >>> _is_foundry_managed(target, "borda-ai-rig/foundry/")
        True
        >>> target = "C:\\\\Users\\\\x\\\\cache\\\\borda-ai-rig\\\\foundry\\\\0.40.0\\\\skills\\\\a"
        >>> _is_foundry_managed(target, "borda-ai-rig/foundry/")
        True
        >>> _is_foundry_managed("/home/x/local/file.md", "borda-ai-rig/foundry/")
        False
    """
    return marker in target or marker in target.replace("\\", "/")


def _is_current(target: str, plugin_root: Path) -> bool:
    """Check whether a link target resides inside the current plugin root.

    Performs a proper path-prefix comparison (``Path.is_relative_to``) instead
    of a substring match, so a plugin root like ``…/foundry/0.1`` doesn't
    spuriously match a target under ``…/foundry/0.10``. Falls back to the
    substring check when the resolution itself fails (e.g. unresolvable
    symlink target on a foreign filesystem) so the cleanup pass is no more
    aggressive than the legacy behaviour.

    Args:
        target: ``readlink`` output.
        plugin_root: Absolute path of the currently-installed plugin version.

    Returns:
        True iff ``target`` resolves to a path under ``plugin_root``.

    Examples:
        >>> _is_current("/old/0.17.0/rules/a.md", Path("/new/0.18.0"))
        False
    """
    try:
        raw = os.path.realpath(target) if not os.path.isabs(target) else target
        resolved = Path(_strip_extended_prefix(raw))
        return resolved.is_relative_to(Path(_strip_extended_prefix(str(plugin_root.resolve()))))
    except (ValueError, OSError):
        return str(plugin_root) in target  # fallback if resolution fails


def _cache_lineage(plugin_root: Path, home: Path) -> Path | None:
    """Return the ``cache/<marketplace>/foundry/`` prefix of an installed root.

    A link left by a previous version of this plugin, installed from this
    marketplace, sits under the same two-segment prefix. Anything else — another
    marketplace, another plugin, a source checkout, a dotfiles tree — shares no
    lineage and is never adopted.

    Args:
        plugin_root: Currently-installed plugin root.
        home: Value of ``$HOME``.

    Returns:
        The lineage directory, or ``None`` when ``plugin_root`` is not an
        installed cache root (e.g. the local source checkout fallback).

    Examples:
        >>> _cache_lineage(Path("/h/.claude/plugins/cache/mkt/foundry/0.40.0"), Path("/h")).as_posix()
        '/h/.claude/plugins/cache/mkt/foundry'
        >>> _cache_lineage(Path("/src/plugins/cc_foundry"), Path("/h")) is None
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


def _resolve_target(dest: Path, target: str) -> Path:
    """Absolutise a raw ``readlink`` target without following symlinks.

    ``realpath`` is deliberately not used: resolving a foreign path could walk it
    into a directory this plugin owns and turn a user's own link into an
    adoption. Relative targets anchor at the link's directory, as the kernel
    reads them.

    Args:
        dest: The symlink whose target this is.
        target: Raw ``readlink`` output.

    Returns:
        Normalised absolute path of the target.

    Examples:
        >>> _resolve_target(Path("/h/.claude/rules/x.md"), "../../dotfiles/x.md").as_posix()
        '/h/dotfiles/x.md'
    """
    target = _strip_extended_prefix(target)
    if os.path.isabs(target):
        return Path(os.path.normpath(target))
    return Path(os.path.normpath(str(dest.parent / target)))


def _base_paths(root: Path) -> set[str]:
    """Return the normalised and realpath spellings of ``root`` used as ownership bases.

    ``realpath`` may hand back an extended-length spelling on Windows, which
    would never prefix-match a stripped target; both spellings are normalised
    the same way so the comparison is separator- and prefix-consistent.

    Args:
        root: Directory to expand into comparison bases.

    Returns:
        Set of path strings suitable for ``Path.is_relative_to``.

    Examples:
        >>> "/tmp" in _base_paths(Path("/tmp")) or "\\\\tmp" in _base_paths(Path("/tmp"))
        True
    """
    return {
        _strip_extended_prefix(os.path.normpath(str(root))),
        _strip_extended_prefix(os.path.realpath(str(root))),
    }


def _owns(dest: Path, target: str, plugin_root: Path, lineage: Path | None) -> bool:
    """Check whether Foundry may replace or delete a destination.

    This is the proof gate for every rules-scope mutation, including migrating a
    pre-namespace unprefixed link. Substring shapes such as
    ``/borda-ai-rig/foundry/`` or ``/plugins/cc_foundry/rules/`` are NOT accepted
    — an earlier implementation used one and deleted a user's
    ``dotfiles/plugins/cc_foundry/rules/…`` link.

    Args:
        dest: Existing entry under ``$HOME/.claude/``.
        target: Raw ``readlink`` output for ``dest``.
        plugin_root: Currently-installed plugin root.
        lineage: Result of :func:`_cache_lineage`, or ``None``.

    Returns:
        True iff the target lies under the current plugin root or the same
        installed-cache lineage. Broken targets are judged on path alone, so a
        dangling link foundry created is still owned (and cleanable) while a
        dangling foreign link stays untouched.

    Examples:
        >>> root = Path("/h/.claude/plugins/cache/mkt/foundry/0.40.0")
        >>> dest = Path("/h/.claude/rules/foundry-quality-gates.md")
        >>> _owns(dest, str(root / "rules/quality-gates.md"), root, None)
        True
        >>> _owns(dest, "/h/dotfiles/plugins/cc_foundry/rules/quality-gates.md", root, None)
        False
    """
    resolved = _resolve_target(dest, target)
    bases = _base_paths(plugin_root)
    if lineage is not None:
        bases |= _base_paths(lineage)
    return any(resolved.is_relative_to(base) for base in bases)


def _list_rule_files(plugin_root: Path) -> list[Path]:
    """List ``*.md`` files in ``<plugin_root>/rules``.

    Args:
        plugin_root: Plugin install root.

    Returns:
        Sorted list of rule-file paths (empty when ``rules/`` is absent).
    """
    rules_dir = plugin_root / "rules"
    if not rules_dir.is_dir():
        return []
    return sorted(p for p in rules_dir.iterdir() if p.is_file() and p.suffix == ".md")


def _build_entries(plugin_root: Path, home: Path) -> list[_Entry]:
    """Build the union of (source, dest, kind) triples for current plugin contents.

    Agents are intentionally absent from this list — they are dispatched
    directly from the plugin's installed namespace (``foundry:sw-engineer``)
    and require no ``~/.claude/agents/`` entry. Stale agent symlinks from
    pre-design installs are purged separately by :func:`cleanup` and are
    therefore never re-created here.

    Skills are absent for the same dispatch reason plus a hazard agents do not
    have: a directory carrying a ``SKILL.md`` under ``~/.claude/skills/``
    registers as a USER-LEVEL skill, and user-level skills silently shadow
    Claude Code's bundled skill of the same name. Foundry skills are invoked as
    ``/foundry:<name>`` only. ``skills/_shared`` is excluded too — no plugin may
    depend on a global ``_shared`` path; each resolves its own via
    ``bin/resolve_shared_path.py``. :func:`cleanup` purges any such symlink.

    Args:
        plugin_root: Plugin install root.
        home: Value of ``$HOME``.

    Returns:
        Triples covering rules ``*.md`` and ``TEAM_PROTOCOL.md`` (when present
        in the plugin).
    """
    entries: list[_Entry] = []
    rules_target = home / ".claude" / "rules"
    for rule in _list_rule_files(plugin_root):
        entries.append(_Entry(source=rule, dest=rules_target / f"{_RULE_PREFIX}{rule.name}", kind="file"))

    team_src = plugin_root / "TEAM_PROTOCOL.md"
    if team_src.is_file():
        entries.append(
            _Entry(source=team_src, dest=home / ".claude" / "TEAM_PROTOCOL.md", kind="file"),
        )

    return entries


def _conflict_label(entry: _Entry, target: str | None) -> str:
    """Render a conflict descriptor keyed on the destination filename.

    Format examples:

    * ``rules/foundry-foo.md → /other/path/foo.md``
    * ``rules/foundry-foo.md  (real file)``
    * ``TEAM_PROTOCOL.md → /other/...``

    The rules label names the *destination* (``foundry-foo.md``), not the source
    basename, because that is the file the user is being asked about and the key
    SKILL.md Phase 4 matches against.

    Only rules and ``TEAM_PROTOCOL.md`` reach this function — skills left
    :func:`_build_entries` (see its docstring for the shadowing hazard), so no
    ``kind="dir"`` entry is ever produced and no ``skills/<name>`` label exists.

    Args:
        entry: Triple being labelled.
        target: ``readlink`` output, or ``None`` when the dest is a real file.

    Returns:
        Single conflict line consumed by SKILL.md Phase 3/4.

    Examples:
        >>> e = _Entry(Path("/p/rules/foo.md"), Path("/d/rules/foundry-foo.md"), "file")
        >>> _conflict_label(e, "/elsewhere/foo.md")
        'rules/foundry-foo.md → /elsewhere/foo.md'
        >>> _conflict_label(e, None)
        'rules/foundry-foo.md  (real file)'
        >>> ts = _Entry(Path("/p/TEAM_PROTOCOL.md"), Path("/d/TEAM_PROTOCOL.md"), "file")
        >>> _conflict_label(ts, "/elsewhere/team.md")
        'TEAM_PROTOCOL.md → /elsewhere/team.md'
    """
    if entry.dest.name == "TEAM_PROTOCOL.md":
        prefix = "TEAM_PROTOCOL.md"
    else:
        prefix = f"rules/{entry.dest.name}"

    if target is None:
        return f"{prefix}  (real file)"
    return f"{prefix} → {target}"


def _existing_dest_symlinks(target_dir: Path) -> list[Path]:
    """Yield all symlinks directly under ``target_dir`` (non-recursive).

    Args:
        target_dir: Directory to scan; missing dirs return an empty list.

    Returns:
        Sorted list of symlink paths.
    """
    if not target_dir.is_dir():
        return []
    return sorted(p for p in target_dir.iterdir() if p.is_symlink())


def _cleanup_rules(plugin_root: Path, home: Path, log: list[str]) -> None:
    """Remove every owned link in ``~/.claude/rules/`` the current version does not provide.

    Ownership is the only licence to delete (:func:`_owns`) — never the link's
    name and never a path substring. Three cases are therefore all handled by one
    rule, "owned but not expected":

    * a link to a rule dropped in this version (obsolete),
    * a dangling link into the current root left by a source rename,
    * a pre-namespace unprefixed link such as ``quality-gates.md``, superseded by
      ``foundry-quality-gates.md`` (migration).

    A same-named link owned by a sibling plugin, a foreign marketplace, a source
    checkout, or a dotfiles tree fails the proof and stays.

    Args:
        plugin_root: Currently-installed plugin root.
        home: Value of ``$HOME``.
        log: Accumulator appended in place.
    """
    rules_dest = home / ".claude" / "rules"
    lineage = _cache_lineage(plugin_root, home)
    expected = {f"{_RULE_PREFIX}{rule.name}" for rule in _list_rule_files(plugin_root)}
    for link_path in _existing_dest_symlinks(rules_dest):
        if link_path.name in expected:
            continue
        target = _readlink(link_path)
        if target is None or not _owns(link_path, target, plugin_root, lineage):
            continue
        try:
            link_path.unlink()
        except OSError:
            continue
        log.append(f"removed obsolete: {link_path.name}")


def cleanup(plugin_root: Path, home: Path, marker: str) -> list[str]:
    """Remove foundry-managed symlinks the current plugin version does not provide.

    Four scopes evaluated, under two different ownership regimes.

    ``~/.claude/rules/`` and ``TEAM_PROTOCOL.md`` are destinations foundry
    legitimately writes, so removal there demands the strict proof in
    :func:`_owns` — under the current root, or the same installed-cache lineage.
    See :func:`_cleanup_rules` for the rules pass, which also performs the
    unprefixed-to-``foundry-`` migration.

    ``~/.claude/skills/`` and ``~/.claude/agents/`` are destinations foundry
    never writes at all: both are dispatched from the plugin namespace and are
    never produced by :func:`_build_entries`. Any foundry-marked symlink there is
    obsolete by construction, so those two scopes keep the cheaper ``marker``
    substring test and purge with no source-existence check. The two differ in
    one respect — see the inline comments for why the skills scope also removes
    links pointing at the *current* plugin root while the agents scope keeps
    those.

    Args:
        plugin_root: Currently-installed plugin root.
        home: Value of ``$HOME``.
        marker: Substring identifying foundry-managed targets (skills/agents scopes).

    Returns:
        Log lines describing removed obsolete items or user-level skill links. Calling
        :func:`main` also prints these lines to stdout.
    """
    log: list[str] = []
    skills_dest = home / ".claude" / "skills"
    agents_dest = home / ".claude" / "agents"
    team_dest = home / ".claude" / "TEAM_PROTOCOL.md"

    _cleanup_rules(plugin_root, home, log)

    # --- TEAM_PROTOCOL.md ---
    if team_dest.is_symlink() and not (plugin_root / "TEAM_PROTOCOL.md").is_file():
        target = _readlink(team_dest)
        if target is not None and _owns(team_dest, target, plugin_root, _cache_lineage(plugin_root, home)):
            try:
                team_dest.unlink()
                log.append("removed obsolete: TEAM_PROTOCOL.md")
            except OSError:
                pass

    # --- skills/ (unconditional purge) ---
    # No source-existence check and — unlike the agents scope below — no
    # `_is_current` skip either. A link pointing at the CURRENT plugin root is
    # precisely the bug this purge exists to fix: it registers the dir as a
    # USER-LEVEL skill, which silently shadows Claude Code's bundled skill of
    # the same name (this is what broke bare `/review`). Do not "restore parity"
    # with the agents scope here — the asymmetry is deliberate.
    for link_path in _existing_dest_symlinks(skills_dest):
        target = _readlink(link_path)
        if target is None or not _is_foundry_managed(target, marker):
            continue
        try:
            link_path.unlink()
        except OSError:
            continue
        log.append(f"removed user-level skill link: {link_path.name}")

    # --- agents/ (unconditional purge) ---
    # No source-existence check — agents are dispatched from the plugin
    # namespace and are never (re-)created in ~/.claude/agents/. Any
    # foundry-managed symlink lingering here is by definition obsolete.
    for link_path in _existing_dest_symlinks(agents_dest):
        target = _readlink(link_path)
        if target is None or not _is_foundry_managed(target, marker):
            continue
        if _is_current(target, plugin_root):
            # A symlink under the current plugin root would still be obsolete
            # by design — we never create them — but leaving it gives a clear
            # signal that something outside init is staging it. Skip removal
            # so the operator can investigate.
            continue
        try:
            link_path.unlink()
        except OSError:
            continue
        log.append(f"removed obsolete agent: {link_path.name}")

    return log


def scan(plugin_root: Path, home: Path, marker: str) -> list[str]:
    """Identify dest entries needing user confirmation before symlink replacement.

    An owned link — current version or same installed-cache lineage — is
    auto-replaced in Phase 4 without prompting. Only these states surface:

    * dest is a real file (not a symlink),
    * dest is a symlink whose target fails the :func:`_owns` proof.

    Args:
        plugin_root: Currently-installed plugin root.
        home: Value of ``$HOME``.
        marker: Unused here; kept so ``scan`` and ``cleanup`` share one CLI.

    Returns:
        Sorted list of conflict descriptor strings, one per affected dest.
    """
    del marker  # ownership is proved by path lineage, not by a substring
    conflicts: list[str] = []
    lineage = _cache_lineage(plugin_root, home)
    for entry in _build_entries(plugin_root, home):
        if entry.dest.is_symlink():
            target = _readlink(entry.dest)
            if target is None or _owns(entry.dest, target, plugin_root, lineage):
                continue
            conflicts.append(_conflict_label(entry, target))
        elif entry.dest.exists():
            conflicts.append(_conflict_label(entry, None))
    return conflicts


def create_link(src: Path, dest: Path, home: Path) -> str:
    """Create a symlink at *dest* pointing to *src*, falling back to a copy.

    Two-tier cascade — stops at first success:

    * **Tier 1 — real symlink**: always attempted; works on macOS/Linux
      unconditionally; requires Developer Mode on Windows.
    * **Tier 2 — copy + sidecar**: ``shutil.copytree`` / ``shutil.copy2``.
      Writes ``.{dest.name}.sourced_from`` alongside dest containing the
      source path relative to ``home / ".claude"`` (absolute fallback when
      src is not under that directory). Always reached on Windows without
      Developer Mode, for both file and directory targets.

    A prior revision inserted a Windows NTFS-junction tier here via
    ``cmd /c mklink /J`` before falling through to the copy tier. It was
    removed as dead code carrying a live cmd.exe metacharacter-injection
    shape (``subprocess.list2cmdline`` does not escape ``&|<>^%``): no
    caller in this repository ever requested a directory link through
    `create` mode. A future reinstatement should use ``_winapi.CreateJunction``
    (stdlib, no cmd.exe, no shell re-parsing) — exercised by CPython's own
    ``test_shutil.py`` and ``test_ntpath.py`` — never the ``cmd /c mklink``
    form.

    Args:
        src: Absolute source path (plugin tree file or directory).
        dest: Absolute destination path inside ``$HOME/.claude/``.
        home: User home directory (for computing relative sidecar path).

    Returns:
        ``"symlink"`` or ``"copy"`` — whichever tier succeeded.

    Raises:
        OSError: When ``dest`` already exists in any form (real file, real
            directory, or symlink — including a dangling one), or when all
            applicable tiers fail.
    """
    # Tier 1 fails on POSIX with FileExistsError (an OSError subclass, caught
    # below) whenever dest already exists in ANY form, including a symlink —
    # that alone cannot tell "safe to fall through" apart from "dest is a
    # pre-existing entry Tier 2 would silently overwrite, or a symlink Tier 2
    # would write through to wherever it points, possibly outside the
    # ~/.claude containment boundary". `dest.exists()` follows symlinks and is
    # False for a dangling one, so both checks together are required to
    # refuse every pre-existing case up front, shared by both tiers.
    if dest.exists() or dest.is_symlink():
        raise OSError(f"refusing to overwrite existing dest: {dest}")
    # sidecar_path is deterministic from dest alone, so checked up front too —
    # checking it only right before Tier 2's write_text (below) would let
    # copytree/copy2 materialise dest first, and a refusal on the sidecar
    # would then leave dest half-written with no sidecar and no recovery path
    # (a re-run trips the dest-exists guard above with nothing to clean up).
    sidecar_path = dest.parent / f".{dest.name}.sourced_from"
    if sidecar_path.exists() or sidecar_path.is_symlink():
        raise OSError(f"refusing to overwrite existing sidecar: {sidecar_path}")

    # --- Tier 1: real symlink ---
    try:
        dest.symlink_to(src)
        return "symlink"
    except FileExistsError:
        # A dest created by a concurrent actor in the window between the guard
        # check above and this call must still fail closed, never fall through
        # to Tier 2's overwrite. Windows without Developer Mode raises a plain
        # OSError here (WinError 1314), not FileExistsError, so that path is
        # unaffected and still falls through below.
        raise
    except (OSError, NotImplementedError):
        pass

    # --- Tier 2: copy + sidecar ---
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dest, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dest)

    claude_dir = home / ".claude"
    try:
        sidecar_value = src.relative_to(claude_dir).as_posix()
    except ValueError:
        sidecar_value = src.as_posix()
    sidecar_path.write_text(sidecar_value + "\n", encoding="utf-8")
    return "copy"


def main(argv: list[str] | None = None) -> int:
    """Install or remove guarded links requested by the command line."""
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    parser = argparse.ArgumentParser(
        prog="symlink_with_guard",
        description="foundry-init symlink cleanup, conflict scan, and link creation.",
    )
    parser.add_argument(
        "mode",
        choices=[mode.value for mode in GuardMode],
        help=(
            "`cleanup` removes obsolete foundry-managed symlinks; "
            "`scan` prints conflicts; "
            "`create` materialises a symlink/copy at --dest pointing at --src."
        ),
    )
    parser.add_argument(
        "--plugin-root",
        required=False,
        default=None,
        metavar="PATH",
        help="Absolute path of the currently-installed foundry plugin version (required for cleanup/scan/create).",
    )
    parser.add_argument(
        "--home",
        default=None,
        metavar="PATH",
        help="Override $HOME (default: value from environment).",
    )
    parser.add_argument(
        "--marker",
        default=_DEFAULT_MARKER,
        metavar="STR",
        help=f"Substring identifying foundry-managed symlink targets (default: {_DEFAULT_MARKER!r}).",
    )
    parser.add_argument(
        "--src",
        default=None,
        metavar="PATH",
        help="Absolute source path to link from (required for `create` mode only).",
    )
    parser.add_argument(
        "--dest",
        default=None,
        metavar="PATH",
        help="Absolute destination path to materialise (required for `create` mode only).",
    )
    args = parser.parse_args(argv)

    home = Path(args.home) if args.home else Path(os.path.expanduser("~"))
    if not home.is_dir():
        print(f"error: home directory {str(home)!r} is not a directory", file=sys.stderr)
        return 1

    if args.mode == GuardMode.CREATE:
        if not args.src or not args.dest:
            print(
                "symlink_with_guard: `create` mode requires both --src and --dest",
                file=sys.stderr,
            )
            return 2
        if not args.plugin_root:
            print("symlink_with_guard: `create` mode requires --plugin-root", file=sys.stderr)
            return 2
        src = Path(os.path.abspath(args.src))
        plugin_root_bases = _base_paths(Path(args.plugin_root))
        if not any(src.is_relative_to(base) for base in plugin_root_bases):
            print(f"symlink_with_guard: --src must be under --plugin-root, got: {src}", file=sys.stderr)
            return 2
        dest = Path(args.dest)
        try:
            dest = _assert_dest_under_home_claude(dest, home)
        except ValueError as exc:
            print(f"symlink_with_guard: {exc}", file=sys.stderr)
            return 2
        try:
            tier = create_link(src, dest, home)
        except OSError as exc:
            print(f"symlink_with_guard: {exc}", file=sys.stderr)
            return 1
        print(tier)
        return 0

    # cleanup / scan share these prerequisites
    try:
        _validate_marker(args.marker or "")
    except ValueError as exc:
        print(f"symlink_with_guard: {exc}", file=sys.stderr)
        return 2

    if not args.plugin_root:
        print(
            "symlink_with_guard: `cleanup` and `scan` modes require --plugin-root",
            file=sys.stderr,
        )
        return 2

    plugin_root = Path(args.plugin_root)
    if not plugin_root.is_dir():
        print(
            f"error: --plugin-root {args.plugin_root!r} is not a directory",
            file=sys.stderr,
        )
        return 1

    if args.mode == GuardMode.CLEANUP:
        for line in cleanup(plugin_root, home, args.marker):
            print(f"  {line}")
        return 0

    # mode == GuardMode.SCAN
    for conflict in scan(plugin_root, home, args.marker):
        print(conflict)
    return 0


if __name__ == "__main__":
    sys.exit(main())
