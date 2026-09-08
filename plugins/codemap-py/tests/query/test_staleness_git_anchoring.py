"""Staleness resolution is git-root anchored, memoized, and honest when git fails.

Four independent defects are gated here, one class each:

* **Anchoring** — every git subprocess behind the staleness check runs with ``cwd``
  at the repository root. The index records paths relative to that root, so an
  unanchored call issued from a subdirectory got subdirectory-relative paths back:
  every stored path then read as "deleted" and every listed path as "added", making
  a query from any subdirectory report a permanently stale index, self-heal on every
  call, and answer ``query_complete: false`` forever.
* **Memoization** — the tracked-blob read is resolved once per invocation. Two
  consumers (the self-heal decision and the coverage block) asked the same question
  and spawned two identical ``git ls-files`` subprocesses per query.
* **Failure honesty** — a git failure *inside* a repository reports staleness as
  undetermined. Swallowed by a blanket ``except Exception``, it previously read as
  "no files changed", i.e. as positive evidence of a fresh index.
* **File-set parity** — the v2 timestamp fallback watches the same file set the
  index writer records, so a changed ``.pyi``/``.rst``/doc file cannot report "fresh".

The anchoring and file-set classes need a real git repo because both checks are
git-driven; the memoization and failure classes drive the resolver in-process so the
subprocess count and the failure taxonomy can be observed exactly.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from codemap_py import query


def test_incremental_self_heal_propagates_refresh_provenance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The child scan receives self-heal facts while preserving unrelated runtime state."""
    scan_bin = tmp_path / "scan-index"
    scan_bin.write_text("placeholder")
    captured: dict[str, object] = {}

    def _fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Capture the incremental-scan command and return success."""
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setenv("CODEMAP_RUNTIME", "codex")
    monkeypatch.setattr(query.subprocess, "run", _fake_run)

    assert query._run_incremental_scan(scan_bin, tmp_path, 3) is True
    env = captured["kwargs"]["env"]  # type: ignore[index]
    assert env["CODEMAP_RUNTIME"] == "codex"  # type: ignore[index]
    assert env["CODEMAP_REFRESH_TRIGGER"] == "query_self_heal"  # type: ignore[index]
    assert env["CODEMAP_REFRESH_CHANGED_COUNT"] == "3"  # type: ignore[index]
    assert env["CODEMAP_REFRESH_STALE_BEFORE"] == "true"  # type: ignore[index]


def _literal_cap(source: str, name: str) -> int:
    """Return the integer *name* is assigned to at module level in *source*.

    Read by AST rather than by importing: two of the three helpers are standalone
    executables (one has no ``.py`` suffix at all) and importing them for a constant
    would drag in their side effects.

    >>> _literal_cap("LIMIT = 7\\n", "LIMIT")
    7
    """
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return int(eval(compile(ast.Expression(node.value), "<cap>", "eval")))  # noqa: S307
    raise AssertionError(f"{name} not assigned at module level")


def _git(root: Path, *args: str) -> None:
    """Run a git command inside *root*, asserting success."""
    result = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _scan(scan_index: Path, root: Path, *extra: str) -> None:
    """Run scan-index over *root*, asserting success."""
    result = subprocess.run(
        [sys.executable, str(scan_index), "--root", str(root), *extra],
        capture_output=True,
        text=True,
        cwd=str(root),
    )
    assert result.returncode == 0, result.stderr


def _query_from(scan_query: Path, cwd: Path, index_path: Path, *args: str, frozen: bool = False) -> dict:
    """Run scan-query from *cwd* against *index_path* and return the parsed JSON.

    Args:
        scan_query: path to the ``scan-query`` entry point.
        cwd: directory to launch the query from — the whole point of these tests.
        index_path: index to query.
        *args: subcommand and its arguments.
        frozen: set ``SCAN_NO_AUTOBUILD=1`` to suppress the inline self-heal, so a
            staleness verdict can be observed instead of being repaired before it is
            reported.
    """
    env = {**os.environ, "CODEMAP_LOGGING": "false"}
    if frozen:
        env["SCAN_NO_AUTOBUILD"] = "1"
    result = subprocess.run(
        [sys.executable, str(scan_query), "--index", str(index_path), *args],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=env,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    return json.loads(result.stdout)


@pytest.fixture(name="committed_repo")
def _committed_repo(tmp_path: Path, scan_index: Path) -> tuple[Path, Path]:
    """Create a clean indexed repository with a nested package.

    Nesting matters: ``pkg/deep/`` is the subdirectory a query is issued from, and
    is deep enough that subdirectory-relative paths cannot coincide with the
    root-relative paths the index recorded.
    """
    root = tmp_path / "anchored"
    (root / "pkg" / "deep").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "gamma.py").write_text('"""Leaf."""\n\n\ndef func_gamma(x):\n    return x + 1\n')
    (root / "pkg" / "alpha.py").write_text(
        '"""Imports gamma."""\n\nimport pkg.gamma as gamma\n\n\ndef func_alpha(x):\n    return gamma.func_gamma(x)\n'
    )
    (root / "pkg" / "deep" / "__init__.py").write_text("")
    (root / "pkg" / "deep" / "leaf.py").write_text('"""Deep leaf."""\n\n\ndef deep_fn():\n    return 1\n')
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.t")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    _scan(scan_index, root)
    index_path = root / ".cache" / "codemap" / f"{root.name}.json"
    assert index_path.exists()
    return root, index_path


# Commit dates are pinned so a ``--since`` window can sit strictly between the two commits.
# With ambient timestamps both commits fall inside any window wide enough to catch the
# second, and a pathspec test then passes on the first commit's .py files no matter what
# the pathspec says.
_SOURCE_COMMIT_DATE = "2020-01-01T00:00:00+0000"
_STUB_COMMIT_DATE = "2021-01-01T00:00:00+0000"
_BETWEEN_COMMITS = "2020-06-01T00:00:00+00:00"
_AFTER_ALL_COMMITS = "2022-01-01T00:00:00+00:00"


def _commit_at(root: Path, when: str, message: str) -> None:
    """Commit staged changes with author and committer date both pinned to *when*.

    ``git log --since`` filters on committer date, which ``--date`` alone does not set.
    """
    env = {**os.environ, "GIT_COMMITTER_DATE": when, "GIT_AUTHOR_DATE": when}
    result = subprocess.run(
        ["git", "commit", "-q", "-m", message, "--date", when],
        cwd=str(root),
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture(name="dated_repo")
def _dated_repo(tmp_path: Path) -> Path:
    """Create a repository whose second commit changes only a type stub.

    Isolating the stub in its own commit is what makes the file-set question decidable:
    if the pathspec does not cover ``*.pyi`` there is nothing else in that commit for it
    to match on.
    """
    root = tmp_path / "dated"
    root.mkdir()
    (root / "mod.py").write_text("def f():\n    return 1\n")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.t")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _commit_at(root, _SOURCE_COMMIT_DATE, "sources")
    (root / "mod.pyi").write_text("def f() -> int: ...\n")
    _git(root, "add", "-A")
    _commit_at(root, _STUB_COMMIT_DATE, "stub only")
    return root


@pytest.fixture(name="reset_query_caches")
def _reset_query_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the module-level git/staleness memos so each in-process test starts cold.

    ``query`` caches git root, exclusions, blob SHAs, and the coverage block for the lifetime of one CLI invocation;
    inside a single pytest process those would otherwise leak between tests.
    """
    monkeypatch.setattr(query, "_file_shas_cache", None)
    monkeypatch.setattr(query, "_coverage_cache", None)
    monkeypatch.setattr(query, "_git_root_cache", None)
    monkeypatch.setattr(query, "_git_root_resolved", False)


