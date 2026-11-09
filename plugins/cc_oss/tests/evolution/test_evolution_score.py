"""Tests for evolution matrix scoring: soft/binary views, pseudo-columns, inheritance, precedence and aggregates.

Every scenario is a hand-built release index, entry list and evidence map, so no git repository is needed. Expected
associations are written in each test before scoring runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from evolution_score import (
    DEFAULT_THRESHOLD,
    aggregate,
    build_columns,
    build_sha_map,
    main,
    pseudo_columns,
    score_rows,
)

_VERSIONS = ["1.0", "1.1", "1.2", "1.3", "1.4"]


def _index(versions: list[str], commits: dict[str, str], line: str = "1", unreleased: tuple[str, ...] = ()) -> dict:
    """Return a single-line release index with each commit first released in the given version."""
    releases = [
        {"tag": f"v{v}", "version": v, "sha": f"t{i}", "date": "", "line": line, "is_prerelease": False, "order": i}
        for i, v in enumerate(versions)
    ]
    return {
        "schema_version": 1,
        "releases": releases,
        "unparsable_tags": [],
        "lines": {line: list(versions)},
        "predecessor": {},
        "commit_releases": {sha: {line: version} for sha, version in commits.items()},
        "head": {"sha": "head", "unreleased": list(unreleased)},
        "fingerprint": "index-fp",
    }


def _column(entry_id: str, commits: list[str], kind: str = "feature", line: str = "1", **extra: Any) -> dict:
    """Return a feature/change entry released in the version its ID starts with."""
    return {
        "entry_id": entry_id,
        "kind": kind,
        "release": entry_id.split("/")[0],
        "line": line,
        "text": f"Add {entry_id}",
        "source": "changelog",
        "commits": commits,
        "link_method": "sha",
        "link_confidence": 1.0,
        **extra,
    }


def _fix(entry_id: str, commits: list[str], line: str = "1", **extra: Any) -> dict:
    """Return a fix entry released in the version its ID starts with."""
    return {
        "entry_id": entry_id,
        "kind": "fix",
        "release": entry_id.split("/")[0],
        "line": line,
        "text": f"Fix {entry_id}",
        "source": "changelog",
        "commits": commits,
        "link_method": "sha",
        "link_confidence": 1.0,
        "text_refs": [],
        **extra,
    }


def _evidence(fix_id: str, blamed: dict[str, int], status: str = "ok", **extra: Any) -> dict:
    """Return an evidence record with the given blamed line counts."""
    record = {"fix_id": fix_id, "status": status, "blamed": blamed, "context_blamed": {}, "files": [], "symbols": []}
    return {"schema_version": 1, "fingerprint": f"ev-{fix_id}-{sorted(blamed.items())}", **record, **extra}


def _score(index: dict, columns: list[dict], fixes: list[dict], evidence: list[dict], **kwargs: Any) -> list[dict]:
    """Build columns and the SHA map, then score every fix row."""
    built = build_columns(columns, index)
    by_fix = {record["fix_id"]: record for record in evidence}
    return score_rows(fixes, by_fix, built, build_sha_map(built, index, fixes), index, **kwargs)


def _cells(row: dict) -> dict[str, dict]:
    """Index a row's cells by column ID."""
    return {cell["column_id"]: cell for cell in row["cells"]}


def _soft(row: dict) -> dict[str, float]:
    """Return a row's non-zero soft shares by column ID."""
    return {cell["column_id"]: cell["soft_share"] for cell in row["cells"] if cell["soft_share"] > 0}


def _field(row: dict, name: str) -> dict[str, Any]:
    """Return one cell field per column ID."""
    return {cell["column_id"]: cell[name] for cell in row["cells"]}


