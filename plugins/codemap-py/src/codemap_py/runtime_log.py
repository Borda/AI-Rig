#!/usr/bin/env python
"""runtime_log.py — runtime-scoped codemap CLI logging (claude / codex / direct).

The only runtime-specific persistent project state is the logging subtree. This
module resolves a per-runtime log directory under the canonical project root so
Claude Code, Codex, and direct CLI never append to the same file, and a missing
session id never collapses concurrent writers into one unqualified ``cli.jsonl``.

``bin/_runtime_log.py`` is a compatibility shim that aliases this module in
``sys.modules``.

Contract:

- runtime identity is an allowlist ``{claude, codex, direct}``; an invalid value
  falls back to ``direct`` with a bounded diagnostic and never becomes a path
  component;
- the log root is ``<canonical-root>/.cache/codemap/logs`` regardless of CWD;
- ``CODEMAP_LOG_DIR`` overrides the log root but the ``<runtime>/`` component is
  still appended; a *relative* override is anchored to the canonical root for the
  same reason the default is;
- log correlation uses the runtime's opaque session id when available, otherwise a
  process/invocation id;
- logging failure never blocks index build, validation, reuse, or query.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
from pathlib import Path

from codemap_py.index_paths import Diagnostic, canonical_root

#: Runtime identities accepted when naming a log directory.
RUNTIME_ALLOWLIST = ("claude", "codex", "direct")
#: Runtime identity used when none or an unknown one is supplied.
DEFAULT_RUNTIME = "direct"
#: Diagnostic code reported when a supplied runtime identity is not in the allowlist.
INVALID_RUNTIME = "invalid_runtime_identity"
#: Default log directory, relative to the anchor root, when no override is configured.
LOG_SUBDIR = Path(".cache", "codemap", "logs")
#: The one environment variable every logging layer honours as a log-root override.
#: Named here so ``telemetry`` and ``query`` read the same key through the same
#: resolver instead of each repeating the literal beside its own default.
LOG_DIR_ENV = "CODEMAP_LOG_DIR"

#: Matches characters outside letters, digits, underscore and hyphen, replaced when building log file names.
_SAFE = re.compile(r"[^A-Za-z0-9_-]")
#: Cached (pid, id) pair giving this process one stable invocation id, regenerated after a pid change.
_INVOCATION: tuple[int, str] | None = None
#: Cached plugin version read from the manifest on first use; "?" when it cannot be read.
_PLUGIN_VERSION: str | None = None


def resolve_runtime(raw: object) -> tuple[str, Diagnostic | None]:
    """Return an allowlisted runtime identity and any fallback diagnostic.

    Args:
        raw: Requested runtime identity (any value; typically a string or ``None``).

    Returns:
        ``(runtime, diagnostic)`` where *runtime* is always in
        :data:`RUNTIME_ALLOWLIST` and *diagnostic* is ``None`` unless *raw* was
        invalid and fell back to ``direct``.

    Examples:
        >>> resolve_runtime("codex")
        ('codex', None)
        >>> runtime, diag = resolve_runtime("bogus")
        >>> runtime, diag.code
        ('direct', 'invalid_runtime_identity')
    """
    if isinstance(raw, str) and raw in RUNTIME_ALLOWLIST:
        return raw, None
    return DEFAULT_RUNTIME, Diagnostic(
        INVALID_RUNTIME,
        f"unknown runtime identity {raw!r}; using {DEFAULT_RUNTIME!r}",
        {"requested": str(raw)},
    )


def invocation_id() -> str:
    """Return a stable per-process invocation id (``<pid>-<time_ns>``).

    Cached per process and regenerated after a fork so parent and child never
    share a correlation id.

    Returns:
        The process invocation id string.

    Examples:
        >>> invocation_id() == invocation_id()
        True
    """
    global _INVOCATION
    pid = os.getpid()
    if _INVOCATION is None or _INVOCATION[0] != pid:
        _INVOCATION = (pid, f"{pid}-{time.time_ns()}")
    return _INVOCATION[1]


def plugin_version() -> str:
    """Return the installed plugin version, or ``"?"`` when it cannot be read."""
    global _PLUGIN_VERSION
    if _PLUGIN_VERSION is None:
        try:
            manifest = Path(__file__).resolve().parents[2] / ".claude-plugin" / "plugin.json"
            _PLUGIN_VERSION = str(json.loads(manifest.read_text(encoding="utf-8")).get("version", "?"))
        except (OSError, TypeError, ValueError, AttributeError):
            _PLUGIN_VERSION = "?"
    return _PLUGIN_VERSION


def log_root(*, root: Path | None = None, override: str | None = None) -> Path:
    """Return the project-anchored log root shared by every codemap logging layer.

    This is the single resolver behind the CLI (:mod:`codemap_py.telemetry`), the
    query engine (:mod:`codemap_py.query`), and the runtime-scoped writer
    (:func:`log_dir_for`). Each of them previously spelled its own
    ``Path(os.environ.get("CODEMAP_LOG_DIR", ".cache/codemap/logs"))``, and a bare
    relative path resolves against the *process* CWD: a session started in a
    subdirectory wrote its shards to ``<subdir>/.cache/codemap/logs`` while the
    hooks (started at the repo root) wrote to ``<root>/.cache/codemap/logs``. The
    layers still joined on one session key, but across two directories — so
    ``debrief-coding`` read one half of a session and reported the other half as
    absent. Anchoring to the canonical root is what keeps the three layers in one
    directory no matter where the process was launched.

    The anchor is the git top-level (via
    :func:`codemap_py.index_paths.canonical_root`), the same convention
    ``query.find_index`` uses for ``<git toplevel>/.cache/codemap/``, so the logs
    sit beside the index they describe.

    Args:
        root: Canonical project root; resolved from the CWD when omitted.
        override: Explicit log-root override; ``None`` reads :data:`LOG_DIR_ENV`.
            An absolute override is honoured verbatim (it is already unambiguous);
            a relative one is anchored to *root* for the same reason the default is.

    Returns:
        The absolute log-root directory (no ``<runtime>/`` component).

    Examples:
        >>> log_root(root=Path("/proj"), override="").as_posix()
        '/proj/.cache/codemap/logs'
        >>> log_root(root=Path("/proj"), override="/shared/logs").as_posix()
        '/shared/logs'
        >>> log_root(root=Path("/proj"), override="build/logs").as_posix()
        '/proj/build/logs'
    """
    raw = override if override is not None else os.environ.get(LOG_DIR_ENV)
    anchor = root if root is not None else canonical_root()
    if not raw:
        return anchor / LOG_SUBDIR
    expanded = Path(raw).expanduser()
    return expanded if expanded.is_absolute() else anchor / expanded


def log_dir_for(runtime: object, *, root: Path | None = None, override: str | None = None) -> Path:
    """Return the ``<log-root>/<runtime>/`` directory for *runtime*.

    Args:
        runtime: Requested runtime identity; normalized through
            :func:`resolve_runtime` so an invalid value maps to ``direct``.
        root: Canonical project root; resolved from the CWD when omitted.
        override: Explicit log-root override; defaults to ``CODEMAP_LOG_DIR``.

    Returns:
        The per-runtime log directory path (the ``<runtime>/`` component is always
        appended, including under an override).

    Examples:
        >>> log_dir_for("claude", root=Path("/proj")).as_posix()
        '/proj/.cache/codemap/logs/claude'
        >>> log_dir_for("codex", root=Path("/proj"), override="/shared/logs").as_posix()
        '/shared/logs/codex'
    """
    resolved, _ = resolve_runtime(runtime)
    return log_root(root=root, override=override) / resolved


def log_path(
    runtime: object,
    session: str | None = None,
    *,
    root: Path | None = None,
    override: str | None = None,
) -> Path:
    """Return the ``cli_<session-or-invocation>.jsonl`` path for *runtime*.

    Args:
        runtime: Requested runtime identity (normalized to the allowlist).
        session: Opaque session id when the runtime exposes one; a missing
            session falls back to a unique per-process invocation id so
            concurrent writers never collapse into one unqualified file.
        root: Canonical project root; resolved from the CWD when omitted.
        override: Explicit log-root override; defaults to ``CODEMAP_LOG_DIR``.

    Returns:
        The resolved per-session log file path.

    Examples:
        >>> log_path("claude", "sess/1", root=Path("/proj")).name
        'cli_sess-1.jsonl'
    """
    token = _SAFE.sub("-", session) if session else invocation_id()
    return log_dir_for(runtime, root=root, override=override) / f"cli_{token}.jsonl"


def write_log(
    runtime: object,
    record: dict,
    *,
    session: str | None = None,
    root: Path | None = None,
    override: str | None = None,
) -> Diagnostic | None:
    """Append one runtime-scoped log record; best-effort, never raises.

    Honors ``CODEMAP_LOGGING=false``. The stored record is stamped with a UTC
    timestamp and the resolved runtime; an invalid runtime is logged under
    ``direct`` and the returned diagnostic reports the fallback.

    Args:
        runtime: Requested runtime identity (normalized to the allowlist).
        record: JSON-serializable fields to merge into the stored record.
        session: Opaque session id, or ``None`` to use the invocation id.
        root: Canonical project root; resolved from the CWD when omitted.
        override: Explicit log-root override; defaults to ``CODEMAP_LOG_DIR``.

    Returns:
        A :class:`Diagnostic` when *runtime* was invalid, else ``None``.
    """
    resolved, diag = resolve_runtime(runtime)
    if os.environ.get("CODEMAP_LOGGING", "true").lower() == "false":
        return diag
    try:
        path = log_path(resolved, session, root=root, override=override)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "runtime": resolved,
            "v": plugin_version(),
        }
        if isinstance(record, dict):
            payload.update(record)
        # The caller may add event metadata but cannot forge the shard identity.
        payload["runtime"] = resolved
        payload["v"] = plugin_version()
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, separators=(",", ":")) + "\n")
    except Exception:  # noqa: S110 - best-effort diagnostics; failure ignored
        pass
    return diag


#: Hooks whose health ``codemap-py doctor`` reports, in output order. Mirrors ``hooks/_hookutil.HOOK_NAMES``: the hooks
#: run without this package, so the roster is restated there and pinned against this one by a test.
HOOK_NAMES = (
    "guard-redundant-scan",
    "inject-preamble",
    "log-skill-start",
    "log-tool-use",
    "record-exhausted",
    "seed-session",
)
#: Runtimes whose hook health doctor reads; ``direct`` CLI runs have no hooks.
HOOK_RUNTIMES = ("claude", "codex")
#: Per-runtime subdirectory holding one directory per hook. Mirrors ``hooks/_hookutil.HOOK_HEALTH_SUBDIR``.
HOOK_HEALTH_SUBDIR = "hook-health"
#: Failure records a hook keeps; the reported count never exceeds it. Mirrors ``_hookutil.HOOK_FAILURE_RETENTION``.
HOOK_FAILURE_RETENTION = 50
#: Name of the per-hook last-clean-run stamp. Mirrors ``hooks/_hookutil.HOOK_SUCCESS_STAMP``.
HOOK_SUCCESS_STAMP = "last-success"
#: Shape of a failure record name (epoch ns, random token, exception class). Mirrors ``_hookutil.HOOK_FAILURE_NAME``;
#: anything else in the directory is ignored, so no file content or foreign name ever reaches doctor output.
_HOOK_FAILURE_NAME = re.compile(r"fail-(\d{20})-[0-9a-f]{8}-([A-Za-z_][A-Za-z0-9_]{0,63})")
#: Shape of the last-success stamp content: epoch nanoseconds, zero-padded to 20 digits.
_HOOK_STAMP = re.compile(r"\d{20}")
#: Most directory entries read per hook, so a directory whose trimming stopped cannot make doctor slow.
_HOOK_SCAN_LIMIT = 1000
#: Most bytes read from a last-success stamp.
_HOOK_STAMP_MAX_BYTES = 64


def _is_plain_directory(path: Path) -> bool:
    """Return whether *path* is a directory itself, not a link, junction, FIFO or regular file, without raising."""
    try:
        info = path.lstat()
    except (OSError, ValueError):
        return False
    return stat.S_ISDIR(info.st_mode) and not getattr(info, "st_reparse_tag", 0)


def _utc_text(epoch_ns: str) -> str | None:
    """Return the ISO-8601 UTC second of a zero-padded epoch-nanosecond string, or ``None`` when out of range.

    The supported range of ``time.gmtime`` is platform-dependent, so an out-of-range value is dropped rather than
    raised.

    Examples:
        >>> _utc_text("00000000000000000000")
        '1970-01-01T00:00:00Z'
        >>> _utc_text("01700000000000000000")
        '2023-11-14T22:13:20Z'
    """
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(epoch_ns) // 1_000_000_000))
    except (OverflowError, OSError, ValueError):
        return None


def _read_stamp(path: Path) -> str | None:
    """Return the UTC time held by one last-success stamp, or ``None`` for a missing, odd or unreadable stamp.

    The stamp is ``lstat``-checked before any open, so a FIFO or link planted at its path is never opened and cannot
    make doctor hang; the open is also non-blocking and the opened file is re-checked against the inspected one.
    """
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
        try:
            if not os.path.samestat(before, os.fstat(descriptor)):
                return None
            raw = os.read(descriptor, _HOOK_STAMP_MAX_BYTES)
        finally:
            os.close(descriptor)
    except (OSError, ValueError):
        return None
    text = raw.decode("ascii", errors="replace").strip()
    return _utc_text(text) if _HOOK_STAMP.fullmatch(text) else None


def _failure_records(directory: Path) -> list[tuple[str, str]]:
    """Return the ``(epoch_ns, class_name)`` pairs of the valid failure records in one hook directory.

    Only regular files whose names match the record shape count; at most :data:`_HOOK_SCAN_LIMIT` entries are read.
    """
    records: list[tuple[str, str]] = []
    try:
        with os.scandir(directory) as entries:
            for position, entry in enumerate(entries):
                if position >= _HOOK_SCAN_LIMIT:
                    break
                match = _HOOK_FAILURE_NAME.fullmatch(entry.name)
                if match and entry.is_file(follow_symlinks=False):
                    records.append((match.group(1), match.group(2)))
    except (OSError, ValueError):
        return []
    return records


def _empty_hook_health() -> dict[str, object]:
    """Return the health entry of a hook with no readable record: zero failures, no times."""
    return {"count": 0, "last_class": None, "last_time": None, "last_success": None}


def _hook_health_entry(directory: Path) -> dict[str, object]:
    """Return one hook's ``count``, ``last_class``, ``last_time`` and ``last_success`` from its health directory."""
    entry = _empty_hook_health()
    if not _is_plain_directory(directory):
        return entry
    records = _failure_records(directory)
    if records:
        newest_ns, newest_class = max(records)
        entry.update(count=min(len(records), HOOK_FAILURE_RETENTION), last_class=newest_class)
        entry["last_time"] = _utc_text(newest_ns)
    entry["last_success"] = _read_stamp(directory / HOOK_SUCCESS_STAMP)
    return entry


