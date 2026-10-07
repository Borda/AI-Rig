"""Direction-scoped completeness and stale-index self-heal for scan-query.

Two behaviours are exercised:

* ``query_complete`` is scoped by command DIRECTION. A degraded (unparsable)
  file must never let a global-in (``rdeps``) or whole-graph (``central``) query
  claim completeness — those directions could hide an inbound / graph-wide edge —
  while a local query (``deps`` / ``symbols``) on a healthy module stays complete.
* A stale index is self-healed inline: committing a new edge and then querying
  ``rdeps`` re-scans the changed file and surfaces the new edge, bounded so a
  large change set falls back to the stale-honest result.

The heal tests need a real git repo because the staleness diff is git-blob based.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


def _scan(scan_index: Path, root: Path, *extra: str) -> None:
    """Run scan-index over *root*, asserting success."""
    result = subprocess.run(
        [sys.executable, str(scan_index), "--root", str(root), *extra],
        capture_output=True,
        text=True,
        cwd=str(root),
    )
    assert result.returncode == 0, result.stderr


def _query(scan_query: Path, root: Path, index_path: Path, *args: str) -> dict:
    """Run scan-query against *index_path* and return the parsed JSON."""
    result = subprocess.run(
        [sys.executable, str(scan_query), "--index", str(index_path), *args],
        capture_output=True,
        text=True,
        cwd=str(root),
    )
    assert result.returncode == 0, result.stderr + result.stdout
    return json.loads(result.stdout)


def _git(root: Path, *args: str) -> None:
    """Run a git command inside *root*, asserting success."""
    result = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.fixture(name="degraded_project", scope="module")
def _degraded_project(tmp_path_factory: pytest.TempPathFactory, scan_index: Path) -> tuple[Path, Path]:
    """Create a degraded non-Git project for completeness checks, built once per module.

    ``consumer`` imports ``leaf``; ``broken.py`` has a syntax error → degraded. Every test using it only queries the
    index (non-Git, so no self-heal rewrite), so one scan is shared across them.
    """
    root = tmp_path_factory.mktemp("degraded")
    (root / "leaf.py").write_text("def leaf_fn(x):\n    return x\n")
    (root / "consumer.py").write_text("import leaf\n\ndef use(x):\n    return leaf.leaf_fn(x)\n")
    (root / "broken.py").write_text("def oops(:\n    return\n")  # SyntaxError → degraded
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("CODEMAP_LOGGING", "false")  # the autouse gate is function-scoped, so it is off during setup
        _scan(scan_index, root)
    index_path = root / ".cache" / "codemap" / f"{root.name}.json"
    assert index_path.exists()
    return root, index_path


class TestDirectionScopedCompleteness:
    """query_complete is decided per command direction, gated by the degraded set."""

    def test_degraded_module_is_indexed(self, degraded_project, scan_query):
        """Sanity: the broken file registers as a degraded module in coverage."""
        root, index_path = degraded_project
        data = _query(scan_query, root, index_path, "deps", "consumer")
        assert data["index"]["degraded"] == 1
        assert any("broken.py" in entry["path"] for entry in data["index"]["degraded_files"])

    def test_local_deps_on_healthy_module_complete(self, degraded_project, scan_query):
        """Local `deps` on a cleanly-parsed module is complete despite a degraded file elsewhere."""
        root, index_path = degraded_project
        data = _query(scan_query, root, index_path, "deps", "consumer")
        assert data["index"]["query_complete"] is True
        assert data["index"]["exhaustive"] is True  # legacy alias tracks query_complete

    def test_local_symbols_on_healthy_module_complete(self, degraded_project, scan_query):
        """Local `symbols` on a healthy module is complete despite a degraded file elsewhere."""
        root, index_path = degraded_project
        data = _query(scan_query, root, index_path, "symbols", "leaf")
        assert data["index"]["query_complete"] is True

    def test_global_in_rdeps_incomplete_with_degraded(self, degraded_project, scan_query):
        """Global-in `rdeps` is never complete while a degraded file could hide an inbound edge."""
        root, index_path = degraded_project
        data = _query(scan_query, root, index_path, "rdeps", "leaf")
        assert data["index"]["query_complete"] is False
        assert data["index"]["exhaustive"] is False
        assert any("broken.py" in entry["path"] for entry in data["index"]["degraded_files"])

    def test_whole_graph_central_incomplete_with_degraded(self, degraded_project, scan_query):
        """Whole-graph `central` is never complete while any file is degraded."""
        root, index_path = degraded_project
        data = _query(scan_query, root, index_path, "central", "--top", "5")
        assert data["index"]["query_complete"] is False


@pytest.fixture(name="clean_project")
def _clean_project(tmp_path: Path, scan_index: Path) -> tuple[Path, Path]:
    """Create a healthy non-Git project for complete directional queries."""
    root = tmp_path / "clean"
    root.mkdir()
    (root / "leaf.py").write_text("def leaf_fn(x):\n    return x\n")
    (root / "consumer.py").write_text("import leaf\n\ndef use(x):\n    return leaf.leaf_fn(x)\n")
    _scan(scan_index, root)
    index_path = root / ".cache" / "codemap" / f"{root.name}.json"
    return root, index_path


class TestCleanGraphCompleteness:
    """With zero degraded files, global-in and whole-graph queries reach completeness."""

    def test_rdeps_complete_when_no_degraded(self, clean_project, scan_query):
        """Global-in `rdeps` is complete on a clean, fresh (non-git → not-stale) index."""
        root, index_path = clean_project
        data = _query(scan_query, root, index_path, "rdeps", "leaf")
        assert data["index"]["degraded"] == 0
        assert data["index"]["query_complete"] is True

    def test_central_complete_when_no_degraded(self, clean_project, scan_query):
        """Whole-graph `central` is complete on a clean index."""
        root, index_path = clean_project
        data = _query(scan_query, root, index_path, "central", "--top", "5")
        assert data["index"]["query_complete"] is True


@pytest.fixture(name="git_project")
def _git_project(tmp_path: Path, scan_index: Path) -> tuple[Path, Path]:
    """Create a committed project for staleness detection and repair."""
    root = tmp_path / "gitrepo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.t")
    _git(root, "config", "user.name", "t")
    (root / "leaf.py").write_text("def leaf_fn(x):\n    return x\n")
    (root / "consumer.py").write_text("import leaf\n\ndef use(x):\n    return leaf.leaf_fn(x)\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    _scan(scan_index, root)
    index_path = root / ".cache" / "codemap" / f"{root.name}.json"
    return root, index_path


class TestSelfHeal:
    """A stale index is refreshed inline (bounded) before the query answers."""

    def test_heal_surfaces_new_edge(self, git_project, scan_query):
        """Committing a new importer then querying rdeps auto-heals and reflects the new edge."""
        root, index_path = git_project
        # New module importing leaf, committed AFTER the index was built → index is stale.
        (root / "newcaller.py").write_text("import leaf\n\ndef also(x):\n    return leaf.leaf_fn(x)\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "add newcaller")

        data = _query(scan_query, root, index_path, "rdeps", "leaf")
        assert "newcaller" in data["imported_by"], "self-heal must surface the newly-committed edge"
        assert data["index"]["stale"] is False, "post-heal index must report fresh"
        assert data["index"]["query_complete"] is True

    def test_no_heal_flag_keeps_stale_result(self, git_project, scan_query):
        """Verify command-line option behavior.

        --no-heal answers from the stale index: new edge invisible, stale flagged honestly.
        """
        root, index_path = git_project
        (root / "newcaller.py").write_text("import leaf\n\ndef also(x):\n    return leaf.leaf_fn(x)\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "add newcaller")

        data = _query(scan_query, root, index_path, "--no-heal", "rdeps", "leaf")
        assert "newcaller" not in data["imported_by"], "stale index must not see the new edge"
        assert data["index"]["stale"] is True
        assert data["index"]["query_complete"] is False


def test_fresh_index_with_stub_and_docs_is_not_falsely_stale(
    tmp_path: Path, scan_index: Path, scan_query: Path
) -> None:
    """Query staleness must compare every source kind hashed by the scanner."""
    root = tmp_path / "documented"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.t")
    _git(root, "config", "user.name", "t")
    (root / "module.py").write_text("def public():\n    return 1\n")
    (root / "module.pyi").write_text("def public() -> int: ...\n")
    (root / "guide.rst").write_text("Guide\n=====\n")
    docs = root / "docs"
    docs.mkdir()
    (docs / "reference.md").write_text("# Reference\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "documented")
    _scan(scan_index, root)
    index_path = root / ".cache" / "codemap" / f"{root.name}.json"

    data = _query(scan_query, root, index_path, "--no-heal", "central", "--top", "5")

    assert data["index"]["stale"] is False
    assert data["index"]["query_complete"] is True


class TestHealBound:
    """The heal is bounded: a large change set falls back to the stale-honest result."""

    def test_large_change_set_skips_heal(self, git_project, scan_query, monkeypatch):
        """More changed files than the heal cap → answer from the stale index, flagged stale."""
        root, index_path = git_project
        # Add more changed files than the cap so the heal is skipped. The cap lives in
        # scan-query as _HEAL_MAX_CHANGED_FILES (50); overshoot it deterministically.
        for i in range(60):
            (root / f"mod{i}.py").write_text(f"import leaf\n\ndef f{i}(x):\n    return leaf.leaf_fn(x)\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "many")

        data = _query(scan_query, root, index_path, "rdeps", "leaf")
        assert data["index"]["stale"] is True, "over-cap change set must leave the index stale"
        assert "mod0" not in data["imported_by"], "skipped heal must not surface new edges"


class TestUntrackedFileVeto:
    """An untracked new .py file vetoes global-in/whole-graph but never a local query."""

    def test_untracked_present(self, git_project, scan_query):
        """Sanity: an uncommitted new .py file is reported in the coverage `untracked_py` list."""
        root, index_path = git_project
        (root / "orphan.py").write_text("x = 1\n")  # present but never `git add`-ed
        data = _query(scan_query, root, index_path, "deps", "consumer")
        assert any("orphan.py" in p for p in data["index"]["untracked_py"])

    def test_untracked_does_not_veto_local(self, git_project, scan_query):
        """Keep healthy dependency queries complete despite unrelated untracked source.

        An untracked file cannot change an already-indexed module's own direct_imports.
        """
        root, index_path = git_project
        (root / "orphan.py").write_text("x = 1\n")
        data = _query(scan_query, root, index_path, "deps", "consumer")
        assert data["index"]["query_complete"] is True

    def test_incomplete_note_matches_flag(self, git_project, scan_query):
        """Rdeps (global-in) is incomplete while an untracked .py could hide an inbound edge.

        The note must agree with the flag, so an untracked-source veto is never reported as a complete result.
        """
        root, index_path = git_project
        (root / "orphan.py").write_text("x = 1\n")
        data = _query(scan_query, root, index_path, "rdeps", "leaf")
        assert data["index"]["query_complete"] is False
        assert "This result is complete" not in data["index"]["note"]
        assert "incomplete" in data["index"]["note"]


class TestCollisionVeto:
    """A qualname collision vetoes whole-graph always, but local only for the colliding module."""

    def _inject_collision(self, index_path: Path, name: str) -> None:
        """Write a `collisions` entry (as scan-index would on collision) into an existing index file."""
        data = json.loads(index_path.read_text())
        data["collisions"] = [{"name": name, "kept": f"{name}.a", "dropped": f"{name}.b"}]
        index_path.write_text(json.dumps(data))

    def test_collision_count_surfaced(self, clean_project, scan_query):
        """collision_count reflects an injected collisions list (missing key would be 0)."""
        root, index_path = clean_project
        self._inject_collision(index_path, "somewhere")
        data = _query(scan_query, root, index_path, "deps", "consumer")
        assert data["index"]["collision_count"] == 1

    @pytest.mark.parametrize(
        ("collided_name", "query_args", "expected_complete"),
        [
            pytest.param("somewhere", ("central", "--top", "5"), False, id="whole-graph-vetoed-by-any-collision"),
            pytest.param("unrelated.module", ("deps", "consumer"), True, id="unrelated-local-stays-complete"),
            pytest.param("consumer", ("deps", "consumer"), False, id="own-local-vetoed"),
        ],
    )
    def test_collision_vetoes_by_direction(
        self, clean_project, scan_query, collided_name, query_args, expected_complete
    ):
        """A collision vetoes whole-graph queries always, and a local query only for the colliding module.

        Central (whole-graph) is incomplete whenever any collision dropped a module; ``deps`` stays complete for a
        module outside the collision and is marked incomplete for the colliding module name itself.
        """
        root, index_path = clean_project
        self._inject_collision(index_path, collided_name)
        data = _query(scan_query, root, index_path, *query_args)
        assert data["index"]["query_complete"] is expected_complete


def _build_excluded_git_project(root: Path, scan_index: Path) -> tuple[Path, Path]:
    """Create a committed project containing files excluded through both mechanisms.

    ``vendored/vendored.py`` is excluded via ``.codemapignore`` (dropped from file_shas); ``.claude/ghost.py`` sits in a
    built-in SKIP_DIR but is git-tracked (kept in the git-blob file_shas, since scan-index's git path filters only user
    exclusions).
    """
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.t")
    _git(root, "config", "user.name", "t")
    (root / "leaf.py").write_text("def leaf_fn(x):\n    return x\n")
    (root / "consumer.py").write_text("import leaf\n\ndef use(x):\n    return leaf.leaf_fn(x)\n")
    (root / "vendored").mkdir()
    (root / "vendored" / "vendored.py").write_text("def v():\n    return 0\n")
    (root / ".codemapignore").write_text("vendored\n")
    (root / ".claude").mkdir()
    (root / ".claude" / "ghost.py").write_text("def ghost():\n    return 1\n")
    _git(root, "add", "-A", "-f")  # -f so .claude/ghost.py is tracked despite common ignores
    _git(root, "commit", "-q", "-m", "init")
    _scan(scan_index, root)
    index_path = root / ".cache" / "codemap" / f"{root.name}.json"
    return root, index_path


@pytest.fixture(name="excluded_git_project")
def _excluded_git_project(tmp_path: Path, scan_index: Path) -> tuple[Path, Path]:
    """Create a fresh excluded-files project for tests that add untracked files to it."""
    return _build_excluded_git_project(tmp_path / "excluded", scan_index)


@pytest.fixture(name="pristine_excluded_git_project", scope="module")
def _pristine_excluded_git_project(tmp_path_factory: pytest.TempPathFactory, scan_index: Path) -> tuple[Path, Path]:
    """Create the excluded-files project once per module for tests that only query it.

    Every consumer queries with ``--no-heal`` and never writes under the root, so the committed tree and index stay
    pristine across the tests sharing it.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("CODEMAP_LOGGING", "false")  # the autouse gate is function-scoped, so it is off during setup
        return _build_excluded_git_project(tmp_path_factory.mktemp("pristine") / "excluded", scan_index)


