#!/usr/bin/env python3
"""telemetry.py — runtime-scoped CLI logging for codemap core tools.

Used by scan-query and scan-index (the two core CLI entry points). Production
invocations append one JSON record under
``.cache/codemap/logs/<runtime>/cli_<session-or-invocation>.jsonl``. Runtime
selection is explicit first, then host detection, then direct; the runtime
component and a non-empty correlation token prevent cross-host writes and bare
``cli.jsonl`` collisions.

Only Claude reads the SessionStart marker
(``$TMPDIR/codemap-<project>-session``). Codex uses ``CODEX_THREAD_ID`` and a
direct CLI may opt into ``CODEMAP_TELEMETRY_SESSION``; every other invocation
uses a process-local correlation token.

``bin/_telemetry.py`` is a compatibility shim that aliases this module in
``sys.modules``. This module lives at ``src/codemap_py/telemetry.py`` — one
directory level deeper than a top-level ``bin/`` script — so the manifest
lookup below walks three parents, not two, to still land on the plugin root.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType

from codemap_py.index_paths import canonical_root
from codemap_py.runtime_log import invocation_id, log_dir_for, plugin_version as runtime_plugin_version, resolve_runtime

LOG_MAX_BYTES = 10 * 1024 * 1024
_SAFE = re.compile(r"[^A-Za-z0-9_-]")
_SAFE_SESSION = re.compile(r"^[A-Za-z0-9_-]+$")


def plugin_version() -> str:
    """Return the codemap plugin version (``.claude-plugin/plugin.json``), cached.

    Stamped into every record as ``v`` so before/after comparisons across plugin
    releases stay possible when analysing merged logs. ``"?"`` when unreadable.

    Examples:
        >>> isinstance(plugin_version(), str)
        True
    """
    return runtime_plugin_version()


def session_id(root: Path | None = None) -> str:
    """Return the seeded session id for the target project, or ``""`` if none."""
    try:
        resolved = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
            cwd=root,
        ).strip()
        proj = Path(resolved).name
    except Exception:  # noqa: BLE001 — git absent / not a repo → fall back to cwd
        proj = (root or Path.cwd()).name
    sid_file = Path(os.environ.get("TMPDIR") or tempfile.gettempdir()) / f"codemap-{proj}-session"
    try:
        return sid_file.read_text().strip()
    except OSError:
        return ""


def runtime_id() -> str:
    """Return the explicit or host-detected runtime for this CLI invocation.

    ``CODEMAP_RUNTIME`` is an explicit selection: an invalid non-empty value deliberately falls back to ``direct``
    rather than being replaced by host detection, while an empty or whitespace-only value counts as unset (the hook
    layer normalizes the same way, so both layers shard one session identically). Without it, a Codex thread wins over
    any inherited Claude marker so one process cannot append into the wrong runtime's shard.
    """
    explicit = (os.environ.get("CODEMAP_RUNTIME") or "").strip().lower()
    if explicit:
        return resolve_runtime(explicit)[0]
    if os.environ.get("CODEX_THREAD_ID"):
        return "codex"
    if os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CSID"):
        return "claude"
    return "direct"


def runtime_session(runtime: str, *, root: Path | None = None) -> str:
    """Return the safe correlation token for one resolved runtime.

    Only Claude reads the target project's seed marker. Codex exposes its own thread identifier,
    while direct invocations may opt into a stable token with
    ``CODEMAP_TELEMETRY_SESSION``; all other cases use a process-local invocation
    token so concurrent direct calls never merge into one bare ``cli.jsonl``.
    """
    if runtime == "codex":
        return os.environ.get("CODEX_THREAD_ID") or invocation_id()
    if runtime == "claude":
        return os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CSID") or session_id(root) or invocation_id()
    explicit = os.environ.get("CODEMAP_TELEMETRY_SESSION", "")
    return explicit if _SAFE_SESSION.fullmatch(explicit) else invocation_id()


def log_path_for(session: str, log_dir: Path) -> Path:
    """Resolve the per-session log file path (``cli.jsonl`` when session empty)."""
    name = f"cli_{_SAFE.sub('-', session)}.jsonl" if session else "cli.jsonl"
    return log_dir / name


def _rotate(path: Path) -> None:
    for i in range(2, 0, -1):
        src = path.parent / f"{path.name}.{i}"
        if src.exists():
            src.rename(path.parent / f"{path.name}.{i + 1}")
    if path.exists():
        path.rename(path.parent / f"{path.name}.1")


def log_cli(
    cmd: str,
    argv: list[str],
    result: object,
    t0: float,
    *,
    log_dir: Path | None = None,
    exit_code: int = 0,
    root: Path | None = None,
) -> None:
    """Append one CLI telemetry record; ``log_dir`` is a test-only final-dir seam.

    Production callers must omit ``log_dir`` so :func:`log_dir_for` applies the runtime component beneath the
    ``CODEMAP_LOG_DIR`` root. The only in-repository callers that inject it are telemetry tests, where it intentionally
    denotes the already-resolved final directory. Engines supply their resolved target ``root`` so log placement,
    project identity and legacy Claude session lookup cannot fall back to an unrelated caller directory.
    """
    if os.environ.get("CODEMAP_LOGGING", "true").lower() == "false":
        return
    try:
        runtime = runtime_id()
        project_root = canonical_root(root)
        # ``log_dir`` remains a final-directory test seam. Production callers omit it,
        # then every invocation receives its required runtime component under the shared
        # log root instead of writing flat records that bypass isolation.
        scoped_log_dir = log_dir if log_dir is not None else log_dir_for(runtime, root=project_root)
        sid = session_id(project_root) if log_dir is not None else runtime_session(runtime, root=project_root)
        log_file = log_path_for(sid, scoped_log_dir)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        if log_file.exists() and log_file.stat().st_size > LOG_MAX_BYTES:
            _rotate(log_file)
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "layer": "cli",
            "runtime": runtime,
            "v": plugin_version(),
            "cmd": cmd,
            "session": sid,
            "project": project_root.as_posix(),
            "argv": argv,
            "timing_ms": max(0, int((time.time() - t0) * 1000)),
            "result": result if isinstance(result, dict) else {},
            "exit_code": exit_code,
        }
        # Benchmark / demo runs tag themselves (export CODEMAP_TELEMETRY_SOURCE=bench)
        # so debrief can separate scripted load from organic usage — untagged demo
        # loops skewed the 2026-07 audit's per-project stats.
        source = os.environ.get("CODEMAP_TELEMETRY_SOURCE")
        if source:
            record["source"] = source
        with log_file.open("a") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception:  # noqa: BLE001 — telemetry must never break the CLI
        pass


@dataclass
class CliInvocation:
    """Record one handled terminal outcome without changing command output or exceptions.

    Engines retain their structured result here until command completion so an error after output cannot masquerade as
    success. Parsing and gate failures are recorded even when no result reached stdout. Abrupt process termination
    cannot guarantee a final record.
    """

    command: str
    argv: list[str]
    result: dict = field(default_factory=dict)
    started: float = field(default_factory=time.time)
    root: Path | None = None

    def __enter__(self) -> CliInvocation:
        """Start the command's bounded terminal logging scope."""
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        """Persist the exit status once, then let the original exception propagate."""
        if isinstance(exc, SystemExit):
            code = exc.code if isinstance(exc.code, int) else 0 if exc.code is None else 1
        elif isinstance(exc, KeyboardInterrupt):
            code = 130
        else:
            code = 1 if exc is not None else 0
        if code and not self.result.get("error"):
            self.result = {**self.result, "error": type(exc).__name__, "exit_code": code}
        # Help exits before a command is selected; it is not a structural query.
        if isinstance(exc, SystemExit) and code == 0 and not self.result:
            return
        log_cli(self.command, self.argv, self.result, self.started, exit_code=code, root=self.root)
