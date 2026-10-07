#!/usr/bin/env python3
"""check_codemap_guard.py — fail on a codemap index-guard copy that nothing manages.

The guard ("is codemap-py installed, and does an index exist for this project?") together
with its index-path derivation was hand-copied across four plugins. Nothing linked the
copies, so each drifted on its own and a single path fix cost ten edits: the pass that
introduced git-root anchoring and raw-basename project names had to touch three separate
copies and still left others behind.

``plugins/CLAUDE.md`` permits exactly two shapes for logic that several plugins need:

1. **Consume the provider's public CLI** — ``codemap-py query``, ``codemap_resolve.py``,
   ``detect_codemap.py``. Such a consumer never spells the index path itself, so it does
   not match :data:`SIGNATURE` and this check never sees it. That is the preferred shape.
2. **One canonical copy propagated byte-identical** via ``propagate_shared.py`` MANIFEST.

Never an imported sibling helper — that is a forbidden cross-plugin runtime dependency.

Anything else is an unmanaged copy: a hand-written index path in a file no MANIFEST entry
covers and no registry entry names. Those fail here, which is what makes adding one
silently impossible.

Inline bash inside agent/skill prose cannot become a MANIFEST entry, because MANIFEST
propagates whole files and these are fragments. Those copies are instead named in
:data:`REGISTRY` with the shape chosen for them, and held to the two invariants that
actually drifted in production:

* **anchoring** — the index dir hangs off a resolved project-root variable, never the
  process CWD; a skill invoked from a subdirectory otherwise reports ``no_index`` while
  an index exists.
* **raw project name** — the name is the unmodified basename. The scanner writes it
  verbatim, so any sanitizing filter seeks a filename that was never written, which is a
  permanent silent ``no_index`` for any directory containing a space, ``+``, or non-ASCII.

Adding a guard to a new file therefore requires an explicit REGISTRY entry with a reason,
and the registry is verified in both directions: an entry naming a file that no longer
holds a guard fails too, so the inventory cannot rot.

Usage:
    check_codemap_guard.py [--root DIR] [--list]

Exit codes:
    0 — every guard is manifested, registered-and-conformant, or provider-CLI based
    1 — unregistered copy, stale registry entry, or invariant violation
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from dataclasses import dataclass
from pathlib import Path

#: Lines that derive a codemap index location. A consumer that calls the provider's CLI
#: instead never matches this, and is therefore invisible to the check by construction.
SIGNATURE = re.compile(r"CODEMAP_INDEX_DIR|\.cache/codemap")

#: Anchoring invariant: the default index dir must hang off a shell variable holding the
#: resolved project root (``${CODEMAP_INDEX_DIR:-$_ROOT/.cache/codemap}``), never a bare
#: or CWD-relative ``.cache/codemap``.
ANCHORED = re.compile(r"CODEMAP_INDEX_DIR:-\$\{?[A-Za-z_][A-Za-z0-9_]*\}?/\.cache/codemap")

#: Sanitization invariant: the project name must never be filtered through tr/sed.
SANITIZED = re.compile(r"basename[^\n]*\|\s*(?:tr|sed)\b")

#: File extensions that are scanned for codemap index path usage.
_SCAN_SUFFIXES = frozenset({".md", ".py", ".js", ".sh"})
#: Path components that exclude a file from scanning, such as tests and caches.
_SKIP_PARTS = frozenset({"__pycache__", ".reports", "tests", ".cache", "node_modules"})

#: The provider plugin *defines* the index layout; consumers mirror it. Scanning it would
#: demand a registry entry for every file that implements the very rule being mirrored.
_PROVIDER_PLUGIN = "codemap-py"

#: This checker spells the pattern in its own regexes and docstring.
_SELF = "check_codemap_guard.py"


@dataclass(frozen=True)
class Guard:
    """One registered guard copy.

    Args:
        shape: Canonicalization shape chosen for this copy.
        reason: Why this shape rather than a MANIFEST entry or the provider CLI.
    """

    shape: str
    reason: str


#: Shapes whose occurrences are bash and must satisfy the anchoring/raw-name invariants.
BASH_SHAPES = frozenset({"bash-preamble", "inline-index-path"})

#: Every guard copy that is not MANIFEST-locked, with the shape chosen for it.
#: A file matching SIGNATURE that appears neither here nor in MANIFEST is a hard failure.
REGISTRY: dict[str, Guard] = {
    # --- cc_foundry agents: identical 4-line bash preamble, inlined in agent prose ---
    "plugins/cc_foundry/agents/challenger.md": Guard(
        "bash-preamble", "inline agent pre-flight; fragment, not a whole file MANIFEST can propagate"
    ),
    "plugins/cc_foundry/agents/doc-scribe.md": Guard(
        "bash-preamble", "inline agent pre-flight; fragment, not a whole file MANIFEST can propagate"
    ),
    "plugins/cc_foundry/agents/perf-optimizer.md": Guard(
        "bash-preamble", "inline agent pre-flight; fragment, not a whole file MANIFEST can propagate"
    ),
    "plugins/cc_foundry/agents/qa-specialist.md": Guard(
        "bash-preamble", "inline agent pre-flight; fragment, not a whole file MANIFEST can propagate"
    ),
    "plugins/cc_foundry/agents/solution-architect.md": Guard(
        "bash-preamble", "inline agent pre-flight plus a one-line prose restatement of the same test"
    ),
    "plugins/cc_foundry/agents/sw-engineer.md": Guard(
        "bash-preamble", "inline agent pre-flight; fragment, not a whole file MANIFEST can propagate"
    ),
    # --- cc_research ---
    "plugins/cc_research/agents/scientist.md": Guard(
        "bash-preamble", "inline agent pre-flight; fragment, not a whole file MANIFEST can propagate"
    ),
    "plugins/cc_research/skills/_shared/codemap-context.md": Guard(
        "bash-preamble", "research-owned wrapper; per-plugin query map differs, so no byte-identical copy"
    ),
    # --- cc_develop ---
    "plugins/cc_develop/skills/refactor/SKILL.md": Guard(
        "bash-preamble", "skill-local pre-flight gated on AFFECTED_MODULES; fragment"
    ),
    "plugins/cc_develop/skills/debug/SKILL.md": Guard(
        "inline-index-path", "index path built inline to read scanned_at out of the index"
    ),
    "plugins/cc_develop/skills/fix/SKILL.md": Guard(
        "inline-index-path", "index path built inline to read scanned_at out of the index"
    ),
    "plugins/cc_develop/skills/_shared/worktree-isolation.md": Guard(
        "index-copy",
        "copies the parent index into a fresh worktree; the bare relative path is the new "
        "worktree root by construction, so root-anchoring does not apply",
    ),
    "plugins/cc_develop/bin/codemap_scan.py": Guard(
        "python-local", "develop-owned batch scanner; resolves the index in Python, not bash"
    ),
    "plugins/cc_develop/README.md": Guard("doc-prose", "user-facing description of index layout, not executable"),
    # --- cc_oss ---
    "plugins/cc_oss/bin/detect_codemap.py": Guard(
        "python-local", "oss-owned detector with its own --prefix/--strict CLI; resolves in Python, not bash"
    ),
    "plugins/cc_oss/skills/resolve/SKILL.md": Guard("bash-preamble", "skill-local pre-flight; fragment"),
    "plugins/cc_oss/skills/resolve/modes/action-item-dispatch.md": Guard(
        "inline-index-path", "index file path built inline for the codemap_cache.py read call"
    ),
    "plugins/cc_oss/skills/review/modes/codemap-context.md": Guard(
        "bash-preamble", "review pre-flight; fragment, and gated on CODEMAP_ENABLED"
    ),
}


def manifest_paths(bin_dir: Path) -> set[str]:
    """Return every repo-relative path MANIFEST covers, canonical and copies alike.

    Read from ``propagate_shared.py`` rather than restated here, so a file becomes exempt
    the moment it is genuinely drift-locked and never merely because someone said so.

    Args:
        bin_dir: Directory holding ``propagate_shared.py``.

    Returns:
        Repo-relative path strings under MANIFEST control.
    """
    spec = importlib.util.spec_from_file_location("_propagate_shared", bin_dir / "propagate_shared.py")
    if spec is None or spec.loader is None:
        return set()
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    paths: set[str] = set()
    for entry in module.MANIFEST:
        paths.add(str(entry["canonical"]))
        paths.update(str(c) for c in entry["copies"])
    return paths


def _scannable(path: Path, root: Path) -> bool:
    """Return True when *path* is a consumer file this check governs.

    Excludes the provider plugin (it defines the layout every consumer mirrors, so every
    file in it would need a registry entry for implementing its own rule) and this
    checker, which spells the pattern in its own regexes.

    Args:
        path: Candidate file.
        root: Repository root.

    Returns:
        True when the file should be scanned.
    """
    if path.suffix not in _SCAN_SUFFIXES or path.name == _SELF:
        return False
    parts = path.relative_to(root).parts
    return not (_SKIP_PARTS & set(parts)) and _PROVIDER_PLUGIN not in parts


def scan_files(root: Path) -> list[Path]:
    """Return every consumer file worth scanning, skipping caches, reports and tests.

    Args:
        root: Repository root.

    Returns:
        Sorted absolute paths.
    """
    plugins = root / "plugins"
    if not plugins.is_dir():
        return []
    return sorted(p for p in plugins.rglob("*") if p.is_file() and _scannable(p, root))


def guard_lines(path: Path) -> list[tuple[int, str]]:
    """Return ``(lineno, text)`` for each line deriving a codemap index location.

    Args:
        path: File to inspect.

    Returns:
        One entry per matching line; empty when the file uses the provider CLI instead.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [(n, line) for n, line in enumerate(text.splitlines(), 1) if SIGNATURE.search(line)]