def _worked_example(**kwargs: Any) -> list[dict]:
    """Score the plan's worked example: 60% of blamed lines in feature A, 40% in code older than every column."""
    index = _index(_VERSIONS, {"a1": "1.3", "p1": "1.2", "f1": "1.4"})
    columns = [_column("1.3/A", ["a1"])]
    fixes = [_fix("1.4/F", ["f1"])]
    return _score(index, columns, fixes, [_evidence("1.4/F", {"a1": 6, "p1": 4})], **kwargs)


class TestWorkedExample:
    """The plan's worked example fixes the provisional weights and threshold."""

    def test_soft_shares_follow_blamed_lines(self) -> None:
        """A fix blaming 60% feature-A lines and 40% older code splits its soft mass 0.6 / 0.4.

        Feature A shipped in 1.3; the older commit first shipped in 1.2, before every tracked column, so it lands on
        ``pre-window@1.2``.
        """
        row = _worked_example()[0]
        assert _soft(row) == pytest.approx({"1.3/A": 0.6, "pre-window@1.2": 0.4})

    def test_feature_a_is_a_binary_link(self) -> None:
        """Raw score 0.6 for feature A clears the provisional threshold, so A is a binary association.

        Pseudo-columns never enter the binary view.
        """
        row = _worked_example()[0]
        assert _cells(row)["1.3/A"]["raw_score"] >= DEFAULT_THRESHOLD
        assert _field(row, "binary") == {"1.3/A": True, "pre-window@1.2": False}


class TestSweepingColumns:
    """Columns whose footprint touches more than ``SWEEPING_FILES`` files get damped binary support."""

    @pytest.mark.parametrize(
        ("file_count", "linked"),
        [pytest.param(10, True, id="focused-feature-links"), pytest.param(60, False, id="sweeping-change-damped")],
    )
    def test_binary_link_depends_on_footprint_size(self, file_count: int, linked: bool) -> None:
        """The worked example links feature A, unless A's commit touched more files than a feature plausibly does.

        Seen on a real repo: a "drop Python 3.8" commit touching most files was blamed for 14 of 24 fixes. Its blame
        support is scaled down so such sweeps stop dominating the binary view, while soft mass still follows lines.
        """
        index = _index(_VERSIONS, {"a1": "1.3", "p1": "1.2", "f1": "1.4"})
        columns = [_column("1.3/A", ["a1"], files=[f"src/m{i}.py" for i in range(file_count)])]
        row = _score(index, columns, [_fix("1.4/F", ["f1"])], [_evidence("1.4/F", {"a1": 6, "p1": 4})])[0]
        assert (_field(row, "binary")["1.3/A"], _soft(row)["1.3/A"]) == (linked, pytest.approx(0.6))


class TestSoftConservation:
    """Every row's soft shares sum to 1, so the matrix total equals the fix count."""

    def test_mixed_matrix_soft_sum_equals_fix_count(self) -> None:
        """Six fixes with blame, shared commits, residue, text-only and no evidence still sum to six.

        The mix covers every way a row gets mass: real blame, a shared commit split 1/k, an unattributable fix, a fix
        with no evidence record, a text-only link and a file-overlap-only fix.
        """
        index = _index(_VERSIONS, {"a1": "1.1", "b1": "1.1", "s1": "1.2", "p1": "1.0"})
        columns = [_column("1.1/A", ["a1", "s1"], files=["src/a.py"]), _column("1.2/B", ["b1", "s1"])]
        fixes = [
            _fix("1.3/F1", ["f1"]),
            _fix("1.3/F2", ["f2"]),
            _fix("1.3/F3", ["f3"]),
            _fix("1.4/F4", ["f4"]),
            _fix("1.4/F5", [], text_refs=["1.2/B"], link_method="unlinked", link_confidence=0.0),
            _fix("1.4/F6", ["f6"]),
        ]
        evidence = [
            _evidence("1.3/F1", {"a1": 3, "p1": 1}),
            _evidence("1.3/F2", {"s1": 4}),
            _evidence("1.3/F3", {}, status="unattributable"),
            _evidence("1.4/F6", {"p1": 2}, files=["src/a.py"]),
        ]
        rows = _score(index, columns, fixes, evidence)
        assert len(rows) == 6
        assert sum(cell["soft_share"] for row in rows for cell in row["cells"]) == pytest.approx(6.0)

    def test_shared_commit_keeps_both_binary_links(self) -> None:
        """A commit in two footprints counts fully for each binary link but splits its soft mass 1/k.

        Both features own commit ``s1``; the fix blames only ``s1``.
        """
        index = _index(_VERSIONS, {"s1": "1.2"})
        columns = [_column("1.2/A", ["s1"]), _column("1.2/B", ["s1"])]
        row = _score(index, columns, [_fix("1.3/F", ["f"])], [_evidence("1.3/F", {"s1": 10})])[0]
        assert _soft(row) == pytest.approx({"1.2/A": 0.5, "1.2/B": 0.5})
        assert _field(row, "binary") == {"1.2/A": True, "1.2/B": True}


