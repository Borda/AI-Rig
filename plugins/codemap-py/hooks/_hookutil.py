#!/usr/bin/env python3
"""_hookutil.py — helpers shared by the codemap-py Claude and Codex hooks.

Every hook here is launched as ``python "<plugin-root>/hooks/<name>.py"``, so
``hooks/`` is already ``sys.path[0]`` and a bare ``import _hookutil`` resolves with
no path manipulation. That is the only reason this module can be shared at all:
the hooks deliberately do NOT import :mod:`codemap_py`, because they fire on every
Grep/Read/Glob/Bash call and must stay free of package imports and subprocesses.

What lives here is exactly the logic that MUST agree across hooks — the project
anchor, runtime identity, the telemetry log directory, and session/sentinel
sanitizer. Each was previously copy-pasted per hook, and every divergence between copies was a silent join failure
rather than an error: shards written under two different keys, or into two different directories, still look like
perfectly healthy telemetry.

It also owns hook health: :func:`hook_outcome` wraps each hook body, counting swallowed failures and stamping the
last clean run under the runtime log directory, where ``codemap-py doctor`` reads them. Without it a hook that failed
on every call looked exactly like a hook that had nothing to do.

consumers: hooks/{seed-session,log-tool-use,log-skill-start,guard-redundant-scan,record-exhausted}.py — imported as bare
``_hookutil``; not a standalone executable
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

#: Characters that must never reach a shard name, a sentinel filename, or a session key.
#: The session id is host-supplied, and the same substitution has to run in every layer
#: that names a file after it — two layers sanitizing differently write two files and
#: join on neither. Mirrors ``codemap_py.telemetry._SAFE``.
UNSAFE_KEY = re.compile(r"[^A-Za-z0-9_-]")

#: Log root relative to the project anchor. Mirrors ``codemap_py.runtime_log.LOG_SUBDIR``;
#: the two layers write shards that are joined on one session key, so they must agree.
LOG_SUBDIR = Path(".cache", "codemap", "logs")
#: Environment override for the log root, honoured identically by the CLI layer
#: (``codemap_py.runtime_log.LOG_DIR_ENV``) — a shard that ignored it would land
#: outside the directory ``debrief-coding`` reads.
LOG_DIR_ENV = "CODEMAP_LOG_DIR"
#: Host runtime names a hook may record; any other value falls back to :data:`DEFAULT_RUNTIME`.
RUNTIME_ALLOWLIST = frozenset({"claude", "codex"})
#: Runtime name used when the host does not identify itself or reports an unrecognised one.
DEFAULT_RUNTIME = "claude"

#: Every hook that records its own health, by script stem. Mirrors ``codemap_py.runtime_log.HOOK_NAMES``, which
#: ``codemap-py doctor`` reports in this order; a name outside this set is never turned into a path component.
HOOK_NAMES = (
    "guard-redundant-scan",
    "inject-preamble",
    "log-skill-start",
    "log-tool-use",
    "record-exhausted",
    "seed-session",
)
#: Subdirectory of the runtime log directory holding one directory per hook. Mirrors
#: ``codemap_py.runtime_log.HOOK_HEALTH_SUBDIR``. It holds no ``*.jsonl`` file, so no telemetry reader that
#: enumerates the log directory (anonymizer export, debrief, join) ever picks a record up as a shard.
HOOK_HEALTH_SUBDIR = "hook-health"
#: Failure records kept per hook; each write deletes the oldest beyond this count. Mirrors
#: ``codemap_py.runtime_log.HOOK_FAILURE_RETENTION``.
HOOK_FAILURE_RETENTION = 50
#: Name of the per-hook stamp holding the UTC time, in epoch nanoseconds, of the last run that ended cleanly.
#: Mirrors ``codemap_py.runtime_log.HOOK_SUCCESS_STAMP``.
HOOK_SUCCESS_STAMP = "last-success"
#: Shape of one failure record name: ``fail-<epoch-ns, 20 digits>-<8 hex>-<exception class>``. The record is an
#: empty file, so its name is the whole record: the class name and the UTC time, never a message, path or identity.
#: Mirrors ``codemap_py.runtime_log._HOOK_FAILURE_NAME``.
HOOK_FAILURE_NAME = re.compile(r"fail-(\d{20})-[0-9a-f]{8}-([A-Za-z_][A-Za-z0-9_]{0,63})")
#: Characters an exception class name may not carry into a record name.
_UNSAFE_CLASS = re.compile(r"[^A-Za-z0-9_]")
#: Longest exception class name kept in a record name, so the name stays short on every filesystem.
_CLASS_NAME_MAX = 64


def runtime() -> str:
    """Return the allowlisted hook runtime, defaulting safely to Claude."""
    value = os.environ.get("CODEMAP_RUNTIME", "").strip().lower()
    return value if value in RUNTIME_ALLOWLIST else DEFAULT_RUNTIME


def runtime_session(payload: dict | None = None, *, telemetry: bool = False) -> str:
    """Return the host session identity without crossing runtime marker boundaries.

    Codex events must never inherit Claude's persisted marker: a Codex thread is supplied by ``CODEX_THREAD_ID`` or the
    hook payload. Claude telemetry prefers event ``session_id``, then ``CLAUDE_CODE_SESSION_ID`` and ``CSID``. Other
    Claude consumers retain event-only identity so their project-scoped fallback keys remain unchanged. Absent identity
    stays empty for consumers to handle without borrowing another runtime's marker.
    """
    event = payload or {}
    if runtime() == "codex":
        for value in (
            os.environ.get("CODEX_THREAD_ID"),
            event.get("thread_id"),
            event.get("session_id"),
        ):
            session = str(value or "").strip()
            if session:
                return session
        return ""
    values = (event.get("session_id"),)
    if telemetry:
        values += (os.environ.get("CLAUDE_CODE_SESSION_ID"), os.environ.get("CSID"))
    for value in values:
        session = str(value or "").strip()
        if session:
            return session
    return ""


def project_root(cwd: Path | None = None) -> Path:
    """Return the nearest enclosing git root, or *cwd* itself outside a repository.

    The root is found by walking for ``.git`` rather than by running ``git rev-parse``:
    the logging hooks fire on every Grep/Read/Glob call, and a subprocess per call is
    exactly the cost their contract forbids. ``.git`` is matched as a file too, which
    is how linked worktrees and submodules mark their root.

    Equivalent in result to :func:`codemap_py.index_paths.canonical_root`, which the
    CLI layer uses — both resolve symlinks and both land on the git top-level — so the
    hook layer and the CLI layer agree on one project identity.

    Args:
        cwd: Directory to resolve from; defaults to the process working directory.

    Returns:
        The resolved git-root path, or the resolved starting directory outside a repo.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     nested = Path(d) / "myproj" / "src" / "pkg"
        ...     nested.mkdir(parents=True)
        ...     (Path(d) / "myproj" / ".git").mkdir()
        ...     project_root(nested).name
        'myproj'
    """
    start = (cwd or Path.cwd()).resolve()
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return start


def project_name(cwd: Path | None = None) -> str:
    """Return the project key: the basename of the nearest enclosing git root.

    Claude's ``seed-session.py`` writes the session marker under this key and every
    Claude reader — the logging hooks and ``codemap_py.telemetry`` — must reproduce it exactly. Keying
    on ``Path.cwd().name`` instead, as three hooks each used to do in their own copy,
    meant a session started in a subdirectory looked for a marker nobody had written:
    records landed in an unsuffixed shard and the cross-layer join returned nothing,
    with every hook still reporting success.

    Args:
        cwd: Directory to resolve from; defaults to the process working directory.

    Returns:
        The git-root basename, or the starting directory's own basename outside a repo.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     nested = Path(d) / "myproj" / "src"
        ...     nested.mkdir(parents=True)
        ...     (Path(d) / "myproj" / ".git").mkdir()
        ...     project_name(nested)
        'myproj'
    """
    return project_root(cwd).name


def session_key(session_id: object) -> str:
    """Return the sanitized sentinel key for *session_id*, or a session-scoped fallback.

    A missing session id must not collapse to one machine-global key: two projects
    running concurrently would then share sentinels and deny each other's queries.
    The fallback is scoped by project directory and by the Claude Code session token.

    Args:
        session_id: Host-supplied session identifier; may be absent or unsanitized.

    Returns:
        A filename-safe key, never empty.

    Examples:
        >>> session_key("abc-123")
        'abc-123'
        >>> session_key("../../etc/passwd")
        '------etc-passwd'
        >>> session_key(None) not in ("", "nosession")
        True
    """
    key = UNSAFE_KEY.sub("-", str(session_id or "").strip())
    if key:
        return key
    if runtime() == "codex":
        csid = os.environ.get("CODEX_THREAD_ID") or "shared"
    else:
        csid = os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"
    return f"{UNSAFE_KEY.sub('-', Path.cwd().name)}-{UNSAFE_KEY.sub('-', csid)}"


def log_dir(cwd: Path | None = None) -> Path:
    """Return the telemetry log directory, anchored to the project root.

    The hook layer, the skill layer, and the CLI layer all append shards keyed on ONE
    session id, and ``debrief-coding`` joins them by reading a single directory. A
    CWD-relative default silently broke that join: a session whose hooks fired at the
    repo root while a query ran from a subdirectory wrote the two halves into
    ``<root>/.cache/codemap/logs`` and ``<subdir>/.cache/codemap/logs``. Neither half
    is an error — the join just returns nothing. Anchoring to the project root is what
    keeps every layer in one directory regardless of where a process was launched.

    ``CODEMAP_LOG_DIR`` overrides the root. An absolute override is honoured verbatim;
    a relative one is anchored here for the same reason the default is.

    Args:
        cwd: Directory to resolve the project anchor from; defaults to the process CWD.

    Returns:
        The runtime-scoped log directory path (not created — callers
        ``mkdir(parents=True)``).

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     nested = Path(d) / "proj" / "sub"
        ...     nested.mkdir(parents=True)
        ...     (Path(d) / "proj" / ".git").mkdir()
        ...     log_dir(nested) == Path(d).resolve() / "proj" / ".cache" / "codemap" / "logs" / "claude"
        True
    """
    anchor = project_root(cwd)
    raw = os.environ.get(LOG_DIR_ENV)
    if not raw:
        root = anchor / LOG_SUBDIR
    else:
        override = Path(raw).expanduser()
        root = override if override.is_absolute() else anchor / override
    return root / runtime()


def plugin_version() -> str:
    """Return the hook package version, degrading to ``"?"`` when unavailable."""
    try:
        manifest = Path(__file__).parents[1] / ".claude-plugin" / "plugin.json"
        return str(json.loads(manifest.read_text(encoding="utf-8")).get("version") or "?")
    except (OSError, TypeError, ValueError):
        return "?"


def tmp_dir() -> Path:
    """Return the temp directory the hook sentinels and session markers live in.

    ``TMPDIR`` is honoured explicitly rather than left to :func:`tempfile.gettempdir`
    alone so the value matches ``codemap_py.telemetry``, which resolves it the same way.

    Examples:
        >>> tmp_dir().is_absolute()
        True
    """
    return Path(os.environ.get("TMPDIR") or tempfile.gettempdir())


def write_sentinel(path: Path, content: str) -> None:
    """Replace sentinel content only after opening an owned, identity-checked regular file.

    Planted symlinks are rejected before exclusive creation, which can follow dangling links on Windows. Existing files
    are opened without truncation and checked against their inspected identity before any bytes change, including on
    platforms without O_NOFOLLOW. Nonblocking opens prevent raced FIFOs from hanging a hook. Filesystem errors remain
    with the hook's existing fail-open boundary.
    """
    if path.is_symlink():
        raise OSError(f"sentinel is not a regular file: {path}")
    previous = None
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        previous = path.lstat()
        if not stat.S_ISREG(previous.st_mode):
            raise OSError(f"sentinel is not a regular file: {path}") from None
        descriptor = os.open(path, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or (previous is not None and not os.path.samestat(previous, info)):
            raise OSError(f"sentinel changed while opening: {path}")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise OSError(f"sentinel is owned by another user: {path}")
        stream.truncate(0)
        stream.write(content)


def project_key(cwd: Path | None = None) -> str:
    """Return the path-free project identity used by codemap telemetry's root_key."""
    identity = str(project_root(cwd))
    if os.name == "nt":
        identity = identity.replace("/", "\\").casefold()
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def logging_enabled() -> bool:
    """Return whether the ``CODEMAP_LOGGING`` opt-out leaves local logging on.

    Hook health records live in the log directory, so they follow the same opt-out as the telemetry shards: with
    ``CODEMAP_LOGGING=false`` no hook writes there, and ``codemap-py doctor`` reports zero counts and no last success.

    Examples:
        >>> isinstance(logging_enabled(), bool)
        True
    """
    return os.environ.get("CODEMAP_LOGGING", "true").lower() != "false"