def invariant_findings(rel: str, guard: Guard, hits: list[tuple[int, str]]) -> list[str]:
    """Return invariant violations for one registered bash guard.

    Only bash shapes are checked: a Python copy resolves the index with ``pathlib`` and a
    doc-prose mention is not executable, so neither can express these invariants.

    Args:
        rel: Repo-relative path, used in the finding text.
        guard: Registry entry for this file.
        hits: Output of :func:`guard_lines`.

    Returns:
        Human-readable findings, one per violating line.

    Examples:
        >>> ok = [(1, '_IDX="${CODEMAP_INDEX_DIR:-$_ROOT/.cache/codemap}"')]
        >>> invariant_findings("f.md", Guard("bash-preamble", ""), ok)
        []
        >>> bad = [(4, '_IDX=".cache/codemap"')]
        >>> invariant_findings("f.md", Guard("bash-preamble", ""), bad)
        ['f.md:4 index dir not anchored to a project-root variable']
    """
    if guard.shape not in BASH_SHAPES:
        return []
    findings: list[str] = []
    for lineno, line in hits:
        if ".cache/codemap" in line and not ANCHORED.search(line):
            findings.append(f"{rel}:{lineno} index dir not anchored to a project-root variable")
        if SANITIZED.search(line):
            findings.append(f"{rel}:{lineno} project name is sanitized; scanner writes the raw basename")
    return findings


