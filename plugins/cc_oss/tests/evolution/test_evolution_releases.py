"""Tests for the evolution release index, target resolution, dedicated clone and open-bug series.

Every scenario builds a small deterministic git history with ``evo_repo`` (fixed dates via ``day=``). Git commands
issued by the module under test run with the fixture's isolated git configuration, and ``gh`` is replaced by a stub in
every test, so no test reads the machine's git config or touches the network.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import evolution_releases as releases_mod
import pytest
from _evolution_common import DEFAULT_BUG_LABELS, EvolutionError, sha256_text
from _evolution_repos import RepoBuilder
from evolution_releases import (
    EXIT_BLOCKED,
    build_release_index,
    fetch_open_bug_series,
    is_shallow,
    main,
    resolve_target,
    sync_clone,
)

#: Day zero of the fixture builder: ``day=N`` is this instant plus N days.
_EPOCH = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _iso(day: int) -> str:
    """Return the ISO-8601 UTC timestamp the index writes for fixture day ``day``."""
    return (_EPOCH + timedelta(days=day)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _commit(repo: RepoBuilder, name: str, day: int) -> str:
    """Add one new source file named after ``name`` and commit it at ``day``; return the SHA."""
    repo.write(f"src/{name}.py", f"{name} = {day}\n")
    return repo.commit(f"feat: {name}", day=day)


def _release(index: dict[str, Any], version: str) -> dict[str, Any]:
    """Return the release record of ``version`` from an index."""
    return next(record for record in index["releases"] if record["version"] == version)


@pytest.fixture(autouse=True)
def _isolate(evo_repo: RepoBuilder, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the module's git calls with the fixture's isolated config and make every ``gh`` call fail by default."""
    for key in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_TERMINAL_PROMPT"):
        monkeypatch.setenv(key, evo_repo.env[key])
    monkeypatch.setattr(releases_mod, "_run_gh", lambda args: None)


# --------------------------------------------------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------------------------------------------------


@pytest.fixture
def version_tags(evo_repo: RepoBuilder) -> dict[str, Any]:
    """Build a linear history tagged in many version styles plus non-version tags; return its index.

    Tags: ``v0.9.0``, ``1.0.0rc1``, SemVer ``1.0.0-rc.2``, ``1.0.0`` with ``devtools-1.0`` beside it, ``1.0.0.post1``
    with the alias ``v1.0.0.post1`` and the conflicting duplicate ``v1.0.0``, ``1.1.0.dev3``, CalVer ``2024.01.15``,
    and ``tree-snapshot`` pointing at a tree.
    """
    tags = [("v0.9.0",), ("1.0.0rc1",), ("1.0.0-rc.2",), ("1.0.0", "devtools-1.0"), ("1.0.0.post1",)]
    for day, names in enumerate(tags, start=1):
        _commit(evo_repo, f"step{day}", day)
        for name in names:
            evo_repo.tag(name, day=day)
    evo_repo.tag("v1.0.0.post1", day=5)
    evo_repo.tag("v1.0.0", day=5)
    _commit(evo_repo, "step6", 6)
    evo_repo.tag("1.1.0.dev3", day=6)
    _commit(evo_repo, "step7", 7)
    evo_repo.tag("2024.01.15", day=7)
    evo_repo.git("tag", "tree-snapshot", "HEAD^{tree}")
    return build_release_index(evo_repo.path)


@pytest.fixture
def maintenance(evo_repo: RepoBuilder) -> tuple[dict[str, str], dict[str, Any]]:
    """Build a trunk plus a 1.4.x maintenance branch with divergent chronology; return its SHAs and index.

    Trunk: ``base`` (v1.4.0), ``feature`` (v2.0.0), ``feature_fix`` (v2.1.0), then unreleased ``wip`` at HEAD.
    Maintenance branch cut at v1.4.0: ``maint_fix`` (v1.4.1), then cherry-picks of ``feature`` and ``feature_fix``
    (v1.4.2, tagged after v2.1.0) - the 2.0 feature backported into 1.4.x and fixed on both lines.
    """
    shas = {"base": _commit(evo_repo, "base", 1)}
    evo_repo.tag("v1.4.0", day=2)
    evo_repo.branch("maint-1.4")
    shas["feature"] = _commit(evo_repo, "feature", 3)
    evo_repo.tag("v2.0.0", day=4)
    evo_repo.checkout("maint-1.4")
    shas["maint_fix"] = _commit(evo_repo, "maint_fix", 5)
    evo_repo.tag("v1.4.1", day=6)
    evo_repo.checkout("main")
    evo_repo.write("src/feature.py", "feature = 'fixed'\n")
    shas["feature_fix"] = evo_repo.commit("fix: feature", day=7)
    evo_repo.tag("v2.1.0", day=8)
    evo_repo.checkout("maint-1.4")
    shas["backport_feature"] = evo_repo.cherry_pick(shas["feature"], day=9)
    shas["backport_fix"] = evo_repo.cherry_pick(shas["feature_fix"], day=10)
    evo_repo.tag("v1.4.2", day=11)
    evo_repo.checkout("main")
    shas["wip"] = _commit(evo_repo, "wip", 12)
    return shas, build_release_index(evo_repo.path)


def _merge_back(repo: RepoBuilder) -> dict[str, str]:
    """Build a maintenance branch merged back into trunk before v2.1.0; return the SHAs."""
    shas = {"base": _commit(repo, "base", 1)}
    repo.tag("v1.4.0", day=2)
    repo.branch("maint-1.4")
    shas["feature"] = _commit(repo, "feature", 3)
    repo.tag("v2.0.0", day=4)
    repo.checkout("maint-1.4")
    shas["maint_fix"] = _commit(repo, "maint_fix", 5)
    repo.tag("v1.4.1", day=6)
    repo.checkout("main")
    shas["merge"] = repo.merge("maint-1.4", "Merge maint-1.4", day=7)
    repo.tag("v2.1.0", day=8)
    return shas


