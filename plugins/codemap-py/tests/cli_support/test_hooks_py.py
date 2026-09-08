"""Contract tests for the codemap Python hooks driven via stdin JSON subprocess.

Each hook is exercised exactly the way Claude Code drives it: a single event JSON is
piped to `python <hook>` on stdin, and the observable side effects (stdout shape, tmp
files written, spawn markers, exit code) are asserted.

Hooks under test:

- ``inject-preamble.py``       — UserPromptSubmit: index-status preamble, stale-index
                                  background refresh with an atomic O_EXCL lock, and
                                  a once-per-session emit flag. Both the lock and the
                                  session flag use ``read_timestamp`` which must map a
                                  corrupted (non-numeric/empty) file to a *stale* age
                                  rather than a NaN that poisons every comparison.
- ``guard-redundant-scan.py``  — PreToolUse(Bash): deny import-discovery greps for a
                                  module already marked exhaustive this session.
- ``seed-session.py``          — SessionStart: seed the per-project session tmpfile.
- ``log-skill-start.py``       — PreToolUse(Skill): log codemap:* skill invocations.

One session key spans them all: ``seed-session`` writes ``codemap-<project>-session``
into TMPDIR and every other layer reads it back, so ``TestSessionKeyAgreement`` pins that
the writer and both readers derive the same ``<project>`` from the same directory.

TEST SEAM (inject-preamble): the hook keys its lock/flag tmp files on the git-root
basename, resolves the index dir from ``CODEMAP_INDEX_DIR``, and resolves the
``scan-index`` binary from ``CLAUDE_PLUGIN_ROOT/bin/scan-index``. Every test therefore
builds a throwaway git repo (staleness is git-blob based), points ``CODEMAP_INDEX_DIR``
at a controlled index, and points ``CLAUDE_PLUGIN_ROOT`` at a fake plugin root whose
``bin/scan-index`` writes a marker file — so a background spawn is observable without a
real scan.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

_HOOKS = Path(__file__).parent.parent.parent / "hooks"
_INJECT = _HOOKS / "inject-preamble.py"
_GUARD = _HOOKS / "guard-redundant-scan.py"
_SEED = _HOOKS / "seed-session.py"
_SKILL = _HOOKS / "log-skill-start.py"

_INJECT_SPEC = importlib.util.spec_from_file_location("codemap_inject_preamble", _INJECT)
assert _INJECT_SPEC and _INJECT_SPEC.loader
_INJECT_MODULE = importlib.util.module_from_spec(_INJECT_SPEC)
_INJECT_SPEC.loader.exec_module(_INJECT_MODULE)


def _load_scanner_module():
    """Return the scanner module that owns indexed-file discovery."""
    src_dir = Path(__file__).parent.parent.parent / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))
    from codemap_py import scanner

    return scanner


_SCANNER_MODULE = _load_scanner_module()

# These must mirror the constants baked into inject-preamble.py.
_LOCK_TTL_MS = 10 * 60 * 1000  # LOCK_TTL_MS
_SESSION_TTL_MS = 30 * 60 * 1000  # SESSION_TTL_MS

# ── helpers ──────────────────────────────────────────────────────────────────────


def _git(root: Path, *args: str) -> None:
    """Run a git command inside *root*, asserting success."""
    result = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _init_repo(root: Path) -> str:
    """Init a git repo with one committed .py file; return the HEAD sha."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.t")
    _git(root, "config", "user.name", "t")
    (root / "a.py").write_text("def f():\n    return 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True, text=True)
    return head.stdout.strip()


def _write_index(idx_dir: Path, proj: str, *, git_sha: str, modules: int = 2) -> Path:
    """Write a minimal codemap index header the preamble hook can peek at."""
    idx_dir.mkdir(parents=True, exist_ok=True)
    file_shas = {f"m{i}": "deadbeef" for i in range(modules)}
    payload = {
        "git_sha": git_sha,
        "scanned_at": "2026-06-20T00:00:00Z",
        "scan_root": str(idx_dir.parent),
        "file_shas": file_shas,
    }
    idx_path = idx_dir / f"{proj}.json"
    idx_path.write_text(json.dumps(payload))
    return idx_path


def _fake_plugin_root(root: Path, *, with_scan_bin: bool, marker: Path) -> Path:
    """Create a fake CLAUDE_PLUGIN_ROOT.

    When *with_scan_bin* is True, ``bin/scan-index`` is a runnable platform-native stub that touches *marker* on spawn —
    the observable proof the hook launched a refresh.
    """
    plugin_root = root / "plugin"
    bin_dir = plugin_root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    if with_scan_bin:
        scan_bin = bin_dir / "scan-index"
        if os.name == "nt":
            scan_bin.write_text(f'from pathlib import Path\nPath(r"{marker}").write_text("spawned")\n')
        else:
            scan_bin.write_text(f'#!/bin/sh\necho spawned > "{marker}"\n')
            scan_bin.chmod(scan_bin.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return plugin_root


def _run_inject(
    repo: Path,
    idx_dir: Path,
    plugin_root: Path,
    tmpdir: Path,
    *,
    cwd: Path | None = None,
    prompt: str = "hello",
    runtime: str = "claude",
) -> subprocess.CompletedProcess:
    """Drive inject-preamble.py with all three seams pinned to controlled dirs."""
    env = {
        **os.environ,
        "CODEMAP_INDEX_DIR": str(idx_dir),
        "CLAUDE_PLUGIN_ROOT": str(plugin_root),
        "PYTHON": sys.executable,
        "TMPDIR": str(tmpdir),
        "TEMP": str(tmpdir),
        "TMP": str(tmpdir),
    }
    env["CODEMAP_RUNTIME"] = runtime
    env.pop("CODEX_THREAD_ID", None)
    return subprocess.run(
        [sys.executable, str(_INJECT)],
        input=json.dumps({"prompt": prompt}),
        text=True,
        capture_output=True,
        cwd=str(cwd or repo),
        env=env,
    )


def _lock_file(tmpdir: Path, proj: str) -> Path:
    """Return the refresh-lock path for a project in the test temp directory.

    >>> _lock_file(Path("tmp"), "demo").name
    'codemap-refresh-demo'
    """
    return tmpdir / f"codemap-refresh-{proj}"


def _session_flag(tmpdir: Path, proj: str, runtime: str = "claude") -> Path:
    """Return the per-runtime preamble-deduplication flag path.

    >>> _session_flag(Path("tmp"), "demo", "codex").name
    'codemap-preamble-demo-codex'
    """
    return tmpdir / f"codemap-preamble-{proj}-{runtime}"


def _stale_flag(tmpdir: Path, proj: str, runtime: str = "claude") -> Path:
    """Return the per-runtime stale-index sentinel path.

    >>> _stale_flag(Path("tmp"), "demo").name
    'codemap-stale-demo-claude'
    """
    return tmpdir / f"codemap-stale-{proj}-{runtime}"


def _session_marker(repo: Path, runtime: str = "claude") -> Path:
    """Return one runtime's repository-local session marker path.

    >>> _session_marker(Path("repo"), "codex").as_posix()
    'repo/.cache/codemap/current-session-codex.json'
    """
    return repo / ".cache" / "codemap" / f"current-session-{runtime}.json"


def _await_marker(marker: Path, timeout_s: float = 8.0) -> bool:
    """Poll until *marker* appears or *timeout_s* elapses; return whether it appeared.

    The stale-index refresh is a detached, fire-and-forget spawn, so the stub's marker write races the assertion and
    must be awaited. The budget is generous because on Windows the stub runs through a fresh ``python`` interpreter
    whose cold start (a new, Defender-scanned ``CreateProcess``) can take several seconds under CI load — far longer
    than the near-instant POSIX ``/bin/sh`` stub. Returns as soon as the marker exists, so a fast platform pays only the
    real spawn latency, not the ceiling.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if marker.exists():
            return True
        time.sleep(0.05)
    return marker.exists()


def _run_inject_with_event(
    repo: Path,
    idx_dir: Path,
    plugin_root: Path,
    tmpdir: Path,
    event: dict,
    *,
    runtime: str = "claude",
) -> subprocess.CompletedProcess:
    """Drive inject-preamble.py piping a full UserPromptSubmit *event* dict on stdin.

    Mirrors :func:`_run_inject` but lets a test control the whole event (e.g. supply ``session_id``) rather than only
    the prompt string.
    """
    env = {
        **os.environ,
        "CODEMAP_INDEX_DIR": str(idx_dir),
        "CLAUDE_PLUGIN_ROOT": str(plugin_root),
        "PYTHON": sys.executable,
        "TMPDIR": str(tmpdir),
        "TEMP": str(tmpdir),
        "TMP": str(tmpdir),
        "CODEMAP_RUNTIME": runtime,
    }
    env.pop("CODEX_THREAD_ID", None)
    return subprocess.run(
        [sys.executable, str(_INJECT)],
        input=json.dumps(event),
        text=True,
        capture_output=True,
        cwd=str(repo),
        env=env,
    )


# ── inject-preamble: staleness detection + preamble emit ─────────────────────────


class TestInjectPreambleCurrency:
    """Currency classification and the once-per-session emit gate."""

    def test_current_index_emits_preamble_once(self, tmp_path: Path) -> None:
        """A current index (sha matches HEAD, clean tree) emits the preamble line."""
        repo = tmp_path / "proj"
        head = _init_repo(repo)
        idx_dir = tmp_path / "idx"
        _write_index(idx_dir, repo.name, git_sha=head)
        marker = tmp_path / "spawned.marker"
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=True, marker=marker)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()

        result = _run_inject(repo, idx_dir, plugin_root, tmpdir)

        assert result.returncode == 0, result.stderr
        assert "[codemap]" in result.stdout
        assert "current" in result.stdout
        assert "2 modules" in result.stdout
        # current index never spawns a refresh
        assert not marker.exists()

    def test_session_flag_fresh_suppresses_second_emit(self, tmp_path: Path) -> None:
        """A valid session flag younger than SESSION_TTL_MS makes a current index exit silent."""
        repo = tmp_path / "proj"
        head = _init_repo(repo)
        idx_dir = tmp_path / "idx"
        _write_index(idx_dir, repo.name, git_sha=head)
        marker = tmp_path / "spawned.marker"
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=True, marker=marker)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        # Pre-seed a fresh, valid flag.
        _session_flag(tmpdir, repo.name).write_text(str(int(time.time() * 1000)))

        result = _run_inject(repo, idx_dir, plugin_root, tmpdir)

        assert result.returncode == 0, result.stderr
        assert result.stdout == ""

    def test_corrupted_session_flag_still_emits_once(self, tmp_path: Path) -> None:
        """SESSION FLAG NaN: a corrupted flag must NOT early-exit — it falls through and re-emits.

        Pins the readTimestamp NaN guard: ``Date.now() - NaN`` is NaN, and the hook must
        treat that as "no valid flag" (emit) rather than silently exiting 0.
        """
        repo = tmp_path / "proj"
        head = _init_repo(repo)
        idx_dir = tmp_path / "idx"
        _write_index(idx_dir, repo.name, git_sha=head)
        marker = tmp_path / "spawned.marker"
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=True, marker=marker)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        # Corrupted (non-numeric) flag — must be treated as stale/absent.
        flag = _session_flag(tmpdir, repo.name)
        flag.write_text("not-a-number")

        result = _run_inject(repo, idx_dir, plugin_root, tmpdir)

        assert result.returncode == 0, result.stderr
        assert "[codemap]" in result.stdout
        # Flag was rewritten to a valid numeric timestamp.
        assert int(flag.read_text().strip()) > 0

    def test_fail_open_on_non_git_no_index(self, tmp_path: Path) -> None:
        """FAIL-OPEN: a non-git dir with no index and no Python markers exits 0, silent, no throw."""
        plain = tmp_path / "plain"
        plain.mkdir()
        (plain / "notes.txt").write_text("hi")  # non-Python, no index
        idx_dir = tmp_path / "idx-empty"  # never created
        marker = tmp_path / "spawned.marker"
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=True, marker=marker)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()

        result = _run_inject(plain, idx_dir, plugin_root, tmpdir)

        assert result.returncode == 0, result.stderr
        assert result.stdout == ""

    @pytest.mark.parametrize("path", ["api.pyi", "guide.rst", "docs/topic/guide.md"])
    def test_indexed_non_python_change_marks_index_stale_and_refreshes(self, tmp_path: Path, path: str) -> None:
        """Every tracked indexed file kind makes an otherwise current index stale."""
        repo = tmp_path / "proj"
        _init_repo(repo)
        changed = repo / path
        changed.parent.mkdir(parents=True, exist_ok=True)
        changed.write_text("indexed baseline\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "add indexed file")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True
        ).stdout.strip()
        idx_dir = tmp_path / "idx"
        _write_index(idx_dir, repo.name, git_sha=head)
        changed.write_text("indexed change\n")
        marker = tmp_path / "spawned.marker"
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=True, marker=marker)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()

        result = _run_inject(repo, idx_dir, plugin_root, tmpdir)

        assert result.returncode == 0, result.stderr
        assert "stale" in result.stdout
        assert _await_marker(marker)

    def test_nested_cwd_detects_root_level_stub_change_and_refreshes(self, tmp_path: Path) -> None:
        """A nested prompt detects a dirty indexed stub above its working directory."""
        repo = tmp_path / "proj"
        _init_repo(repo)
        changed = repo / "api.pyi"
        changed.write_text("def api() -> int: ...\n")
        _git(repo, "add", "api.pyi")
        _git(repo, "commit", "-q", "-m", "add stub")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True
        ).stdout.strip()
        idx_dir = tmp_path / "idx"
        _write_index(idx_dir, repo.name, git_sha=head)
        changed.write_text("def api() -> str: ...\n")
        nested_cwd = repo / "pkg" / "feature"
        nested_cwd.mkdir(parents=True)
        marker = tmp_path / "spawned.marker"
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=True, marker=marker)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()

        result = _run_inject(repo, idx_dir, plugin_root, tmpdir, cwd=nested_cwd)

        assert result.returncode == 0, result.stderr
        assert "stale" in result.stdout
        assert _await_marker(marker)

    def test_unrelated_change_keeps_current_index_and_skips_refresh(self, tmp_path: Path) -> None:
        """A non-indexed file must not make the refresh hook run."""
        repo = tmp_path / "proj"
        head = _init_repo(repo)
        idx_dir = tmp_path / "idx"
        _write_index(idx_dir, repo.name, git_sha=head)
        (repo / "notes.txt").write_text("unrelated\n")
        marker = tmp_path / "spawned.marker"
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=True, marker=marker)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()

        result = _run_inject(repo, idx_dir, plugin_root, tmpdir)

        assert result.returncode == 0, result.stderr
        assert "current" in result.stdout
        assert not marker.exists()

    def test_hook_pathspec_matches_scanner_contract(self) -> None:
        """Prompt freshness watches exactly the file kinds the scanner hashes."""
        from codemap_py import query

        assert _INJECT_MODULE._INDEXED_PATHSPEC == _SCANNER_MODULE.INDEXED_PATHSPEC == query._INDEXED_PATHSPEC


# ── inject-preamble: bounded index-header read ────────────────────────────────────


class TestInjectPreambleHeaderFields:
    """Read identity out of a bounded index prefix, never the whole file.

    The bound is the point: this runs on every prompt, and real indexes reach hundreds of MB
    (well past ``MAX_PARSE_BYTES``), so a whole-file ``json.loads`` here would either stall the
    prompt path or report every large project as ``unknown`` currency and never refresh.
    """

    def test_returns_every_identity_field_from_a_normal_index(self, tmp_path: Path) -> None:
        """A small index yields git_sha, scanned_at and scan_root exactly as written."""
        idx_dir = tmp_path / ".cache" / "codemap"
        idx_path = _write_index(idx_dir, "proj", git_sha="a" * 40)

        fields = _INJECT_MODULE.header_fields(idx_path)

        assert fields == {
            "git_sha": "a" * 40,
            "scanned_at": "2026-06-20T00:00:00Z",
            "scan_root": str(idx_dir.parent),
        }

    def test_backslashes_in_a_value_come_back_unescaped(self, tmp_path: Path) -> None:
        """A Windows ``scan_root`` is stored JSON-escaped; the reader owes the caller the real path."""
        idx_dir = tmp_path / ".cache" / "codemap"
        idx_dir.mkdir(parents=True)
        idx_path = idx_dir / "proj.json"
        scan_root = r"C:\Users\runneradmin\AppData\Local\Temp\proj"
        idx_path.write_text(
            json.dumps({"git_sha": "c" * 40, "scanned_at": "2026-06-20T00:00:00Z", "scan_root": scan_root}),
            encoding="utf-8",
        )

        fields = _INJECT_MODULE.header_fields(idx_path)

        assert fields["scan_root"] == scan_root
        assert r"\\" not in fields["scan_root"]

    def test_field_past_the_peek_window_reads_as_absent(self, tmp_path: Path) -> None:
        """Only the first HEADER_PEEK_BYTES are consulted — a later field comes back empty."""
        idx_dir = tmp_path / ".cache" / "codemap"
        idx_dir.mkdir(parents=True)
        idx_path = idx_dir / "proj.json"
        padding = "x" * (_INJECT_MODULE.HEADER_PEEK_BYTES * 2)
        idx_path.write_text(
            json.dumps({"git_sha": "b" * 40, "scan_root": str(tmp_path), "pad": padding, "scanned_at": "2026-07-01"}),
            encoding="utf-8",
        )

        fields = _INJECT_MODULE.header_fields(idx_path)

        assert fields["git_sha"] == "b" * 40
        assert fields["scanned_at"] == ""

    def test_unreadable_index_maps_every_field_to_empty(self, tmp_path: Path) -> None:
        """An index path that cannot be read degrades to empty values, never an exception."""
        missing = tmp_path / "absent" / "proj.json"

        fields = _INJECT_MODULE.header_fields(missing)

        assert fields == {"git_sha": "", "scanned_at": "", "scan_root": ""}


# ── inject-preamble: stale-index background refresh + lock lifecycle ──────────────


class TestInjectPreambleRefreshLock:
    """The O_EXCL lock lifecycle around the stale-index background scan spawn."""

    def _stale_repo(self, tmp_path: Path) -> tuple[Path, Path, str]:
        """Build a repo whose committed HEAD differs from the indexed sha (→ stale)."""
        repo = tmp_path / "proj"
        _init_repo(repo)
        idx_dir = tmp_path / "idx"
        # Index carries a bogus sha so currency resolves to "stale".
        _write_index(idx_dir, repo.name, git_sha="0" * 40)
        return repo, idx_dir, repo.name

    def test_stale_index_spawns_refresh_and_writes_lock(self, tmp_path: Path) -> None:
        """STALE → exactly one scan-index spawn; lockfile written; note says 'refresh started'."""
        repo, idx_dir, proj = self._stale_repo(tmp_path)
        marker = tmp_path / "spawned.marker"
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=True, marker=marker)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()

        result = _run_inject(repo, idx_dir, plugin_root, tmpdir)

        assert result.returncode == 0, result.stderr
        assert "stale" in result.stdout
        assert "refresh started" in result.stdout
        # Detached child may lag; await the spawn marker (Windows python cold-start is slow).
        assert _await_marker(marker), "scan-index was not spawned for a stale index"
        assert _lock_file(tmpdir, proj).exists(), "lock file not written on spawn"

    def test_fresh_lock_blocks_second_spawn(self, tmp_path: Path) -> None:
        """LOCK CONTENTION: a fresh valid lock (age < TTL) suppresses the spawn.

        Note reads ' · refresh in progress' and no second scan-index is launched.
        """
        repo, idx_dir, proj = self._stale_repo(tmp_path)
        marker = tmp_path / "spawned.marker"
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=True, marker=marker)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        # Pre-seed a fresh lock so acquisition hits EEXIST + fresh → no takeover.
        _lock_file(tmpdir, proj).write_text(str(int(time.time() * 1000)))

        result = _run_inject(repo, idx_dir, plugin_root, tmpdir)

        assert result.returncode == 0, result.stderr
        assert "refresh in progress" in result.stdout
        time.sleep(0.15)
        assert not marker.exists(), "a held fresh lock must suppress the spawn"

    def test_stale_lock_taken_over_single_spawn(self, tmp_path: Path) -> None:
        """STALE-LOCK TAKEOVER: a lock older than LOCK_TTL_MS is unlinked+recreated, one spawn."""
        repo, idx_dir, proj = self._stale_repo(tmp_path)
        marker = tmp_path / "spawned.marker"
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=True, marker=marker)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        # Lock timestamp well older than the TTL → taken over.
        stale_ts = int(time.time() * 1000) - (_LOCK_TTL_MS + 60_000)
        _lock_file(tmpdir, proj).write_text(str(stale_ts))

        result = _run_inject(repo, idx_dir, plugin_root, tmpdir)

        assert result.returncode == 0, result.stderr
        assert "refresh started" in result.stdout
        assert _await_marker(marker), "a stale lock must be taken over and the scan spawned"

    def test_corrupted_lock_treated_as_stale(self, tmp_path: Path) -> None:
        """CORRUPTED LOCK: a non-numeric lock is NaN-aged → treated as stale, taken over, one spawn.

        Guards against ``Date.now() - NaN`` being read as "fresh" (which would wrongly
        suppress the takeover and leave the index stale forever).
        """
        repo, idx_dir, proj = self._stale_repo(tmp_path)
        marker = tmp_path / "spawned.marker"
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=True, marker=marker)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        _lock_file(tmpdir, proj).write_text("")  # empty → parseInt → NaN

        result = _run_inject(repo, idx_dir, plugin_root, tmpdir)

        assert result.returncode == 0, result.stderr
        assert "refresh started" in result.stdout
        assert _await_marker(marker), "a corrupted lock must be treated as stale and taken over"

    def test_missing_scan_bin_releases_lock_no_spawn(self, tmp_path: Path) -> None:
        """NO-SCANBIN LOCK RELEASE: no scan-index present → lock unlinked, no spawn (later retry)."""
        repo, idx_dir, proj = self._stale_repo(tmp_path)
        marker = tmp_path / "spawned.marker"
        # Fake plugin root WITHOUT a scan-index bin.
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=False, marker=marker)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()

        result = _run_inject(repo, idx_dir, plugin_root, tmpdir)

        assert result.returncode == 0, result.stderr
        assert not marker.exists()
        # The lock the hook briefly acquired must be released so a later hook can retry.
        assert not _lock_file(tmpdir, proj).exists(), "lock must be released when scan bin absent"

    def test_simulated_windows_refresh_uses_python_and_new_process_group(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Windows branch starts the script through Python in an isolated process group."""
        scan_bin = tmp_path / "scan-index"
        scan_bin.write_text("print('scan')\n")
        calls: list[tuple[list[str], dict]] = []

        def _fake_popen(command: list[str], **kwargs: object) -> object:
            """Record the Windows refresh spawn arguments."""
            calls.append((command, kwargs))
            return object()

        monkeypatch.setattr(_INJECT_MODULE.subprocess, "Popen", _fake_popen)
        monkeypatch.setattr(_INJECT_MODULE.os, "name", "nt")
        monkeypatch.setenv("SystemRoot", "C:\\Windows")

        assert _INJECT_MODULE.spawn_refresh(scan_bin, tmp_path, tmp_path)
        command, kwargs = calls.pop()
        child_env = {key.upper(): value for key, value in kwargs["env"].items()}
        assert command[:2] == [sys.executable, str(scan_bin)]
        assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        assert "start_new_session" not in kwargs
        assert kwargs["env"]["CODEMAP_RUNTIME"] == "claude"
        assert kwargs["env"]["CODEMAP_REFRESH_TRIGGER"] == "claude_prompt_background"
        assert kwargs["env"]["CODEMAP_REFRESH_STALE_BEFORE"] == "true"
        assert child_env["SYSTEMROOT"] == "C:\\Windows"

    def test_codex_refresh_records_codex_prompt_trigger(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """A Codex prompt refresh must not be attributed to the Claude hook path."""
        calls: list[dict] = []

        def _fake_popen(command: list[str], **kwargs: object) -> object:
            """Record the Codex refresh spawn arguments."""
            calls.append(kwargs)
            return object()

        monkeypatch.setattr(_INJECT_MODULE.subprocess, "Popen", _fake_popen)
        monkeypatch.setenv("CODEMAP_RUNTIME", "codex")

        assert _INJECT_MODULE.spawn_refresh(tmp_path / "scan-index", tmp_path, tmp_path)
        assert calls[0]["env"]["CODEMAP_RUNTIME"] == "codex"
        assert calls[0]["env"]["CODEMAP_REFRESH_TRIGGER"] == "codex_prompt_background"


# ── inject-preamble: session marker (cross-agent scan-query contract) ─────────────


class TestInjectPreambleSessionMarker:
    """The runtime-sharded repository session marker written every invocation.

    scan-query reads this marker to correlate queries with the session that triggered a refresh (the coverage-diet
    dedup). The hook must write it UNCONDITIONALLY after resolving the project root — before the no-index early exit,
    before the early return for a current-path session flag — so the marker's ts advances on every prompt even when the
    preamble itself is suppressed. It carries single-line JSON ``{"session_id","ts"}`` + trailing newline, and fails
    open (never throws).
    """

    def test_marker_written_with_session_id_and_ts(self, tmp_path: Path) -> None:
        """A current-index turn writes the marker with the stdin session_id and an ms ts."""
        repo = tmp_path / "proj"
        head = _init_repo(repo)
        idx_dir = tmp_path / "idx"
        _write_index(idx_dir, repo.name, git_sha=head)
        marker = tmp_path / "spawned.marker"
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=True, marker=marker)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        before_ms = int(time.time() * 1000)

        result = _run_inject_with_event(
            repo, idx_dir, plugin_root, tmpdir, {"prompt": "hi", "session_id": "sid-marker"}
        )

        assert result.returncode == 0, result.stderr
        raw = _session_marker(repo).read_text()
        assert raw.endswith("\n"), "marker must end with a trailing newline"
        payload = json.loads(raw)
        assert payload["session_id"] == "sid-marker"
        assert isinstance(payload["ts"], int)
        assert payload["ts"] >= before_ms

    def test_marker_written_before_no_index_exit(self, tmp_path: Path) -> None:
        """A non-Python, no-index dir stays silent yet still writes the session marker."""
        plain = tmp_path / "plain"
        plain.mkdir()
        (plain / "notes.txt").write_text("hi")  # non-Python, no index → silent exit
        idx_dir = tmp_path / "idx-empty"  # never created
        marker = tmp_path / "spawned.marker"
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=True, marker=marker)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()

        result = _run_inject_with_event(
            plain, idx_dir, plugin_root, tmpdir, {"prompt": "hi", "session_id": "sid-silent"}
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout == ""  # silent exit path
        payload = json.loads(_session_marker(plain).read_text())
        assert payload["session_id"] == "sid-silent"  # marker written despite the silent exit

    def test_marker_ts_advances_on_deduped_second_turn(self, tmp_path: Path) -> None:
        """A session-flag-suppressed second turn still advances the marker ts (written pre-dedup)."""
        repo = tmp_path / "proj"
        head = _init_repo(repo)
        idx_dir = tmp_path / "idx"
        _write_index(idx_dir, repo.name, git_sha=head)
        marker = tmp_path / "spawned.marker"
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=True, marker=marker)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        # Pre-seed a fresh session flag so the current-index turn dedups to a silent exit.
        _session_flag(tmpdir, repo.name).write_text(str(int(time.time() * 1000)))

        first = _run_inject_with_event(repo, idx_dir, plugin_root, tmpdir, {"prompt": "hi", "session_id": "sid-dedup"})
        first_ts = json.loads(_session_marker(repo).read_text())["ts"]
        time.sleep(0.01)
        second = _run_inject_with_event(repo, idx_dir, plugin_root, tmpdir, {"prompt": "hi", "session_id": "sid-dedup"})

        assert first.returncode == 0 and second.returncode == 0
        assert second.stdout == "", "second current-index turn must dedup to a silent exit"
        second_ts = json.loads(_session_marker(repo).read_text())["ts"]
        assert second_ts >= first_ts, "marker ts must advance even on a deduped turn"

    def test_marker_session_id_empty_on_unparsable_stdin(self, tmp_path: Path) -> None:
        """Unparsable stdin fails open: marker written with session_id '' (no throw, exit 0)."""
        repo = tmp_path / "proj"
        head = _init_repo(repo)
        idx_dir = tmp_path / "idx"
        _write_index(idx_dir, repo.name, git_sha=head)
        marker = tmp_path / "spawned.marker"
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=True, marker=marker)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        env = {
            **os.environ,
            "CODEMAP_INDEX_DIR": str(idx_dir),
            "CLAUDE_PLUGIN_ROOT": str(plugin_root),
            "TMPDIR": str(tmpdir),
            "TEMP": str(tmpdir),
            "TMP": str(tmpdir),
            "CODEMAP_RUNTIME": "claude",
        }
        result = subprocess.run(
            [sys.executable, str(_INJECT)],
            input="}{ not json",
            text=True,
            capture_output=True,
            cwd=str(repo),
            env=env,
        )

        assert result.returncode == 0, result.stderr
        payload = json.loads(_session_marker(repo).read_text())
        assert payload["session_id"] == ""

    def test_markers_are_isolated_across_claude_and_codex_turns(self, tmp_path: Path) -> None:
        """A Codex prompt must not replace the current Claude coverage session."""
        repo = tmp_path / "proj"
        head = _init_repo(repo)
        idx_dir = tmp_path / "idx"
        _write_index(idx_dir, repo.name, git_sha=head)
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=False, marker=tmp_path / "unused")
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()

        claude = _run_inject_with_event(
            repo,
            idx_dir,
            plugin_root,
            tmpdir,
            {"prompt": "claude", "session_id": "claude-session"},
            runtime="claude",
        )
        codex = _run_inject_with_event(
            repo,
            idx_dir,
            plugin_root,
            tmpdir,
            {"prompt": "codex", "thread_id": "codex-thread"},
            runtime="codex",
        )

        assert claude.returncode == 0 and codex.returncode == 0
        assert json.loads(_session_marker(repo, "claude").read_text())["session_id"] == "claude-session"
        assert json.loads(_session_marker(repo, "codex").read_text())["session_id"] == "codex-thread"


