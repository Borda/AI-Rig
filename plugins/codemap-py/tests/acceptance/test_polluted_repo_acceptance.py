"""Integration-level acceptance for codemap index hardening (exclusions, dedup, completeness).

Driven through the shared ``polluted_repo`` factory fixture (see conftest.py): one realistic git repo exercising
exclusions, deterministic dedup, direction-scoped query completeness, and stale-index self-heal — the cross-task
interaction the per-concern unit suites (test_scan_index.py, test_query_complete.py) do not cover.

Tree recap for the collision-free variant, with dotted names in parentheses:

* ``pkg/leaf.py`` (``pkg.leaf``) is a healthy leaf.
* ``pkg/consumer.py`` (``pkg.consumer``) imports ``pkg.leaf``.
* ``pkg/broken.py`` (``pkg.broken``) contains a syntax error and is marked degraded.
* ``.claude/worktrees/agent-x/pkg/…`` is an excluded ghost copy that is pruned before it can collide.
* ``vendored-lib/vendored.py`` is pruned through ``.codemapignore``.

The ``with_collision=True`` variant adds ``wt/pkg/…`` to create a real ``pkg.*`` qualified-name collision.

``TestExcludedPathStaleness`` regression-guards a fixed scan-index/scan-query integration gap: scan-index filters
excluded paths out of the index ``file_shas`` while scan-query's staleness diff applies the same exclusions (shared via
``bin/_exclusions.py``), so a ``.codemapignore``-excluded tracked ``.py`` (here ``vendored-lib/vendored.py``) no longer
counts as "added" and the index is not falsely reported stale.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


def _query(scan_query: Path, root: Path, index_path: Path, *args: str) -> dict:
    """Run scan-query against *index_path* under *root* and return parsed JSON."""
    result = subprocess.run(
        [sys.executable, str(scan_query), "--index", str(index_path), *args],
        capture_output=True,
        text=True,
        cwd=str(root),
    )
    assert result.returncode == 0, result.stderr + result.stdout
    return json.loads(result.stdout)


def _rescan(scan_index: Path, root: Path) -> None:
    """Run a full scan-index over *root*, asserting success."""
    result = subprocess.run(
        [sys.executable, str(scan_index), "--root", str(root)],
        capture_output=True,
        text=True,
        cwd=str(root),
    )
    assert result.returncode == 0, result.stderr


def _git(root: Path, *args: str) -> None:
    """Run a git command inside *root*, asserting success."""
    result = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _module_paths(index: dict) -> set[str]:
    """Return the set of module paths present in an index."""
    return {m["path"] for m in index.get("modules", [])}


@pytest.mark.integration
class TestExclusions:
    """The ghost worktree and vendored dir never enter the module index."""

    def test_no_modules_under_excluded_roots(self, polluted_repo):
        """Fresh scan indexes zero modules under .claude/ or the vendored dir."""
        _root, index_path = polluted_repo()
        paths = _module_paths(json.loads(index_path.read_text()))
        assert not any(p.startswith(".claude/") for p in paths), paths
        assert not any(p.startswith("vendored-lib/") for p in paths), paths

    def test_healthy_pkg_modules_survive(self, polluted_repo):
        """Real package modules are still indexed after exclusions are applied."""
        _root, index_path = polluted_repo()
        paths = _module_paths(json.loads(index_path.read_text()))
        assert {"pkg/leaf.py", "pkg/consumer.py"} <= paths

    def test_codemapignore_entry_in_excluded_roots_meta(self, polluted_repo):
        """A .codemapignore dir appears in `excluded_roots` with source and a non-zero count."""
        _root, index_path = polluted_repo()
        index = json.loads(index_path.read_text())
        record = next((r for r in index["excluded_roots"] if r["pattern"] == "vendored-lib"), None)
        assert record is not None, index["excluded_roots"]
        assert record["source"] == ".codemapignore"
        assert record["count"] >= 1, "vendored dir held a .py file that must be counted"

    def test_builtin_skip_dir_stays_implicit(self, polluted_repo):
        """The built-in .claude SKIP_DIR is pruned but deliberately omitted from meta."""
        _root, index_path = polluted_repo()
        index = json.loads(index_path.read_text())
        patterns = {r["pattern"] for r in index["excluded_roots"]}
        assert ".claude" not in patterns


@pytest.mark.integration
class TestDedupDeterminism:
    """The non-excluded duplicate tree collides deterministically."""

    def test_collision_recorded_for_shared_qualname(self, polluted_repo):
        """pkg/leaf.py and wt/pkg/leaf.py share dotted name pkg.leaf → one collision record."""
        _root, index_path = polluted_repo(with_collision=True)
        index = json.loads(index_path.read_text())
        collision = next((c for c in index["collisions"] if c["name"] == "pkg.leaf"), None)
        assert collision is not None, index["collisions"]
        assert set(collision["dropped"]) | {collision["kept"]} == {"pkg/leaf.py", "wt/pkg/leaf.py"}

    def test_no_collision_in_collision_free_variant(self, polluted_repo):
        """Without the wt/ copy the index records no collisions."""
        _root, index_path = polluted_repo()
        assert json.loads(index_path.read_text())["collisions"] == []

    def test_ghost_copy_never_collides(self, polluted_repo):
        """The excluded .claude ghost copy is pruned before dedup, so it is in no collision record."""
        _root, index_path = polluted_repo(with_collision=True)
        index = json.loads(index_path.read_text())
        involved: set[str] = set()
        for c in index["collisions"]:
            involved.add(c["kept"])
            involved |= set(c["dropped"])
        assert not any(p.startswith(".claude/") for p in involved), involved

    def test_winner_stable_across_repeated_scans(self, polluted_repo, scan_index):
        """The dedup winner for pkg.leaf is identical across repeated full scans."""
        root, index_path = polluted_repo(with_collision=True)
        winners = set()
        for _ in range(3):
            _rescan(scan_index, root)
            index = json.loads(index_path.read_text())
            collision = next(c for c in index["collisions"] if c["name"] == "pkg.leaf")
            winners.add(collision["kept"])
        assert len(winners) == 1, f"dedup winner must be deterministic, saw {winners}"


@pytest.mark.integration
class TestDirectionScopedIncompleteness:
    """A degraded file forces global-in / whole-graph queries to report incomplete."""

    def test_degraded_file_registered(self, polluted_repo, scan_query):
        """broken.py registers as the sole degraded module, surfaced in degraded_files."""
        root, index_path = polluted_repo()
        data = _query(scan_query, root, index_path, "--no-heal", "rdeps", "pkg.leaf")
        assert data["index"]["degraded"] == 1
        assert any("broken.py" in entry["path"] for entry in data["index"]["degraded_files"])

    @pytest.mark.parametrize(
        ("command", "args"),
        [
            pytest.param("rdeps", ("pkg.leaf",), id="rdeps-global-in"),
            pytest.param("test-impact", ("pkg.leaf",), id="test-impact-global-in"),
            pytest.param("fn-rdeps", ("pkg.leaf::leaf_fn",), id="fn-rdeps-global-in"),
            pytest.param("mock-rdeps", ("pkg.leaf::leaf_fn",), id="mock-rdeps-global-in"),
            pytest.param("central", ("--top", "5"), id="central-whole-graph"),
        ],
    )
    def test_global_and_whole_graph_incomplete_with_degraded(self, polluted_repo, scan_query, command, args):
        """Every global-in / whole-graph query reports incomplete while a degraded file exists."""
        root, index_path = polluted_repo()
        data = _query(scan_query, root, index_path, "--no-heal", command, *args)
        assert data["index"]["query_complete"] is False


@pytest.mark.integration
class TestSelfHeal:
    """A stale index is refreshed inline before the query answers."""

    def test_new_committed_edge_surfaces_via_heal(self, polluted_repo, scan_query):
        """Committing a new importer then querying rdeps auto-heals and reflects the new edge."""
        root, index_path = polluted_repo()
        (root / "pkg" / "newcaller.py").write_text(
            "import pkg.leaf\n\n\ndef also(x):\n    return pkg.leaf.leaf_fn(x)\n"
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "add newcaller")

        data = _query(scan_query, root, index_path, "rdeps", "pkg.leaf")
        assert "pkg.newcaller" in data["imported_by"], "self-heal must surface the newly-committed edge"

    def test_no_heal_flag_keeps_stale_result(self, polluted_repo, scan_query):
        """Verify command-line option behavior.

        --no-heal answers from the stale index: the new edge stays invisible.
        """
        root, index_path = polluted_repo()
        (root / "pkg" / "newcaller.py").write_text(
            "import pkg.leaf\n\n\ndef also(x):\n    return pkg.leaf.leaf_fn(x)\n"
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "add newcaller")

        data = _query(scan_query, root, index_path, "--no-heal", "rdeps", "pkg.leaf")
        assert "pkg.newcaller" not in data["imported_by"], "stale index must not see the new edge"


@pytest.mark.integration
class TestExcludedPathStaleness:
    """Keep excluded tracked Python files from poisoning staleness or completeness.

    The excluded tracked fixture is ``vendored-lib/vendored.py``. Scan-index drops paths matched by ``.codemapignore``
    from the index ``file_shas``, and scan-query's staleness diff applies the same exclusions through
    ``bin/_exclusions.py``. The fixture therefore no longer appears as an "added" blob. A freshly scanned, unmodified
    repo reports fresh, and ``query_complete`` reaches True once no degraded file constrains the direction. This guards
    against the earlier bug where any excluded tracked Python file forced the index to remain stale.
    """

    def test_fresh_index_reports_not_stale(self, polluted_repo, scan_query):
        """A freshly scanned, unmodified repo reports a fresh (non-stale) index."""
        root, index_path = polluted_repo()
        data = _query(scan_query, root, index_path, "--no-heal", "deps", "pkg.consumer")
        assert data["index"]["stale"] is False

    def test_local_deps_on_healthy_module_complete(self, polluted_repo, scan_query):
        """Local `deps` on a cleanly-parsed module is complete despite a degraded file elsewhere."""
        root, index_path = polluted_repo()
        data = _query(scan_query, root, index_path, "--no-heal", "deps", "pkg.consumer")
        assert data["index"]["query_complete"] is True

    def test_rdeps_complete_after_broken_removed(self, polluted_repo, scan_query, scan_index):
        """Removing broken.py then rescanning flips global-in `rdeps` to complete."""
        root, index_path = polluted_repo()
        (root / "pkg" / "broken.py").unlink()
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "remove broken")
        _rescan(scan_index, root)

        data = _query(scan_query, root, index_path, "--no-heal", "rdeps", "pkg.leaf")
        assert data["index"]["degraded"] == 0
        assert data["index"]["query_complete"] is True