class TestExclusionAwareStaleness:
    """Apply scanner exclusions consistently during query staleness checks."""

    def test_fresh_excluded_repo_not_stale(self, pristine_excluded_git_project, scan_query):
        """A .codemapignore-excluded tracked .py must not force a false stale on a fresh index."""
        root, index_path = pristine_excluded_git_project
        data = _query(scan_query, root, index_path, "--no-heal", "deps", "consumer")
        assert data["index"]["stale"] is False

    def test_excluded_repo_local_complete(self, pristine_excluded_git_project, scan_query):
        """With no real change, local `deps` reaches completeness despite excluded/SKIP_DIR files."""
        root, index_path = pristine_excluded_git_project
        data = _query(scan_query, root, index_path, "--no-heal", "deps", "consumer")
        assert data["index"]["query_complete"] is True

    def test_excluded_repo_rdeps_complete(self, pristine_excluded_git_project, scan_query):
        """Global-in `rdeps` reaches completeness — excluded files are not phantom edges."""
        root, index_path = pristine_excluded_git_project
        data = _query(scan_query, root, index_path, "--no-heal", "rdeps", "leaf")
        assert data["index"]["stale"] is False
        assert data["index"]["query_complete"] is True

    @pytest.mark.parametrize(
        ("excluded_dir", "orphan_name", "orphan_source"),
        [
            pytest.param("vendored", "new_orphan", "y = 2\n", id="codemapignore-excluded-dir"),
            pytest.param(".claude", "new_scratch", "z = 3\n", id="builtin-skip-dir"),
        ],
    )
    def test_untracked_in_excluded_dir_does_not_poison(
        self, excluded_git_project, scan_query, excluded_dir, orphan_name, orphan_source
    ):
        """An untracked .py inside an excluded dir must not appear in untracked_py or block completeness.

        Covers a ``.codemapignore``-excluded directory and a built-in SKIP_DIR alike.
        """
        root, index_path = excluded_git_project
        (root / excluded_dir / f"{orphan_name}.py").write_text(orphan_source)  # untracked, inside an excluded dir
        data = _query(scan_query, root, index_path, "--no-heal", "rdeps", "leaf")
        assert not any(orphan_name in p for p in data["index"]["untracked_py"])
        assert data["index"]["query_complete"] is True


