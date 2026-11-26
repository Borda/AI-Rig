"""Compare resolve item anchors between the reviewed commit and the PR head.

Purpose: Give the picker evidence about changed paths and lines before selection.
Scope: Read local commit objects and the resolve run's action-items JSONL. A changed
    line may merit checking in Phase 1, which determines whether a concern is fixed.
Usage: Run with --items, --report-file (whose sibling is head-sha.txt), and
    --current-head from the PR repository or one of its subdirectories.
Outputs: A JSON array of advisory markers with item IDs, paths, and diff counts.
    Files without verifiable paths are omitted. Git reports binary counts as null.
Failure: Report an unusable input or comparison on stderr and leave stdout empty.
Used by: The oss:resolve Step 3d selection table.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

_OID = re.compile(r"(?:[0-9a-f]{7,40}|[0-9a-f]{64})", re.IGNORECASE | re.ASCII)
_DIFF_OPTIONS = (
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--no-renames",
    "--no-relative",
    "--ignore-submodules=none",
    "--submodule=short",
)
_HUNK = re.compile(rb"^@@ -([0-9]+)(?:,([0-9]+))? \+[0-9]+(?:,[0-9]+)? @@", re.MULTILINE)


def _git(*args: str, input_data: bytes | None = None, check: bool = True) -> bytes:
    """Read Git data with lazy fetching, transports, and optional locks disabled."""
    env = {**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": "", "GIT_OPTIONAL_LOCKS": "0"}
    # GIT_DIFF_OPTS can replace the explicit zero-context setting used for line evidence.
    env.pop("GIT_DIFF_OPTS", None)
    return subprocess.run(  # noqa: S603 - fixed git argv, no shell
        ["git", "--no-replace-objects", *args],  # noqa: S607
        input=input_data,
        capture_output=True,
        env=env,
        check=check,
        timeout=5,
    ).stdout


def _commit_ids(review: str, current: str) -> tuple[str, str]:
    """Resolve recorded hex IDs to local commit objects and verify any abbreviation."""
    labels = ("review", "PR")
    requested = (review.lower(), current.lower())
    for label, oid in zip(labels, requested, strict=True):
        if not _OID.fullmatch(oid):
            raise ValueError(f"{label} head is not a hexadecimal commit ID")
    queries = f"{requested[0]}\n{requested[1]}\n".encode("ascii")
    records = _git("cat-file", "--batch-check=%(objectname) %(objecttype)", input_data=queries).decode("ascii")
    resolved = []
    for label, oid, record in zip(labels, requested, records.splitlines(), strict=True):
        full_id, _, object_type = record.partition(" ")
        if object_type != "commit" or not full_id.startswith(oid):
            raise ValueError(f"{label} head is unavailable or ambiguous in the local repository")
        resolved.append(full_id)
    return resolved[0], resolved[1]


def _old_changed_ranges(patch: bytes) -> list[tuple[int, int]]:
    r"""Return inclusive old-side line ranges affected by a zero-context patch.

    >>> _old_changed_ranges(b"@@ -2 +2 @@\n-old\n+new\n")
    [(2, 2)]
    >>> _old_changed_ranges(b"@@ -3,0 +4,2 @@\n+new\n+lines\n")
    []
    """
    ranges = []
    for start_text, count_text in _HUNK.findall(patch):
        start, count = int(start_text), int(count_text or b"1")
        if count:
            ranges.append((start, start + count - 1))
    return ranges


def _file_drift(
    review: str, current: str, path: str, change: bytes | None
) -> tuple[str, dict[str, int | None], list[tuple[int, int]]] | None:
    """Read the status, line counts, and changed review lines for one exact path."""
    if change is None:
        if _git("cat-file", "-t", f"{review}:{path}", check=False).strip() != b"blob":
            return None
        return "current", {"added": 0, "deleted": 0}, []

    old_mode, new_mode, _, _, status = change.split()
    # With renames disabled, the status filter excludes descendants when a file becomes a directory or vice versa.
    patch = _git(
        "diff",
        *_DIFF_OPTIONS,
        "--numstat",
        "--patch",
        "--unified=0",
        "--inter-hunk-context=0",
        "--diff-algorithm=myers",
        "--no-indent-heuristic",
        f"--diff-filter={status.decode('ascii')}",
        review,
        current,
        "--",
        f":(top,literal){path}",
    )
    added, deleted, _ = patch.partition(b"\n")[0].split(b"\t", 2)
    stat = {"added": None if added == b"-" else int(added), "deleted": None if deleted == b"-" else int(deleted)}
    if status == b"D":
        return "file-removed", stat, []
    # Symlink and submodule pseudo-lines do not refer to source lines.
    ranges = _old_changed_ranges(patch) if old_mode.startswith(b":100") and new_mode.startswith(b"100") else []
    return "file-changed-since-review", stat, ranges


def _classify(items_file: Path, review: str, current: str) -> list[dict]:
    """Build one advisory row per item whose repository path can be compared."""
    raw = _git("diff", *_DIFF_OPTIONS, "--raw", "-z", review, current, "--").split(b"\0")
    changes = {os.fsdecode(path): header for header, path in zip(raw[:-1:2], raw[1:-1:2], strict=True)}
    files = {}
    rows = []
    with items_file.open(encoding="utf-8") as stream:
        for record in stream:
            if not record.strip():
                continue
            item = json.loads(record)
            if not isinstance(item, dict) or type(item.get("id")) not in (int, str):
                raise ValueError("action item is missing a valid ID")
            path = item.get("file")
            if not isinstance(path, str) or "\0" in path or any(part in ("", ".", "..") for part in path.split("/")):
                continue
            if path not in files:
                files[path] = _file_drift(review, current, path, changes.get(path))
            evidence = files[path]
            if evidence is None:
                continue
            marker, stat, changed_ranges = evidence
            line_text = str(item.get("line", "")).strip()
            line = int(line_text) if re.fullmatch(r"[1-9][0-9]*", line_text) else None
            # GitHub comments have no recorded anchor revision in action-items.jsonl.
            if line is not None and "[gh]" not in str(item.get("type", "")):
                if any(start <= line <= end for start, end in changed_ranges):
                    marker = "already-fixed-candidate"
            rows.append({"id": item["id"], "path": path, "line": line, "marker": marker, "diff_stat": stat})
    return rows


def main(argv: list[str] | None = None) -> int:
    """Print drift evidence or report why the picker should omit markers."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--items", type=Path, required=True)
    parser.add_argument("--report-file", type=Path, required=True)
    parser.add_argument("--current-head", required=True)
    args = parser.parse_args(argv)
    try:
        recorded = args.report_file.with_name("head-sha.txt").read_text(encoding="utf-8").strip()
        review, current = _commit_ids(recorded, args.current_head)
        print(json.dumps(_classify(args.items, review, current)))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"resolve-item-drift: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
