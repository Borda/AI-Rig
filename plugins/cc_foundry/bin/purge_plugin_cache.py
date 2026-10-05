#!/usr/bin/env python
"""purge_plugin_cache.py — reclaim orphaned plugin cache versions under ~/.claude/plugins/cache.

Claude Code keeps every previously-installed plugin version in its cache and marks
superseded ones with an ``.orphaned_at`` sentinel (epoch milliseconds). Nothing
ever removes them, so the cache grows without bound — and a plugin renamed or
uninstalled (e.g. ``codemap`` → ``codemap-py``) leaves an entire tree that no
future install will ever revisit.

Two modes:

* **report** (default) — print one line per purge candidate and a total. Deletes
  nothing, exits 0. This is what ``/foundry:setup`` shows the user before asking.
* ``--apply`` — delete the candidate dirs with ``shutil.rmtree``.

``.in_use`` is deliberately NOT used as a safety signal: it is a directory of
per-PID lease files that Claude Code never clears, so it is present on every
version dir including long-dead ones. Lease counts are reported as information
only. The real guard is ``--min-orphan-age-hours``, which keeps a just-superseded
version around while a live session may still be executing from it.

Deletion is irreversible, so every candidate must clear ALL of these:

1. real directory, not a symlink;
2. contains ``.claude-plugin/plugin.json`` (it is genuinely a version dir);
3. carries a parseable ``.orphaned_at`` (unparsable → skipped, fail-safe);
4. orphaned at least ``--min-orphan-age-hours`` ago (default 24);
5. is not the ``installPath`` recorded in Claude Code's plugin registry;
6. is not listed in ``--protect`` (pass ``$CLAUDE_PLUGIN_ROOT`` and the caller's
   own plugin root so a run can never delete the code executing it);
7. is not the newest-by-version dir of a plugin that appears in the registry.
   Plugins absent from the registry have no live consumer, so all their versions
   including the newest are reclaimable — this is what frees a renamed plugin.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/purge_plugin_cache.py" [--cache-dir PATH] [--marketplace NAME]
        [--registry PATH] [--protect PATH ...] [--min-orphan-age-hours N] [--apply] [--expect-count N]

Exit codes:
    0   success (report printed, or apply completed)
    1   irrecoverable error (cache dir missing, ``--expect-count`` mismatch)
    2   argument error (bad marketplace token, negative age)
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_MS_PER_HOUR = 3_600_000

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from get_plugin_install_path import resolve_install_path


@dataclass(frozen=True)
class Candidate:
    """One cache version directory eligible for deletion.

    Attributes:
        path: Absolute path to the version directory.
        plugin: Plugin short-name (parent directory name).
        version: Version directory name.
        orphaned_at_ms: Epoch-millisecond stamp read from ``.orphaned_at``.
        size_bytes: Recursive size of the directory.
        leases: Number of entries under ``.in_use`` (informational only).
    """

    path: Path
    plugin: str
    version: str
    orphaned_at_ms: int
    size_bytes: int
    leases: int


@dataclass(frozen=True)
class PurgeContext:
    """Immutable inputs shared by every :func:`classify` call.

    Attributes:
        registry: Path to Claude Code's ``installed_plugins.json``.
        marketplace: Marketplace short-name owning the scanned subtree.
        protected: Resolved paths that must never be deleted.
        min_age_ms: Minimum orphan age, in milliseconds.
        now_ms: Current time in epoch milliseconds.
        newest_registered: Plugin → newest version dir name, for plugins present
            in the registry. Plugins absent from the registry are absent here too.
    """

    registry: Path
    marketplace: str
    protected: frozenset[Path]
    min_age_ms: int
    now_ms: int
    newest_registered: dict[str, str]


def version_key(name: str) -> tuple[int, ...]:
    """Convert a version directory name into a numerically sortable key.

    Non-numeric segments sort as ``-1`` so they never outrank a real release.

    Args:
        name: Version directory name, e.g. ``0.38.4``.

    Returns:
        Tuple of integers suitable for ``max``/``sorted``.

    Examples:
        >>> version_key("0.38.4")
        (0, 38, 4)
        >>> sorted(["0.9.0", "0.10.0", "0.10.2"], key=version_key)[-1]
        '0.10.2'
        >>> version_key("unknown")
        (-1,)
    """
    return tuple(int(part) if part.isdigit() else -1 for part in name.split("."))


def age_hours(orphaned_at_ms: int, now_ms: int) -> float:
    """Return how many hours ago a version was orphaned.

    Args:
        orphaned_at_ms: Epoch-millisecond stamp from ``.orphaned_at``.
        now_ms: Current epoch milliseconds.

    Returns:
        Age in hours; negative when the stamp is in the future (clock skew).

    Examples:
        >>> age_hours(0, 3_600_000)
        1.0
        >>> age_hours(3_600_000, 3_600_000)
        0.0
    """
    return (now_ms - orphaned_at_ms) / _MS_PER_HOUR


def read_orphan_stamp(version_dir: Path) -> int | None:
    """Read and parse the ``.orphaned_at`` sentinel.

    Args:
        version_dir: Candidate version directory.

    Returns:
        Epoch-millisecond stamp, or ``None`` when the marker is absent,
        unreadable, or not an integer (treated as not-orphaned, fail-safe).
    """
    marker = version_dir / ".orphaned_at"
    try:
        raw = marker.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def dir_size(path: Path) -> int:
    """Sum the size of every regular file under *path* (symlinks not followed).

    Args:
        path: Directory to measure.

    Returns:
        Total size in bytes; unreadable entries contribute 0.
    """
    total = 0
    for entry in path.rglob("*"):
        if entry.is_symlink() or not entry.is_file():
            continue
        try:
            total += entry.stat().st_size
        except OSError:
            continue
    return total


def count_leases(version_dir: Path) -> int:
    """Count per-PID lease files under ``.in_use`` (informational only).

    Args:
        version_dir: Candidate version directory.

    Returns:
        Number of entries, or 0 when ``.in_use`` is absent or unreadable.
    """
    in_use = version_dir / ".in_use"
    if not in_use.is_dir():
        return 0
    try:
        return len(list(in_use.iterdir()))
    except OSError:
        return 0


def iter_version_dirs(cache_dir: Path, marketplace: str) -> list[Path]:
    """List every ``<cache>/<marketplace>/<plugin>/<version>/`` directory.

    Args:
        cache_dir: Claude Code plugin cache root.
        marketplace: Marketplace short-name to scan.

    Returns:
        Sorted list of version directories (empty when the subtree is absent).
    """
    root = cache_dir / marketplace
    if not root.is_dir():
        return []
    found: list[Path] = []
    for plugin_dir in sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink()):
        found.extend(sorted(v for v in plugin_dir.iterdir() if v.is_dir()))
    return found


def _is_version_dir(path: Path) -> bool:
    """Report whether *path* is a real version dir (guards 1 and 2)."""
    if path.is_symlink() or not path.is_dir():
        return False
    return (path / ".claude-plugin" / "plugin.json").is_file()


def _is_protected(path: Path, ctx: PurgeContext) -> bool:
    """Report whether *path* must be kept regardless of age (guards 5, 6 and 7)."""
    resolved = path.resolve()
    if resolved in ctx.protected:
        return True
    installed = resolve_install_path(ctx.registry, ctx.marketplace, path.parent.name)
    if installed and Path(installed).resolve() == resolved:
        return True
    return ctx.newest_registered.get(path.parent.name) == path.name


def build_newest_registered(version_dirs: list[Path], registry: Path, marketplace: str) -> dict[str, str]:
    """Map each registry-known plugin to its newest cached version name.

    Plugins absent from the registry are omitted, which makes every one of their
    versions reclaimable — the only way a renamed or uninstalled plugin's tree is
    ever freed.

    Args:
        version_dirs: Output of :func:`iter_version_dirs`.
        registry: Path to ``installed_plugins.json``.
        marketplace: Marketplace short-name.

    Returns:
        Plugin short-name → newest version directory name.
    """
    by_plugin: dict[str, list[str]] = {}
    for version_dir in version_dirs:
        by_plugin.setdefault(version_dir.parent.name, []).append(version_dir.name)
    newest: dict[str, str] = {}
    for plugin, versions in by_plugin.items():
        if resolve_install_path(registry, marketplace, plugin) is None:
            continue
        newest[plugin] = max(versions, key=version_key)
    return newest


def classify(version_dir: Path, ctx: PurgeContext) -> Candidate | None:
    """Decide whether *version_dir* may be deleted.

    Args:
        version_dir: Directory to evaluate.
        ctx: Shared inputs (registry, protections, age threshold, clock).

    Returns:
        A :class:`Candidate` when every guard passes, else ``None``.
    """
    if not _is_version_dir(version_dir):
        return None
    orphaned_at_ms = read_orphan_stamp(version_dir)
    if orphaned_at_ms is None:
        return None
    if ctx.now_ms - orphaned_at_ms < ctx.min_age_ms:
        return None
    if _is_protected(version_dir, ctx):
        return None
    return Candidate(
        path=version_dir,
        plugin=version_dir.parent.name,
        version=version_dir.name,
        orphaned_at_ms=orphaned_at_ms,
        size_bytes=dir_size(version_dir),
        leases=count_leases(version_dir),
    )


def render_report(candidates: list[Candidate], now_ms: int) -> str:
    """Format the human-facing report for report mode.

    Args:
        candidates: Purge candidates, in scan order.
        now_ms: Current epoch milliseconds, for age display.

    Returns:
        Multi-line report string (no trailing newline).
    """
    if not candidates:
        return "nothing to purge — no orphaned cache versions past the age threshold"
    lines = [f"{len(candidates)} orphaned cache version(s) eligible for purge:"]
    total = 0
    for cand in candidates:
        total += cand.size_bytes
        hours = age_hours(cand.orphaned_at_ms, now_ms)
        lines.append(
            f"  {cand.plugin}/{cand.version}\t{cand.size_bytes // 1_048_576} MB"
            f"\torphaned {hours / 24:.1f}d ago\tleases:{cand.leases}",
        )
    lines.append(f"total: {total // 1_048_576} MB")
    lines.append("re-run with --apply to delete (nothing was deleted)")
    return "\n".join(lines)


def purge(candidates: list[Candidate]) -> tuple[list[str], int]:
    """Delete every candidate directory.

    Args:
        candidates: Purge candidates.

    Returns:
        Tuple of (log lines, total bytes reclaimed). A directory that fails to
        delete is reported and skipped rather than aborting the run.
    """
    log: list[str] = []
    reclaimed = 0
    for cand in candidates:
        try:
            shutil.rmtree(cand.path)
        except OSError as exc:
            log.append(f"  ! failed: {cand.plugin}/{cand.version} — {exc}")
            continue
        reclaimed += cand.size_bytes
        log.append(f"  purged: {cand.plugin}/{cand.version}")
    return log, reclaimed


def _build_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser."""
    parser = argparse.ArgumentParser(prog="purge_plugin_cache", description=__doc__.splitlines()[0])
    parser.add_argument("--cache-dir", type=Path, default=Path.home() / ".claude" / "plugins" / "cache")
    parser.add_argument("--marketplace", default="borda-ai-rig")
    parser.add_argument(
        "--registry",
        type=Path,
        default=Path.home() / ".claude" / "plugins" / "installed_plugins.json",
    )
    parser.add_argument("--protect", action="append", default=[], metavar="PATH")
    parser.add_argument("--min-orphan-age-hours", type=int, default=24)
    parser.add_argument("--apply", action="store_true", help="delete (default is report-only)")
    parser.add_argument("--expect-count", type=int, default=None, help="abort unless exactly N candidates found")
    return parser