class TestResidue:
    """Rows with no other evidence put their whole mass on exactly one residue bucket."""

    @pytest.mark.parametrize(
        ("evidence", "bucket"),
        [
            pytest.param([_evidence("1.3/F", {}, status="unlinked")], "unlinked", id="unlinked"),
            pytest.param([_evidence("1.3/F", {}, status="unattributable")], "unattributable", id="unattributable"),
            pytest.param([_evidence("1.3/F", {"a1": 5}, status="timed_out")], "timed_out", id="timed-out"),
            pytest.param([], "unknown", id="no-evidence"),
        ],
    )
    def test_status_selects_residue_bucket(self, evidence: list[dict], bucket: str) -> None:
        """The evidence status picks the residue bucket; a fix with no evidence record goes to ``unknown``.

        A timed-out fix's partial blame is ignored, so it cannot link a feature until blame finishes.
        """
        index = _index(_VERSIONS, {"a1": "1.1"})
        row = _score(index, [_column("1.1/A", ["a1"])], [_fix("1.3/F", ["f"])], evidence)[0]
        assert _soft(row) == {bucket: 1.0}

    def test_file_overlap_alone_never_links(self) -> None:
        """A fix touching a feature's file but blaming only older lines gets no binary link (negative control).

        File overlap weighs 0.2, below the threshold, so it can only take a little soft mass.
        """
        index = _index(_VERSIONS, {"a1": "1.2", "p1": "1.0"})
        columns = [_column("1.2/A", ["a1"], files=["src/a.py"])]
        evidence = [_evidence("1.3/F", {"p1": 8}, files=["src/a.py"])]
        cells = _cells(_score(index, columns, [_fix("1.3/F", ["f"])], evidence)[0])
        assert cells["1.2/A"]["binary"] is False
        assert cells["1.2/A"]["raw_score"] < DEFAULT_THRESHOLD

    def test_unattributable_fix_ignores_file_overlap(self) -> None:
        """An unattributable fix (tests and changelog only) never associates through file overlap.

        The fix's touched files overlap the feature's footprint, yet the row stays fully residue.
        """
        index = _index(_VERSIONS, {"a1": "1.2"})
        columns = [_column("1.2/A", ["a1"], files=["tests/test_a.py"])]
        evidence = [_evidence("1.3/F", {}, status="unattributable", files=["tests/test_a.py"])]
        row = _score(index, columns, [_fix("1.3/F", ["f"])], evidence)[0]
        assert _soft(row) == {"unattributable": 1.0}

    def test_text_link_needs_no_evidence(self) -> None:
        """An unlinked fix whose text references a feature with no footprint still links it.

        This is the "Added entry reached only by a text link" fixture: the column has no commits.
        """
        index = _index(_VERSIONS, {})
        fix = _fix("1.3/F", [], text_refs=["1.1/A"], link_method="unlinked", link_confidence=0.0)
        row = _score(index, [_column("1.1/A", [])], [fix], [_evidence("1.3/F", {}, status="unlinked")])[0]
        assert _soft(row) == {"1.1/A": 1.0}
        assert _field(row, "method") == {"1.1/A": "text"}