def _off_branch_release(repo: RepoBuilder) -> dict[str, str]:
    """Build trunk 1.3.5 -> 1.3.7 with 1.3.6 tagged on a release branch that never merged back; return the SHAs."""
    shas = {"base": _commit(repo, "base", 1)}
    repo.tag("v1.3.5", day=1)
    shas["feature"] = _commit(repo, "feature", 2)
    repo.branch("release-1.3.6")
    repo.checkout("release-1.3.6")
    shas["bump"] = _commit(repo, "bump", 3)
    repo.tag("v1.3.6", day=3)
    repo.checkout("main")
    shas["later"] = _commit(repo, "later", 4)
    repo.tag("v1.3.7", day=5)
    return shas


def _single_release_hotfix(repo: RepoBuilder) -> None:
    """Build a one-release 1.3.x hotfix branch forked at v1.3.5 while trunk moved on to v1.4.0."""
    _commit(repo, "base", 1)
    repo.tag("v1.3.5", day=1)
    repo.branch("maint-1.3")
    _commit(repo, "feature", 2)
    repo.tag("v1.4.0", day=3)
    repo.checkout("maint-1.3")
    _commit(repo, "hotfix", 4)
    repo.tag("v1.3.6", day=5)
    repo.checkout("main")


def _maint_merges_trunk(repo: RepoBuilder) -> None:
    """Build a maintenance branch that merges trunk's v2.0.0 before tagging v1.4.2."""
    _commit(repo, "base", 1)
    repo.tag("v1.4.0", day=2)
    repo.branch("maint-1.4")
    _commit(repo, "feature", 3)
    repo.tag("v2.0.0", day=4)
    repo.checkout("maint-1.4")
    _commit(repo, "maint_fix", 5)
    repo.tag("v1.4.1", day=6)
    repo.merge("main", "Merge main into maint", day=7)
    repo.tag("v1.4.2", day=8)


# --------------------------------------------------------------------------------------------------------------------
# Release index
# --------------------------------------------------------------------------------------------------------------------


class TestVersionTags:
    """Version keys: which tags become releases, their canonical versions and pre-release flags."""

    @pytest.mark.parametrize(
        ("tag", "version", "prerelease"),
        [
            pytest.param("v0.9.0", "0.9.0", False, id="v-prefix"),
            pytest.param("1.0.0rc1", "1.0.0rc1", True, id="pep440-rc"),
            pytest.param("1.0.0-rc.2", "1.0.0rc2", True, id="semver-rc"),
            pytest.param("1.0.0", "1.0.0", False, id="final"),
            pytest.param("1.0.0.post1", "1.0.0.post1", False, id="post"),
            pytest.param("1.1.0.dev3", "1.1.0.dev3", True, id="dev"),
            pytest.param("2024.01.15", "2024.1.15", False, id="calver"),
        ],
    )
    def test_version_tag_becomes_release(
        self, version_tags: dict[str, Any], tag: str, version: str, prerelease: bool
    ) -> None:
        """A tag in PEP 440, SemVer, CalVer or v-prefixed form becomes a release with canonical version and pre flag.

        The linear fixture carries every style on its own commit; each release record keeps the tag name as given.
        """
        record = _release(version_tags, version)
        assert (record["tag"], record["is_prerelease"]) == (tag, prerelease)

    def test_unparsable_tags_are_counted(self, version_tags: dict[str, Any]) -> None:
        """Non-version tags, tags on trees and conflicting duplicate versions are listed as unparsable.

        ``devtools-1.0`` contains ``dev`` but is no version, ``tree-snapshot`` points at a tree, and ``v1.0.0`` repeats
        1.0.0 on a later commit. The same-commit alias ``v1.0.0.post1`` is folded silently.
        """
        assert version_tags["unparsable_tags"] == ["devtools-1.0", "tree-snapshot", "v1.0.0"]

    def test_one_release_per_version(self, version_tags: dict[str, Any]) -> None:
        """Each canonical version appears once, and the earliest tag of an alias pair is kept.

        ``1.0.0.post1`` and ``v1.0.0.post1`` share a commit and a day; the name tiebreak keeps ``1.0.0.post1``.
        """
        versions = [record["version"] for record in version_tags["releases"]]
        assert versions == ["0.9.0", "1.0.0rc1", "1.0.0rc2", "1.0.0", "1.0.0.post1", "1.1.0.dev3", "2024.1.15"]
        assert _release(version_tags, "1.0.0.post1")["tag"] == "1.0.0.post1"

    def test_linear_tags_form_one_line(self, version_tags: dict[str, Any]) -> None:
        """A linear history yields the single trunk line with every release in order.

        Pre-releases take part in the chronology like any other release.
        """
        assert version_tags["lines"] == {
            "main": ["0.9.0", "1.0.0rc1", "1.0.0rc2", "1.0.0", "1.0.0.post1", "1.1.0.dev3", "2024.1.15"]
        }


class TestPeeling:
    """Tags of tags are peeled through every level to their commit."""

    def test_tag_of_tag_peels_to_commit(self, evo_repo: RepoBuilder) -> None:
        """An annotated tag pointing at another annotated tag becomes a release on the underlying commit.

        ``v1.1.0`` is created on the tag object of ``v1.0.0``, two peel levels above the commit.
        """
        sha = _commit(evo_repo, "one", 1)
        evo_repo.tag("v1.0.0", day=1)
        evo_repo.git("tag", "-a", "v1.1.0", "-m", "v1.1.0", "refs/tags/v1.0.0", day=2)
        index = build_release_index(evo_repo.path)
        assert (_release(index, "1.1.0")["sha"], _release(index, "1.1.0")["date"]) == (sha, _iso(2))