def _collect(args: argparse.Namespace) -> list[Candidate]:
    """Scan the cache and return every purge candidate."""
    version_dirs = iter_version_dirs(args.cache_dir, args.marketplace)
    ctx = PurgeContext(
        registry=args.registry,
        marketplace=args.marketplace,
        protected=frozenset(Path(p).resolve() for p in args.protect if p),
        min_age_ms=args.min_orphan_age_hours * _MS_PER_HOUR,
        now_ms=int(time.time() * 1000),
        newest_registered=build_newest_registered(version_dirs, args.registry, args.marketplace),
    )
    return [c for c in (classify(v, ctx) for v in version_dirs) if c is not None]


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument vector; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code (see module docstring).
    """
    args = _build_parser().parse_args(argv)
    if not _TOKEN_RE.match(args.marketplace):
        print(f"! marketplace {args.marketplace!r} contains disallowed characters", file=sys.stderr)
        return 2
    if args.min_orphan_age_hours < 0:
        print("! --min-orphan-age-hours must be >= 0", file=sys.stderr)
        return 2
    if not args.cache_dir.is_dir():
        print(f"! cache dir not found: {args.cache_dir}", file=sys.stderr)
        return 1

    candidates = _collect(args)
    if args.expect_count is not None and len(candidates) != args.expect_count:
        print(
            f"! expected {args.expect_count} candidate(s) but found {len(candidates)} — "
            "cache changed since the report; nothing deleted",
            file=sys.stderr,
        )
        return 1
    if not args.apply:
        print(render_report(candidates, int(time.time() * 1000)))
        return 0
    log, reclaimed = purge(candidates)
    print("\n".join([*log, f"reclaimed: {reclaimed // 1_048_576} MB from {len(candidates)} version(s)"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
