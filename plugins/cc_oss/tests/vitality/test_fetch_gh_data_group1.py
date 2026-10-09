"""Tests for ``bin/fetch_gh_data_group1.py``.

Arg-parsing tests run without any subprocess calls. The happy-path and individual-failure tests monkeypatch
``subprocess.run`` and ``which`` so no real ``gh`` invocation occurs.
"""

from __future__ import annotations

from pathlib import Path

import assemble_vitality_data as avd
import fetch_gh_data_group1 as fgd
import pytest

_CUTOFFS = fgd.Cutoffs(
    three_years="2023-01-01",
    days_30="2024-07-01T00:00:00Z",
    days_90="2024-06-01T00:00:00Z",
    days_180="2024-03-01T00:00:00Z",
)


class _FakeCompleted:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        """Store the status and output returned by a fake GitHub CLI command."""
        self.returncode = returncode
        self.stdout = stdout


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        pytest.param(["--output-dir", "out"], "--repo required", id="missing-repo"),
        pytest.param(["--repo", "owner/repo"], "--output-dir required", id="missing-output-dir"),
        pytest.param(["--unknown"], "unknown arg", id="unrecognized-flag"),
        pytest.param([], "--repo required", id="no-args"),
    ],
)
def test_invalid_args_exit_1_with_stderr_message(
    capsys: pytest.CaptureFixture[str], argv: list[str], message: str
) -> None:
    """A missing ``--repo``, a missing ``--output-dir``, an unknown flag or no args → exit 1 naming the problem."""
    rc = fgd.main(argv)
    assert rc == 1
    assert message in capsys.readouterr().err


def test_build_datasets_returns_20_entries() -> None:
    """Return exactly 20 (name, args) tuples; CI runs are a Group 2 fetch scoped to the default branch."""
    datasets = fgd._build_datasets("o/r", _CUTOFFS)
    assert len(datasets) == 20
    names = [name for name, _ in datasets]
    assert "open_issues" in names
    assert "commits_50" in names


@pytest.mark.parametrize(
    "expected_name",
    [
        "open_issues",
        "closed_issues",
        "open_prs",
        "closed_prs",
        "commits",
        "releases",
        "contributor_stats",
        "root_contents",
        "repo_metadata",
        "dependabot_alerts",
        "secret_scanning_alerts",
        "fork_dates",
        "all_issues",
        "all_prs",
        "discussions",
        "responsiveness_gql",
        "review_coverage_gql",
        "ci_workflows",
        "merged_prs_90d",
        "commits_50",
    ],
)
def test_build_datasets_includes_expected_dataset_names(expected_name: str) -> None:
    datasets = fgd._build_datasets("owner/repo", _CUTOFFS)
    assert expected_name in {name for name, _ in datasets}


def test_build_datasets_commands_include_repo_and_cutoffs() -> None:
    datasets = dict(fgd._build_datasets("owner/repo", _CUTOFFS))
    assert datasets["open_issues"][2:4] == ["-R", "owner/repo"]
    assert "closed:>=2024-07-01T00:00:00Z" in datasets["closed_issues"]
    assert "closed:>=2024-07-01T00:00:00Z" in datasets["closed_prs"]
    assert "merged:>=2024-06-01T00:00:00Z" in datasets["merged_prs_90d"]
    assert "repos/owner/repo/commits?per_page=100" in datasets["commits"]
    assert "-f" in datasets["discussions"]
    assert "owner=owner" in datasets["discussions"]
    assert "repo=repo" in datasets["discussions"]


def test_successful_fetch_writes_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """All gh calls succeed → one JSON file per dataset written, exit 0."""
    monkeypatch.setattr(
        fgd.subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(returncode=0, stdout="[]"),
    )
    monkeypatch.setattr(fgd, "which", lambda _: "/fake/gh")
    rc = fgd.main(["--repo", "owner/repo", "--output-dir", str(tmp_path)])
    assert rc == 0
    json_files = list(tmp_path.glob("*.json"))
    assert len(json_files) == 20


