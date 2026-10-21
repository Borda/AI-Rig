"""Tests for ``bin/gh_batch_query.py`` — batched GraphQL lookups replacing per-commit and per-PR ``gh`` loops."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import gh_batch_query as ghq
import pytest

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


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


def test_commit_query_has_one_alias_per_sha() -> None:
    """Every sha becomes its own alias inside a single repository selection."""
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
def test_commit_query_rejects_non_hash(bad: str) -> None:
    """Only full hexadecimal hashes may be interpolated into the query text."""
    with pytest.raises(ValueError, match="full commit hash"):
        ghq.build_commit_query([bad])


def test_parse_commit_response_keeps_only_merged_prs_in_input_order() -> None:
    """Unmerged PRs are dropped, the REST-shaped record is built, and order follows the input shas."""
    repository = {
        "c0": {
            "associatedPullRequests": {
                "pageInfo": {"hasNextPage": False},
                "nodes": [_pr(7), _pr(8, merged=False)],
            }
        },
        "c1": {"associatedPullRequests": {"pageInfo": {"hasNextPage": False}, "nodes": []}},
    }
    records = ghq.parse_commit_response(repository, [SHA_A, SHA_B])
    assert [r["commit"] for r in records] == [SHA_A, SHA_B]
    assert records[0]["pull_requests"] == [
        {
            "number": 7,
            "title": "PR 7",
            "author": {"login": "user7"},
            "base": "main",
            "merge_commit_sha": "m" * 40,
        }
    ]
    assert records[1] == {"commit": SHA_B, "pull_requests": []}


def test_parse_commit_response_flags_unknown_commit_and_overflow() -> None:
    """A commit GitHub cannot resolve and a truncated page are coverage gaps, never silent empties."""
    repository = {
        "c0": None,
        "c1": {"associatedPullRequests": {"pageInfo": {"hasNextPage": True}, "nodes": [_pr(1)]}},
    }
    unknown, overflow = ghq.parse_commit_response(repository, [SHA_A, SHA_B])
    assert unknown["coverage_gap"] == "commit unknown to GitHub"
    assert unknown["pull_requests"] == []
    assert "more than" in overflow["coverage_gap"]
    assert overflow["pull_requests"][0]["number"] == 1


def test_parse_commit_response_tolerates_null_merge_commit() -> None:
    """A merged PR without a merge commit oid keeps a ``None`` merge sha instead of crashing."""
    repository = {
        "c0": {
            "associatedPullRequests": {
                "pageInfo": {"hasNextPage": False},
                "nodes": [_pr(3, merge_sha=None)],
            }
        }
    }
    (record,) = ghq.parse_commit_response(repository, [SHA_A])
    assert record["pull_requests"][0]["merge_commit_sha"] is None


def test_pr_files_query_and_parse() -> None:
    """File lists come back per PR in input order; a missing PR and a truncated list are flagged."""
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


def _fake_run(monkeypatch: pytest.MonkeyPatch, shas: list[str]) -> list[list[str]]:
    """Patch git/gh calls: ``rev-list`` returns ``shas``; GraphQL batches are recorded and answered empty."""
    batches: list[list[str]] = []

    def fake_run_graphql(query: str, owner: str, name: str, cwd: Path) -> dict:
        assert (owner, name) == ("org", "proj")
        batch = [s for s in shas if f'oid:"{s}"' in query]
        batches.append(batch)
        empty = {"associatedPullRequests": {"pageInfo": {"hasNextPage": False}, "nodes": []}}
        return {f"c{i}": empty for i in range(len(batch))}

    def fake_run(cmd: list[str], cwd: Path) -> str:
        assert cmd[:2] == ["git", "rev-list"]
        return "\n".join(shas) + "\n"

    monkeypatch.setattr(ghq, "run_graphql", fake_run_graphql)
    monkeypatch.setattr(ghq, "_run", fake_run)
    return batches


def test_main_commit_prs_batches_requests(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Three commits with batch size 2 cost two requests and print every record in range order."""
    batches = _fake_run(monkeypatch, [SHA_A, SHA_B, SHA_C])
    code = ghq.main(["commit-prs", "--range", "v1..v2", "--repo", "org/proj", "--batch-size", "2"])
    out = capsys.readouterr()
    assert code == 0
    assert [len(b) for b in batches] == [2, 1]
    assert [json.loads(line)["commit"] for line in out.out.splitlines()] == [SHA_A, SHA_B, SHA_C]
    assert "NEXT_OFFSET" not in out.err