def check(root: Path) -> list[str]:
    """Return every finding: unregistered copies, stale entries, invariant violations.

    Args:
        root: Repository root.

    Returns:
        Human-readable findings; empty when every guard is managed.
    """
    manifested = manifest_paths(root / "plugins" / "cc_foundry" / "bin")
    findings: list[str] = []
    seen: set[str] = set()

    for path in scan_files(root):
        hits = guard_lines(path)
        if not hits:
            continue
        rel = path.relative_to(root).as_posix()
        if rel in manifested:
            continue
        guard = REGISTRY.get(rel)
        if guard is None:
            findings.append(
                f"{rel}:{hits[0][0]} unmanaged codemap guard — add a MANIFEST entry, consume the "
                "provider CLI, or register it in check_codemap_guard.REGISTRY with a reason"
            )
            continue
        seen.add(rel)
        findings.extend(invariant_findings(rel, guard, hits))

    findings.extend(
        f"{rel} registered in REGISTRY but holds no codemap guard — drop the stale entry"
        for rel in sorted(set(REGISTRY) - seen)
    )
    return findings


def main(argv: list[str] | None = None) -> int:
    """CLI entry point - returns 0 when every guard is managed, 1 otherwise."""
    parser = argparse.ArgumentParser(description="Fail on unmanaged codemap index-guard copies.")
    parser.add_argument("--root", default=".", help="repository root (default: cwd)")
    parser.add_argument("--list", action="store_true", help="print the guard registry and exit")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()

    if args.list:
        for rel, guard in sorted(REGISTRY.items()):
            print(f"{guard.shape:20s} {rel}\n{'':20s} {guard.reason}")
        manifested = sorted(p for p in manifest_paths(root / "plugins" / "cc_foundry" / "bin") if "codemap" in p)
        for rel in manifested:
            print(f"{'manifested':20s} {rel}\n{'':20s} byte-identical copy enforced by propagate_shared.py")
        return 0

    findings = check(root)
    if findings:
        print("\n".join(f"CODEMAP-GUARD: {f}" for f in findings))
        print("  see plugins/cc_foundry/bin/check_codemap_guard.py docstring for the two permitted shapes")
        return 1
    print("✓: every codemap index guard is manifested, registered or provider-CLI based")
    return 0


if __name__ == "__main__":
    sys.exit(main())
