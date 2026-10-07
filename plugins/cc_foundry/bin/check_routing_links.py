#!/usr/bin/env python3
"""check_routing_links.py — validate computed file-path references in plugin SKILL.md and agent .md files.

Implements three checks:

  R1 — Computed path resolution (local + installed duality)
       Walk all SKILL.md and agent .md files. Extract every pattern that constructs a
       file path via variable substitution. Verify each resolved path exists both:
         (a) locally under plugins/<plugin>/...
         (b) in the active installed plugin cache (version from installed_plugins.json)
       WARN: file exists locally but NOT in installed cache (local-only — not yet released).
       WARN: file exists in installed cache but NOT locally (installed-only — stale install).
       INFO: plugin not installed — can't verify installed state.
       Both installed-cache comparisons are advisory (WARN, never FAIL): the cache is
       machine-local mutable state, so a local-only file is the normal state between adding
       a file and releasing it. Failing on it would block the commit that releases the file.

  R2 — Grep-visible referencing (orphan-risk detection)
       For every .md file under plugins/*/skills/*/modes/, plugins/*/skills/*/templates/,
       and plugins/*/skills/_shared/ — verify its basename appears as a literal string in at
       least one consumer SKILL.md or agent .md file in the same plugin.
       Flag: ORPHAN-RISK when basename is invisible to grep — deletion-prone.
       Scope: modes/, templates/, _shared/ only. SKILL.md/agent files themselves are covered
       by Check 32a (checks-skills.md). R2 is complementary, not overlapping.

  R3 — bin/ script reference integrity (reverse direction of Check 32d)
       Check 32d walks bin/ scripts and verifies each is referenced somewhere in .md files
       (orphaned-bin detection). R3 is the reverse: for every ${CLAUDE_PLUGIN_ROOT}/bin/<x>
       reference in any plugin .md file, verify the script actually exists locally.
       FAIL: script referenced but missing locally — skill dispatch fails immediately.
       WARN: script exists locally but absent from active installed cache — breaks after install.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/check_routing_links.py" [options]

Options:
    --plugins-dir DIR              Root dir containing plugin subdirs (default: plugins/)
    --cache-dir DIR                Plugin cache root (default: ~/.claude/plugins/cache/borda-ai-rig/)
    --installed-plugins-json PATH  Path to installed_plugins.json (default: ~/.claude/plugins/installed_plugins.json)
    --check R1,R2,R3               Comma-separated checks to run (default: R1,R2,R3)
    --timeout SECS                 Timeout for the git rev-parse subprocess (default: 30)

Output (stdout):
    One finding line per issue + hint line, or a pass line per check.

Exit codes:
    0   all checks pass
    1   one or more FAIL findings (R3 script referenced but missing locally)
    2   argument error
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class Severity(str, Enum):
    """Finding severity - only FAIL drives a non-zero exit code.

    Subclasses ``str`` rather than ``enum.StrEnum`` because ``requires-python`` is ``>=3.10``
    and ``StrEnum`` landed in 3.11. The ``str`` mixin keeps ``Severity.FAIL == "FAIL"`` true, so
    existing string comparisons and f-string interpolation keep working unchanged.

    Examples:
        >>> Severity.FAIL == "FAIL"
        True
        >>> f"R1-{Severity.WARN.value}"
        'R1-WARN'
        >>> Severity("INFO") is Severity.INFO
        True
    """

    FAIL = "FAIL"
    WARN = "WARN"
    INFO = "INFO"


class RefType(str, Enum):
    """How a path reference was expressed in the source file.

    Examples:
        >>> RefType.COMPUTED_REL == "computed_rel"
        True
    """

    COMPUTED_REL = "computed_rel"
    COMPUTED_ABS = "computed_abs"
    BIN_SCRIPT = "bin_script"
    HARDCODED = "hardcoded"


#: Default location of Claude Code's installed-plugins registry, used to resolve cached plugin paths.
_DEFAULT_INSTALLED_PLUGINS_JSON = Path.home() / ".claude" / "plugins" / "installed_plugins.json"

#: Guard against pathological inputs that would exhaust heap memory when read
#: in one shot. 10 MB is well above any realistic Markdown / agent file.
_MAX_FILE_SIZE = 10 * 1024 * 1024


def get_active_install_paths(installed_plugins_json: Path) -> dict[str, Path]:
    """Return active installPath per borda-ai-rig plugin from installed_plugins.json.

    Args:
        installed_plugins_json: Path to Claude Code's installed_plugins.json.

    Returns:
        Mapping of plugin name (e.g. "foundry") to its active installPath (Path).
        Empty dict when file absent or unreadable.

    Examples:
        >>> import tempfile, json
        >>> from pathlib import Path
        >>> with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        ...     _ = json.dump({"version": 2, "plugins": {
        ...         "foundry@borda-ai-rig": [{"installPath": "/cache/foundry/0.17.0", "version": "0.17.0"}]
        ...     }}, f)
        ...     name = f.name
        >>> result = get_active_install_paths(Path(name))
        >>> result["foundry"] == Path("/cache/foundry/0.17.0")
        True
    """
    if not installed_plugins_json.is_file():
        return {}
    try:
        data = json.loads(installed_plugins_json.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    active: dict[str, Path] = {}
    for key, entries in data.get("plugins", {}).items():
        if "@borda-ai-rig" not in key:
            continue
        plugin = key.split("@")[0]
        if not entries:
            continue
        install_path = entries[-1].get("installPath", "")
        if install_path:
            active[plugin] = Path(install_path)
    return active


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class PathRef:
    """A file-path reference extracted from a plugin .md file.

    Attributes:
        source_file: The .md file where the reference was found.
        plugin: Plugin owning the source_file (e.g. "foundry").
        raw_expr: The original expression text, e.g. ``$AUDIT_TPL/../modes/upgrade.md``.
        resolved_local: Best-effort local path relative to project root, e.g.
            ``plugins/cc_foundry/skills/audit/modes/upgrade.md``.
        target_basename: Final filename component, e.g. ``upgrade.md``.
        ref_type: Which RefType the reference was expressed as.
    """

    source_file: str
    plugin: str
    raw_expr: str
    resolved_local: str
    target_basename: str
    ref_type: RefType


@dataclass
class R1Finding:
    """R1 path-resolution finding.

    Attributes:
        severity: Severity.FAIL, Severity.WARN, or Severity.INFO.
        source_file: .md file containing the reference.
        raw_expr: Original expression text.
        resolved_local: Local path (may not exist).
        exists_locally: Whether file exists in plugins/<plugin>/...
        installed_versions: List of installed version directories checked.
        exists_installed: Whether file exists in any installed cache version.
        message: Human-readable description.
    """

    severity: Severity
    source_file: str
    raw_expr: str
    resolved_local: str
    exists_locally: bool
    installed_versions: list[str]
    exists_installed: bool
    message: str


@dataclass
class R2Finding:
    """R2 orphan-risk finding."""

    source_file: str
    plugin: str
    basename: str
    message: str


@dataclass
class R3Finding:
    """R3 bin/ script finding.

    Attributes:
        severity: Severity.FAIL or Severity.WARN.
        source_file: .md file containing the reference.
        script_name: Name of the referenced script, e.g. ``check_orphaned_bin.py``.
        plugin: Plugin owning the bin/ script, e.g. "foundry".
        local_path: Expected local path.
        exists_locally: Whether script exists locally.
        exists_installed: Whether script exists in any installed cache version.
        message: Human-readable description.
    """

    severity: Severity
    source_file: str
    script_name: str
    plugin: str
    local_path: str
    exists_locally: bool
    exists_installed: bool
    message: str


@dataclass
class CheckResults:
    """Aggregated results for all three checks."""

    r1: list[R1Finding] = field(default_factory=list)
    r2: list[R2Finding] = field(default_factory=list)
    r3: list[R3Finding] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Path extraction helpers
# ---------------------------------------------------------------------------

#: Pattern 1: $VAR/../<dir>/<file>.md  e.g. $AUDIT_TPL/../modes/upgrade.md
_COMPUTED_REL_RE = re.compile(r'"?\$\{?([A-Z_]+)\}?\s*/\.\./([^\s"\'`]+\.[a-zA-Z]{1,6})"?')

#: Pattern 2: $VAR/<file>.ext  e.g. Read "$_FS/task-hygiene.md"
_COMPUTED_ABS_RE = re.compile(r'"?\$\{?([A-Z_]+)\}?/([a-zA-Z0-9_.-]+\.[a-zA-Z]{1,6})"?')

#: Pattern 3: ${CLAUDE_PLUGIN_ROOT:-plugins/<x>}/bin/<script>  OR  ${CLAUDE_PLUGIN_ROOT}/bin/<script>
_BIN_SCRIPT_RE = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT(?::-plugins/([a-zA-Z0-9_-]+))?\}/bin/([a-zA-Z0-9_.-]+)")

#: Pattern 4: Read "$X/filename.ext" where X is a variable
_READ_VAR_RE = re.compile(r'(?:Read|read)\s+"?\$\{?([A-Z_]+)\}?/([a-zA-Z0-9_.-]+\.[a-zA-Z]{1,6})"?')

#: Pattern 5: hardcoded plugins/<plugin>/skills/<dir>/<file>.md
_HARDCODED_RE = re.compile(r'plugins/([a-zA-Z0-9_-]+)/[^\s"\'`]*?/([a-zA-Z0-9_.-]+\.md)\b')

#: Map known variable names to their resolution roots within the local plugin tree.
#: Value is (folder, path_within_plugin). The first element is the on-disk SOURCE
#: folder (cc_-prefixed after the folder rename) so resolved_local points at a
#: path that exists in the repo working tree. The marketplace plugin NAME used for
#: cache/registry lookup is derived separately via `_folder_to_name`.
_VAR_ROOTS: dict[str, tuple[str, str]] = {
    "AUDIT_TPL": ("cc_foundry", "skills/audit/templates"),
    "_FS": ("cc_foundry", "skills/_shared"),
    "_FOUNDRY_SHARED": ("cc_foundry", "skills/_shared"),
    "_SHARED": ("cc_foundry", "skills/_shared"),
    "_RESEARCH_SHARED": ("cc_research", "skills/_shared"),
    "_RESEARCH_RUN_MODES": ("cc_research", "skills/run/modes"),
    "_RESEARCH_AGENT_DIR": ("cc_research", "references/data-steward"),
}


def _folder_to_name(folder: str) -> str:
    """Map an on-disk plugin folder basename to its marketplace plugin name.

    Four source folders were renamed with a ``cc_`` prefix (``cc_foundry`` …)
    while their plugin names stayed bare (``foundry`` …). Cache dirs,
    ``installed_plugins.json`` keys, namespaces, and subprocess ``plugin`` args
    all use the bare name, so a folder-derived identity must strip the prefix to
    resolve against the installed plugin. Folders whose plugin name carries no
    ``cc_`` prefix (``codemap-py``, ``codex-rig``) pass through unchanged.

    Args:
        folder: On-disk folder basename, e.g. ``"cc_foundry"`` or ``"codemap-py"``.

    Returns:
        The bare plugin name, e.g. ``"foundry"`` or ``"codemap-py"``.

    Examples:
        >>> _folder_to_name("cc_foundry")
        'foundry'
        >>> _folder_to_name("codemap-py")
        'codemap-py'
    """
    return folder.removeprefix("cc_")


def _name_to_folder(plugins_dir: Path, name: str) -> Path | None:
    """Return the on-disk plugin folder for a bare plugin name, if it exists.

    Reverse of :func:`_folder_to_name` for local-existence checks: prefers the
    ``cc_<name>`` folder (post-rename) and falls back to the bare ``<name>``
    folder (non-renamed plugins, or test fixtures that build a bare tree).

    Args:
        plugins_dir: Root directory containing plugin folders.
        name: Bare plugin name, e.g. ``"foundry"``.

    Returns:
        The existing folder ``Path`` (``cc_<name>`` preferred), or ``None`` when
        neither form exists on disk.

    Examples:
        >>> import tempfile
        >>> from pathlib import Path
        >>> with tempfile.TemporaryDirectory() as d:
        ...     root = Path(d)
        ...     (root / "cc_foundry").mkdir()
        ...     _name_to_folder(root, "foundry").name
        'cc_foundry'
    """
    prefixed = plugins_dir / f"cc_{name}"
    if prefixed.is_dir():
        return prefixed
    bare = plugins_dir / name
    if bare.is_dir():
        return bare
    return None


def _resolve_computed_rel(var: str, rel_path: str, plugins_dir: Path) -> str | None:
    """Resolve $VAR/../<rel_path> to a local filesystem path.

    Args:
        var: Variable name without $ or braces, e.g. "AUDIT_TPL".
        rel_path: Path component after ../, e.g. "modes/upgrade.md".
        plugins_dir: Root of plugins directory.

    Returns:
        Resolved relative path string (from project root) or None if var unknown.

    Examples:
        >>> from pathlib import Path
        >>> _resolve_computed_rel("AUDIT_TPL", "modes/upgrade.md", Path("plugins"))
        'plugins/cc_foundry/skills/audit/modes/upgrade.md'
        >>> _resolve_computed_rel("UNKNOWN_VAR", "foo.md", Path("plugins")) is None
        True
    """
    if var not in _VAR_ROOTS:
        return None
    plugin, root_rel = _VAR_ROOTS[var]
    # Navigate one level up from root, then append rel_path
    root = Path(root_rel)
    resolved = plugins_dir / plugin / root.parent / rel_path
    return resolved.as_posix()


def _resolve_computed_abs(var: str, filename: str, plugins_dir: Path) -> str | None:
    """Resolve $VAR/<filename> to a local filesystem path.

    Args:
        var: Variable name without $ or braces, e.g. "_FS".
        filename: Filename component, e.g. "task-hygiene.md".
        plugins_dir: Root of plugins directory.

    Returns:
        Resolved relative path string or None if var unknown.

    Examples:
        >>> from pathlib import Path
        >>> _resolve_computed_abs("_FS", "task-hygiene.md", Path("plugins"))
        'plugins/cc_foundry/skills/_shared/task-hygiene.md'
        >>> _resolve_computed_abs("UNKNOWN", "foo.md", Path("plugins")) is None
        True
    """
    if var not in _VAR_ROOTS:
        return None
    plugin, root_rel = _VAR_ROOTS[var]
    resolved = plugins_dir / plugin / root_rel / filename
    return resolved.as_posix()


def extract_path_refs(md_file: Path, plugin: str, plugins_dir: Path) -> list[PathRef]:
    """Extract all computed path references from a single .md file.

    Applies all five patterns. Deduplicates by (raw_expr, ref_type).

    Args:
        md_file: Path to the .md file to scan.
        plugin: Plugin name owning this file.
        plugins_dir: Root of plugins directory.

    Returns:
        List of PathRef objects for each unique path reference found.
    """
    try:
        if md_file.stat().st_size > _MAX_FILE_SIZE:
            return []
        text = md_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    refs: list[PathRef] = []
    seen: set[tuple[str, str]] = set()

    def _add(raw: str, resolved: str | None, basename: str, ref_type: RefType) -> None:
        # Dedup by resolved path + type so patterns 2 and 4 don't double-count the same target.
        key = (resolved or raw, ref_type)
        if key in seen or resolved is None:
            return
        seen.add(key)
        refs.append(
            PathRef(
                source_file=str(md_file),
                plugin=plugin,
                raw_expr=raw,
                resolved_local=resolved,
                target_basename=basename,
                ref_type=ref_type,
            )
        )

    # Pattern 1: $VAR/../dir/file
    for m in _COMPUTED_REL_RE.finditer(text):
        var, rel = m.group(1), m.group(2)
        resolved = _resolve_computed_rel(var, rel, plugins_dir)
        basename = Path(rel).name
        _add(m.group(0).strip('"'), resolved, basename, RefType.COMPUTED_REL)

    # Pattern 2: $VAR/file (but skip bin/ scripts — covered by R3)
    for m in _COMPUTED_ABS_RE.finditer(text):
        var, fname = m.group(1), m.group(2)
        if var in _VAR_ROOTS:
            resolved = _resolve_computed_abs(var, fname, plugins_dir)
            _add(m.group(0).strip('"'), resolved, fname, RefType.COMPUTED_ABS)

    # Pattern 4: Read "$VAR/file" (explicit Read calls — same resolution as pattern 2)
    for m in _READ_VAR_RE.finditer(text):
        var, fname = m.group(1), m.group(2)
        if var in _VAR_ROOTS:
            resolved = _resolve_computed_abs(var, fname, plugins_dir)
            _add(m.group(0).strip('"'), resolved, fname, RefType.COMPUTED_ABS)

    # Pattern 5: hardcoded plugins/<plugin>/... paths
    for m in _HARDCODED_RE.finditer(text):
        # Reconstruct full path from match
        raw = m.group(0)
        fname = m.group(2)
        # Full path already embedded in the match
        _add(raw, raw, fname, RefType.HARDCODED)

    return refs


def extract_bin_refs(md_file: Path, plugin: str) -> list[tuple[str, str, str, bool]]:
    """Extract bin/ script references from a .md file.

    Args:
        md_file: Path to the .md file.
        plugin: Plugin owning the .md file.

    Returns:
        List of (source_file, bin_plugin, script_name, explicit_plugin) tuples where
        explicit_plugin is True when the plugin was stated via the ``:-plugins/<x>`` fallback
        form and False when inferred from the owning plugin (bare ``${CLAUDE_PLUGIN_ROOT}``).

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     f = Path(d) / "SKILL.md"
        ...     _ = f.write_text('run "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/foo.py"')
        ...     result = extract_bin_refs(f, "foundry")
        ...     result[0][1], result[0][2], result[0][3]
        ('foundry', 'foo.py', True)
        >>> with tempfile.TemporaryDirectory() as d:
        ...     f = Path(d) / "SKILL.md"
        ...     _ = f.write_text('python "${CLAUDE_PLUGIN_ROOT}/bin/health_sentinel.py"')
        ...     result = extract_bin_refs(f, "foundry")
        ...     result[0][1], result[0][2], result[0][3]
        ('foundry', 'health_sentinel.py', False)
    """
    try:
        if md_file.stat().st_size > _MAX_FILE_SIZE:
            return []
        text = md_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    results: list[tuple[str, str, str, bool]] = []
    seen: set[tuple[str, str]] = set()
    for m in _BIN_SCRIPT_RE.finditer(text):
        explicit_plugin = m.group(1) is not None
        # Both the :-fallback folder (e.g. plugins/cc_develop) and the owning
        # plugin folder are cc_-prefixed on disk; normalise to the bare plugin
        # NAME used by cache/registry/subprocess lookups.
        bin_plugin = _folder_to_name(m.group(1) or plugin)  # no :-fallback → infer from owner
        script = m.group(2)
        key = (bin_plugin, script)
        if key not in seen:
            seen.add(key)
            results.append((str(md_file), bin_plugin, script, explicit_plugin))
    return results


# ---------------------------------------------------------------------------
# Installed cache helpers
# ---------------------------------------------------------------------------


def get_installed_versions(cache_dir: Path, plugin: str) -> list[Path]:
    """Return all version directories for a plugin in the installed cache.

    Args:
        cache_dir: Root cache directory, e.g. ~/.claude/plugins/cache/borda-ai-rig/
        plugin: Plugin name, e.g. "foundry".

    Returns:
        Sorted list of version directory paths (newest first by name).
    """
    plugin_cache = cache_dir / plugin
    if not plugin_cache.is_dir():
        return []
    versions = sorted(
        [v for v in plugin_cache.iterdir() if v.is_dir()],
        key=lambda p: p.name,
        reverse=True,
    )
    return versions


def find_in_installed(
    local_path: str,
    plugin: str,
    cache_dir: Path,
    active_install_paths: dict[str, Path] | None = None,
) -> tuple[bool, list[str]]:
    """Check whether a local path exists in the active installed cache for the plugin.

    Prefers the active install path from ``active_install_paths`` (read from
    ``installed_plugins.json``) over scanning all version directories, which avoids
    false-negatives from historical versions that predate the file.

    Args:
        local_path: Full local path string — absolute or relative
            (e.g. ``plugins/cc_foundry/skills/audit/modes/upgrade.md``).
        plugin: Plugin name, e.g. ``"foundry"``.
        cache_dir: Root cache directory, e.g. ``~/.claude/plugins/cache/borda-ai-rig/``.
            Used as fallback when ``active_install_paths`` is absent.
        active_install_paths: Optional mapping of plugin → active installPath from
            ``installed_plugins.json``. When provided, only the active version is checked.

    Returns:
        Tuple of (found, list_of_checked_paths).

    Examples:
        >>> import tempfile
        >>> from pathlib import Path
        >>> with tempfile.TemporaryDirectory() as d:
        ...     install = Path(d) / "foundry" / "0.17.0"
        ...     (install / "skills" / "audit" / "modes").mkdir(parents=True)
        ...     _ = (install / "skills" / "audit" / "modes" / "upgrade.md").write_text("x")
        ...     found, _ = find_in_installed(
        ...         "plugins/cc_foundry/skills/audit/modes/upgrade.md", "foundry",
        ...         Path(d), {"foundry": install},
        ...     )
        ...     found
        True
    """
    # Derive path relative to plugin root from local_path. The local path may
    # carry the on-disk cc_-prefixed folder (plugins/cc_foundry/...) while
    # `plugin` is the bare NAME used by the installed cache — match either form.
    folder_segments = {plugin, f"cc_{plugin}"}
    parts = Path(local_path).parts
    rel: str | None = None
    for i, part in enumerate(parts):
        if part in folder_segments and i + 1 < len(parts):
            rel = str(Path(*parts[i + 1 :]))
            break
    if rel is None:
        for prefix in (f"plugins/{plugin}/", f"plugins/cc_{plugin}/"):
            if local_path.startswith(prefix):
                rel = local_path[len(prefix) :]
                break
        if rel is None:
            return False, []

    # Prefer active install path from installed_plugins.json.
    if active_install_paths and plugin in active_install_paths:
        candidate = active_install_paths[plugin] / rel
        return candidate.exists(), [str(candidate)]

    # Fallback: check newest version dir in cache.
    versions = get_installed_versions(cache_dir, plugin)
    if not versions:
        return False, []
    checked: list[str] = []
    for version_dir in versions:
        candidate = version_dir / rel
        checked.append(str(candidate))
        if candidate.exists():
            return True, checked
    return False, checked


# ---------------------------------------------------------------------------
# Check R1
# ---------------------------------------------------------------------------


def _resolve_target_plugin(ref: PathRef) -> str:
    """Name the plugin a reference points *into*, which is not always its source plugin.

    For cross-plugin refs, the target plugin is embedded in the resolved path (e.g.
    ``plugins/cc_foundry/skills/_shared/foo.md`` → ``"foundry"``), not the source plugin. ``ref.resolved_local`` is
    always relative to the project root (PathRef contract) while the caller's ``plugins_dir`` is always absolute —
    ``relative_to()`` between a relative and an absolute path always raises ValueError, so that comparison silently fell
    back to ``ref.plugin`` (the SOURCE plugin) for every cross-plugin reference, masking real cache-drift findings under
    the wrong plugin's cache. Locate the ``"plugins"`` segment directly instead — works regardless of which path form
    either side is in. The path segment is the ``cc_``-prefixed folder; strip it for the installed/cache side
    (``active_install_paths`` keys, cache dirs, :func:`find_in_installed`), while the caller's local-existence test uses
    the ``cc_`` folder form.
    """
    resolved_parts = Path(ref.resolved_local).parts
    try:
        target_folder = resolved_parts[resolved_parts.index("plugins") + 1]
    except (ValueError, IndexError):
        target_folder = ref.plugin
    return _folder_to_name(target_folder)


def _check_ref_installed_state(
    ref: PathRef,
    target_plugin: str,
    cache_dir: Path,
    active_install_paths: dict[str, Path] | None,
) -> R1Finding | None:
    """Compare one reference's local and installed resolution, if they disagree.

    Returns ``None`` when local and installed agree — both present, or both absent behind an unknown variable — which is
    the no-finding case.
    """
    local_exists = Path(ref.resolved_local).exists()
    if active_install_paths is not None:
        not_installed = target_plugin not in active_install_paths
    else:
        not_installed = len(get_installed_versions(cache_dir, target_plugin)) == 0

    if not_installed:
        # Cannot verify installed state — info only, not a failure
        return R1Finding(
            severity=Severity.INFO,
            source_file=ref.source_file,
            raw_expr=ref.raw_expr,
            resolved_local=ref.resolved_local,
            exists_locally=local_exists,
            installed_versions=[],
            exists_installed=False,
            message=(
                f"R1-INFO: {ref.source_file} — `{ref.raw_expr}` "
                f"→ plugin '{target_plugin}' not installed; cannot verify installed state"
            ),
        )

    installed_found, checked_paths = find_in_installed(
        ref.resolved_local, target_plugin, cache_dir, active_install_paths
    )

    if local_exists and not installed_found:
        # WARN, not FAIL: the installed cache is machine-local mutable state whose
        # contents depend on when the user last synced and on what has been pushed to
        # the marketplace remote. A newly added shared file is legitimately local-only
        # until its commit ships, so failing here would block the very commit that
        # releases the file (commit -> push -> sync is the only way to populate the
        # cache). Pre-commit must assert facts about the source tree, which is what the
        # local-existence checks and R3 do; cache drift is advisory only.
        return R1Finding(
            severity=Severity.WARN,
            source_file=ref.source_file,
            raw_expr=ref.raw_expr,
            resolved_local=ref.resolved_local,
            exists_locally=True,
            installed_versions=checked_paths,
            exists_installed=False,
            message=(
                f"R1-WARN: {ref.source_file} — `{ref.raw_expr}` "
                f"resolves to `{ref.resolved_local}` (exists locally) "
                f"but absent from installed cache — users on the released version lack it\n"
                f"  fix: none needed if the file is newly added and not yet released; "
                f"otherwise verify it is included in the plugin manifest"
            ),
        )
    if not local_exists and installed_found:
        return R1Finding(
            severity=Severity.WARN,
            source_file=ref.source_file,
            raw_expr=ref.raw_expr,
            resolved_local=ref.resolved_local,
            exists_locally=False,
            installed_versions=checked_paths,
            exists_installed=True,
            message=(
                f"R1-WARN: {ref.source_file} — `{ref.raw_expr}` "
                f"resolves to `{ref.resolved_local}` (missing locally) "
                f"but present in installed cache — stale install; will break after plugin update\n"
                f"  fix: restore file locally or remove the reference"
            ),
        )
    return None


def run_computed_path_duality(
    plugins_dir: Path,
    cache_dir: Path,
    active_install_paths: dict[str, Path] | None = None,
) -> list[R1Finding]:
    """Run Check R1 — computed path resolution (local + installed duality).

    Args:
        plugins_dir: Root of plugin directories.
        cache_dir: Root of installed plugin cache (fallback when active_install_paths absent).
        active_install_paths: Active install paths from installed_plugins.json; when provided,
            only the active version is checked (avoids false-negatives from historical versions).

    Returns:
        List of R1Finding objects (empty when all references resolve correctly).
    """
    findings: list[R1Finding] = []

    for plugin_dir in sorted(plugins_dir.iterdir()):
        if not plugin_dir.is_dir():
            continue
        plugin = plugin_dir.name

        # Collect all .md files: skills and agents
        md_files: list[Path] = []
        for pattern in ["skills/*/SKILL.md", "skills/*/modes/*.md", "skills/*/templates/*", "agents/*.md"]:
            md_files.extend(plugin_dir.glob(pattern))

        for md_file in sorted(md_files):
            for ref in extract_path_refs(md_file, plugin, plugins_dir):
                finding = _check_ref_installed_state(ref, _resolve_target_plugin(ref), cache_dir, active_install_paths)
                if finding is not None:
                    findings.append(finding)

    return findings


# ---------------------------------------------------------------------------
# Check R2
# ---------------------------------------------------------------------------


def collect_all_md_basenames_in_skill_dirs(plugin_dir: Path) -> dict[str, list[str]]:
    """Return a map of basename → [absolute_path] for all .md files in skill subdirs and agents/.

    Args:
        plugin_dir: Plugin root directory.

    Returns:
        Dict mapping filename basename to list of full path strings.
    """
    result: dict[str, list[str]] = {}
    for md_file in plugin_dir.glob("skills/**/*.md"):
        bn = md_file.name
        result.setdefault(bn, []).append(str(md_file))
    for md_file in plugin_dir.glob("agents/*.md"):
        bn = md_file.name
        result.setdefault(bn, []).append(str(md_file))
    return result


def is_basename_grep_visible(basename: str, plugin_dir: Path) -> bool:
    """Return True if basename appears as a literal string in any SKILL.md or agent .md in the plugin.

    Args:
        basename: Filename to search for, e.g. "upgrade.md".
        plugin_dir: Plugin root directory to walk.

    Returns:
        True if literal basename appears in at least one consumer .md file.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     p = Path(d)
        ...     (p / "skills" / "audit").mkdir(parents=True)
        ...     _ = (p / "skills" / "audit" / "SKILL.md").write_text("Read audit-modes/upgrade.md here")
        ...     is_basename_grep_visible("upgrade.md", p)
        True
        >>> with tempfile.TemporaryDirectory() as d:
        ...     p = Path(d)
        ...     (p / "skills" / "audit").mkdir(parents=True)
        ...     _ = (p / "skills" / "audit" / "SKILL.md").write_text("$AUDIT_TPL/../modes/upgrade.md")
        ...     is_basename_grep_visible("upgrade.md", p)
        True
    """
    for md_file in plugin_dir.rglob("*.md"):
        try:
            depth = len(md_file.relative_to(plugin_dir).parts)
        except ValueError:
            depth = 0
        if depth > 10:
            continue
        try:
            if md_file.stat().st_size > _MAX_FILE_SIZE:
                continue
            text = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if basename in text:
            return True
    return False


def run_orphan_risk_detection(plugins_dir: Path) -> list[R2Finding]:
    """Run Check R2 — grep-visible referencing (orphan-risk detection).

    Scans mode files, template files, and _shared/ files. Any file whose basename
    does not appear as a literal string anywhere in the plugin's .md files is
    ORPHAN-RISK: invisible to grep-based dead-file detection.

    Args:
        plugins_dir: Root of plugin directories.

    Returns:
        List of R2Finding objects.
    """
    findings: list[R2Finding] = []

    for plugin_dir in sorted(plugins_dir.iterdir()):
        if not plugin_dir.is_dir():
            continue
        plugin = plugin_dir.name

        # Candidate files: modes/, templates/, _shared/ — the indirect-load dirs
        candidate_files: list[Path] = []
        for pattern in [
            "skills/*/modes/*.md",
            "skills/*/templates/*.md",
            "skills/_shared/*.md",
            "skills/_shared/*.json",
        ]:
            candidate_files.extend(plugin_dir.glob(pattern))

        for candidate in sorted(candidate_files):
            basename = candidate.name
            if not is_basename_grep_visible(basename, plugin_dir):
                findings.append(
                    R2Finding(
                        source_file=str(candidate),
                        plugin=plugin,
                        basename=basename,
                        message=(
                            f"R2-ORPHAN-RISK: {candidate} — basename `{basename}` "
                            f"not grep-visible as literal string in any {plugin} plugin .md file\n"
                            f"  detail: file is likely loaded only via computed path (e.g. $AUDIT_TPL/../modes/{basename}); "
                            f"a grep-based dead-file scan will find zero references and flag it as orphaned\n"
                            f"  fix: add a comment `# loads: {basename}` in the consumer SKILL.md, "
                            f"or ensure the filename appears as a literal string somewhere in the skill"
                        ),
                    )
                )

    return findings


# ---------------------------------------------------------------------------
# Check R3
# ---------------------------------------------------------------------------


def _resolve_bin_ref_local(bin_plugin: str, script_name: str, plugins_dir: Path) -> Path | None:
    """Locate where a referenced bin/ script would live, or ``None`` when it is not real dispatch.

    A reference into a plugin dir that does not exist is an illustrative placeholder (e.g.
    ``plugins/myplugin/bin/resolve.py`` in an authoring guide), not real dispatch — a "missing bin" failure is
    meaningless for a non-plugin. ``bin_plugin`` is the bare NAME; the on-disk folder is ``cc_``-prefixed.
    """
    plugin_folder = _name_to_folder(plugins_dir, bin_plugin)
    if plugin_folder is None:
        return None
    return plugin_folder / "bin" / script_name


def _build_r3_finding(
    source_file: str,
    bin_plugin: str,
    script_name: str,
    *,
    explicit_plugin: bool,
    local_path: Path,
    cache_dir: Path,
    active_install_paths: dict[str, Path] | None,
) -> R3Finding | None:
    """Judge one bin/ reference, returning ``None`` when it resolves everywhere checkable."""
    if not local_path.exists():
        # Bare ${CLAUDE_PLUGIN_ROOT}/bin/ form (no :-plugin fallback) is used in
        # documentation guides as a generic placeholder — downgrade to WARN to avoid
        # false failures on example snippets.  Explicit :-form refs are real dispatch.
        severity = Severity.FAIL if explicit_plugin else Severity.WARN
        label = "R3-FAIL" if explicit_plugin else "R3-WARN"
        return R3Finding(
            severity=severity,
            source_file=source_file,
            script_name=script_name,
            plugin=bin_plugin,
            local_path=str(local_path),
            exists_locally=False,
            exists_installed=False,
            message=(
                f"{label}: {source_file} — references `{script_name}` in "
                f"{bin_plugin}/bin/ but file is missing locally: {local_path}\n"
                f"  fix: create {local_path} or remove the reference"
            ),
        )

    if active_install_paths is not None:
        not_installed = bin_plugin not in active_install_paths
    else:
        not_installed = len(get_installed_versions(cache_dir, bin_plugin)) == 0
    if not_installed:
        # Can't check installed state
        return None

    installed_rel = f"plugins/{bin_plugin}/bin/{script_name}"
    installed_found, _ = find_in_installed(installed_rel, bin_plugin, cache_dir, active_install_paths)
    if installed_found:
        return None
    return R3Finding(
        severity=Severity.WARN,
        source_file=source_file,
        script_name=script_name,
        plugin=bin_plugin,
        local_path=str(local_path),
        exists_locally=True,
        exists_installed=False,
        message=(
            f"R3-WARN: {source_file} — references `{script_name}` in "
            f"{bin_plugin}/bin/ (exists locally) but absent from installed cache\n"
            f"  detail: users running installed plugin version will get broken dispatch\n"
            f"  fix: run `claude plugin install {bin_plugin}@borda-ai-rig` to sync"
        ),
    )


def run_bin_ref_integrity(
    plugins_dir: Path,
    cache_dir: Path,
    active_install_paths: dict[str, Path] | None = None,
) -> list[R3Finding]:
    """Run Check R3 — bin/ script reference integrity (reverse direction of Check 32d).

    Check 32d walks bin/ scripts and flags those with no .md reference (orphaned scripts).
    R3 is the reverse: for every ${CLAUDE_PLUGIN_ROOT}/bin/<x> reference in any plugin .md
    file, verify the script actually exists locally. Also checks the active installed cache.

    Args:
        plugins_dir: Root of plugin directories.
        cache_dir: Root of installed plugin cache (fallback when active_install_paths absent).
        active_install_paths: Active install paths from installed_plugins.json.

    Returns:
        List of R3Finding objects.
    """
    findings: list[R3Finding] = []
    seen: set[tuple[str, str]] = set()  # (bin_plugin, script_name)

    for plugin_dir in sorted(plugins_dir.iterdir()):
        if not plugin_dir.is_dir():
            continue
        plugin = plugin_dir.name

        for md_file in sorted(plugin_dir.rglob("*.md")):
            try:
                depth = len(md_file.relative_to(plugin_dir).parts)
            except ValueError:
                depth = 0
            if depth > 10:
                continue
            for source_file, bin_plugin, script_name, explicit_plugin in extract_bin_refs(md_file, plugin):
                key = (bin_plugin, script_name)
                if key in seen:
                    continue
                seen.add(key)

                local_path = _resolve_bin_ref_local(bin_plugin, script_name, plugins_dir)
                if local_path is None:
                    continue

                finding = _build_r3_finding(
                    source_file,
                    bin_plugin,
                    script_name,
                    explicit_plugin=explicit_plugin,
                    local_path=local_path,
                    cache_dir=cache_dir,
                    active_install_paths=active_install_paths,
                )
                if finding is not None:
                    findings.append(finding)

    return findings


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def _format_r1_section(results: CheckResults) -> tuple[list[str], int]:
    """Render the R1 computed-path block, and whether it failed."""
    fails = [f for f in results.r1 if f.severity == Severity.FAIL]
    warns = [f for f in results.r1 if f.severity == Severity.WARN]
    infos = [f for f in results.r1 if f.severity == Severity.INFO]
    lines = ["=== Check R1: Computed path resolution (local + installed duality) ==="]
    if fails or warns:
        lines += [f.message for f in fails + warns]
    else:
        lines.append("✓: Check R1 — all computed path references resolve correctly at local + installed")
    if infos:
        lines.append(f"  ({len(infos)} reference(s) skipped — plugin not installed locally)")
    return lines, 1 if fails else 0


def _format_r2_section(results: CheckResults) -> list[str]:
    """Render the R2 orphan-risk block.

    R2 is a structural safety warning, not a hard failure, so it contributes no exit code.
    """
    lines = ["=== Check R2: Grep-visible referencing (orphan-risk detection) ==="]
    if results.r2:
        return lines + [f.message for f in results.r2]
    lines.append("✓: Check R2 — all indirect-load .md files have grep-visible basename references")
    return lines


def _format_r3_section(results: CheckResults) -> tuple[list[str], int]:
    """Render the R3 bin/-existence block, and whether it failed."""
    fails = [f for f in results.r3 if f.severity == Severity.FAIL]
    warns = [f for f in results.r3 if f.severity == Severity.WARN]
    lines = ["=== Check R3: bin/ script existence (local + installed) ==="]
    if fails or warns:
        lines += [f.message for f in fails + warns]
    else:
        lines.append("✓: Check R3 — all bin/ script references resolve at local + installed")
    return lines, 1 if fails else 0


def format_results(results: CheckResults, active_checks: set[str]) -> tuple[str, int]:
    """Format all check results into a printable report string.

    Args:
        results: Aggregated CheckResults from all three checks.
        active_checks: Set of check codes to include (e.g. {"R1", "R2", "R3"}).

    Returns:
        Tuple of (report_string, exit_code) where exit_code is 0 (pass) or 1 (failures).
    """
    lines: list[str] = []
    exit_code = 0

    if "R1" in active_checks:
        section, failed = _format_r1_section(results)
        lines += section
        exit_code |= failed

    if "R2" in active_checks:
        lines += _format_r2_section(results)

    if "R3" in active_checks:
        section, failed = _format_r3_section(results)
        lines += section
        exit_code |= failed

    # Summary counts
    total_fail = sum(1 for f in results.r1 if f.severity == Severity.FAIL) + len(
        [f for f in results.r3 if f.severity == Severity.FAIL]
    )
    total_warn = sum(1 for f in results.r1 if f.severity == Severity.WARN) + len(
        [f for f in results.r3 if f.severity == Severity.WARN]
    )
    total_orphan = len(results.r2)
    if total_fail or total_warn or total_orphan:
        lines.append(f"\nSummary: {total_fail} FAIL  {total_warn} WARN  {total_orphan} ORPHAN-RISK")

    return "\n".join(lines), exit_code


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Check routed script references for invalid targets."""
    parser = argparse.ArgumentParser(
        prog="check_routing_links",
        description="Validate computed file-path references in plugin SKILL.md and agent .md files.",
    )
    parser.add_argument(
        "--plugins-dir",
        default="plugins",
        metavar="DIR",
        help="Root dir containing plugin subdirs (default: plugins/).",
    )
    parser.add_argument(
        "--cache-dir",
        default=str(Path.home() / ".claude" / "plugins" / "cache" / "borda-ai-rig"),
        metavar="DIR",
        help="Plugin cache root (default: ~/.claude/plugins/cache/borda-ai-rig/).",
    )
    parser.add_argument(
        "--installed-plugins-json",
        default=str(_DEFAULT_INSTALLED_PLUGINS_JSON),
        metavar="PATH",
        help="Path to installed_plugins.json (default: ~/.claude/plugins/installed_plugins.json).",
    )
    parser.add_argument(
        "--check",
        default="R1,R2,R3",
        metavar="CHECKS",
        help="Comma-separated checks to run: R1, R2, R3 (default: R1,R2,R3).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        metavar="SECS",
        help="Timeout in seconds for the git rev-parse subprocess (default: 30).",
    )
    args = parser.parse_args(argv)

    # Normalise argv-supplied paths so ``..`` components cannot escape the
    # project tree. ``--plugins-dir`` must stay inside the project root.
    # ``--cache-dir`` is allowed to point anywhere under ``~/.claude``
    # (the default lives there) — it just must not contain unresolved traversal.
    plugins_dir = Path(args.plugins_dir).resolve()
    cache_dir = Path(args.cache_dir).resolve()
    try:
        import subprocess as _sp

        _r = _sp.run(
            ["git", "rev-parse", "--show-toplevel"],  # noqa: S607 - git resolved via PATH on purpose
            capture_output=True,
            text=True,
            cwd=str(plugins_dir),
            timeout=args.timeout,
        )
        project_root = Path(_r.stdout.strip()).resolve() if _r.returncode == 0 else Path.cwd().resolve()
    except Exception:
        project_root = Path.cwd().resolve()
    try:
        plugins_dir.relative_to(project_root)
    except ValueError:
        print(
            f"! SECURITY: --plugins-dir must be within project root: {args.plugins_dir}",
            file=sys.stderr,
        )
        return 2
    active_checks = {c.strip().upper() for c in args.check.split(",")}

    if not plugins_dir.is_dir():
        print(f"error: {args.plugins_dir!r} is not a directory", file=sys.stderr)
        return 2

    invalid = active_checks - {"R1", "R2", "R3"}
    if invalid:
        print(f"error: unknown check(s): {', '.join(sorted(invalid))}", file=sys.stderr)
        return 2

    active_install_paths = get_active_install_paths(Path(args.installed_plugins_json))

    results = CheckResults()

    if "R1" in active_checks:
        results.r1 = run_computed_path_duality(plugins_dir, cache_dir, active_install_paths)

    if "R2" in active_checks:
        results.r2 = run_orphan_risk_detection(plugins_dir)

    if "R3" in active_checks:
        results.r3 = run_bin_ref_integrity(plugins_dir, cache_dir, active_install_paths)

    report, exit_code = format_results(results, active_checks)
    print(report)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
