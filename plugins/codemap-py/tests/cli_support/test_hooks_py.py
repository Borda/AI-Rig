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
real scan. The one-commit repository is built once per session and copied per test,
because the hook itself reads real git state and the six setup commands dominated runtime.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import NamedTuple
from unittest import mock

import pytest

_HOOKS = Path(__file__).parent.parent.parent / "hooks"
_INJECT = _HOOKS / "inject-preamble.py"
_GUARD = _HOOKS / "guard-redundant-scan.py"
_SEED = _HOOKS / "seed-session.py"
_SKILL = _HOOKS / "log-skill-start.py"
_TOOL_HOOK = _HOOKS / "log-tool-use.py"

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

_TASK = "def task(): ...\n"

#: Environment keys every hook run drops so the host session cannot leak into a test.
_HERMETIC_ENV = {"CODEX_THREAD_ID": None, "CODEMAP_LOG_DIR": None, "CLAUDE_CODE_SESSION_ID": None, "CSID": None}

# ── repository template ──────────────────────────────────────────────────────────

_REPO_TEMPLATE: dict[str, object] = {}


def _git(root: Path, *args: str) -> str:
    """Run a git command inside *root*, asserting success; return its stdout."""
    result = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _git_head(root: Path) -> str:
    """Return the HEAD sha of the repository at *root*."""
    return _git(root, "rev-parse", "HEAD").strip()


@pytest.fixture(autouse=True, scope="session", name="_repo_template")
def _build_repo_template(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Build the one-commit repository once per session; ``_init_repo`` copies it."""
    root = tmp_path_factory.mktemp("repo-template") / "template"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.t")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "core.autocrlf", "false")  # Windows write_text emits CRLF; blobs must hold the bytes written
    (root / "a.py").write_text("def f():\n    return 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    _REPO_TEMPLATE.update(root=root, head=_git_head(root))


def _init_repo(root: Path) -> str:
    """Create a git repo at *root* with one committed .py file; return the HEAD sha."""
    shutil.copytree(_REPO_TEMPLATE["root"], root, dirs_exist_ok=True)
    return str(_REPO_TEMPLATE["head"])


# ── helpers ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Change:
    """A working-tree mutation: files written, files deleted, then git commands run, in that order."""

    writes: dict[str, str] = field(default_factory=dict)
    deletes: tuple[str, ...] = ()
    git: tuple[tuple[str, ...], ...] = ()


def _apply(repo: Path, change: Change) -> None:
    """Apply *change* to the repository at *repo*."""
    for relative, text in change.writes.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    for relative in change.deletes:
        (repo / relative).unlink()
    for args in change.git:
        _git(repo, *args)


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


def _write_scan_stub(plugin_root: Path, *lines: str) -> None:
    """Replace the fake ``scan-index`` with a Python script made of *lines*."""
    (plugin_root / "bin" / "scan-index").write_text("#!/usr/bin/env python3\n" + "\n".join(lines) + "\n")


def _run_hook(hook: Path, stdin: str, cwd: Path | None, tmpdir: Path, **env: str | None) -> subprocess.CompletedProcess:
    """Run *hook* with *stdin* from *cwd*, TMPDIR pinned and the host session scrubbed.

    ``env`` entries add or override variables; a ``None`` value removes the variable from the child environment.
    """
    overrides = {**_HERMETIC_ENV, **env}
    child_env = {**os.environ, "TMPDIR": str(tmpdir), "TEMP": str(tmpdir), "TMP": str(tmpdir)}
    child_env.update({key: value for key, value in overrides.items() if value is not None})
    for key in [key for key, value in overrides.items() if value is None]:
        child_env.pop(key, None)
    return subprocess.run(
        [sys.executable, str(hook)], input=stdin, text=True, capture_output=True, cwd=cwd, env=child_env
    )


def _run_inject(
    repo: Path,
    idx_dir: Path,
    plugin_root: Path,
    tmpdir: Path,
    *,
    cwd: Path | None = None,
    prompt: str = "hello",
    runtime: str = "claude",
    event: dict | None = None,
) -> subprocess.CompletedProcess:
    """Drive inject-preamble.py with all three seams pinned; *event* replaces the default ``{"prompt": ...}`` stdin."""
    payload = {"prompt": prompt} if event is None else event
    return _run_hook(
        _INJECT,
        json.dumps(payload),
        cwd or repo,
        tmpdir,
        CODEMAP_INDEX_DIR=str(idx_dir),
        CLAUDE_PLUGIN_ROOT=str(plugin_root),
        PYTHON=sys.executable,
        CODEMAP_RUNTIME=runtime,
    )


def _run_inject_times(count: int, *args, **kwargs) -> list[subprocess.CompletedProcess]:
    """Run :func:`_run_inject` *count* times with the same arguments and return every result."""
    return [_run_inject(*args, **kwargs) for _ in range(count)]


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


def _await_marker(path: Path, needle: str = "", timeout_s: float = 8.0) -> bool:
    """Wait for non-empty content containing *needle*, requiring complete JSON for ``.json`` markers.

    The stale-index refresh is a detached, fire-and-forget spawn, so the stub's marker write races the assertion and
    must be awaited. The budget is generous because on Windows the stub runs through a fresh ``python`` interpreter
    whose cold start (a new, Defender-scanned ``CreateProcess``) can take several seconds under CI load — far longer
    than a warm Python process. Returns as soon as the condition holds, so a fast platform pays only the real spawn
    latency, not the ceiling. JSON decoding rejects partial writes as well as the empty file visible after truncation;
    plain-text publication records use *needle* as their completion signal. Return False on timeout.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            content = path.read_text(encoding="utf-8")
            if content and needle in content:
                try:
                    if path.suffix == ".json":
                        json.loads(content)
                except json.JSONDecodeError:
                    pass
                else:
                    return True
        time.sleep(0.02)
    return False


@pytest.mark.parametrize(
    ("name", "initial", "complete"),
    [
        pytest.param("child.json", "", '{"session": "ready"}', id="empty-json"),
        pytest.param("child.json", '{"session":', '{"session": "ready"}', id="partial-json"),
        pytest.param("spawned.marker", "", "spawned", id="empty-text"),
    ],
)
def test_await_marker_waits_for_complete_content(tmp_path: Path, name: str, initial: str, complete: str) -> None:
    """Wait for a child to finish writing before letting a caller consume its marker.

    The stub announces an empty or partial write, pauses, then finishes the marker. Reading immediately after the shared
    waiter returns must yield the complete payload rather than the intermediate bytes.
    """
    marker = tmp_path / name
    plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=True, marker=marker)
    _write_scan_stub(
        plugin_root,
        "import time",
        "from pathlib import Path",
        f"marker = Path({str(marker)!r})",
        f"marker.write_text({initial!r}, encoding='utf-8')",
        "print('writing', flush=True)",
        "time.sleep(0.3)",
        f"marker.write_text({complete!r}, encoding='utf-8')",
    )
    with subprocess.Popen(
        [sys.executable, str(plugin_root / "bin" / "scan-index")], stdout=subprocess.PIPE, text=True
    ) as child:
        assert child.stdout is not None
        assert child.stdout.readline() == "writing\n"
        assert _await_marker(marker)
        observed = marker.read_text(encoding="utf-8")

    assert child.returncode == 0
    assert observed == complete


def _expire_lock(tmpdir: Path, proj: str) -> str:
    """Age the refresh lock beyond its TTL and return the written text."""
    _lock_file(tmpdir, proj).write_text(str(int(time.time() * 1000) - _LOCK_TTL_MS - 60_000))
    return _lock_file(tmpdir, proj).read_text()


def _plain_context(result: subprocess.CompletedProcess) -> str:
    """Return Claude's prompt context: the plain stdout text."""
    return result.stdout.strip()


def _codex_context(result: subprocess.CompletedProcess) -> str:
    """Return Codex's prompt context from its ``hookSpecificOutput`` JSON envelope."""
    return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]


_by_runtime_output = pytest.mark.parametrize(
    ("runtime", "context_of"),
    [pytest.param("claude", _plain_context, id="claude"), pytest.param("codex", _codex_context, id="codex")],
)


class Project(NamedTuple):
    """A git repository plus the controlled index directory describing it."""

    repo: Path
    idx_dir: Path
    index_path: Path
    head: str

    @property
    def name(self) -> str:
        """Project key the hook derives from the git-root basename."""
        return self.repo.name


class DirtyProject(NamedTuple):
    """A repository whose index is on HEAD but whose tree carries an unstaged edit."""

    repo: Path
    idx_dir: Path
    index_path: Path
    plugin_root: Path
    tmpdir: Path


@pytest.fixture(name="hook_tmp")
def _hook_tmp(tmp_path: Path) -> Path:
    """Empty TMPDIR the hook writes its sentinels into."""
    path = tmp_path / "tmp"
    path.mkdir()
    return path


@pytest.fixture(name="spawn_marker")
def _spawn_marker(tmp_path: Path) -> Path:
    """Path the fake ``scan-index`` writes when a refresh is spawned."""
    return tmp_path / "spawned.marker"


@pytest.fixture(name="plugin_root")
def _plugin_root(tmp_path: Path, spawn_marker: Path) -> Path:
    """Fake plugin root whose ``scan-index`` stub writes ``spawn_marker``."""
    return _fake_plugin_root(tmp_path, with_scan_bin=True, marker=spawn_marker)


@pytest.fixture(name="bare_plugin_root")
def _bare_plugin_root(tmp_path: Path) -> Path:
    """Fake plugin root with no ``scan-index``, so a refresh cannot spawn."""
    return _fake_plugin_root(tmp_path, with_scan_bin=False, marker=tmp_path / "unused")


def _project(tmp_path: Path, *, index_sha: str | None) -> Project:
    """Build the repo plus an index recording *index_sha* (HEAD when ``None``)."""
    repo = tmp_path / "proj"
    head = _init_repo(repo)
    idx_dir = tmp_path / "idx"
    index_path = _write_index(idx_dir, repo, git_sha=index_sha or head)
    return Project(repo, idx_dir, index_path, head)


@pytest.fixture(name="current_proj")
def _current_proj(tmp_path: Path) -> Project:
    """Repository with an index recorded at HEAD (currency resolves to current)."""
    return _project(tmp_path, index_sha=None)


@pytest.fixture(name="stale_proj")
def _stale_proj(tmp_path: Path) -> Project:
    """Repository whose index carries a bogus sha, so currency resolves to stale."""
    return _project(tmp_path, index_sha="0" * 40)


@pytest.fixture(name="dirty_proj")
def _dirty_proj(tmp_path: Path) -> DirtyProject:
    """Index on HEAD with an unstaged edit; the stub ``scan-index`` publishes like the real scan.

    The stub does what the real scan does after a successful publish: it marks the record it was handed through
    ``CODEMAP_REFRESH_RECORD``/``CODEMAP_REFRESH_FINGERPRINT`` as ``published``.
    """
    repo = tmp_path / "proj"
    head = _init_repo(repo)
    idx_dir = tmp_path / "idx"
    index_path = _write_index(idx_dir, repo, git_sha=head)
    (repo / "a.py").write_text("def f():\n    return 2\n")
    plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=True, marker=tmp_path / "spawned.marker")
    _write_scan_stub(
        plugin_root,
        "import json, os, sys",
        "from pathlib import Path",
        f"sys.path.insert(0, {str(_HOOKS.parent / 'src')!r})",
        "from codemap_py import scanner",
        "from codemap_py.scanner.discovery import fingerprint_file_hashes",
        f"root = Path({str(repo)!r})",
        f"index_path = Path({str(index_path)!r})",
        "payload = json.loads(index_path.read_text())",
        "payload['source_fingerprint'] = fingerprint_file_hashes("
        "scanner.get_file_hashes(root, scanner._load_exclusions(root)))",
        "index_path.write_text(json.dumps(payload))",
        "Path(os.environ['CODEMAP_REFRESH_RECORD']).write_text("
        "os.environ['CODEMAP_REFRESH_FINGERPRINT'] + '\\n1\\npublished', encoding='utf-8')",
    )
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    return DirtyProject(repo, idx_dir, index_path, plugin_root, tmpdir)