class TestLinearIndex:
    """Linear history: predecessor chain, commit coverage, dates, order and fingerprint."""

    @pytest.fixture
    def linear(self, evo_repo: RepoBuilder) -> tuple[list[str], dict[str, Any]]:
        """Build three tagged commits and one unreleased commit; return the commit SHAs and index."""
        shas = [_commit(evo_repo, "one", 1)]
        evo_repo.tag("v1.0.0", day=2)
        shas.append(_commit(evo_repo, "two", 3))
        shas.append(_commit(evo_repo, "three", 4))
        evo_repo.tag("v1.1.0", day=5)
        shas.append(_commit(evo_repo, "four", 6))
        return shas, build_release_index(evo_repo.path)

    def test_predecessor_chain(self, linear: tuple[list[str], dict[str, Any]]) -> None:
        """Each release's predecessor is the previous release; the first has none.

        Two releases on a straight history form the chain 1.0.0 -> 1.1.0.
        """
        assert linear[1]["predecessor"] == {"1.0.0": None, "1.1.0": "1.0.0"}

    @pytest.mark.parametrize(
        ("position", "expected"),
        [
            pytest.param(0, {"main": "1.0.0"}, id="first-release"),
            pytest.param(1, {"main": "1.1.0"}, id="between-releases"),
            pytest.param(2, {"main": "1.1.0"}, id="tagged-commit"),
        ],
    )
    def test_commit_first_release(
        self, linear: tuple[list[str], dict[str, Any]], position: int, expected: dict
    ) -> None:
        """Every released commit maps to the first release on the line that contains it.

        Commits between v1.0.0 and v1.1.0 belong to the ``v1.1.0 ^v1.0.0`` range.
        """
        shas, index = linear
        assert index["commit_releases"][shas[position]] == expected

    def test_head_unreleased(self, linear: tuple[list[str], dict[str, Any]]) -> None:
        """Commits on HEAD after the last release are listed as unreleased and are absent from the coverage map.

        The fourth commit follows v1.1.0 and is HEAD.
        """
        shas, index = linear
        assert index["head"] == {"sha": shas[3], "unreleased": [shas[3]]}
        assert shas[3] not in index["commit_releases"]

    def test_release_record_shape(self, linear: tuple[list[str], dict[str, Any]]) -> None:
        """A release record carries tag, canonical version, peeled commit, tagger date, line, flag and order.

        v1.1.0 is an annotated tag created on day 5 on the third commit (committed on day 4).
        """
        shas, index = linear
        assert _release(index, "1.1.0") == {
            "tag": "v1.1.0",
            "version": "1.1.0",
            "sha": shas[2],
            "date": _iso(5),
            "line": "main",
            "is_prerelease": False,
            "order": 1,
        }

    def test_fingerprint_stable_until_tags_change(self, evo_repo: RepoBuilder, linear: tuple[list[str], dict]) -> None:
        """Rebuilding gives the same fingerprint; adding a tag changes it.

        The fingerprint covers the tag map and the index algorithm version.
        """
        first = linear[1]["fingerprint"]
        assert build_release_index(evo_repo.path)["fingerprint"] == first
        evo_repo.tag("v1.2.0", day=7)
        assert build_release_index(evo_repo.path)["fingerprint"] != first


class TestDatesAndOrder:
    """Release dates and the global chronological order."""

    def test_lightweight_tag_uses_commit_date(self, evo_repo: RepoBuilder) -> None:
        """A lightweight tag has no tagger date, so the release date is its commit's committer date.

        The commit is made on day 5 and tagged without an annotation two days later.
        """
        _commit(evo_repo, "one", 5)
        evo_repo.git("tag", "v1.0.0", day=7)
        assert build_release_index(evo_repo.path)["releases"][0]["date"] == _iso(5)

    def test_annotated_tag_uses_tagger_date(self, evo_repo: RepoBuilder) -> None:
        """An annotated tag's release date is its tagger date, not the commit date.

        The commit is made on day 5 and the annotated tag is created on day 7.
        """
        _commit(evo_repo, "one", 5)
        evo_repo.tag("v1.0.0", day=7)
        assert build_release_index(evo_repo.path)["releases"][0]["date"] == _iso(7)

    def test_same_date_orders_by_version_key(self, evo_repo: RepoBuilder) -> None:
        """Releases tagged at the same instant are ordered by version key, not by tag name.

        ``v1.10.0`` sorts before ``v1.9.0`` by name, but 1.9.0 is the lower version and gets order 0.
        """
        _commit(evo_repo, "one", 1)
        evo_repo.tag("v1.9.0", day=5)
        _commit(evo_repo, "two", 2)
        evo_repo.tag("v1.10.0", day=5)
        index = build_release_index(evo_repo.path)
        assert [(record["version"], record["order"]) for record in index["releases"]] == [("1.9.0", 0), ("1.10.0", 1)]


