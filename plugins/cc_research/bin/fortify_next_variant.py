#!/usr/bin/env python
"""fortify_next_variant.py — read the current ablation variant by cursor and pre-register cleanup.

Extracted from ``skills/fortify/SKILL.md`` step 4a-init. Three jobs in one call, because the
loop pays a ~12 s round-trip per Bash call: advance-or-stop on the cursor, skip variants already
terminal in ``results.jsonl`` (resume after a compaction), and append the worktree path to the
cleanup accumulator *before* the worktree exists, closing the interrupt gap.

The cursor lives in a sentinel rather than a shell variable, which would die between Bash calls.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/fortify_next_variant.py" -- "$FORTIFY_DIR"

Sentinels read:   fortify-variant-idx-${CSID} (cursor, default 1), fortify-paths-ptr-${CSID}
Sentinels written: fortify-variant-name-${CSID}; fortify-variant-idx-${CSID} on a resume skip

Stdout markers the skill branches on:
    ``FORTIFY_LOOP_DONE=1``     — cursor past the last variant; halt the loop
    ``FORTIFY_SKIP_VARIANT=1``  — cursor already advanced; go straight back to 4a-init
    ``! BLOCKED``               — unreadable variant name; halt F4

Exit codes:
    0 — a variant is ready, the loop is done, or the variant was skipped
    1 — variant name unreadable, or a sentinel write failed
    2 — argument error (argparse default)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

#: Terminal non-timeout statuses — a timed-out variant is deliberately retried.
_TERMINAL_STATUS_RE = re.compile(r'"status":"(completed|revert-conflict|revert-missing|metric-failed)"')


def normalise_variant_name(raw: str) -> str:
    """Turn a scientist-supplied variant label into the canonical ``variant-<slug>`` directory name.

    Args:
        raw: ``.variant_name`` value from ``variants.jsonl``.

    Returns:
        The normalised name, always carrying exactly one ``variant-`` prefix.

    Examples:
        >>> normalise_variant_name("No Augmentation")
        'variant-no-augmentation'
        >>> normalise_variant_name("variant-Dropout")
        'variant-dropout'
    """
    slug = raw.replace(" ", "-").lower()
    if slug.startswith("variant-"):
        slug = slug[len("variant-") :]
    return f"variant-{slug}"


def read_variant_name(variants_file: Path, index: int) -> str:
    """Return ``.variant_name`` from the 1-based ``index`` line of ``variants.jsonl``.

    A malformed line, a missing key, or a JSON ``null`` all yield an empty string — the same
    collapse ``jq -r '.variant_name // empty' 2>/dev/null`` performed.

    Args:
        variants_file: Path to ``variants.jsonl``.
        index: 1-based line number.

    Returns:
        The variant name, or an empty string when it cannot be read.
    """
    try:
        lines = variants_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    if index < 1 or index > len(lines):
        return ""
    try:
        record = json.loads(lines[index - 1])
    except (ValueError, TypeError):
        return ""
    value = record.get("variant_name") if isinstance(record, dict) else None
    return value if isinstance(value, str) else ""


def is_already_terminal(results_file: Path, bare_name: str) -> bool:
    """Report whether ``results.jsonl`` already records a terminal, non-timeout run of this variant.

    Args:
        results_file: Path to ``results.jsonl``.
        bare_name: Variant name without its ``variant-`` prefix.

    Returns:
        True when a single line names this variant and carries a terminal status.
    """
    try:
        lines = results_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    # re.escape, unlike the grep original, keeps a name containing regex metacharacters literal.
    variant_re = re.compile(rf'"variant":"(variant-)?{re.escape(bare_name)}"')
    return any(variant_re.search(line) and _TERMINAL_STATUS_RE.search(line) for line in lines)


def _sentinel_dir() -> Path:
    """Return the session temp directory (never a hardcoded ``/tmp`` — absent on native Windows)."""
    return Path(os.environ.get("TMPDIR") or tempfile.gettempdir())


def _session_token() -> str:
    """Return the sentinel session suffix; ``os.getppid()`` would name the calling shell, not the session."""
    return os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"


def _read_sentinel(path: Path) -> str:
    """Return the first line of a sentinel file, or an empty string when it is absent."""
    try:
        return path.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        return ""


def _count_lines(path: Path) -> int:
    """Count lines holding at least one character, as ``grep -c .`` did."""
    try:
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line)
    except OSError:
        return 0


def _register_cleanup_path(tmp: Path, csid: str, fortify_dir: str, variant_name: str) -> None:
    """Append the worktree path to the cleanup accumulator before the worktree is created.

    Over-registering is safe: the post-loop sweep skips paths with no directory on disk.
    """
    paths_file = _read_sentinel(tmp / f"fortify-paths-ptr-{csid}")
    if not paths_file:
        paths_file = str(tmp / f"fortify-worktree-paths-fallback-{csid}")
    # Forward-slash join, not pathlib: step 4a rebuilds this same string in the shell, and a
    # Windows backslash join here would not match what `git worktree add` is handed there.
    worktree = os.environ.get("FORTIFY_WORKTREE") or f"{fortify_dir}/worktrees/{variant_name}"
    with Path(paths_file).open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(worktree + "\n")


def main(argv: list[str] | None = None) -> int:
    """Advance the variant cursor and emit the marker the fortify loop branches on.

    Args:
        argv: Optional argv override (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` when a variant is ready, the loop is done, or the variant was skipped; ``1`` on a
        blocked variant or a failed write.
    """
    parser = argparse.ArgumentParser(
        prog="fortify_next_variant.py",
        description="Read the current ablation variant by cursor and pre-register its cleanup path.",
    )
    parser.add_argument("fortify_dir", nargs="?", default="", help="Run directory holding variants.jsonl.")
    args = parser.parse_args(argv)

    tmp = _sentinel_dir()
    csid = _session_token()
    idx_sentinel = tmp / f"fortify-variant-idx-{csid}"
    try:
        index = int(_read_sentinel(idx_sentinel) or 1)
    except ValueError:
        index = 1

    fortify_dir = args.fortify_dir
    total = _count_lines(Path(fortify_dir) / "variants.jsonl")
    if index > total:
        print(f"FORTIFY_LOOP_DONE=1 — all {total} variants processed; proceed to post-loop delta computation")
        return 0

    raw_name = read_variant_name(Path(fortify_dir) / "variants.jsonl", index)
    if not raw_name or raw_name == "null":
        # No invented fallback — a synthesized name would collapse every iteration onto one worktree.
        print(f"! BLOCKED — variants.jsonl line {index} has no readable .variant_name; check F3 output. Halting F4.")
        return 1

    variant_name = normalise_variant_name(raw_name)
    bare_name = variant_name[len("variant-") :]
    try:
        (tmp / f"fortify-variant-name-{csid}").write_text(variant_name + "\n", encoding="utf-8", newline="\n")
    except OSError as exc:
        print(f"fortify_next_variant: cannot write variant-name sentinel: {exc}", file=sys.stderr)
        return 1

    if is_already_terminal(Path(fortify_dir) / "results.jsonl", bare_name):
        try:
            idx_sentinel.write_text(f"{index + 1}\n", encoding="utf-8", newline="\n")
        except OSError as exc:
            print(f"fortify_next_variant: cannot advance cursor: {exc}", file=sys.stderr)
            return 1
        print(f"→ {bare_name} already terminal (non-timeout) in results.jsonl — skipping (resume)")
        print("FORTIFY_SKIP_VARIANT=1")
        return 0

    try:
        _register_cleanup_path(tmp, csid, fortify_dir, variant_name)
    except OSError as exc:
        print(f"fortify_next_variant: cannot register cleanup path: {exc}", file=sys.stderr)
        return 1

    print(f"→ variant {index}/{total}: {variant_name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