class TestGitRootAnchoring:
    """A query answers identically from the repository root and from a subdirectory."""

    def test_query_from_root_is_not_stale(self, committed_repo, scan_query) -> None:
        """Baseline: a freshly scanned, fully committed repo is not stale from its root."""
        root, index_path = committed_repo
        data = _query_from(scan_query, root, index_path, "deps", "pkg.alpha")
        assert data["index"]["stale"] is False

    def test_query_from_subdirectory_is_not_stale(self, committed_repo, scan_query) -> None:
        """The same unchanged repo is not stale when queried from a nested subdirectory."""
        root, index_path = committed_repo
        data = _query_from(scan_query, root / "pkg" / "deep", index_path, "deps", "pkg.alpha")
        assert data["index"]["stale"] is False

    def test_subdirectory_query_stays_complete(self, committed_repo, scan_query) -> None:
        """A subdirectory query still claims completeness rather than a phantom blind spot."""
        root, index_path = committed_repo
        data = _query_from(scan_query, root / "pkg" / "deep", index_path, "rdeps", "pkg.gamma")
        assert data["index"]["query_complete"] is True

    def test_subdirectory_query_reports_no_untracked_blind_spot(self, committed_repo, scan_query) -> None:
        """Fully committed tree: the untracked-file blind spot list is empty from a subdirectory."""
        root, index_path = committed_repo
        data = _query_from(scan_query, root / "pkg" / "deep", index_path, "rdeps", "pkg.gamma")
        assert data["index"]["untracked_py"] == []

    def test_real_edit_is_still_detected_from_subdirectory(self, committed_repo, scan_query) -> None:
        """Anchoring must not blind the check: an actual committed edit still reads as stale.

        Run frozen, because the inline self-heal would otherwise repair the index and report ``stale: false`` truthfully
        — hiding whether the change was seen at all.
        """
        root, index_path = committed_repo
        (root / "pkg" / "gamma.py").write_text('"""Leaf."""\n\n\ndef func_gamma(x):\n    return x + 99\n')
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "edit gamma")
        data = _query_from(scan_query, root / "pkg" / "deep", index_path, "deps", "pkg.alpha", frozen=True)
        assert data["index"]["stale"] is True