class TestMaintenanceLines:
    """Maintenance branches: release lines, predecessors and per-line commit coverage."""

    def test_lines_carry_inherited_prefix(self, maintenance: tuple[dict, dict[str, Any]]) -> None:
        """The trunk and the maintenance line each list their full chain, sharing releases up to the fork point.

        The 1.4.x branch was cut at v1.4.0, so both chains start there.
        """
        assert maintenance[1]["lines"] == {"main": ["1.4.0", "2.0.0", "2.1.0"], "1.4.x": ["1.4.0", "1.4.1", "1.4.2"]}

    @pytest.mark.parametrize(
        ("version", "line", "predecessor", "order"),
        [
            pytest.param("1.4.0", "main", None, 0, id="fork-point"),
            pytest.param("2.0.0", "main", "1.4.0", 1, id="trunk"),
            pytest.param("1.4.1", "1.4.x", "1.4.0", 2, id="maint-after-trunk-major"),
            pytest.param("2.1.0", "main", "2.0.0", 3, id="trunk-next"),
            pytest.param("1.4.2", "1.4.x", "1.4.1", 4, id="maint-latest"),
        ],
    )
    def test_release_line_and_predecessor(
        self, maintenance: tuple[dict, dict[str, Any]], version: str, line: str, predecessor: str | None, order: int
    ) -> None:
        """Each release gets its home line, its nearest ancestor release on that line, and its global order.

        Maintenance releases dated after 2.0.0 stay on 1.4.x and interleave with trunk releases in global order.
        """
        index = maintenance[1]
        record = _release(index, version)
        assert (record["line"], index["predecessor"][version], record["order"]) == (line, predecessor, order)

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            pytest.param("base", {"main": "1.4.0", "1.4.x": "1.4.0"}, id="shared-history"),
            pytest.param("feature", {"main": "2.0.0"}, id="trunk-feature"),
            pytest.param("maint_fix", {"1.4.x": "1.4.1"}, id="maint-only"),
            pytest.param("feature_fix", {"main": "2.1.0"}, id="trunk-fix"),
            pytest.param("backport_feature", {"1.4.x": "1.4.2"}, id="backported-feature"),
            pytest.param("backport_fix", {"1.4.x": "1.4.2"}, id="backported-fix"),
        ],
    )
    def test_divergent_chronology_commit_releases(
        self, maintenance: tuple[dict, dict[str, Any]], name: str, expected: dict[str, str]
    ) -> None:
        """Each commit maps to the first release containing it on every line it reaches.

        The 2.0 feature and its fix are cherry-picked into 1.4.2: the originals map to trunk releases, the copies to the
        maintenance release, and history before the fork maps to v1.4.0 on both lines.
        """
        shas, index = maintenance
        assert index["commit_releases"][shas[name]] == expected

    def test_head_unreleased_ignores_other_lines(self, maintenance: tuple[dict, dict[str, Any]]) -> None:
        """Only commits reachable from HEAD and from no release are unreleased.

        HEAD is trunk's ``wip`` commit after v2.1.0; maintenance commits are all released.
        """
        shas, index = maintenance
        assert index["head"] == {"sha": shas["wip"], "unreleased": [shas["wip"]]}

    def test_merge_back_keeps_trunk_predecessor(self, evo_repo: RepoBuilder) -> None:
        """A maintenance branch merged into trunk leaves 2.0.0 as v2.1.0's predecessor and covers the fix on both lines.

        v2.1.0 contains v2.0.0 and v1.4.1; the higher version below 2.1.0 wins, and the merged maintenance fix is first
        released on trunk by 2.1.0.
        """
        shas = _merge_back(evo_repo)
        index = build_release_index(evo_repo.path)
        assert index["predecessor"]["2.1.0"] == "2.0.0"
        assert index["commit_releases"][shas["maint_fix"]] == {"1.4.x": "1.4.1", "main": "2.1.0"}

    def test_maint_merging_trunk_stays_on_its_line(self, evo_repo: RepoBuilder) -> None:
        """A maintenance branch that merges a newer trunk release keeps its own predecessor and line.

        v1.4.2 contains v2.0.0 through the merge, but 2.0.0 is above 1.4.2, so 1.4.1 stays its predecessor.
        """
        _maint_merges_trunk(evo_repo)
        index = build_release_index(evo_repo.path)
        assert (index["predecessor"]["1.4.2"], _release(index, "1.4.2")["line"]) == ("1.4.1", "1.4.x")
        assert index["lines"] == {"main": ["1.4.0", "2.0.0"], "1.4.x": ["1.4.0", "1.4.1", "1.4.2"]}


class TestOffBranchReleases:
    """Releases tagged off trunk on dead-end branches join the trunk instead of forming their own lines."""

    @pytest.fixture
    def off_branch(self, evo_repo: RepoBuilder) -> tuple[dict[str, str], dict[str, Any]]:
        """Build the squash-merged release-branch history; return its SHAs and index."""
        shas = _off_branch_release(evo_repo)
        return shas, build_release_index(evo_repo.path)

    def test_off_branch_release_joins_trunk(self, off_branch: tuple[dict, dict[str, Any]]) -> None:
        """A release tagged on an unmerged release branch sits on the trunk between its neighbours.

        v1.3.6 is in trunk's 1.3 family and below trunk's next release v1.3.7, so it is not a maintenance line; its
        ancestry predecessor stays v1.3.5, as does v1.3.7's.
        """
        index = off_branch[1]
        assert index["lines"] == {"main": ["1.3.5", "1.3.6", "1.3.7"]}
        assert (index["predecessor"]["1.3.6"], index["predecessor"]["1.3.7"]) == ("1.3.5", "1.3.5")

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            pytest.param("feature", "1.3.6", id="shipped-first-off-branch"),
            pytest.param("bump", "1.3.6", id="branch-only-commit"),
            pytest.param("later", "1.3.7", id="trunk-after-branch"),
        ],
    )
    def test_off_branch_commit_coverage(
        self, off_branch: tuple[dict, dict[str, Any]], name: str, expected: str
    ) -> None:
        """Commits map to the first trunk-chain release containing them, including the off-branch release.

        ``feature`` was on trunk when the release branch was cut, so v1.3.6 shipped it first.
        """
        shas, index = off_branch
        assert index["commit_releases"][shas[name]] == {"main": expected}

    def test_branch_prefix_includes_earlier_off_branch_release(self, evo_repo: RepoBuilder) -> None:
        """A maintenance line forked after an off-branch release inherits the trunk chain including that release.

        v1.3.6 folds into trunk; the 1.3.x branch is cut at v1.3.7 and releases v1.3.8 while trunk moves to v1.4.0.
        """
        _off_branch_release(evo_repo)
        evo_repo.branch("maint-1.3")
        _commit(evo_repo, "next", 6)
        evo_repo.tag("v1.4.0", day=7)
        evo_repo.checkout("maint-1.3")
        _commit(evo_repo, "hotfix", 8)
        evo_repo.tag("v1.3.8", day=9)
        assert build_release_index(evo_repo.path)["lines"] == {
            "main": ["1.3.5", "1.3.6", "1.3.7", "1.4.0"],
            "1.3.x": ["1.3.5", "1.3.6", "1.3.7", "1.3.8"],
        }

    def test_single_release_hotfix_keeps_its_line(self, evo_repo: RepoBuilder) -> None:
        """A one-release hotfix branch in an older family than trunk's next release stays a line of its own.

        v1.3.6 forks at v1.3.5 while trunk released v1.4.0, so it is maintenance, not an off-branch trunk release.
        """
        _single_release_hotfix(evo_repo)
        index = build_release_index(evo_repo.path)
        assert index["lines"] == {"main": ["1.3.5", "1.4.0"], "1.3.x": ["1.3.5", "1.3.6"]}