def hook_health(runtime: object, *, root: Path | None = None, override: str | None = None) -> dict[str, dict]:
    """Return the failure count, last failure and last clean run of every hook for one runtime, never raising.

    The hooks record under ``<log-dir>/<runtime>/hook-health/<hook>/`` (``hooks/_hookutil.record_failure``). The read
    is bounded and identity-free: names that do not match the record shape, links, FIFOs and corrupt stamps all read
    as nothing, and only an exception class name and UTC times leave this function. ``count`` is capped at
    :data:`HOOK_FAILURE_RETENTION`, the number of records each hook keeps.

    Args:
        runtime: Runtime identity, normalized through :func:`resolve_runtime`.
        root: Canonical project root; resolved from the CWD when omitted.
        override: Explicit log-root override; defaults to ``CODEMAP_LOG_DIR``.

    Returns:
        One entry per name in :data:`HOOK_NAMES`, each with ``count`` (int), ``last_class``, ``last_time`` and
        ``last_success`` (str or ``None``; times are ISO-8601 UTC seconds).

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     health = hook_health("codex", root=Path(d), override="")
        >>> health["log-tool-use"]
        {'count': 0, 'last_class': None, 'last_time': None, 'last_success': None}
    """
    try:
        base = log_dir_for(runtime, root=root, override=override) / HOOK_HEALTH_SUBDIR
    except (OSError, RuntimeError, ValueError):
        return {hook: _empty_hook_health() for hook in HOOK_NAMES}
    if not _is_plain_directory(base):
        return {hook: _empty_hook_health() for hook in HOOK_NAMES}
    return {hook: _hook_health_entry(base / hook) for hook in HOOK_NAMES}