# ── inject-preamble: stale-reminder collapse ──────────────────────────────────────


class TestInjectPreambleStaleCollapse:
    """The once-per-session full stale notice collapses to a single line thereafter.

    A separate sentinel (``codemap-stale-<proj>``, distinct from the preamble flag)
    tracks the stale-reminder dedup: the first still-stale prompt emits the full notice on two lines and writes the
    sentinel; every later still-stale prompt within
    SESSION_TTL_MS collapses to one line ``[codemap] index stale<refreshNote>`` and
    exits before the module-count parse. Currency paths other than stale are untouched.
    """

    def _stale_repo(self, tmp_path: Path) -> tuple[Path, Path, str]:
        """Build a repo whose committed HEAD differs from the indexed sha (→ stale)."""
        repo = tmp_path / "proj"
        _init_repo(repo)
        idx_dir = tmp_path / "idx"
        _write_index(idx_dir, repo.name, git_sha="0" * 40)
        return repo, idx_dir, repo.name

    def test_first_stale_emits_full_notice_and_writes_sentinel(self, tmp_path: Path) -> None:
        """The first stale prompt emits the full two-line notice and drops the stale sentinel."""
        repo, idx_dir, proj = self._stale_repo(tmp_path)
        marker = tmp_path / "spawned.marker"
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=True, marker=marker)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()

        result = _run_inject(repo, idx_dir, plugin_root, tmpdir)

        assert result.returncode == 0, result.stderr
        # Full form carries the canonical query hint and a module count.
        assert "Prefer `codemap-py query`" in result.stdout
        assert "modules" in result.stdout
        assert _stale_flag(tmpdir, proj).exists(), "the stale sentinel must be written on the full notice"

    def test_second_stale_collapses_to_single_line(self, tmp_path: Path) -> None:
        """A fresh stale sentinel collapses the notice to one line with no second body line."""
        repo, idx_dir, proj = self._stale_repo(tmp_path)
        marker = tmp_path / "spawned.marker"
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=True, marker=marker)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        # Pre-seed a fresh stale sentinel so the reminder collapses immediately.
        _stale_flag(tmpdir, proj).write_text(str(int(time.time() * 1000)))
        # Pre-seed a fresh refresh lock so the note reads "refresh in progress" deterministically.
        _lock_file(tmpdir, proj).write_text(str(int(time.time() * 1000)))

        result = _run_inject(repo, idx_dir, plugin_root, tmpdir)

        assert result.returncode == 0, result.stderr
        lines = [ln for ln in result.stdout.splitlines() if ln]
        assert lines == ["[codemap] index stale - refresh in progress"]
        # Collapsed form exits before the module-count parse — no second "Prefer" line.
        assert "Prefer `codemap-py query`" not in result.stdout

    def test_collapsed_line_refresh_pending_when_no_note(self, tmp_path: Path) -> None:
        """When neither spawn nor lock produced a note, the collapsed line reads 'refresh pending'.

        A stale-but-non-git currency cannot occur here, so drive the note-less path by pre-seeding the stale sentinel
        while removing the scan bin AND holding no lock — the hook takes the lock, finds no scan bin, releases it, and
        emits no note. The collapsed reminder then supplies its own ' · refresh pending' default.
        """
        repo, idx_dir, proj = self._stale_repo(tmp_path)
        marker = tmp_path / "spawned.marker"
        # No scan bin → spawn path releases the lock and sets no refreshNote.
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=False, marker=marker)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        _stale_flag(tmpdir, proj).write_text(str(int(time.time() * 1000)))

        result = _run_inject(repo, idx_dir, plugin_root, tmpdir)

        assert result.returncode == 0, result.stderr
        lines = [ln for ln in result.stdout.splitlines() if ln]
        assert lines == ["[codemap] index stale - refresh pending"]

    def test_stale_collapse_isolated_from_preamble_flag(self, tmp_path: Path) -> None:
        """A fresh preamble flag (current-path dedup) does NOT collapse the stale reminder.

        The two sentinels track independent conditions: seeding only the preamble flag
        must still yield the full stale notice, proving the stale path keys on its own
        ``codemap-stale-<proj>`` sentinel rather than the preamble one.
        """
        repo, idx_dir, proj = self._stale_repo(tmp_path)
        marker = tmp_path / "spawned.marker"
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=True, marker=marker)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        # Seed the PREAMBLE flag only — the stale sentinel is absent.
        _session_flag(tmpdir, proj).write_text(str(int(time.time() * 1000)))

        result = _run_inject(repo, idx_dir, plugin_root, tmpdir)

        assert result.returncode == 0, result.stderr
        assert "Prefer `codemap-py query`" in result.stdout, (
            "stale full notice must not be suppressed by the preamble flag"
        )
        assert _stale_flag(tmpdir, proj).exists()