def _complete_refresh(proj: DirtyProject) -> None:
    """Wait for publication, then expire the refresh lock and stale notice so refresh notes are observable."""
    (record,) = proj.tmpdir.glob(f"codemap-refresh-fp-{proj.repo.name}-*")
    assert _await_marker(record, "published")
    _expire_lock(proj.tmpdir, proj.repo.name)
    git_sha = json.loads(proj.index_path.read_text())["git_sha"]
    _write_stale_flag(_stale_flag(proj.tmpdir, proj.repo.name), git_sha, expired=True)


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
    def test_sentinels_use_safe_session_suffix(
        self, stale_proj: Project, bare_plugin_root: Path, hook_tmp: Path, runtime: str, identity: str, token: str
    ) -> None:
        """All prompt sentinels stay in the temp directory with the sanitized terminal suffix.

        A stale index then a missing index are driven for the same session identity; an empty or path-hostile identity
        must collapse to a filesystem-safe token, never escape the temp directory or produce a nested path.
        """
        repo = stale_proj.repo
        (repo / "pyproject.toml").write_text("")
        event = {"thread_id" if runtime == "codex" else "session_id": identity}

        stale = _run_inject(repo, stale_proj.idx_dir, bare_plugin_root, hook_tmp, event=event, runtime=runtime)
        assert stale.returncode == 0, stale.stderr
        assert "Prefer `codemap-py query`" in stale.stdout
        assert _stale_flag(hook_tmp, repo.name, runtime, token).is_file()
        assert _session_flag(hook_tmp, repo.name, runtime, token).is_file()
        assert json.loads(_session_marker(repo, runtime).read_text())["session_id"] == identity
        stale_proj.index_path.unlink()
        missing = _run_inject(repo, stale_proj.idx_dir, bare_plugin_root, hook_tmp, event=event, runtime=runtime)
        assert missing.returncode == 0, missing.stderr
        assert "No structural index" in missing.stdout
        assert (hook_tmp / f"codemap-noindex-{repo.name}-{runtime}-{token}").is_file()
        assert all(path.is_file() for path in hook_tmp.iterdir())

    @pytest.mark.integration
    @pytest.mark.parametrize("runtime", ["claude", "codex"])
    @pytest.mark.parametrize(
        ("session", "other"),
        [
            pytest.param("session-a", "session-b", id="a-after-b"),
            pytest.param("session-b", "session-a", id="b-after-a"),
        ],
    )
    def test_missing_index_emits_once_per_session_until_ttl(
        self, tmp_path: Path, bare_plugin_root: Path, hook_tmp: Path, runtime: str, session: str, other: str
    ) -> None:
        """Each session gets one bootstrap directive, re-emitted only after its existing TTL.

        With no index at all, a session's first prompt gets the bootstrap directive, the repeat is silent, and an
        expired flag re-emits it. Another session having already been told must not suppress this session's directive.
        """
        repo = tmp_path / "proj"
        _init_repo(repo)
        (repo / "pyproject.toml").write_text("")
        idx_dir = tmp_path / "idx"
        _run_inject(repo, idx_dir, bare_plugin_root, hook_tmp, event={"session_id": other}, runtime=runtime)
        event = {"session_id": session}

        first = _run_inject(repo, idx_dir, bare_plugin_root, hook_tmp, event=event, runtime=runtime)
        assert first.returncode == 0, first.stderr
        assert "ordinary query automatically prepares" in first.stdout
        assert "ask the user" not in first.stdout
        assert "AskUserQuestion" not in first.stdout
        flag = hook_tmp / f"codemap-noindex-{repo.name}-{runtime}-{session}"
        stamped = flag.read_text()
        repeated = _run_inject(repo, idx_dir, bare_plugin_root, hook_tmp, event=event, runtime=runtime)
        assert repeated.returncode == 0, repeated.stderr
        assert repeated.stdout == ""
        assert flag.read_text() == stamped
        flag.write_text(str(int(time.time() * 1000) - _INJECT_MODULE.NOINDEX_TTL_MS - 60_000))
        expired = _run_inject(repo, idx_dir, bare_plugin_root, hook_tmp, event=event, runtime=runtime)
        assert expired.returncode == 0, expired.stderr
        assert expired.stdout == first.stdout
        assert int(flag.read_text()) > int(stamped) - 1

    @pytest.mark.integration
    @pytest.mark.parametrize("runtime", ["claude", "codex"])
    @pytest.mark.parametrize(
        ("session", "other"),
        [
            pytest.param("session-a", "session-b", id="a-after-b"),
            pytest.param("session-b", "session-a", id="b-after-a"),
        ],
    )
    def test_current_preamble_reemits_after_ttl_per_session(
        self, current_proj: Project, bare_plugin_root: Path, hook_tmp: Path, runtime: str, session: str, other: str
    ) -> None:
        """Current indexes retain full preamble re-emission and silence within each session's TTL.

        A current index emits the full preamble once per session, stays silent inside the TTL and re-emits when the
        session flag ages out. A sibling session already holding a fresh flag must not silence this one.
        """
        repo, idx_dir = current_proj.repo, current_proj.idx_dir
        _run_inject(repo, idx_dir, bare_plugin_root, hook_tmp, event={"session_id": other}, runtime=runtime)
        event = {"session_id": session}

        first = _run_inject(repo, idx_dir, bare_plugin_root, hook_tmp, event=event, runtime=runtime)
        assert first.returncode == 0, first.stderr
        assert "Prefer `codemap-py query`" in first.stdout
        repeated = _run_inject(repo, idx_dir, bare_plugin_root, hook_tmp, event=event, runtime=runtime)
        assert repeated.returncode == 0, repeated.stderr
        assert repeated.stdout == ""
        _session_flag(hook_tmp, repo.name, runtime, session).write_text(
            str(int(time.time() * 1000) - _SESSION_TTL_MS - 60_000)
        )
        expired = _run_inject(repo, idx_dir, bare_plugin_root, hook_tmp, event=event, runtime=runtime)
        assert expired.returncode == 0, expired.stderr
        assert expired.stdout == first.stdout

    @pytest.mark.integration
    @pytest.mark.parametrize("runtime", ["claude", "codex"])
    @pytest.mark.parametrize(
        ("sources", "config", "ignore"),
        [
            pytest.param({}, "", "", id="tooling-only"),
            pytest.param({".venv/lib/pkg.py": _TASK}, "", "", id="virtualenv-only"),
            pytest.param({"node_modules/pkg.py": _TASK}, "", "", id="dependency-directory-only"),
            pytest.param({".hidden/pkg.py": _TASK}, "", "", id="hidden-only"),
            pytest.param({"vendor/pkg.py": _TASK}, "", "vendor", id="ignored-directory"),
            pytest.param({"generated/api.pyi": _TASK}, "", "generated/*.pyi", id="ignored-stub-glob"),
            pytest.param(
                {"vendor/pkg.py": _TASK}, '[tool.codemap]\nexclude = ["vendor"]', "", id="configured-exclusion"
            ),
        ],
    )
    def test_no_indexable_source_stays_silent(
        self,
        tmp_path: Path,
        plugin_root: Path,
        spawn_marker: Path,
        hook_tmp: Path,
        runtime: str,
        sources: dict[str, str],
        config: str,
        ignore: str,
    ) -> None:
        """Without an existing index, metadata and excluded sources never prompt or dispatch a refresh.

        The repository holds only tooling files or sources the scanner excludes (virtualenv, hidden, ignored,
        configured), so there is nothing to index and the hook must neither nag the user nor spawn a scan.
        """
        repo = tmp_path / "proj"
        _init_repo(repo)
        (repo / "a.py").unlink()
        (repo / "pyproject.toml").write_text(config)
        (repo / ".codemapignore").write_text(ignore)
        _apply(repo, Change(writes=sources))

        result = _run_inject(repo, tmp_path / "idx", plugin_root, hook_tmp, event={}, runtime=runtime)

        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
        # Git's macOS launcher may create xcrun_db; only hook-owned sentinels belong to this contract.
        assert list(hook_tmp.glob("codemap-*")) == []
        assert not spawn_marker.exists()
        assert _session_marker(repo, runtime).is_file()

    @pytest.mark.integration
    @pytest.mark.parametrize("runtime", ["claude", "codex"])
    @pytest.mark.parametrize("source", ["task.py", "src/namespace/api.py", "src/namespace/api.pyi"])
    def test_source_without_initializer_can_prompt(
        self, tmp_path: Path, bare_plugin_root: Path, hook_tmp: Path, runtime: str, source: str
    ) -> None:
        """Source-backed packaging markers remain eligible without package initializers.

        A project with ``pyproject.toml`` and a bare module or namespace package (no ``__init__.py``) is still a Python
        project, so the bootstrap notice must be shown.
        """
        repo = tmp_path / "proj"
        repo.mkdir()
        path = repo / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def task(): ...\n")
        (repo / "pyproject.toml").write_text("")

        result = _run_inject(repo, tmp_path / "idx", bare_plugin_root, hook_tmp, event={}, runtime=runtime)

        assert result.returncode == 0, result.stderr
        assert "No structural index" in result.stdout

    @pytest.mark.integration
    @pytest.mark.parametrize("runtime", ["claude", "codex"])
    def test_plain_script_does_not_prompt(
        self, tmp_path: Path, bare_plugin_root: Path, hook_tmp: Path, runtime: str
    ) -> None:
        """A marker-free standalone script emits no unsolicited bootstrap notice.

        A lone ``task.py`` with no packaging marker is a scratch script, not a project worth indexing.
        """
        repo = tmp_path / "proj"
        repo.mkdir()
        (repo / "task.py").write_text("def task(): ...\n")

        result = _run_inject(repo, tmp_path / "idx", bare_plugin_root, hook_tmp, event={}, runtime=runtime)

        assert result.returncode == 0, result.stderr
        assert result.stdout == ""

    def test_current_index_emits_preamble(
        self, current_proj: Project, plugin_root: Path, spawn_marker: Path, hook_tmp: Path
    ) -> None:
        """A current index (sha matches HEAD, clean tree) emits the preamble line and spawns no refresh.

        The preamble names the index state and module count; a current index must never launch a background scan.
        """
        result = _run_inject(current_proj.repo, current_proj.idx_dir, plugin_root, hook_tmp)

        assert result.returncode == 0, result.stderr
        assert "[codemap]" in result.stdout
        assert "current" in result.stdout
        assert "2 modules" in result.stdout
        assert not spawn_marker.exists()

    def test_fresh_session_flag_suppresses_second_emit(
        self, current_proj: Project, plugin_root: Path, hook_tmp: Path
    ) -> None:
        """A valid session flag younger than SESSION_TTL_MS makes a current index exit silent.

        The flag is seeded with the current time, standing in for a preamble already delivered this session.
        """
        _session_flag(hook_tmp, current_proj.name).write_text(str(int(time.time() * 1000)))

        result = _run_inject(current_proj.repo, current_proj.idx_dir, plugin_root, hook_tmp)

        assert result.returncode == 0, result.stderr
        assert result.stdout == ""

    def test_corrupted_session_flag_still_emits(self, current_proj: Project, plugin_root: Path, hook_tmp: Path) -> None:
        """A non-numeric session flag is treated as absent: the preamble is emitted and the flag rewritten.

        Pins the readTimestamp NaN guard: ``Date.now() - NaN`` is NaN, and the hook must treat that as "no valid flag"
        (emit) rather than silently exiting 0.
        """
        flag = _session_flag(hook_tmp, current_proj.name)
        flag.write_text("not-a-number")

        result = _run_inject(current_proj.repo, current_proj.idx_dir, plugin_root, hook_tmp)

        assert result.returncode == 0, result.stderr
        assert "[codemap]" in result.stdout
        assert int(flag.read_text().strip()) > 0

    def test_non_git_directory_without_index_fails_open(
        self, tmp_path: Path, plugin_root: Path, hook_tmp: Path
    ) -> None:
        """A non-git dir with no index and no Python markers exits 0, silent, without throwing.

        The hook runs on every prompt in any directory, so an unrelated folder must never produce output or errors.
        """
        plain = tmp_path / "plain"
        plain.mkdir()
        (plain / "notes.txt").write_text("hi")

        result = _run_inject(plain, tmp_path / "idx-empty", plugin_root, hook_tmp)

        assert result.returncode == 0, result.stderr
        assert result.stdout == ""

    @pytest.mark.parametrize(
        ("path", "subdir"),
        [
            pytest.param("api.pyi", "", id="stub"),
            pytest.param("guide.rst", "", id="restructured-text"),
            pytest.param("docs/topic/guide.md", "", id="markdown"),
            pytest.param("api.pyi", "pkg/feature", id="stub-from-nested-cwd"),
        ],
    )
    def test_indexed_file_change_marks_index_stale_and_refreshes(
        self, tmp_path: Path, plugin_root: Path, spawn_marker: Path, hook_tmp: Path, path: str, subdir: str
    ) -> None:
        """Every tracked indexed file kind makes an otherwise current index stale, from any working directory.

        An index recorded at HEAD becomes stale once a committed stub, reStructuredText or Markdown file is edited in
        the working tree; a prompt run from a nested directory must still find the root-level change.
        """
        repo = tmp_path / "proj"
        _init_repo(repo)
        changed = repo / path
        changed.parent.mkdir(parents=True, exist_ok=True)
        changed.write_text("indexed baseline\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "add indexed file")
        idx_dir = tmp_path / "idx"
        _write_index(idx_dir, repo, git_sha=_git_head(repo))
        changed.write_text("indexed change\n")
        cwd = repo / subdir
        cwd.mkdir(parents=True, exist_ok=True)

        result = _run_inject(repo, idx_dir, plugin_root, hook_tmp, cwd=cwd)

        assert result.returncode == 0, result.stderr
        assert "stale" in result.stdout
        assert _await_marker(spawn_marker)

    def test_unrelated_change_keeps_index_current_and_skips_refresh(
        self, current_proj: Project, plugin_root: Path, spawn_marker: Path, hook_tmp: Path
    ) -> None:
        """A non-indexed file must not make the refresh hook run.

        An edit to ``notes.txt`` is outside the scanner's input kinds, so the index stays current.
        """
        (current_proj.repo / "notes.txt").write_text("unrelated\n")

        result = _run_inject(current_proj.repo, current_proj.idx_dir, plugin_root, hook_tmp)

        assert result.returncode == 0, result.stderr
        assert "current" in result.stdout
        assert not spawn_marker.exists()

    def test_hook_pathspec_matches_scanner_contract(self) -> None:
        """Prompt freshness watches exactly the file kinds the scanner hashes.

        The hook, the scanner and the query layer each carry a pathspec; if they drift the hook declares an index
        current while the scanner would have indexed different files.
        """
        from codemap_py import query

        assert _INJECT_MODULE._INDEXED_PATHSPEC == _SCANNER_MODULE.INDEXED_PATHSPEC == query._INDEXED_PATHSPEC

    @pytest.mark.integration
    def test_foreign_root_cannot_appear_current(
        self, current_proj: Project, plugin_root: Path, spawn_marker: Path, hook_tmp: Path
    ) -> None:
        """Matching source bytes and commit never establish provenance for another checkout.

        The index claims a different ``scan_root`` while its commit and source bytes match this repository, so it was
        built for another checkout and must read as stale and refresh.
        """
        payload = json.loads(current_proj.index_path.read_text())
        payload["scan_root"] = str(current_proj.repo.parent / "foreign")
        current_proj.index_path.write_text(json.dumps(payload))

        result = _run_inject(current_proj.repo, current_proj.idx_dir, plugin_root, hook_tmp)

        assert result.returncode == 0, result.stderr
        assert "stale" in result.stdout
        assert _await_marker(spawn_marker)

    @pytest.mark.integration
    def test_frozen_environment_suppresses_prompt_refresh(
        self, current_proj: Project, plugin_root: Path, spawn_marker: Path, hook_tmp: Path, monkeypatch
    ) -> None:
        """The frozen-index opt-out also prevents prompt-triggered background writes.

        With ``SCAN_NO_AUTOBUILD=1`` a stale index is reported as such, but no lock is taken, nothing is spawned and the
        index file stays byte-identical.
        """
        before = current_proj.index_path.read_bytes()
        (current_proj.repo / "a.py").unlink()
        monkeypatch.setenv("SCAN_NO_AUTOBUILD", "1")

        result = _run_inject(current_proj.repo, current_proj.idx_dir, plugin_root, hook_tmp)

        assert result.returncode == 0, result.stderr
        assert "stale" in result.stdout
        assert "SCAN_NO_AUTOBUILD=1" in result.stdout
        assert not _lock_file(hook_tmp, current_proj.name).exists()
        assert not spawn_marker.exists()
        assert current_proj.index_path.read_bytes() == before

    def test_refresh_record_name_matches_scanner_contract(self) -> None:
        """The scan marks only records whose name carries the hook's prefix, so the two literals must agree.

        If they drift, the scan silently stops marking records published and every stale prompt refreshes again.
        """
        from codemap_py import graph

        name = _INJECT_MODULE.refresh_record_path("proj", PurePath("/x/proj.json")).name

        assert name.startswith(graph._REFRESH_RECORD_PREFIX)


# ── inject-preamble: bounded index-header read ────────────────────────────────────


def _header_file(tmp_path: Path, payload: dict) -> Path:
    """Write *payload* as the JSON index ``.cache/codemap/proj.json`` and return its path."""
    idx_dir = tmp_path / ".cache" / "codemap"
    idx_dir.mkdir(parents=True)
    idx_path = idx_dir / "proj.json"
    idx_path.write_text(json.dumps(payload), encoding="utf-8")
    return idx_path


class TestInjectPreambleHeaderFields:
    """Read identity out of a bounded index prefix, never the whole file.

    The bound is the point: this runs on every prompt, and real indexes reach hundreds of MB
    (well past ``MAX_PARSE_BYTES``), so a whole-file ``json.loads`` here would either stall the
    prompt path or report every large project as ``unknown`` currency and never refresh.
    """

    @pytest.mark.integration
    def test_module_count_uses_real_modules_not_freshness_inputs(
        self, tmp_path: Path, scan_index: Path, bare_plugin_root: Path, hook_tmp: Path
    ) -> None:
        """A Python module plus consumed config/docs and coverage metadata still displays one module.

        A real scan of a repo with a YAML target and an rst guide adds freshness inputs to ``file_shas`` but only one
        module; the displayed count must follow modules, and an index past the parse budget must read ``?``.
        """
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
        prompt = _run_inject(repo, index_path.parent, bare_plugin_root, hook_tmp)
        assert prompt.returncode == 0, prompt.stderr
        assert " - 1 modules - current" in prompt.stdout

    def test_header_never_reads_beyond_prefix_budget(self, tmp_path: Path) -> None:
        """Reading a large real index must keep every read bounded to the declared header budget.

        The file is twice the peek budget; a reader that asks for the whole file trips the assertion in the wrapped
        ``read``.
        """

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

    def test_returns_identity_fields(self, tmp_path: Path) -> None:
        """A small index yields git_sha, scanned_at, scan_root and fingerprint exactly as written.

        The header reader is the hook's only view of the index, so each identity field must round-trip unchanged.
        """
        idx_path = _write_index(tmp_path / ".cache" / "codemap", tmp_path, git_sha="a" * 40)

        fields = _INJECT_MODULE.header_fields(idx_path)

        assert fields == {
            "git_sha": "a" * 40,
            "scanned_at": "2026-06-20T00:00:00Z",
            "scan_root": str(tmp_path.resolve()),
            "source_fingerprint": _INJECT_MODULE.fingerprint_file_hashes({}),
        }

    def test_unescapes_backslashes(self, tmp_path: Path) -> None:
        """A Windows ``scan_root`` is stored JSON-escaped; the reader owes the caller the real path.

        Without unescaping, comparing the header against the live checkout path would never match on Windows.
        """
        scan_root = r"C:\Users\runneradmin\AppData\Local\Temp\proj"
        idx_path = _header_file(
            tmp_path, {"git_sha": "c" * 40, "scanned_at": "2026-06-20T00:00:00Z", "scan_root": scan_root}
        )

        fields = _INJECT_MODULE.header_fields(idx_path)

        assert fields["scan_root"] == scan_root
        assert r"\\" not in fields["scan_root"]

    def test_field_past_peek_window_reads_as_absent(self, tmp_path: Path) -> None:
        """Only the first HEADER_PEEK_BYTES are consulted — a later field comes back empty.

        ``scanned_at`` is placed after padding larger than the window, so only the earlier ``git_sha`` is visible.
        """
        padding = "x" * (_INJECT_MODULE.HEADER_PEEK_BYTES * 2)
        idx_path = _header_file(
            tmp_path, {"git_sha": "b" * 40, "scan_root": str(tmp_path), "pad": padding, "scanned_at": "2026-07-01"}
        )

        fields = _INJECT_MODULE.header_fields(idx_path)

        assert fields["git_sha"] == "b" * 40
        assert fields["scanned_at"] == ""

    def test_unreadable_index_maps_every_field_to_empty(self, tmp_path: Path) -> None:
        """An index path that cannot be read degrades to empty values, never an exception.

        The hook runs on every prompt and must fail open when the index vanishes or is unreadable.
        """
        fields = _INJECT_MODULE.header_fields(tmp_path / "absent" / "proj.json")

        assert fields == {"git_sha": "", "scanned_at": "", "scan_root": "", "source_fingerprint": ""}


# ── inject-preamble: stale-index background refresh + lock lifecycle ──────────────


class TestInjectPreambleRefreshLock:
    """The O_EXCL lock lifecycle around the stale-index background scan spawn."""

    def test_stale_index_spawns_refresh_and_takes_lock(
        self, stale_proj: Project, plugin_root: Path, spawn_marker: Path, hook_tmp: Path
    ) -> None:
        """A stale index spawns exactly one scan-index, writes the lockfile and says 'refresh started'.

        The bogus index sha makes currency stale; the fake plugin's stub scan marks the spawn.
        """
        result = _run_inject(stale_proj.repo, stale_proj.idx_dir, plugin_root, hook_tmp)

        assert result.returncode == 0, result.stderr
        assert "stale" in result.stdout
        assert "refresh started" in result.stdout
        assert _await_marker(spawn_marker), "scan-index was not spawned for a stale index"
        assert _lock_file(hook_tmp, stale_proj.name).exists(), "lock file not written on spawn"

    def test_fresh_lock_blocks_second_spawn(
        self, stale_proj: Project, plugin_root: Path, spawn_marker: Path, hook_tmp: Path
    ) -> None:
        """A fresh valid lock (age < TTL) suppresses the spawn: the note reads 'refresh in progress'.

        A held lock is pre-seeded so acquisition hits EEXIST on a fresh lock and does not take over. Every spawn path
        rewrites the lock, so its unchanged text is the deterministic proof that no second scan was launched.
        """
        lock = _lock_file(hook_tmp, stale_proj.name)
        lock.write_text(str(int(time.time() * 1000)))
        seeded = lock.read_text()

        result = _run_inject(stale_proj.repo, stale_proj.idx_dir, plugin_root, hook_tmp)

        assert result.returncode == 0, result.stderr
        assert "refresh in progress" in result.stdout
        assert lock.read_text() == seeded
        assert not spawn_marker.exists(), "a held fresh lock must suppress the spawn"

    @pytest.mark.parametrize(
        "lock_text",
        [
            pytest.param("1000", id="expired-timestamp"),
            pytest.param("", id="empty-corrupt"),
        ],
    )
    def test_expired_or_corrupt_lock_is_taken_over(
        self, stale_proj: Project, plugin_root: Path, spawn_marker: Path, hook_tmp: Path, lock_text: str
    ) -> None:
        """A lock older than LOCK_TTL_MS, or unreadable as a number, is replaced and one scan is spawned.

        ``1000`` is an epoch-era timestamp, older than any TTL. An empty lock parses as NaN age and must count as stale,
        not fresh, or the index would stay stale forever.
        """
        _lock_file(hook_tmp, stale_proj.name).write_text(lock_text)

        result = _run_inject(stale_proj.repo, stale_proj.idx_dir, plugin_root, hook_tmp)

        assert result.returncode == 0, result.stderr
        assert "refresh started" in result.stdout
        assert _await_marker(spawn_marker), "an expired or corrupt lock must be taken over and the scan spawned"

    def test_missing_scan_bin_releases_lock_without_spawn(
        self, stale_proj: Project, bare_plugin_root: Path, spawn_marker: Path, hook_tmp: Path
    ) -> None:
        """With no scan-index present the lock is released and nothing spawns, so a later prompt can retry.

        The hook briefly acquires the lock, finds no scan binary and must give the lock back.
        """
        result = _run_inject(stale_proj.repo, stale_proj.idx_dir, bare_plugin_root, hook_tmp)

        assert result.returncode == 0, result.stderr
        assert not spawn_marker.exists()
        assert not _lock_file(hook_tmp, stale_proj.name).exists(), "lock must be released when scan bin absent"

    @mock.patch("subprocess.Popen", autospec=True)
    def test_simulated_windows_refresh_uses_python_and_new_process_group(
        self, popen: mock.Mock, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Windows branch starts the script through Python in an isolated process group.

        ``os.name`` is forced to ``nt`` and Popen is mocked, so the Windows launch arguments are verified on any host
        without spawning a process.
        """
        scan_bin = tmp_path / "scan-index"
        scan_bin.write_text("print('scan')\n")
        monkeypatch.setattr(_INJECT_MODULE.os, "name", "nt")
        monkeypatch.setenv("SystemRoot", "C:\\Windows")

        assert _INJECT_MODULE.spawn_refresh(scan_bin, tmp_path, tmp_path)
        (command,), kwargs = popen.call_args
        child_env = {key.upper(): value for key, value in kwargs["env"].items()}
        assert command[:2] == [sys.executable, str(scan_bin)]
        assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        assert "start_new_session" not in kwargs
        assert kwargs["env"]["CODEMAP_RUNTIME"] == "claude"
        assert kwargs["env"]["CODEMAP_REFRESH_TRIGGER"] == "claude_prompt_background"
        assert kwargs["env"]["CODEMAP_REFRESH_STALE_BEFORE"] == "true"
        assert child_env["SYSTEMROOT"] == "C:\\Windows"

    @mock.patch("subprocess.Popen", autospec=True)
    def test_codex_refresh_records_codex_prompt_trigger(
        self, popen: mock.Mock, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A Codex prompt refresh must not be attributed to the Claude hook path.

        With ``CODEMAP_RUNTIME=codex`` the spawned scan's environment names the Codex runtime and trigger.
        """
        monkeypatch.setenv("CODEMAP_RUNTIME", "codex")

        assert _INJECT_MODULE.spawn_refresh(tmp_path / "scan-index", tmp_path, tmp_path)
        env = popen.call_args.kwargs["env"]
        assert env["CODEMAP_RUNTIME"] == "codex"
        assert env["CODEMAP_REFRESH_TRIGGER"] == "codex_prompt_background"

    def test_spawned_scan_is_kept_for_reaping_while_it_runs(self, tmp_path: Path) -> None:
        """The detached scan's process handle is kept for reaping, never dropped while the scan still runs.

        The spawn returns long before the scan exits. Dropping the ``Popen`` handle at that point makes
        ``Popen.__del__`` warn ``subprocess N is still running`` and leaves the finished child unreaped in a long-lived
        caller, so the spawn must hand the handle to a reaper instead.
        """
        scan_bin = tmp_path / "scan-index"
        scan_bin.write_text("")

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("ignore")
            warnings.filterwarnings("always", message=r"subprocess \d+ is still running", category=ResourceWarning)
            started = _INJECT_MODULE.spawn_refresh(scan_bin, tmp_path, tmp_path)

        assert started
        assert caught == []


_skip_launcher_rejects_interpreter = pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="the launcher accepts CPython >=3.11 only, so this interpreter cannot be the override",
)


class TestInjectPreambleRefreshFingerprint:
    """A stale index refreshes once per distinct scanner input, not once per prompt.

    The scanner fingerprints actual discovered source bytes. The stub publishes matching header evidence before
    acknowledging publication, preserving the same producer/consumer contract as the real scanner.
    """

    @pytest.mark.integration
    @pytest.mark.parametrize(
        "python_env",
        [
            pytest.param({}, id="unset"),
            pytest.param({"CODEMAP_PYTHON": ""}, id="empty"),
            pytest.param({"CODEMAP_PYTHON": sys.executable}, id="explicit", marks=_skip_launcher_rejects_interpreter),
        ],
    )
    def test_public_hook_refreshes_with_optional_python_override(
        self, tmp_path: Path, scan_index: Path, hook_tmp: Path, python_env: dict[str, str]
    ) -> None:
        """The public launcher and its background scan agree on empty, absent and explicit interpreter overrides.

        The real ``codemap-py --run-hook`` launcher is driven end to end in a path with spaces, then the detached real
        scan must publish a graph that reflects the edit.
        """
        repo = tmp_path / "project with spaces"
        _init_repo(repo)
        idx_dir = tmp_path / "idx"
        inherited = {k: v for k, v in os.environ.items() if k not in {"SCAN_NO_AUTOBUILD", "CODEMAP_PYTHON"}}
        env = {
            **inherited,
            "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
            "CODEMAP_INDEX_DIR": str(idx_dir),
            "PLUGIN_ROOT": str(scan_index.parent.parent),
            "CODEMAP_RUNTIME": "codex",
            "TMPDIR": str(hook_tmp),
            "TEMP": str(hook_tmp),
            "TMP": str(hook_tmp),
            **python_env,
        }
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
        (record,) = hook_tmp.glob(f"codemap-refresh-fp-{repo.name}-*")
        assert _await_marker(record, "published"), record.read_text()
        index = json.loads((idx_dir / f"{repo.name}.json").read_text())
        assert index["modules"][0]["direct_imports"] == ["pathlib"]

    @pytest.mark.integration
    @pytest.mark.parametrize(
        ("change", "expected_modules", "expected_imports"),
        [
            pytest.param(Change(writes={"a.py": "import pathlib\n"}), {"a"}, {"a": ["pathlib"]}, id="unstaged-edit"),
            pytest.param(Change(writes={"b.py": "import a\n"}), {"a", "b"}, {"b": ["a"]}, id="untracked-file"),
            pytest.param(Change(deletes=("a.py",)), set(), {}, id="delete-last-file"),
        ],
    )
    def test_real_refresh_publishes_working_tree_graph(
        self,
        tmp_path: Path,
        scan_index: Path,
        hook_tmp: Path,
        monkeypatch,
        change: Change,
        expected_modules: set[str],
        expected_imports: dict[str, list[str]],
    ) -> None:
        """A real detached scan publishes edited modules, additions and the final deletion without staging.

        A real initial scan is followed by one working-tree change and a prompt. The published graph must match the
        tree, git status must be untouched, an unchanged tree must stay silent, and restoring the older index bytes must
        trigger a fresh refresh, because a past publish cannot vouch for bytes restored to the same cache path.
        """
        repo = tmp_path / "proj"
        _init_repo(repo)
        (repo / ".gitignore").write_text(".cache/\n")
        idx_dir = tmp_path / "idx"
        plugin_root = scan_index.parent.parent
        monkeypatch.setenv("CODEMAP_INDEX_DIR", str(idx_dir))
        monkeypatch.setenv("PLUGIN_ROOT", str(plugin_root))
        monkeypatch.delenv("SCAN_NO_AUTOBUILD", raising=False)
        scanned = subprocess.run([sys.executable, str(scan_index), "--root", str(repo)], capture_output=True, text=True)
        assert scanned.returncode == 0, scanned.stderr
        index_path = idx_dir / f"{repo.name}.json"
        original_index = index_path.read_bytes()
        _apply(repo, change)
        before_status = _git(repo, "status", "--porcelain")

        result = _run_inject(repo, idx_dir, plugin_root, hook_tmp)

        assert result.returncode == 0, result.stderr
        assert "refresh started" in result.stdout
        (record,) = hook_tmp.glob(f"codemap-refresh-fp-{repo.name}-*")
        assert _await_marker(record, "published"), record.read_text()
        index = json.loads(index_path.read_text())
        assert {module["name"] for module in index["modules"]} == expected_modules
        published_imports = {module["name"]: module["direct_imports"] for module in index["modules"]}
        assert {name: imports for name, imports in published_imports.items() if name in expected_imports} == (
            expected_imports
        )
        assert _git(repo, "status", "--porcelain") == before_status
        aged_lock = _expire_lock(hook_tmp, repo.name)
        repeated = _run_inject(repo, idx_dir, plugin_root, hook_tmp)
        assert repeated.returncode == 0, repeated.stderr
        assert repeated.stdout == ""
        assert _lock_file(hook_tmp, repo.name).read_text() == aged_lock
        index_path.write_bytes(original_index)
        repaired = _run_inject(repo, idx_dir, plugin_root, hook_tmp, event={"session_id": "after-rollback"})
        assert repaired.returncode == 0, repaired.stderr
        assert "refresh started" in repaired.stdout
        assert _await_marker(record, "published"), record.read_text()
        repaired_index = json.loads(index_path.read_text())
        assert repaired_index["source_fingerprint"] == index["source_fingerprint"]
        assert repaired_index["modules"] == index["modules"]

    def test_unchanged_dirty_tree_spawns_one_refresh(self, dirty_proj: DirtyProject) -> None:
        """A second prompt on the same dirty tree stays silent and never takes the lock again.

        The lock keeps its aged timestamp, which proves no spawn was attempted: every spawn path rewrites it.
        """
        repo, idx_dir, _, plugin_root, tmpdir = dirty_proj
        first = _run_inject(repo, idx_dir, plugin_root, tmpdir)
        assert "refresh started" in first.stdout, first.stderr
        _complete_refresh(dirty_proj)
        aged_lock = _lock_file(tmpdir, repo.name).read_text()

        second = _run_inject(repo, idx_dir, plugin_root, tmpdir)

        assert second.returncode == 0, second.stderr
        assert second.stdout == ""
        assert _lock_file(tmpdir, repo.name).read_text() == aged_lock

    @pytest.mark.parametrize(
        "change",
        [
            pytest.param(Change(writes={"a.py": "def f():\n    return 3\n"}), id="unstaged-edit-refreshes"),
            pytest.param(Change(writes={"b.py": "def g():\n    return 4\n"}), id="untracked-file-refreshes"),
            pytest.param(
                Change(writes={".gitignore": "scratch.py\n", "scratch.py": "def scratch():\n    return 5\n"}),
                id="discovered-ignored-file-refreshes",
            ),
            pytest.param(Change(deletes=("a.py",)), id="last-source-deletion-refreshes"),
            pytest.param(Change(git=(("commit", "-q", "--allow-empty", "-m", "move head"),)), id="head-move-refreshes"),
        ],
    )
    def test_scanner_visible_changes_refresh_again(self, dirty_proj: DirtyProject, change: Change) -> None:
        """Refresh changed scanner inputs, including unstaged additions and the final source deletion.

        After one completed refresh, each further change to a scanner input (edit, addition, ignored-but-discovered
        file, deletion, HEAD move) must start another refresh.
        """
        repo, idx_dir, _, plugin_root, tmpdir = dirty_proj
        _run_inject(repo, idx_dir, plugin_root, tmpdir)
        _complete_refresh(dirty_proj)
        _apply(repo, change)

        result = _run_inject(repo, idx_dir, plugin_root, tmpdir)

        assert result.returncode == 0, result.stderr
        assert "refresh started" in result.stdout

    def test_staging_identical_source_stays_settled(self, dirty_proj: DirtyProject) -> None:
        """Staging a file whose bytes the scanner already saw does not trigger another refresh.

        The fingerprint hashes content, not index state, so ``git add`` of an already-published edit is silent.
        """
        repo, idx_dir, _, plugin_root, tmpdir = dirty_proj
        _run_inject(repo, idx_dir, plugin_root, tmpdir)
        _complete_refresh(dirty_proj)
        _git(repo, "add", "a.py")

        result = _run_inject(repo, idx_dir, plugin_root, tmpdir)

        assert result.returncode == 0, result.stderr
        assert result.stdout == ""

    def test_refresh_that_never_published_is_retried_even_after_another_publish(self, dirty_proj: DirtyProject) -> None:
        """A spawned refresh that never marked its record published is retried, whoever else wrote the index.

        Reproduces the lock-takeover race: refresh B fails ``index_busy`` behind an older, slower scan A, and A's
        publish lands after B's spawn. An index mtime newer than the spawn used to settle B's fingerprint with A's
        older content; only B's own ``published`` mark may.
        """
        repo, idx_dir, index_path, plugin_root, tmpdir = dirty_proj
        _write_scan_stub(plugin_root, "raise SystemExit(1)")
        _run_inject(repo, idx_dir, plugin_root, tmpdir)
        later_ns = time.time_ns() + 5_000_000_000
        os.utime(index_path, ns=(later_ns, later_ns))
        _expire_lock(tmpdir, repo.name)
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
        """Only this fingerprint's ``published`` line settles a refresh; anything else lets the next prompt retry.

        Each record shape (empty, garbled, spawned-only, legacy, other fingerprint) must read as not settled.
        """
        record = tmp_path / "record"
        record.write_text(content, encoding="utf-8")

        assert _INJECT_MODULE.refresh_settled(record, "abc") is False

    @mock.patch("subprocess.Popen", autospec=True)
    def test_spawn_hands_the_record_to_the_scan(
        self, popen: mock.Mock, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The record is written ``spawned`` before the child starts, and its path and fingerprint reach the child.

        Popen is mocked with a side effect that reads the record at spawn time, proving the write precedes the launch.
        """
        record = tmp_path / "codemap-refresh-fp-proj-0123456789ab"
        record_at_spawn: list[str] = []

        def _read_record_at_spawn(*args: object, **kwargs: object) -> object:
            """Capture the record as the child would see it, then return the Popen handle the spawn keeps."""
            record_at_spawn.append(record.read_text(encoding="utf-8"))
            return mock.DEFAULT

        popen.side_effect = _read_record_at_spawn
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("PLUGIN_ROOT", str(_fake_plugin_root(tmp_path, with_scan_bin=True, marker=tmp_path / "m")))

        note = _INJECT_MODULE.start_refresh("proj", tmp_path, tmp_path, "", None, record=(record, "abc"))

        assert note == _INJECT_MODULE.STARTED_NOTE
        assert record_at_spawn[0].split("\n")[::2] == ["abc", "spawned"]
        env = popen.call_args.kwargs["env"]
        assert env["CODEMAP_REFRESH_RECORD"] == str(record)
        assert env["CODEMAP_REFRESH_FINGERPRINT"] == "abc"

    @pytest.mark.parametrize(
        "index_path",
        [
            pytest.param(PurePosixPath("/home/me/proj/.cache/codemap/proj.json"), id="posix"),
            pytest.param(PureWindowsPath(r"C:\Users\me\proj\.cache\codemap\proj.json"), id="windows"),
        ],
    )
    def test_record_name_is_a_portable_filename(self, monkeypatch, tmp_path: Path, index_path: PurePath) -> None:
        """Any host's index path hashes to a plain ``codemap-refresh-fp-<project>-<hex>`` name inside the temp dir.

        POSIX and Windows spellings of the index path are both accepted on every host.
        """
        monkeypatch.setenv("TMPDIR", str(tmp_path))

        record = _INJECT_MODULE.refresh_record_path("proj", index_path)

        assert record.parent == tmp_path
        assert re.fullmatch(r"codemap-refresh-fp-proj-[0-9a-f]{12}", record.name)


class TestRefreshFingerprint:
    """The refresh fingerprint moves exactly when scanner inputs or HEAD move."""

    @pytest.mark.parametrize(
        ("change", "moves"),
        [
            pytest.param(Change(writes={"a.py": "def f():\n    return 2\n"}), True, id="unstaged-edit"),
            pytest.param(Change(writes={"b.py": "def g():\n    return 3\n"}), True, id="untracked-file"),
            pytest.param(
                Change(writes={"a.py": "def f():\n    return 2\n"}, git=(("add", "a.py"),)), True, id="staged-edit"
            ),
            pytest.param(Change(deletes=("a.py",)), True, id="delete-last-file"),
            pytest.param(Change(git=(("commit", "-q", "--allow-empty", "-m", "move head"),)), True, id="head-moved"),
            pytest.param(Change(writes={"notes.txt": "not indexed\n"}), False, id="unrelated-file"),
            pytest.param(Change(writes={".hidden/excluded.py": "x = 1\n"}), False, id="excluded-file"),
        ],
    )
    def test_moves_only_for_scanner_inputs(self, tmp_path: Path, change: Change, moves: bool) -> None:
        """Observe each filesystem change separately, preserving scanner exclusions.

        Edits, additions, deletions and HEAD moves change the fingerprint; unrelated or scanner-excluded files do not.
        """
        repo = tmp_path / "proj"
        head = _init_repo(repo)
        before = _INJECT_MODULE.refresh_fingerprint(repo, head)
        _apply(repo, change)

        after = _INJECT_MODULE.refresh_fingerprint(repo, _git_head(repo))

        assert before is not None
        assert after is not None
        assert (after != before) is moves

    def test_non_git_source_edit_changes_fingerprint(self, tmp_path: Path) -> None:
        """Non-Git projects retain repeat suppression while detecting actual source edits.

        Without git, the fingerprint hashes source bytes directly, so rewriting a file changes it.
        """
        source = tmp_path / "a.py"
        source.write_text("x = 1\n")
        before = _INJECT_MODULE.refresh_fingerprint(tmp_path, "")
        source.write_text("x = 2\n")

        after = _INJECT_MODULE.refresh_fingerprint(tmp_path, "")

        assert before is not None
        assert after is not None
        assert after != before


# ── inject-preamble: changed-file count handed to the refresh child ───────────────


def _no_edit(repo: Path, path: Path, content: str) -> None:
    """Leave the tree clean."""


def _edit_source(repo: Path, path: Path, content: str) -> None:
    """Rewrite the committed Python source."""
    (repo / "a.py").write_text("import pathlib\n")


def _edit_input(repo: Path, path: Path, content: str) -> None:
    """Rewrite the consumed input file."""
    path.write_text(content + "\n")


def _delete_input(repo: Path, path: Path, content: str) -> None:
    """Delete the consumed input file."""
    path.unlink()


class TestInjectPreambleChangedCount:
    """Refresh provenance counts eligible working-tree differences from the indexed commit, not reparse work.

    Invalid or unreachable commit identities leave the count unknown.
    """

    @pytest.mark.integration
    @pytest.mark.parametrize(
        ("edit", "count"),
        [
            pytest.param(_no_edit, 0, id="none"),
            pytest.param(_edit_source, 1, id="source-edit"),
            pytest.param(_edit_input, 1, id="input-edit"),
            pytest.param(_delete_input, 1, id="input-delete"),
        ],
    )
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
        self, tmp_path: Path, edit, count: int, relative: str, content: str
    ) -> None:
        """Clean consumed inputs count zero; source/config/doc edits and deletion each count one path.

        Config and docs the scanner consumes (even when excluded from source indexing) must count as changes in the same
        way, whether edited or deleted, and a clean committed tree counts zero.
        """
        repo = tmp_path / "proj"
        _init_repo(repo)
        (repo / ".codemapignore").write_text("vendor\ndocs/*.md\n.hidden\n")
        input_path = repo / relative
        input_path.parent.mkdir(parents=True, exist_ok=True)
        input_path.write_text(content)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "consumed inputs")
        head = _git_head(repo)
        edit(repo, input_path, content)

        assert _INJECT_MODULE.changed_file_count(repo, head) == count

    @pytest.mark.integration
    @pytest.mark.parametrize("relative", ["alias.py", "guide.rst", "docs/guide.md", "service.yaml"])
    def test_historical_symlink_inputs_do_not_invent_deleted_source(self, tmp_path: Path, relative: str) -> None:
        """Git symlink blobs are not source bytes; skipped sources count zero and consumed configs stay unknown.

        A symlink committed under a source or doc name must not be read as deleted source; a symlinked config the
        scanner would consume makes the count unknowable.
        """
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
        head = _git_head(repo)

        expected = None if relative == "service.yaml" else 0
        assert _INJECT_MODULE.changed_file_count(repo, head) == expected

    @pytest.mark.parametrize(
        "git_cmds",
        [
            pytest.param((), id="indexed-commit-is-head"),
            pytest.param((("commit", "-q", "-m", "changed"),), id="indexed-commit-behind-head"),
        ],
    )
    def test_count_filters_scanner_exclusions(self, tmp_path: Path, git_cmds: tuple) -> None:
        """Changed source, docs and ignore rules count; excluded Python paths contribute no changes.

        Edits under ``vendor``, ``generated``, a sandbox and ``__pycache__`` are excluded; ``a.py``, the ignore file and
        a docs page count, whether the changes are staged or already committed past the indexed commit.
        """
        repo = tmp_path / "proj"
        index_sha = _init_repo(repo)
        files = ("vendor/a.py", "generated/a.py", ".sandbox/a.py", "__pycache__/a.py", "docs/guide.rst")
        writes = {".codemapignore": "vendor\ngenerated/*.py\n", "a.py": "changed = True\n"}
        _apply(repo, Change(writes={**writes, **dict.fromkeys(files, "content\n")}, git=(("add", "-A"),)))
        _apply(repo, Change(git=git_cmds))

        assert _INJECT_MODULE.changed_file_count(repo, index_sha) == 3

    @mock.patch("subprocess.run", autospec=True)
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
    def test_non_hex_index_sha_is_unknown_without_git_call(self, run: mock.Mock, tmp_path: Path, git_sha: str) -> None:
        """An index-header sha that is not a hex object name never reaches git; the count is unknown.

        The header is unauthenticated on-disk input. An option-shaped value would otherwise be parsed by git as a flag
        (``--output`` writes a file at an attacker-chosen path), so the guard is the whole defense.
        """
        assert _INJECT_MODULE.changed_file_count(tmp_path, git_sha) is None
        run.assert_not_called()

    @pytest.mark.parametrize(
        "git_cmds",
        [
            pytest.param((), id="unstaged-edit-counted"),
            pytest.param((("add", "a.py"),), id="staged-edit-reparsed"),
        ],
    )
    def test_same_commit_counts_working_tree_edits(self, tmp_path: Path, git_cmds: tuple) -> None:
        """An eligible source edit counts once regardless of staging.

        Whether the edit is only in the working tree or also staged, one source file changed.
        """
        repo = tmp_path / "proj"
        head = _init_repo(repo)
        _apply(repo, Change(writes={"a.py": "def f():\n    return 2\n"}, git=git_cmds))

        assert _INJECT_MODULE.changed_file_count(repo, head) == 1

    def test_untracked_indexed_file_counts(self, tmp_path: Path) -> None:
        """An untracked stub is a discovered scanner input and counts as an addition.

        A new ``.pyi`` that git has never seen is still something the next scan will index.
        """
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
        (tmp_path / "codemap-refresh-proj").write_text(str(int(time.time() * 1000)))
        count = mock.Mock(return_value=1)

        note = _INJECT_MODULE.start_refresh("proj", tmp_path, tmp_path, "", count)

        assert note == " - refresh in progress"
        count.assert_not_called()

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
        _write_child_count_stub(plugin_root, marker)
        monkeypatch.setenv("PLUGIN_ROOT", str(plugin_root))
        count = mock.Mock(side_effect=UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "invalid start byte"))

        note = _INJECT_MODULE.start_refresh("proj", tmp_path, tmp_path, "", count)

        assert note == " - refresh started"
        assert _await_marker(marker)
        assert json.loads(marker.read_text()) == {"changed": None}

    def test_unreachable_index_commit_is_unknown(self, tmp_path: Path) -> None:
        """An index built from a commit git cannot resolve reports no count rather than a partial one.

        A well-formed but non-existent sha means the history is gone, so the honest answer is unknown.
        """
        repo = tmp_path / "proj"
        _init_repo(repo)

        assert _INJECT_MODULE.changed_file_count(repo, "0" * 40) is None

    def test_index_behind_head_counts_all_changed_source_paths_once(self, tmp_path: Path) -> None:
        """Committed, staged, unstaged and untracked source paths count once; unrelated files never count.

        ``b.py`` is committed then restaged (one path), ``with space.py`` is staged (quoted by porcelain-style output),
        ``a.py`` is an unstaged edit and ``c.pyi`` is untracked; ``notes.txt`` and ``README`` are not indexed.
        """
        repo = tmp_path / "proj"
        old = _init_repo(repo)
        (repo / "b.py").write_text("x = 1\n")
        (repo / "notes.txt").write_text("not indexed\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "second")
        (repo / "b.py").write_text("x = 2\n")
        (repo / "with space.py").write_text("y = 1\n")
        _git(repo, "add", "b.py", "with space.py")
        (repo / "a.py").write_text("def f():\n    return 3\n")
        (repo / "c.pyi").write_text("def c() -> int: ...\n")
        (repo / "README").write_text("ignored\n")

        assert _INJECT_MODULE.changed_file_count(repo, old) == 4

    @pytest.mark.integration
    def test_staged_edit_same_commit_child_receives_count(self, tmp_path: Path, hook_tmp: Path) -> None:
        """The spawned scan receives one source change without counting unchanged committed configuration.

        The index is at HEAD, a committed ``service.yaml`` is unchanged and ``a.py`` is staged with an edit, so the
        child's ``CODEMAP_REFRESH_CHANGED_COUNT`` must be exactly 1.
        """
        repo = tmp_path / "proj"
        _init_repo(repo)
        (repo / "service.yaml").write_text("target: package.worker\n")
        _git(repo, "add", "service.yaml")
        _git(repo, "commit", "-q", "-m", "configuration")
        idx_dir = tmp_path / "idx"
        _write_index(idx_dir, repo, git_sha=_git_head(repo))
        _apply(repo, Change(writes={"a.py": "def f():\n    return 2\n"}, git=(("add", "a.py"),)))
        marker = tmp_path / "child.json"
        plugin_root = _fake_plugin_root(tmp_path, with_scan_bin=True, marker=marker)
        _write_child_count_stub(plugin_root, marker)

        result = _run_inject(repo, idx_dir, plugin_root, hook_tmp)

        assert result.returncode == 0, result.stderr
        assert _await_marker(marker), result.stdout
        assert json.loads(marker.read_text()) == {"changed": "1"}

    @pytest.mark.integration
    def test_unreachable_index_commit_child_receives_no_count(self, stale_proj: Project, hook_tmp: Path) -> None:
        """An unresolvable index commit leaves the env key unset so the record says unknown.

        The index names a sha git cannot resolve, so the spawned scan must see no changed-count variable at all.
        """
        marker = stale_proj.repo.parent / "child.json"
        plugin_root = _fake_plugin_root(stale_proj.repo.parent, with_scan_bin=True, marker=marker)
        _write_child_count_stub(plugin_root, marker)

        result = _run_inject(stale_proj.repo, stale_proj.idx_dir, plugin_root, hook_tmp)

        assert result.returncode == 0, result.stderr
        assert _await_marker(marker), result.stdout
        assert json.loads(marker.read_text()) == {"changed": None}


def _write_child_count_stub(plugin_root: Path, marker: Path) -> None:
    """Make the fake ``scan-index`` record the changed-file count it received into *marker*."""
    _write_scan_stub(
        plugin_root,
        "import json, os",
        "from pathlib import Path",
        f"Path({str(marker)!r}).write_text(json.dumps({{'changed': os.environ.get('CODEMAP_REFRESH_CHANGED_COUNT')}}))",
    )


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
    def test_payload_session_reaches_refresh_child(self, stale_proj: Project, hook_tmp: Path, runtime: str) -> None:
        """The detached process receives the event identity, not a per-process fallback.

        The stub scan records the runtime session variable, runtime and trigger it was launched with; they must carry
        the session id from the prompt payload.
        """
        marker = stale_proj.repo.parent / "child.json"
        plugin_root = _fake_plugin_root(stale_proj.repo.parent, with_scan_bin=True, marker=marker)
        key = "CODEX_THREAD_ID" if runtime == "codex" else "CLAUDE_CODE_SESSION_ID"
        _write_scan_stub(
            plugin_root,
            "import json, os",
            "from pathlib import Path",
            f"Path({str(marker)!r}).write_text(json.dumps({{'session': os.environ.get({key!r}), "
            "'runtime': os.environ.get('CODEMAP_RUNTIME'), 'trigger': os.environ.get('CODEMAP_REFRESH_TRIGGER')}))",
        )
        event = {"prompt": "inspect", "session_id": "payload-thread"}

        result = _run_inject(stale_proj.repo, stale_proj.idx_dir, plugin_root, hook_tmp, event=event, runtime=runtime)

        assert result.returncode == 0, result.stderr
        assert _await_marker(marker), result.stdout
        assert json.loads(marker.read_text()) == {
            "session": "payload-thread",
            "runtime": runtime,
            "trigger": f"{runtime}_prompt_background",
        }

    def test_marker_written_with_session_id_and_ts(
        self, current_proj: Project, plugin_root: Path, hook_tmp: Path
    ) -> None:
        """A current-index turn writes the marker with the stdin session_id and an ms ts.

        The marker is one JSON line with a trailing newline, carrying the event's session id and a millisecond clock
        reading taken no earlier than the start of the prompt.
        """
        before_ms = int(time.time() * 1000)

        result = _run_inject(
            current_proj.repo,
            current_proj.idx_dir,
            plugin_root,
            hook_tmp,
            event={"prompt": "hi", "session_id": "sid-marker"},
        )

        assert result.returncode == 0, result.stderr
        raw = _session_marker(current_proj.repo).read_text()
        assert raw.endswith("\n"), "marker must end with a trailing newline"
        payload = json.loads(raw)
        assert payload["session_id"] == "sid-marker"
        assert isinstance(payload["ts"], int)
        assert payload["ts"] >= before_ms

    def test_marker_written_before_no_index_exit(self, tmp_path: Path, plugin_root: Path, hook_tmp: Path) -> None:
        """A non-Python, no-index dir stays silent yet still writes the session marker.

        The marker precedes the early exit so scan-query can still correlate sessions in directories the hook ignores.
        """
        plain = tmp_path / "plain"
        plain.mkdir()
        (plain / "notes.txt").write_text("hi")

        result = _run_inject(
            plain, tmp_path / "idx-empty", plugin_root, hook_tmp, event={"prompt": "hi", "session_id": "sid-silent"}
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
        assert json.loads(_session_marker(plain).read_text())["session_id"] == "sid-silent"

    def test_marker_ts_advances_on_deduped_second_turn(
        self, current_proj: Project, plugin_root: Path, hook_tmp: Path
    ) -> None:
        """A session-flag-suppressed second turn still advances the marker ts (written pre-dedup).

        A fresh session flag makes both turns dedup to silence; the marker written before the dedup return must not move
        backwards.
        """
        repo = current_proj.repo
        _session_flag(hook_tmp, current_proj.name, session="sid-dedup").write_text(str(int(time.time() * 1000)))
        event = {"prompt": "hi", "session_id": "sid-dedup"}

        first = _run_inject(repo, current_proj.idx_dir, plugin_root, hook_tmp, event=event)
        first_ts = json.loads(_session_marker(repo).read_text())["ts"]
        second = _run_inject(repo, current_proj.idx_dir, plugin_root, hook_tmp, event=event)

        assert first.returncode == 0
        assert second.returncode == 0
        assert second.stdout == "", "second current-index turn must dedup to a silent exit"
        assert json.loads(_session_marker(repo).read_text())["ts"] >= first_ts, "marker ts must advance"

    def test_marker_session_id_empty_on_unparsable_stdin(
        self, current_proj: Project, plugin_root: Path, hook_tmp: Path
    ) -> None:
        """Unparsable stdin fails open: marker written with session_id '' (no throw, exit 0).

        A host that pipes malformed JSON must not break the prompt or lose the marker.
        """
        result = _run_hook(
            _INJECT,
            "}{ not json",
            current_proj.repo,
            hook_tmp,
            CODEMAP_INDEX_DIR=str(current_proj.idx_dir),
            CLAUDE_PLUGIN_ROOT=str(plugin_root),
            CODEMAP_RUNTIME="claude",
        )

        assert result.returncode == 0, result.stderr
        assert json.loads(_session_marker(current_proj.repo).read_text())["session_id"] == ""

    def test_markers_are_isolated_across_claude_and_codex_turns(
        self, current_proj: Project, bare_plugin_root: Path, hook_tmp: Path
    ) -> None:
        """A Codex prompt must not replace the current Claude coverage session.

        Each runtime writes its own marker file, so interleaved turns keep their own session ids.
        """
        repo, idx_dir = current_proj.repo, current_proj.idx_dir

        claude = _run_inject(
            repo,
            idx_dir,
            bare_plugin_root,
            hook_tmp,
            event={"prompt": "claude", "session_id": "claude-session"},
            runtime="claude",
        )
        codex = _run_inject(
            repo,
            idx_dir,
            bare_plugin_root,
            hook_tmp,
            event={"prompt": "codex", "thread_id": "codex-thread"},
            runtime="codex",
        )

        assert claude.returncode == 0
        assert codex.returncode == 0
        assert json.loads(_session_marker(repo, "claude").read_text())["session_id"] == "claude-session"
        assert json.loads(_session_marker(repo, "codex").read_text())["session_id"] == "codex-thread"


# ── inject-preamble: stale-reminder collapse ──────────────────────────────────────


class TestInjectPreambleStaleCollapse:
    """Suppress repeated stale notices until their index state changes or their TTL expires."""

    def test_first_stale_emits_full_notice_and_writes_sentinel(
        self, stale_proj: Project, plugin_root: Path, hook_tmp: Path
    ) -> None:
        """The first stale prompt emits the full notice and drops the stale sentinel.

        The full form carries the canonical query hint and a module count.
        """
        result = _run_inject(stale_proj.repo, stale_proj.idx_dir, plugin_root, hook_tmp)

        assert result.returncode == 0, result.stderr
        assert "Prefer `codemap-py query`" in result.stdout
        assert "modules" in result.stdout
        assert _stale_flag(hook_tmp, stale_proj.name).exists(), "the stale sentinel must be written on the full notice"

    @pytest.mark.integration
    @pytest.mark.parametrize("runtime", ["claude", "codex"])
    def test_expired_unchanged_stale_reemits_full_preamble(
        self, stale_proj: Project, bare_plugin_root: Path, hook_tmp: Path, runtime: str
    ) -> None:
        """An unchanged stale state re-delivers query context after the reminder TTL.

        Once the stale record ages past the TTL the full preamble is shown again and the record is restamped, after
        which prompts are silent again.
        """
        repo, idx_dir = stale_proj.repo, stale_proj.idx_dir
        first = _run_inject(repo, idx_dir, bare_plugin_root, hook_tmp, runtime=runtime)
        assert first.returncode == 0, first.stderr
        assert "Prefer `codemap-py query`" in first.stdout
        flag = _stale_flag(hook_tmp, stale_proj.name, runtime)
        _write_stale_flag(flag, "0" * 40, expired=True)

        repeated = _run_inject(repo, idx_dir, bare_plugin_root, hook_tmp, runtime=runtime)

        assert repeated.returncode == 0, repeated.stderr
        assert "Prefer `codemap-py query`" in repeated.stdout
        assert "modules" in repeated.stdout
        assert json.loads(flag.read_text())[0] > int(time.time() * 1000) - _SESSION_TTL_MS
        quiet = _run_inject(repo, idx_dir, bare_plugin_root, hook_tmp, runtime=runtime)
        assert quiet.returncode == 0, quiet.stderr
        assert quiet.stdout == ""

    @pytest.mark.integration
    @pytest.mark.parametrize("runtime", ["claude", "codex"])
    def test_current_clears_stale_state_before_suppressed_preamble(
        self, current_proj: Project, bare_plugin_root: Path, hook_tmp: Path, runtime: str
    ) -> None:
        """A current index resets stale dedup even when its current preamble is suppressed.

        Edit to stale (notice recorded), revert to current (record cleared, silent), edit again: the stale notice must
        reappear in full instead of being deduplicated against the earlier record.
        """
        repo, idx_dir, head = current_proj.repo, current_proj.idx_dir, current_proj.head
        source = repo / "a.py"
        source.write_text("def f():\n    return 2\n")
        first = _run_inject(repo, idx_dir, bare_plugin_root, hook_tmp, runtime=runtime)
        assert first.returncode == 0, first.stderr
        assert "Prefer `codemap-py query`" in first.stdout
        flag = _stale_flag(hook_tmp, repo.name, runtime)
        assert json.loads(flag.read_text())[1:] == [head, "stale"]

        source.write_text("def f():\n    return 1\n")
        current = _run_inject(repo, idx_dir, bare_plugin_root, hook_tmp, runtime=runtime)
        assert current.returncode == 0, current.stderr
        assert current.stdout == ""
        assert not flag.exists()

        source.write_text("def f():\n    return 2\n")
        stale = _run_inject(repo, idx_dir, bare_plugin_root, hook_tmp, runtime=runtime)
        assert stale.returncode == 0, stale.stderr
        assert "Prefer `codemap-py query`" in stale.stdout
        assert json.loads(flag.read_text())[1:] == [head, "stale"]

    def test_changed_stale_collapses_to_single_line(
        self, stale_proj: Project, plugin_root: Path, hook_tmp: Path
    ) -> None:
        """A changed stale record emits one line within TTL and suppresses subsequent unchanged prompts.

        A held refresh lock makes the note deterministic ("refresh in progress"). The collapsed form leaves out the
        query hint, and three further unchanged prompts are silent without touching the record.
        """
        proj = stale_proj.name
        _write_stale_flag(_stale_flag(hook_tmp, proj), "1" * 40)
        _lock_file(hook_tmp, proj).write_text(str(int(time.time() * 1000)))

        result = _run_inject(stale_proj.repo, stale_proj.idx_dir, plugin_root, hook_tmp)

        assert result.returncode == 0, result.stderr
        assert [line for line in result.stdout.splitlines() if line] == ["[codemap] index stale - refresh in progress"]
        stamped = _stale_flag(hook_tmp, proj).read_text()
        assert json.loads(stamped)[0] > int(time.time() * 1000) - _SESSION_TTL_MS
        repeats = _run_inject_times(3, stale_proj.repo, stale_proj.idx_dir, plugin_root, hook_tmp)
        assert all(repeated.returncode == 0 for repeated in repeats)
        assert {repeated.stdout for repeated in repeats} == {""}
        assert _stale_flag(hook_tmp, proj).read_text() == stamped

    def test_collapsed_line_reads_refresh_pending_when_no_note(
        self, stale_proj: Project, bare_plugin_root: Path, hook_tmp: Path
    ) -> None:
        """When neither spawn nor lock produced a note, the collapsed line reads 'refresh pending'.

        With no scan binary the hook takes the lock, finds nothing to run, releases it and emits no refresh note; the
        collapsed reminder then supplies its own ' - refresh pending' default.
        """
        _write_stale_flag(_stale_flag(hook_tmp, stale_proj.name), "1" * 40)

        result = _run_inject(stale_proj.repo, stale_proj.idx_dir, bare_plugin_root, hook_tmp)

        assert result.returncode == 0, result.stderr
        assert [line for line in result.stdout.splitlines() if line] == ["[codemap] index stale - refresh pending"]

    def test_stale_collapse_isolated_from_preamble_flag(
        self, stale_proj: Project, plugin_root: Path, hook_tmp: Path
    ) -> None:
        """A fresh preamble flag (current-path dedup) does NOT collapse the stale reminder.

        The two sentinels track independent conditions: seeding only the preamble flag must still yield the full stale
        notice, proving the stale path keys on its own session-scoped stale sentinel.
        """
        _session_flag(hook_tmp, stale_proj.name).write_text(str(int(time.time() * 1000)))

        result = _run_inject(stale_proj.repo, stale_proj.idx_dir, plugin_root, hook_tmp)

        assert result.returncode == 0, result.stderr
        assert "Prefer `codemap-py query`" in result.stdout, "stale full notice must not be suppressed by preamble flag"
        assert _stale_flag(hook_tmp, stale_proj.name).exists()

    @pytest.mark.integration
    @pytest.mark.parametrize("runtime", ["claude", "codex"])
    def test_repeated_stale_prompts_are_silent(
        self, stale_proj: Project, bare_plugin_root: Path, hook_tmp: Path, runtime: str
    ) -> None:
        """The first stale preamble stamps its state and all unchanged prompts stay silent.

        The record holds the index sha and 'stale'; three more identical prompts add no output and leave it as is.
        """
        repo, idx_dir = stale_proj.repo, stale_proj.idx_dir

        first = _run_inject(repo, idx_dir, bare_plugin_root, hook_tmp, runtime=runtime)

        assert first.returncode == 0, first.stderr
        assert "Prefer `codemap-py query`" in first.stdout
        stamped = _stale_flag(hook_tmp, stale_proj.name, runtime).read_text()
        assert json.loads(stamped)[1:] == ["0" * 40, "stale"]
        repeats = _run_inject_times(3, repo, idx_dir, bare_plugin_root, hook_tmp, runtime=runtime)
        assert all(repeated.returncode == 0 for repeated in repeats)
        assert {repeated.stdout for repeated in repeats} == {""}
        assert _stale_flag(hook_tmp, stale_proj.name, runtime).read_text() == stamped

    @pytest.mark.integration
    @_by_runtime_output
    @pytest.mark.parametrize(
        ("flag_sha", "flag_currency", "index_sha"),
        [
            pytest.param("0" * 40, "stale", "1" * 40, id="git-sha-changed"),
            pytest.param("0" * 40, "current", "0" * 40, id="currency-changed"),
        ],
    )
    def test_changed_state_emits_immediately(
        self,
        stale_proj: Project,
        bare_plugin_root: Path,
        hook_tmp: Path,
        runtime: str,
        context_of,
        flag_sha: str,
        flag_currency: str,
        index_sha: str,
    ) -> None:
        """Either part of the recorded state key changing emits once inside the TTL.

        A fresh stale record differing from the live index in its sha or its currency yields the one-line collapsed
        reminder immediately; the record is updated and the next identical prompt is silent.
        """
        repo, idx_dir = stale_proj.repo, stale_proj.idx_dir
        flag = _stale_flag(hook_tmp, stale_proj.name, runtime)
        _write_stale_flag(flag, flag_sha, currency=flag_currency)
        _write_index(idx_dir, repo, git_sha=index_sha)

        result = _run_inject(repo, idx_dir, bare_plugin_root, hook_tmp, runtime=runtime)

        assert result.returncode == 0, result.stderr
        assert context_of(result) == "[codemap] index stale - refresh pending"
        assert json.loads(flag.read_text())[1:] == [index_sha, "stale"]
        repeated = _run_inject(repo, idx_dir, bare_plugin_root, hook_tmp, runtime=runtime)
        assert repeated.returncode == 0, repeated.stderr
        assert repeated.stdout == ""

    @pytest.mark.integration
    @_by_runtime_output
    @pytest.mark.parametrize(
        ("session", "other"),
        [
            pytest.param("session-a", "session-b", id="a-after-b"),
            pytest.param("session-b", "session-a", id="b-after-a"),
        ],
    )
    def test_sessions_have_independent_stale_notices(
        self,
        stale_proj: Project,
        bare_plugin_root: Path,
        hook_tmp: Path,
        runtime: str,
        context_of,
        session: str,
        other: str,
    ) -> None:
        """Sessions sharing a project each receive and deduplicate their own stale context.

        One session has already been told the index is stale. The other still gets the full notice, then its own
        collapsed reminder when its state changes and silence after; the first session's record is left untouched.
        """
        repo, idx_dir, proj = stale_proj.repo, stale_proj.idx_dir, stale_proj.name
        _run_inject(repo, idx_dir, bare_plugin_root, hook_tmp, event={"session_id": other}, runtime=runtime)
        other_record = _stale_flag(hook_tmp, proj, runtime, other).read_text()
        event = {"session_id": session}

        first = _run_inject(repo, idx_dir, bare_plugin_root, hook_tmp, event=event, runtime=runtime)
        assert first.returncode == 0, first.stderr
        assert "Prefer `codemap-py query`" in first.stdout
        flag = _stale_flag(hook_tmp, proj, runtime, session)
        assert flag.is_file()
        assert _session_flag(hook_tmp, proj, runtime, session).is_file()
        _write_stale_flag(flag, "1" * 40)
        reminder = _run_inject(repo, idx_dir, bare_plugin_root, hook_tmp, event=event, runtime=runtime)
        assert reminder.returncode == 0, reminder.stderr
        assert context_of(reminder) == "[codemap] index stale - refresh pending"
        repeated = _run_inject(repo, idx_dir, bare_plugin_root, hook_tmp, event=event, runtime=runtime)
        assert repeated.returncode == 0, repeated.stderr
        assert repeated.stdout == ""
        assert _stale_flag(hook_tmp, proj, runtime, other).read_text() == other_record

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param("1700000000000", id="legacy-timestamp"),
            pytest.param("", id="empty"),
            pytest.param("not-json", id="not-json"),
            pytest.param('[true, "sha", "stale"]', id="boolean-timestamp"),
            pytest.param('[1, 2, "stale"]', id="non-string-sha"),
            pytest.param('[1, "sha"]', id="missing-currency"),
        ],
    )
    def test_unusable_stale_record_emits_full_notice(
        self, stale_proj: Project, bare_plugin_root: Path, hook_tmp: Path, content: str
    ) -> None:
        """Legacy or corrupt records fail open and are replaced with the emitted state.

        A bare timestamp, garbage, or a record of the wrong shape cannot dedup anything, so the full notice is shown and
        a valid record is written.
        """
        flag = _stale_flag(hook_tmp, stale_proj.name)
        flag.write_text(content)

        result = _run_inject(stale_proj.repo, stale_proj.idx_dir, bare_plugin_root, hook_tmp)

        assert result.returncode == 0, result.stderr
        assert "Prefer `codemap-py query`" in result.stdout
        assert "2 modules" in result.stdout
        assert json.loads(flag.read_text())[1:] == ["0" * 40, "stale"]


# ── guard-redundant-scan ─────────────────────────────────────────────────────────

_EXHAUSTED_MODULES = ["mypackage.auth", "mypackage/auth"]


def _run_guard(command: str, session: str, tmpdir: Path, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Drive guard-redundant-scan.py with an isolated TMPDIR for the sentinel lookup."""
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "session_id": session}
    return _run_hook(_GUARD, json.dumps(payload), cwd, tmpdir)


def _seed_sentinel(tmpdir: Path, session: str, modules: list[str]) -> Path:
    """Write the exhausted-caller sentinel the guard reads (one module per line)."""
    sentinel = tmpdir / f"codemap-exhausted-{session}"
    sentinel.write_text("\n".join(modules) + "\n")
    return sentinel


def _decision(result: subprocess.CompletedProcess) -> str:
    """Return the guard's permission decision, or ``allow`` when it printed nothing."""
    return json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] if result.stdout else "allow"


