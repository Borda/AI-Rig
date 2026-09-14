#!/usr/bin/env python3
"""Emit codemap index context before a user prompt.

Purpose:
    Tell the active host whether a Python project's structural codemap is available and
    trigger a bounded background refresh when the index is stale.

Scope:
    Read project and index metadata, write a persistent session marker and temporary
    refresh/preamble sentinels, and emit the host-specific ``UserPromptSubmit`` envelope.
    The hook never edits source files.

Usage:
    Invoke as a Claude or Codex ``UserPromptSubmit`` hook with the host JSON payload on
    standard input.

Outputs:
    Print plain preamble text for Claude or one JSON hook envelope for Codex; refresh
    requests and session markers are best-effort local side effects.

Failure:
    Malformed stdin is treated as an empty event, and unavailable Git falls back to
    local state; either case may still emit context. Expected I/O, type, and value errors
    are suppressed so prompt submission can continue.

Used by:
    The codemap-py hook configuration for Claude and Codex prompt submission events.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_HOOKS_DIR = Path(__file__).resolve().parent
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

import _hookutil  # noqa: E402  (needs the sys.path insert above)

_SRC_DIR = _HOOKS_DIR.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from codemap_py import scanner  # noqa: E402  (resolve from the installed plugin)

MAX_PARSE_BYTES = 10 * 1024 * 1024
LOCK_TTL_MS = 10 * 60 * 1000
HEADER_PEEK_BYTES = 8 * 1024
NOINDEX_TTL_MS = 30 * 60 * 1000
SESSION_TTL_MS = 30 * 60 * 1000
# Git freshness includes documentation as well as Python sources; the source-eligibility
# check below deliberately uses only the scanner's Python discovery rules.
_INDEXED_PATHSPEC: tuple[str, ...] = ("*.py", "*.pyi", "*.rst", "docs/**/*.md")

#: Identity fields read out of the index header, compiled once at import. They used to be
#: matched by a pattern built — and an ``import re`` executed — inside a nested closure,
#: once per field, on every prompt.
# The value alternation consumes `\"` as one unit so an embedded quote does not end the match
# early; whatever it captures is still a JSON string body, so `_json_unescape` decodes it.
_HEADER_FIELD_RES = {
    name: re.compile(rf'"{name}"\s*:\s*"((?:[^"\\]|\\.)*)"') for name in ("git_sha", "scanned_at", "scan_root")
}


def now_ms() -> int:
    """Return the wall-clock millisecond timestamp used in hook sentinel files."""
    return int(time.time() * 1000)


def tmp_dir() -> Path:
    """Return the temp directory this hook's sentinel files live in."""
    return Path(os.environ.get("TMPDIR") or tempfile.gettempdir())


def read_timestamp(path: Path) -> int | None:
    """Return a sentinel timestamp, treating missing or corrupt content as absent."""
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def write_timestamp(path: Path) -> None:
    """Best-effort write of the current timestamp to a hook sentinel file."""
    try:
        path.write_text(str(now_ms()), encoding="utf-8")
    except OSError:
        pass


def within_ttl(flag: Path, ttl_ms: int = SESSION_TTL_MS) -> bool:
    """Return whether *flag* was written less than *ttl_ms* ago."""
    timestamp = read_timestamp(flag)
    return timestamp is not None and now_ms() - timestamp < ttl_ms


def git_output(args: list[str], cwd: Path) -> str:
    """Return a bounded git command result, or an empty string when unavailable."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=3,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def has_python_source(root: Path) -> bool:
    """Find eligible Python source without enumerating or parsing the entire project.

    Reuse scanner exclusions and stop at the first non-symlink .py/.pyi file. An empty project requires walking its
    unexcluded directories; excluded trees are never traversed just to count their contents as they are during a full
    scan.
    """
    exclusions = scanner._load_exclusions(root)
    skip_dirs = scanner.SKIP_DIRS | exclusions.dirs
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in skip_dirs and not name.startswith(".")]
        for name in filenames:
            if not scanner._is_python_source(name):
                continue
            path = Path(dirpath) / name
            if (
                not path.is_symlink()
                and scanner._match_exclusion(path.relative_to(root).as_posix(), exclusions) is None
            ):
                return True
    return False


def is_python_project(root: Path) -> bool:
    """Return whether bounded package or packaging markers identify a Python project."""
    try:
        if (root / "__init__.py").is_file() or any(
            (child / "__init__.py").is_file()
            for child in root.iterdir()
            if child.is_dir() and not child.name.startswith(".")
        ):
            return True
        src = root / "src"
        if src.is_dir() and any(
            (child / "__init__.py").is_file()
            for child in src.iterdir()
            if child.is_dir() and not child.name.startswith(".")
        ):
            return True
    except OSError:
        return False
    return any((root / marker).is_file() for marker in ("pyproject.toml", "setup.py"))


def handle_missing_index(root: Path, project: str) -> None:
    """Emit the once-per-session Python-project bootstrap directive when needed."""
    if not is_python_project(root):
        return
    flag = tmp_dir() / f"codemap-noindex-{project}-{_hookutil.runtime()}"
    if within_ttl(flag, NOINDEX_TTL_MS):
        return
    write_timestamp(flag)
    ask = "call AskUserQuestion - ask the user" if _hookutil.runtime() == "claude" else "ask the user"
    emit_preamble(
        f'[codemap] No structural index for "{project}" (.cache/codemap/{project}.json missing) - blast-radius / coupling queries unavailable.\n'
        f"ACTION (ask once): {ask} whether to build the codemap index now.\n"
        "  - yes -> run `codemap-py index` in the FOREGROUND and WAIT until it finishes, then continue using `codemap-py query`.\n"
        "    (bare command resolves through the plugin's bin/ PATH entry; where unavailable, invoke the active plugin root's bin/codemap-py launcher.)\n"
        "  - no -> proceed without codemap; do not raise again this session."
    )


def write_session_marker(root: Path, session_id: str) -> None:
    """Write the current runtime's session marker before any possible early return."""
    try:
        runtime = _hookutil.runtime()
        marker = root / ".cache" / "codemap" / f"current-session-{runtime}.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"session_id": session_id, "ts": now_ms()}) + "\n", encoding="utf-8")
    except OSError:
        pass


