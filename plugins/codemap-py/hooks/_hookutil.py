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

consumers: hooks/{seed-session,log-tool-use,log-skill-start,guard-redundant-scan,record-exhausted}.py — imported as bare
``_hookutil``; not a standalone executable
"""

from __future__ import annotations

import json
import os
import re
import tempfile
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
RUNTIME_ALLOWLIST = frozenset({"claude", "codex"})
DEFAULT_RUNTIME = "claude"


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
