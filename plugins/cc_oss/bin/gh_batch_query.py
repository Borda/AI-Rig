#!/usr/bin/env python
"""Purpose: Batch many per-item GitHub lookups into a few GraphQL requests.

Scope: Two read-only subcommands replace one-``gh``-call-per-item loops that outgrow a single Bash timeout.
``commit-prs`` maps every commit of a Git range to its associated merged pull requests through the commit field
``associatedPullRequests``. ``pr-files`` lists the changed file paths of several pull requests. Both send one GraphQL
request per batch of aliases (default 25), so 500 commits cost about 20 requests instead of 500. Neither writes to
GitHub, the repository, or any file.

Usage:
    gh_batch_query.py commit-prs --range <git-range> [--repo OWNER/NAME] [--repo-root DIR]
                                 [--offset N | --offset-file F] [--limit N]
    gh_batch_query.py pr-files <number> [<number> ...] [--repo OWNER/NAME] [--repo-root DIR]

Outputs: One JSON object per line on stdout.
  - ``commit-prs``: ``{"commit": sha, "pull_requests": [{number, title, author, base, merge_commit_sha}]}`` with merged
    PRs only; the shape matches the REST ``commits/<sha>/pulls`` loop it replaces.
  - ``pr-files``: ``{"pr": number, "files": [path, ...]}``.
  A record carries ``coverage_gap`` (a reason string) when the lookup cannot be complete: commit unknown to GitHub,
  pull request not found, or more results than one page holds (``pull_requests``/``files`` then hold the first page).
  Callers keep the gap in their evidence instead of claiming complete coverage. Progress and the ``NEXT_OFFSET=<n>``
  continuation hint (``commit-prs`` ranges longer than ``--limit``) go to stderr. With ``--offset-file`` the next
  offset is also saved as ``<range>`` TAB ``<n>`` (``0`` once the range is finished), so re-running the identical
  command continues where the last run stopped; an offset saved for a different range is ignored.

Failure: Exit 1 reports invalid arguments or an unresolvable repository; exit 2 reports a failed git or GraphQL call
(including GraphQL ``errors``). An already-printed record never hides a later failure, so do not treat partial output
as complete after a non-zero exit.

Used by: ``skills/release/SKILL.md`` (Gather: commit-to-PR associations), ``skills/analyse/modes/codemap-signals.md``
(Signal B: changed files of open PRs). Tests in ``tests/test_gh_batch_query.py``.

Arguments:
    --range:      Git revision range for ``git rev-list`` (``commit-prs``); must not start with ``-``.
    --repo:       ``OWNER/NAME``; default is read from ``gh repo view`` in ``--repo-root``.
    --repo-root:  Directory to run git and gh in (default: cwd).
    --offset:     Commits to skip from the start of the range (default 0).
    --offset-file: File holding the resume offset between runs; replaces ``--offset``.
    --limit:      Commits to process in this run (default 500).
    --batch-size: Aliases per GraphQL request (default 25, max 50).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

#: Default and maximum aliases per GraphQL request; larger batches risk GitHub's node and timeout limits.
DEFAULT_BATCH = 25
MAX_BATCH = 50
#: Default commits per run, sized so one run stays well inside the caller's Bash timeout.
DEFAULT_LIMIT = 500
#: Seconds allowed for one ``gh``/``git`` subprocess.
_CALL_TIMEOUT = 90
#: Page size requested for nested lists; more results than this are reported as a coverage gap.
_PAGE = 100

_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class QueryError(RuntimeError):
    """Raised when a git or GitHub call fails; the message names the failing step."""


def build_commit_query(shas: list[str]) -> str:
    """Return one GraphQL document looking up the associated pull requests of every sha.

    Args:
        shas: Full commit hashes; each becomes alias ``c<index>``.

    Returns:
        GraphQL query text taking ``$owner`` and ``$name`` variables.

    Raises:
        ValueError: A value is not a full hexadecimal commit hash (guards against query injection).
    """
    for sha in shas:
        if not _SHA_RE.match(sha):
            raise ValueError(f"not a full commit hash: {sha!r}")
    fields = (
        f"associatedPullRequests(first:{_PAGE}){{pageInfo{{hasNextPage}} nodes{{number title merged baseRefName "
        "mergeCommit{oid} author{login}}}"
    )
    aliases = " ".join(f'c{i}:object(oid:"{sha}"){{...on Commit{{{fields}}}}}' for i, sha in enumerate(shas))
    return f"query($owner:String!,$name:String!){{repository(owner:$owner,name:$name){{{aliases}}}}}"


def parse_commit_response(repository: dict, shas: list[str]) -> list[dict]:
    """Convert a ``build_commit_query`` response into one record per sha, in input order.

    Args:
        repository: The ``data.repository`` object of the GraphQL response.
        shas: The hashes the query was built from.

    Returns:
        Records ``{"commit", "pull_requests"}`` plus ``coverage_gap`` when the lookup is incomplete.
    """
    records: list[dict] = []
    for index, sha in enumerate(shas):
        node = (repository or {}).get(f"c{index}")
        if not node:
            records.append({"commit": sha, "pull_requests": [], "coverage_gap": "commit unknown to GitHub"})
            continue
        assoc = node["associatedPullRequests"]
        pulls = [
            {
                "number": pr["number"],
                "title": pr["title"],
                "author": pr["author"],
                "base": pr["baseRefName"],
                "merge_commit_sha": (pr.get("mergeCommit") or {}).get("oid"),
            }
            for pr in assoc["nodes"]
            if pr.get("merged")
        ]
        record: dict = {"commit": sha, "pull_requests": pulls}
        if assoc["pageInfo"]["hasNextPage"]:
            record["coverage_gap"] = f"more than {_PAGE} associated pull requests"
        records.append(record)
    return records


def build_pr_files_query(numbers: list[int]) -> str:
    """Return one GraphQL document listing the changed files of every pull request number.

    Args:
        numbers: Pull request numbers; each becomes alias ``p<index>``.

    Returns:
        GraphQL query text taking ``$owner`` and ``$name`` variables.
    """
    fields = f"number files(first:{_PAGE}){{pageInfo{{hasNextPage}} nodes{{path}}}}"
    aliases = " ".join(f"p{i}:pullRequest(number:{int(n)}){{{fields}}}" for i, n in enumerate(numbers))
    return f"query($owner:String!,$name:String!){{repository(owner:$owner,name:$name){{{aliases}}}}}"


def parse_pr_files_response(repository: dict, numbers: list[int]) -> list[dict]:
    """Convert a ``build_pr_files_query`` response into one record per pull request, in input order.

    Args:
        repository: The ``data.repository`` object of the GraphQL response.
        numbers: The pull request numbers the query was built from.

    Returns:
        Records ``{"pr", "files"}`` plus ``coverage_gap`` when the list is incomplete or the PR is missing.
    """
    records: list[dict] = []
    for index, number in enumerate(numbers):
        node = (repository or {}).get(f"p{index}")
        if not node:
            records.append({"pr": number, "files": [], "coverage_gap": "pull request not found"})
            continue
        record: dict = {"pr": number, "files": [f["path"] for f in node["files"]["nodes"]]}
        if node["files"]["pageInfo"]["hasNextPage"]:
            record["coverage_gap"] = f"more than {_PAGE} changed files"
        records.append(record)
    return records


def _capture(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run one subprocess and return the finished process; raise ``QueryError`` on timeout or a missing executable."""
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=_CALL_TIMEOUT, check=False)  # noqa: S603
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise QueryError(f"{cmd[0]} {cmd[1] if len(cmd) > 1 else ''} failed: {exc}") from exc


