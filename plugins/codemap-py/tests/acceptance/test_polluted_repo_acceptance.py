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

Every test here drives the real ``scan-index`` / ``scan-query`` scripts and a real git repo: the subprocess round trip
is the contract, so nothing is mocked.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from _scan_helpers import run_git, run_query

_NEWCALLER_SRC = "import pkg.leaf\n\n\ndef also(x):\n    return pkg.leaf.leaf_fn(x)\n"


def _rescan(scan_index: Path, root: Path) -> None:
    """Run a full scan-index over *root*, asserting success."""
    result = subprocess.run(
        [sys.executable, str(scan_index), "--root", str(root)],
        capture_output=True,
        text=True,
        cwd=str(root),
    )
    assert result.returncode == 0, result.stderr


def _load_index(index_path: Path) -> dict:
    """Return the parsed index JSON stored at *index_path*."""
    return json.loads(index_path.read_text())


def _module_paths(index: dict) -> set[str]:
    """Return the set of module paths present in an index."""
    return {m["path"] for m in index.get("modules", [])}


def _collision_paths(index: dict) -> set[str]:
    """Return every path named as kept or dropped by any collision record in an index."""
    return {p for c in index["collisions"] for p in (c["kept"], *c["dropped"])}


def _commit_all(root: Path, message: str) -> None:
    """Stage everything under *root* and commit it."""
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", message)


def _commit_newcaller(root: Path) -> None:
    """Commit a new ``pkg/newcaller.py`` that imports ``pkg.leaf``, leaving the index stale."""
    (root / "pkg" / "newcaller.py").write_text(_NEWCALLER_SRC)
    _commit_all(root, "add newcaller")


def _winners_over_rescans(scan_index: Path, root: Path, index_path: Path, rounds: int) -> set[str]:
    """Rescan *root* *rounds* times and return the distinct kept paths recorded for ``pkg.leaf``."""
    winners = set()
    for _ in range(rounds):
        _rescan(scan_index, root)
        collision = next(c for c in _load_index(index_path)["collisions"] if c["name"] == "pkg.leaf")
        winners.add(collision["kept"])
    return winners


@pytest.mark.integration
class TestExclusions:
    """The ghost worktree and vendored dir never enter the module index."""

    @pytest.mark.parametrize("excluded_prefix", [".claude/", "vendored-lib/"])
    def test_no_modules_under_excluded_roots(self, polluted_repo, excluded_prefix):
        """A fresh scan indexes zero modules under the built-in ``.claude/`` or the ``.codemapignore``d dir.

        The tree holds a ghost worktree copy and a vendored copy; both must be pruned before indexing so they cannot
        shadow or duplicate the real package.
        """
        _root, index_path = polluted_repo()
        paths = _module_paths(_load_index(index_path))
        assert not any(p.startswith(excluded_prefix) for p in paths), paths

    def test_healthy_pkg_modules_survive(self, polluted_repo):
        """Real package modules are still indexed after exclusions are applied.

        Guards against over-eager pruning: the collision-free tree must still index ``pkg/leaf.py`` and
        ``pkg/consumer.py`` next to the excluded copies.
        """
        _root, index_path = polluted_repo()
        assert {"pkg/leaf.py", "pkg/consumer.py"} <= _module_paths(_load_index(index_path))

    def test_codemapignore_entry_in_excluded_roots_meta(self, polluted_repo):
        """A .codemapignore dir appears in `excluded_roots` with source and a non-zero count.

        ``vendored-lib`` holds one tracked ``.py``, so its metadata record must name the source file and count it.
        """
        _root, index_path = polluted_repo()
        index = _load_index(index_path)
        record = next((r for r in index["excluded_roots"] if r["pattern"] == "vendored-lib"), None)
        assert record is not None, index["excluded_roots"]
        assert record["source"] == ".codemapignore"
        assert record["count"] >= 1, "vendored dir held a .py file that must be counted"

    def test_builtin_skip_dir_stays_implicit(self, polluted_repo):
        """The built-in .claude SKIP_DIR is pruned but deliberately omitted from meta.

        Only user-configured exclusions are reported in ``excluded_roots``; the always-on ``.claude`` prune must not
        leak into that list.
        """
        _root, index_path = polluted_repo()
        patterns = {r["pattern"] for r in _load_index(index_path)["excluded_roots"]}
        assert ".claude" not in patterns