class TestPseudoColumns:
    """Blamed commits in no footprint go to pre-window or non-feature pseudo-columns by release."""

    @pytest.mark.parametrize(
        ("commits", "unreleased", "expected"),
        [
            pytest.param({"r1": "1.2"}, (), "non-feature@1.2", id="inside-window"),
            pytest.param({"r1": "1.0"}, (), "pre-window@1.0", id="before-every-column"),
            pytest.param({}, ("r1",), "non-feature@unreleased", id="unreleased-head-commit"),
            pytest.param({}, (), "pre-window@untagged", id="untagged"),
        ],
    )
    def test_pseudo_column_by_commit_release(
        self, commits: dict[str, str], unreleased: tuple[str, ...], expected: str
    ) -> None:
        """A commit at or after the oldest column release is non-feature; older code is pre-window.

        The only real column ships in 1.1; the fix blames one commit owned by no column.
        """
        index = _index(_VERSIONS, {"a1": "1.1", **commits}, unreleased=unreleased)
        row = _score(index, [_column("1.1/A", ["a1"])], [_fix("1.4/F", ["f"])], [_evidence("1.4/F", {"r1": 3})])[0]
        assert _soft(row) == {expected: 1.0}

    def test_own_commits_are_not_evidence(self) -> None:
        """Lines blamed on the fix's own earlier commit are dropped from numerator and denominator.

        A two-commit fix: the second commit's blame finds the first; only the feature lines remain.
        """
        index = _index(_VERSIONS, {"a1": "1.1", "f1": "1.3"})
        row = _score(
            index, [_column("1.1/A", ["a1"])], [_fix("1.3/F", ["f1", "f2"])], [_evidence("1.3/F", {"f1": 9, "a1": 1})]
        )[0]
        assert _soft(row) == {"1.1/A": 1.0}


class TestInheritance:
    """A fix of an earlier fix inherits that fix's final associations."""

    def _rows(self, parent_blame: dict[str, int], parent_status: str = "ok", **kwargs: Any) -> list[dict]:
        """Score parent fix P (1.3, commit p) and child fix C (1.4) that blames only P's commit."""
        index = _index(_VERSIONS, {"a1": "1.1", "b1": "1.2", "p": "1.3"})
        columns = [_column("1.1/A", ["a1"]), _column("1.2/B", ["b1"])]
        fixes = [_fix("1.4/C", ["c"]), _fix("1.3/P", ["p"])]
        evidence = [_evidence("1.3/P", parent_blame, status=parent_status), _evidence("1.4/C", {"p": 10})]
        return _score(index, columns, fixes, evidence, **kwargs)

    def test_child_mass_follows_parent_soft_shares(self) -> None:
        """Lines blamed on the parent fix spread over the parent's final shares, keeping both binary links.

        The parent repairs A and B half each; the child, given first in the entry list, is still scored after it.
        """
        parent, child = self._rows({"a1": 5, "b1": 5})
        assert [parent["fix_id"], child["fix_id"]] == ["1.3/P", "1.4/C"]
        assert _soft(child) == pytest.approx({"1.1/A": 0.5, "1.2/B": 0.5})
        assert _field(child, "method") == {"1.1/A": "inherited", "1.2/B": "inherited"}
        assert _field(child, "binary") == {"1.1/A": True, "1.2/B": True}

    def test_inherited_confidence_is_below_parent(self) -> None:
        """An inherited cell's confidence is strictly below the parent cell's confidence.

        The parent links A by direct blame with confidence 1.0.
        """
        parent, child = self._rows({"a1": 10})
        assert _cells(child)["1.1/A"]["confidence"] < _cells(parent)["1.1/A"]["confidence"]

    def test_residue_parent_falls_back_to_non_feature(self) -> None:
        """A parent with only residue mass sends the child's lines to ``non-feature@<parent release>``.

        The parent touched no source lines (unattributable).
        """
        _, child = self._rows({}, parent_status="unattributable")
        assert _soft(child) == {"non-feature@1.3": 1.0}

    def test_parent_scores_feed_child_fingerprint(self) -> None:
        """Changing the parent's evidence changes the child's score fingerprint even with identical child evidence.

        The parent first repairs A, then B.
        """
        first = self._rows({"a1": 10})[1]["fingerprints"]["scores"]
        second = self._rows({"b1": 10})[1]["fingerprints"]["scores"]
        assert first != second