class TestEmptyRepositories:
    """Repositories without releases."""

    def test_no_tags_lists_all_head_commits(self, evo_repo: RepoBuilder) -> None:
        """Without tags the index has no releases or lines and every HEAD commit is unreleased.

        Two commits, no tags: the orchestrator stops on the empty release list.
        """
        first = _commit(evo_repo, "one", 1)
        second = _commit(evo_repo, "two", 2)
        index = build_release_index(evo_repo.path)
        assert (index["releases"], index["lines"], index["commit_releases"]) == ([], {}, {})
        assert index["head"] == {"sha": second, "unreleased": [second, first]}

    def test_unborn_head(self, evo_repo: RepoBuilder) -> None:
        """An empty repository yields an empty index with a null HEAD.

        ``git init`` without commits has no HEAD commit and no tags.
        """
        assert build_release_index(evo_repo.path)["head"] == {"sha": None, "unreleased": []}


# --------------------------------------------------------------------------------------------------------------------
# Shallow detection and the dedicated clone
# --------------------------------------------------------------------------------------------------------------------


class TestShallow:
    """Shallow-clone detection."""

    def test_depth_one_clone_is_shallow(self, evo_repo: RepoBuilder, tmp_path: Path) -> None:
        """A ``--depth 1`` clone over a file URL is shallow; its full source is not.

        The index of a shallow clone would be truncated, so the CLI blocks on it.
        """
        _commit(evo_repo, "one", 1)
        _commit(evo_repo, "two", 2)
        dest = tmp_path / "shallow"
        evo_repo.git("clone", "-q", "--depth", "1", evo_repo.path.as_uri(), str(dest))
        assert (is_shallow(dest), is_shallow(evo_repo.path)) == (True, False)


def _tags(path: Path, builder: RepoBuilder) -> dict[str, str]:
    """Return ``{tag: peeled commit}`` for a repository, read through the fixture's isolated git."""
    out = builder.git("-C", str(path), "for-each-ref", "--format=%(refname:strip=2) %(*objectname)", "refs/tags")
    return dict(line.split(" ", 1) for line in out.splitlines())


def _ssh_url(path: Path) -> str:
    """Return a fixture SSH URL served by the local upload-pack command."""
    return "ssh://fixture" + path.as_uri().removeprefix("file://")


class TestSyncClone:
    """Dedicated clone creation and refresh."""

    @pytest.fixture
    def source(self, evo_repo: RepoBuilder, monkeypatch: pytest.MonkeyPatch) -> RepoBuilder:
        """Build a source repository with two tags, one on a commit no branch reaches."""
        transport = Path(__file__).resolve().parents[1] / "_evolution_ssh_transport.py"
        monkeypatch.setenv("GIT_SSH_COMMAND", f'"{sys.executable}" "{transport}"')
        monkeypatch.setenv("GIT_SSH_VARIANT", "ssh")
        _commit(evo_repo, "one", 1)
        evo_repo.tag("v1.0.0", day=1)
        evo_repo.branch("side")
        evo_repo.checkout("side")
        _commit(evo_repo, "side", 2)
        evo_repo.tag("v0.5.0", day=2)
        evo_repo.checkout("main")
        evo_repo.git("branch", "-D", "side")
        return evo_repo

    def test_fresh_clone_has_every_tag(self, source: RepoBuilder, tmp_path: Path) -> None:
        """A new dedicated clone holds every source tag, including one on unreachable history, and is not shallow.

        ``v0.5.0`` sits on a commit whose branch was deleted.
        """
        clone = tmp_path / "store" / "repo"
        assert sync_clone(clone, _ssh_url(source.path)) == []
        assert (_tags(clone, source), is_shallow(clone)) == (_tags(source.path, source), False)

    def test_refresh_follows_moved_new_and_deleted_tags(self, source: RepoBuilder, tmp_path: Path) -> None:
        """A rerun fetches new tags, force-updates moved tags, prunes deleted tags and moves HEAD to origin's tip.

        After the first sync the source adds ``v1.1.0``, moves ``v1.0.0`` and deletes ``v0.5.0``.
        """
        clone = tmp_path / "store" / "repo"
        sync_clone(clone, _ssh_url(source.path))
        tip = _commit(source, "two", 3)
        source.tag("v1.1.0", day=3)
        source.git("tag", "-f", "-a", "v1.0.0", "-m", "moved", day=4)
        source.git("tag", "-d", "v0.5.0")
        assert sync_clone(clone, _ssh_url(source.path)) == []
        assert _tags(clone, source) == _tags(source.path, source)
        assert source.git("-C", str(clone), "rev-parse", "HEAD").strip() == tip

    def test_upstream_tags_land_in_dedicated_clone_only(self, source: RepoBuilder, tmp_path: Path) -> None:
        """A fork's parent tags are fetched into the dedicated clone; the source checkout gains no remote.

        The upstream repository carries ``v9.0.0`` that the fork lacks.
        """
        upstream = RepoBuilder.create(tmp_path / "upstream", tmp_path / "upcfg")
        _commit(upstream, "up", 1)
        upstream.tag("v9.0.0", day=1)
        clone = tmp_path / "store" / "repo"
        assert sync_clone(clone, _ssh_url(source.path), _ssh_url(upstream.path)) == []
        assert "v9.0.0" in _tags(clone, source)
        assert (source.git("remote").split(), source.git("-C", str(clone), "remote").split()) == (
            [],
            ["origin", "upstream"],
        )

    def test_occupied_directory_is_refused(self, source: RepoBuilder, tmp_path: Path) -> None:
        """A non-empty directory that is not a clone is never overwritten.

        The store's ``repo/`` holds a stray file.
        """
        clone = tmp_path / "occupied"
        clone.mkdir()
        (clone / "stray.txt").write_text("x", encoding="utf-8")
        with pytest.raises(EvolutionError, match="not a git clone"):
            sync_clone(clone, source.path.as_uri())

    def test_option_like_url_is_refused(self, tmp_path: Path) -> None:
        """A URL starting with ``-`` is rejected before git runs, so it can never be read as an option.

        ``--upload-pack=...`` would otherwise reach ``git clone``.
        """
        with pytest.raises(ValueError, match="must not start"):
            sync_clone(tmp_path / "repo", "--upload-pack=touch")


