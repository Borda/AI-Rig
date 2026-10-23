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

import fnmatch
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path, PurePath

#: Directory holding this hook, put on ``sys.path`` so the shared ``_hookutil`` helper imports under any loader.
_HOOKS_DIR = Path(__file__).resolve().parent
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

import _hookutil  # noqa: E402  (needs the sys.path insert above)

#: Plugin ``src`` directory, put on ``sys.path`` so the hook imports the bundled ``codemap_py`` scanner.
_SRC_DIR = _HOOKS_DIR.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from codemap_py import scanner  # noqa: E402  (resolve from the installed plugin)
from codemap_py.scanner.discovery import fingerprint_file_hashes  # noqa: E402
from codemap_py.scanner.docs_xrefs import _CONFIG_SCAN_PATTERNS  # noqa: E402

#: Largest index file, in bytes, the hook fully parses to count modules; a larger index reports a placeholder instead.
MAX_PARSE_BYTES = 10 * 1024 * 1024
#: Age in milliseconds after which an index-refresh lock file is considered stale and may be taken over.
LOCK_TTL_MS = 10 * 60 * 1000
#: Number of leading bytes of the index file read to extract its header fields without decoding the whole file.
HEADER_PEEK_BYTES = 8 * 1024
#: Milliseconds the once-per-session missing-index directive stays suppressed after it was emitted.
NOINDEX_TTL_MS = 30 * 60 * 1000
#: Default lifetime in milliseconds of a sentinel flag checked by :func:`within_ttl`.
SESSION_TTL_MS = 30 * 60 * 1000
# Missing host identity retains a shared fallback without borrowing another runtime's session.
SHARED_SESSION = "shared"
#: Git freshness includes documentation as well as Python sources; the source-eligibility
#: check below deliberately uses only the scanner's Python discovery rules.
_INDEXED_PATHSPEC: tuple[str, ...] = ("*.py", "*.pyi", "*.rst", "docs/**/*.md")
#: The only shape of index-header ``git_sha`` handed to git as a revision argument. The header is
#: on-disk, unauthenticated input; a value that is not a hex object name never reaches a subprocess.
_HEX_OBJECT_RE = re.compile(r"[0-9a-fA-F]{7,64}")
#: Note for a stale index whose refresh inputs have not changed since the last completed refresh.
UNCHANGED_NOTE = " - unchanged since last refresh"
#: Note returned by :func:`start_refresh` when it actually spawned a background scan.
STARTED_NOTE = " - refresh started"

