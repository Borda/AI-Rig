"""Tests for ``bin/assemble_vitality_data.py``.

The fixture builds a complete Group 1 directory (one valid file per dataset) so each test can break exactly one dataset
and assert how the assembled DATA_FILE and the coverage envelope react.
"""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path

import assemble_vitality_data as avd
import fetch_gh_data_group1 as fgd
import pytest
from _vitality_data import NOW

_REPO = "owner/repo"
_TEXT = "text"

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

#: Records Group 2 writes on every run that reaches GitHub; the assembler requires both as proof Group 2 ran.
_BRANCH_STATUS = {"type": "default_branch_status", "branch": "main", "data": {"protected": True}}
_CI_RUNS = {
    "type": "ci_runs",
    "branch": "main",
    "records": 1,
    "data": [{"conclusion": "success", "name": "CI", "event": "push", "head_branch": "main"}],
}


@pytest.fixture
def group1_dir(tmp_path: Path) -> Path:
    """Write one valid JSON file per Group 1 dataset."""
    directory = tmp_path / "raw.group1"
    directory.mkdir()
    for name, payload in _GROUP1_PAYLOADS.items():
        (directory / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
    return directory


@pytest.fixture
def data_file(tmp_path: Path) -> Path:
    """Return the not-yet-written DATA_FILE path inside ``tmp_path``."""
    return tmp_path / "raw.jsonl"


def _cli(data_file: Path, group1_dir: Path, repo: str = _REPO) -> int:
    """Run the CLI with the three required options and return its exit code."""
    return avd.main(["--data-file", str(data_file), "--group1-dir", str(group1_dir), "--repo", repo])


def _run(data_file: Path, group1_dir: Path, capsys: pytest.CaptureFixture[str]) -> dict:
    """Run the CLI after Group 2's always-present records are in DATA_FILE and return the parsed stdout envelope."""
    existing = data_file.read_text(encoding="utf-8") if data_file.exists() else ""
    canaries = "".join(
        json.dumps(record) + "\n" for record in (_BRANCH_STATUS, _CI_RUNS) if record["type"] not in existing
    )
    data_file.parent.mkdir(parents=True, exist_ok=True)
    data_file.write_text(canaries + existing, encoding="utf-8")
    rc = avd.main(
        ["--data-file", str(data_file), "--group1-dir", str(group1_dir), "--repo", _REPO, "--analysis-now", str(NOW)]
    )
    assert rc == 0
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _records(data_file: Path) -> dict[str, dict]:
    """Parse DATA_FILE into a type → record map, asserting each type appears once."""
    lines = [json.loads(line) for line in data_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    by_type = {record["type"]: record for record in lines}
    assert len(by_type) == len(lines), "duplicate record types"
    return by_type


def _write_group2(data_file: Path, records: dict[str, object]) -> None:
    """Write one Group 2 record per ``type → data`` entry into DATA_FILE, one JSON object per line."""
    data_file.write_text("".join(json.dumps({"type": rtype, "data": data}) + "\n" for rtype, data in records.items()))


class TestAssembly:
    """How Group 1 files and Group 2 records already in DATA_FILE combine into the assembled file."""

    def test_group1_dataset_table_matches_producer(self) -> None:
        """One Group 1 dataset spec exists per dataset the Group 1 fetcher writes, so no new fetch goes unchecked.

        The assembler's table is hand-maintained; a dataset added to the fetcher but not here would be silently ignored.
        """
        # Arrange
        cutoffs = fgd.Cutoffs("2023-01-01", "2023-03-02T00:00:00Z", "2023-01-01T00:00:00Z", "2022-07-01T00:00:00Z")
        produced = {name for name, _ in fgd._build_datasets("o/r", cutoffs)}

        # Act
        specified = {spec.name for spec in avd.GROUP1_DATASETS}

        # Assert
        assert specified == produced

    def test_complete_inputs_write_every_dataset_and_report_done(
        self, data_file: Path, group1_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Write all 20 Group 1 records with metadata, keep Group 2's records, and report done at full confidence.

        The happy path: every dataset present and Group 2's canaries in place, so nothing is missing or truncated.
        """
        # Act
        envelope = _run(data_file, group1_dir, capsys)

        # Assert
        records = _records(data_file)
        assert set(records) == set(_GROUP1_PAYLOADS) | {"default_branch_status", "ci_runs"}
        assert records["open_issues"] == {
            "type": "open_issues",
            "repo": _REPO,
            "timestamp": NOW,
            "records": 1,
            "partial": False,
            "data": [{"number": 1}],
        }
        assert envelope["status"] == "done"
        assert envelope["missing_required"] == []
        assert envelope["confidence"] == 0.95
        assert envelope["datasets"] == 22

    def test_group2_records_kept_verbatim_after_group1(
        self, data_file: Path, group1_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Keep the text-content records Group 2 appended, after the rebuilt Group 1 records.

        Group 2 fetches README and CODEOWNERS text before this step runs; rebuilding the file must not alter or reorder
        them, only put the Group 1 records ahead.
        """
        # Arrange
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

    def test_rerun_is_idempotent(self, data_file: Path, group1_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Produce byte-identical output on a second run, with no duplicated record types.

        The skill may re-run this step after a retry; a second pass over its own output must change nothing.
        """
        # Arrange
        data_file.write_text(json.dumps({"type": "github_dir", "data": ["workflows"]}) + "\n", encoding="utf-8")
        _run(data_file, group1_dir, capsys)
        first = data_file.read_bytes()

        # Act
        _run(data_file, group1_dir, capsys)

        # Assert
        assert data_file.read_bytes() == first
        _records(data_file)

    def test_stale_group1_records_replaced(
        self, data_file: Path, group1_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Replace a Group 1 record type already in DATA_FILE with the one rebuilt from the directory.

        A leftover record from an earlier run must never win over the freshly fetched dataset of the same type.
        """
        # Arrange
        data_file.write_text(json.dumps({"type": "open_issues", "data": ["stale"]}) + "\n", encoding="utf-8")

        # Act
        _run(data_file, group1_dir, capsys)

        # Assert
        assert _records(data_file)["open_issues"]["data"] == [{"number": 1}]

    def test_duplicate_group2_appends_collapse_to_last(
        self, data_file: Path, group1_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Keep only the last record when Group 2 appended the same type twice.

        A retried Group 2 fetch appends again; the newest answer is the one to score.
        """
        # Arrange
        lines = [
            json.dumps({"type": "readme_content", "data": "old"}),
            json.dumps({"type": "readme_content", "data": "new"}),
        ]
        data_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # Act
        _run(data_file, group1_dir, capsys)

        # Assert
        assert _records(data_file)["readme_content"]["data"] == "new"

    def test_malformed_existing_lines_dropped_with_warning(
        self, data_file: Path, group1_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Drop unparsable or untyped lines already in DATA_FILE and warn on stderr.

        A truncated line from an interrupted Group 2 append must not abort assembly or survive into the output.
        """
        # Arrange
        data_file.write_text('{"broken\n{"data": "no type"}\n\n', encoding="utf-8")

        # Act
        rc = _cli(data_file, group1_dir)

        # Assert
        assert rc == 0
        assert "dropped malformed line 1" in capsys.readouterr().err
        assert set(_records(data_file)) == set(_GROUP1_PAYLOADS)

    def test_write_leaves_no_temporary_file(
        self, tmp_path: Path, group1_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Leave only DATA_FILE behind after the atomic replace.

        The output goes through a temporary file in its own directory; a stray sibling would pollute the run directory.
        """
        # Arrange
        out_dir = tmp_path / "out"

        # Act
        _run(out_dir / "raw.jsonl", group1_dir, capsys)

        # Assert
        assert sorted(path.name for path in out_dir.iterdir()) == ["raw.jsonl"]


class TestFetchFailures:
    """How failed, empty, truncated, or missing Group 1 datasets show up in the records and the envelope."""

    @pytest.mark.parametrize(
        ("dataset", "content", "expected"),
        [
            pytest.param(
                "dependabot_alerts", "", {"data": "403", "records": 0, "partial": False}, id="alerts-failed-403"
            ),
            pytest.param(
                "secret_scanning_alerts", "", {"data": "403", "records": 0, "partial": False}, id="secrets-failed-403"
            ),
            pytest.param(
                "dependabot_alerts", "[]", {"data": [], "records": 0, "partial": False}, id="alerts-zero-open"
            ),
            pytest.param(
                "contributor_stats", "", {"data": None, "partial": True, "202_pending": True}, id="stats-failed-202"
            ),
            pytest.param(
                "contributor_stats",
                "[]",
                {"data": None, "partial": True, "202_pending": True},
                id="stats-empty-list-202",
            ),
            pytest.param(
                "contributor_stats",
                "{}",
                {"data": None, "partial": True, "202_pending": True},
                id="stats-empty-obj-202",
            ),
            pytest.param("releases", "[]", {"data": [], "records": 0, "partial": False}, id="valid-empty-list-kept"),
        ],
    )
    def test_failed_or_empty_fetch_recorded_per_kind(
        self,
        data_file: Path,
        group1_dir: Path,
        capsys: pytest.CaptureFixture[str],
        dataset: str,
        content: str,
        expected: dict,
    ) -> None:
        """Record alert failures as 403, empty stats as 202 pending, and a valid empty list as real data.

        The same empty file means different things per dataset: a 403 for alerts, a still-computing 202 for stats, and
        a legitimate zero for a list. None of them may count as a missing required dataset.
        """
        # Arrange
        (group1_dir / f"{dataset}.json").write_text(content, encoding="utf-8")

        # Act
        envelope = _run(data_file, group1_dir, capsys)

        # Assert
        record = _records(data_file)[dataset]
        assert {key: record[key] for key in expected} == expected
        assert dataset not in envelope["missing_required"]

    @pytest.mark.parametrize(
        "break_file",
        [
            pytest.param(partial(Path.write_text, data=""), id="zero-byte-file"),
            pytest.param(partial(Path.write_text, data="{not json"), id="invalid-json"),
            pytest.param(Path.unlink, id="file-absent"),
        ],
    )
    def test_missing_required_dataset_makes_status_partial(
        self, data_file: Path, group1_dir: Path, capsys: pytest.CaptureFixture[str], break_file
    ) -> None:
        """Omit the failed dataset, list it as missing, and drop confidence below the complete-file value.

        A required dataset that is empty, unparsable, or absent is equally unusable, so all three give the same result.
        """
        # Arrange
        break_file(group1_dir / "review_coverage_gql.json")

        # Act
        envelope = _run(data_file, group1_dir, capsys)

        # Assert
        assert "review_coverage_gql" not in _records(data_file)
        assert envelope["status"] == "partial"
        assert envelope["missing_required"] == ["review_coverage_gql"]
        assert envelope["confidence"] == 0.7

    def test_failed_optional_dataset_listed_without_partial_status(
        self, data_file: Path, group1_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """List a failed optional dataset separately and keep the status done.

        Discussions are optional (many repositories disable them), so their failure must not downgrade the run.
        """
        # Arrange
        (group1_dir / "discussions.json").write_text("", encoding="utf-8")

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
        self,
        data_file: Path,
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
    def test_workflow_registry_truncation_reads_total_count(
        self, data_file: Path, group1_dir: Path, capsys: pytest.CaptureFixture[str], registry: dict, partial: bool
    ) -> None:
        """Flag the workflow registry partial when GitHub holds more entries than the one fetched page returned.

        The registry was fetched at GitHub's default page of 30 with ``total_count`` dropped, so a repository with more
        registered workflows (YAML files plus ``dynamic/*`` entries) silently lost the rest — including the code-
        scanning default-setup entry Axis 5 checkpoint 4 credits.
        """
        # Arrange
        (group1_dir / "ci_workflows.json").write_text(json.dumps(registry), encoding="utf-8")

        # Act
        envelope = _run(data_file, group1_dir, capsys)

        # Assert
        assert _records(data_file)["ci_workflows"]["partial"] is partial
        assert ("ci_workflows" in envelope["partial"]) is partial

    def test_full_page_of_runs_never_lowers_confidence(
        self, data_file: Path, group1_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Keep a full page of 100 CI runs out of the truncation count.

        The pass rate samples its newest 20 counted runs. As a capped Group 1 dataset, every active repository's runs
        were flagged partial and dropped the envelope to 0.88 while the rubric called a full page "never a degrader". A
        page that leaves fewer than 20 counted runs is a notes item for the scorer (``runs_sampled`` beside
        ``runs_fetched``), not a dataset truncation.
        """
        # Arrange
        runs = {"type": "ci_runs", "branch": "main", "records": 100, "data": [{"conclusion": "success"}] * 100}
        data_file.write_text(json.dumps(runs) + "\n", encoding="utf-8")

        # Act
        envelope = _run(data_file, group1_dir, capsys)

        # Assert
        assert envelope["confidence"] == 0.95
        assert "ci_runs" not in envelope["partial"]

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
    def test_coverage_confidence_penalises_gaps(self, required_partial: int, missing: int, expected: float) -> None:
        """Never report the complete-file confidence for a file missing required datasets.

        Truncated datasets lower confidence stepwise, one missing required dataset caps it, and many missing ones hit
        the 0.40 floor.
        """
        # Arrange
        coverage = avd.Coverage(10, [], [f"d{i}" for i in range(missing)], [], required_partial)

        # Act
        confidence = avd.coverage_confidence(coverage)

        # Assert
        assert confidence == expected


class TestGroup2Expectations:
    """Which Group 2 records are required, judged from what the repository listing proves exists."""

    def test_missing_branch_status_shows_group2_never_ran(
        self, data_file: Path, group1_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Report Group 2's branch-status and CI-runs records as missing when DATA_FILE holds no Group 2 record at all.

        The public branches and runs endpoints answer on every run, so their records are the canary: without them Group
        2 was skipped or queried a wrong default branch, and every content-based checkpoint downstream would read as
        absent.
        """
        # Act
        rc = _cli(data_file, group1_dir)

        # Assert
        envelope = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert rc == 0
        assert envelope["status"] == "partial"
        assert envelope["missing_required"] == ["ci_runs", "default_branch_status"]

    @pytest.mark.parametrize(
        ("root", "group2", "missing"),
        [
            pytest.param(["README.md"], {}, ["readme_content"], id="readme-listed-not-fetched"),
            pytest.param(["README.md"], {"readme_content": _TEXT}, [], id="readme-listed-and-fetched"),
            pytest.param(["LICENSE"], {}, [], id="no-readme-in-repo"),
            pytest.param([".github"], {}, ["github_dir"], id="github-dir-listed-not-fetched"),
            pytest.param(["docs"], {}, ["docs_dir"], id="docs-dir-listed-not-fetched"),
            pytest.param(
                [".github"],
                {"github_dir": ["workflows", "CODEOWNERS", "dependabot.yml"]},
                ["codeowners_text", "workflows_list", "workflow_files", "dependabot_config"],
                id="github-dir-entries-not-fetched",
            ),
            pytest.param(["CONTRIBUTING.md"], {}, ["contributing_text"], id="contributing-listed-not-fetched"),
            pytest.param(
                [".github"],
                {"github_dir": ["SECURITY.md", "CONTRIBUTING.md"]},
                ["contributing_text", "security_text"],
                id="community-files-in-github-dir-not-fetched",
            ),
            pytest.param(["CHANGELOG.md"], {}, ["changelog_headings"], id="changelog-listed-not-fetched"),
            pytest.param(["changes", "news"], {}, [], id="fragment-dirs-are-not-changelogs"),
            pytest.param(["changelog"], {}, [], id="bare-changelog-may-be-a-fragment-dir"),
            pytest.param(
                [".github"],
                {"github_dir": ["dependabot.yaml"]},
                ["dependabot_config"],
                id="dependabot-yaml-listed-not-fetched",
            ),
            pytest.param(["security", "contributing"], {}, [], id="policy-named-dirs-are-not-policy-files"),
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
    def test_group2_record_required_only_when_listing_shows_it(
        self,
        data_file: Path,
        group1_dir: Path,
        capsys: pytest.CaptureFixture[str],
        root: list[str],
        group2: dict[str, object],
        missing: list[str],
    ) -> None:
        """Report a Group 2 record as missing only when the repository listing proves the file exists.

        Content is expected only for a listed file: a README or policy file in the root, a ``.yml``/``.yaml``
        workflow, or CODEOWNERS wherever GitHub reads it. A ``.github/workflows/`` directory holding only a README once
        flagged ``workflow_files`` missing and capped the envelope at 0.70 although nothing could be fetched.
        """
        # Arrange
        (group1_dir / "root_contents.json").write_text(json.dumps(root), encoding="utf-8")
        _write_group2(data_file, group2)

        # Act
        envelope = _run(data_file, group1_dir, capsys)

        # Assert
        assert envelope["missing_required"] == missing
        assert envelope["status"] == ("partial" if missing else "done")


class TestInputLimits:
    """Size caps and argument validation applied before anything is written."""

    def test_oversized_group1_file_counts_as_failed_fetch(
        self,
        data_file: Path,
        group1_dir: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Treat a Group 1 file over the size cap as a failed fetch: warn, list it missing, still assemble the rest.

        The file holds repository-controlled data; reading it whole would let one repository exhaust memory, while
        aborting the run would lose every other dataset.
        """
        # Arrange — cap above every other fixture file and the Group 2 canaries, below the one oversized dataset
        monkeypatch.setattr(avd, "_MAX_INPUT_CHARS", 1000)
        (group1_dir / "open_issues.json").write_text(json.dumps([{"number": n} for n in range(200)]), encoding="utf-8")

        # Act
        envelope = _run(data_file, group1_dir, capsys)

        # Assert
        assert "open_issues" in envelope["missing_required"]
        assert "open_issues" not in _records(data_file)

    def test_oversized_data_file_exits_1_and_keeps_it(
        self,
        data_file: Path,
        group1_dir: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Exit 1 and leave DATA_FILE untouched when the records already in it exceed the size cap.

        Silently dropping them would score a README or CI history that Group 2 did fetch as absent.
        """
        # Arrange
        monkeypatch.setattr(avd, "_MAX_INPUT_CHARS", 64)
        oversized = json.dumps({"type": "readme_content", "data": "x" * 100}) + "\n"
        data_file.write_text(oversized, encoding="utf-8")

        # Act
        rc = _cli(data_file, group1_dir)

        # Assert
        assert rc == 1
        assert "exceeds 64 characters" in capsys.readouterr().err
        assert data_file.read_text(encoding="utf-8") == oversized

    def test_missing_group1_dir_exits_1(
        self, tmp_path: Path, data_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Exit 1 without writing DATA_FILE when the Group 1 directory does not exist.

        A wrong directory means Group 1 never ran; assembling from nothing would score an empty repository.
        """
        # Act
        rc = _cli(data_file, tmp_path / "absent")

        # Assert
        assert rc == 1
        assert "Group 1 directory not found" in capsys.readouterr().err
        assert not data_file.exists()

    @pytest.mark.parametrize("repo", ["no-slash", "o/r;rm"])
    def test_invalid_repo_rejected(self, data_file: Path, group1_dir: Path, repo: str) -> None:
        """Reject an owner/repo slug outside the allowed character set before writing anything.

        The slug reaches record metadata and later shell commands, so a missing slash or a shell metacharacter stops at
        argument parsing (exit 2).
        """
        # Act
        with pytest.raises(SystemExit) as exc:
            _cli(data_file, group1_dir, repo)

        # Assert
        assert exc.value.code == 2
        assert not data_file.exists()
