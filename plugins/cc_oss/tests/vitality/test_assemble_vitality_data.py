"""Tests for ``bin/assemble_vitality_data.py``.

The fixture builds a complete Group 1 directory (one valid file per dataset) so each test can break exactly one dataset
and assert how the assembled DATA_FILE and the coverage envelope react.
"""

from __future__ import annotations

import json
from pathlib import Path

import assemble_vitality_data as avd
import pytest

_REPO = "owner/repo"
_NOW = 1_800_000_000

#: Valid Group 1 payload per dataset — small, under every cap.
_GROUP1_PAYLOADS: dict[str, object] = {
    "open_issues": [{"number": 1}],
    "closed_issues": [{"number": 2}],
    "open_prs": [{"number": 3}],
    "closed_prs": [{"number": 4}],
    "commits": ["2026-10-01T00:00:00Z"],
    "releases": [{"tag": "v1.0.0", "published": "2026-09-01T00:00:00Z", "downloads": 0}],
    "contributor_stats": [{"author": "alice", "total": 3, "weeks": [{"w": 1, "a": 0, "d": 0, "c": 3}]}],
    "repo_metadata": {"default_branch": "main"},
    "ci_workflows": {"count": 1, "names": ["CI"]},
    "dependabot_alerts": [],
    "secret_scanning_alerts": [],
    "fork_dates": ["2026-09-01T00:00:00Z"],
    "merged_prs_90d": [{"number": 5}],
    "commits_50": [{"sha": "abc1234", "message": "fix", "author": "alice", "date": "2026-10-01T00:00:00Z"}],
    "responsiveness_gql": {"data": {"repository": {"issues": {"nodes": []}, "pullRequests": {"nodes": []}}}},
    "review_coverage_gql": {"data": {"repository": {"pullRequests": {"nodes": []}}}},
    "root_contents": ["LICENSE"],
    "all_issues": [{"number": 1}],
    "all_prs": [{"number": 3}],
    "discussions": {"data": {"repository": {"discussions": {"nodes": []}}}},
}


