#!/usr/bin/env python
"""resolve_skill_subdir.py — resolve a plugin skill subdirectory via three-tier cascade.

Encapsulates the LOCAL_MODE → cache scan resolution pattern used by foundry
skills (audit, calibrate, manage) to locate per-skill ``templates/`` and
``modes/`` directories regardless of whether the plugin is being run from a
local source tree or from the installed plugin cache.

Cascade order:

1. ``plugins/cc_foundry/skills/<skill>/<subdir>`` when ``--local`` is passed and
   the local source tree contains it (plugin-dev workflows) — checked first
   so ``--local`` genuinely overrides ``CLAUDE_PLUGIN_ROOT`` rather than being
   a no-op whenever that env var happens to be set.
2. ``CLAUDE_PLUGIN_ROOT/skills/<skill>/<subdir>`` when ``CLAUDE_PLUGIN_ROOT``
   is set in the environment and the path exists (handles installed-plugin
   runtime — the canonical fast path).
3. ``.claude/skills/<skill>/<subdir>`` when present (project-local override).
4. Fallback scan: ``find ~/.claude/plugins/cache -path '*/<skill>/<subdir>'``
   then pick a deterministic best match (highest version, lex-sorted).

Prints the resolved absolute path to stdout on success. On failure, prints a
``! BREAKING`` line to stderr and exits 1.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_skill_subdir.py" <skill> <subdir> [--local]

Arguments:
    skill   Skill directory name (e.g. ``calibrate``, ``manage``, ``audit``).
    subdir  Subdirectory name to resolve (e.g. ``modes``, ``templates``).

Options:
    --local  Prefer ``plugins/cc_foundry/skills/<skill>/<subdir>`` after
             ``CLAUDE_PLUGIN_ROOT`` check; for plugin-dev workflows.

Exit codes:
    0  Success — resolved path printed to stdout
    1  Subdirectory not found in any tier
    2  Argument error
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

#: Characters permitted in a skill name; any other character is rejected as unsafe.
_SKILL_RE_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789-_"
#: Matches each run of digits in a version path segment, used to sort versions numerically.
_VERSION_TOKEN_RE = re.compile(r"\d+")


def _validate_token(value: str, label: str) -> None:
    """Reject empty, slashed, or shell-metacharacter tokens to avoid path traversal.

    Tokens are accepted only when every character is in ``[a-z0-9_-]``; this
    blocks path separators (``/``, ``\\``), traversal segments (``..``), and
    any shell metacharacter outright. No silent sanitisation — invalid input
    raises ``ValueError`` so the caller can present a clear error message.

    Args:
        value: Token to validate.
        label: Field name used in the error message.

    Raises:
        ValueError: When ``value`` is empty or contains a character outside
            ``[a-z0-9_-]``.

    Examples:
        >>> _validate_token("calibrate", "skill")
        >>> _validate_token("templates", "subdir")
        >>> _validate_token("with_underscore", "skill")
        >>> _validate_token("", "skill")
        Traceback (most recent call last):
            ...
        ValueError: skill must not be empty
        >>> _validate_token("../etc", "skill")
        Traceback (most recent call last):
            ...
        ValueError: skill '../etc' contains disallowed characters (allowed: [a-z0-9_-])
        >>> _validate_token("foo/bar", "subdir")
        Traceback (most recent call last):
            ...
        ValueError: subdir 'foo/bar' contains disallowed characters (allowed: [a-z0-9_-])
    """
    if not value:
        raise ValueError(f"{label} must not be empty")
    for ch in value:
        if ch not in _SKILL_RE_CHARS:
            raise ValueError(f"{label} {value!r} contains disallowed characters (allowed: [a-z0-9_-])")


def _from_plugin_root(skill: str, subdir: str) -> Path | None:
    """Resolve via ``CLAUDE_PLUGIN_ROOT`` env var when set.

    Args:
        skill: Skill directory name.
        subdir: Subdirectory under the skill (e.g. ``templates``).

    Returns:
        The candidate path when it exists as a directory, else ``None``.
    """
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    if not plugin_root:
        return None
    candidate = Path(plugin_root) / "skills" / skill / subdir
    return candidate if candidate.is_dir() else None


def _from_local_source(skill: str, subdir: str) -> Path | None:
    """Resolve via the in-tree ``plugins/cc_foundry`` source layout.

    Args:
        skill: Skill directory name.
        subdir: Subdirectory under the skill.

    Returns:
        The candidate path when it exists as a directory, else ``None``.
    """
    candidate = Path("plugins") / "cc_foundry" / "skills" / skill / subdir
    return candidate if candidate.is_dir() else None


def _from_project_local(skill: str, subdir: str) -> Path | None:
    """Resolve via the project-local override at ``.claude/skills/``.

    Args:
        skill: Skill directory name.
        subdir: Subdirectory under the skill.

    Returns:
        The candidate path when it exists as a directory, else ``None``.
    """
    candidate = Path(".claude") / "skills" / skill / subdir
    return candidate if candidate.is_dir() else None


def _version_sort_key(path: Path) -> tuple:
    """Return a version-aware sort key — mirrors ``sort -V`` segment ordering.

    Splits the path into segments, then each segment into (int-runs, text-runs)
    so that ``0.18.0`` sorts after ``0.9.0`` (numeric compare) but a plain
    string suffix still falls back to lexical ordering.

    Args:
        path: Filesystem path; each segment is keyed individually.

    Returns:
        Tuple of per-segment tuples suitable for ``sorted(key=...)``.

    Examples:
        >>> _version_sort_key(Path("/a/0.9.0/x")) < _version_sort_key(Path("/a/0.18.0/x"))
        True
        >>> _version_sort_key(Path("/a/foo")) == _version_sort_key(Path("/a/foo"))
        True
    """
    keys: list[tuple] = []
    for segment in path.parts:
        # Each segment becomes a tuple of alternating (numeric, text) keys so
        # numeric runs compare as ints and surrounding text stays stable.
        tokens: list[tuple[int, str | int]] = []
        last = 0
        for match in _VERSION_TOKEN_RE.finditer(segment):
            start, end = match.span()
            if start > last:
                tokens.append((0, segment[last:start]))  # text tag = 0 (sorts before numeric)
            tokens.append((1, int(match.group())))  # numeric tag = 1
            last = end
        if last < len(segment):
            tokens.append((0, segment[last:]))
        keys.append(tuple(tokens) if tokens else ((0, ""),))
    return tuple(keys)


def _find_version_dir(match_path: Path, marker: str = "borda-ai-rig") -> Path | None:
    """Walk up from a cache match to the plugin's version-level directory.

    A cache match path follows the layout
    ``.../borda-ai-rig/<plugin>/<version>/.../<skill>/<subdir>``. The version
    segment is the one whose **grandparent's** basename equals ``marker``
    (i.e. version dir is `<marker>/<plugin>/<version>` — two levels below the
    marker). Walks upward from ``match_path`` and returns the deepest such
    ancestor. When ``marker`` isn't found above ``match_path`` (i.e. the
    match is in some other layout), returns ``None`` — the caller treats this
    as "no orphan check possible, include the match".

    Args:
        match_path: A directory beneath the version dir to start from.
        marker: Marketplace segment that sits two levels above the version
            directory (default ``"borda-ai-rig"``).

    Returns:
        The version-level directory, or ``None`` when no ancestor has
        ``marker`` as its grandparent's basename.

    Examples:
        >>> _find_version_dir(Path("/x/borda-ai-rig/foundry/0.18.0/skills/_shared")).as_posix()
        '/x/borda-ai-rig/foundry/0.18.0'
        >>> _find_version_dir(Path("/x/borda-ai-rig/oss/0.9.0/skills/_shared/inner")).as_posix()
        '/x/borda-ai-rig/oss/0.9.0'
        >>> _find_version_dir(Path("/x/elsewhere/skills/_shared")) is None
        True
    """
    current = match_path
    # Bound the walk so a path that never contains the marker terminates.
    for _ in range(20):
        if current.parent == current:
            return None
        # current is the version dir when current.parent.parent.name == marker.
        grandparent = current.parent.parent
        if grandparent != current.parent and grandparent.name == marker:
            return current
        current = current.parent
    return None


def _from_cache_scan(skill: str, subdir: str, home: Path) -> Path | None:
    """Resolve via ``find ~/.claude/plugins/cache -path '*/<skill>/<subdir>'``.

    Args:
        skill: Skill directory name.
        subdir: Subdirectory name.
        home: Value of ``$HOME``.

    Returns:
        The version-greatest matching directory (matches ``sort -Vr | head -1``
        behaviour for semantic-version path segments) or ``None``. Matches
        whose version directory carries a ``.orphaned_at`` sentinel are
        filtered out before ranking — they represent versions superseded by a
        later install and Claude Code no longer dispatches to them.
    """
    cache_root = home / ".claude" / "plugins" / "cache"
    if not cache_root.is_dir():
        return None
    matches: list[Path] = []
    suffix = f"/{skill}/{subdir}"
    # Bounded walk: prune at fixed depth so a stray giant tree never blows up.
    for dirpath, dirnames, _files in os.walk(cache_root):
        depth = Path(dirpath).relative_to(cache_root).parts
        if len(depth) > 6:
            dirnames[:] = []
            continue
        # Match dirs whose path ends with /<skill>/<subdir> (normalize for Windows)
        if dirpath.replace("\\", "/").endswith(suffix):
            match_path = Path(dirpath)
            version_dir = _find_version_dir(match_path)
            if version_dir is not None and (version_dir / ".orphaned_at").exists():
                continue
            matches.append(match_path)
    if not matches:
        return None
    matches.sort(key=_version_sort_key, reverse=True)
    return matches[0]


def resolve(skill: str, subdir: str, *, local: bool, home: Path | None = None) -> Path | None:
    """Run the three-tier cascade and return the first match.

    Args:
        skill: Skill directory name.
        subdir: Subdirectory under the skill.
        local: When True, the local source tree is preferred ahead of the
            ``CLAUDE_PLUGIN_ROOT`` tier (plugin-dev workflow).
        home: Override ``$HOME`` for the cache-scan tier. Defaults to
            ``os.path.expanduser("~")``.

    Returns:
        First matching directory path, or ``None`` when no tier resolves.

    Examples:
        >>> # All tiers miss for a fictional skill — None.
        >>> import os
        >>> os.environ.pop("CLAUDE_PLUGIN_ROOT", None) is None or True
        True
        >>> resolve("__nonexistent__", "__nope__", local=False, home=Path("/__no_home__")) is None
        True
    """
    if home is None:
        home = Path(os.path.expanduser("~"))

    # Tier 1: local source tree, when ``--local`` opted in. Must precede the
    # CLAUDE_PLUGIN_ROOT tier below — otherwise ``--local`` is a no-op whenever
    # CLAUDE_PLUGIN_ROOT is set (the common case, since it's always set inside
    # an installed plugin run), because that tier would win first.
    if local:
        hit = _from_local_source(skill, subdir)
        if hit is not None:
            return hit

    # Tier 2: CLAUDE_PLUGIN_ROOT (canonical fast path inside the installed plugin)
    hit = _from_plugin_root(skill, subdir)
    if hit is not None:
        return hit

    # Tier 3: project-local override
    hit = _from_project_local(skill, subdir)
    if hit is not None:
        return hit

    # Tier 4: cache scan
    return _from_cache_scan(skill, subdir, home)


def main(argv: list[str] | None = None) -> int:
    """Resolve and print the selected skill subdirectory."""
    parser = argparse.ArgumentParser(
        prog="resolve_skill_subdir",
        description="Resolve a plugin skill subdirectory via three-tier cascade.",
    )
    parser.add_argument("skill", help="Skill directory name (e.g. calibrate, manage, audit).")
    parser.add_argument("subdir", help="Subdirectory under the skill (e.g. modes, templates).")
    parser.add_argument(
        "--local",
        action="store_true",
        help="Prefer plugins/cc_foundry/skills/<skill>/<subdir> (plugin-dev workflows).",
    )
    parser.add_argument(
        "--home",
        default=None,
        help="Override $HOME for the cache-scan tier (default: env value).",
    )
    args = parser.parse_args(argv)

    try:
        _validate_token(args.skill, "skill")
        _validate_token(args.subdir, "subdir")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    home = Path(args.home) if args.home else Path(os.path.expanduser("~"))
    resolved = resolve(args.skill, args.subdir, local=args.local, home=home)
    if resolved is None:
        print(
            f"! BREAKING: {args.skill}/{args.subdir} not found — re-install foundry plugin: "
            "claude plugin install foundry@borda-ai-rig",
            file=sys.stderr,
        )
        return 1

    print(resolved.as_posix())
    return 0


if __name__ == "__main__":
    sys.exit(main())