class TestGuardRedundantScan:
    """Import-discovery grep denial keyed on the per-session exhausted sentinel."""

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param('grep -rn "import mypackage.auth" .', id="dotted-import"),
            pytest.param('grep -rn "from mypackage/auth" src/', id="slashed-from"),
            pytest.param('rg "import mypackage.auth"', id="rg-dotted"),
            pytest.param('grep -rn "import mypackage.auth" src/', id="grep-with-directory"),
            pytest.param('rg "from mypackage.auth" -n', id="rg-with-trailing-flag"),
        ],
    )
    def test_exhausted_module_grep_denied(self, command: str, tmp_path: Path) -> None:
        """A grep naming an exhausted module (dotted or slashed) is denied with a reason naming the module.

        The sentinel carries both module spellings, as record-exhausted writes them. Narrowing the guard's pattern to
        real search commands must not stop the greps it exists to deny, with or without a directory or trailing flag.
        """
        _seed_sentinel(tmp_path, "sess-exhausted", _EXHAUSTED_MODULES)

        result = _run_guard(command, "sess-exhausted", tmp_path)

        assert result.returncode == 0, result.stderr
        decision = json.loads(result.stdout)["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"
        assert "mypackage.auth" in decision["permissionDecisionReason"]

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param('grep -rn "import mypackage.auth2" .', id="near-miss-suffix"),
            pytest.param('grep -rn "import notmypackage.auth" .', id="near-miss-prefix"),
            pytest.param('python -c "import mypackage.auth" && rm -r tmp', id="python-one-liner-with-rm-r"),
            pytest.param("python -m pip install -r requirements.txt  # import mypackage.auth", id="pip-install-r"),
            pytest.param("mv mypackage/auth.py mypackage/auth_v2.py", id="move-file"),
            pytest.param("ls mypackage.auth", id="non-grep-command"),
        ],
    )
    def test_command_not_denied(self, command: str, tmp_path: Path) -> None:
        """Only a real search for exactly an exhausted module is denied; everything else passes untouched.

        Near-miss names that merely contain an exhausted module are matched on a word boundary. Commands that run no
        search tool — even one pairing the word "import" with a ``-r`` flag, such as ``rm -r`` or ``pip install -r`` —
        are not inspected, while the sentinel is armed.
        """
        _seed_sentinel(tmp_path, "sess-allowed", _EXHAUSTED_MODULES)

        result = _run_guard(command, "sess-allowed", tmp_path)

        assert result.returncode == 0, result.stderr
        assert result.stdout == "", f"command must not be denied: {command}"

    def test_no_sentinel_allows(self, tmp_path: Path) -> None:
        """No sentinel for the session means nothing is marked exhaustive, so the grep is allowed.

        Without a recorded caller set the guard has no authority to deny.
        """
        result = _run_guard('grep -rn "import mypackage.auth" .', "sess-none", tmp_path)

        assert result.returncode == 0, result.stderr
        assert result.stdout == ""


