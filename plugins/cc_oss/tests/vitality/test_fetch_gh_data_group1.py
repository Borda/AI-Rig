"""Tests for ``bin/fetch_gh_data_group1.py``.

Argument handling, dataset construction and the GraphQL query text are checked without any subprocess. The fan-out tests
patch ``subprocess.run`` and ``which`` so no real ``gh`` invocation occurs.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import assemble_vitality_data as avd
import fetch_gh_data_group1 as fgd
import pytest
from _process import done

_CUTOFFS = fgd.Cutoffs(
    three_years="2023-01-01",
    days_30="2024-07-01T00:00:00Z",
    days_90="2024-06-01T00:00:00Z",
    days_180="2024-03-01T00:00:00Z",
)


def _timeout_on_contents(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
    """Hang on the root-contents listing and answer every other dataset with an empty list."""
    if "repos/owner/repo/contents" in cmd:
        raise subprocess.TimeoutExpired(cmd, 90)
    return done(stdout="[]")


@pytest.fixture
def gh_run() -> Iterator[mock.MagicMock]:
    """Patch ``which`` to find ``gh`` and ``subprocess.run`` to a mock answering ``[]``; tune ``.return_value``."""
    with (
        mock.patch.object(fgd, "which", autospec=True, return_value="/fake/gh"),
        mock.patch.object(fgd.subprocess, "run", autospec=True, return_value=done(stdout="[]")) as run,
    ):
        yield run


@pytest.fixture
def base_argv(tmp_path: Path) -> list[str]:
    """Return the minimal valid command line: ``--repo owner/repo --output-dir <tmp_path>``."""
    return ["--repo", "owner/repo", "--output-dir", str(tmp_path)]


@pytest.fixture
def datasets() -> dict[str, list[str]]:
    """Return the ``gh`` command of every dataset built for ``owner/repo``, keyed by dataset name."""
    return dict(fgd._build_datasets("owner/repo", _CUTOFFS))


class TestInvalidArgs:
    """Main: bad command lines are refused before any ``gh`` call."""

    @pytest.mark.parametrize(
        ("argv", "message"),
        [
            pytest.param(["--output-dir", "out"], "--repo required", id="missing-repo"),
            pytest.param(["--repo", "owner/repo"], "--output-dir required", id="missing-output-dir"),
            pytest.param(["--unknown"], "unknown arg", id="unrecognized-flag"),
            pytest.param([], "--repo required", id="no-args"),
        ],
    )
    def test_exits_1_naming_the_problem(
        self, capsys: pytest.CaptureFixture[str], argv: list[str], message: str
    ) -> None:
        """A missing ``--repo``, a missing ``--output-dir``, an unknown flag or no args → exit 1 naming the problem.

        The caller is a skill step that reads stderr, so the message must say which argument to fix.
        """
        rc = fgd.main(argv)
        assert rc == 1
        assert message in capsys.readouterr().err

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["--repo", "./r"], id="repo-dot-owner"),
            pytest.param(["--repo", "o/.."], id="repo-dotdot-name"),
            pytest.param(["--repo", "o/r", "--cutoff-30d", "2024-01-01 repo:other/x"], id="cutoff-extra-qualifier"),
            pytest.param(["--repo", "o/r", "--cutoff-3y", "yesterday"], id="cutoff-not-a-date"),
            pytest.param(["--repo", "o/r", "--cutoff-90d", "2024-01-01T00:00:00Z\n"], id="cutoff-trailing-newline"),
        ],
    )
    def test_refuses_path_and_search_injection(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], gh_run: mock.MagicMock, argv: list[str]
    ) -> None:
        """A dot path segment or a cutoff that could extend the search query is refused before any gh call.

        Both values are spliced into a ``gh`` argument: ``repos/<owner>/<repo>`` for the API path and
        ``closed:>=<cutoff>`` for the search, so a ``..`` segment or an embedded qualifier would reach a different
        repository.
        """
        rc = fgd.main([*argv, "--output-dir", str(tmp_path)])
        assert rc == 1
        assert "fetch_gh_data_group1:" in capsys.readouterr().err
        assert (list(tmp_path.iterdir()), gh_run.call_count) == ([], 0)

    @mock.patch("fetch_gh_data_group1.which", autospec=True, return_value=None)
    def test_missing_gh_binary_raises(self, which: mock.MagicMock, base_argv: list[str]) -> None:
        """No ``gh`` on PATH → ``FileNotFoundError`` naming it propagates instead of a silent empty run."""
        with pytest.raises(FileNotFoundError, match="gh"):
            fgd.main(base_argv)


class TestBuildDatasets:
    """_build_datasets: the 20 ``gh`` commands of the Group 1 fan-out."""

    def test_builds_twenty_datasets(self) -> None:
        """Return exactly 20 (name, args) tuples; CI runs are a Group 2 fetch scoped to the default branch.

        The assembler reads one file per dataset, so a silently dropped or duplicated entry shows up as a missing or
        stale input file.
        """
        assert len(fgd._build_datasets("o/r", _CUTOFFS)) == 20

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
    def test_includes_dataset(self, datasets: dict[str, list[str]], expected_name: str) -> None:
        """Each dataset the assembler consumes is built under its documented name."""
        assert expected_name in datasets

    def test_scopes_open_issues_to_the_repo(self, datasets: dict[str, list[str]]) -> None:
        """``gh issue list`` receives ``-R owner/repo`` as its third and fourth arguments."""
        assert datasets["open_issues"][2:4] == ["-R", "owner/repo"]

    @pytest.mark.parametrize(
        ("dataset", "element"),
        [
            pytest.param("closed_issues", "closed:>=2024-07-01T00:00:00Z", id="closed-issues-30d-cutoff"),
            pytest.param("closed_prs", "closed:>=2024-07-01T00:00:00Z", id="closed-prs-30d-cutoff"),
            pytest.param("merged_prs_90d", "merged:>=2024-06-01T00:00:00Z", id="merged-prs-90d-cutoff"),
            pytest.param("commits", "repos/owner/repo/commits?per_page=100", id="commits-api-path"),
            pytest.param("discussions", "-f", id="discussions-field-flag"),
            pytest.param("discussions", "owner=owner", id="discussions-owner-field"),
            pytest.param("discussions", "repo=repo", id="discussions-repo-field"),
        ],
    )
    def test_command_carries_repo_and_cutoff(self, datasets: dict[str, list[str]], dataset: str, element: str) -> None:
        """The repository and each cutoff reach the command as a whole argument, not as a loose substring."""
        assert element in datasets[dataset]

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
    def test_requests_fields_rubric_checkpoints_need(
        self, datasets: dict[str, list[str]], dataset: str, fragment: str
    ) -> None:
        """Request the fields that make rubric checkpoints decidable instead of indeterminate.

        PR authors let Axis 4 drop bot PRs, the description and ``archived`` flag feed the Axis 2 abandonment override,
        the 30-day closing window keeps old issues and PRs closed this month in the closed lists, workflow paths prove
        an active code-scanning default setup (read from a 100-entry registry page whose ``total_count`` exposes any
        entry past it — the default page of 30 hid them silently), ten events per item let Axis 1 find the first human
        non-author response past bot comments, and ``__typename`` marks bot accounts whose GraphQL login lacks the
        ``[bot]`` suffix.
        """
        assert fragment in " ".join(datasets[dataset])

    @pytest.mark.parametrize(
        "fragment",
        [
            pytest.param(
                "pullRequests(first:30,states:MERGED,orderBy:{field:UPDATED_AT,direction:DESC})", id="newest-first"
            ),
            pytest.param("reviews(states:APPROVED,first:10)", id="nested-reviews-bounded"),
        ],
    )
    def test_review_coverage_query_bounds_every_connection(self, fragment: str) -> None:
        """Bound every connection in the review-coverage query so GitHub accepts it.

        GitHub rejects a GraphQL query whose nested ``reviews`` connection lacks ``first``/``last``; ``gh`` then exits
        1, the Group 1 file is left empty and review coverage is lost on every run. ``last`` with DESC order also
        selected the oldest merged PRs instead of the newest.
        """
        assert fragment in fgd._REVIEW_COVERAGE_QUERY

    def test_closed_issue_limit_equals_assembler_cap(self, datasets: dict[str, list[str]]) -> None:
        """Ask the closed-issue search for exactly the cap the assembler flags as truncated.

        ``gh issue list --search`` stops at GitHub's 1000-result search ceiling, so a ``--limit`` of 1001 could never be
        reached and a month with more closures than that read as a complete list.
        """
        command = datasets["closed_issues"]
        assert int(command[command.index("--limit") + 1]) == avd.SEARCH_RESULT_CAP == 1000


class TestMain:
    """Main: run every dataset through ``gh`` and write one file per dataset."""

    @pytest.mark.parametrize(
        "stdout", [pytest.param("[]", id="empty-list"), pytest.param('[{"sha":"abc"}]', id="payload")]
    )
    def test_writes_gh_stdout_for_every_dataset(
        self, gh_run: mock.MagicMock, base_argv: list[str], tmp_path: Path, stdout: str
    ) -> None:
        """All gh calls succeed → exit 0 and one JSON file per dataset holding gh's stdout verbatim.

        Downstream assembly reads these files as-is, so the content must not be rewrapped or trimmed on the way out.
        """
        gh_run.return_value = done(stdout=stdout)
        rc = fgd.main(base_argv)
        files = list(tmp_path.glob("*.json"))
        assert (rc, len(files), {file.read_text() for file in files}) == (0, 20, {stdout})

    def test_failed_calls_are_nonfatal(
        self, gh_run: mock.MagicMock, base_argv: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """All gh calls fail → empty files written, warnings on stderr, exit 0.

        One flaky dataset must not abort the whole vitality run; the assembler treats an empty file as missing data.
        """
        gh_run.return_value = done(returncode=1)
        rc = fgd.main(base_argv)
        empty_files = [file for file in tmp_path.glob("*.json") if file.read_text() == ""]
        assert (rc, len(empty_files), "failed (non-fatal)" in capsys.readouterr().err) == (0, 20, True)

    def test_timed_out_dataset_leaves_the_others(
        self, gh_run: mock.MagicMock, base_argv: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """One hung gh call is recorded as failed while every other dataset is still written.

        ``future.result()`` re-raised the timeout, so a single slow dataset used to abort the whole fan-out with a
        traceback.
        """
        gh_run.side_effect = _timeout_on_contents
        rc = fgd.main(base_argv)
        assert rc == 0
        assert "root_contents timed out" in capsys.readouterr().err
        assert ((tmp_path / "root_contents.json").read_text(), (tmp_path / "open_issues.json").read_text()) == (
            "",
            "[]",
        )

    @pytest.mark.usefixtures("gh_run")
    def test_accepts_explicit_cutoffs(self, base_argv: list[str]) -> None:
        """Explicit cutoff flags are accepted without error."""
        cutoffs = ["--cutoff-3y", "2023-01-01", "--cutoff-30d", "2024-07-01T00:00:00Z"]
        cutoffs += ["--cutoff-90d", "2024-06-01T00:00:00Z", "--cutoff-180d", "2024-03-01T00:00:00Z"]
        assert fgd.main([*base_argv, *cutoffs]) == 0

    def test_defaults_each_omitted_cutoff_on_its_own(self, gh_run: mock.MagicMock, base_argv: list[str]) -> None:
        """Default every cutoff the caller omits, even when another cutoff was passed.

        Defaults used to apply only when ``--cutoff-3y`` was missing, so passing it alone left the 90-day merged search
        as an empty ``merged:>=`` qualifier; the new 30-day closed-PR window must not repeat that.
        """
        # Act
        fgd.main([*base_argv, "--cutoff-3y", "2023-01-01"])

        # Assert
        commands = [call.args[0] for call in gh_run.call_args_list]
        searches = [cmd[cmd.index("--search") + 1] for cmd in commands if "--search" in cmd]
        assert sorted(search.split(":>=")[0] for search in searches) == ["closed", "closed", "merged"]
        assert all(search.split(":>=")[1] for search in searches)