class TestUntrackedCompleteness:
    """Untracked files veto wide queries only while the index does not cover them."""

    def test_new_untracked_file_vetoes_wide_query(self, git_project, scan_query):
        """A brand-new untracked importer is invisible to the SHA diff → rdeps must not claim complete."""
        root, index_path = git_project
        (root / "scratch.py").write_text("import leaf\n")
        data = _query(scan_query, root, index_path, "--no-heal", "rdeps", "leaf")
        assert data["index"]["query_complete"] is False
        assert data["index"]["completeness_reason"] == "untracked"
        assert "scratch.py" in data["index"]["untracked_py"]

    def test_indexed_untracked_does_not_veto(self, git_project, scan_query):
        """Once a re-scan indexes the still-untracked file, its edges are covered → no veto."""
        root, index_path = git_project
        scratch = root / "scratch.py"
        scratch.write_text("import leaf\n")
        _scan(_self_scan_index(scan_query), root)
        data = _query(scan_query, root, index_path, "--no-heal", "rdeps", "leaf")
        assert data["index"]["untracked_py"] == []
        assert data["index"]["query_complete"] is True
        assert data["index"]["completeness_reason"] == "ok"

    def test_indexed_untracked_edit_marks_stale(self, git_project, scan_query):
        """Editing an indexed-but-untracked file after the scan flips staleness (mtime signal)."""
        import os as _os
        import time as _time

        root, index_path = git_project
        scratch = root / "scratch.py"
        scratch.write_text("import leaf\n")
        _scan(_self_scan_index(scan_query), root)
        future = _time.time() + 30
        _os.utime(scratch, (future, future))
        data = _query(scan_query, root, index_path, "--no-heal", "rdeps", "leaf")
        assert data["index"]["stale"] is True
        assert data["index"]["query_complete"] is False
        assert data["index"]["completeness_reason"] == "stale"


def _self_scan_index(scan_query: Path) -> Path:
    """Resolve the sibling scan-index binary from the scan-query path."""
    return scan_query.parent / "scan-index"