class TestGuardSentinelExpiry:
    """A recorded caller set stops denying once its authority can no longer be trusted.

    ``record-exhausted.py`` drops the sentinel on every Claude source edit; the TTL here bounds the mutation that hook
    never sees (a checkout, an external editor, a sibling agent), which previously left a deny armed for the rest of the
    session.
    """

    _TTL_S = 30 * 60  # guard-redundant-scan.py _SENTINEL_TTL_S

    @pytest.mark.parametrize(
        ("age_s", "verdict"),
        [
            pytest.param(0, "deny", id="fresh-sentinel-denies"),
            pytest.param(_TTL_S + 60, "allow", id="expired-sentinel-allows"),
        ],
    )
    def test_sentinel_denies_only_inside_ttl(self, tmp_path: Path, age_s: int, verdict: str) -> None:
        """A just-recorded caller set denies; past the TTL the same sentinel no longer has the authority to.

        An expired exhaustive result must not keep denying greps for the rest of the session.
        """
        sentinel = _seed_sentinel(tmp_path, "sess-ttl", ["mypackage.auth"])
        aged = time.time() - age_s
        os.utime(sentinel, (aged, aged))

        result = _run_guard('grep -rn "import mypackage.auth" .', "sess-ttl", tmp_path)

        assert result.returncode == 0, result.stderr
        assert _decision(result) == verdict


