#!/usr/bin/env python3
"""codemap_cache.py — shared review→resolve codemap pre-flight cache.

consumers: resolve/SKILL.md, resolve/modes/action-item-dispatch.md, _shared/codemap-context.md

Materializes the per-module structural-context artifacts that let ``oss:resolve``
reuse the pre-flight ``codemap-py query`` answers ``develop:review``/``oss:review``
already computed, instead of re-issuing the same queries.

Artifact shape — one file per module at
``<cache-dir>/<module>.json``, split into a stable *prefix* and a volatile
*delta* so a later cross-skill handoff feature generalizes without rework:

    {
      "module": "pkg.mod",
      "prefix": {                     # index-derived; content-hashed + index-stamped
        "git_sha": "<index git_sha>",
        "scanned_at": "<index ISO timestamp>",
        "index_stamp": "<canonical-path>:<size>:<mtime_ns> of the index file",
        "content_hash": "<sha256 of answers>",
        "answers": {"rdeps": {...}, "fn-rdeps": {...}, ...}
      },
      "delta": {                      # volatile; mutated as the consumer works
        "touched_files": [],
        "exhausted_queries": [],
        "notes": []
      }
    }

Freshness rule: an artifact is reusable when its ``prefix.scanned_at`` is not
older than the current index ``scanned_at`` (the index has not been rebuilt
since the artifact was written), its ``git_sha`` matches, and its
``index_stamp`` still equals the index file's ``<canonical-path>:<size>:<mtime_ns>``. A rebuilt
index (newer ``scanned_at``) invalidates every artifact — the consumer must
re-query.

The ``index_stamp`` is what makes the rule fail **closed** without trusting the
index's declared metadata: an index rewritten in place without advancing
``scanned_at`` (an ``--incremental`` re-scan, a restored backup, a manual edit)
is byte-different but metadata-identical, and the two declared fields alone
would happily reuse answers taken from the previous contents. Stat data needs no
knowledge of the index schema, so it also keeps working if the provider renames
either field.

Health metric: ``reuse_ratio`` = reused answers / total persisted answers,
printed by the ``report`` command for telemetry.

Subcommands:
    write   Split a ``codemap-py query batch`` result into per-module artifacts.
    read    Emit a reuse verdict + cached answers for one module.
    report  Emit the aggregate ``reuse_ratio`` health metric for a cache dir.

Usage:
    codemap_cache.py write  --batch <batch.json> --index <index.json> --cache-dir <dir>
    codemap_cache.py read   --module <dotted> --index <index.json> --cache-dir <dir>
    codemap_cache.py report --cache-dir <dir>

Exit codes:
    0   success (read: including a cold-miss verdict — miss is not an error)
    1   bad arguments or unreadable required input
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

# The per-module queries oss:review's pre-flight issues, and therefore the only
# `cmd` values `_module_answers_from_batch` will group. Authority is oss's own
# review block (skills/review/modes/codemap-context.md); tests assert the two
# stay equal. cc_develop's build_codemap_batch.py keeps a deliberately shorter
# list — this is NOT a mirror of it, and nothing here detects drift against it:
# grouping keys on the emitted `cmd`, so a batch from any producer contributes
# whichever of these queries it happens to carry.
PER_MODULE_QUERIES: tuple[str, ...] = (
    "rdeps",
    "fn-rdeps",
    "fn-blast",
    "mock-rdeps",
    "uncovered",
    "xrefs",
    "undocumented",
)

# Mirrors resolve_shared_path.py::_validate_subdir's shape. Leading '.' and
# '-' are legitimate in real module names (e.g. ".github.scripts.x",
# "plugins.codemap-py.bin.foo") so the class stays permissive; '..' and '\\'
# are rejected explicitly since a module builds a path under ``--cache-dir``.
_MODULE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _valid_module(module: str) -> bool:
    """Check whether a module name is safe for use in a cache path.

    Args:
        module: Dotted module name to validate.

    Returns:
        False when ``module`` contains anything outside ``_MODULE_RE``, or
        contains ``..`` or ``\\`` (path-escape guard for ``--cache-dir``).

    Examples:
        >>> _valid_module("pkg.mod")
        True
        >>> _valid_module(".github.scripts.x")
        True
        >>> _valid_module("../etc/passwd")
        False
        >>> _valid_module("a\\\\b")
        False
    """
    return bool(_MODULE_RE.match(module)) and ".." not in module and "\\" not in module


def _content_hash(answers: dict[str, object]) -> str:
    """Return a stable sha256 over the answer payload.

    Args:
        answers: Mapping of query name to its ``codemap-py query`` result payload.

    Returns:
        Hex sha256 of the canonically-serialized answers.

    Examples:
        >>> _content_hash({"rdeps": {"importers": []}})[:8]
        'bf9671a5'
    """
    canonical = json.dumps(answers, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _file_stamp(index_path: Path) -> str:
    """Bind an index's canonical local path and file-stat freshness into one opaque stamp.

    Deliberately schema-free: no key of the provider's index is read here, so this
    signal survives any rename or restructuring on the provider side and still
    changes whenever the file is rewritten.

    Args:
        index_path: Path to the codemap index JSON.

    Returns:
        ``"<canonical-path>:<size>:<mtime_ns>"``; old path-free stamps fail closed.
    """
    stat = index_path.stat()
    return f"{index_path.resolve().as_posix()}:{stat.st_size}:{stat.st_mtime_ns}"


def _index_stamp(index_path: Path) -> tuple[str, str, str]:
    """Read the freshness triple for a codemap index.

    ``git_sha`` and ``scanned_at`` are the index's own declared metadata; the third
    element is the schema-free file stamp from :func:`_file_stamp`. All three are
    compared on read — see the module docstring for why the declared pair alone is
    not sufficient.

    Args:
        index_path: Path to the codemap index JSON.

    Returns:
        ``(git_sha, scanned_at, index_stamp)``; the first two are empty strings
        when the field is absent.
    """
    meta = json.loads(index_path.read_text(encoding="utf-8"))
    git_sha = meta.get("git_sha")
    scanned_at = meta.get("scanned_at")
    # Do not turn absent or malformed coordinates into plausible identity strings.
    return (
        git_sha if isinstance(git_sha, str) else "",
        scanned_at if isinstance(scanned_at, str) else "",
        _file_stamp(index_path),
    )


def _result_module(result: dict) -> str:
    """Extract the owning module from one query result payload.

    Module-level queries (``rdeps``, ``uncovered``, ``mock-rdeps``, ``xrefs``,
    ``undocumented``) carry a dotted module under ``module``/``query``/``target``.
    Function-level queries (``fn-rdeps``, ``fn-blast``) instead carry a ``qname``
    of the form ``pkg.mod::Symbol`` — the module is the part before ``::``.

    Args:
        result: One ``codemap-py query`` result payload from a batch item.

    Returns:
        Dotted module name, or empty string when none can be derived.

    Examples:
        >>> _result_module({"query": "pkg.mod", "module": "ignored"})
        'pkg.mod'
        >>> _result_module({"qname": "pkg.mod::Thing"})
        'pkg.mod'
        >>> _result_module({"status": "error"})
        ''
    """
    module = result.get("query") or result.get("module") or result.get("target")
    if module:
        # mock-rdeps echoes a qualified "pkg.mod::Symbol" under module/target.
        return str(module).split("::", 1)[0]
    qname = result.get("qname")
    if qname and "::" in str(qname):
        return str(qname).split("::", 1)[0]
    return ""


def _module_answers_from_batch(batch: dict) -> dict[str, dict[str, object]]:
    """Group a ``codemap-py query batch`` result into per-module answer maps.

    The batch is ``central`` followed by seven queries per module, emitted in
    the ``PER_MODULE_QUERIES`` order. Grouping keys on the module each result
    payload names (see :func:`_result_module`) rather than positional
    arithmetic — robust to a missing ``central`` or a per-item parse error.

    Args:
        batch: Decoded ``codemap-py query batch`` output (``{"batch": [...], ...}``).

    Returns:
        Mapping of dotted module name to ``{query_name: result_payload}``.

    Examples:
        >>> batch = {"batch": [
        ...     {"cmd": "rdeps", "ok": True, "result": {"module": "pkg.mod"}},
        ...     {"cmd": "fn-rdeps", "ok": True,
        ...      "result": {"qname": "pkg.mod::Thing"}},
        ...     {"cmd": "rdeps", "ok": False, "result": {"module": "pkg.mod"}},
        ... ]}
        >>> _module_answers_from_batch(batch)
        {'pkg.mod': {'rdeps': {'module': 'pkg.mod'}, 'fn-rdeps': {'qname': 'pkg.mod::Thing'}}}
    """
    by_module: dict[str, dict[str, object]] = {}
    for item in batch.get("batch", []):
        cmd = item.get("cmd", "")
        if cmd not in PER_MODULE_QUERIES or not item.get("ok", False):
            continue
        module = _result_module(item.get("result", {}))
        if not module:
            continue
        by_module.setdefault(module, {})[cmd] = item.get("result", {})
    return by_module


def cmd_write(args: argparse.Namespace) -> int:
    """Split a batch result into per-module prefix/delta artifacts.

    Args:
        args: Namespace with ``batch``, ``index``, ``cache_dir``.

    Returns:
        Exit code (0 on success).
    """
    batch = json.loads(Path(args.batch).read_text(encoding="utf-8"))
    git_sha, scanned_at, index_stamp = _index_stamp(Path(args.index))
    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    by_module = _module_answers_from_batch(batch)
    written = 0
    for module, answers in by_module.items():
        if not _valid_module(module):
            # e.g. derive_modules_from_diff's flat-layout fallback can emit
            # "plugins/cc_oss/bin" — skip it, don't abort the whole batch.
            print(f"codemap_cache: skipping invalid module {module!r}", file=sys.stderr)
            continue
        artifact = {
            "module": module,
            "prefix": {
                "git_sha": git_sha,
                "scanned_at": scanned_at,
                "index_stamp": index_stamp,
                "content_hash": _content_hash(answers),
                "answers": answers,
            },
            "delta": {"touched_files": [], "exhausted_queries": [], "notes": []},
        }
        (cache_dir / f"{module}.json").write_text(json.dumps(artifact), encoding="utf-8")
        written += 1

    print(json.dumps({"status": "done", "modules_written": written}))
    return 0


def _reuse_verdict(artifact: dict, git_sha: str, scanned_at: str, index_stamp: str) -> tuple[bool, str]:
    """Decide whether an artifact may be reused against the current index.

    Args:
        artifact: Decoded per-module artifact.
        git_sha: Current index ``git_sha``.
        scanned_at: Current index ``scanned_at`` (ISO timestamp).
        index_stamp: Current index ``"<size>:<mtime_ns>"`` from :func:`_file_stamp`.

    Returns:
        ``(reuse, reason)`` — ``reuse`` False on any staleness signal.

    Examples:
        >>> answers = {"rdeps": {"importers": []}}
        >>> artifact = {"prefix": {
        ...     "git_sha": "abc", "scanned_at": "2026-01-01T00:00:00Z",
        ...     "index_stamp": "10:20", "answers": answers,
        ...     "content_hash": _content_hash(answers),
        ... }}
        >>> _reuse_verdict(artifact, "abc", "2026-01-01T00:00:00Z", "10:20")
        (True, 'fresh')
        >>> _reuse_verdict(artifact, "abc", "2026-01-01T00:00:00Z", "11:21")
        (False, 'index_stamp_mismatch')
    """
    prefix = artifact.get("prefix", {})
    art_sha = prefix.get("git_sha")
    art_scanned = prefix.get("scanned_at")
    if (
        not isinstance(art_sha, str)
        or not art_sha.strip()
        or not isinstance(git_sha, str)
        or not git_sha.strip()
        or not isinstance(art_scanned, str)
    ):
        return False, "missing_freshness_metadata"
    if art_sha != git_sha:
        return False, "git_sha_mismatch"
    # Compare actual instants, not strings: valid ISO timestamps can use different offsets.
    try:
        artifact_time = datetime.fromisoformat(art_scanned.replace("Z", "+00:00"))
        index_time = datetime.fromisoformat(scanned_at.replace("Z", "+00:00"))
        if artifact_time.tzinfo is None or index_time.tzinfo is None:
            return False, "invalid_freshness_metadata"
    except ValueError:
        return False, "invalid_freshness_metadata"
    if artifact_time < index_time:
        return False, "index_rebuilt"
    # Fail closed: an artifact written before this field existed has no stamp and
    # is re-queried rather than trusted. Catches an in-place rewrite that left
    # git_sha and scanned_at untouched — invisible to both checks above.
    if str(prefix.get("index_stamp", "")) != index_stamp:
        return False, "index_stamp_mismatch"
    if _content_hash(prefix.get("answers", {})) != prefix.get("content_hash", ""):
        return False, "content_hash_mismatch"
    return True, "fresh"


def cmd_read(args: argparse.Namespace) -> int:
    """Emit a reuse verdict and cached answers for one module.

    A cold miss (no artifact) is a ``reuse=false`` verdict, not an error — the
    consumer falls back to a live query. Exit stays 0 so the caller branches on
    the verdict, not the exit code.

    Args:
        args: Namespace with ``module``, ``index``, ``cache_dir``.

    Returns:
        Exit code (0 including cold miss).
    """
    if not _valid_module(args.module):
        print(json.dumps({"reuse": False, "reason": "invalid_module", "answers": {}}))
        return 0
    artifact_path = Path(args.cache_dir) / f"{args.module}.json"
    if not artifact_path.exists():
        print(json.dumps({"reuse": False, "reason": "cold_miss", "answers": {}}))
        return 0
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    reuse, reason = _reuse_verdict(artifact, *_index_stamp(Path(args.index)))
    answers = artifact.get("prefix", {}).get("answers", {}) if reuse else {}
    print(json.dumps({"reuse": reuse, "reason": reason, "answers": answers}))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Emit the aggregate reuse-ratio health metric for a cache dir.

    ``reuse_ratio`` = answers whose module artifact was read at least once
    (``delta.notes`` records a ``reused`` marker) over total persisted answers.
    A cache dir with no read markers reports ``reuse_ratio=0.0``.

    Args:
        args: Namespace with ``cache_dir``.

    Returns:
        Exit code (0 on success).
    """
    cache_dir = Path(args.cache_dir)
    total = 0
    reused = 0
    for artifact_path in sorted(cache_dir.glob("*.json")):
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        answers = artifact.get("prefix", {}).get("answers", {})
        total += len(answers)
        if any(str(n).startswith("reused") for n in artifact.get("delta", {}).get("notes", [])):
            reused += len(answers)
    ratio = round(reused / total, 3) if total else 0.0
    print(json.dumps({"reuse_ratio": ratio, "answers_reused": reused, "answers_total": total}))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to the requested subcommand.

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Exit code.
    """
    parser = argparse.ArgumentParser(description="review→resolve codemap pre-flight cache.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_write = sub.add_parser("write", help="Split a codemap-py query batch into per-module artifacts.")
    p_write.add_argument("--batch", required=True, help="Path to codemap-py query batch output JSON.")
    p_write.add_argument("--index", required=True, help="Path to the codemap index JSON.")
    p_write.add_argument("--cache-dir", required=True, help="Directory to write per-module artifacts to.")

    p_read = sub.add_parser("read", help="Emit reuse verdict + cached answers for a module.")
    p_read.add_argument("--module", required=True, help="Dotted module name.")
    p_read.add_argument("--index", required=True, help="Path to the codemap index JSON.")
    p_read.add_argument("--cache-dir", required=True, help="Directory holding per-module artifacts.")

    p_report = sub.add_parser("report", help="Emit the aggregate reuse_ratio health metric.")
    p_report.add_argument("--cache-dir", required=True, help="Directory holding per-module artifacts.")

    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "write":
            return cmd_write(args)
        if args.command == "read":
            return cmd_read(args)
        return cmd_report(args)
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