# --------------------------------------------------------------------------------------------------------------------
# Target resolution
# --------------------------------------------------------------------------------------------------------------------


def _fork_gh(parent: dict[str, Any] | None) -> Any:
    """Return a ``_run_gh`` stub answering ``gh repo view --json parent`` with ``parent``."""
    return lambda args: json.dumps({"parent": parent})


class TestResolveTarget:
    """Target resolution: identity of the checkout or of a remote target, and fork upstream lookup."""

    def test_local_repo_without_remote(self, evo_repo: RepoBuilder) -> None:
        """A checkout without ``origin`` resolves to a ``local/<hash>`` identity of its absolute path.

        The scan runs read-only in the checkout itself.
        """
        _commit(evo_repo, "one", 1)
        top = evo_repo.path.resolve()
        assert resolve_target(None, evo_repo.path) == {
            "parts": ["local", sha256_text(top.as_posix())[:12]],
            "checkout": str(top),
            "dedicated": False,
            "clone_url": None,
            "upstream_url": None,
            "warnings": [],
        }

    def test_subdirectory_resolves_to_top_level(self, evo_repo: RepoBuilder) -> None:
        """Starting from a subdirectory resolves to the work-tree root.

        The scan is started from ``src/``.
        """
        _commit(evo_repo, "one", 1)
        assert resolve_target(None, evo_repo.path / "src")["checkout"] == str(evo_repo.path.resolve())

    def test_non_github_origin_skips_gh(self, evo_repo: RepoBuilder, monkeypatch: pytest.MonkeyPatch) -> None:
        """A non-GitHub ``origin`` gives host/owner/repo parts and never calls gh.

        The gh stub fails the test if reached.
        """
        evo_repo.git("remote", "add", "origin", "https://example.invalid/acme/widget.git")
        monkeypatch.setattr(releases_mod, "_run_gh", pytest.fail)
        result = resolve_target(None, evo_repo.path)
        assert (result["parts"], result["upstream_url"], result["warnings"]) == (
            ["example.invalid", "acme", "widget"],
            None,
            [],
        )

    def test_github_fork_resolves_parent(self, evo_repo: RepoBuilder, monkeypatch: pytest.MonkeyPatch) -> None:
        """A GitHub fork's parent becomes the upstream URL, and the read-only checkout is warned about.

        gh reports ``upstream-org/widget`` as the parent of ``acme/widget``.
        """
        evo_repo.git("remote", "add", "origin", "git@github.com:acme/widget.git")
        monkeypatch.setattr(releases_mod, "_run_gh", _fork_gh({"name": "widget", "owner": {"login": "upstream-org"}}))
        result = resolve_target(None, evo_repo.path)
        assert result["upstream_url"] == "https://github.com/upstream-org/widget.git"
        assert result["warnings"] == ["target is a fork; upstream tags are not fetched into the read-only checkout"]

    def test_gh_failure_degrades_to_warning(self, evo_repo: RepoBuilder) -> None:
        """Without a working gh the fork upstream is unknown and a warning says so.

        The autouse stub makes every gh call fail.
        """
        evo_repo.git("remote", "add", "origin", "https://github.com/acme/widget.git")
        result = resolve_target(None, evo_repo.path)
        assert (result["upstream_url"], result["warnings"]) == (
            None,
            ["gh unavailable or failed; fork upstream not resolved"],
        )

    def test_shorthand_matching_checkout_uses_checkout(self, evo_repo: RepoBuilder) -> None:
        """A shorthand naming the checkout's own repository (any case) reuses the checkout and its parts.

        ``ACME/Widget`` matches ``origin`` ``github.com/acme/widget``.
        """
        evo_repo.git("remote", "add", "origin", "https://github.com/acme/widget.git")
        result = resolve_target("ACME/Widget", evo_repo.path)
        assert (result["dedicated"], result["parts"], result["clone_url"]) == (
            False,
            ["github.com", "acme", "widget"],
            None,
        )

    def test_other_repository_needs_dedicated_clone(self, evo_repo: RepoBuilder) -> None:
        """A shorthand naming another repository resolves to a dedicated clone of its GitHub URL.

        The checkout's ``origin`` is ``acme/widget``; the target is ``octo/gadget``.
        """
        evo_repo.git("remote", "add", "origin", "https://github.com/acme/widget.git")
        result = resolve_target("octo/gadget", evo_repo.path)
        assert (result["dedicated"], result["checkout"], result["clone_url"], result["parts"]) == (
            True,
            None,
            "https://github.com/octo/gadget.git",
            ["github.com", "octo", "gadget"],
        )

    def test_url_target_outside_any_repository(self, tmp_path: Path) -> None:
        """A URL target works from a directory that is not a git work tree.

        The scan starts from a plain directory and the target is an SSH URL on GitLab.
        """
        plain = tmp_path / "plain"
        plain.mkdir()
        result = resolve_target("git@gitlab.com:group/sub/proj.git", plain)
        assert (result["parts"], result["dedicated"]) == (["gitlab.com", "group", "sub", "proj"], True)

    @pytest.mark.parametrize(
        "target",
        [
            pytest.param("not a url", id="free-text"),
            pytest.param("-oProxy/repo", id="option-like"),
            pytest.param("../repo", id="dot-dot"),
            pytest.param("a/b/c", id="path"),
        ],
    )
    def test_invalid_target_is_rejected(self, evo_repo: RepoBuilder, target: str) -> None:
        """Targets that are neither OWNER/REPO nor a remote URL raise ValueError.

        Option-like and ``..`` components never reach git or the store path.
        """
        with pytest.raises(ValueError, match="neither OWNER/REPO"):
            resolve_target(target, evo_repo.path)

    def test_no_target_outside_repository(self, tmp_path: Path) -> None:
        """Without a target, a directory outside any git work tree is an error naming the fix.

        The scan starts from a plain directory.
        """
        plain = tmp_path / "plain"
        plain.mkdir()
        with pytest.raises(EvolutionError, match="pass a target"):
            resolve_target(None, plain)