class TestPrecedence:
    """LLM verdicts and overrides apply after the deterministic score: manual > llm > inherited > deterministic."""

    def _two_feature_row(self, **kwargs: Any) -> dict:
        """Score one fix blaming features A and B equally."""
        index = _index(_VERSIONS, {"a1": "1.1", "b1": "1.2"})
        columns = [_column("1.1/A", ["a1"]), _column("1.2/B", ["b1"])]
        return _score(index, columns, [_fix("1.3/F", ["f"])], [_evidence("1.3/F", {"a1": 5, "b1": 5})], **kwargs)[0]

    def test_llm_verdict_replaces_real_columns(self) -> None:
        """The chosen column takes the verdict confidence; the unchosen candidate drops out of both views.

        The LLM picks A with confidence 0.9 out of the A/B tie.
        """
        row = self._two_feature_row(adjudications={"1.3/F": {"verdict": {"columns": {"1.1/A": 0.9}}, "key": "k1"}})
        assert _field(row, "binary") == {"1.1/A": True, "1.2/B": False}
        assert _field(row, "method") == {"1.1/A": "llm", "1.2/B": "llm"}
        assert _soft(row) == {"1.1/A": 1.0}

    def test_llm_unknown_sets_unknown_residue(self) -> None:
        """An ``unknown`` verdict moves the whole row to the ``unknown`` residue bucket.

        Deterministic blame had linked both features.
        """
        row = self._two_feature_row(adjudications={"1.3/F": {"unknown": True}})
        assert _soft(row) == {"unknown": 1.0}
        assert _field(row, "binary") == {"1.1/A": False, "1.2/B": False, "unknown": False}

    def test_override_beats_llm(self) -> None:
        """A manual deny and assert win over the LLM verdict on the same cells.

        The LLM picks A; the user denies A and asserts B.
        """
        overrides = [
            {"fix_id": "1.3/F", "column_id": "1.1/A", "action": "deny", "note": ""},
            {"fix_id": "1.3/F", "column_id": "1.2/B", "action": "assert", "note": ""},
        ]
        row = self._two_feature_row(adjudications={"1.3/F": {"columns": {"1.1/A": 0.9}}}, overrides=overrides)
        assert _field(row, "method") == {"1.1/A": "manual", "1.2/B": "manual"}
        assert _field(row, "binary") == {"1.1/A": False, "1.2/B": True}
        assert _soft(row) == {"1.2/B": 1.0}

    def test_deny_removes_link_and_its_mass(self) -> None:
        """Denying the worked example's feature leaves the pre-window code with the whole row.

        Deterministically A held 0.6 of the soft mass and a binary link.
        """
        deny = [{"fix_id": "1.4/F", "column_id": "1.3/A", "action": "deny", "note": "not this feature"}]
        row = _worked_example(overrides=deny)[0]
        assert _soft(row) == {"pre-window@1.2": 1.0}
        assert _cells(row)["1.3/A"]["binary"] is False

    def test_deny_everything_lands_on_unknown(self) -> None:
        """Denying every column the fix reached leaves a single ``unknown`` residue cell with soft 1.0.

        The fix blames only feature A.
        """
        index = _index(_VERSIONS, {"a1": "1.1"})
        deny = [{"fix_id": "1.3/F", "column_id": "1.1/A", "action": "deny", "note": ""}]
        row = _score(
            index, [_column("1.1/A", ["a1"])], [_fix("1.3/F", ["f"])], [_evidence("1.3/F", {"a1": 4})], overrides=deny
        )[0]
        assert _soft(row) == {"unknown": 1.0}

    def test_assert_on_residue_row_replaces_residue(self) -> None:
        """Asserting a column on a fix with no evidence moves the whole row to that column.

        Residue is decided last, so it never shares the row with an asserted column.
        """
        index = _index(_VERSIONS, {"a1": "1.1"})
        assert_a = [{"fix_id": "1.3/F", "column_id": "1.1/A", "action": "assert", "note": ""}]
        row = _score(index, [_column("1.1/A", ["a1"])], [_fix("1.3/F", ["f"])], [], overrides=assert_a)[0]
        assert _soft(row) == {"1.1/A": 1.0}

    def test_invalid_override_action_is_rejected(self) -> None:
        """An override action other than assert/deny fails fast instead of being ignored.

        The action ``keep`` is not part of the overrides format.
        """
        bad = [{"fix_id": "1.4/F", "column_id": "1.3/A", "action": "keep", "note": ""}]
        with pytest.raises(ValueError, match="keep"):
            _worked_example(overrides=bad)

    def test_weights_feed_score_fingerprint(self) -> None:
        """A weight change changes the score fingerprint, so the store rescores the row.

        Only the file weight changes; the evidence is identical.
        """
        default = _worked_example()[0]["fingerprints"]["scores"]
        tuned = _worked_example(weights={"file": 0.1})[0]["fingerprints"]["scores"]
        assert default != tuned