def test_main_commit_prs_hints_next_offset(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A limit below the range length processes the slice and prints the continuation offset on stderr."""
    _fake_run(monkeypatch, [SHA_A, SHA_B, SHA_C])
    code = ghq.main(["commit-prs", "--range", "v1..v2", "--repo", "org/proj", "--limit", "2"])
    out = capsys.readouterr()
    assert code == 0
    assert len(out.out.splitlines()) == 2
    assert "NEXT_OFFSET=2" in out.err


def test_offset_file_resumes_then_resets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Re-running the identical command walks the range slice by slice and resets the saved offset at the end."""
    _fake_run(monkeypatch, [SHA_A, SHA_B, SHA_C])
    state = tmp_path / "offset"
    argv = ["commit-prs", "--range", "v1..v2", "--repo", "org/proj", "--limit", "2", "--offset-file", str(state)]
    seen: list[str] = []
    for expected_state in ("v1..v2\t2\n", "v1..v2\t0\n"):
        assert ghq.main(argv) == 0
        seen += [json.loads(line)["commit"] for line in capsys.readouterr().out.splitlines()]
        assert state.read_text(encoding="utf-8") == expected_state
    assert seen == [SHA_A, SHA_B, SHA_C]


def test_offset_file_for_other_range_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An offset left behind by an abandoned run of a different range never skips commits of this range."""
    _fake_run(monkeypatch, [SHA_A, SHA_B])
    state = tmp_path / "offset"
    state.write_text("old..range\t1\n", encoding="utf-8")
    assert ghq.main(["commit-prs", "--range", "v1..v2", "--repo", "org/proj", "--offset-file", str(state)]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 2


def test_main_rejects_option_shaped_range(capsys: pytest.CaptureFixture[str]) -> None:
    """A range beginning with ``-`` could smuggle a git option and is refused before any call."""
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
def test_main_rejects_invalid_arguments(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    """A malformed ``--repo`` or an out-of-range batch size exits 1 with an error before any request."""
    assert ghq.main(argv) == 1
    assert "Error:" in capsys.readouterr().err


def test_main_graphql_failure_exits_2(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A failed request is fail-closed: exit 2 and the error names the step."""

    def boom(query: str, owner: str, name: str, cwd: Path) -> dict:
        raise ghq.QueryError("gh api graphql exited 1: rate limited")

    monkeypatch.setattr(ghq, "run_graphql", boom)
    assert ghq.main(["pr-files", "5", "--repo", "org/proj"]) == 2
    assert "rate limited" in capsys.readouterr().err


def _gh_returns(monkeypatch: pytest.MonkeyPatch, body: dict, returncode: int) -> None:
    """Make every ``gh`` call finish with the given JSON body and exit code."""
    done = subprocess.CompletedProcess(args=["gh"], returncode=returncode, stdout=json.dumps(body), stderr="gh: boom")
    monkeypatch.setattr(ghq, "_capture", lambda cmd, cwd: done)


def test_run_graphql_rejects_other_graphql_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """A response carrying a non-``NOT_FOUND`` error fails the request even though the body parses."""
    _gh_returns(monkeypatch, {"errors": [{"type": "RATE_LIMITED", "message": "slow down"}], "data": None}, 1)
    with pytest.raises(ghq.QueryError, match="RATE_LIMITED"):
        ghq.run_graphql("query{}", "o", "r", Path("."))


def test_run_graphql_tolerates_not_found_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """``gh`` exits 1 for a missing pull request but the other aliases still carry data, so the data is returned."""
    body = {
        "errors": [{"type": "NOT_FOUND", "message": "gone"}],
        "data": {"repository": {"p0": None, "p1": {"number": 2}}},
    }
    _gh_returns(monkeypatch, body, 1)
    assert ghq.run_graphql("query{}", "o", "r", Path(".")) == {"p0": None, "p1": {"number": 2}}


def test_run_graphql_fails_on_non_json_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero exit with no JSON body (auth failure, network) surfaces gh's stderr."""
    done = subprocess.CompletedProcess(
        args=["gh"], returncode=4, stdout="", stderr="gh: To use GitHub CLI, run: gh auth login"
    )
    monkeypatch.setattr(ghq, "_capture", lambda cmd, cwd: done)
    with pytest.raises(ghq.QueryError, match="auth login"):
        ghq.run_graphql("query{}", "o", "r", Path("."))