def emit_preamble(message: str) -> None:
    """Emit prompt context in the current host's required output envelope."""
    if _hookutil.runtime() == "codex":
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": message,
                    }
                }
            )
        )
        return
    print(message)


def regular_file_stat(path: Path) -> os.stat_result | None:
    """Return *path*'s stat when it is a regular file, or ``None`` when it is not usable.

    Folds what used to be a ``stat()`` inside ``try`` followed by a separate ``is_file()`` branch — a second syscall
    that could only ever fire for a directory or device node sitting where the index belongs. Both conditions now mean
    the same thing to the caller: there is no index to read.
    """
    try:
        info = path.stat()
    except OSError:
        return None
    return info if stat.S_ISREG(info.st_mode) else None


def _json_unescape(raw: str) -> str:
    r"""Decode the body of a JSON string literal captured by regex.

    The prefix scan matches raw file text, so what a capture group holds is still *encoded*:
    a Windows ``scan_root`` is stored as ``C:\\Users\\me`` and was handed to callers with the
    backslashes doubled, which is not a path that exists. Decoding restores the written value
    on every platform — POSIX roots simply contain nothing to unescape.

    Args:
        raw: Capture group contents, without the surrounding quotes.

    Returns:
        The decoded string, or ``raw`` unchanged when it is not a decodable literal.
    """
    try:
        return json.loads(f'"{raw}"')
    except ValueError:
        return raw  # a truncated escape at the peek boundary is still better raw than dropped


def header_fields(index_path: Path) -> dict[str, str]:
    """Return the index identity fields from a bounded prefix read.

    Deliberately *not* a ``json.loads`` of the whole file: this runs on every prompt, while
    the module count below is computed only on the turns that actually print. The cap that
    would have to guard a full decode is ``MAX_PARSE_BYTES`` (10 MB), and real indexes
    exceed it — the index of this repository is 131 MB — so a parse-or-nothing header would
    report every large project as ``unknown`` currency and never trigger a refresh.

    Args:
        index_path: Path to the codemap index JSON.

    Returns:
        Each known header field mapped to its value, or to ``""`` when absent from the
        prefix or unreadable.
    """
    try:
        header = index_path.read_bytes()[:HEADER_PEEK_BYTES].decode("utf-8", errors="replace")
    except OSError:
        return dict.fromkeys(_HEADER_FIELD_RES, "")
    fields = {}
    for name, pattern in _HEADER_FIELD_RES.items():
        match = pattern.search(header)
        fields[name] = _json_unescape(match.group(1)) if match else ""
    return fields


def module_count(index_path: Path, size: int) -> int | str:
    """Return the indexed-module count, or ``"?"`` when the index is too large to parse."""
    if size > MAX_PARSE_BYTES:
        return "?"
    try:
        shas = json.loads(index_path.read_text(encoding="utf-8")).get("file_shas", {})
    except (OSError, ValueError, AttributeError):
        return "?"
    return len(shas) if isinstance(shas, dict) else "?"


def resolve_currency(head: str, git_sha: str, dirty: str) -> str:
    """Return ``current``/``stale``/``unknown`` for the index against the working tree."""
    if not head or not git_sha:
        return "unknown"
    return "stale" if head != git_sha or dirty else "current"


