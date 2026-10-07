#!/usr/bin/env python
"""smoke_test_index.py — validate a codemap index file and report mtime staleness.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/smoke_test_index.py" --index-path <path> [--max-age-hours N]

Behaviour:
    1. Smoke test — open the index path and json.load it; reject if missing,
       unreadable, not a JSON object, or empty.
    2. Staleness — compare filesystem mtime against current wall-clock; flag stale
       when age exceeds ``--max-age-hours`` (default: 24).

Output:
    Single JSON object on stdout, e.g.::

        {"ok": true,  "stale": false, "age_hours": 2.31, "path": "<path>"}
        {"ok": false, "stale": false, "age_hours": null,  "path": "<path>",
         "error": "index file not found"}

Exit codes:
    0  index ok and not stale.
    1  index missing/invalid OR stale.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Index age in hours above which the smoke test reports the index as stale; default of ``--max-age-hours``.
DEFAULT_MAX_AGE_HOURS = 24
#: DoS guard, held at the query engine's ceiling (``query._MAX_INDEX_SIZE_BYTES``):
#: a helper that refuses an index the engine serves reports a healthy project as broken.
MAX_INDEX_SIZE = 512 * 1024 * 1024


def _is_user_owned(path: Path) -> bool:
    """Return ``True`` iff ``path`` is owned by the current real user.

    Used to gate world-writable temp directories: on a multi-user system
    ``/tmp`` is shared, so another user could plant a file there. Requiring the
    file's owner to match the caller blocks that vector.

    Args:
        path: file path to inspect.

    Returns:
        ``True`` when ``path.stat().st_uid`` equals the current uid; ``False`` on
        any stat error.
    """
    try:
        if not hasattr(os, "getuid"):
            return True  # Windows: ownership check via UID unsupported
        return path.stat().st_uid == os.getuid()
    except OSError:
        return False


def _validate_index_path(raw: str) -> Path | None:
    """Resolve and validate that ``raw`` stays within a safe base directory.

    Permitted base directories (any one is sufficient):
      * The current working directory (treated as the repository root)
      * ``~/.claude`` (where codemap indices typically live)
      * The OS temporary directory (``tempfile.gettempdir()``) — needed for
        pytest's ``tmp_path`` fixture and other sandboxed test runs.

    Paths resolving under the shared temp directory (``tempfile.gettempdir()`` or
    ``/tmp``) are additionally required to be owned by the current user,
    since those directories are world-writable on multi-user systems.

    Args:
        raw: User-supplied path from argv.

    Returns:
        Resolved ``Path`` if validation succeeds; ``None`` if the path is empty,
        does not point at a file, resolves outside every allowed base, or lives in
        a shared temp directory but is not owned by the current user.
    """
    if not raw:
        return None
    candidate = Path(raw).expanduser().resolve()
    if not candidate.is_file():
        return None
    tempdir = Path(tempfile.gettempdir()).resolve()
    ownership_gated_roots = {tempdir, Path("/tmp").resolve()}  # noqa: S108 - macOS /tmp symlink target
    allowed_roots = [
        Path.cwd().resolve(),
        (Path(os.path.expanduser("~")) / ".claude").resolve(),
        tempdir,
    ]
    for root in allowed_roots:
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        # Shared temp dirs are world-writable — require current-user ownership.
        if root in ownership_gated_roots and not _is_user_owned(candidate):
            return None
        return candidate
    return None


@dataclass(frozen=True)
class SmokeResult:
    """Outcome of an index smoke test.

    Attributes:
        ok: True iff the index file exists, is readable, and parses as a non-empty JSON object.
        stale: True iff the file mtime is older than ``max_age_hours`` (only meaningful when ``ok``).
        age_hours: Filesystem age of the index file in hours, rounded to 2 dp. ``None`` if file missing.
        path: Absolute path of the index that was inspected.
        error: Short human-readable diagnostic when ``ok`` is False; ``None`` otherwise.
    """

    ok: bool
    stale: bool
    age_hours: float | None
    path: str
    error: str | None = None

    def to_json(self) -> str:
        """Serialize to a single-line JSON string.

        Returns:
            JSON object string with stable field order.

        Examples:
            >>> SmokeResult(True, False, 1.5, "/x.json").to_json()
            '{"ok": true, "stale": false, "age_hours": 1.5, "path": "/x.json"}'
            >>> SmokeResult(False, False, None, "/x.json", "missing").to_json()
            '{"ok": false, "stale": false, "age_hours": null, "path": "/x.json", "error": "missing"}'
        """
        payload: dict[str, Any] = {
            "ok": self.ok,
            "stale": self.stale,
            "age_hours": self.age_hours,
            "path": self.path,
        }
        if self.error is not None:
            payload["error"] = self.error
        return json.dumps(payload)


def compute_age_hours(mtime: float, now: float) -> float:
    """Return file age in hours, rounded to 2 decimal places, clamped at 0.

    Args:
        mtime: filesystem mtime (POSIX seconds since epoch).
        now: current wall-clock time (POSIX seconds since epoch).

    Returns:
        Non-negative age in hours.

    Examples:
        >>> compute_age_hours(0.0, 3600.0)
        1.0
        >>> compute_age_hours(0.0, 8190.0)
        2.27
        >>> compute_age_hours(100.0, 50.0)
        0.0
    """
    return round(max(0.0, (now - mtime) / 3600.0), 2)


def smoke_test_index(index_path: Path, max_age_hours: int, now: float | None = None) -> SmokeResult:
    """Run smoke test on a codemap index file.

    Smoke test sequence (any failure short-circuits with ``ok=False``):

    1. File exists and is a regular file.
    2. File contents parse as JSON.
    3. Parsed value is a dict and non-empty.

    Staleness is computed only when the smoke test passes.

    Args:
        index_path: path to the codemap index JSON.
        max_age_hours: age threshold (hours) above which the index is reported stale.
        now: POSIX timestamp used as "current time" — overridable for tests.

    Returns:
        :class:`SmokeResult` describing validity and staleness.
    """
    abs_path = str(index_path.resolve()) if index_path.exists() else str(index_path)

    if not index_path.exists() or not index_path.is_file():
        return SmokeResult(ok=False, stale=False, age_hours=None, path=abs_path, error="index file not found")

    # DoS guard: refuse oversized index files before json.load to avoid memory exhaustion.
    try:
        if index_path.stat().st_size > MAX_INDEX_SIZE:
            return SmokeResult(
                ok=False,
                stale=False,
                age_hours=None,
                path=abs_path,
                error=f"index too large ({index_path.stat().st_size} bytes; max {MAX_INDEX_SIZE})",
            )
    except OSError as exc:
        return SmokeResult(ok=False, stale=False, age_hours=None, path=abs_path, error=f"stat failed: {exc}")

    try:
        with index_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return SmokeResult(ok=False, stale=False, age_hours=None, path=abs_path, error=f"unreadable index: {exc}")

    if not isinstance(data, dict) or not data:
        return SmokeResult(
            ok=False, stale=False, age_hours=None, path=abs_path, error="index payload empty or not a JSON object"
        )

    current = time.time() if now is None else now
    age_hours = compute_age_hours(index_path.stat().st_mtime, current)
    stale = age_hours > max_age_hours
    return SmokeResult(ok=True, stale=stale, age_hours=age_hours, path=abs_path)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: optional argv override for testing. Defaults to ``sys.argv[1:]``.

    Returns:
        Exit code (0 = ok & fresh, 1 = invalid OR stale).
    """
    parser = argparse.ArgumentParser(
        description="Smoke-test a codemap index file and report mtime staleness.",
    )
    parser.add_argument("--index-path", required=True, help="Path to the codemap index JSON file.")
    parser.add_argument(
        "--max-age-hours",
        type=int,
        default=DEFAULT_MAX_AGE_HOURS,
        help=f"Hours above which the index is considered stale (default: {DEFAULT_MAX_AGE_HOURS}).",
    )
    args = parser.parse_args(argv)

    validated = _validate_index_path(args.index_path)
    if validated is None:
        abs_path = str(Path(args.index_path).expanduser().resolve()) if args.index_path else args.index_path
        result = SmokeResult(
            ok=False,
            stale=False,
            age_hours=None,
            path=abs_path,
            error="index file not found or outside allowed roots (cwd, ~/.claude, tempdir)",
        )
        print(result.to_json())
        return 1

    result = smoke_test_index(validated, max_age_hours=args.max_age_hours)
    print(result.to_json())
    return 0 if (result.ok and not result.stale) else 1


if __name__ == "__main__":
    sys.exit(main())