def _real_directory(path: Path) -> None:
    """Create *path* as one owned directory level, refusing anything that is not a plain directory.

    Only the two levels this module creates are checked. Higher components may legitimately be symlinks (a macOS
    temporary path, a user's ``CODEMAP_LOG_DIR``), but a planted link at ``hook-health`` or a per-hook directory must
    never redirect a record outside the log directory. An entry that reports a Windows reparse tag is refused too, as
    intended defence against a junction.
    """
    with suppress(FileExistsError):
        path.mkdir(mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_reparse_tag", 0):
        raise OSError(f"hook health path is not a plain directory: {path}")


def _health_dir(hook: str) -> Path:
    """Return the runtime-scoped health directory of *hook*, creating it as plain directories."""
    base = log_dir()
    base.mkdir(parents=True, exist_ok=True)
    health = base / HOOK_HEALTH_SUBDIR
    _real_directory(health)
    directory = health / hook
    _real_directory(directory)
    return directory


#: Newest timestamp this process stamped on a failure record, so a coarse clock cannot hand two records one value.
_LAST_RECORD_NS = [0]
_RECORD_NS_LOCK = threading.Lock()


def _next_record_ns() -> int:
    """Return an epoch-nanosecond stamp that is strictly greater than every earlier stamp of this process.

    Windows before Python 3.13 reads a timer that advances every 15.6 ms, so a burst of failures shares one value and
    the record names would fall back to their random token to order, which trimmed the newest records first.

    Examples:
        >>> first = _next_record_ns()
        >>> _next_record_ns() > first
        True
    """
    with _RECORD_NS_LOCK:
        _LAST_RECORD_NS[0] = stamp = max(time.time_ns(), _LAST_RECORD_NS[0] + 1)
    return stamp


def _failure_record_name(exc: BaseException) -> str:
    """Return a new failure record name carrying only the class name of *exc* and the current UTC time.

    Examples:
        >>> bool(HOOK_FAILURE_NAME.fullmatch(_failure_record_name(OSError("not stored"))))
        True
        >>> _failure_record_name(ValueError()).endswith("-ValueError")
        True
    """
    cls = _UNSAFE_CLASS.sub("_", type(exc).__name__)[:_CLASS_NAME_MAX]
    if not cls or cls[0].isdigit():
        cls = f"_{cls}"[:_CLASS_NAME_MAX]
    return f"fail-{_next_record_ns():020d}-{secrets.token_hex(4)}-{cls}"


def _trim_failures(directory: Path) -> None:
    """Delete the oldest failure records of one hook beyond :data:`HOOK_FAILURE_RETENTION`.

    Records are separate files, so trimming only unlinks: a concurrent writer's new record is never lost to a rewrite.
    Two concurrent trimmers both delete a prefix of the same time-ordered list, so the directory never drops below the
    retention count once that many records exist.
    """
    names = sorted(name for name in os.listdir(directory) if HOOK_FAILURE_NAME.fullmatch(name))
    for stale in names[:-HOOK_FAILURE_RETENTION]:
        with suppress(FileNotFoundError):
            (directory / stale).unlink()


def record_failure(hook: str, exc: BaseException) -> None:
    """Record one swallowed failure of *hook* for ``codemap-py doctor``, never raising.

    Each failure is one empty file created with ``O_CREAT | O_EXCL`` under
    ``<log-dir>/<runtime>/hook-health/<hook>/``. Each record is its own file, so concurrent hook processes never
    share a write target and the count does not depend on append atomicity across handles, which differs by
    platform. Only the exception class name and the UTC time are kept; the message, the paths and the session
    identity are never written.

    The recorder is diagnostics: any error it meets (unwritable log directory, planted link, exhausted disk) is
    dropped so the hook keeps its fail-open contract. A failure that happens before this function can run, such as an
    import error or a launcher or interpreter failure, is therefore invisible here; a stale last-success stamp is the
    only cue for it.

    Args:
        hook: Name from :data:`HOOK_NAMES`; any other value is ignored.
        exc: The exception the hook swallowed or is about to re-raise.

    Examples:
        >>> record_failure("not-a-hook", OSError("ignored"))  # unknown names never become a path
    """
    try:
        if hook not in HOOK_NAMES or not logging_enabled():
            return
        directory = _health_dir(hook)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        os.close(os.open(directory / _failure_record_name(exc), flags, 0o600))
        _trim_failures(directory)
    except Exception:  # noqa: S110 - diagnostics must never become the hook's own failure
        pass


def record_success(hook: str) -> None:
    """Stamp the UTC time of a clean run of *hook*, never raising.

    The stamp is what makes a hook that stopped running visible: its age grows while the failure count stays at zero.

    Args:
        hook: Name from :data:`HOOK_NAMES`; any other value is ignored.

    Examples:
        >>> record_success("not-a-hook")  # unknown names never become a path
    """
    try:
        if hook not in HOOK_NAMES or not logging_enabled():
            return
        write_sentinel(_health_dir(hook) / HOOK_SUCCESS_STAMP, f"{time.time_ns():020d}\n")
    except Exception:  # noqa: S110 - diagnostics must never become the hook's own failure
        pass


@contextmanager
def hook_outcome(hook: str, expected: tuple[type[BaseException], ...]) -> Iterator[None]:
    """Run one hook body, recording how it ended without changing what the hook returns or raises.

    An exception in *expected* is recorded and swallowed, exactly as the hook's own ``except ...: pass`` did, so the
    hook still fails open. Any other exception is recorded and re-raised, keeping the hook's previous exit behaviour.
    A body that ends without an exception, including an early ``return``, stamps the last success.

    Args:
        hook: Name from :data:`HOOK_NAMES`.
        expected: Exception classes the hook has always swallowed.

    Yields:
        Control to the hook body.

    Examples:
        >>> with hook_outcome("not-a-hook", (ValueError,)):
        ...     raise ValueError("swallowed")
    """
    try:
        yield
    except expected as exc:
        record_failure(hook, exc)
        return
    except Exception as exc:
        record_failure(hook, exc)
        raise
    record_success(hook)
