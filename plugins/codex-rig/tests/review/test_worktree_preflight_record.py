"""Keep versioned worktree preflight records small and readable alongside unversioned historical ones.

Versioned records list only overlapping dirty paths plus a bounded sample and count the rest, so a large ignored
environment tree no longer inflates every review run. Readers must accept both shapes and reject a versioned record
whose listed, omitted, and counted paths disagree.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

import pytest
from test_review_completion_gate import _module

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_HEAD = "a" * 40
_PR_HEAD = "b" * 40
_THOUSAND_DIRTY = {"tracked": 1, "staged": 0, "untracked_unignored": 0, "ignored": 999, "total": 1000}


@pytest.fixture(name="collector")
def _collector() -> ModuleType:
    """Load the shipped collector, which owns the record shape and its consistency check."""
    return _module(PLUGIN_ROOT / "shared" / "collect_pr.py")


@pytest.fixture(name="validator")
def _validator() -> ModuleType:
    """Load the shipped Code Review validator, one reader of preflight records."""
    return _module(PLUGIN_ROOT / "skills" / "code-review" / "validate_artifacts.py")


def _record(**overrides: object) -> dict[str, object]:
    """Return one consistent versioned before-checkout record with one overlap and a sampled ignored tree."""
    record: dict[str, object] = {
        "schema_version": 1,
        "phase": "before-checkout",
        "status": "blocked-pr-dirty-paths",
        "current_head": _HEAD,
        "expected_head": _PR_HEAD,
        "dirty_paths": ["a.py", ".venv/x.py"],
        "dirty_paths_omitted": 0,
        "dirty_path_counts": {"tracked": 1, "staged": 0, "untracked_unignored": 0, "ignored": 1, "total": 2},
        "unmerged_paths": [],
        "pr_paths": ["a.py"],
        "checkout_paths": ["a.py"],
        "overlapping_paths": ["a.py"],
        "overlapping_pr_paths": ["a.py"],
    }
    record.update(overrides)
    return record


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        pytest.param({"dirty_paths": ["every/historical/path"]}, True, id="unversioned-historical"),
        pytest.param(_record(), True, id="versioned-consistent"),
        pytest.param(_record(dirty_paths_omitted=997), False, id="omitted-count-disagrees"),
        pytest.param(
            _record(dirty_path_counts={"tracked": 1, "ignored": 999, "total": 1000}), False, id="class-missing"
        ),
        pytest.param(_record(schema_version=2), False, id="unknown-schema"),
        pytest.param(
            _record(dirty_paths=["a.py", "a.py"], dirty_paths_omitted=0),
            False,
            id="duplicate-listed-path",
        ),
        pytest.param(
            _record(
                dirty_paths=["a.py", *(f"s{index:02d}" for index in range(50))],
                dirty_paths_omitted=949,
                dirty_path_counts=_THOUSAND_DIRTY,
            ),
            True,
            id="full-sample-with-omissions",
        ),
        pytest.param(
            _record(dirty_paths=["a.py", ".venv/x.py"], dirty_paths_omitted=998, dirty_path_counts=_THOUSAND_DIRTY),
            False,
            id="omissions-before-sample-is-full",
        ),
        pytest.param(
            _record(
                dirty_paths=["a.py", *(f"s{index:02d}" for index in range(51))],
                dirty_paths_omitted=948,
                dirty_path_counts=_THOUSAND_DIRTY,
            ),
            False,
            id="sample-over-limit",
        ),
    ],
)
def test_dirty_path_record_accounts_for_every_path(
    collector: ModuleType, record: dict[str, object], expected: bool
) -> None:
    """A versioned record lists or counts every distinct dirty path once, within the sample limit.

    Unversioned historical records listed every path and stay readable without counts.
    """
    assert collector.dirty_path_record_is_consistent(record) is expected


@pytest.mark.parametrize(
    "record",
    [
        pytest.param(
            {
                key: value
                for key, value in _record().items()
                if key
                not in {
                    "schema_version",
                    "dirty_paths_omitted",
                    "dirty_path_counts",
                }
            },
            id="historical-shape",
        ),
        pytest.param(_record(), id="versioned-shape"),
    ],
)
def test_unavailable_review_reads_both_preflight_shapes(
    validator: ModuleType, tmp_path: Path, record: dict[str, object]
) -> None:
    """The unavailable-review diagnostic renders head identifiers from historical and versioned records alike.

    Its exact key-set check predates the counts, so the versioned keys are admitted only beside a schema version.
    """
    (tmp_path / "worktree-preflight.json").write_text(json.dumps(record), encoding="utf-8")

    diagnostic = validator._unavailable_preflight_diagnostic(tmp_path)

    assert diagnostic == f"Worktree preflight: local head `{_HEAD}`; expected PR head `{_PR_HEAD}`."


@pytest.mark.parametrize(
    "record",
    [
        pytest.param(_record(dirty_paths_omitted=5), id="counts-disagree"),
        pytest.param({**_record(), "schema_version": 1, "unexpected": []}, id="extra-key"),
        pytest.param(
            {key: value for key, value in _record().items() if key != "schema_version"}, id="counts-without-version"
        ),
    ],
)
def test_unavailable_review_rejects_inconsistent_preflight(
    validator: ModuleType, tmp_path: Path, record: dict[str, object]
) -> None:
    """A versioned record whose counts disagree, or an unversioned one carrying counts, is rejected."""
    (tmp_path / "worktree-preflight.json").write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(SystemExit, match="unavailable-review-worktree-preflight-invalid"):
        validator._unavailable_preflight_diagnostic(tmp_path)