@pytest.mark.integration
class TestDedupDeterminism:
    """The non-excluded duplicate tree collides deterministically."""

    def test_collision_recorded_for_shared_qualname(self, polluted_repo):
        """pkg/leaf.py and wt/pkg/leaf.py share dotted name pkg.leaf → one collision record.

        The ``wt/`` copy is not excluded, so both files map to ``pkg.leaf`` and the index must record which one was kept
        and which dropped.
        """
        _root, index_path = polluted_repo(with_collision=True)
        index = _load_index(index_path)
        collision = next((c for c in index["collisions"] if c["name"] == "pkg.leaf"), None)
        assert collision is not None, index["collisions"]
        assert set(collision["dropped"]) | {collision["kept"]} == {"pkg/leaf.py", "wt/pkg/leaf.py"}

    def test_no_collision_in_collision_free_variant(self, polluted_repo):
        """Without the wt/ copy the index records no collisions.

        Pruned ghost and vendored copies must not leave collision records behind.
        """
        _root, index_path = polluted_repo()
        assert _load_index(index_path)["collisions"] == []

    def test_ghost_copy_never_collides(self, polluted_repo):
        """The excluded .claude ghost copy is pruned before dedup, so it is in no collision record.

        Even with a genuine ``wt/`` collision present, the ghost tree under ``.claude/`` must not appear as a kept or
        dropped path.
        """
        _root, index_path = polluted_repo(with_collision=True)
        involved = _collision_paths(_load_index(index_path))
        assert not any(p.startswith(".claude/") for p in involved), involved

    def test_winner_stable_across_repeated_scans(self, polluted_repo, scan_index):
        """The dedup winner for pkg.leaf is identical across repeated full scans.

        Three real rescans of the colliding tree must keep the same path each time; a filesystem-order-dependent winner
        would flip between runs.
        """
        root, index_path = polluted_repo(with_collision=True)
        winners = _winners_over_rescans(scan_index, root, index_path, rounds=3)
        assert len(winners) == 1, f"dedup winner must be deterministic, saw {winners}"


@pytest.mark.integration
class TestDirectionScopedIncompleteness:
    """A degraded file forces global-in / whole-graph queries to report incomplete."""

    def test_degraded_file_registered(self, polluted_repo, scan_query):
        """broken.py registers as the sole degraded module, surfaced in degraded_files.

        The syntax-error module must be counted once and named in the query's index metadata.
        """
        root, index_path = polluted_repo()
        data = run_query(scan_query, root, index_path, "--no-heal", "rdeps", "pkg.leaf")
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
        """Every global-in / whole-graph query reports incomplete while a degraded file exists.

        An unparsed file may hold importers the index cannot see, so answers that span the whole graph must not claim
        completeness.
        """
        root, index_path = polluted_repo()
        data = run_query(scan_query, root, index_path, "--no-heal", command, *args)
        assert data["index"]["query_complete"] is False


@pytest.mark.integration
class TestSelfHeal:
    """A stale index is refreshed inline before the query answers."""

    @pytest.mark.parametrize(
        ("flags", "sees_new_edge"),
        [
            pytest.param((), True, id="heal-surfaces-new-edge"),
            pytest.param(("--no-heal",), False, id="no-heal-keeps-stale-result"),
        ],
    )
    def test_committed_edge_visibility(self, polluted_repo, scan_query, flags, sees_new_edge):
        """A newly committed importer is visible to `rdeps` only when scan-query may self-heal.

        The index is built, then a new importer is committed. Default queries must refresh inline and show the edge;
        `--no-heal` must answer from the stale index and leave it invisible.
        """
        root, index_path = polluted_repo()
        _commit_newcaller(root)
        data = run_query(scan_query, root, index_path, *flags, "rdeps", "pkg.leaf")
        assert ("pkg.newcaller" in data["imported_by"]) is sees_new_edge


@pytest.mark.integration
class TestExcludedPathStaleness:
    """Keep excluded tracked Python files from poisoning staleness or completeness.

    The excluded tracked fixture is ``vendored-lib/vendored.py``. Scan-index drops paths matched by ``.codemapignore``
    from the index ``file_shas``, and scan-query's staleness diff applies the same exclusions through
    ``bin/_exclusions.py``. The fixture therefore no longer appears as an "added" blob. A freshly scanned, unmodified
    repo reports fresh, and ``query_complete`` reaches True once no degraded file constrains the direction. This guards
    against the earlier bug where any excluded tracked Python file forced the index to remain stale.
    """

    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            pytest.param("stale", False, id="fresh-index-not-stale"),
            pytest.param("query_complete", True, id="local-deps-on-healthy-module-complete"),
        ],
    )
    def test_local_deps_on_fresh_index(self, polluted_repo, scan_query, key, expected):
        """A local `deps` query on a freshly scanned repo is fresh and complete despite a degraded file elsewhere.

        The tracked-but-excluded vendored file must not count as an added blob, and `pkg.consumer` parses cleanly, so
        neither staleness nor the degraded `pkg.broken` may taint the answer.
        """
        root, index_path = polluted_repo()
        data = run_query(scan_query, root, index_path, "--no-heal", "deps", "pkg.consumer")
        assert data["index"][key] is expected

    def test_rdeps_complete_after_broken_removed(self, polluted_repo, scan_query, scan_index):
        """Removing broken.py then rescanning flips global-in `rdeps` to complete.

        With the only degraded module gone and the index rebuilt, nothing constrains the global-in direction anymore.
        """
        root, index_path = polluted_repo()
        (root / "pkg" / "broken.py").unlink()
        _commit_all(root, "remove broken")
        _rescan(scan_index, root)

        data = run_query(scan_query, root, index_path, "--no-heal", "rdeps", "pkg.leaf")
        assert data["index"]["degraded"] == 0
        assert data["index"]["query_complete"] is True