# ── guard-redundant-scan ─────────────────────────────────────────────────────────


def _run_guard(command: str, session: str, tmpdir: Path, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Drive guard-redundant-scan.py with an isolated TMPDIR for the sentinel lookup."""
    env = {**os.environ, "TMPDIR": str(tmpdir), "TEMP": str(tmpdir), "TMP": str(tmpdir)}
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "session_id": session}
    return subprocess.run(
        [sys.executable, str(_GUARD)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=str(cwd) if cwd else None,
        env=env,
    )


def _seed_sentinel(tmpdir: Path, session: str, modules: list[str]) -> Path:
    """Write the exhausted-caller sentinel the guard reads (one module per line)."""
    sentinel = tmpdir / f"codemap-exhausted-{session}"
    sentinel.write_text("\n".join(modules) + "\n")
    return sentinel


class TestGuardRedundantScan:
    """Import-discovery grep denial keyed on the per-session exhausted sentinel."""

    @pytest.mark.parametrize(
        ("command", "form"),
        [
            pytest.param('grep -rn "import mypackage.auth" .', "dotted", id="dotted-import"),
            pytest.param('grep -rn "from mypackage/auth" src/', "slashed", id="slashed-from"),
            pytest.param('rg "import mypackage.auth"', "dotted", id="rg-dotted"),
        ],
    )
    def test_exhausted_module_grep_denied(self, command: str, form: str, tmp_path: Path) -> None:
        """A grep naming an exhausted module (dotted or slashed) is denied with a deny decision."""
        session = f"sess-{form}"
        # Sentinel carries both forms, as record-exhausted.js writes them.
        _seed_sentinel(tmp_path, session, ["mypackage.auth", "mypackage/auth"])

        result = _run_guard(command, session, tmp_path)

        assert result.returncode == 0, result.stderr
        decision = json.loads(result.stdout)["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"
        assert "mypackage.auth" in decision["permissionDecisionReason"]

    @pytest.mark.parametrize(
        "command",
        ['grep -rn "import mypackage.auth2" .', 'grep -rn "import notmypackage.auth" .'],
    )
    def test_near_miss_module_not_denied(self, command: str, tmp_path: Path) -> None:
        """A near-miss module (name contains an exhausted module as a substring) must not be denied.

        The guard uses a word-boundary matcher (guard-redundant-scan.js matchesModule), so a grep for
        ``mypackage.auth2`` or ``notmypackage.auth`` — whose names merely *contain* the exhausted ``mypackage.auth`` —
        stays allowed rather than being falsely blocked.
        """
        session = "sess-nearmiss"
        _seed_sentinel(tmp_path, session, ["mypackage.auth", "mypackage/auth"])

        result = _run_guard(command, session, tmp_path)

        assert result.returncode == 0, result.stderr
        assert result.stdout == "", f"near-miss must not be denied: {command}"

    def test_no_sentinel_allows(self, tmp_path: Path) -> None:
        """No sentinel for the session → nothing marked exhaustive → allow."""
        result = _run_guard('grep -rn "import mypackage.auth" .', "sess-none", tmp_path)

        assert result.returncode == 0, result.stderr
        assert result.stdout == ""

    def test_non_grep_command_untouched(self, tmp_path: Path) -> None:
        """A non-import-discovery command is never inspected, even with a live sentinel."""
        session = "sess-passthru"
        _seed_sentinel(tmp_path, session, ["mypackage.auth"])

        result = _run_guard("ls mypackage.auth", session, tmp_path)

        assert result.returncode == 0, result.stderr
        assert result.stdout == ""

    def test_garbage_stdin_fails_open(self, tmp_path: Path) -> None:
        """Non-JSON stdin must fail open (exit 0, no deny)."""
        result = subprocess.run(
            [sys.executable, str(_GUARD)],
            input="not json at all",
            text=True,
            capture_output=True,
            env={**os.environ, "TMPDIR": str(tmp_path)},
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""


class TestGuardCommandAnchoring:
    """Only a real search command may be denied.

    The pattern this pins replaces an unanchored ``\\bimport\\b.*-r`` alternative that named no search tool at all, so
    any command pairing the word "import" with a ``-r`` flag — a Python one-liner next to an ``rm -r``, most damagingly
    — was denied while naming an exhausted module.
    """

    _SESSION = "sess-anchor"
    _MODULES = ["mypackage.auth", "mypackage/auth"]

    @pytest.mark.parametrize(
        "command",
        [
            'python -c "import mypackage.auth" && rm -r tmp',
            "python -m pip install -r requirements.txt  # import mypackage.auth",
            "mv mypackage/auth.py mypackage/auth_v2.py",
        ],
    )
    def test_non_search_command_never_denied(self, command: str, tmp_path: Path) -> None:
        """A command that runs no search tool is allowed even while the sentinel is armed."""
        _seed_sentinel(tmp_path, self._SESSION, self._MODULES)

        result = _run_guard(command, self._SESSION, tmp_path)

        assert result.returncode == 0, result.stderr
        assert result.stdout == "", f"non-search command must not be denied: {command}"

    @pytest.mark.parametrize("command", ['grep -rn "import mypackage.auth" src/', 'rg "from mypackage.auth" -n'])
    def test_search_command_still_denied(self, command: str, tmp_path: Path) -> None:
        """Narrowing the pattern must not stop the greps the guard exists to deny."""
        _seed_sentinel(tmp_path, self._SESSION, self._MODULES)

        result = _run_guard(command, self._SESSION, tmp_path)

        assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


class TestGuardSentinelExpiry:
    """A recorded caller set stops denying once its authority can no longer be trusted.

    ``record-exhausted.py`` drops the sentinel on every Claude source edit; the TTL here bounds the mutation that hook
    never sees (a checkout, an external editor, a sibling agent), which previously left a deny armed for the rest of the
    session.
    """

    _SESSION = "sess-ttl"
    _COMMAND = 'grep -rn "import mypackage.auth" .'
    _TTL_S = 30 * 60  # guard-redundant-scan.py _SENTINEL_TTL_S

    def test_fresh_sentinel_denies(self, tmp_path: Path) -> None:
        """A just-recorded caller set is inside the TTL and still denies."""
        _seed_sentinel(tmp_path, self._SESSION, ["mypackage.auth"])

        result = _run_guard(self._COMMAND, self._SESSION, tmp_path)

        assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_expired_sentinel_allows(self, tmp_path: Path) -> None:
        """Past the TTL the same sentinel no longer has the authority to deny."""
        sentinel = _seed_sentinel(tmp_path, self._SESSION, ["mypackage.auth"])
        expired = time.time() - self._TTL_S - 60
        os.utime(sentinel, (expired, expired))

        result = _run_guard(self._COMMAND, self._SESSION, tmp_path)

        assert result.returncode == 0, result.stderr
        assert result.stdout == "", "an expired exhaustive result must not keep denying greps"


class TestGuardMissingSessionKey:
    """Two projects without a session id must not deny through one shared sentinel."""

    _COMMAND = 'grep -rn "import mypackage.auth" .'

    @staticmethod
    def _run(tmpdir: Path, project: Path) -> subprocess.CompletedProcess:
        """Drive the guard with no session_id from inside *project*."""
        env = {**os.environ, "TMPDIR": str(tmpdir), "TEMP": str(tmpdir), "TMP": str(tmpdir), "CSID": "session-one"}
        return subprocess.run(
            [sys.executable, str(_GUARD)],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": TestGuardMissingSessionKey._COMMAND}}),
            text=True,
            capture_output=True,
            cwd=str(project),
            env=env,
        )

    def test_other_projects_sentinel_does_not_deny(self, tmp_path: Path) -> None:
        """A sentinel armed under another project's fallback key is invisible here."""
        alpha, beta = tmp_path / "proj-alpha", tmp_path / "proj-beta"
        alpha.mkdir()
        beta.mkdir()
        _seed_sentinel(tmp_path, "proj-alpha-session-one", ["mypackage.auth"])

        result = self._run(tmp_path, beta)

        assert result.returncode == 0, result.stderr
        assert result.stdout == "", "a sibling project's exhausted set must never deny here"

    def test_own_fallback_sentinel_still_denies(self, tmp_path: Path) -> None:
        """The fallback key is a working key, not a disabled one."""
        alpha = tmp_path / "proj-alpha"
        alpha.mkdir()
        _seed_sentinel(tmp_path, "proj-alpha-session-one", ["mypackage.auth"])

        result = self._run(tmp_path, alpha)

        assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


# ── seed-session ─────────────────────────────────────────────────────────────────


def _run_seed(
    payload: dict,
    cwd: Path,
    tmpdir: Path,
    *,
    runtime: str = "claude",
) -> subprocess.CompletedProcess:
    """Drive seed-session.py with cwd + TMPDIR pinned so the session tmpfile is observable."""
    env = {
        **os.environ,
        "TMPDIR": str(tmpdir),
        "TEMP": str(tmpdir),
        "TMP": str(tmpdir),
        "CODEMAP_RUNTIME": runtime,
    }
    env.pop("CODEX_THREAD_ID", None)
    return subprocess.run(
        [sys.executable, str(_SEED)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=str(cwd),
        env=env,
    )


class TestSeedSession:
    """SessionStart seeding of the per-project session tmpfile."""

    def test_writes_session_id_to_project_tmpfile(self, tmp_path: Path) -> None:
        """A non-empty session_id is written to codemap-<project>-session in TMPDIR."""
        repo = tmp_path / "proj"
        _init_repo(repo)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()

        result = _run_seed({"session_id": "sid-xyz"}, repo, tmpdir)

        assert result.returncode == 0, result.stderr
        # Project name is the git-root basename.
        sidfile = tmpdir / f"codemap-{repo.name}-session"
        assert sidfile.exists()
        assert sidfile.read_text() == "sid-xyz"

    def test_empty_session_id_writes_nothing(self, tmp_path: Path) -> None:
        """An empty session_id is a no-op — no tmpfile written."""
        repo = tmp_path / "proj"
        _init_repo(repo)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()

        result = _run_seed({"session_id": ""}, repo, tmpdir)

        assert result.returncode == 0, result.stderr
        assert not (tmpdir / f"codemap-{repo.name}-session").exists()

    def test_garbage_stdin_fails_open(self, tmp_path: Path) -> None:
        """Non-JSON stdin must fail open (exit 0), writing nothing."""
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        result = subprocess.run(
            [sys.executable, str(_SEED)],
            input="}{ not json",
            text=True,
            capture_output=True,
            cwd=str(tmp_path),
            env={**os.environ, "TMPDIR": str(tmpdir)},
        )
        assert result.returncode == 0, result.stderr

    def test_codex_start_does_not_overwrite_claude_marker(self, tmp_path: Path) -> None:
        """Codex owns no shared temp marker and cannot relabel later Claude logs."""
        repo = tmp_path / "proj"
        _init_repo(repo)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        sidfile = tmpdir / f"codemap-{repo.name}-session"

        claude = _run_seed({"session_id": "claude-session"}, repo, tmpdir)
        codex = _run_seed({"thread_id": "codex-thread"}, repo, tmpdir, runtime="codex")

        assert claude.returncode == 0 and codex.returncode == 0
        assert sidfile.read_text() == "claude-session"


# ── log-skill-start ──────────────────────────────────────────────────────────────


def _run_skill(payload: dict, cwd: Path, tmpdir: Path) -> subprocess.CompletedProcess:
    """Drive log-skill-start.py with cwd + TMPDIR pinned."""
    env = {**os.environ, "TMPDIR": str(tmpdir), "TEMP": str(tmpdir), "TMP": str(tmpdir)}
    # The hook honours CODEMAP_LOG_DIR; an inherited one would redirect the shard the
    # assertions below look for, so the tests stay hermetic by dropping it.
    env.pop("CODEMAP_LOG_DIR", None)
    return subprocess.run(
        [sys.executable, str(_SKILL)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=str(cwd),
        env=env,
    )


def _skill_records(cwd: Path) -> list[dict]:
    """Return all records across flat legacy and runtime-scoped skill shards."""
    log_dir = cwd / ".cache" / "codemap" / "logs"
    records: list[dict] = []
    for shard in sorted(log_dir.rglob("skills*.jsonl")):
        records += [json.loads(line) for line in shard.read_text().splitlines() if line.strip()]
    return records


class TestLogSkillStart:
    """Record structural-query skill invocations from the pre-tool hook."""

    def test_codemap_skill_logged(self, tmp_path: Path) -> None:
        """A codemap:* Skill call appends one start record carrying skill + intent."""
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        payload = {
            "tool_name": "Skill",
            "tool_input": {"skill": "codemap-py:query-code", "args": "who calls foo"},
            "session_id": "hook-sid",
        }

        result = _run_skill(payload, tmp_path, tmpdir)

        assert result.returncode == 0, result.stderr
        records = _skill_records(tmp_path)
        assert len(records) == 1
        assert records[0]["skill"] == "codemap-py:query-code"
        assert records[0]["event"] == "start"
        assert records[0]["intent"] == "who calls foo"
        assert records[0]["layer"] == "skill"
        assert records[0]["runtime"] == "claude"
        assert records[0]["v"] not in ("", "?")

    def test_non_codemap_skill_ignored(self, tmp_path: Path) -> None:
        """A non-codemap skill is ignored — no record written."""
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        payload = {"tool_name": "Skill", "tool_input": {"skill": "foundry:audit"}}

        result = _run_skill(payload, tmp_path, tmpdir)

        assert result.returncode == 0, result.stderr
        assert _skill_records(tmp_path) == []

    def test_non_skill_tool_ignored(self, tmp_path: Path) -> None:
        """A non-Skill tool call is ignored — no record written."""
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        payload = {"tool_name": "Bash", "tool_input": {"command": "ls"}}

        result = _run_skill(payload, tmp_path, tmpdir)

        assert result.returncode == 0, result.stderr
        assert _skill_records(tmp_path) == []

    def test_seeded_session_shard_join(self, tmp_path: Path) -> None:
        """A seeded session tmpfile routes the record to skills_<session>.jsonl."""
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        # Outside a repository the project key falls back to the cwd basename.
        (tmpdir / f"codemap-{tmp_path.name}-session").write_text("seeded-sid")
        payload = {"tool_name": "Skill", "tool_input": {"skill": "codemap-py:test-impact"}}

        result = _run_skill(payload, tmp_path, tmpdir)

        assert result.returncode == 0, result.stderr
        shard = tmp_path / ".cache" / "codemap" / "logs" / "claude" / "skills_seeded-sid.jsonl"
        assert shard.exists(), "record not routed to the seeded per-session shard"
        assert json.loads(shard.read_text().strip())["session"] == "seeded-sid"

    def test_garbage_stdin_fails_open(self, tmp_path: Path) -> None:
        """Non-JSON stdin must fail open (exit 0), writing nothing."""
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        result = subprocess.run(
            [sys.executable, str(_SKILL)],
            input="not-json",
            text=True,
            capture_output=True,
            cwd=str(tmp_path),
            env={**os.environ, "TMPDIR": str(tmpdir)},
        )
        assert result.returncode == 0, result.stderr
        assert _skill_records(tmp_path) == []


# ── cross-hook session keying ────────────────────────────────────────────────────


class TestSessionKeyAgreement:
    """The marker's writer and its readers must agree on the project key from any cwd.

    ``seed-session`` keys the marker on the git-root basename, as does the cli layer
    (``codemap_py.telemetry.session_id``). The tool and skill hooks used to key it on
    ``Path.cwd().name``, so a Claude session started in a subdirectory looked for a marker
    nobody had written: every tool and skill record landed in an unsuffixed shard and the
    join across layers returned nothing while every hook still reported success.
    """

    _TOOL_HOOK = _HOOKS / "log-tool-use.py"

    @staticmethod
    def _repo_with_subdir(tmp_path: Path) -> tuple[Path, Path, Path]:
        """Return (tmpdir, repo root, nested working directory) for a throwaway repo."""
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        repo = tmp_path / "myproj"
        _init_repo(repo)
        nested = repo / "src" / "pkg"
        nested.mkdir(parents=True)
        return tmpdir, repo, nested

    @staticmethod
    def _run(hook: Path, payload: dict, cwd: Path, tmpdir: Path) -> subprocess.CompletedProcess:
        """Drive *hook* from *cwd* with TMPDIR pinned and telemetry re-enabled."""
        env = {**os.environ, "TMPDIR": str(tmpdir), "TEMP": str(tmpdir), "TMP": str(tmpdir)}
        env.pop("CODEMAP_LOG_DIR", None)
        env["CODEMAP_LOGGING"] = "true"  # conftest's autouse gate exports false suite-wide
        return subprocess.run(
            [sys.executable, str(hook)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            cwd=str(cwd),
            env=env,
        )

    def test_seed_from_subdir_writes_the_git_root_key(self, tmp_path: Path) -> None:
        """The marker is named for the repository, not for whichever directory ran the hook."""
        tmpdir, repo, nested = self._repo_with_subdir(tmp_path)

        result = self._run(_SEED, {"session_id": "sid-sub"}, nested, tmpdir)

        assert result.returncode == 0, result.stderr
        assert (tmpdir / f"codemap-{repo.name}-session").read_text() == "sid-sub"
        assert not (tmpdir / f"codemap-{nested.name}-session").exists()

    def test_tool_hook_from_subdir_joins_the_seeded_session(self, tmp_path: Path) -> None:
        """A tool record written from a subdirectory carries the seeded session id."""
        tmpdir, repo, nested = self._repo_with_subdir(tmp_path)
        (tmpdir / f"codemap-{repo.name}-session").write_text("sid-sub")

        result = self._run(self._TOOL_HOOK, {"tool_name": "Grep", "tool_input": {"pattern": "x"}}, nested, tmpdir)

        assert result.returncode == 0, result.stderr
        # Shard is asserted at the REPOSITORY root, not at *nested*: the hook anchors its
        # log dir to the project root, so a subdirectory invocation joins the same shard
        # the cli layer writes. Asserting `nested/.cache/...` here pinned the split-log
        # defect itself — two halves of one session in two directories, neither an error.
        shard = repo / ".cache" / "codemap" / "logs" / "claude" / "tools_sid-sub.jsonl"
        assert shard.exists(), "subdirectory tool record did not join the seeded session shard"
        record = json.loads(shard.read_text().strip())
        assert record["session"] == "sid-sub"
        assert record["runtime"] == "claude"
        assert record["v"] not in ("", "?")

    def test_skill_hook_from_subdir_joins_the_seeded_session(self, tmp_path: Path) -> None:
        """A skill record written from a subdirectory carries the same seeded session id."""
        tmpdir, repo, nested = self._repo_with_subdir(tmp_path)
        (tmpdir / f"codemap-{repo.name}-session").write_text("sid-sub")
        payload = {"tool_name": "Skill", "tool_input": {"skill": "codemap-py:query-code"}}

        result = self._run(_SKILL, payload, nested, tmpdir)

        assert result.returncode == 0, result.stderr
        shard = repo / ".cache" / "codemap" / "logs" / "claude" / "skills_sid-sub.jsonl"
        assert shard.exists(), "subdirectory skill record did not join the seeded session shard"
        record = json.loads(shard.read_text().strip())
        assert record["session"] == "sid-sub"
        assert record["runtime"] == "claude"
        assert record["v"] not in ("", "?")

    def test_skill_hook_does_not_mint_a_second_session(self, tmp_path: Path) -> None:
        """Reading the marker from a subdirectory must not look absent and mint a rival id."""
        tmpdir, repo, nested = self._repo_with_subdir(tmp_path)
        (tmpdir / f"codemap-{repo.name}-session").write_text("sid-sub")

        self._run(_SKILL, {"tool_name": "Skill", "tool_input": {"skill": "codemap-py:scan-codebase"}}, nested, tmpdir)

        assert [p.name for p in tmpdir.glob("codemap-*-session")] == [f"codemap-{repo.name}-session"]


class TestPromptRuntimeOutput:
    """UserPromptSubmit output remains host-specific while index state stays shared."""

    def test_codex_prompt_uses_hook_specific_output(self, tmp_path: Path) -> None:
        """A Codex prompt emits the documented hookSpecificOutput envelope."""
        repo = tmp_path / "project"
        head = _init_repo(repo)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=False, marker=tmp_path / "unused")
        _write_index(repo / ".cache" / "codemap", repo.name, git_sha=head)

        result = _run_inject(
            repo,
            repo / ".cache" / "codemap",
            plugin_root,
            tmpdir,
            runtime="codex",
        )

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        assert "[codemap]" in payload["hookSpecificOutput"]["additionalContext"]

    def test_claude_prompt_remains_plain_text(self, tmp_path: Path) -> None:
        """Adding Codex output support must not wrap Claude's existing plain preamble."""
        repo = tmp_path / "project"
        head = _init_repo(repo)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=False, marker=tmp_path / "unused")
        _write_index(repo / ".cache" / "codemap", repo.name, git_sha=head)

        result = _run_inject(
            repo,
            repo / ".cache" / "codemap",
            plugin_root,
            tmpdir,
            runtime="claude",
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith("[codemap]")
        with pytest.raises(json.JSONDecodeError):
            json.loads(result.stdout)


class TestCodexRuntimeTelemetry:
    """Codex hook calls must not inherit Claude's marker or runtime directory."""

    def test_codex_tool_record_uses_thread_id_and_codex_shard(self, tmp_path: Path) -> None:
        """A Codex Read uses CODEX_THREAD_ID even when a Claude marker is present."""
        repo = tmp_path / "project"
        _init_repo(repo)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        (tmpdir / f"codemap-{repo.name}-session").write_text("claude-session")
        env = {
            **os.environ,
            "TMPDIR": str(tmpdir),
            "TEMP": str(tmpdir),
            "TMP": str(tmpdir),
            "CODEMAP_RUNTIME": "codex",
            "CODEX_THREAD_ID": "codex-thread",
            "CODEMAP_LOGGING": "true",
        }
        env.pop("CODEMAP_LOG_DIR", None)

        result = subprocess.run(
            [sys.executable, str(TestSessionKeyAgreement._TOOL_HOOK)],
            input=json.dumps({"tool_name": "Read", "tool_input": {"file_path": "src/module.py"}}),
            text=True,
            capture_output=True,
            cwd=str(repo),
            env=env,
        )

        assert result.returncode == 0, result.stderr
        shard = repo / ".cache" / "codemap" / "logs" / "codex" / "tools_codex-thread.jsonl"
        record = json.loads(shard.read_text().strip())
        assert record["runtime"] == "codex"
        assert record["session"] == "codex-thread"
        assert not (repo / ".cache" / "codemap" / "logs" / "claude" / "tools_claude-session.jsonl").exists()