# --------------------------------------------------------------------------------------------------------------------
# Open-bug series
# --------------------------------------------------------------------------------------------------------------------


def _event(kind: str, day: int) -> dict[str, str]:
    """Return one timeline node of type ``kind`` at fixture day ``day``."""
    return {"__typename": kind, "createdAt": _iso(day)}


def _issue(number: int, created: int, events: list[dict], closed: int | None = None, more: str = "") -> dict:
    """Return one issue node; ``more`` is the timeline end cursor when another timeline page exists."""
    return {
        "number": number,
        "createdAt": _iso(created),
        "closedAt": None if closed is None else _iso(closed),
        "timelineItems": {"pageInfo": {"hasNextPage": bool(more), "endCursor": more or None}, "nodes": events},
    }


def _page(nodes: list[dict], cursor: str = "") -> dict[str, Any]:
    """Return one connection page; a non-empty ``cursor`` marks a following page."""
    return {"pageInfo": {"hasNextPage": bool(cursor), "endCursor": cursor or None}, "nodes": nodes}


#: GraphQL responses keyed by (connection, label or issue number, cursor).
_PAGES: dict[tuple[str, Any, str | None], dict[str, Any]] = {
    ("labels", None, None): {"labels": _page([{"name": "Bug"}, {"name": "enhancement"}], "L1")},
    ("labels", None, "L1"): {"labels": _page([{"name": "Type: Bug"}])},
    ("issues", "Bug", None): {
        "issues": _page(
            [
                _issue(1, 1, [_event("ClosedEvent", 5)], closed=5),
                _issue(2, 2, [_event("ClosedEvent", 3), _event("ReopenedEvent", 6)]),
            ],
            "I1",
        )
    },
    ("issues", "Bug", "I1"): {"issues": _page([_issue(3, 10, [])])},
    ("issues", "Type: Bug", None): {
        "issues": _page(
            [
                _issue(2, 2, [_event("ClosedEvent", 3), _event("ReopenedEvent", 6)]),
                _issue(4, 1, [], closed=4),
                _issue(5, 2, [_event("ClosedEvent", 3)], closed=7, more="T1"),
            ]
        )
    },
    ("issue", 5, "T1"): {"issue": {"timelineItems": _page([_event("ReopenedEvent", 5), _event("ClosedEvent", 7)])}},
}


def _fake_graphql(missing: tuple[str, Any, str | None] | None = None) -> Any:
    """Return a ``_gh_graphql`` stub serving ``_PAGES``; the ``missing`` page fails like a gh error."""

    def answer(query: str, variables: dict[str, Any]) -> dict[str, Any] | None:
        """Look up the page for one query; None when it is the failing page."""
        kind = "labels" if "labels(first" in query else ("issue" if "issue(number" in query else "issues")
        key = (kind, variables.get("label", variables.get("number")), variables.get("cursor"))
        return None if key == missing else {"repository": _PAGES[key]}

    return answer


_RELEASES = [{"version": f"1.{day}.0", "date": _iso(day)} for day in (0, 4, 6, 8, 12)]


