#!/usr/bin/env python
"""fetch_gh_data_group1.py — Group 1 parallel gh API data fetch for oss:gh-scraper.

Fetches all GitHub REST + GraphQL data sources with no inter-dependency
(issues, PRs, releases, commits, contributor stats, security alerts,
forks, stargazers, workflows, etc.). Writes one JSON file per dataset
under the output directory; downstream scorers treat missing files as
"data unavailable".

Individual fetch failures are non-fatal — printed to stderr with a
warning prefix; the empty file signals "tried, failed" to scorers.

Usage:
    fetch_gh_data_group1.py --repo <owner/repo> --output-dir <path>
                            [--cutoff-3y <YYYY-MM-DD>]
                            [--cutoff-30d <iso>]
                            [--cutoff-90d <iso>]
                            [--cutoff-180d <iso>]

Exit: 0 on success (warnings on individual failures); 1 on bad args.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from shutil import which

#: GitHub allows owner and repo names matching [A-Za-z0-9._-]; we enforce the
#: combined ``owner/repo`` shape strictly to defuse URL-path injection (A03:2021).
_REPO_RE = re.compile(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$")

#: GraphQL query listing the 100 most recently updated discussions with number, title, state and creation time.
_DISCUSSIONS_QUERY = (
    "query($owner:String!,$repo:String!){"
    "repository(owner:$owner,name:$repo){"
    "discussions(first:100,orderBy:{field:UPDATED_AT,direction:DESC}){"
    "nodes { number title closed createdAt }"
    "}}}"
)
#: GraphQL query fetching recent issues and PRs with their first comments and reviews to measure response times.
#: Ten events per item, not one: the rubric needs the first event by a human other than the author, and both the
#: author's own follow-ups and bot comments (coverage, CLA, security scanners, AI reviewers) often come first — with
#: ``first:1`` such an item read as unresponded or as answered in seconds. ``__typename`` marks bot accounts: GraphQL
#: returns an app's login without its ``[bot]`` suffix, so the login alone cannot tell ``codecov`` from a person.
_RESPONSIVENESS_QUERY = (
    "query($owner:String!,$repo:String!){"
    "repository(owner:$owner,name:$repo){"
    "issues(first:20,orderBy:{field:CREATED_AT,direction:DESC},states:OPEN){"
    "nodes{number createdAt author{login __typename} comments(first:10){nodes{createdAt author{login __typename}}}}}"
    "pullRequests(first:20,orderBy:{field:CREATED_AT,direction:DESC},states:[OPEN,MERGED]){"
    "nodes{number createdAt author{login __typename}"
    " reviews(states:[APPROVED,CHANGES_REQUESTED,COMMENTED],first:10){nodes{createdAt author{login __typename}}}"
    " comments(first:10){nodes{createdAt author{login __typename}}}}}}}"
)
#: GraphQL query fetching the 30 most recently updated merged PRs with their approving reviewers to measure review
#: coverage. ``first`` (not ``last``) because the order is DESC — ``last`` returned the 30 oldest merged PRs. The nested
#: ``reviews`` connection needs its own ``first``: without it GitHub rejects the whole query and the dataset is lost.
_REVIEW_COVERAGE_QUERY = (
    "query($owner:String!,$repo:String!){"
    "repository(owner:$owner,name:$repo){"
    "pullRequests(first:30,states:MERGED,orderBy:{field:UPDATED_AT,direction:DESC}){"
    "nodes{number author{login __typename} reviews(states:APPROVED,first:10){nodes{author{login __typename}}}}}}}"
)


def _resolve(cmd: str) -> str:
    """Resolve a CLI tool to its absolute path.

    Args:
        cmd: Bare executable name (e.g. ``"gh"``).

    Returns:
        Absolute path to the executable.

    Raises:
        FileNotFoundError: If ``cmd`` is not present on ``PATH``.

    Examples:
        >>> import shutil
        >>> _resolve("gh") == shutil.which("gh") or shutil.which("gh") is None
        True
    """
    p = which(cmd)
    if p is None:
        raise FileNotFoundError(f"executable not found on PATH: {cmd}")
    return p


def _fetch_one(gh: str, name: str, cmd_args: list[str], output_dir: Path) -> tuple[str, bool]:
    """Run one gh command and write result to ``<output_dir>/<name>.json``.

    Args:
        gh: Absolute path to the gh binary.
        name: Dataset name (used as filename stem).
        cmd_args: Arguments passed after the gh binary.
        output_dir: Directory to write the output file.

    Returns:
        ``(name, True)`` on success; ``(name, False)`` on failure.

    Examples:
        No doctest — subprocess-dependent; covered by pytest.
    """
    out_path = output_dir / f"{name}.json"
    result = subprocess.run(  # noqa: S603
        [gh, *cmd_args],
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )
    if result.returncode == 0:
        out_path.write_text(result.stdout, encoding="utf-8")
        return name, True
    out_path.write_text("", encoding="utf-8")
    print(f"⚠ fetch_gh_data_group1: {name} failed (non-fatal)", file=sys.stderr)
    return name, False


@dataclass(frozen=True)
class Cutoffs:
    """Lookback boundaries the time-windowed fetches search from.

    Attributes:
        three_years: ISO date (``YYYY-MM-DD``) for the 3-year lookback (reserved; closed issues use ``days_30``).
        days_30: ISO datetime for the 30-day closed-issue and closed-PR windows.
        days_90: ISO datetime for the 90-day merged-PR window.
        days_180: ISO datetime for the 180-day lookback (reserved).
    """

    three_years: str
    days_30: str
    days_90: str
    days_180: str


def _build_datasets(owner_repo: str, cutoffs: Cutoffs) -> list[tuple[str, list[str]]]:
    """Build the full list of ``(name, gh_command_args)`` for all datasets.

    ``closed_issues`` and ``closed_prs`` cover the 30-day closing window only: the rubric reads nothing older from them,
    and a list ordered by creation and cut at its cap silently dropped old items closed this month while their counts
    read as complete. CI runs are fetched by Group 2, which knows the default branch they are scoped to.

    Args:
        owner_repo: ``"owner/repo"`` string.
        cutoffs: Lookback boundaries of the windowed searches.

    Returns:
        List of ``(name, cmd_args)`` tuples, one per dataset.

    Examples:
        >>> cut = Cutoffs("2023-01-01", "2023-03-02T00:00:00Z", "2023-01-01T00:00:00Z", "2022-07-01T00:00:00Z")
        >>> ds = _build_datasets("o/r", cut)
        >>> len(ds) == 20
        True
        >>> ds[0][0]
        'open_issues'
    """
    owner, _, repo = owner_repo.partition("/")
    return [
        (
            "open_issues",
            [
                "issue",
                "list",
                "-R",
                owner_repo,
                "--state",
                "open",
                "--json",
                "number,title,createdAt,updatedAt,labels",
                "--limit",
                "501",
            ],
        ),
        (
            "closed_issues",
            [
                "issue",
                "list",
                "-R",
                owner_repo,
                "--state",
                "closed",
                "--search",
                f"closed:>={cutoffs.days_30}",
                "--json",
                "number,title,createdAt,closedAt",
                "--limit",
                "1000",
            ],
        ),
        (
            "open_prs",
            [
                "pr",
                "list",
                "-R",
                owner_repo,
                "--state",
                "open",
                "--json",
                "number,title,createdAt,updatedAt,reviews,statusCheckRollup,author",
                "--limit",
                "201",
            ],
        ),
        (
            "closed_prs",
            [
                "pr",
                "list",
                "-R",
                owner_repo,
                "--state",
                "closed",
                "--search",
                f"closed:>={cutoffs.days_30}",
                "--json",
                "number,title,createdAt,closedAt,mergedAt,author",
                "--limit",
                "201",
            ],
        ),
        (
            "commits",
            ["api", f"repos/{owner_repo}/commits?per_page=100", "--jq", "[.[].commit.author.date]"],
        ),
        (
            "releases",
            [
                "api",
                f"repos/{owner_repo}/releases?per_page=10",
                "--jq",
                "[.[] | {tag: .tag_name, published: .published_at, downloads: ([.assets[].download_count] | add // 0)}]",
            ],
        ),
        (
            "contributor_stats",
            [
                "api",
                f"repos/{owner_repo}/stats/contributors",
                "--jq",
                "[.[] | {author: .author.login, total: .total, weeks: .weeks}]",
            ],
        ),
        (
            "root_contents",
            ["api", f"repos/{owner_repo}/contents", "--jq", "[.[] | .name]"],
        ),
        (
            "repo_metadata",
            [
                "api",
                f"repos/{owner_repo}",
                "--jq",
                "{default_branch,description,archived,has_issues,has_projects,allow_forking,stargazers_count,forks_count,subscribers_count,open_issues_count}",
            ],
        ),
        (
            "dependabot_alerts",
            ["api", f"repos/{owner_repo}/dependabot/alerts?state=open&per_page=100"],
        ),
        (
            "secret_scanning_alerts",
            ["api", f"repos/{owner_repo}/secret-scanning/alerts?state=open"],
        ),
        (
            "fork_dates",
            ["api", f"repos/{owner_repo}/forks?sort=newest&per_page=100", "--jq", "[.[] | .created_at]"],
        ),
        (
            "all_issues",
            [
                "issue",
                "list",
                "-R",
                owner_repo,
                "--state",
                "all",
                "--json",
                "number,title,state,labels,createdAt",
                "--limit",
                "200",
            ],
        ),
        (
            "all_prs",
            [
                "pr",
                "list",
                "-R",
                owner_repo,
                "--state",
                "all",
                "--json",
                "number,title,state,createdAt",
                "--limit",
                "100",
            ],
        ),
        (
            "discussions",
            ["api", "graphql", "-f", f"query={_DISCUSSIONS_QUERY}", "-f", f"owner={owner}", "-f", f"repo={repo}"],
        ),
        (
            "responsiveness_gql",
            ["api", "graphql", "-f", f"query={_RESPONSIVENESS_QUERY}", "-f", f"owner={owner}", "-f", f"repo={repo}"],
        ),
        (
            "review_coverage_gql",
            ["api", "graphql", "-f", f"query={_REVIEW_COVERAGE_QUERY}", "-f", f"owner={owner}", "-f", f"repo={repo}"],
        ),
        (
            "ci_workflows",
            [
                "api",
                # one full page plus GitHub's total: the default page of 30 cut the registry silently
                f"repos/{owner_repo}/actions/workflows?per_page=100",
                "--jq",
                "{count: (.workflows | length), total_count, names: [.workflows[].name],"
                " workflows: [.workflows[] | {name, path, state}]}",
            ],
        ),
        (
            "merged_prs_90d",
            [
                "pr",
                "list",
                "-R",
                owner_repo,
                "--state",
                "closed",
                "--search",
                f"merged:>={cutoffs.days_90}",
                "--json",
                "number,createdAt,mergedAt,author",
                "--limit",
                "201",
            ],
        ),
        (
            "commits_50",
            [
                "api",
                f"repos/{owner_repo}/commits?per_page=50",
                "--jq",
                '[.[] | {sha:.sha[:7], message:(.commit.message | split("\\n")[0]), author:(.author.login // .commit.author.name // "unknown"), date:.commit.author.date}]',
            ],
        ),
    ]


#: Accepted value flags and the ``_parse_args`` field each one sets.
_FLAG_FIELDS = {
    "--repo": "owner_repo",
    "--output-dir": "output_dir",
    "--cutoff-3y": "cutoff_3y",
    "--cutoff-30d": "cutoff_30d",
    "--cutoff-90d": "cutoff_90d",
    "--cutoff-180d": "cutoff_180d",
}


def _parse_args(args: list[str]) -> tuple[dict, str | None]:
    """Parse the manual reject-strict argv flags for ``--repo``/``--output-dir``/cutoffs.

    Already reject-strict (unknown arg → error) and enforces required ``--repo``/
    ``--output-dir`` plus the owner/repo regex — kept manual rather than a broad
    ``argparse.parse_args`` call, which would replace this exit-1 contract with
    argparse's exit-2.

    Args:
        args: Argument list to parse (``-h``/``--help`` already handled by the caller).

    Returns:
        ``(fields, None)`` on success, or ``(fields, error_message)`` on the first
        invalid/unknown/missing-required flag. ``fields`` keys: ``owner_repo``,
        ``output_dir``, ``cutoff_3y``, ``cutoff_30d``, ``cutoff_90d``, ``cutoff_180d``.

    Examples:
        >>> fields, error = _parse_args(["--repo", "o/r", "--output-dir", "out", "--cutoff-30d", "2024-01-01"])
        >>> fields["cutoff_30d"], error
        ('2024-01-01', None)
        >>> _parse_args(["--repo", "o/r", "--bogus"])[1]
        "fetch_gh_data_group1: unknown arg '--bogus'"
    """
    fields = dict.fromkeys(_FLAG_FIELDS.values(), "")
    i = 0
    while i < len(args):
        field = _FLAG_FIELDS.get(args[i])
        if field is None:
            return fields, f"fetch_gh_data_group1: unknown arg '{args[i]}'"
        i += 1
        fields[field] = args[i] if i < len(args) else ""
        i += 1

    owner_repo = fields["owner_repo"]
    output_dir = fields["output_dir"]
    if not owner_repo:
        return fields, "fetch_gh_data_group1: --repo required"
    if not _REPO_RE.match(owner_repo):
        return (
            fields,
            f"fetch_gh_data_group1: --repo must match 'owner/repo' (allowed chars: A-Za-z0-9._-), got: {owner_repo!r}",
        )
    if not output_dir:
        return fields, "fetch_gh_data_group1: --output-dir required"

    return fields, None


def _fetch_all(gh: str, datasets: list[tuple[str, list[str]]], out_path: Path) -> int:
    """Fan out ``_fetch_one`` across all datasets and count the written files.

    Cap concurrency at 10 — running every dataset's ``gh api`` call at once easily
    triggers GitHub's secondary rate limits (HTTP 403 "abuse detection")
    which cause silent partial failures across the dataset.

    Args:
        gh: Absolute path to the gh binary.
        datasets: List of ``(name, cmd_args)`` tuples to fetch.
        out_path: Directory each dataset is written into.

    Returns:
        Count of ``.json`` dataset files written to ``out_path``.
    """
    with ThreadPoolExecutor(max_workers=min(len(datasets), 10)) as executor:
        futures = {executor.submit(_fetch_one, gh, name, cmd_args, out_path): name for name, cmd_args in datasets}
        for future in as_completed(futures):
            future.result()

    return sum(1 for f in out_path.iterdir() if f.suffix == ".json" and f.is_file())


def main(argv: list[str] | None = None) -> int:
    """Entry point — mirrors ``fetch_gh_data_group1.sh`` behaviour.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Exit code: 1 on bad args; 0 on success (individual failures non-fatal).

    Examples:
        No doctest — subprocess-dependent; covered by pytest.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    # Honour only ``-h/--help`` via argparse; every other flag flows through the manual
    # loop below, which is already reject-strict (unknown arg → exit 1) and enforces
    # required --repo/--output-dir plus owner/repo regex. A broad parse_args would
    # replace that exit-1 contract with argparse's exit-2 — keep the manual parser.
    if args in (["-h"], ["--help"]):
        argparse.ArgumentParser(
            prog="fetch_gh_data_group1.py",
            description="Group 1 parallel gh API data fetch for oss:gh-scraper.",
        ).parse_args(["-h"])

    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]

    fields, err = _parse_args(args)
    if err:
        print(err, file=sys.stderr)
        return 1

    owner_repo: str = fields["owner_repo"]
    output_dir: str = fields["output_dir"]

    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    # each omitted cutoff defaults on its own — a lone --cutoff-3y no longer leaves an empty `merged:>=` search
    now = datetime.now(tz=timezone.utc)
    cutoffs = Cutoffs(
        three_years=fields["cutoff_3y"] or (now - timedelta(days=1095)).strftime("%Y-%m-%d"),
        days_30=fields["cutoff_30d"] or (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        days_90=fields["cutoff_90d"] or (now - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        days_180=fields["cutoff_180d"] or (now - timedelta(days=180)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    gh = _resolve("gh")
    datasets = _build_datasets(owner_repo, cutoffs)

    count = _fetch_all(gh, datasets, out_path)
    print(f"[fetch_gh_data_group1] wrote {count} dataset files → {output_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