def test_individual_failure_nonfatal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """All gh calls fail → empty files written, warnings on stderr, exit 0."""
    monkeypatch.setattr(
        fgd.subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(returncode=1, stdout=""),
    )
    monkeypatch.setattr(fgd, "which", lambda _: "/fake/gh")
    rc = fgd.main(["--repo", "owner/repo", "--output-dir", str(tmp_path)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "failed (non-fatal)" in err
    empty_files = [f for f in tmp_path.glob("*.json") if f.read_text() == ""]
    assert len(empty_files) == 20


def test_successful_fetch_file_content(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Successful gh call → file content equals gh stdout."""
    monkeypatch.setattr(
        fgd.subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(returncode=0, stdout='[{"sha":"abc"}]'),
    )
    monkeypatch.setattr(fgd, "which", lambda _: "/fake/gh")
    fgd.main(["--repo", "owner/repo", "--output-dir", str(tmp_path)])
    assert (tmp_path / "open_issues.json").read_text() == '[{"sha":"abc"}]'


def test_custom_cutoffs_accepted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Explicit cutoff flags accepted without error."""
    monkeypatch.setattr(
        fgd.subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(returncode=0, stdout="[]"),
    )
    monkeypatch.setattr(fgd, "which", lambda _: "/fake/gh")
    rc = fgd.main(
        [
            "--repo",
            "owner/repo",
            "--output-dir",
            str(tmp_path),
            "--cutoff-3y",
            "2023-01-01",
            "--cutoff-30d",
            "2024-07-01T00:00:00Z",
            "--cutoff-90d",
            "2024-06-01T00:00:00Z",
            "--cutoff-180d",
            "2024-03-01T00:00:00Z",
        ]
    )
    assert rc == 0


def test_each_omitted_cutoff_defaults_on_its_own(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Default every cutoff the caller omits, even when another cutoff was passed.

    Defaults used to apply only when ``--cutoff-3y`` was missing, so passing it alone left the 90-day merged search as
    an empty ``merged:>=`` qualifier; the new 30-day closed-PR window must not repeat that.
    """
    # Arrange
    commands: list[list[str]] = []

    def _run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        """Record each gh command and answer it with an empty list."""
        commands.append(cmd)
        return _FakeCompleted(returncode=0, stdout="[]")

    monkeypatch.setattr(fgd.subprocess, "run", _run)
    monkeypatch.setattr(fgd, "which", lambda _: "/fake/gh")

    # Act
    fgd.main(["--repo", "owner/repo", "--output-dir", str(tmp_path), "--cutoff-3y", "2023-01-01"])

    # Assert
    searches = [cmd[cmd.index("--search") + 1] for cmd in commands if "--search" in cmd]
    assert sorted(search.split(":>=")[0] for search in searches) == ["closed", "closed", "merged"]
    assert all(search.split(":>=")[1] for search in searches)


def test_gh_missing_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Return None → FileNotFoundError propagates."""
    monkeypatch.setattr(fgd, "which", lambda _: None)
    with pytest.raises(FileNotFoundError, match="gh"):
        fgd.main(["--repo", "owner/repo", "--output-dir", str(tmp_path)])


@pytest.mark.parametrize(
    "fragment",
    [
        pytest.param(
            "pullRequests(first:30,states:MERGED,orderBy:{field:UPDATED_AT,direction:DESC})", id="newest-first"
        ),
        pytest.param("reviews(states:APPROVED,first:10)", id="nested-reviews-bounded"),
    ],
)
def test_review_coverage_query_paginates_every_connection(fragment: str) -> None:
    """Bound every connection in the review-coverage query so GitHub accepts it.

    GitHub rejects a GraphQL query whose nested ``reviews`` connection lacks ``first``/``last``; ``gh`` then exits 1,
    the Group 1 file is left empty and review coverage is lost on every run. ``last`` with DESC order also selected the
    oldest merged PRs instead of the newest.
    """
    # Act
    query = fgd._REVIEW_COVERAGE_QUERY

    # Assert
    assert fragment in query


@pytest.mark.parametrize(
    ("dataset", "fragment"),
    [
        pytest.param("open_prs", ",author", id="open-prs-author-for-bot-filter"),
        pytest.param("closed_prs", ",author", id="closed-prs-author-for-bot-filter"),
        pytest.param("repo_metadata", "description", id="description-for-abandonment-check"),
        pytest.param("repo_metadata", ",archived,", id="archived-flag-for-abandonment-check"),
        pytest.param("closed_prs", "closed:>=2024-07-01T00:00:00Z", id="closed-prs-in-30d-closing-window"),
        pytest.param("closed_issues", "closed:>=2024-07-01T00:00:00Z", id="closed-issues-in-30d-closing-window"),
        pytest.param("ci_workflows", "{name, path, state}", id="workflow-paths-for-dynamic-scanning"),
        pytest.param("ci_workflows", "actions/workflows?per_page=100", id="workflow-registry-full-page"),
        pytest.param("ci_workflows", "total_count", id="workflow-registry-total-for-truncation"),
        pytest.param("responsiveness_gql", "comments(first:10)", id="first-human-event-reachable"),
        pytest.param("responsiveness_gql", "author{login __typename}", id="responsiveness-bot-typename"),
        pytest.param("review_coverage_gql", "author{login __typename}", id="review-coverage-bot-typename"),
    ],
)
def test_group1_fetches_the_fields_rubric_checkpoints_need(dataset: str, fragment: str) -> None:
    """Request the fields that make rubric checkpoints decidable instead of indeterminate.

    PR authors let Axis 4 drop bot PRs, the description and ``archived`` flag feed the Axis 2 abandonment override, the
    30-day closing window keeps old issues and PRs closed this month in the closed lists, workflow paths prove an active
    code-scanning default setup (read from a 100-entry registry page whose ``total_count`` exposes any entry past it —
    the default page of 30 hid them silently), ten events per item let Axis 1 find the first human non-author response
    past bot comments, and ``__typename`` marks bot accounts whose GraphQL login lacks the ``[bot]`` suffix.
    """
    # Arrange
    datasets = dict(fgd._build_datasets("o/r", _CUTOFFS))

    # Act
    command = " ".join(datasets[dataset])

    # Assert
    assert fragment in command


def test_closed_issue_limit_is_the_search_ceiling_the_assembler_caps_at() -> None:
    """Ask the closed-issue search for exactly the cap the assembler flags as truncated.

    ``gh issue list --search`` stops at GitHub's 1000-result search ceiling, so a ``--limit`` of 1001 could never be
    reached and a month with more closures than that read as a complete list.
    """
    # Arrange
    command = dict(fgd._build_datasets("o/r", _CUTOFFS))["closed_issues"]

    # Act
    limit = int(command[command.index("--limit") + 1])

    # Assert
    assert limit == avd.SEARCH_RESULT_CAP == 1000
