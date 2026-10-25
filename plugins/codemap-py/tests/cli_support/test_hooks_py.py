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
import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import ClassVar

import pytest

_HOOKS = Path(__file__).parent.parent.parent / "hooks"
_INJECT = _HOOKS / "inject-preamble.py"
_GUARD = _HOOKS / "guard-redundant-scan.py"
_SEED = _HOOKS / "seed-session.py"
_SKILL = _HOOKS / "log-skill-start.py"

_INJECT_SPEC = importlib.util.spec_from_file_location("codemap_inject_preamble", _INJECT)
assert _INJECT_SPEC
assert _INJECT_SPEC.loader
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


def _write_index(idx_dir: Path, source_root: Path, *, git_sha: str, modules: int = 2) -> Path:
    """Write a minimal codemap index header the preamble hook can peek at."""
    idx_dir.mkdir(parents=True, exist_ok=True)
    file_shas = {f"m{i}": "deadbeef" for i in range(modules)}
    payload = {
        "git_sha": git_sha,
        "scanned_at": "2026-06-20T00:00:00Z",
        "scan_root": str(source_root.resolve()),
        "source_fingerprint": _INJECT_MODULE.fingerprint_file_hashes(
            _SCANNER_MODULE.get_file_hashes(source_root, _SCANNER_MODULE._load_exclusions(source_root))
        ),
        "file_shas": file_shas,
        "modules": [{"name": f"m{i}"} for i in range(modules)],
    }
    idx_path = idx_dir / f"{source_root.name}.json"
    idx_path.write_text(json.dumps(payload))
    return idx_path


def _fake_plugin_root(root: Path, *, with_scan_bin: bool, marker: Path) -> Path:
    """Create a fake CLAUDE_PLUGIN_ROOT.

    When *with_scan_bin* is True, ``bin/scan-index`` is a Python stub that touches *marker* on spawn — the observable
    proof the hook launched a refresh.
    """
    plugin_root = root / "plugin"
    bin_dir = plugin_root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    if with_scan_bin:
        scan_bin = bin_dir / "scan-index"
        scan_bin.write_text(f'from pathlib import Path\nPath({str(marker)!r}).write_text("spawned")\n')
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


def _session_flag(tmpdir: Path, proj: str, runtime: str = "claude", session: str = "shared") -> Path:
    """Return the per-session preamble-deduplication flag path.

    >>> _session_flag(Path("tmp"), "demo", "codex").name
    'codemap-preamble-demo-codex-shared'
    """
    return tmpdir / f"codemap-preamble-{proj}-{runtime}-{session}"


def _stale_flag(tmpdir: Path, proj: str, runtime: str = "claude", session: str = "shared") -> Path:
    """Return the per-session stale-index sentinel path.

    >>> _stale_flag(Path("tmp"), "demo").name
    'codemap-stale-demo-claude-shared'
    """
    return tmpdir / f"codemap-stale-{proj}-{runtime}-{session}"


def _write_stale_flag(path: Path, git_sha: str, *, expired: bool = False, currency: str = "stale") -> None:
    """Seed a stale notice record with a controlled age and index state."""
    timestamp = int(time.time() * 1000) - (_SESSION_TTL_MS + 60_000 if expired else 0)
    path.write_text(json.dumps([timestamp, git_sha, currency]), encoding="utf-8")


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
    than a warm Python process. Returns as soon as the marker exists, so a fast platform pays only the real spawn
    latency, not the ceiling.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if marker.exists():
            return True
        time.sleep(0.05)
    return marker.exists()