class TestGuardMissingSessionKey:
    """Two projects without a session id must not deny through one shared sentinel."""

    @pytest.mark.parametrize(
        ("project", "verdict"),
        [
            pytest.param("proj-beta", "allow", id="other-projects-sentinel-ignored"),
            pytest.param("proj-alpha", "deny", id="own-fallback-sentinel-denies"),
        ],
    )
    def test_fallback_key_is_per_project(self, tmp_path: Path, project: str, verdict: str) -> None:
        """A sentinel armed under one project's fallback key is invisible to another and works for its own.

        With no ``session_id`` the guard keys on project plus ``CSID``. The sentinel is armed for ``proj-alpha``:
        ``proj-beta`` must be allowed, while ``proj-alpha`` proves the fallback key is working, not disabled.
        """
        (tmp_path / "proj-alpha").mkdir()
        (tmp_path / "proj-beta").mkdir()
        _seed_sentinel(tmp_path, "proj-alpha-session-one", ["mypackage.auth"])
        payload = {"tool_name": "Bash", "tool_input": {"command": 'grep -rn "import mypackage.auth" .'}}

        result = _run_hook(_GUARD, json.dumps(payload), tmp_path / project, tmp_path, CSID="session-one")

        assert result.returncode == 0, result.stderr
        assert _decision(result) == verdict