class TestFileShaMemoization:
    """The tracked-blob read costs one git subprocess per invocation, not one per consumer."""

    def test_repeated_reads_spawn_one_subprocess(self, reset_query_caches, monkeypatch) -> None:
        """Both staleness consumers share a single resolved result."""
        calls: list[list[str]] = []

        def _fake_check_output(cmd, **kwargs):
            """Return empty tracked-blob output while recording the git command."""
            calls.append(list(cmd))
            return ""

        monkeypatch.setattr(query, "_get_git_root_cached", lambda: Path("/repo"))
        monkeypatch.setattr(query.subprocess, "check_output", _fake_check_output)
        query._get_current_file_shas()
        query._get_current_file_shas()
        query._current_file_shas()
        assert len(calls) == 1

    def test_memoized_call_is_the_anchored_ls_files(self, reset_query_caches, monkeypatch) -> None:
        """The single subprocess is the root-anchored ``git ls-files -s`` read."""
        seen: dict[str, object] = {}

        def _fake_check_output(cmd, **kwargs):
            """Return empty tracked-blob output and retain subprocess arguments."""
            seen["cmd"] = list(cmd)
            seen["cwd"] = kwargs.get("cwd")
            return ""

        monkeypatch.setattr(query, "_get_git_root_cached", lambda: Path("/repo"))
        monkeypatch.setattr(query.subprocess, "check_output", _fake_check_output)
        query._get_current_file_shas()
        assert seen["cmd"][:4] == ["git", "ls-files", "-s", "--"]
        assert seen["cwd"] == str(Path("/repo"))


