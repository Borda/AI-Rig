"""Tests for ``bin/gh_batch_query.py`` — batched GraphQL lookups replacing per-commit and per-PR ``gh`` loops.

No test reaches GitHub or git: the GraphQL runner, the ``git rev-list`` runner and the ``gh`` capture are replaced by
canned answers, so the tests pin query construction, response parsing, batching and the offset-file protocol.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import gh_batch_query as ghq
import pytest

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
RANGE_ARGS = ["commit-prs", "--range", "v1..v2", "--repo", "org/proj"]


def _pr(number: int, *, merged: bool = True, base: str = "main", merge_sha: str | None = "m" * 40) -> dict:
    """Return one GraphQL ``associatedPullRequests`` node."""
    return {
        "number": number,
        "title": f"PR {number}",
        "merged": merged,
        "baseRefName": base,
        "mergeCommit": {"oid": merge_sha} if merge_sha else None,
        "author": {"login": f"user{number}"},
    }


def _associated(nodes: list[dict], *, has_next: bool = False) -> dict:
    """Return one commit alias with its ``associatedPullRequests`` connection."""
    return {"associatedPullRequests": {"pageInfo": {"hasNextPage": has_next}, "nodes": nodes}}


def _fake_run(monkeypatch: pytest.MonkeyPatch, shas: list[str]) -> list[list[str]]:
    """Patch git/gh calls: ``rev-list`` returns ``shas``; GraphQL batches are recorded and answered empty."""
    batches: list[list[str]] = []

    def fake_run_graphql(query: str, owner: str, name: str, cwd: Path) -> dict:
        """Record which shas one request carried and answer every alias with no pull requests."""
        assert (owner, name) == ("org", "proj")
        batch = [s for s in shas if f'oid:"{s}"' in query]
        batches.append(batch)
        return {f"c{i}": _associated([]) for i in range(len(batch))}

    def fake_run(cmd: list[str], cwd: Path) -> str:
        """Answer the range listing with the canned shas."""
        assert cmd[:2] == ["git", "rev-list"]
        return "\n".join(shas) + "\n"

    monkeypatch.setattr(ghq, "run_graphql", fake_run_graphql)
    monkeypatch.setattr(ghq, "_run", fake_run)
    return batches


def _gh_returns(monkeypatch: pytest.MonkeyPatch, stdout: str, returncode: int, stderr: str = "gh: boom") -> None:
    """Make every ``gh`` call finish with the given stdout, exit code and stderr."""
    done = subprocess.CompletedProcess(args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr)
    monkeypatch.setattr(ghq, "_capture", lambda cmd, cwd: done)


class TestBuildCommitQuery:
    """Covers ``build_commit_query``: one alias per sha and nothing but full hashes in the query text."""

    def test_has_one_alias_per_sha(self) -> None:
        """Every sha becomes its own alias inside a single repository selection.

        One repository selection keeps the whole batch to a single request, whatever the number of shas.
        """
        query = ghq.build_commit_query([SHA_A, SHA_B])
        assert f'c0:object(oid:"{SHA_A}")' in query
        assert f'c1:object(oid:"{SHA_B}")' in query
        assert query.count("repository(") == 1

    @pytest.mark.parametrize(
        "bad",
        [
            pytest.param('abc") { id } x:object(oid:"def', id="query-injection"),
            pytest.param("abc123", id="abbreviated"),
            pytest.param("--all", id="option-shaped"),
        ],
    )
    def test_rejects_non_hash(self, bad: str) -> None:
        """Only full hexadecimal hashes may be interpolated into the query text.

        The shas come from ``git rev-list`` output or user input; anything else would let a value rewrite the query.
        """
        with pytest.raises(ValueError, match="full commit hash"):
            ghq.build_commit_query([bad])


class TestParseCommitResponse:
    """Covers ``parse_commit_response``: REST-shaped records in input order, with gaps reported."""

    def test_keeps_only_merged_prs_in_input_order(self) -> None:
        """Unmerged PRs are dropped, the REST-shaped record is built, and order follows the input shas.

        Scenario: the first commit has one merged and one open pull request, the second has none; callers rely on the
        output order matching the shas they passed in.
        """
        repository = {"c0": _associated([_pr(7), _pr(8, merged=False)]), "c1": _associated([])}
        records = ghq.parse_commit_response(repository, [SHA_A, SHA_B])
        assert [r["commit"] for r in records] == [SHA_A, SHA_B]
        assert records[0]["pull_requests"] == [
            {"number": 7, "title": "PR 7", "author": {"login": "user7"}, "base": "main", "merge_commit_sha": "m" * 40}
        ]
        assert records[1] == {"commit": SHA_B, "pull_requests": []}

    def test_flags_unknown_commit_and_overflow(self) -> None:
        """A commit GitHub cannot resolve and a truncated page are coverage gaps, never silent empties.

        An empty list would read as "no pull request touched this commit" when the truth is "not checked completely".
        """
        repository = {"c0": None, "c1": _associated([_pr(1)], has_next=True)}
        unknown, overflow = ghq.parse_commit_response(repository, [SHA_A, SHA_B])
        assert unknown["coverage_gap"] == "commit unknown to GitHub"
        assert unknown["pull_requests"] == []
        assert "more than" in overflow["coverage_gap"]
        assert overflow["pull_requests"][0]["number"] == 1

    def test_tolerates_null_merge_commit(self) -> None:
        """A merged PR without a merge commit oid keeps a ``None`` merge sha instead of crashing.

        GitHub returns a null ``mergeCommit`` for some historical merges; the record must still be produced.
        """
        (record,) = ghq.parse_commit_response({"c0": _associated([_pr(3, merge_sha=None)])}, [SHA_A])
        assert record["pull_requests"][0]["merge_commit_sha"] is None


class TestPrFiles:
    """Covers the ``pr-files`` query builder and response parser."""

    def test_query_and_parse(self) -> None:
        """File lists come back per PR in input order; a missing PR and a truncated list are flagged.

        Scenario: three PRs where the second does not exist and the third has more files than one page returns.
        """
        assert "p0:pullRequest(number:12)" in ghq.build_pr_files_query([12, 13])
        repository = {
            "p0": {"number": 12, "files": {"pageInfo": {"hasNextPage": False}, "nodes": [{"path": "a.py"}]}},
            "p1": None,
            "p2": {"number": 14, "files": {"pageInfo": {"hasNextPage": True}, "nodes": [{"path": "b.py"}]}},
        }
        ok, missing, truncated = ghq.parse_pr_files_response(repository, [12, 13, 14])
        assert ok == {"pr": 12, "files": ["a.py"]}
        assert missing["coverage_gap"] == "pull request not found"
        assert truncated["files"] == ["b.py"]
        assert "more than" in truncated["coverage_gap"]


class TestMainCommitPrs:
    """Covers the ``commit-prs`` subcommand: batching, continuation offsets and the offset file."""

    def test_batches_requests(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """Three commits with batch size 2 cost two requests and print every record in range order.

        Batching is the point of the script: one request per commit would hit the rate limit on a release range.
        """
        batches = _fake_run(monkeypatch, [SHA_A, SHA_B, SHA_C])
        code = ghq.main([*RANGE_ARGS, "--batch-size", "2"])
        out = capsys.readouterr()
        assert code == 0
        assert [len(b) for b in batches] == [2, 1]
        assert [json.loads(line)["commit"] for line in out.out.splitlines()] == [SHA_A, SHA_B, SHA_C]
        assert "NEXT_OFFSET" not in out.err

    def test_exclude_file_removes_shipped_commits_before_slicing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Commits the TSV marks ``shipped`` are never queried; ``review`` rows stay in the range.

        Counting them would credit authors and PRs of work an older off-branch tag already released.
        """
        _fake_run(monkeypatch, [SHA_A, SHA_B, SHA_C])
        tsv = tmp_path / "shipped.tsv"
        tsv.write_text(f"{SHA_A}\tshipped\t1.11.1\tcherry\n{SHA_B}\treview\t1.11.1\tsame PR\n", encoding="utf-8")

        code = ghq.main([*RANGE_ARGS, "--exclude-file", str(tsv)])
        out = capsys.readouterr()

        assert code == 0
        assert [json.loads(line)["commit"] for line in out.out.splitlines()] == [SHA_B, SHA_C]
        assert "of 2" in out.err

    def test_hints_next_offset(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """A limit below the range length processes the slice and prints the continuation offset on stderr.

        The caller resumes a long range in slices, so the unprocessed remainder must be announced, not dropped.
        """
        _fake_run(monkeypatch, [SHA_A, SHA_B, SHA_C])
        code = ghq.main([*RANGE_ARGS, "--limit", "2"])
        out = capsys.readouterr()
        assert code == 0
        assert len(out.out.splitlines()) == 2
        assert "NEXT_OFFSET=2" in out.err

    def test_offset_file_resumes_then_resets(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Re-running the identical command walks the range slice by slice and resets the saved offset at the end.

        Scenario: three commits with ``--limit 2``. The first run prints two and saves offset 2; the identical second
        run prints the last one and writes offset 0, so a third run would start over.
        """
        _fake_run(monkeypatch, [SHA_A, SHA_B, SHA_C])
        state = tmp_path / "offset"
        argv = [*RANGE_ARGS, "--limit", "2", "--offset-file", str(state)]
        assert ghq.main(argv) == 0
        first = [json.loads(line)["commit"] for line in capsys.readouterr().out.splitlines()]
        saved_after_first = state.read_text(encoding="utf-8")
        assert ghq.main(argv) == 0
        second = [json.loads(line)["commit"] for line in capsys.readouterr().out.splitlines()]
        saved_after_second = state.read_text(encoding="utf-8")
        assert (first, saved_after_first, second, saved_after_second) == (
            [SHA_A, SHA_B],
            "v1..v2\t2\n",
            [SHA_C],
            "v1..v2\t0\n",
        )

    def test_offset_file_for_other_range_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An offset left behind by an abandoned run of a different range never skips commits of this range.

        The offset file records the range it belongs to; a stale ``old..range`` offset of 1 must not hide SHA_A here.
        """
        _fake_run(monkeypatch, [SHA_A, SHA_B])
        state = tmp_path / "offset"
        state.write_text("old..range\t1\n", encoding="utf-8")
        assert ghq.main([*RANGE_ARGS, "--offset-file", str(state)]) == 0
        assert len(capsys.readouterr().out.splitlines()) == 2


class TestMainArguments:
    """Covers argument validation and fail-closed exits of ``main``."""

    def test_exclusion_file_read_once_for_entire_range(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Read one exclusion snapshot for the range and omit shipped commits before batching."""
        batches = _fake_run(monkeypatch, [SHA_A, SHA_B, SHA_C])
        excluded = tmp_path / "excluded.tsv"
        excluded.write_text(f"{SHA_B}\tshipped\n", encoding="utf-8")
        read_text = Path.read_text
        reads: list[Path] = []

        def record_read(path: Path, *args: object, **kwargs: object) -> str:
            """Record real text-file reads while retaining the file's actual content."""
            reads.append(path)
            return read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", record_read)
        assert ghq.main([*RANGE_ARGS, "--exclude-file", str(excluded)]) == 0
        assert reads == [excluded]
        assert batches == [[SHA_A, SHA_C]]
        records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert [record["commit"] for record in records] == [SHA_A, SHA_C]

    def test_rejects_option_shaped_range(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A range beginning with ``-`` could smuggle a git option and is refused before any call.

        ``--range=--all`` would otherwise reach ``git rev-list`` as an option instead of a revision range.
        """
        assert ghq.main(["commit-prs", "--range=--all", "--repo", "org/proj"]) == 1
        assert "must not start with '-'" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["pr-files", "5", "--repo", "not a slug"], id="malformed-repo"),
            pytest.param(["pr-files", "5", "--repo", "o/r", "--batch-size", "0"], id="zero-batch-size"),
            pytest.param(["pr-files", "5", "--repo", "o/r", "--batch-size", "51"], id="batch-size-over-max"),
        ],
    )
    def test_rejects_invalid_arguments(self, argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
        """A malformed ``--repo`` or an out-of-range batch size exits 1 with an error before any request.

        Validation runs before the first network call, so a typo costs nothing and cannot reach the API path.
        """
        assert ghq.main(argv) == 1
        assert "Error:" in capsys.readouterr().err

    def test_graphql_failure_exits_2(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """A failed request is fail-closed: exit 2 and the error names the step.

        Scenario: the rate limit is hit mid-run; the caller must see exit 2 with gh's reason, never partial output.
        """

        def boom(query: str, owner: str, name: str, cwd: Path) -> dict:
            """Fail like a rate-limited ``gh api graphql``."""
            raise ghq.QueryError("gh api graphql exited 1: rate limited")

        monkeypatch.setattr(ghq, "run_graphql", boom)
        assert ghq.main(["pr-files", "5", "--repo", "org/proj"]) == 2
        assert "rate limited" in capsys.readouterr().err


class TestRunGraphql:
    """Covers ``run_graphql``: which ``gh`` failures are tolerated and which are surfaced."""

    def test_rejects_other_graphql_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A response carrying a non-``NOT_FOUND`` error fails the request even though the body parses.

        ``RATE_LIMITED`` data may be partial; treating the parsed body as success would hide the gap.
        """
        body = {"errors": [{"type": "RATE_LIMITED", "message": "slow down"}], "data": None}
        _gh_returns(monkeypatch, json.dumps(body), 1)
        with pytest.raises(ghq.QueryError, match="RATE_LIMITED"):
            ghq.run_graphql("query{}", "o", "r", Path("."))

    def test_tolerates_not_found_alias(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``gh`` exits 1 for a missing pull request but the other aliases still carry data, so the data is returned.

        A batch naming one deleted PR must still answer for the others; the missing alias is a null entry.
        """
        body = {
            "errors": [{"type": "NOT_FOUND", "message": "gone"}],
            "data": {"repository": {"p0": None, "p1": {"number": 2}}},
        }
        _gh_returns(monkeypatch, json.dumps(body), 1)
        assert ghq.run_graphql("query{}", "o", "r", Path(".")) == {"p0": None, "p1": {"number": 2}}

    def test_fails_on_non_json_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A non-zero exit with no JSON body (auth failure, network) surfaces gh's stderr.

        Without a body there is nothing to parse, so the message the user needs is the one ``gh`` printed.
        """
        _gh_returns(monkeypatch, "", 4, stderr="gh: To use GitHub CLI, run: gh auth login")
        with pytest.raises(ghq.QueryError, match="auth login"):
            ghq.run_graphql("query{}", "o", "r", Path("."))