def _await_marker_content(path: Path, needle: str, timeout_s: float = 8.0) -> bool:
    """Poll until *path* exists and contains *needle*; the detached stub may still be running."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists() and needle in path.read_text(encoding="utf-8"):
            return True
        time.sleep(0.05)
    return False


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

    @pytest.mark.integration
    @pytest.mark.parametrize("runtime", ["claude", "codex"])
    @pytest.mark.parametrize(
        ("identity", "token"),
        [
            pytest.param("", "shared", id="empty-identity"),
            pytest.param("session.a/b\\c: d?é", "session_a_b_c__d__", id="unsafe-identity"),
        ],
    )
    def test_prompt_sentinels_use_safe_session_suffix(
        self, tmp_path: Path, runtime: str, identity: str, token: str
    ) -> None:
        """All prompt sentinels stay in the temp directory with the sanitized terminal suffix."""
        repo = tmp_path / "proj"
        _init_repo(repo)
        (repo / "pyproject.toml").write_text("")
        idx_dir = tmp_path / "idx"
        index_path = _write_index(idx_dir, repo, git_sha="0" * 40)
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=False, marker=tmp_path / "unused")
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        event = {"thread_id" if runtime == "codex" else "session_id": identity}

        stale = _run_inject_with_event(repo, idx_dir, plugin_root, tmpdir, event, runtime=runtime)
        assert stale.returncode == 0, stale.stderr
        assert "Prefer `codemap-py query`" in stale.stdout
        assert _stale_flag(tmpdir, repo.name, runtime, token).is_file()
        assert _session_flag(tmpdir, repo.name, runtime, token).is_file()
        assert json.loads(_session_marker(repo, runtime).read_text())["session_id"] == identity
        index_path.unlink()
        missing = _run_inject_with_event(repo, idx_dir, plugin_root, tmpdir, event, runtime=runtime)
        assert missing.returncode == 0, missing.stderr
        assert "No structural index" in missing.stdout
        assert (tmpdir / f"codemap-noindex-{repo.name}-{runtime}-{token}").is_file()
        assert all(path.is_file() for path in tmpdir.iterdir())

    @pytest.mark.integration
    @pytest.mark.parametrize("runtime", ["claude", "codex"])
    def test_missing_index_is_once_per_session_with_existing_ttl(self, tmp_path: Path, runtime: str) -> None:
        """Each session gets one bootstrap directive, re-emitted only after its existing TTL."""
        repo = tmp_path / "proj"
        _init_repo(repo)
        (repo / "pyproject.toml").write_text("")
        idx_dir = tmp_path / "idx"
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        for session in ("session-a", "session-b"):
            event = {"session_id": session}
            first = _run_inject_with_event(repo, idx_dir, tmp_path / "plugin", tmpdir, event, runtime=runtime)
            assert first.returncode == 0, first.stderr
            assert "ordinary query automatically prepares" in first.stdout
            assert "ask the user" not in first.stdout
            assert "AskUserQuestion" not in first.stdout
            flag = tmpdir / f"codemap-noindex-{repo.name}-{runtime}-{session}"
            stamped = flag.read_text()
            repeated = _run_inject_with_event(repo, idx_dir, tmp_path / "plugin", tmpdir, event, runtime=runtime)
            assert repeated.returncode == 0, repeated.stderr
            assert repeated.stdout == ""
            assert flag.read_text() == stamped
            flag.write_text(str(int(time.time() * 1000) - _INJECT_MODULE.NOINDEX_TTL_MS - 60_000))
            expired = _run_inject_with_event(repo, idx_dir, tmp_path / "plugin", tmpdir, event, runtime=runtime)
            assert expired.returncode == 0, expired.stderr
            assert expired.stdout == first.stdout
            assert int(flag.read_text()) > int(stamped) - 1

    @pytest.mark.integration
    @pytest.mark.parametrize("runtime", ["claude", "codex"])
    def test_current_preamble_is_per_session_and_reemits_after_ttl(self, tmp_path: Path, runtime: str) -> None:
        """Current indexes retain full preamble re-emission and silence within each session's TTL."""
        repo = tmp_path / "proj"
        head = _init_repo(repo)
        idx_dir = tmp_path / "idx"
        _write_index(idx_dir, repo, git_sha=head)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        for session in ("session-a", "session-b"):
            event = {"session_id": session}
            first = _run_inject_with_event(repo, idx_dir, tmp_path / "plugin", tmpdir, event, runtime=runtime)
            assert first.returncode == 0, first.stderr
            assert "Prefer `codemap-py query`" in first.stdout
            repeated = _run_inject_with_event(repo, idx_dir, tmp_path / "plugin", tmpdir, event, runtime=runtime)
            assert repeated.returncode == 0, repeated.stderr
            assert repeated.stdout == ""
            _session_flag(tmpdir, repo.name, runtime, session).write_text(
                str(int(time.time() * 1000) - _SESSION_TTL_MS - 60_000)
            )
            expired = _run_inject_with_event(repo, idx_dir, tmp_path / "plugin", tmpdir, event, runtime=runtime)
            assert expired.returncode == 0, expired.stderr
            assert expired.stdout == first.stdout

    @pytest.mark.integration
    @pytest.mark.parametrize("runtime", ["claude", "codex"])
    @pytest.mark.parametrize(
        ("source", "config", "ignore"),
        [
            pytest.param(None, "", "", id="tooling-only"),
            pytest.param(".venv/lib/pkg.py", "", "", id="virtualenv-only"),
            pytest.param("node_modules/pkg.py", "", "", id="dependency-directory-only"),
            pytest.param(".hidden/pkg.py", "", "", id="hidden-only"),
            pytest.param("vendor/pkg.py", "", "vendor", id="ignored-directory"),
            pytest.param("generated/api.pyi", "", "generated/*.pyi", id="ignored-stub-glob"),
            pytest.param("vendor/pkg.py", '[tool.codemap]\nexclude = ["vendor"]', "", id="configured-exclusion"),
        ],
    )
    def test_no_indexable_source_stays_silent(
        self, tmp_path: Path, runtime: str, source: str | None, config: str, ignore: str
    ) -> None:
        """Without an existing index, metadata and excluded sources never prompt or dispatch a refresh."""
        repo = tmp_path / "proj"
        _init_repo(repo)
        (repo / "a.py").unlink()
        (repo / "pyproject.toml").write_text(config)
        (repo / ".codemapignore").write_text(ignore)
        if source:
            path = repo / source
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("def task(): ...\n")
        idx_dir = tmp_path / "idx"
        marker = tmp_path / "spawned.marker"
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=True, marker=marker)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()

        result = _run_inject_with_event(repo, idx_dir, plugin_root, tmpdir, {}, runtime=runtime)

        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
        # Git's macOS launcher may create xcrun_db; only hook-owned sentinels belong to this contract.
        assert list(tmpdir.glob("codemap-*")) == []
        assert not marker.exists()
        assert (repo / ".cache/codemap" / f"current-session-{runtime}.json").is_file()

    @pytest.mark.integration
    @pytest.mark.parametrize("runtime", ["claude", "codex"])
    @pytest.mark.parametrize("source", ["task.py", "src/namespace/api.py", "src/namespace/api.pyi"])
    def test_source_without_initializer_can_prompt(self, tmp_path: Path, runtime: str, source: str) -> None:
        """Source-backed packaging markers remain eligible without package initializers."""
        repo = tmp_path / "proj"
        repo.mkdir()
        path = repo / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def task(): ...\n")
        (repo / "pyproject.toml").write_text("")
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()

        result = _run_inject_with_event(repo, tmp_path / "idx", tmp_path / "plugin", tmpdir, {}, runtime=runtime)

        assert result.returncode == 0, result.stderr
        assert "No structural index" in result.stdout

    @pytest.mark.integration
    @pytest.mark.parametrize("runtime", ["claude", "codex"])
    def test_plain_script_does_not_prompt(self, tmp_path: Path, runtime: str) -> None:
        """A marker-free standalone script emits no unsolicited bootstrap notice."""
        repo = tmp_path / "proj"
        repo.mkdir()
        (repo / "task.py").write_text("def task(): ...\n")
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()

        result = _run_inject_with_event(repo, tmp_path / "idx", tmp_path / "plugin", tmpdir, {}, runtime=runtime)

        assert result.returncode == 0, result.stderr
        assert result.stdout == ""

    def test_current_index_emits_preamble_once(self, tmp_path: Path) -> None:
        """A current index (sha matches HEAD, clean tree) emits the preamble line."""
        repo = tmp_path / "proj"
        head = _init_repo(repo)
        idx_dir = tmp_path / "idx"
        _write_index(idx_dir, repo, git_sha=head)
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
        _write_index(idx_dir, repo, git_sha=head)
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
        _write_index(idx_dir, repo, git_sha=head)
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
        _write_index(idx_dir, repo, git_sha=head)
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
        _write_index(idx_dir, repo, git_sha=head)
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
        _write_index(idx_dir, repo, git_sha=head)
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

    @pytest.mark.integration
    def test_foreign_root_cannot_appear_current(self, tmp_path: Path) -> None:
        """Matching source bytes and commit never establish provenance for another checkout."""
        repo = tmp_path / "proj"
        head = _init_repo(repo)
        idx_dir = tmp_path / "idx"
        index_path = _write_index(idx_dir, repo, git_sha=head)
        payload = json.loads(index_path.read_text())
        payload["scan_root"] = str(tmp_path / "foreign")
        index_path.write_text(json.dumps(payload))
        marker = tmp_path / "spawned.marker"
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=True, marker=marker)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()

        result = _run_inject(repo, idx_dir, plugin_root, tmpdir)

        assert result.returncode == 0, result.stderr
        assert "stale" in result.stdout
        assert _await_marker(marker)

    @pytest.mark.integration
    def test_frozen_environment_suppresses_prompt_refresh(self, tmp_path: Path, monkeypatch) -> None:
        """The frozen-index opt-out also prevents prompt-triggered background writes."""
        repo = tmp_path / "proj"
        head = _init_repo(repo)
        idx_dir = tmp_path / "idx"
        index_path = _write_index(idx_dir, repo, git_sha=head)
        before = index_path.read_bytes()
        (repo / "a.py").unlink()
        marker = tmp_path / "spawned.marker"
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=True, marker=marker)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        monkeypatch.setenv("SCAN_NO_AUTOBUILD", "1")

        result = _run_inject(repo, idx_dir, plugin_root, tmpdir)

        assert result.returncode == 0, result.stderr
        assert "stale" in result.stdout
        assert "SCAN_NO_AUTOBUILD=1" in result.stdout
        assert not _lock_file(tmpdir, repo.name).exists()
        assert not marker.exists()
        assert index_path.read_bytes() == before

    def test_refresh_record_name_matches_scanner_contract(self) -> None:
        """The scan marks only records whose name carries the hook's prefix, so the two literals must agree.

        If they drift, the scan silently stops marking records published and every stale prompt refreshes again.
        """
        from codemap_py import graph

        name = _INJECT_MODULE.refresh_record_path("proj", PurePath("/x/proj.json")).name

        assert name.startswith(graph._REFRESH_RECORD_PREFIX)


# ── inject-preamble: bounded index-header read ────────────────────────────────────