def acquire_refresh_lock(path: Path) -> int | None:
    """Atomically acquire a fresh or stale-taken-over refresh lock, else return ``None``."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        return os.open(path, flags)
    except FileExistsError:
        if within_ttl(path, LOCK_TTL_MS):
            return None
        try:
            path.unlink()
            return os.open(path, flags)
        except OSError:
            return None
    except OSError:
        return None


def spawn_refresh(scan_bin: Path, scan_root: Path, cwd: Path) -> bool:
    """Spawn a detached incremental scan with platform-specific process isolation."""
    # The exclusive write lease is the child's, not this hook's: `bin/scan-index` is a thin
    # launcher over `codemap_py.graph.main`, which wraps build and publish in
    # `rwgate.write_index` — so this detached scan is gated even though nothing here leases.
    # This process must not take one: `rwgate.write_index` scopes the lease to a callback in
    # *this* process, and the prompt path has to return immediately rather than block for the
    # scan's 300s budget. Spawning the launcher rather than the `codemap-py` dispatcher leaves
    # no gap either — `codemap-py index` shells out to this same binary (see `codemap_py.cli`),
    # so both routes reach the identical leased engine, and going direct skips one process
    # layer on the latency-sensitive prompt path.
    scan_args = ["--incremental", "--root", str(scan_root), "--timeout", "300"]
    runtime = _hookutil.runtime()
    kwargs: dict[str, object] = {
        "cwd": cwd,
        "env": {
            **os.environ,
            "CODEMAP_RUNTIME": runtime,
            "CODEMAP_REFRESH_TRIGGER": f"{runtime}_prompt_background",
            "CODEMAP_REFRESH_STALE_BEFORE": "true",
        },
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        command = [os.environ.get("CODEMAP_PYTHON", sys.executable), str(scan_bin), *scan_args]
    else:
        kwargs["start_new_session"] = True
        command = [str(scan_bin), *scan_args]
    try:
        subprocess.Popen(command, **kwargs)
    except OSError:
        return False
    return True


def start_refresh(project: str, scan_root: Path, cwd: Path) -> str:
    """Take the refresh lock and spawn one background scan; return the preamble's note."""
    lock = tmp_dir() / f"codemap-refresh-{project}"
    descriptor = acquire_refresh_lock(lock)
    if descriptor is None:
        return " - refresh in progress"
    try:
        os.write(descriptor, str(now_ms()).encode("ascii"))
    finally:
        os.close(descriptor)
    plugin_root = os.environ.get("PLUGIN_ROOT") or os.environ.get("CLAUDE_PLUGIN_ROOT") or Path(__file__).parents[1]
    scan_bin = Path(plugin_root) / "bin" / "scan-index"
    if scan_bin.is_file() and spawn_refresh(scan_bin, scan_root, cwd):
        return " - refresh started"
    try:
        lock.unlink()
    except OSError:
        pass
    return ""


def collapse_stale_notice(project: str, refresh_note: str) -> bool:
    """Print the one-line stale notice when the full one already fired this session."""
    flag = tmp_dir() / f"codemap-stale-{project}-{_hookutil.runtime()}"
    if within_ttl(flag):
        emit_preamble(f"[codemap] index stale{refresh_note or ' - refresh pending'}")
        return True
    write_timestamp(flag)
    return False


def stdin_payload() -> dict:
    """Return the hook event as a dict, treating any unusable stdin as an empty event."""
    try:
        payload = json.load(sys.stdin)
    except (ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def main() -> int:
    """Emit one fail-open preamble and optionally start a background incremental refresh."""
    try:
        cwd = Path.cwd()
        root = Path(git_output(["rev-parse", "--show-toplevel"], cwd) or cwd)
        project = root.name
        payload = stdin_payload()
        write_session_marker(root, _hookutil.runtime_session(payload))
        if not has_python_source(root):
            return 0
        index_dir = Path(os.environ.get("CODEMAP_INDEX_DIR", root / ".cache" / "codemap"))
        index_path = index_dir / f"{project}.json"
        index_stat = regular_file_stat(index_path)
        if index_stat is None:
            handle_missing_index(root, project)
            return 0
        fields = header_fields(index_path)
        git_sha = fields["git_sha"]
        raw_root = fields["scan_root"]
        scan_root = Path(raw_root).resolve() if raw_root and Path(raw_root).is_absolute() else cwd
        head = git_output(["rev-parse", "HEAD"], cwd)
        dirty = (
            git_output(["status", "--porcelain", "--", *_INDEXED_PATHSPEC], root) if head and git_sha == head else ""
        )
        currency = resolve_currency(head, git_sha, dirty)
        refresh_note = start_refresh(project, scan_root, cwd) if currency == "stale" else ""
        session_flag = tmp_dir() / f"codemap-preamble-{project}-{_hookutil.runtime()}"
        if currency == "current" and within_ttl(session_flag):
            return 0
        write_timestamp(session_flag)
        if currency == "stale" and collapse_stale_notice(project, refresh_note):
            return 0
        sha_label = f" (git: {git_sha[:7]})" if currency == "current" else ""
        emit_preamble(
            f"[codemap] {os.path.relpath(index_path, cwd)} - {module_count(index_path, index_stat.st_size)} modules"
            f" - {currency}{sha_label}{refresh_note} - scanned: {fields['scanned_at'][:10]}\n"
            "Prefer `codemap-py query` over file reads: rdeps, fn-rdeps, fn-blast, xrefs, symbol."
        )
    except (OSError, TypeError, ValueError):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