class TestGitFailureIsUndetermined:
    """A git failure inside a repository is reported, never read as a fresh index."""

    @staticmethod
    def _fail_git(monkeypatch, exc: Exception) -> None:
        """Make the tracked-blob git read fail while a repository root still resolves."""

        def _raise(cmd, **kwargs):
            """Raise the injected failure from the tracked-blob lookup."""
            raise exc

        monkeypatch.setattr(query, "_get_git_root_cached", lambda: Path("/repo"))
        monkeypatch.setattr(query.subprocess, "check_output", _raise)

    @pytest.mark.parametrize(
        "exc",
        [
            pytest.param(FileNotFoundError("git"), id="git-binary-missing"),
            pytest.param(subprocess.CalledProcessError(128, "git"), id="git-nonzero-exit"),
            pytest.param(subprocess.TimeoutExpired("git", 10), id="git-timeout"),
        ],
    )
    def test_failure_classifies_as_git_error(self, reset_query_caches, monkeypatch, exc) -> None:
        """Every git failure mode inside a repo resolves to the git_error status."""
        self._fail_git(monkeypatch, exc)
        assert query._current_file_shas().status == query._SHAS_GIT_ERROR

    def test_failure_emits_a_diagnostic(self, reset_query_caches, monkeypatch, capsys) -> None:
        """The failure is surfaced on stderr rather than swallowed silently."""
        self._fail_git(monkeypatch, FileNotFoundError("git"))
        query._current_file_shas()
        assert "UNDETERMINED" in capsys.readouterr().err

    def test_absent_repository_is_not_an_error(self, reset_query_caches, monkeypatch, capsys) -> None:
        """Outside a repository there is no git failure to report — the path stays quiet."""
        monkeypatch.setattr(query, "_get_git_root_cached", lambda: None)
        resolved = query._current_file_shas()
        assert resolved.status == query._SHAS_NO_REPO
        assert capsys.readouterr().err == ""

    def test_coverage_marks_staleness_undetermined(self, reset_query_caches, monkeypatch) -> None:
        """The coverage block carries the undetermined flag instead of a bare stale=False."""
        self._fail_git(monkeypatch, FileNotFoundError("git"))
        monkeypatch.setattr(query, "_untracked_py_files", lambda: [])
        base = query._coverage({"modules": [], "file_shas": {"a.py": "deadbeef"}})
        assert base["stale_undetermined"] is True

    def test_undetermined_staleness_vetoes_completeness(self, reset_query_caches, monkeypatch) -> None:
        """An unmeasurable index cannot yield a complete answer."""
        self._fail_git(monkeypatch, FileNotFoundError("git"))
        monkeypatch.setattr(query, "_untracked_py_files", lambda: [])
        base = query._coverage({"modules": [], "file_shas": {"a.py": "deadbeef"}})
        verdict = query._query_complete(base, command="central", module_status=None, module_name=None)
        assert verdict == (False, "stale_undetermined")

    def test_absent_repository_keeps_the_legacy_block(self, reset_query_caches, monkeypatch) -> None:
        """Without a repository the coverage block gains no new key (non-git trees unchanged)."""
        monkeypatch.setattr(query, "_get_git_root_cached", lambda: None)
        monkeypatch.setattr(query, "_untracked_py_files", lambda: [])
        base = query._coverage({"modules": [], "file_shas": {"a.py": "deadbeef"}})
        assert "stale_undetermined" not in base