class TestInjectPreambleHeaderFields:
    """Read identity out of a bounded index prefix, never the whole file.

    The bound is the point: this runs on every prompt, and real indexes reach hundreds of MB
    (well past ``MAX_PARSE_BYTES``), so a whole-file ``json.loads`` here would either stall the
    prompt path or report every large project as ``unknown`` currency and never refresh.
    """

    @pytest.mark.integration
    def test_module_count_uses_real_modules_not_freshness_inputs(self, tmp_path: Path, scan_index: Path) -> None:
        """A Python module plus consumed config/docs and coverage metadata still displays one module."""
        repo = tmp_path / "proj"
        _init_repo(repo)
        (repo / "service.yaml").write_text("target: package.worker\n")
        (repo / "guide.rst").write_text(":func:`a.f`\n")
        scanned = subprocess.run([sys.executable, str(scan_index), "--root", str(repo)], capture_output=True, text=True)
        assert scanned.returncode == 0, scanned.stderr
        index_path = repo / ".cache" / "codemap" / f"{repo.name}.json"
        index = json.loads(index_path.read_text())
        assert len(index["modules"]) == 1
        index["file_shas"]["__coverage_mtime__"] = "123"
        index_path.write_text(json.dumps(index))

        assert _INJECT_MODULE.module_count(index_path, index_path.stat().st_size) == 1
        assert _INJECT_MODULE.module_count(index_path, _INJECT_MODULE.MAX_PARSE_BYTES + 1) == "?"
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=False, marker=tmp_path / "unused")
        prompt = _run_inject(repo, index_path.parent, plugin_root, tmpdir)
        assert prompt.returncode == 0, prompt.stderr
        assert " - 1 modules - current" in prompt.stdout

    def test_header_never_reads_beyond_prefix_budget(self, tmp_path: Path) -> None:
        """Reading a large real index must keep every read bounded to the declared header budget."""

        class BudgetedReader(io.BufferedReader):
            def read(self, size: int = -1) -> bytes:
                """Reject unbounded reads at the file I/O boundary."""
                assert 0 <= size <= _INJECT_MODULE.HEADER_PEEK_BYTES
                return super().read(size)

        class BudgetedPath(type(tmp_path)):
            def open(self, *args, **kwargs):
                """Expose the real file through a reader enforcing the header byte budget."""
                return BudgetedReader(io.FileIO(self, "r"))

        path = tmp_path / "large.json"
        path.write_text(json.dumps({"git_sha": "a" * 40, "padding": "x" * (_INJECT_MODULE.HEADER_PEEK_BYTES * 2)}))

        assert _INJECT_MODULE.header_fields(BudgetedPath(path))["git_sha"] == "a" * 40

    def test_returns_every_identity_field_from_a_normal_index(self, tmp_path: Path) -> None:
        """A small index yields git_sha, scanned_at and scan_root exactly as written."""
        idx_dir = tmp_path / ".cache" / "codemap"
        idx_path = _write_index(idx_dir, tmp_path, git_sha="a" * 40)

        fields = _INJECT_MODULE.header_fields(idx_path)

        assert fields == {
            "git_sha": "a" * 40,
            "scanned_at": "2026-06-20T00:00:00Z",
            "scan_root": str(tmp_path.resolve()),
            "source_fingerprint": _INJECT_MODULE.fingerprint_file_hashes({}),
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

        assert fields == {"git_sha": "", "scanned_at": "", "scan_root": "", "source_fingerprint": ""}


# ── inject-preamble: stale-index background refresh + lock lifecycle ──────────────


class TestInjectPreambleRefreshLock:
    """The O_EXCL lock lifecycle around the stale-index background scan spawn."""

    def _stale_repo(self, tmp_path: Path) -> tuple[Path, Path, str]:
        """Build a repo whose committed HEAD differs from the indexed sha (→ stale)."""
        repo = tmp_path / "proj"
        _init_repo(repo)
        idx_dir = tmp_path / "idx"
        # Index carries a bogus sha so currency resolves to "stale".
        _write_index(idx_dir, repo, git_sha="0" * 40)
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


class TestInjectPreambleRefreshFingerprint:
    """A stale index refreshes once per distinct scanner input, not once per prompt.

    The scanner fingerprints actual discovered source bytes. The stub publishes matching header evidence before
    acknowledging publication, preserving the same producer/consumer contract as the real scanner.
    """

    @pytest.mark.integration
    @pytest.mark.parametrize("override", ["unset", "empty", "explicit"])
    def test_public_hook_refreshes_with_optional_python_override(
        self, tmp_path: Path, scan_index: Path, override: str
    ) -> None:
        """The public launcher and its background scan agree on empty, absent and explicit interpreter overrides."""
        repo = tmp_path / "project with spaces"
        _init_repo(repo)
        idx_dir = tmp_path / "idx"
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        plugin_root = scan_index.parent.parent
        env = {
            **os.environ,
            "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
            "CODEMAP_INDEX_DIR": str(idx_dir),
            "PLUGIN_ROOT": str(plugin_root),
            "CODEMAP_RUNTIME": "codex",
            "TMPDIR": str(tmpdir),
            "TEMP": str(tmpdir),
            "TMP": str(tmpdir),
        }
        env.pop("SCAN_NO_AUTOBUILD", None)
        env.pop("CODEMAP_PYTHON", None)
        if override != "unset":
            env["CODEMAP_PYTHON"] = "" if override == "empty" else sys.executable
        scanned = subprocess.run(
            [sys.executable, str(scan_index), "--root", str(repo)], env=env, capture_output=True, text=True
        )
        assert scanned.returncode == 0, scanned.stderr
        (repo / "a.py").write_text("import pathlib\n")
        launcher = scan_index.parent / ("codemap-py.cmd" if os.name == "nt" else "codemap-py")

        result = subprocess.run(
            [str(launcher), "--run-hook", "inject-preamble.py"],
            input=json.dumps({"thread_id": "freshness-check"}),
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert "refresh started" in result.stdout
        (record,) = tmpdir.glob(f"codemap-refresh-fp-{repo.name}-*")
        assert _await_marker_content(record, "published"), record.read_text()
        index = json.loads((idx_dir / f"{repo.name}.json").read_text())
        assert index["modules"][0]["direct_imports"] == ["pathlib"]

    @pytest.mark.integration
    @pytest.mark.parametrize("change", ["unstaged-edit", "untracked-file", "delete-last-file"])
    def test_real_refresh_publishes_working_tree_graph(
        self, tmp_path: Path, scan_index: Path, monkeypatch, change: str
    ) -> None:
        """A real detached scan publishes edited modules, additions and the final deletion without staging."""
        repo = tmp_path / "proj"
        _init_repo(repo)
        (repo / ".gitignore").write_text(".cache/\n")
        idx_dir = tmp_path / "idx"
        monkeypatch.setenv("CODEMAP_INDEX_DIR", str(idx_dir))
        monkeypatch.setenv("PLUGIN_ROOT", str(scan_index.parent.parent))
        monkeypatch.delenv("SCAN_NO_AUTOBUILD", raising=False)
        scanned = subprocess.run([sys.executable, str(scan_index), "--root", str(repo)], capture_output=True, text=True)
        assert scanned.returncode == 0, scanned.stderr
        index_path = idx_dir / f"{repo.name}.json"
        original_index = index_path.read_bytes()
        if change == "unstaged-edit":
            (repo / "a.py").write_text("import pathlib\n")
        elif change == "untracked-file":
            (repo / "b.py").write_text("import a\n")
        else:
            (repo / "a.py").unlink()
        before_status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()

        result = _run_inject(repo, idx_dir, scan_index.parent.parent, tmpdir)

        assert result.returncode == 0, result.stderr
        assert "refresh started" in result.stdout
        (record,) = tmpdir.glob(f"codemap-refresh-fp-{repo.name}-*")
        assert _await_marker_content(record, "published"), record.read_text()
        index = json.loads((idx_dir / f"{repo.name}.json").read_text())
        expected_modules = (
            {"a", "b"} if change == "untracked-file" else set() if change == "delete-last-file" else {"a"}
        )
        assert {module["name"] for module in index["modules"]} == expected_modules
        if change == "unstaged-edit":
            assert index["modules"][0]["direct_imports"] == ["pathlib"]
        if change == "untracked-file":
            assert next(module for module in index["modules"] if module["name"] == "b")["direct_imports"] == ["a"]
        assert (
            subprocess.run(
                ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
            ).stdout
            == before_status
        )
        _lock_file(tmpdir, repo.name).write_text(str(int(time.time() * 1000) - _LOCK_TTL_MS - 60_000))
        aged_lock = _lock_file(tmpdir, repo.name).read_text()

        repeated = _run_inject(repo, idx_dir, scan_index.parent.parent, tmpdir)

        assert repeated.returncode == 0, repeated.stderr
        assert repeated.stdout == ""
        assert _lock_file(tmpdir, repo.name).read_text() == aged_lock

        # A past publish cannot vouch for older bytes subsequently restored to the same cache path.
        index_path.write_bytes(original_index)
        repaired = _run_inject_with_event(
            repo, idx_dir, scan_index.parent.parent, tmpdir, {"session_id": "after-rollback"}
        )

        assert repaired.returncode == 0, repaired.stderr
        assert "refresh started" in repaired.stdout
        assert _await_marker_content(record, "published"), record.read_text()
        repaired_index = json.loads(index_path.read_text())
        assert repaired_index["source_fingerprint"] == index["source_fingerprint"]
        assert repaired_index["modules"] == index["modules"]

    @staticmethod
    def _dirty_current_repo(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
        """Return ``(repo, idx_dir, index_path, plugin_root, tmpdir)`` for an index on HEAD with an unstaged edit.

        The stub ``scan-index`` does what the real scan does after a successful publish: it marks the record it was
        handed through ``CODEMAP_REFRESH_RECORD``/``CODEMAP_REFRESH_FINGERPRINT`` as ``published``.
        """
        repo = tmp_path / "proj"
        head = _init_repo(repo)
        idx_dir = tmp_path / "idx"
        index_path = _write_index(idx_dir, repo, git_sha=head)
        (repo / "a.py").write_text("def f():\n    return 2\n")
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=True, marker=tmp_path / "spawned.marker")
        (plugin_root / "bin" / "scan-index").write_text(
            "#!/usr/bin/env python3\nimport json, os, sys\nfrom pathlib import Path\n"
            f"sys.path.insert(0, {str(_HOOKS.parent / 'src')!r})\n"
            "from codemap_py import scanner\n"
            "from codemap_py.scanner.discovery import fingerprint_file_hashes\n"
            f"root = Path({str(repo)!r})\nindex_path = Path({str(index_path)!r})\n"
            "payload = json.loads(index_path.read_text())\n"
            "payload['source_fingerprint'] = fingerprint_file_hashes(scanner.get_file_hashes(root, scanner._load_exclusions(root)))\n"
            "index_path.write_text(json.dumps(payload))\n"
            "Path(os.environ['CODEMAP_REFRESH_RECORD']).write_text("
            "os.environ['CODEMAP_REFRESH_FINGERPRINT'] + '\\n1\\npublished', encoding='utf-8')\n"
        )
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        return repo, idx_dir, index_path, plugin_root, tmpdir

    @staticmethod
    def _complete_refresh(index_path: Path, tmpdir: Path, proj: str) -> None:
        """Wait for publication and expire the refresh lock and notice so refresh notes are observable."""
        (record,) = tmpdir.glob(f"codemap-refresh-fp-{proj}-*")
        assert _await_marker_content(record, "published")
        _lock_file(tmpdir, proj).write_text(str(int(time.time() * 1000) - _LOCK_TTL_MS - 60_000))
        _write_stale_flag(_stale_flag(tmpdir, proj), json.loads(index_path.read_text())["git_sha"], expired=True)

    def test_unchanged_dirty_tree_spawns_one_refresh(self, tmp_path: Path) -> None:
        """A second prompt on the same dirty tree stays silent and never takes the lock again.

        The lock keeps its aged timestamp, which proves no spawn was attempted: every spawn path rewrites it.
        """
        repo, idx_dir, index_path, plugin_root, tmpdir = self._dirty_current_repo(tmp_path)
        first = _run_inject(repo, idx_dir, plugin_root, tmpdir)
        assert "refresh started" in first.stdout, first.stderr
        self._complete_refresh(index_path, tmpdir, repo.name)
        aged_lock = _lock_file(tmpdir, repo.name).read_text()

        second = _run_inject(repo, idx_dir, plugin_root, tmpdir)

        assert second.returncode == 0, second.stderr
        assert second.stdout == ""
        assert _lock_file(tmpdir, repo.name).read_text() == aged_lock

    @pytest.mark.parametrize(
        ("change", "expected_note"),
        [
            pytest.param("unstaged-edit", "refresh started", id="unstaged-edit-refreshes"),
            pytest.param("untracked-file", "refresh started", id="untracked-file-refreshes"),
            pytest.param("ignored-file", "refresh started", id="discovered-ignored-file-refreshes"),
            pytest.param("delete-last-file", "refresh started", id="last-source-deletion-refreshes"),
            pytest.param("staged-edit", "", id="staging-identical-source-stays-settled"),
            pytest.param("head-moved", "refresh started", id="head-move-refreshes"),
        ],
    )
    def test_only_scanner_visible_changes_refresh_again(self, tmp_path: Path, change: str, expected_note: str) -> None:
        """Refresh changed scanner inputs, including unstaged additions and the final source deletion."""
        repo, idx_dir, index_path, plugin_root, tmpdir = self._dirty_current_repo(tmp_path)
        _run_inject(repo, idx_dir, plugin_root, tmpdir)
        self._complete_refresh(index_path, tmpdir, repo.name)
        edits = {
            "unstaged-edit": lambda: (repo / "a.py").write_text("def f():\n    return 3\n"),
            "untracked-file": lambda: (repo / "b.py").write_text("def g():\n    return 4\n"),
            "staged-edit": lambda: _git(repo, "add", "a.py"),
            "head-moved": lambda: _git(repo, "commit", "-q", "--allow-empty", "-m", "move head"),
            "ignored-file": lambda: (
                (repo / ".gitignore").write_text("scratch.py\n"),
                (repo / "scratch.py").write_text("def scratch():\n    return 5\n"),
            ),
            "delete-last-file": lambda: (repo / "a.py").unlink(),
        }
        edits[change]()

        result = _run_inject(repo, idx_dir, plugin_root, tmpdir)

        assert result.returncode == 0, result.stderr
        if expected_note:
            assert expected_note in result.stdout
        else:
            assert result.stdout == ""

    def test_refresh_that_never_published_is_retried_even_after_another_publish(self, tmp_path: Path) -> None:
        """A spawned refresh that never marked its record published is retried, whoever else wrote the index.

        Reproduces the lock-takeover race: refresh B fails ``index_busy`` behind an older, slower scan A, and A's
        publish lands after B's spawn. An index mtime newer than the spawn used to settle B's fingerprint with A's
        older content; only B's own ``published`` mark may.
        """
        repo, idx_dir, index_path, plugin_root, tmpdir = self._dirty_current_repo(tmp_path)
        (plugin_root / "bin" / "scan-index").write_text("#!/usr/bin/env python3\nraise SystemExit(1)\n")
        _run_inject(repo, idx_dir, plugin_root, tmpdir)
        later_ns = time.time_ns() + 5_000_000_000
        os.utime(index_path, ns=(later_ns, later_ns))
        _lock_file(tmpdir, repo.name).write_text(str(int(time.time() * 1000) - _LOCK_TTL_MS - 60_000))
        _write_stale_flag(_stale_flag(tmpdir, repo.name), json.loads(index_path.read_text())["git_sha"], expired=True)

        result = _run_inject(repo, idx_dir, plugin_root, tmpdir)

        assert "refresh started" in result.stdout

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param("", id="empty"),
            pytest.param("not-a-record", id="garbled"),
            pytest.param("abc\n1\nspawned", id="spawned-not-published"),
            pytest.param("abc\n1", id="legacy-two-line-record"),
            pytest.param("xyz\n1\npublished", id="other-fingerprint"),
        ],
    )
    def test_unpublished_or_corrupt_record_reads_as_no_refresh(self, tmp_path: Path, content: str) -> None:
        """Only this fingerprint's ``published`` line settles a refresh; anything else lets the next prompt retry."""
        record = tmp_path / "record"
        record.write_text(content, encoding="utf-8")

        assert _INJECT_MODULE.refresh_settled(record, "abc") is False

    def test_spawn_hands_the_record_to_the_scan(self, monkeypatch, tmp_path: Path) -> None:
        """The record is written ``spawned`` before the child starts, and its path and fingerprint reach the child."""
        calls: list[dict] = []
        record = tmp_path / "codemap-refresh-fp-proj-0123456789ab"

        def _fake_popen(command: list[str], **kwargs: object) -> object:
            """Capture the spawn environment and the record state at spawn time."""
            calls.append({"env": kwargs["env"], "record": record.read_text(encoding="utf-8")})
            return object()

        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("PLUGIN_ROOT", str(_fake_plugin_root(tmp_path, with_scan_bin=True, marker=tmp_path / "m")))
        monkeypatch.setattr(_INJECT_MODULE.subprocess, "Popen", _fake_popen)

        note = _INJECT_MODULE.start_refresh("proj", tmp_path, tmp_path, "", None, record=(record, "abc"))

        assert note == _INJECT_MODULE.STARTED_NOTE
        assert calls[0]["record"].split("\n")[::2] == ["abc", "spawned"]
        assert calls[0]["env"]["CODEMAP_REFRESH_RECORD"] == str(record)
        assert calls[0]["env"]["CODEMAP_REFRESH_FINGERPRINT"] == "abc"

    @pytest.mark.parametrize(
        "index_path",
        [
            pytest.param(PurePosixPath("/home/me/proj/.cache/codemap/proj.json"), id="posix"),
            pytest.param(PureWindowsPath(r"C:\Users\me\proj\.cache\codemap\proj.json"), id="windows"),
        ],
    )
    def test_record_name_is_a_portable_filename(self, monkeypatch, tmp_path: Path, index_path: PurePath) -> None:
        """Any host's index path hashes to a plain ``codemap-refresh-fp-<project>-<hex>`` name inside the temp dir."""
        monkeypatch.setenv("TMPDIR", str(tmp_path))

        record = _INJECT_MODULE.refresh_record_path("proj", index_path)

        assert record.parent == tmp_path
        assert re.fullmatch(r"codemap-refresh-fp-proj-[0-9a-f]{12}", record.name)