class TestChronology:
    """Cells carry the column's introduction on the fix's own release line."""

    def test_cross_line_flag(self) -> None:
        """A column absent from the fix's line uses its global release and is marked cross-line.

        Feature X exists only on the 2.x line; feature Y was backported into 1.4.3. The fix ships in 1.4.4.
        """
        releases = [("1.4.2", "1.4"), ("2.0", "2"), ("1.4.3", "1.4"), ("1.4.4", "1.4")]
        index = {
            "releases": [
                {"tag": v, "version": v, "sha": v, "date": "", "line": ln, "is_prerelease": False, "order": i}
                for i, (v, ln) in enumerate(releases)
            ],
            "lines": {"1.4": ["1.4.2", "1.4.3", "1.4.4"], "2": ["2.0"]},
            "commit_releases": {"x1": {"2": "2.0"}, "y1": {"2": "2.0"}, "y2": {"1.4": "1.4.3"}},
            "head": {"sha": "h", "unreleased": []},
        }
        columns = [_column("2.0/X", ["x1"], line="2"), _column("2.0/Y", ["y1", "y2"], line="2")]
        fix = _fix("1.4.4/F", ["f"], line="1.4")
        row = _score(index, columns, [fix], [_evidence("1.4.4/F", {"x1": 5, "y2": 5})])[0]
        assert _field(row, "release") == {"2.0/X": "2.0", "2.0/Y": "1.4.3"}
        assert _field(row, "cross_line") == {"2.0/X": True, "2.0/Y": False}

    def test_column_release_is_earliest_line_introduction(self) -> None:
        """A backported column keeps one global release (the earliest) and one introduction per line.

        Feature Y first shipped in 2.0 and was cherry-picked into 1.4.3.
        """
        index = {
            "releases": [
                {"version": "2.0", "order": 0, "line": "2"},
                {"version": "1.4.3", "order": 1, "line": "1.4"},
            ],
            "commit_releases": {"y1": {"2": "2.0"}, "y2": {"1.4": "1.4.3"}},
        }
        column = build_columns([_column("2.0/Y", ["y1", "y2"], line="2")], index)[0]
        assert column["release"] == "2.0"
        assert column["line_introductions"] == {"1.4": "1.4.3", "2": "2.0"}

    def test_shared_fix_commit_belongs_to_first_fix(self) -> None:
        """A commit owned by two fix entries maps to the earlier fix in processing order.

        Two changelog entries in 1.2 and 1.3 link the same PR commit.
        """
        fixes = [_fix("1.3/G", ["c"]), _fix("1.2/F", ["c"])]
        assert build_sha_map([], _index(_VERSIONS, {}), fixes)["fixes"] == {"c": "1.2/F"}