# ── seed-session ─────────────────────────────────────────────────────────────────


def _run_seed(payload: dict, cwd: Path, tmpdir: Path, *, runtime: str = "claude") -> subprocess.CompletedProcess:
    """Drive seed-session.py with cwd + TMPDIR pinned so the session tmpfile is observable."""
    return _run_hook(_SEED, json.dumps(payload), cwd, tmpdir, CODEMAP_RUNTIME=runtime)


class TestSeedSession:
    """SessionStart seeding of the per-project session tmpfile."""

    def test_writes_session_id_to_project_tmpfile(self, tmp_path: Path, hook_tmp: Path) -> None:
        """A non-empty session_id is written to codemap-<project>-session in TMPDIR.

        The project name is the git-root basename, which later hooks use to find the session id.
        """
        repo = tmp_path / "proj"
        _init_repo(repo)

        result = _run_seed({"session_id": "sid-xyz"}, repo, hook_tmp)

        assert result.returncode == 0, result.stderr
        assert (hook_tmp / f"codemap-{repo.name}-session").read_text() == "sid-xyz"

    def test_empty_session_id_writes_nothing(self, tmp_path: Path, hook_tmp: Path) -> None:
        """An empty session_id is a no-op — no tmpfile written.

        An empty identity would overwrite a valid marker with nothing useful.
        """
        repo = tmp_path / "proj"
        _init_repo(repo)

        result = _run_seed({"session_id": ""}, repo, hook_tmp)

        assert result.returncode == 0, result.stderr
        assert not (hook_tmp / f"codemap-{repo.name}-session").exists()

    def test_codex_start_does_not_overwrite_claude_marker(self, tmp_path: Path, hook_tmp: Path) -> None:
        """Codex owns no shared temp marker and cannot relabel later Claude logs.

        A Claude session seeds the marker, then a Codex thread starts in the same project; the marker keeps the Claude
        session id.
        """
        repo = tmp_path / "proj"
        _init_repo(repo)

        claude = _run_seed({"session_id": "claude-session"}, repo, hook_tmp)
        codex = _run_seed({"thread_id": "codex-thread"}, repo, hook_tmp, runtime="codex")

        assert claude.returncode == 0
        assert codex.returncode == 0
        assert (hook_tmp / f"codemap-{repo.name}-session").read_text() == "claude-session"