class TestRefreshFingerprint:
    """The refresh fingerprint moves exactly when scanner inputs or HEAD move."""

    @pytest.mark.parametrize(
        ("change", "moves"),
        [
            pytest.param("unstaged-edit", True, id="unstaged-edit"),
            pytest.param("untracked-file", True, id="untracked-file"),
            pytest.param("staged-edit", True, id="staged-edit"),
            pytest.param("delete-last-file", True, id="delete-last-file"),
            pytest.param("head-moved", True, id="head-moved"),
            pytest.param("unrelated-file", False, id="unrelated-file"),
            pytest.param("excluded-file", False, id="excluded-file"),
        ],
    )
    def test_moves_only_for_scanner_inputs(self, tmp_path: Path, change: str, moves: bool) -> None:
        """Observe each filesystem change separately, preserving scanner exclusions."""
        repo = tmp_path / "proj"
        head = _init_repo(repo)
        before = _INJECT_MODULE.refresh_fingerprint(repo, head)
        if change in {"unstaged-edit", "staged-edit"}:
            (repo / "a.py").write_text("def f():\n    return 2\n")
        elif change == "untracked-file":
            (repo / "b.py").write_text("def g():\n    return 3\n")
        elif change == "delete-last-file":
            (repo / "a.py").unlink()
        elif change == "unrelated-file":
            (repo / "notes.txt").write_text("not indexed\n")
        elif change == "excluded-file":
            (repo / ".hidden").mkdir()
            (repo / ".hidden" / "excluded.py").write_text("x = 1\n")
        if change == "staged-edit":
            _git(repo, "add", "a.py")
        if change == "head-moved":
            _git(repo, "commit", "-q", "--allow-empty", "-m", "move head")
        after_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True
        ).stdout.strip()

        after = _INJECT_MODULE.refresh_fingerprint(repo, after_head)

        assert before is not None
        assert after is not None
        assert (after != before) is moves

    def test_non_git_source_changes_have_fingerprints(self, tmp_path: Path) -> None:
        """Non-Git projects retain repeat suppression while detecting actual source edits."""
        source = tmp_path / "a.py"
        source.write_text("x = 1\n")
        before = _INJECT_MODULE.refresh_fingerprint(tmp_path, "")
        source.write_text("x = 2\n")
        after = _INJECT_MODULE.refresh_fingerprint(tmp_path, "")
        assert before is not None
        assert after is not None
        assert after != before