@pytest.fixture
def group1_dir(tmp_path: Path) -> Path:
    """Write one valid JSON file per Group 1 dataset."""
    directory = tmp_path / "raw.group1"
    directory.mkdir()
    for name, payload in _GROUP1_PAYLOADS.items():
        (directory / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
    return directory


#: Records Group 2 writes on every run that reaches GitHub; the assembler requires both as proof Group 2 ran.
_BRANCH_STATUS = {"type": "default_branch_status", "branch": "main", "data": {"protected": True}}
_CI_RUNS = {
    "type": "ci_runs",
    "branch": "main",
    "records": 1,
    "data": [{"conclusion": "success", "name": "CI", "event": "push", "head_branch": "main"}],
}


def _run(data_file: Path, group1_dir: Path, capsys: pytest.CaptureFixture[str]) -> dict:
    """Run the CLI after Group 2's always-present records are in DATA_FILE and return the parsed stdout envelope."""
    existing = data_file.read_text(encoding="utf-8") if data_file.exists() else ""
    canaries = "".join(
        json.dumps(record) + "\n" for record in (_BRANCH_STATUS, _CI_RUNS) if record["type"] not in existing
    )
    data_file.parent.mkdir(parents=True, exist_ok=True)
    data_file.write_text(canaries + existing, encoding="utf-8")
    rc = avd.main(
        ["--data-file", str(data_file), "--group1-dir", str(group1_dir), "--repo", _REPO, "--analysis-now", str(_NOW)]
    )
    assert rc == 0
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _records(data_file: Path) -> dict[str, dict]:
    """Parse DATA_FILE into a type → record map, asserting each type appears once."""
    lines = [json.loads(line) for line in data_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    by_type = {record["type"]: record for record in lines}
    assert len(by_type) == len(lines), "duplicate record types"
    return by_type


def test_group1_dataset_table_matches_group1_producer() -> None:
    """Expect one Group 1 dataset spec per dataset the Group 1 fetcher writes, so no new fetch goes unchecked."""
    import fetch_gh_data_group1 as fgd

    # Arrange
    cutoffs = fgd.Cutoffs("2023-01-01", "2023-03-02T00:00:00Z", "2023-01-01T00:00:00Z", "2022-07-01T00:00:00Z")
    produced = {name for name, _ in fgd._build_datasets("o/r", cutoffs)}

    # Act
    specified = {spec.name for spec in avd.GROUP1_DATASETS}

    # Assert
    assert specified == produced


def test_complete_inputs_write_every_dataset_and_report_done(
    tmp_path: Path, group1_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Write all 20 Group 1 records with metadata, keep Group 2's records, and report done at full confidence."""
    # Arrange
    data_file = tmp_path / "raw.jsonl"

    # Act
    envelope = _run(data_file, group1_dir, capsys)

    # Assert
    records = _records(data_file)
    assert set(records) == set(_GROUP1_PAYLOADS) | {"default_branch_status", "ci_runs"}
    assert records["open_issues"] == {
        "type": "open_issues",
        "repo": _REPO,
        "timestamp": _NOW,
        "records": 1,
        "partial": False,
        "data": [{"number": 1}],
    }
    assert envelope["status"] == "done"
    assert envelope["missing_required"] == []
    assert envelope["confidence"] == 0.95
    assert envelope["datasets"] == 22


def test_missing_branch_status_shows_group2_never_ran(
    tmp_path: Path, group1_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Report Group 2's branch-status and CI-runs records as missing when DATA_FILE holds no Group 2 record at all.

    The public branches and runs endpoints answer on every run, so their records are the canary: without them Group 2
    was skipped or queried a wrong default branch, and every content-based checkpoint downstream would read as absent.
    """
    # Arrange
    data_file = tmp_path / "raw.jsonl"

    # Act
    rc = avd.main(["--data-file", str(data_file), "--group1-dir", str(group1_dir), "--repo", _REPO])

    # Assert
    envelope = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 0
    assert envelope["status"] == "partial"
    assert envelope["missing_required"] == ["ci_runs", "default_branch_status"]


def test_group2_records_already_in_data_file_are_kept_verbatim(
    tmp_path: Path, group1_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Keep the text-content records Group 2 appended, after the rebuilt Group 1 records."""
    # Arrange
    data_file = tmp_path / "raw.jsonl"
    readme = {"type": "readme_content", "data": "# Title\npip install x\n"}
    codeowners = {"type": "codeowners_text", "data": "* @alice\n", "source": ".github/CODEOWNERS"}
    data_file.write_text(json.dumps(readme) + "\n" + json.dumps(codeowners) + "\n", encoding="utf-8")

    # Act
    _run(data_file, group1_dir, capsys)

    # Assert
    records = _records(data_file)
    assert records["readme_content"] == readme
    assert records["codeowners_text"] == codeowners
    last_two = [json.loads(line)["type"] for line in data_file.read_text(encoding="utf-8").splitlines()[-2:]]
    assert last_two == ["readme_content", "codeowners_text"]


def test_rerun_is_idempotent(tmp_path: Path, group1_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Produce byte-identical output on a second run, with no duplicated record types."""
    # Arrange
    data_file = tmp_path / "raw.jsonl"
    data_file.write_text(json.dumps({"type": "github_dir", "data": ["workflows"]}) + "\n", encoding="utf-8")
    _run(data_file, group1_dir, capsys)
    first = data_file.read_bytes()

    # Act
    _run(data_file, group1_dir, capsys)

    # Assert
    assert data_file.read_bytes() == first
    _records(data_file)


def test_stale_group1_records_in_data_file_are_replaced(
    tmp_path: Path, group1_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Replace a Group 1 record type already in DATA_FILE with the one rebuilt from the directory."""
    # Arrange
    data_file = tmp_path / "raw.jsonl"
    data_file.write_text(json.dumps({"type": "open_issues", "data": ["stale"]}) + "\n", encoding="utf-8")

    # Act
    _run(data_file, group1_dir, capsys)

    # Assert
    assert _records(data_file)["open_issues"]["data"] == [{"number": 1}]


def test_duplicate_group2_appends_collapse_to_last(
    tmp_path: Path, group1_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Keep only the last record when Group 2 appended the same type twice."""
    # Arrange
    data_file = tmp_path / "raw.jsonl"
    lines = [
        json.dumps({"type": "readme_content", "data": "old"}),
        json.dumps({"type": "readme_content", "data": "new"}),
    ]
    data_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Act
    _run(data_file, group1_dir, capsys)

    # Assert
    assert _records(data_file)["readme_content"]["data"] == "new"


@pytest.mark.parametrize(
    ("dataset", "content", "expected"),
    [
        pytest.param("dependabot_alerts", "", {"data": "403", "records": 0, "partial": False}, id="alerts-failed-403"),
        pytest.param(
            "secret_scanning_alerts", "", {"data": "403", "records": 0, "partial": False}, id="secrets-failed-403"
        ),
        pytest.param("dependabot_alerts", "[]", {"data": [], "records": 0, "partial": False}, id="alerts-zero-open"),
        pytest.param(
            "contributor_stats", "", {"data": None, "partial": True, "202_pending": True}, id="stats-failed-202"
        ),
        pytest.param(
            "contributor_stats", "[]", {"data": None, "partial": True, "202_pending": True}, id="stats-empty-list-202"
        ),
        pytest.param(
            "contributor_stats", "{}", {"data": None, "partial": True, "202_pending": True}, id="stats-empty-obj-202"
        ),
        pytest.param("releases", "[]", {"data": [], "records": 0, "partial": False}, id="valid-empty-list-kept"),
    ],
)
def test_failed_or_empty_fetch_is_recorded_per_kind(
    tmp_path: Path,
    group1_dir: Path,
    capsys: pytest.CaptureFixture[str],
    dataset: str,
    content: str,
    expected: dict,
) -> None:
    """Record alert failures as 403, empty stats as 202 pending, and a valid empty list as real data."""
    # Arrange
    (group1_dir / f"{dataset}.json").write_text(content, encoding="utf-8")
    data_file = tmp_path / "raw.jsonl"

    # Act
    envelope = _run(data_file, group1_dir, capsys)

    # Assert
    record = _records(data_file)[dataset]
    assert {key: record[key] for key in expected} == expected
    assert dataset not in envelope["missing_required"]


@pytest.mark.parametrize(
    ("dataset", "content"),
    [
        pytest.param("review_coverage_gql", "", id="zero-byte-file"),
        pytest.param("review_coverage_gql", "{not json", id="invalid-json"),
        pytest.param("review_coverage_gql", None, id="file-absent"),
    ],
)
def test_missing_required_dataset_makes_status_partial(
    tmp_path: Path,
    group1_dir: Path,
    capsys: pytest.CaptureFixture[str],
    dataset: str,
    content: str | None,
) -> None:
    """Omit the failed dataset, list it as missing, and drop confidence below the complete-file value."""
    # Arrange
    path = group1_dir / f"{dataset}.json"
    if content is None:
        path.unlink()
    else:
        path.write_text(content, encoding="utf-8")
    data_file = tmp_path / "raw.jsonl"

    # Act
    envelope = _run(data_file, group1_dir, capsys)

    # Assert
    assert dataset not in _records(data_file)
    assert envelope["status"] == "partial"
    assert envelope["missing_required"] == [dataset]
    assert envelope["confidence"] == 0.7


def test_failed_optional_dataset_is_listed_without_partial_status(
    tmp_path: Path, group1_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """List a failed optional dataset separately and keep the status done."""
    # Arrange
    (group1_dir / "discussions.json").write_text("", encoding="utf-8")
    data_file = tmp_path / "raw.jsonl"

    # Act
    envelope = _run(data_file, group1_dir, capsys)

    # Assert
    assert envelope["status"] == "done"
    assert envelope["missing_optional"] == ["discussions"]


@pytest.mark.parametrize(
    ("dataset", "count", "partial"),
    [
        pytest.param("open_issues", 501, True, id="open-issues-at-cap"),
        pytest.param("open_issues", 500, False, id="open-issues-below-cap"),
        pytest.param("commits", 100, True, id="commits-at-cap"),
        pytest.param("commits_50", 50, False, id="fixed-sample-never-partial"),
        pytest.param("closed_issues", 1000, True, id="closed-issues-at-the-search-ceiling"),
        pytest.param("closed_issues", 999, False, id="closed-issues-below-the-search-ceiling"),
    ],
)
def test_truncation_flag_follows_fetch_cap(
    tmp_path: Path,
    group1_dir: Path,
    capsys: pytest.CaptureFixture[str],
    dataset: str,
    count: int,
    partial: bool,
) -> None:
    """Flag a list as partial exactly when it reaches the item cap its fetch requested.

    A search-backed list (``closed_issues``) is capped at GitHub's 1000-result search ceiling: asking for 1001 never
    returned more than 1000, so the old cap could not be reached and a truncated month read as complete.
    """
    # Arrange
    (group1_dir / f"{dataset}.json").write_text(json.dumps([{"n": i} for i in range(count)]), encoding="utf-8")
    data_file = tmp_path / "raw.jsonl"

    # Act
    envelope = _run(data_file, group1_dir, capsys)

    # Assert
    record = _records(data_file)[dataset]
    assert record["records"] == count
    assert record["partial"] is partial
    assert (dataset in envelope["partial"]) is partial


@pytest.mark.parametrize(
    ("registry", "partial"),
    [
        pytest.param({"count": 100, "total_count": 130, "names": []}, True, id="registry-past-one-page"),
        pytest.param({"count": 29, "total_count": 29, "names": []}, False, id="registry-on-one-page"),
        pytest.param({"count": 100, "total_count": 100, "names": []}, False, id="registry-exactly-one-full-page"),
        pytest.param({"count": 29, "names": []}, False, id="older-registry-without-total-count"),
        pytest.param({"count": 100, "names": []}, True, id="registry-at-the-page-cap-without-total-count"),
    ],
)
def test_workflow_registry_truncation_reads_the_total_count(
    tmp_path: Path, group1_dir: Path, capsys: pytest.CaptureFixture[str], registry: dict, partial: bool
) -> None:
    """Flag the workflow registry partial when GitHub holds more entries than the one fetched page returned.

    The registry was fetched at GitHub's default page of 30 with ``total_count`` dropped, so a repository with more
    registered workflows (YAML files plus ``dynamic/*`` entries) silently lost the rest — including the code-scanning
    default-setup entry Axis 5 checkpoint 4 credits.
    """
    # Arrange
    (group1_dir / "ci_workflows.json").write_text(json.dumps(registry), encoding="utf-8")
    data_file = tmp_path / "raw.jsonl"

    # Act
    envelope = _run(data_file, group1_dir, capsys)

    # Assert
    assert _records(data_file)["ci_workflows"]["partial"] is partial
    assert ("ci_workflows" in envelope["partial"]) is partial


@pytest.mark.parametrize(
    ("root", "github_dir", "group2", "missing"),
    [
        pytest.param(["README.md"], None, [], ["readme_content"], id="readme-listed-not-fetched"),
        pytest.param(["README.md"], None, ["readme_content"], [], id="readme-listed-and-fetched"),
        pytest.param(["LICENSE"], None, [], [], id="no-readme-in-repo"),
        pytest.param([".github"], None, [], ["github_dir"], id="github-dir-listed-not-fetched"),
        pytest.param(["docs"], None, [], ["docs_dir"], id="docs-dir-listed-not-fetched"),
        pytest.param(
            [".github"],
            ["workflows", "CODEOWNERS", "dependabot.yml"],
            ["github_dir"],
            ["codeowners_text", "workflows_list", "workflow_files", "dependabot_config"],
            id="github-dir-entries-not-fetched",
        ),
        pytest.param(["CONTRIBUTING.md"], None, [], ["contributing_text"], id="contributing-listed-not-fetched"),
        pytest.param(
            [".github"],
            ["SECURITY.md", "CONTRIBUTING.md"],
            ["github_dir"],
            ["contributing_text", "security_text"],
            id="community-files-in-github-dir-not-fetched",
        ),
        pytest.param(["CHANGELOG.md"], None, [], ["changelog_headings"], id="changelog-listed-not-fetched"),
        pytest.param(["changes", "news"], None, [], [], id="fragment-dirs-are-not-changelogs"),
        pytest.param(["changelog"], None, [], [], id="bare-changelog-may-be-a-fragment-dir"),
        pytest.param(
            [".github"],
            ["dependabot.yaml"],
            ["github_dir"],
            ["dependabot_config"],
            id="dependabot-yaml-listed-not-fetched",
        ),
        pytest.param(["security", "contributing"], None, [], [], id="policy-named-dirs-are-not-policy-files"),
    ],
)
def test_group2_dataset_required_only_when_listing_shows_the_file(
    tmp_path: Path,
    group1_dir: Path,
    capsys: pytest.CaptureFixture[str],
    root: list[str],
    github_dir: list[str] | None,
    group2: list[str],
    missing: list[str],
) -> None:
    """Report a Group 2 record as missing only when the repository listing proves the file exists."""
    # Arrange
    (group1_dir / "root_contents.json").write_text(json.dumps(root), encoding="utf-8")
    data_file = tmp_path / "raw.jsonl"
    lines = []
    for rtype in group2:
        data = github_dir if rtype == "github_dir" else "text"
        lines.append(json.dumps({"type": rtype, "data": data}))
    data_file.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")

    # Act
    envelope = _run(data_file, group1_dir, capsys)

    # Assert
    assert envelope["missing_required"] == missing
    assert envelope["status"] == ("partial" if missing else "done")


def test_full_page_of_runs_never_lowers_confidence(
    tmp_path: Path, group1_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Keep a full page of 100 CI runs out of the truncation count: the pass rate samples its newest 20 counted runs.

    As a capped Group 1 dataset, every active repository's runs were flagged partial and dropped the envelope to 0.88
    while the rubric called a full page "never a degrader". A page that leaves fewer than 20 counted runs is a notes
    item for the scorer (``runs_sampled`` beside ``runs_fetched``), not a dataset truncation.
    """
    # Arrange
    data_file = tmp_path / "raw.jsonl"
    runs = {"type": "ci_runs", "branch": "main", "records": 100, "data": [{"conclusion": "success"}] * 100}
    data_file.write_text(json.dumps(runs) + "\n", encoding="utf-8")

    # Act
    envelope = _run(data_file, group1_dir, capsys)

    # Assert
    assert envelope["confidence"] == 0.95
    assert "ci_runs" not in envelope["partial"]


@pytest.mark.parametrize(
    ("root", "group2", "missing"),
    [
        pytest.param(
            [".github"],
            {"github_dir": ["workflows"], "workflows_list": ["README.md"]},
            [],
            id="workflows-dir-without-yaml-needs-no-content",
        ),
        pytest.param(
            [".github"],
            {"github_dir": ["workflows"], "workflows_list": ["ci.yml"]},
            ["workflow_files"],
            id="yaml-workflow-listed-content-missing",
        ),
        pytest.param(
            ["docs"], {"docs_dir": ["index.md", "CODEOWNERS"]}, ["codeowners_text"], id="codeowners-listed-in-docs"
        ),
    ],
)
def test_group2_content_expected_only_where_a_listing_shows_it(
    tmp_path: Path, group1_dir: Path, capsys: pytest.CaptureFixture[str], root: list[str], group2: dict, missing: list
) -> None:
    """Expect workflow content only for a listed ``.yml``/``.yaml`` file, and CODEOWNERS wherever GitHub reads it.

    A ``.github/workflows/`` directory holding only a README flagged ``workflow_files`` missing and capped the envelope
    at 0.70 although nothing could be fetched; a ``docs/CODEOWNERS`` was never expected at all.
    """
    # Arrange
    (group1_dir / "root_contents.json").write_text(json.dumps(root), encoding="utf-8")
    data_file = tmp_path / "raw.jsonl"
    lines = [json.dumps({"type": rtype, "data": data}) for rtype, data in group2.items()]
    data_file.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")

    # Act
    envelope = _run(data_file, group1_dir, capsys)

    # Assert
    assert envelope["missing_required"] == missing


def test_malformed_existing_lines_are_dropped_with_warning(
    tmp_path: Path, group1_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Drop unparsable or untyped lines already in DATA_FILE and warn on stderr."""
    # Arrange
    data_file = tmp_path / "raw.jsonl"
    data_file.write_text('{"broken\n{"data": "no type"}\n\n', encoding="utf-8")

    # Act
    rc = avd.main(["--data-file", str(data_file), "--group1-dir", str(group1_dir), "--repo", _REPO])

    # Assert
    assert rc == 0
    assert "dropped malformed line 1" in capsys.readouterr().err
    assert set(_records(data_file)) == set(_GROUP1_PAYLOADS)


def test_write_leaves_no_temporary_file(tmp_path: Path, group1_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Leave only DATA_FILE behind after the atomic replace."""
    # Arrange
    out_dir = tmp_path / "out"
    data_file = out_dir / "raw.jsonl"

    # Act
    _run(data_file, group1_dir, capsys)

    # Assert
    assert sorted(path.name for path in out_dir.iterdir()) == ["raw.jsonl"]


def test_missing_group1_dir_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Exit 1 without writing DATA_FILE when the Group 1 directory does not exist."""
    # Arrange
    data_file = tmp_path / "raw.jsonl"

    # Act
    rc = avd.main(["--data-file", str(data_file), "--group1-dir", str(tmp_path / "absent"), "--repo", _REPO])

    # Assert
    assert rc == 1
    assert "Group 1 directory not found" in capsys.readouterr().err
    assert not data_file.exists()


@pytest.mark.parametrize("repo", ["no-slash", "o/r;rm"])
def test_invalid_repo_is_rejected(tmp_path: Path, group1_dir: Path, repo: str) -> None:
    """Reject an owner/repo slug outside the allowed character set before writing anything."""
    # Act
    with pytest.raises(SystemExit) as exc:
        avd.main(["--data-file", str(tmp_path / "raw.jsonl"), "--group1-dir", str(group1_dir), "--repo", repo])

    # Assert
    assert exc.value.code == 2
    assert not (tmp_path / "raw.jsonl").exists()


@pytest.mark.parametrize(
    ("required_partial", "missing", "expected"),
    [
        pytest.param(0, 0, 0.95, id="complete"),
        pytest.param(2, 0, 0.88, id="two-truncated"),
        pytest.param(3, 0, 0.78, id="three-truncated"),
        pytest.param(0, 1, 0.70, id="one-missing-caps"),
        pytest.param(3, 2, 0.65, id="two-missing"),
        pytest.param(0, 20, 0.40, id="floor"),
    ],
)
def test_coverage_confidence(required_partial: int, missing: int, expected: float) -> None:
    """Never report the complete-file confidence for a file missing required datasets."""
    # Arrange
    coverage = avd.Coverage(10, [], [f"d{i}" for i in range(missing)], [], required_partial)

    # Act
    confidence = avd.coverage_confidence(coverage)

    # Assert
    assert confidence == expected