class TestOpenBugSeries:
    """Open-bug counts per release from a monkeypatched GraphQL transport."""

    def test_series_replays_close_and_reopen(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Open counts follow creation, close, reopen and closedAt fallback across every paginated page.

        Labels and issues each span two pages, issue 2 carries both bug labels and is reopened on day 6, issue 4 has
        only ``closedAt``, and issue 5 needs a second timeline page to see its reopen and final close.
        """
        monkeypatch.setattr(releases_mod, "_gh_graphql", _fake_graphql())
        result = fetch_open_bug_series("acme", "widget", _RELEASES, DEFAULT_BUG_LABELS)
        assert [point["open"] for point in result["series"]] == [0, 1, 2, 1, 2]
        assert (result["labels"], result["issue_count"], result["approximate"]) == (["Bug", "Type: Bug"], 5, True)

    @pytest.mark.parametrize(
        "missing",
        [
            pytest.param(("labels", None, "L1"), id="label-page"),
            pytest.param(("issues", "Bug", "I1"), id="issue-page"),
            pytest.param(("issue", 5, "T1"), id="timeline-page"),
        ],
    )
    def test_any_failed_page_returns_none(self, monkeypatch: pytest.MonkeyPatch, missing: tuple) -> None:
        """A failure on any page drops the whole series instead of reporting a partial count.

        Each case fails a different second page.
        """
        monkeypatch.setattr(releases_mod, "_gh_graphql", _fake_graphql(missing))
        assert fetch_open_bug_series("acme", "widget", _RELEASES, DEFAULT_BUG_LABELS) is None

    def test_no_matching_label_gives_zero_series(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When no repository label matches the set, every count is zero and a warning says why.

        The requested label ``defect`` does not exist in the repository.
        """
        monkeypatch.setattr(releases_mod, "_gh_graphql", _fake_graphql())
        result = fetch_open_bug_series("acme", "widget", _RELEASES[:2], ["defect"])
        assert ([point["open"] for point in result["series"]], result["labels"]) == ([0, 0], [])
        assert result["warnings"] == ["no repository label matches the bug label set; the series is all zeros"]

    def test_missing_gh_returns_none(self) -> None:
        """Without gh the series is unavailable (None), never an exception.

        The autouse stub makes the transport fail on the first request.
        """
        assert fetch_open_bug_series("acme", "widget", _RELEASES, DEFAULT_BUG_LABELS) is None

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            pytest.param({"data": {"repository": {}}}, {"repository": {}}, id="data"),
            pytest.param({"data": None, "errors": [{"type": "NOT_FOUND"}]}, None, id="graphql-error"),
            pytest.param("not json", None, id="garbage"),
        ],
    )
    def test_graphql_transport(self, monkeypatch: pytest.MonkeyPatch, body: Any, expected: Any) -> None:
        """The transport types integer variables, omits null ones, and returns data only for an error-free body.

        The ``_run_gh`` stub records its arguments and answers with the given body.
        """
        calls: list[list[str]] = []
        text = body if isinstance(body, str) else json.dumps(body)
        monkeypatch.setattr(releases_mod, "_run_gh", lambda args: calls.append(args) or text)
        assert releases_mod._gh_graphql("q", {"label": "Bug", "number": 5, "cursor": None}) == expected
        assert calls == [["api", "graphql", "-f", "query=q", "-f", "label=Bug", "-F", "number=5"]]


# --------------------------------------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------------------------------------


class TestCli:
    """The ``index`` and ``target`` subcommands."""

    def test_index_writes_json(self, evo_repo: RepoBuilder, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """``index`` writes the index file and prints a one-line summary.

        One tagged commit gives one release on the trunk.
        """
        _commit(evo_repo, "one", 1)
        evo_repo.tag("v1.0.0", day=1)
        out = tmp_path / "out" / "index.json"
        assert main(["index", "--repo", str(evo_repo.path), "--out", str(out)]) == 0
        assert json.loads(out.read_text(encoding="utf-8"))["lines"] == {"main": ["1.0.0"]}
        assert capsys.readouterr().out.startswith("index: 1 releases, 1 lines, 0 unparsable tags")

    def test_index_blocks_without_releases(self, evo_repo: RepoBuilder, tmp_path: Path) -> None:
        """``index`` exits 3 when no tag parses as a version, after writing the (empty) index.

        The only tag is ``latest``.
        """
        _commit(evo_repo, "one", 1)
        evo_repo.tag("latest", day=1)
        out = tmp_path / "index.json"
        assert main(["index", "--repo", str(evo_repo.path), "--out", str(out)]) == EXIT_BLOCKED
        assert json.loads(out.read_text(encoding="utf-8"))["unparsable_tags"] == ["latest"]

    def test_index_blocks_shallow_clone(self, evo_repo: RepoBuilder, tmp_path: Path) -> None:
        """``index`` exits 3 on a shallow clone and writes nothing.

        The clone is made with ``--depth 1``.
        """
        _commit(evo_repo, "one", 1)
        evo_repo.tag("v1.0.0", day=1)
        dest = tmp_path / "shallow"
        evo_repo.git("clone", "-q", "--depth", "1", evo_repo.path.as_uri(), str(dest))
        out = tmp_path / "index.json"
        assert main(["index", "--repo", str(dest), "--out", str(out)]) == EXIT_BLOCKED
        assert not out.exists()

    def test_target_prints_json(
        self, evo_repo: RepoBuilder, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``target`` prints the resolved target of the current directory as one JSON line.

        The checkout's ``origin`` is on a non-GitHub host, so gh is not involved.
        """
        evo_repo.git("remote", "add", "origin", "https://example.invalid/acme/widget.git")
        monkeypatch.chdir(evo_repo.path)
        assert main(["target"]) == 0
        assert json.loads(capsys.readouterr().out)["parts"] == ["example.invalid", "acme", "widget"]

    def test_target_rejects_invalid(self, evo_repo: RepoBuilder, capsys: pytest.CaptureFixture[str]) -> None:
        """An invalid ``--target`` exits 1 with the reason on stderr.

        ``not a url`` is neither form.
        """
        assert main(["target", "--target", "not a url"]) == 1
        assert "neither OWNER/REPO" in capsys.readouterr().err