def _aggregate_one(column_id: str, release: str, fix_release: str = "1.9") -> dict:
    """Aggregate one fix whose whole soft mass sits on one column introduced in ``release``."""
    versions = [f"1.{n}" for n in range(10)]
    columns = [{"column_id": column_id, "kind": "feature", "release": release, "title": "x"}]
    cell = {"column_id": column_id, "release": release, "soft_share": 1.0, "binary": False, "confidence": 1.0}
    row = {"fix_id": "F", "fix_release": fix_release, "line": "1", "cells": [cell]}
    return aggregate([row], columns, _index(versions, {}))


class TestAggregate:
    """Release-level views: flow buckets, same-cycle panel, burn-up, ranking and residue."""

    @pytest.mark.parametrize(
        ("column_id", "release", "bucket"),
        [
            pytest.param("1.8/x", "1.8", "previous", id="previous"),
            pytest.param("1.4/x", "1.4", "2-5", id="five-back"),
            pytest.param("1.3/x", "1.3", "legacy", id="six-back"),
            pytest.param("pre-window@1.0", "1.0", "legacy", id="pre-window"),
        ],
    )
    def test_age_bucket(self, column_id: str, release: str, bucket: str) -> None:
        """A fix in 1.9 repairing code from N releases back lands in the matching age bucket.

        Releases 1.0 to 1.9 form one line; pre-window code is always legacy.
        """
        assert [band["bucket"] for band in _aggregate_one(column_id, release)["flow"]] == [bucket]

    def test_same_cycle_excluded_from_flow(self) -> None:
        """A fix repairing a feature from its own release goes to the same-cycle panel, never into the flow.

        Feature and fix both ship in 1.9.
        """
        agg = _aggregate_one("1.9/x", "1.9")
        assert agg["flow"] == []
        assert agg["heatmap"] == {}
        assert [(item["fix_id"], item["column_id"], item["release"]) for item in agg["same_cycle"]] == [
            ("F", "1.9/x", "1.9")
        ]

    def test_burnup_counts_features_and_fixes_cumulatively(self) -> None:
        """Burn-up counts feature columns (not changes) and fixes up to each release.

        Features ship in 1.0 and 1.2, a behaviour change in 1.1; fixes ship once in 1.1 and twice in 1.2.
        """
        columns = [
            {"column_id": "1.0/a", "kind": "feature", "release": "1.0"},
            {"column_id": "1.1/c", "kind": "change", "release": "1.1"},
            {"column_id": "1.2/b", "kind": "feature", "release": "1.2"},
        ]
        rows = [
            {"fix_id": "x", "fix_release": "1.1", "line": "1", "cells": []},
            {"fix_id": "y", "fix_release": "1.2", "line": "1", "cells": []},
            {"fix_id": "z", "fix_release": "1.2", "line": "1", "cells": []},
        ]
        agg = aggregate(rows, columns, _index(["1.0", "1.1", "1.2"], {}))
        assert [(p["release"], p["features"], p["fixes"]) for p in agg["burnup"]] == [
            ("1.0", 1, 0),
            ("1.1", 1, 1),
            ("1.2", 2, 3),
        ]

    def test_top_features_rank_binary_fix_counts(self) -> None:
        """The ranking counts binary links per column; totals count links, fixes and the soft sum.

        Feature A is linked by two fixes, B by one; the third fix is residue.
        """
        index = _index(_VERSIONS, {"a1": "1.1", "b1": "1.2"})
        columns = [_column("1.1/A", ["a1"]), _column("1.2/B", ["b1"])]
        fixes = [_fix("1.3/F1", ["f1"]), _fix("1.3/F2", ["f2"]), _fix("1.4/F3", ["f3"])]
        evidence = [_evidence("1.3/F1", {"a1": 2}), _evidence("1.3/F2", {"a1": 1, "b1": 1})]
        rows = _score(index, columns, fixes, evidence)
        agg = aggregate(rows, build_columns(columns, index), index)
        assert [(item["column_id"], item["fix_count"]) for item in agg["top_features"]] == [("1.1/A", 2), ("1.2/B", 1)]
        assert agg["totals"]["binary_links"] == 3
        assert agg["totals"]["soft_sum"] == pytest.approx(3.0)
        assert agg["residue"] == {"1.4": {"unknown": 1.0}}