# ── inject-preamble: session marker (cross-agent scan-query contract) ─────────────


class TestInjectPreambleChangedCount:
    """Refresh provenance counts eligible working-tree differences from the indexed commit, not reparse work.

    Invalid or unreachable commit identities leave the count unknown.
    """

    @pytest.mark.integration
    @pytest.mark.parametrize("change", ["none", "source-edit", "input-edit", "input-delete"])
    @pytest.mark.parametrize(
        ("relative", "content"),
        [
            pytest.param("service.yaml", "target: package.worker\n", id="root-yaml"),
            pytest.param("pyproject.toml", "[tool.example]\nvalue = 1\n", id="root-toml"),
            pytest.param(".codemapignore", "vendor\ndocs/*.md\n.hidden\n", id="ignore-rules"),
            pytest.param("docs/guide.md", "::: package.worker\n", id="excluded-markdown-consumed"),
            pytest.param("vendor/guide.rst", ":func:`a.f`\n", id="excluded-rst-consumed"),
            pytest.param(".hidden/guide.rst", ":func:`a.f`\n", id="hidden-rst-consumed"),
        ],
    )
    def test_consumed_inputs_have_symmetric_change_counts(
        self, tmp_path: Path, change: str, relative: str, content: str
    ) -> None:
        """Clean consumed inputs count zero; source/config/doc edits and deletion each count one path."""
        repo = tmp_path / "proj"
        _init_repo(repo)
        (repo / ".codemapignore").write_text("vendor\ndocs/*.md\n.hidden\n")
        input_path = repo / relative
        input_path.parent.mkdir(parents=True, exist_ok=True)
        input_path.write_text(content)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "consumed inputs")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        if change == "source-edit":
            (repo / "a.py").write_text("import pathlib\n")
        elif change == "input-edit":
            input_path.write_text(content + "\n")
        elif change == "input-delete":
            input_path.unlink()

        assert _INJECT_MODULE.changed_file_count(repo, head) == (0 if change == "none" else 1)

    @pytest.mark.integration
    @pytest.mark.parametrize("relative", ["alias.py", "guide.rst", "docs/guide.md", "service.yaml"])
    def test_historical_symlink_inputs_do_not_invent_deleted_source(self, tmp_path: Path, relative: str) -> None:
        """Git symlink blobs are not source bytes; skipped sources count zero and consumed configs stay unknown."""
        repo = tmp_path / "proj"
        _init_repo(repo)
        link_blob = subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            input="target.txt",
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        # Construct the historical link in Git without requiring host symlink privileges.
        _git(repo, "update-index", "--add", "--cacheinfo", f"120000,{link_blob},{relative}")
        _git(repo, "commit", "-q", "-m", "historical link")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()

        expected = None if relative == "service.yaml" else 0
        assert _INJECT_MODULE.changed_file_count(repo, head) == expected

    @staticmethod
    def _child_records_count(plugin_root: Path, marker: Path) -> None:
        (plugin_root / "bin" / "scan-index").write_text(
            "#!/usr/bin/env python3\nimport json, os\nfrom pathlib import Path\n"
            f"Path({str(marker)!r}).write_text(json.dumps({{'changed': os.environ.get('CODEMAP_REFRESH_CHANGED_COUNT')}}))\n"
        )

    @pytest.mark.parametrize("behind_head", [False, True])
    def test_refresh_count_filters_scanner_exclusions(self, tmp_path: Path, behind_head: bool) -> None:
        """Changed source, docs and ignore rules count; excluded Python paths contribute no changes."""
        repo = tmp_path / "proj"
        index_sha = _init_repo(repo)
        (repo / ".codemapignore").write_text("vendor\ngenerated/*.py\n")
        for relative in ("vendor/a.py", "generated/a.py", ".sandbox/a.py", "__pycache__/a.py", "docs/guide.rst"):
            path = repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("content\n")
        (repo / "a.py").write_text("changed = True\n")
        _git(repo, "add", "-A")
        if behind_head:
            _git(repo, "commit", "-q", "-m", "changed")
        assert _INJECT_MODULE.changed_file_count(repo, index_sha) == 3

    @pytest.mark.parametrize(
        "git_sha",
        [
            pytest.param("", id="empty"),
            pytest.param("--output=planted.txt", id="option-shaped"),
            pytest.param("HEAD~1", id="symbolic-revision"),
            pytest.param("abc12", id="too-short"),
            pytest.param("g" * 40, id="non-hex"),
        ],
    )
    def test_non_hex_index_sha_is_unknown_without_git_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_sha: str
    ) -> None:
        """An index-header sha that is not a hex object name never reaches git; the count is unknown.

        The header is unauthenticated on-disk input. An option-shaped value would otherwise be parsed by git as a flag
        (``--output`` writes a file at an attacker-chosen path), so the guard is the whole defense.
        """
        monkeypatch.setattr(_INJECT_MODULE.subprocess, "run", lambda *a, **k: pytest.fail("no git call expected"))
        assert _INJECT_MODULE.changed_file_count(tmp_path, git_sha) is None

    @pytest.mark.parametrize(
        ("stage", "expected"),
        [
            pytest.param(False, 1, id="unstaged-edit-counted"),
            pytest.param(True, 1, id="staged-edit-reparsed"),
        ],
    )
    def test_same_commit_counts_working_tree_edits(self, tmp_path: Path, stage: bool, expected: int) -> None:
        """An eligible source edit counts regardless of staging."""
        repo = tmp_path / "proj"
        head = _init_repo(repo)
        (repo / "a.py").write_text("def f():\n    return 2\n")
        if stage:
            _git(repo, "add", "a.py")

        assert _INJECT_MODULE.changed_file_count(repo, head) == expected

    def test_untracked_indexed_file_counts(self, tmp_path: Path) -> None:
        """An untracked stub is a discovered scanner input and counts as an addition."""
        repo = tmp_path / "proj"
        head = _init_repo(repo)
        (repo / "c.pyi").write_text("def c() -> int: ...\n")

        assert _INJECT_MODULE.changed_file_count(repo, head) == 1

    def test_held_lock_never_evaluates_count(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """While a refresh is in progress the count thunk stays unevaluated, so no git call runs on that prompt.

        The lock is held for up to ten minutes during a scan; every prompt in that window returns "in progress" and must
        not pay for a diff against a possibly distant index commit.
        """
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        lock = tmp_path / "codemap-refresh-proj"
        lock.write_text(str(int(time.time() * 1000)))

        note = _INJECT_MODULE.start_refresh(
            "proj", tmp_path, tmp_path, "", lambda: pytest.fail("count evaluated while lock held")
        )

        assert note == " - refresh in progress"

    def test_raising_count_thunk_still_starts_refresh_and_releases_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A count that raises (for example a decode error on a raw git path) leaves the count unknown, not the lock
        held.

        The thunk runs after the lock is taken; an exception escaping here would carry the lock out of the hook and turn
        every prompt for the next ten minutes into "refresh in progress" with no scan running.
        """
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        marker = tmp_path / "child.json"
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=True, marker=marker)
        self._child_records_count(plugin_root, marker)
        monkeypatch.setenv("PLUGIN_ROOT", str(plugin_root))

        def _boom() -> int:
            raise UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "invalid start byte")

        note = _INJECT_MODULE.start_refresh("proj", tmp_path, tmp_path, "", _boom)

        assert note == " - refresh started"
        assert _await_marker(marker)
        assert json.loads(marker.read_text()) == {"changed": None}

    def test_unreachable_index_commit_is_unknown(self, tmp_path: Path) -> None:
        """An index built from a commit git cannot resolve reports no count rather than a partial one."""
        repo = tmp_path / "proj"
        _init_repo(repo)
        assert _INJECT_MODULE.changed_file_count(repo, "0" * 40) is None

    def test_index_behind_head_counts_all_changed_source_paths_once(self, tmp_path: Path) -> None:
        """Committed, staged, unstaged and untracked source paths count once; unrelated files never count."""
        repo = tmp_path / "proj"
        old = _init_repo(repo)
        (repo / "b.py").write_text("x = 1\n")
        (repo / "notes.txt").write_text("not indexed\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "second")
        (repo / "b.py").write_text("x = 2\n")  # committed and staged again: one path
        (repo / "with space.py").write_text("y = 1\n")  # staged, indexed, quoted by porcelain-style output
        _git(repo, "add", "b.py", "with space.py")
        (repo / "a.py").write_text("def f():\n    return 3\n")  # unstaged edit: scanner input
        (repo / "c.pyi").write_text("def c() -> int: ...\n")  # untracked: scanner input
        (repo / "README").write_text("ignored\n")  # untracked, not indexed

        assert _INJECT_MODULE.changed_file_count(repo, old) == 4

    @pytest.mark.integration
    def test_staged_edit_same_commit_child_receives_count(self, tmp_path: Path) -> None:
        """The spawned scan receives one source change without counting unchanged committed configuration."""
        repo = tmp_path / "proj"
        _init_repo(repo)
        (repo / "service.yaml").write_text("target: package.worker\n")
        _git(repo, "add", "service.yaml")
        _git(repo, "commit", "-q", "-m", "configuration")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        idx_dir = tmp_path / "idx"
        _write_index(idx_dir, repo, git_sha=head)
        (repo / "a.py").write_text("def f():\n    return 2\n")
        _git(repo, "add", "a.py")
        marker = tmp_path / "child.json"
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=True, marker=marker)
        self._child_records_count(plugin_root, marker)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()

        result = _run_inject(repo, idx_dir, plugin_root, tmpdir)

        assert result.returncode == 0, result.stderr
        assert _await_marker(marker), result.stdout
        assert json.loads(marker.read_text()) == {"changed": "1"}

    @pytest.mark.integration
    def test_unreachable_index_commit_child_receives_no_count(self, tmp_path: Path) -> None:
        """An unresolvable index commit leaves the env key unset so the record says unknown."""
        repo = tmp_path / "proj"
        _init_repo(repo)
        idx_dir = tmp_path / "idx"
        _write_index(idx_dir, repo, git_sha="0" * 40)
        marker = tmp_path / "child.json"
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=True, marker=marker)
        self._child_records_count(plugin_root, marker)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()

        result = _run_inject(repo, idx_dir, plugin_root, tmpdir)

        assert result.returncode == 0, result.stderr
        assert _await_marker(marker), result.stdout
        assert json.loads(marker.read_text()) == {"changed": None}


class TestInjectPreambleSessionMarker:
    """The runtime-sharded repository session marker written every invocation.

    scan-query reads this marker to correlate queries with the session that triggered a refresh (the coverage-diet
    dedup). The hook must write it UNCONDITIONALLY after resolving the project root — before the no-index early exit,
    before the early return for a current-path session flag — so the marker's ts advances on every prompt even when the
    preamble itself is suppressed. It carries single-line JSON ``{"session_id","ts"}`` + trailing newline, and fails
    open (never throws).
    """

    @pytest.mark.integration
    @pytest.mark.parametrize("runtime", ["claude", "codex"])
    def test_payload_session_reaches_refresh_child(self, tmp_path: Path, runtime: str) -> None:
        """The detached process receives the event identity, not a per-process fallback."""
        repo = tmp_path / "proj"
        _init_repo(repo)
        idx_dir = tmp_path / "idx"
        _write_index(idx_dir, repo, git_sha="0" * 40)
        marker = tmp_path / "child.json"
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=True, marker=marker)
        scan_bin = plugin_root / "bin" / "scan-index"
        key = "CODEX_THREAD_ID" if runtime == "codex" else "CLAUDE_CODE_SESSION_ID"
        scan_bin.write_text(
            "#!/usr/bin/env python3\nimport json, os\nfrom pathlib import Path\n"
            f"Path({str(marker)!r}).write_text(json.dumps({{'session': os.environ.get({key!r}), "
            "'runtime': os.environ.get('CODEMAP_RUNTIME'), 'trigger': os.environ.get('CODEMAP_REFRESH_TRIGGER')}))\n"
        )
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        result = _run_inject_with_event(
            repo, idx_dir, plugin_root, tmpdir, {"prompt": "inspect", "session_id": "payload-thread"}, runtime=runtime
        )
        assert result.returncode == 0, result.stderr
        assert _await_marker(marker), result.stdout
        assert json.loads(marker.read_text()) == {
            "session": "payload-thread",
            "runtime": runtime,
            "trigger": f"{runtime}_prompt_background",
        }

    def test_marker_written_with_session_id_and_ts(self, tmp_path: Path) -> None:
        """A current-index turn writes the marker with the stdin session_id and an ms ts."""
        repo = tmp_path / "proj"
        head = _init_repo(repo)
        idx_dir = tmp_path / "idx"
        _write_index(idx_dir, repo, git_sha=head)
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
        _write_index(idx_dir, repo, git_sha=head)
        marker = tmp_path / "spawned.marker"
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=True, marker=marker)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        # Pre-seed a fresh session flag so the current-index turn dedups to a silent exit.
        _session_flag(tmpdir, repo.name, session="sid-dedup").write_text(str(int(time.time() * 1000)))

        first = _run_inject_with_event(repo, idx_dir, plugin_root, tmpdir, {"prompt": "hi", "session_id": "sid-dedup"})
        first_ts = json.loads(_session_marker(repo).read_text())["ts"]
        time.sleep(0.01)
        second = _run_inject_with_event(repo, idx_dir, plugin_root, tmpdir, {"prompt": "hi", "session_id": "sid-dedup"})

        assert first.returncode == 0
        assert second.returncode == 0
        assert second.stdout == "", "second current-index turn must dedup to a silent exit"
        second_ts = json.loads(_session_marker(repo).read_text())["ts"]
        assert second_ts >= first_ts, "marker ts must advance even on a deduped turn"

    def test_marker_session_id_empty_on_unparsable_stdin(self, tmp_path: Path) -> None:
        """Unparsable stdin fails open: marker written with session_id '' (no throw, exit 0)."""
        repo = tmp_path / "proj"
        head = _init_repo(repo)
        idx_dir = tmp_path / "idx"
        _write_index(idx_dir, repo, git_sha=head)
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
        _write_index(idx_dir, repo, git_sha=head)
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

        assert claude.returncode == 0
        assert codex.returncode == 0
        assert json.loads(_session_marker(repo, "claude").read_text())["session_id"] == "claude-session"
        assert json.loads(_session_marker(repo, "codex").read_text())["session_id"] == "codex-thread"


# ── inject-preamble: stale-reminder collapse ──────────────────────────────────────


class TestInjectPreambleStaleCollapse:
    """Suppress repeated stale notices until their index state changes or their TTL expires."""

    def _stale_repo(self, tmp_path: Path) -> tuple[Path, Path, str]:
        """Build a repo whose committed HEAD differs from the indexed sha (→ stale)."""
        repo = tmp_path / "proj"
        _init_repo(repo)
        idx_dir = tmp_path / "idx"
        _write_index(idx_dir, repo, git_sha="0" * 40)
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

    @pytest.mark.integration
    @pytest.mark.parametrize("runtime", ["claude", "codex"])
    def test_expired_unchanged_stale_reemits_full_preamble(self, tmp_path: Path, runtime: str) -> None:
        """An unchanged stale state re-delivers query context after the reminder TTL."""
        repo, idx_dir, proj = self._stale_repo(tmp_path)
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=False, marker=tmp_path / "unused")
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        first = _run_inject(repo, idx_dir, plugin_root, tmpdir, runtime=runtime)
        assert first.returncode == 0, first.stderr
        assert "Prefer `codemap-py query`" in first.stdout
        flag = _stale_flag(tmpdir, proj, runtime)
        _write_stale_flag(flag, "0" * 40, expired=True)

        repeated = _run_inject(repo, idx_dir, plugin_root, tmpdir, runtime=runtime)

        assert repeated.returncode == 0, repeated.stderr
        assert "Prefer `codemap-py query`" in repeated.stdout
        assert "modules" in repeated.stdout
        assert json.loads(flag.read_text())[0] > int(time.time() * 1000) - _SESSION_TTL_MS
        quiet = _run_inject(repo, idx_dir, plugin_root, tmpdir, runtime=runtime)
        assert quiet.returncode == 0, quiet.stderr
        assert quiet.stdout == ""

    @pytest.mark.integration
    @pytest.mark.parametrize("runtime", ["claude", "codex"])
    def test_current_clears_stale_state_before_suppressed_preamble(self, tmp_path: Path, runtime: str) -> None:
        """A current index resets stale dedup even when its current preamble is suppressed."""
        repo = tmp_path / "proj"
        head = _init_repo(repo)
        idx_dir = tmp_path / "idx"
        _write_index(idx_dir, repo, git_sha=head)
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=False, marker=tmp_path / "unused")
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        source = repo / "a.py"
        source.write_text("def f():\n    return 2\n")
        first = _run_inject(repo, idx_dir, plugin_root, tmpdir, runtime=runtime)
        assert first.returncode == 0, first.stderr
        assert "Prefer `codemap-py query`" in first.stdout
        flag = _stale_flag(tmpdir, repo.name, runtime)
        assert json.loads(flag.read_text())[1:] == [head, "stale"]

        source.write_text("def f():\n    return 1\n")
        current = _run_inject(repo, idx_dir, plugin_root, tmpdir, runtime=runtime)
        assert current.returncode == 0, current.stderr
        assert current.stdout == ""
        assert not flag.exists()

        source.write_text("def f():\n    return 2\n")
        stale = _run_inject(repo, idx_dir, plugin_root, tmpdir, runtime=runtime)
        assert stale.returncode == 0, stale.stderr
        assert "Prefer `codemap-py query`" in stale.stdout
        assert json.loads(flag.read_text())[1:] == [head, "stale"]

    def test_changed_stale_collapses_to_single_line(self, tmp_path: Path) -> None:
        """A changed stale record emits one line within TTL and suppresses subsequent unchanged prompts."""
        repo, idx_dir, proj = self._stale_repo(tmp_path)
        marker = tmp_path / "spawned.marker"
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=True, marker=marker)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        _write_stale_flag(_stale_flag(tmpdir, proj), "1" * 40)
        # Pre-seed a fresh refresh lock so the note reads "refresh in progress" deterministically.
        _lock_file(tmpdir, proj).write_text(str(int(time.time() * 1000)))

        result = _run_inject(repo, idx_dir, plugin_root, tmpdir)

        assert result.returncode == 0, result.stderr
        lines = [ln for ln in result.stdout.splitlines() if ln]
        assert lines == ["[codemap] index stale - refresh in progress"]
        # Collapsed form exits before the module-count parse — no second "Prefer" line.
        assert "Prefer `codemap-py query`" not in result.stdout
        stamped = _stale_flag(tmpdir, proj).read_text()
        assert json.loads(stamped)[0] > int(time.time() * 1000) - _SESSION_TTL_MS
        for _ in range(3):
            repeated = _run_inject(repo, idx_dir, plugin_root, tmpdir)
            assert repeated.returncode == 0, repeated.stderr
            assert repeated.stdout == ""
            assert _stale_flag(tmpdir, proj).read_text() == stamped

    def test_collapsed_line_refresh_pending_when_no_note(self, tmp_path: Path) -> None:
        """When neither spawn nor lock produced a note, the collapsed line reads 'refresh pending'.

        A stale-but-non-git currency cannot occur here, so change the stale state key within TTL
        while removing the scan bin AND holding no lock — the hook takes the lock, finds no scan bin, releases it, and
        emits no note. The collapsed reminder then supplies its own ' - refresh pending' default.
        """
        repo, idx_dir, proj = self._stale_repo(tmp_path)
        marker = tmp_path / "spawned.marker"
        # No scan bin → spawn path releases the lock and sets no refreshNote.
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=False, marker=marker)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        _write_stale_flag(_stale_flag(tmpdir, proj), "1" * 40)

        result = _run_inject(repo, idx_dir, plugin_root, tmpdir)

        assert result.returncode == 0, result.stderr
        lines = [ln for ln in result.stdout.splitlines() if ln]
        assert lines == ["[codemap] index stale - refresh pending"]

    def test_stale_collapse_isolated_from_preamble_flag(self, tmp_path: Path) -> None:
        """A fresh preamble flag (current-path dedup) does NOT collapse the stale reminder.

        The two sentinels track independent conditions: seeding only the preamble flag
        must still yield the full stale notice, proving the stale path keys on its own
        session-scoped stale sentinel rather than the preamble one.
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

    @pytest.mark.integration
    @pytest.mark.parametrize("runtime", ["claude", "codex"])
    def test_repeated_stale_prompts_are_silent(self, tmp_path: Path, runtime: str) -> None:
        """The first stale preamble stamps its state and all unchanged prompts stay silent."""
        repo, idx_dir, proj = self._stale_repo(tmp_path)
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=False, marker=tmp_path / "unused")
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()

        first = _run_inject(repo, idx_dir, plugin_root, tmpdir, runtime=runtime)
        assert first.returncode == 0, first.stderr
        assert "Prefer `codemap-py query`" in first.stdout
        stamped = _stale_flag(tmpdir, proj, runtime).read_text()
        assert json.loads(stamped)[1:] == ["0" * 40, "stale"]
        for _ in range(3):
            repeated = _run_inject(repo, idx_dir, plugin_root, tmpdir, runtime=runtime)
            assert repeated.returncode == 0, repeated.stderr
            assert repeated.stdout == ""
            assert _stale_flag(tmpdir, proj, runtime).read_text() == stamped

    @pytest.mark.integration
    @pytest.mark.parametrize("runtime", ["claude", "codex"])
    @pytest.mark.parametrize("changed_field", ["git_sha", "currency"])
    def test_changed_state_emits_immediately(self, tmp_path: Path, runtime: str, changed_field: str) -> None:
        """Either part of the recorded state key changing emits once inside the TTL."""
        repo, idx_dir, proj = self._stale_repo(tmp_path)
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=False, marker=tmp_path / "unused")
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        flag = _stale_flag(tmpdir, proj, runtime)
        _write_stale_flag(flag, "0" * 40, currency="current" if changed_field == "currency" else "stale")
        if changed_field == "git_sha":
            _write_index(idx_dir, repo, git_sha="1" * 40)

        result = _run_inject(repo, idx_dir, plugin_root, tmpdir, runtime=runtime)
        assert result.returncode == 0, result.stderr
        context = (
            json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
            if runtime == "codex"
            else result.stdout.strip()
        )
        assert context == "[codemap] index stale - refresh pending"
        assert json.loads(flag.read_text())[1:] == ["1" * 40 if changed_field == "git_sha" else "0" * 40, "stale"]
        repeated = _run_inject(repo, idx_dir, plugin_root, tmpdir, runtime=runtime)
        assert repeated.returncode == 0, repeated.stderr
        assert repeated.stdout == ""

    @pytest.mark.integration
    @pytest.mark.parametrize("runtime", ["claude", "codex"])
    def test_sessions_have_independent_stale_notices(self, tmp_path: Path, runtime: str) -> None:
        """Sessions sharing a project each receive and deduplicate their own stale context."""
        repo, idx_dir, proj = self._stale_repo(tmp_path)
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=False, marker=tmp_path / "unused")
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        for session in ("session-a", "session-b"):
            event = {"session_id": session}
            first = _run_inject_with_event(repo, idx_dir, plugin_root, tmpdir, event, runtime=runtime)
            assert first.returncode == 0, first.stderr
            assert "Prefer `codemap-py query`" in first.stdout
            flag = _stale_flag(tmpdir, proj, runtime, session)
            assert flag.is_file()
            assert _session_flag(tmpdir, proj, runtime, session).is_file()
            _write_stale_flag(flag, "1" * 40)
            reminder = _run_inject_with_event(repo, idx_dir, plugin_root, tmpdir, event, runtime=runtime)
            assert reminder.returncode == 0, reminder.stderr
            context = (
                json.loads(reminder.stdout)["hookSpecificOutput"]["additionalContext"]
                if runtime == "codex"
                else reminder.stdout.strip()
            )
            assert context == "[codemap] index stale - refresh pending"
            repeated = _run_inject_with_event(repo, idx_dir, plugin_root, tmpdir, event, runtime=runtime)
            assert repeated.returncode == 0, repeated.stderr
            assert repeated.stdout == ""

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param("timestamp-only", id="legacy-timestamp"),
            "",
            "not-json",
            '[true, "sha", "stale"]',
            '[1, 2, "stale"]',
            '[1, "sha"]',
        ],
    )
    def test_unusable_stale_record_emits_full_notice(self, tmp_path: Path, content: str) -> None:
        """Legacy or corrupt records fail open and are replaced with the emitted state."""
        repo, idx_dir, proj = self._stale_repo(tmp_path)
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=False, marker=tmp_path / "unused")
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        flag = _stale_flag(tmpdir, proj)
        flag.write_text(str(int(time.time() * 1000)) if content == "timestamp-only" else content)

        result = _run_inject(repo, idx_dir, plugin_root, tmpdir)
        assert result.returncode == 0, result.stderr
        assert "Prefer `codemap-py query`" in result.stdout
        assert "2 modules" in result.stdout
        assert json.loads(flag.read_text())[1:] == ["0" * 40, "stale"]


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
    _MODULES: ClassVar = ["mypackage.auth", "mypackage/auth"]

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

        assert claude.returncode == 0
        assert codex.returncode == 0
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

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({"tool_name": "Skill", "tool_input": {"skill": "foundry:audit"}}, id="non-codemap-skill"),
            pytest.param({"tool_name": "Bash", "tool_input": {"command": "ls"}}, id="non-skill-tool"),
        ],
    )
    def test_unrelated_call_ignored(self, tmp_path: Path, payload: dict) -> None:
        """A non-codemap skill or a non-Skill tool call is ignored — no record written."""
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()

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

    @pytest.mark.parametrize(
        ("hook", "payload", "layer"),
        [
            pytest.param(_TOOL_HOOK, {"tool_name": "Grep", "tool_input": {"pattern": "x"}}, "tools", id="tool-hook"),
            pytest.param(
                _SKILL,
                {"tool_name": "Skill", "tool_input": {"skill": "codemap-py:query-code"}},
                "skills",
                id="skill-hook",
            ),
        ],
    )
    def test_hook_from_subdir_joins_the_seeded_session(
        self, tmp_path: Path, hook: Path, payload: dict, layer: str
    ) -> None:
        """A tool or skill record written from a subdirectory carries the seeded session id."""
        tmpdir, repo, nested = self._repo_with_subdir(tmp_path)
        (tmpdir / f"codemap-{repo.name}-session").write_text("sid-sub")

        result = self._run(hook, payload, nested, tmpdir)

        assert result.returncode == 0, result.stderr
        # Shard is asserted at the REPOSITORY root, not at *nested*: the hook anchors its
        # log dir to the project root, so a subdirectory invocation joins the same shard
        # the cli layer writes. Asserting `nested/.cache/...` here pinned the split-log
        # defect itself — two halves of one session in two directories, neither an error.
        shard = repo / ".cache" / "codemap" / "logs" / "claude" / f"{layer}_sid-sub.jsonl"
        assert shard.exists(), f"subdirectory {layer} record did not join the seeded session shard"
        record = json.loads(shard.read_text().strip())
        assert record["session"] == "sid-sub"
        assert record["runtime"] == "claude"
        assert record["v"] not in ("", "?")

    @pytest.mark.parametrize("tool", ["Grep", "Skill"])
    def test_event_session_overrides_other_session_marker(self, tmp_path: Path, tool: str) -> None:
        """Concurrent Claude sessions cannot inherit another event's project marker."""
        tmpdir, repo, nested = self._repo_with_subdir(tmp_path)
        marker = tmpdir / f"codemap-{repo.name}-session"
        marker.write_text("other-session")
        payload = {
            "tool_name": tool,
            "session_id": "event-session",
            "tool_input": {"pattern": "x", "skill": "codemap-py:query-code"},
        }
        hook = _SKILL if tool == "Skill" else self._TOOL_HOOK
        result = self._run(hook, payload, nested, tmpdir)
        assert result.returncode == 0, result.stderr
        layer = "skills" if tool == "Skill" else "tools"
        shard = repo / ".cache/codemap/logs/claude" / f"{layer}_event-session.jsonl"
        assert shard.exists()
        assert json.loads(shard.read_text())["session"] == "event-session"
        assert marker.read_text() == "other-session"

    @pytest.mark.parametrize("tool", ["Grep", "Skill"])
    @pytest.mark.parametrize("variable", ["CLAUDE_CODE_SESSION_ID", "CSID"])
    def test_claude_environment_identity_precedes_marker(
        self, tmp_path: Path, monkeypatch, tool: str, variable: str
    ) -> None:
        """A payload without identity still joins the CLI's provided Claude session."""
        tmpdir, repo, nested = self._repo_with_subdir(tmp_path)
        (tmpdir / f"codemap-{repo.name}-session").write_text("stale-marker")
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CSID", raising=False)
        monkeypatch.setenv(variable, "environment-session")
        payload = {"tool_name": tool, "tool_input": {"pattern": "x", "skill": "codemap-py:query-code"}}
        hook = _SKILL if tool == "Skill" else self._TOOL_HOOK
        result = self._run(hook, payload, nested, tmpdir)
        assert result.returncode == 0, result.stderr
        layer = "skills" if tool == "Skill" else "tools"
        shard = repo / ".cache/codemap/logs/claude" / f"{layer}_environment-session.jsonl"
        assert shard.exists()
        record = json.loads(shard.read_text())
        assert record["session"] == "environment-session"
        assert record["project"] == repo.resolve().as_posix()

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
        _write_index(repo / ".cache" / "codemap", repo, git_sha=head)

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
        _write_index(repo / ".cache" / "codemap", repo, git_sha=head)

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

    @pytest.mark.parametrize("hook", ["log-tool-use.py", "log-skill-start.py"])
    def test_missing_identity_never_borrows_any_project_marker(self, tmp_path: Path, hook: str) -> None:
        """A current project marker cannot prove which concurrent thread invoked a hook."""
        repo = tmp_path / "project"
        _init_repo(repo)
        tmpdir = tmp_path / "tmp"
        tmpdir.mkdir()
        (tmpdir / f"codemap-{repo.name}-session").write_text("claude-session")
        cache = repo / ".cache" / "codemap"
        cache.mkdir(parents=True)
        (cache / "current-session-codex.json").write_text(json.dumps({"session_id": "another-codex-thread"}))
        env = {
            **os.environ,
            "TMPDIR": str(tmpdir),
            "TEMP": str(tmpdir),
            "TMP": str(tmpdir),
            "CODEMAP_RUNTIME": "codex",
            "CODEMAP_LOGGING": "true",
        }
        env.pop("CODEX_THREAD_ID", None)
        env.pop("CODEMAP_LOG_DIR", None)
        skill = hook == "log-skill-start.py"
        payload = {
            "tool_name": "Skill" if skill else "Read",
            "tool_input": {"skill": "codemap-py:query-code"} if skill else {"file_path": "pkg/mod.py"},
        }
        result = subprocess.run(
            [sys.executable, str(TestSessionKeyAgreement._TOOL_HOOK.with_name(hook))],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            cwd=repo,
            env=env,
        )
        assert result.returncode == 0, result.stderr
        shard = cache / "logs" / "codex" / ("skills.jsonl" if skill else "tools.jsonl")
        record = json.loads(shard.read_text())
        assert record["session"] == ""
        assert record["project"] == repo.resolve().as_posix()
        assert (tmpdir / f"codemap-{repo.name}-session").read_text() == "claude-session"

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