def _run(cmd: list[str], cwd: Path) -> str:
    """Run one subprocess and return stdout; raise ``QueryError`` when it exits non-zero."""
    done = _capture(cmd, cwd)
    if done.returncode != 0:
        raise QueryError(f"{' '.join(cmd[:3])} exited {done.returncode}: {done.stderr.strip()[:300]}")
    return done.stdout


def resolve_repo(repo: str | None, cwd: Path) -> tuple[str, str]:
    """Return ``(owner, name)`` from ``--repo`` or from ``gh repo view`` in ``cwd``.

    Raises:
        ValueError: The value is not ``OWNER/NAME``.
        QueryError: ``gh repo view`` failed.
    """
    slug = repo or _run(["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"], cwd).strip()
    if not _REPO_RE.match(slug):
        raise ValueError(f"repository is not OWNER/NAME: {slug!r}")
    owner, name = slug.split("/", 1)
    return owner, name


def run_graphql(query: str, owner: str, name: str, cwd: Path) -> dict:
    """Send one GraphQL request through ``gh api graphql`` and return ``data.repository``.

    ``gh`` exits non-zero whenever the response carries ``errors`` yet still prints the body, so the body decides:
    ``NOT_FOUND`` errors leave their alias null in ``data`` and the parsers report that item as a coverage gap,
    while any other error fails the whole request.

    Raises:
        QueryError: The call failed, or the response carries a GraphQL error other than ``NOT_FOUND``.
    """
    done = _capture(["gh", "api", "graphql", "-f", f"query={query}", "-f", f"owner={owner}", "-f", f"name={name}"], cwd)
    try:
        body = json.loads(done.stdout)
    except json.JSONDecodeError as exc:
        raise QueryError(f"gh api graphql exited {done.returncode}: {done.stderr.strip()[:300]}") from exc
    errors = body.get("errors") or []
    if any(err.get("type") != "NOT_FOUND" for err in errors) or (done.returncode != 0 and not errors):
        raise QueryError(f"GraphQL failed (exit {done.returncode}): {json.dumps(errors)[:300] or done.stderr[:300]}")
    return (body.get("data") or {}).get("repository") or {}