# ── log-skill-start ──────────────────────────────────────────────────────────────


def _run_skill(payload: dict, cwd: Path, tmpdir: Path) -> subprocess.CompletedProcess:
    """Drive log-skill-start.py with cwd + TMPDIR pinned (and no inherited log directory)."""
    return _run_hook(_SKILL, json.dumps(payload), cwd, tmpdir)


def _skill_records(cwd: Path) -> list[dict]:
    """Return all records across flat legacy and runtime-scoped skill shards."""
    log_dir = cwd / ".cache" / "codemap" / "logs"
    records: list[dict] = []
    for shard in sorted(log_dir.rglob("skills*.jsonl")):
        records += [json.loads(line) for line in shard.read_text().splitlines() if line.strip()]
    return records


class TestLogSkillStart:
    """Record structural-query skill invocations from the pre-tool hook."""

    def test_codemap_skill_logged(self, tmp_path: Path, hook_tmp: Path) -> None:
        """A codemap:* Skill call appends one start record carrying skill + intent.

        The record also names the layer and runtime and carries a real plugin version.
        """
        payload = {
            "tool_name": "Skill",
            "tool_input": {"skill": "codemap-py:query-code", "args": "who calls foo"},
            "session_id": "hook-sid",
        }

        result = _run_skill(payload, tmp_path, hook_tmp)

        assert result.returncode == 0, result.stderr
        (record,) = _skill_records(tmp_path)
        assert record["skill"] == "codemap-py:query-code"
        assert record["event"] == "start"
        assert record["intent"] == "who calls foo"
        assert record["layer"] == "skill"
        assert record["runtime"] == "claude"
        assert record["v"] not in ("", "?")

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({"tool_name": "Skill", "tool_input": {"skill": "foundry:audit"}}, id="non-codemap-skill"),
            pytest.param({"tool_name": "Bash", "tool_input": {"command": "ls"}}, id="non-skill-tool"),
        ],
    )
    def test_unrelated_call_ignored(self, tmp_path: Path, hook_tmp: Path, payload: dict) -> None:
        """A non-codemap skill or a non-Skill tool call is ignored — no record written.

        The hook only logs codemap skills; any other call must leave the log untouched.
        """
        result = _run_skill(payload, tmp_path, hook_tmp)

        assert result.returncode == 0, result.stderr
        assert _skill_records(tmp_path) == []

    def test_seeded_session_shard_join(self, tmp_path: Path, hook_tmp: Path) -> None:
        """A seeded session tmpfile routes the record to skills_<session>.jsonl.

        Outside a repository the project key falls back to the cwd basename, so the seeded marker is named for it.
        """
        (hook_tmp / f"codemap-{tmp_path.name}-session").write_text("seeded-sid")
        payload = {"tool_name": "Skill", "tool_input": {"skill": "codemap-py:test-impact"}}

        result = _run_skill(payload, tmp_path, hook_tmp)

        assert result.returncode == 0, result.stderr
        shard = tmp_path / ".cache" / "codemap" / "logs" / "claude" / "skills_seeded-sid.jsonl"
        assert shard.exists(), "record not routed to the seeded per-session shard"
        assert json.loads(shard.read_text().strip())["session"] == "seeded-sid"