#: : Identity fields read out of the index header, compiled once at import. They used to be
#: : matched by a pattern built — and an ``import re`` executed — inside a nested closure,
#: : once per field, on every prompt.
#: The value alternation consumes `\"` as one unit so an embedded quote does not end the match
#: early; whatever it captures is still a JSON string body, so `_json_unescape` decodes it.
_HEADER_FIELD_RES = {
    name: re.compile(rf'"{name}"\s*:\s*"((?:[^"\\]|\\.)*)"')
    for name in ("git_sha", "scanned_at", "scan_root", "source_fingerprint")
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
        return subprocess.run(  # noqa: S603 - argv list, no shell; tool resolved via PATH on purpose
            ["git", *args],  # noqa: S607 - argv list, no shell; tool resolved via PATH on purpose
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


def handle_missing_index(root: Path, project: str, session: str) -> None:
    """Emit the once-per-session Python-project bootstrap directive when needed."""
    if not is_python_project(root):
        return
    flag = tmp_dir() / f"codemap-noindex-{project}-{_hookutil.runtime()}-{session}"
    if within_ttl(flag, NOINDEX_TTL_MS):
        return
    write_timestamp(flag)
    preparation = (
        "Automatic preparation is disabled by SCAN_NO_AUTOBUILD=1; queries require an existing frozen index."
        if os.environ.get("SCAN_NO_AUTOBUILD") == "1"
        else "Use `codemap-py query` from this source checkout: an ordinary query automatically prepares its own-root index."
    )
    emit_preamble(
        f'[codemap] No structural index for "{project}" (.cache/codemap/{project}.json missing).\n'
        f"{preparation}\n"
        "`--no-heal` and SCAN_NO_AUTOBUILD=1 retain explicit frozen-query behavior."
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
        with index_path.open("rb") as handle:
            header = handle.read(HEADER_PEEK_BYTES).decode("utf-8", errors="replace")
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
        modules = json.loads(index_path.read_text(encoding="utf-8")).get("modules")
    except (OSError, ValueError, AttributeError):
        return "?"
    return len(modules) if isinstance(modules, list) else "?"


def changed_file_count(root: Path, git_sha: str) -> int | None:
    """Count eligible working-tree differences from the index commit, or report unknown.

    This is a consumed-input delta against the recorded commit, not a measured reparse count. Configuration and
    documentation remain inputs even when Python discovery excludes their paths. The commit tree supplies preimages
    for deletions. Invalid commits, unreadable inputs, and historical symlink configs remain unknown.
    """
    if not _HEX_OBJECT_RE.fullmatch(git_sha or ""):
        return None
    try:
        tree = subprocess.run(  # noqa: S603 - explicit argv, validated revision
            ["git", "ls-tree", "-r", "-z", git_sha],  # noqa: S607 - Git intentionally resolved via PATH
            cwd=root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
            check=True,
        ).stdout
        exclusions = scanner._load_exclusions(root)
        current = scanner.get_file_hashes(root, exclusions)
        before = {}
        for entry in tree.split("\0"):
            meta, separator, path = entry.partition("\t")
            fields = meta.split()
            if not separator or len(fields) != 3 or fields[1] != "blob":
                continue
            parts = path.split("/")
            config_input = len(parts) == 1 and (
                path == ".codemapignore" or any(fnmatch.fnmatch(path, pattern) for pattern in _CONFIG_SCAN_PATTERNS)
            )
            if fields[0] == "120000":
                if config_input:
                    # The historical blob stores a link target, not the configuration bytes the scanner consumed.
                    return None
                continue
            python_input = scanner._is_python_source(path) and not scanner.is_excluded(path, exclusions)
            doc_input = (path.endswith(".rst") or (path.startswith("docs/") and path.endswith(".md"))) and not any(
                part in scanner.SKIP_DIRS for part in parts[:-1]
            )
            if config_input or python_input or doc_input:
                before[path] = fields[2]
        return sum(before.get(path) != current.get(path) for path in before.keys() | current.keys())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def refresh_fingerprint(root: Path, head: str, source_fingerprint: str | None = None) -> str | None:
    """Identify a refresh's actual discovered inputs and commit without requiring staging.

    The optional source fingerprint reuses the main hook's already-computed value. Missing HEAD is valid for a
    non-Git project; an unreadable source cannot establish an unchanged input set.
    """
    try:
        if source_fingerprint is None:
            source_fingerprint = fingerprint_file_hashes(scanner.get_file_hashes(root, scanner._load_exclusions(root)))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return hashlib.sha256(f"{head}\0{source_fingerprint}".encode()).hexdigest()


def refresh_record_path(project: str, index_path: PurePath) -> Path:
    """Return the record of the last spawned refresh for one index.

    Deliberately session- and runtime-independent, like the refresh lock: Claude and Codex prompts refresh one shared
    index, so a refresh either of them started covers both. The index-path hash keeps two checkouts that share a
    directory basename apart.

    Examples:
        >>> name = refresh_record_path("proj", PurePath("/repo/.cache/codemap/proj.json")).name
        >>> name.startswith("codemap-refresh-fp-proj-") and len(name) == len("codemap-refresh-fp-proj-") + 12
        True
    """
    key = hashlib.sha256(index_path.as_posix().encode("utf-8")).hexdigest()[:12]
    return tmp_dir() / f"codemap-refresh-fp-{project}-{key}"  # tmpdir-exempt: shared by Claude and Codex, like the lock


def refresh_settled(record: Path, fingerprint: str) -> bool:
    """Return whether the refresh spawned for *fingerprint* itself published the index.

    The record reads ``<fingerprint>\\n<ms>\\n<state>``. The hook writes ``spawned``; only the scan it spawned
    rewrites it to ``published``, after its own publish (``graph._mark_refresh_published``). An index mtime could not
    prove that: a refresh failing ``index_busy`` behind an older, slower scan would see that scan's publish land after
    its spawn. A crashed, timed-out or still-running refresh, and a missing or corrupt record, read as "not settled",
    so the next prompt after the lock expires may retry.

    Args:
        record: path from :func:`refresh_record_path`.
        fingerprint: current :func:`refresh_fingerprint`.

    Examples:
        >>> import tempfile
        >>> path = Path(tempfile.mkdtemp()) / "record"
        >>> _ = path.write_text("abc\\n1\\npublished", encoding="utf-8")
        >>> refresh_settled(path, "abc"), refresh_settled(path, "xyz")
        (True, False)
    """
    try:
        lines = record.read_text(encoding="utf-8").split("\n")
    except (OSError, ValueError):
        return False
    return len(lines) == 3 and lines[0] == fingerprint and lines[2] == "published"


def write_refresh_record(record: Path, fingerprint: str) -> None:
    """Best-effort ``spawned`` record of the fingerprint a refresh is about to index, with the spawn time."""
    try:
        record.write_text(f"{fingerprint}\n{now_ms()}\nspawned", encoding="utf-8")
    except OSError:
        pass


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


def spawn_refresh(
    scan_bin: Path,
    scan_root: Path,
    cwd: Path,
    session: str = "",
    changed_count: int | None = None,
    record: tuple[Path, str] | None = None,
) -> bool:
    """Spawn a detached scan with the event's runtime/session, refresh provenance, and platform isolation."""
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
    if session:
        # A payload-only identity otherwise disappears at the subprocess boundary.
        session_key = "CODEX_THREAD_ID" if runtime == "codex" else "CLAUDE_CODE_SESSION_ID"
        kwargs["env"][session_key] = session
    if changed_count is not None:
        # The child never re-derives this; an absent key records the count as unknown.
        kwargs["env"]["CODEMAP_REFRESH_CHANGED_COUNT"] = str(changed_count)
    if record is not None:
        # The scan marks this record published after its own publish; see refresh_settled.
        kwargs["env"]["CODEMAP_REFRESH_RECORD"] = str(record[0])
        kwargs["env"]["CODEMAP_REFRESH_FINGERPRINT"] = record[1]
    # The hook already runs in the selected runtime; executing the script's shebang again can pick an older Python.
    command = [os.environ.get("CODEMAP_PYTHON") or sys.executable, str(scan_bin), *scan_args]
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(command, **kwargs)  # noqa: S603 - argv list, no shell; tool resolved via PATH on purpose
    except OSError:
        return False
    return True


def start_refresh(
    project: str,
    scan_root: Path,
    cwd: Path,
    session: str = "",
    changed_count: Callable[[], int | None] | None = None,
    record: tuple[Path, str] | None = None,
) -> str:
    """Take the refresh lock and spawn one background scan; return the preamble's note.

    ``changed_count`` is a thunk, evaluated only after the lock is held: while a scan is running every prompt in the
    lock's 10-minute TTL returns "in progress" here, and the git calls behind the count must not run on those turns.
    The count is provenance only: a thunk that raises leaves the count unknown and the refresh still starts, so no
    exception can carry the held lock out of this function and silence refreshes for the lock's whole TTL.

    ``record`` (record path, fingerprint) is written as ``spawned`` only once this call holds the lock, just before the
    spawn, and handed to the scan, which alone marks it ``published``. Writing it while another refresh holds the
    lock would let that older scan's publish settle newer source inputs; writing it after the spawn could overwrite
    a fast scan's ``published`` line.
    """
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
    if scan_bin.is_file():
        count = None
        if changed_count is not None:
            try:
                count = changed_count()
            except Exception:
                count = None
        if record is not None:
            write_refresh_record(*record)
        if spawn_refresh(scan_bin, scan_root, cwd, session, count, record):
            return STARTED_NOTE
    try:
        lock.unlink()
    except OSError:
        pass
    return ""


def stale_refresh_note(
    project: str,
    root: Path,
    head: str,
    index_path: Path,
    start: Callable[[tuple[Path, str] | None], str],
    source_fingerprint: str | None = None,
) -> str:
    """Skip the refresh when its inputs are unchanged since the last completed one; otherwise call *start*.

    Repeat suppression requires the scanner's publication acknowledgement and a matching current index header.
    A historical publication cannot suppress repair after older index bytes are restored to the same cache path.

    Args:
        project: project basename keying the record beside the refresh lock.
        root: source root for the working-tree fingerprint.
        head: current HEAD commit.
        index_path: index file the refresh would publish.
        start: spawns the refresh given the record it hands the scan; returns the preamble note.
    """
    fingerprint = refresh_fingerprint(root, head, source_fingerprint)
    if fingerprint is None:
        return start(None)
    record = refresh_record_path(project, index_path)
    if refresh_settled(record, fingerprint):
        fields = header_fields(index_path)
        if (
            fields["scan_root"] == str(root.resolve())
            and fields["git_sha"] == head
            and fields["source_fingerprint"]
            and refresh_fingerprint(root, head, fields["source_fingerprint"]) == fingerprint
        ):
            return UNCHANGED_NOTE
    return start((record, fingerprint))


def read_stale_state(path: Path) -> tuple[int, str, str] | None:
    """Read the stale notice's timestamp and index state, treating unusable records as absent."""
    try:
        timestamp, git_sha, currency = json.loads(path.read_text(encoding="utf-8"))
        if type(timestamp) is not int or not isinstance(git_sha, str) or not isinstance(currency, str):
            return None
        return timestamp, git_sha, currency
    except (OSError, TypeError, ValueError):
        return None


def collapse_stale_notice(project: str, session: str, git_sha: str, currency: str, refresh_note: str) -> bool:
    """Suppress unchanged state within TTL, remind on changes, and re-deliver full context after TTL."""
    flag = tmp_dir() / f"codemap-stale-{project}-{_hookutil.runtime()}-{session}"
    previous = read_stale_state(flag)
    timestamp = now_ms()
    if previous is not None and previous[1:] == (git_sha, currency) and timestamp - previous[0] < SESSION_TTL_MS:
        return True
    try:
        flag.write_text(json.dumps([timestamp, git_sha, currency]), encoding="utf-8")
    except OSError:
        pass
    if previous is None or timestamp - previous[0] >= SESSION_TTL_MS:
        return False
    emit_preamble(f"[codemap] index stale{refresh_note or ' - refresh pending'}")
    return True


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
        session = _hookutil.runtime_session(payload)
        session_token = _hookutil.UNSAFE_KEY.sub("_", session) or SHARED_SESSION
        write_session_marker(root, session)
        index_dir = Path(os.environ.get("CODEMAP_INDEX_DIR", root / ".cache" / "codemap"))
        index_path = index_dir / f"{project}.json"
        index_stat = regular_file_stat(index_path)
        if index_stat is None:
            if not has_python_source(root):
                return 0
            handle_missing_index(root, project, session_token)
            return 0
        fields = header_fields(index_path)
        git_sha = fields["git_sha"]
        raw_root = fields["scan_root"]
        same_root = bool(raw_root) and Path(raw_root).is_absolute() and Path(raw_root).resolve() == root.resolve()
        head = git_output(["rev-parse", "HEAD"], root)
        source_fingerprint = fingerprint_file_hashes(scanner.get_file_hashes(root, scanner._load_exclusions(root)))
        # Header provenance follows the same bytes the scanner parsed; Git dirtiness alone cannot say whether a
        # working-tree index is fresh. Legacy headers get one automatic refresh to establish this evidence.
        currency = (
            "current"
            if same_root and source_fingerprint == fields["source_fingerprint"] and head == git_sha
            else "stale"
        )
        refresh_note = ""
        if currency == "stale" and os.environ.get("SCAN_NO_AUTOBUILD") == "1":
            refresh_note = " - automatic refresh disabled by SCAN_NO_AUTOBUILD=1"
        elif currency == "stale" and not same_root:
            # A settled refresh record cannot vouch for a foreign index copied over its publication.
            refresh_note = start_refresh(project, root, cwd, session)
        elif currency == "stale":
            refresh_note = stale_refresh_note(
                project,
                root,
                head,
                index_path,
                lambda record: start_refresh(
                    project, root, cwd, session, lambda: changed_file_count(root, git_sha), record=record
                ),
                source_fingerprint,
            )
        session_flag = tmp_dir() / f"codemap-preamble-{project}-{_hookutil.runtime()}-{session_token}"
        if currency == "current":
            # A refreshed index resets stale dedup even when the current preamble is already suppressed.
            with suppress(OSError):
                (tmp_dir() / f"codemap-stale-{project}-{_hookutil.runtime()}-{session_token}").unlink(missing_ok=True)
            if within_ttl(session_flag):
                return 0
        write_timestamp(session_flag)
        if currency == "stale" and collapse_stale_notice(project, session_token, git_sha, currency, refresh_note):
            return 0
        sha_label = f" (git: {git_sha[:7]})" if currency == "current" else ""
        emit_preamble(
            f"[codemap] {os.path.relpath(index_path, cwd)} - {module_count(index_path, index_stat.st_size)} modules"
            f" - {currency}{sha_label}{refresh_note} - scanned: {fields['scanned_at'][:10]}\n"
            "Prefer `codemap-py query` over file reads: rdeps, fn-rdeps, fn-blast, xrefs, symbol.\n"
            # Telemetry: one session re-ran the same query 21 times. A full ranking piped into a filter (as the
            # foundry agents' pre-flight does) never enters context, so only output read directly is bounded here.
            "Reuse an earlier answer only while source inputs are unchanged; rerun after edits. Keep `--top` small"
            " when the output enters context."
        )
    except (OSError, TypeError, ValueError, subprocess.SubprocessError):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