def _batches(items: list, size: int) -> list[list]:
    """Split ``items`` into consecutive chunks of at most ``size``."""
    return [items[i : i + size] for i in range(0, len(items), size)]


def _load_offset(path: Path, range_spec: str) -> int:
    """Return the offset saved for ``range_spec`` in ``path``; ``0`` if absent, unreadable, or for another range."""
    try:
        saved_range, _, saved_offset = path.read_text(encoding="utf-8").strip().partition("\t")
        return int(saved_offset) if saved_range == range_spec and int(saved_offset) >= 0 else 0
    except (OSError, ValueError):
        return 0


def commit_prs(args: argparse.Namespace) -> int:
    """Print one association record per commit of the range slice; return the process exit code."""
    root = Path(args.repo_root)
    if args.range.startswith("-"):
        print(f"Error: range must not start with '-': {args.range}", file=sys.stderr)
        return 1
    offset = _load_offset(Path(args.offset_file), args.range) if args.offset_file else args.offset
    owner, name = resolve_repo(args.repo, root)
    everything = _run(["git", "rev-list", args.range], root).split()
    window = everything[offset : offset + args.limit]
    print(f"PR associations: commits {offset + 1}-{offset + len(window)} of {len(everything)}", file=sys.stderr)
    for batch in _batches(window, args.batch_size):
        for record in parse_commit_response(run_graphql(build_commit_query(batch), owner, name, root), batch):
            print(json.dumps(record, ensure_ascii=False))
        sys.stdout.flush()
    more = offset + args.limit < len(everything)
    if args.offset_file:
        Path(args.offset_file).write_text(
            f"{args.range}\t{offset + args.limit if more else 0}\n", encoding="utf-8", newline="\n"
        )
    if more:
        print(
            f"NEXT_OFFSET={offset + args.limit} - re-run the same command to continue; coverage is incomplete until "
            "the last slice",
            file=sys.stderr,
        )
    return 0


def pr_files(args: argparse.Namespace) -> int:
    """Print one changed-file record per pull request number; return the process exit code."""
    root = Path(args.repo_root)
    owner, name = resolve_repo(args.repo, root)
    for batch in _batches(args.numbers, args.batch_size):
        for record in parse_pr_files_response(run_graphql(build_pr_files_query(batch), owner, name, root), batch):
            print(json.dumps(record, ensure_ascii=False))
        sys.stdout.flush()
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser with the ``commit-prs`` and ``pr-files`` subcommands."""
    parser = argparse.ArgumentParser(description="Batch per-item GitHub lookups into few GraphQL requests.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", help="OWNER/NAME (default: gh repo view in --repo-root)")
    common.add_argument("--repo-root", default=".", help="directory to run git and gh in (default: cwd)")
    common.add_argument("--batch-size", type=int, default=DEFAULT_BATCH, help=f"aliases per request (max {MAX_BATCH})")
    sub = parser.add_subparsers(dest="command", required=True)
    commits = sub.add_parser("commit-prs", parents=[common], help="merged PRs associated with each commit of a range")
    commits.add_argument("--range", required=True, help="git revision range for rev-list")
    resume = commits.add_mutually_exclusive_group()
    resume.add_argument("--offset", type=int, default=0, help="commits to skip (default 0)")
    resume.add_argument("--offset-file", help="file holding the resume offset between runs (replaces --offset)")
    commits.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"commits per run (default {DEFAULT_LIMIT})")
    commits.set_defaults(func=commit_prs)
    files = sub.add_parser("pr-files", parents=[common], help="changed file paths of pull requests")
    files.add_argument("numbers", nargs="+", type=int, help="pull request numbers")
    files.set_defaults(func=pr_files)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the chosen subcommand.

    Args:
        argv: Command-line arguments; ``None`` reads ``sys.argv``.

    Returns:
        ``0`` on success, ``1`` for invalid arguments or repository, ``2`` for a failed git or GraphQL call.
    """
    args = build_parser().parse_args(argv)
    if not 1 <= args.batch_size <= MAX_BATCH or getattr(args, "offset", 0) < 0 or getattr(args, "limit", 1) < 1:
        print(f"Error: --batch-size must be 1-{MAX_BATCH}, --offset >= 0, --limit >= 1", file=sys.stderr)
        return 1
    try:
        return args.func(args)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except QueryError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