class TestCli:
    """The ``score`` subcommand reads JSON inputs and writes rows, columns and the aggregate."""

    def _inputs(self, tmp_path: Path) -> list[str]:
        """Write the worked example's inputs and return the CLI arguments."""
        index = _index(_VERSIONS, {"a1": "1.3", "p1": "1.2"})
        entries = {"fixes": [_fix("1.4/F", ["f1"])], "columns": [_column("1.3/A", ["a1"])]}
        (tmp_path / "entries.json").write_text(json.dumps(entries), encoding="utf-8")
        (tmp_path / "index.json").write_text(json.dumps(index), encoding="utf-8")
        evidence_line = json.dumps(_evidence("1.4/F", {"a1": 6, "p1": 4}))
        (tmp_path / "evidence.jsonl").write_text(evidence_line + "\n", encoding="utf-8")
        names = ("entries.json", "evidence.jsonl", "index.json", "rows.jsonl", "columns.jsonl", "agg.json")
        flags = ("--entries", "--evidence", "--index", "--out-rows", "--out-columns", "--out-aggregate")
        return ["score", *[part for flag, name in zip(flags, names) for part in (flag, str(tmp_path / name))]]

    def test_score_writes_rows_columns_and_aggregate(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """One CLI run writes one row, the real and pseudo columns used, and an aggregate with matching totals.

        Inputs are the worked example's entries, evidence and index.
        """
        assert main(self._inputs(tmp_path)) == 0
        rows = (tmp_path / "rows.jsonl").read_text(encoding="utf-8").splitlines()
        columns = [json.loads(line)["column_id"] for line in (tmp_path / "columns.jsonl").read_text().splitlines()]
        agg = json.loads((tmp_path / "agg.json").read_text(encoding="utf-8"))
        assert len(rows) == 1
        assert columns == ["1.3/A", "pre-window@1.2"]
        assert agg["totals"]["fixes"] == 1
        assert "1 binary links" in capsys.readouterr().out

    def test_bad_override_action_exits_2(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """An overrides CSV with an unknown action stops the run with exit code 2 and a named error.

        The CSV asks to ``keep`` a cell, which is not an override action.
        """
        (tmp_path / "overrides.csv").write_text("fix_id,column_id,action,note\n1.4/F,1.3/A,keep,x\n", encoding="utf-8")
        args = [*self._inputs(tmp_path), "--overrides", str(tmp_path / "overrides.csv")]
        assert main(args) == 2
        assert "keep" in capsys.readouterr().err

    def test_pseudo_column_records_describe_used_buckets(self) -> None:
        """Pseudo-column records are produced only for pseudo-columns the rows use, with their kind and release.

        The worked example uses one pre-window column.
        """
        rows = _worked_example()
        records = pseudo_columns(rows, _index(_VERSIONS, {}))
        assert [(r["column_id"], r["kind"], r["release"]) for r in records] == [("pre-window@1.2", "pre-window", "1.2")]