class TestGarbageStdin:
    """Hooks driven with unparsable stdin fail open and leave no trace."""

    @pytest.mark.parametrize(
        ("hook", "stdin"),
        [
            pytest.param(_GUARD, "not json at all", id="guard-redundant-scan"),
            pytest.param(_SEED, "}{ not json", id="seed-session"),
            pytest.param(_SKILL, "not-json", id="log-skill-start"),
        ],
    )
    def test_fails_open_and_writes_nothing(self, tmp_path: Path, hook_tmp: Path, hook: Path, stdin: str) -> None:
        """Non-JSON stdin exits 0 with no output, no session sentinel and no log record.

        The hooks run on every host event; a malformed payload must never block the tool call or leave partial state.
        """
        result = _run_hook(hook, stdin, tmp_path, hook_tmp)

        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
        assert list(hook_tmp.glob("codemap-*")) == []
        assert not (tmp_path / ".cache").exists()


# ── cross-hook session keying ────────────────────────────────────────────────────

_LOG_HOOK_CASES = pytest.mark.parametrize(
    ("hook", "payload", "layer"),
    [
        pytest.param(_TOOL_HOOK, {"tool_name": "Grep", "tool_input": {"pattern": "x"}}, "tools", id="tool-hook"),
        pytest.param(
            _SKILL, {"tool_name": "Skill", "tool_input": {"skill": "codemap-py:query-code"}}, "skills", id="skill-hook"
        ),
    ],
)


@pytest.fixture(name="subdir_repo")
def _subdir_repo(tmp_path: Path, hook_tmp: Path) -> tuple[Path, Path, Path]:
    """Return (tmpdir, repo root, nested working directory) for a throwaway repo."""
    repo = tmp_path / "myproj"
    _init_repo(repo)
    nested = repo / "src" / "pkg"
    nested.mkdir(parents=True)
    return hook_tmp, repo, nested


def _run_logged(hook: Path, payload: dict, cwd: Path, tmpdir: Path, **env: str | None) -> subprocess.CompletedProcess:
    """Drive *hook* from *cwd* with TMPDIR pinned and telemetry re-enabled."""
    return _run_hook(hook, json.dumps(payload), cwd, tmpdir, CODEMAP_LOGGING="true", **env)


class TestSessionKeyAgreement:
    """The marker's writer and its readers must agree on the project key from any cwd.

    ``seed-session`` keys the marker on the git-root basename, as does the cli layer
    (``codemap_py.telemetry.session_id``). The tool and skill hooks used to key it on
    ``Path.cwd().name``, so a Claude session started in a subdirectory looked for a marker
    nobody had written: every tool and skill record landed in an unsuffixed shard and the
    join across layers returned nothing while every hook still reported success.
    """

    def test_seed_from_subdir_writes_the_git_root_key(self, subdir_repo: tuple[Path, Path, Path]) -> None:
        """The marker is named for the repository, not for whichever directory ran the hook.

        Seeding from ``src/pkg`` must write ``codemap-myproj-session`` and not ``codemap-pkg-session``.
        """
        tmpdir, repo, nested = subdir_repo

        result = _run_logged(_SEED, {"session_id": "sid-sub"}, nested, tmpdir)

        assert result.returncode == 0, result.stderr
        assert (tmpdir / f"codemap-{repo.name}-session").read_text() == "sid-sub"
        assert not (tmpdir / f"codemap-{nested.name}-session").exists()

    @_LOG_HOOK_CASES
    def test_hook_from_subdir_joins_the_seeded_session(
        self, subdir_repo: tuple[Path, Path, Path], hook: Path, payload: dict, layer: str
    ) -> None:
        """A tool or skill record written from a subdirectory carries the seeded session id.

        The shard is asserted at the REPOSITORY root, not at the nested directory: the hook anchors its log dir to the
        project root, so a subdirectory invocation joins the same shard the cli layer writes. Asserting
        ``nested/.cache/...`` would pin the split-log defect itself — two halves of one session in two directories.
        """
        tmpdir, repo, nested = subdir_repo
        (tmpdir / f"codemap-{repo.name}-session").write_text("sid-sub")

        result = _run_logged(hook, payload, nested, tmpdir)

        assert result.returncode == 0, result.stderr
        shard = repo / ".cache" / "codemap" / "logs" / "claude" / f"{layer}_sid-sub.jsonl"
        assert shard.exists(), f"subdirectory {layer} record did not join the seeded session shard"
        record = json.loads(shard.read_text().strip())
        assert record["session"] == "sid-sub"
        assert record["runtime"] == "claude"
        assert record["v"] not in ("", "?")

    @_LOG_HOOK_CASES
    def test_event_session_overrides_other_session_marker(
        self, subdir_repo: tuple[Path, Path, Path], hook: Path, payload: dict, layer: str
    ) -> None:
        """Concurrent Claude sessions cannot inherit another event's project marker.

        The event carries its own ``session_id``; it must win over a marker left by another session, and the marker
        itself stays untouched.
        """
        tmpdir, repo, nested = subdir_repo
        marker = tmpdir / f"codemap-{repo.name}-session"
        marker.write_text("other-session")

        result = _run_logged(hook, {**payload, "session_id": "event-session"}, nested, tmpdir)

        assert result.returncode == 0, result.stderr
        shard = repo / ".cache/codemap/logs/claude" / f"{layer}_event-session.jsonl"
        assert json.loads(shard.read_text())["session"] == "event-session"
        assert marker.read_text() == "other-session"

    @_LOG_HOOK_CASES
    @pytest.mark.parametrize("variable", ["CLAUDE_CODE_SESSION_ID", "CSID"])
    def test_claude_environment_identity_precedes_marker(
        self, subdir_repo: tuple[Path, Path, Path], hook: Path, payload: dict, layer: str, variable: str
    ) -> None:
        """A payload without identity still joins the CLI's provided Claude session.

        Either session environment variable outranks a stale project marker when the event carries no session id.
        """
        tmpdir, repo, nested = subdir_repo
        (tmpdir / f"codemap-{repo.name}-session").write_text("stale-marker")

        result = _run_logged(hook, payload, nested, tmpdir, **{variable: "environment-session"})

        assert result.returncode == 0, result.stderr
        shard = repo / ".cache/codemap/logs/claude" / f"{layer}_environment-session.jsonl"
        record = json.loads(shard.read_text())
        assert record["session"] == "environment-session"
        assert record["project"] == repo.resolve().as_posix()

    def test_skill_hook_does_not_mint_a_second_session(self, subdir_repo: tuple[Path, Path, Path]) -> None:
        """Reading the marker from a subdirectory must not look absent and mint a rival id.

        After the skill hook runs from ``src/pkg`` the only session marker in TMPDIR is still the seeded one.
        """
        tmpdir, repo, nested = subdir_repo
        (tmpdir / f"codemap-{repo.name}-session").write_text("sid-sub")
        payload = {"tool_name": "Skill", "tool_input": {"skill": "codemap-py:scan-codebase"}}

        _run_logged(_SKILL, payload, nested, tmpdir)

        assert [p.name for p in tmpdir.glob("codemap-*-session")] == [f"codemap-{repo.name}-session"]


class TestPromptRuntimeOutput:
    """UserPromptSubmit output remains host-specific while index state stays shared."""

    def test_codex_prompt_uses_hook_specific_output(
        self, current_proj: Project, bare_plugin_root: Path, hook_tmp: Path
    ) -> None:
        """A Codex prompt emits the documented hookSpecificOutput envelope.

        The envelope names the UserPromptSubmit event and carries the preamble as additional context.
        """
        result = _run_inject(current_proj.repo, current_proj.idx_dir, bare_plugin_root, hook_tmp, runtime="codex")

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        assert "[codemap]" in payload["hookSpecificOutput"]["additionalContext"]

    def test_claude_prompt_remains_plain_text(
        self, current_proj: Project, bare_plugin_root: Path, hook_tmp: Path
    ) -> None:
        """Adding Codex output support must not wrap Claude's existing plain preamble.

        Claude's stdout is the bare preamble line, not JSON.
        """
        result = _run_inject(current_proj.repo, current_proj.idx_dir, bare_plugin_root, hook_tmp, runtime="claude")

        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith("[codemap]")
        with pytest.raises(json.JSONDecodeError):
            json.loads(result.stdout)


class TestCodexRuntimeTelemetry:
    """Codex hook calls must not inherit Claude's marker or runtime directory."""

    @pytest.mark.parametrize(
        ("hook", "payload", "shard_name"),
        [
            pytest.param(
                _TOOL_HOOK,
                {"tool_name": "Read", "tool_input": {"file_path": "pkg/mod.py"}},
                "tools.jsonl",
                id="tool-hook",
            ),
            pytest.param(
                _SKILL,
                {"tool_name": "Skill", "tool_input": {"skill": "codemap-py:query-code"}},
                "skills.jsonl",
                id="skill-hook",
            ),
        ],
    )
    def test_missing_identity_never_borrows_any_project_marker(
        self, tmp_path: Path, hook_tmp: Path, hook: Path, payload: dict, shard_name: str
    ) -> None:
        """A current project marker cannot prove which concurrent thread invoked a hook.

        With no thread id, Codex records get an empty session even though a Claude marker and another Codex thread's
        session file exist; neither marker is modified.
        """
        repo = tmp_path / "project"
        _init_repo(repo)
        (hook_tmp / f"codemap-{repo.name}-session").write_text("claude-session")
        cache = repo / ".cache" / "codemap"
        cache.mkdir(parents=True)
        (cache / "current-session-codex.json").write_text(json.dumps({"session_id": "another-codex-thread"}))

        result = _run_logged(hook, payload, repo, hook_tmp, CODEMAP_RUNTIME="codex")

        assert result.returncode == 0, result.stderr
        record = json.loads((cache / "logs" / "codex" / shard_name).read_text())
        assert record["session"] == ""
        assert record["project"] == repo.resolve().as_posix()
        assert (hook_tmp / f"codemap-{repo.name}-session").read_text() == "claude-session"

    def test_codex_tool_record_uses_thread_id_and_codex_shard(self, tmp_path: Path, hook_tmp: Path) -> None:
        """A Codex Read uses CODEX_THREAD_ID even when a Claude marker is present.

        The record lands in the Codex tools shard for that thread, and no Claude shard is created.
        """
        repo = tmp_path / "project"
        _init_repo(repo)
        (hook_tmp / f"codemap-{repo.name}-session").write_text("claude-session")
        payload = {"tool_name": "Read", "tool_input": {"file_path": "src/module.py"}}

        result = _run_logged(
            _TOOL_HOOK, payload, repo, hook_tmp, CODEMAP_RUNTIME="codex", CODEX_THREAD_ID="codex-thread"
        )

        assert result.returncode == 0, result.stderr
        logs = repo / ".cache" / "codemap" / "logs"
        record = json.loads((logs / "codex" / "tools_codex-thread.jsonl").read_text().strip())
        assert record["runtime"] == "codex"
        assert record["session"] == "codex-thread"
        assert not (logs / "claude" / "tools_claude-session.jsonl").exists()