class TestWriterReaderFileSetParity:
    """Both staleness paths watch the file set the index writer actually records."""

    @pytest.mark.parametrize("pattern", ["*.py", "*.pyi", "*.rst", "docs/**/*.md"])
    def test_pathspec_covers_every_indexed_kind(self, pattern: str) -> None:
        """Every file kind scan-index records a SHA for is watched for staleness."""
        assert pattern in query._INDEXED_PATHSPEC

    def test_blob_reader_passes_the_shared_pathspec(self, reset_query_caches, monkeypatch) -> None:
        """The v3 blob diff asks git for exactly the shared file set."""
        seen: list[str] = []

        def _fake_check_output(cmd, **kwargs):
            """Return empty tracked-blob output while recording command tokens."""
            seen.extend(cmd)
            return ""

        monkeypatch.setattr(query, "_get_git_root_cached", lambda: Path("/repo"))
        monkeypatch.setattr(query.subprocess, "check_output", _fake_check_output)
        query._get_current_file_shas()
        assert seen[seen.index("--") + 1 :] == list(query._INDEXED_PATHSPEC)

    def test_timestamp_fallback_passes_the_shared_pathspec(self, reset_query_caches, monkeypatch) -> None:
        """The v2 fallback asks git for the same file set, not a narrower hand-written one."""
        seen: list[str] = []

        def _fake_run(cmd, **kwargs):
            """Return a successful empty subprocess result while recording arguments."""
            seen.extend(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(query, "_get_git_root_cached", lambda: Path("/repo"))
        monkeypatch.setattr(query.subprocess, "run", _fake_run)
        query.check_staleness("2020-01-01T00:00:00+00:00")
        assert seen[seen.index("--") + 1 :] == list(query._INDEXED_PATHSPEC)

    def test_timestamp_fallback_sees_a_stub_only_commit(self, dated_repo, monkeypatch) -> None:
        """A commit touching only a ``.pyi`` registers as a change.

        The pre-fix pathspec watched ``*.py`` alone, so this commit read as "fresh" and a ``file_shas``-less index
        claimed currency it did not have.
        """
        monkeypatch.chdir(dated_repo)
        monkeypatch.setattr(query, "_git_root_cache", dated_repo)
        monkeypatch.setattr(query, "_git_root_resolved", True)
        assert query.check_staleness(_BETWEEN_COMMITS) is True

    def test_window_after_every_commit_reports_fresh(self, dated_repo, monkeypatch) -> None:
        """Control: past the last commit the same repo reports no change.

        Without this, the assertion above could pass merely because the window also swept in the earlier ``.py`` commit.
        """
        monkeypatch.chdir(dated_repo)
        monkeypatch.setattr(query, "_git_root_cache", dated_repo)
        monkeypatch.setattr(query, "_git_root_resolved", True)
        assert query.check_staleness(_AFTER_ALL_COMMITS) is False


class TestIndexSizeCapAgreement:
    """Every index-size ceiling in the plugin is the same number as the query engine's.

    This class replaces one that asserted the *opposite* — that the helpers deliberately capped lower. That divergence
    was never a second policy: at 50 MB the helpers refused real indexes, and this repository's own index measured 131
    MB, so `check-index-currency` answered ``no_index`` for it. ``no_index`` is what a project with no index at all
    reports, so the staleness gate silently stopped firing on exactly the large repositories it exists for. A helper
    ceiling below the engine's would recreate that blind spot.
    """

    #: Helper modules whose ceiling must match the engine's, with the constant's name.
    _HELPER_CAPS = (
        ("bin/check-index-currency", "MAX_INDEX_SIZE"),
        ("bin/scan-stats.py", "MAX_INDEX_SIZE"),
        ("bin/smoke_test_index.py", "MAX_INDEX_SIZE"),
    )

    def test_engine_cap_is_the_documented_value(self) -> None:
        """The ceiling stays pinned at the documented 512 MiB rather than drifting."""
        assert query._MAX_INDEX_SIZE_BYTES == 512 * 1024 * 1024

    def test_no_helper_caps_below_the_engine(self) -> None:
        """No helper refuses an index the engine would serve."""
        plugin_root = Path(query.__file__).resolve().parents[2]
        below = [
            f"{rel}={value}"
            for rel, const in self._HELPER_CAPS
            for value in [_literal_cap((plugin_root / rel).read_text(encoding="utf-8"), const)]
            if value < query._MAX_INDEX_SIZE_BYTES
        ]
        assert below == [], f"helper ceilings below the engine's {query._MAX_INDEX_SIZE_BYTES}: {below}"
