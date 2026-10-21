#!/usr/bin/env python
"""fortify_next_variant.py — read the current ablation variant by cursor and pre-register cleanup.

Extracted from ``skills/fortify/SKILL.md`` step 4a-init. Three jobs in one call, because the
loop pays a ~12 s round-trip per Bash call: advance-or-stop on the cursor, skip variants already
terminal in ``results.jsonl`` (resume after a compaction), and append the worktree path to the
cleanup accumulator *before* the worktree exists, closing the interrupt gap.

The cursor lives in a sentinel rather than a shell variable, which would die between Bash calls.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/fortify_next_variant.py" -- "$FORTIFY_DIR"

Sentinels read:   fortify-variant-idx-${CSID} (cursor, default 1)
Sentinels written: fortify-variant-name-${CSID}; fortify-variant-idx-${CSID} on a resume skip
Appends to:       <fortify_dir>/worktree-paths.txt (cleanup accumulator, created by the skill)

Stdout markers the skill branches on:
    ``FORTIFY_LOOP_DONE=1``     — cursor past the last variant; halt the loop
    ``FORTIFY_SKIP_VARIANT=1``  — cursor already advanced; go straight back to 4a-init
    ``FORTIFY_STALE_WORKTREE=1`` — the variant's worktree directory already exists; remove it (4f) before 4a
    ``! BLOCKED``               — unreadable variant name, line 1 not ``full``, or (past line 1) the ``full``
                                  record is not ``completed`` (full-first gate); halt F4

Exit codes:
    0 — a variant is ready, the loop is done, or the variant was skipped
    1 — variant name unreadable, full-first gate failed, or a sentinel write failed
    2 — argument error (argparse default)
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import re
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

#: Sibling import: bin/ is not a package. Putting it on the path resolves the private writer whether this file runs as
#: a script, under the test conftest, or as a doctest module.
_BIN_DIR = str(Path(__file__).resolve().parent)
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)

from _sentinel_io import write_private_text  # noqa: E402

#: Terminal non-timeout statuses — a timed-out variant is deliberately retried.
_TERMINAL_STATUS_RE = re.compile(r'"status":"(completed|revert-conflict|revert-missing|metric-failed|diverged)"')
#: Normalised name of the sanity-check variant that must run first and complete before any ablation.
FULL_VARIANT = "variant-full"


def _iter_lines(path: Path) -> Iterator[str]:
    """Yield the lines of ``path`` without line endings, streamed; yield nothing when the file cannot be opened.

    ``results.jsonl`` and ``variants.jsonl`` grow with every variant and are scanned on every loop step, so they are
    read line by line instead of loaded whole.
    """
    try:
        handle = path.open(encoding="utf-8")
    except OSError:
        return
    with handle:
        for raw in handle:
            yield raw.rstrip("\n")


def _nth_line(path: Path, number: int) -> str | None:
    """Return the 1-based ``number``-th line of ``path``, or ``None`` when the file is shorter or unreadable.

    Examples:
        >>> import tempfile, pathlib
        >>> ledger = pathlib.Path(tempfile.mkdtemp()) / "v.jsonl"
        >>> _ = ledger.write_text("a\\nb\\n", encoding="utf-8")
        >>> _nth_line(ledger, 2), _nth_line(ledger, 3)
        ('b', None)
    """
    lines = _iter_lines(path)
    try:
        return next(itertools.islice(lines, number - 1, None), None)
    finally:
        lines.close()


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


def is_safe_variant_name(name: str) -> bool:
    """Report whether a normalised variant name is safe to use as one worktree directory component.

    The name comes from scientist-authored ``variants.jsonl`` and is joined into
    ``<fortify_dir>/worktrees/<name>`` for ``git worktree add`` / ``remove --force`` and written
    as one line of the cleanup accumulator. A path separator would escape ``worktrees/``; a
    newline or other control character would inject an extra accumulator line (an arbitrary
    path for the cleanup sweep) and split the name sentinel, whose reader takes line one only.

    Args:
        name: Output of :func:`normalise_variant_name`.

    Returns:
        False for an empty slug, a ``/`` or ``\\`` separator, or any control character.

    Examples:
        >>> is_safe_variant_name("variant-no-augmentation")
        True
        >>> is_safe_variant_name("variant-../../etc")
        False
        >>> is_safe_variant_name("variant-a\\nb")
        False
        >>> is_safe_variant_name("variant-")
        False
    """
    slug = name[len("variant-") :] if name.startswith("variant-") else name
    if not slug or "/" in slug or "\\" in slug:
        return False
    return not any(ord(char) < 32 or ord(char) == 127 for char in slug)


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
    if index < 1:
        return ""
    line = _nth_line(variants_file, index)
    if line is None:
        return ""
    try:
        record = json.loads(line)
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
    # re.escape, unlike the grep original, keeps a name containing regex metacharacters literal.
    variant_re = re.compile(rf'"variant":"(variant-)?{re.escape(bare_name)}"')
    return any(variant_re.search(line) and _TERMINAL_STATUS_RE.search(line) for line in _iter_lines(results_file))


def full_variant_status(results_file: Path) -> str:
    """Return the last recorded ``status`` of the ``full`` variant in ``results.jsonl``.

    Args:
        results_file: Path to ``results.jsonl``.

    Returns:
        The status string, or an empty string when no ``full`` record exists.
    """
    status = ""
    for line in _iter_lines(results_file):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("variant") in ("full", FULL_VARIANT):
            status = str(record.get("status", ""))
    return status


def _full_first_gate(fortify_dir: str, index: int, variant_name: str) -> int | None:
    """Enforce that the ``full`` variant runs first and completes before any ablation runs.

    Line 1 must be ``full``: an ablation measured before the baseline is known to reproduce the source run is wasted
    compute. Past line 1 the ``full`` record must say ``completed`` — ``diverged`` (mean outside tolerance of
    ``best_metric``, decided by ``fortify_metric_stats.py``), ``metric-failed`` or a missing record all abort the loop.

    Returns:
        ``None`` when the variant may run, else ``1`` after printing the ``! BLOCKED`` line.
    """
    if index == 1:
        if variant_name == FULL_VARIANT:
            return None
        print(
            f"! BLOCKED — variants.jsonl line 1 is {variant_name!r}, not the full variant; F3 writes full first. Halting F4."
        )
        return 1
    status = full_variant_status(Path(fortify_dir) / "results.jsonl")
    if status == "completed":
        return None
    print(
        f"! BLOCKED — full-first gate: full variant {status or 'not recorded'} in results.jsonl; no ablation runs "
        "against an unreproduced baseline. Halting F4."
    )
    return 1


def _sentinel_dir() -> Path:
    """Return the session temp directory (never a hardcoded ``/tmp`` — absent on native Windows)."""
    return Path(os.environ.get("TMPDIR") or tempfile.gettempdir())


def _session_token() -> str:
    """Return the sentinel session suffix; ``os.getppid()`` would name the calling shell, not the session."""
    return os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"


def _validate_fortify_dir(raw: str) -> Path | None:
    """Resolve ``raw`` and confirm it stays within an allowed root.

    Mirrors the containment check in read_state_field.py / retro_analyze.py /
    verify_patient_split.py. An empty value is a legitimate "no run directory yet" case
    (the CLI arg is optional) and is returned unchanged rather than rejected.

    Args:
        raw: Raw ``fortify_dir`` positional argument.

    Returns:
        ``Path(raw)`` when ``raw`` is empty or resolves inside an allowed root; ``None``
        when it resolves outside every allowed root.
    """
    if not raw:
        return Path(raw)
    resolved = Path(raw).expanduser().resolve()
    project_root = Path.cwd().resolve()
    allowed_roots = [
        project_root,
        (project_root / ".experiments").resolve(),
        (Path(os.path.expanduser("~")) / ".claude" / "projects").resolve(),
        Path(tempfile.gettempdir()).resolve(),
    ]
    for root in allowed_roots:
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        return Path(raw)
    return None


def _read_sentinel(path: Path) -> str:
    """Return the first line of a sentinel file, or an empty string when it is absent."""
    line = _nth_line(path, 1)
    return line if line is not None else ""


def _count_lines(path: Path) -> int:
    """Count lines holding at least one character, as ``grep -c .`` did."""
    return sum(1 for line in _iter_lines(path) if line)


def _worktree_path(fortify_dir: str, variant_name: str) -> str:
    """Return the variant's worktree path as step 4a builds it in the shell.

    Forward-slash join, not pathlib: step 4a rebuilds this same string in the shell, and a Windows backslash join here
    would not match what ``git worktree add`` is handed there.
    """
    return os.environ.get("FORTIFY_WORKTREE") or f"{fortify_dir}/worktrees/{variant_name}"


def _register_cleanup_path(fortify_dir: str, variant_name: str) -> None:
    """Append the worktree path to the run directory's cleanup accumulator before the worktree is created.

    The accumulator is ``<fortify_dir>/worktree-paths.txt``, a fixed name inside the run directory. A pointer read from
    a shared-TMPDIR sentinel would let anything that can write that sentinel repoint this append at an arbitrary file.
    Over-registering is safe: the post-loop sweep skips paths with no directory on disk.
    """
    paths_file = Path(fortify_dir) / "worktree-paths.txt"
    with paths_file.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(_worktree_path(fortify_dir, variant_name) + "\n")


def _resolve_pending_variant(fortify_dir: str, index: int) -> tuple[str, int] | int:
    """Name the variant at ``index``, or return the exit code that ends this step.

    Returns ``(variant_name, total)`` when a variant is ready to run. Returns ``0`` once the cursor has passed the last
    variant (the loop is done), or ``1`` for an out-of-bounds ``fortify_dir`` or an unreadable variant name.
    """
    if _validate_fortify_dir(fortify_dir) is None:
        print(
            f"! BLOCKED — fortify_dir outside allowed roots (project root, .experiments/, "
            f"~/.claude/projects, tempdir): {fortify_dir}"
        )
        return 1

    variants = Path(fortify_dir) / "variants.jsonl"
    total = _count_lines(variants)
    if index > total:
        print(f"FORTIFY_LOOP_DONE=1 — all {total} variants processed; proceed to post-loop delta computation")
        return 0

    raw_name = read_variant_name(variants, index)
    if not raw_name or raw_name == "null":
        # No invented fallback — a synthesized name would collapse every iteration onto one worktree.
        print(f"! BLOCKED — variants.jsonl line {index} has no readable .variant_name; check F3 output. Halting F4.")
        return 1

    variant_name = normalise_variant_name(raw_name)
    if not is_safe_variant_name(variant_name):
        print(
            f"! BLOCKED — variants.jsonl line {index} .variant_name {raw_name!r} contains a path separator or "
            "control character (or is empty); rename it in F3 output. Halting F4."
        )
        return 1
    return variant_name, total


def _advance_if_terminal(fortify_dir: str, bare_name: str, idx_sentinel: Path, index: int) -> int | None:
    """Skip a variant already recorded terminal, advancing the cursor past it.

    Returns ``None`` when the variant still needs running, ``0`` after skipping it on a resume, or ``1`` when the cursor
    could not be advanced.
    """
    if not is_already_terminal(Path(fortify_dir) / "results.jsonl", bare_name):
        return None
    try:
        write_private_text(idx_sentinel, f"{index + 1}\n")
    except OSError as exc:
        print(f"fortify_next_variant: cannot advance cursor: {exc}", file=sys.stderr)
        return 1
    print(f"→ {bare_name} already terminal (non-timeout) in results.jsonl — skipping (resume)")
    print("FORTIFY_SKIP_VARIANT=1")
    return 0


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
    csid = _session_token()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", csid) or ".." in csid:
        print("fortify_next_variant: invalid sentinel session identity", file=sys.stderr)
        return 2

    tmp = _sentinel_dir()
    idx_sentinel = tmp / f"fortify-variant-idx-{csid}"
    try:
        index = int(_read_sentinel(idx_sentinel) or 1)
    except ValueError:
        index = 1

    fortify_dir = args.fortify_dir
    outcome = _resolve_pending_variant(fortify_dir, index)
    if isinstance(outcome, int):
        return outcome
    variant_name, total = outcome
    gate_code = _full_first_gate(fortify_dir, index, variant_name)
    if gate_code is not None:
        return gate_code

    bare_name = variant_name[len("variant-") :]
    try:
        write_private_text(tmp / f"fortify-variant-name-{csid}", variant_name + "\n")
    except OSError as exc:
        print(f"fortify_next_variant: cannot write variant-name sentinel: {exc}", file=sys.stderr)
        return 1

    skip_code = _advance_if_terminal(fortify_dir, bare_name, idx_sentinel, index)
    if skip_code is not None:
        return skip_code

    stale = Path(_worktree_path(fortify_dir, variant_name)).exists()
    try:
        _register_cleanup_path(fortify_dir, variant_name)
    except OSError as exc:
        print(f"fortify_next_variant: cannot register cleanup path: {exc}", file=sys.stderr)
        return 1

    print(f"→ variant {index}/{total}: {variant_name}")
    if stale:
        print("FORTIFY_STALE_WORKTREE=1 — worktree directory left by an interrupted attempt; run 4f before 4a")
    return 0


if __name__ == "__main__":
    sys.exit(main())
