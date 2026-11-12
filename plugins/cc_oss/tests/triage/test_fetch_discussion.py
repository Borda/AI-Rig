"""Exercise complete discussion pagination and conservative partial output without network access."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import fetch_discussion as fetch
import pytest


def _connection(nodes: list[dict], more: bool = False, cursor: str | None = None) -> dict:
    """Build an API connection with explicit pagination evidence."""
    return {"nodes": nodes, "pageInfo": {"hasNextPage": more, "endCursor": cursor}}


def _comment(identifier: str, replies: dict | None = None) -> dict:
    """Build one comment retaining the nested initial reply page."""
    return {"id": identifier, "body": identifier, "replies": replies or _connection([])}


def _discussion(comments: dict) -> dict:
    """Wrap discussion metadata and its comment connection as a GraphQL response."""
    return {"data": {"repository": {"discussion": {"title": "Question", "body": "Text", "comments": comments}}}}


def _page(payload: dict, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    """Encode one mocked subprocess response at the external boundary."""
    return subprocess.CompletedProcess([], returncode, json.dumps(payload), "private upstream error")


def _run(tmp_path: Path) -> tuple[int, dict]:
    """Invoke the public CLI and read its persisted discussion object."""
    output = tmp_path / "discussion.json"
    result = fetch.main(["--repo", "org/project", "--number", "7", "--output", str(output)])
    return result, json.loads(output.read_text(encoding="utf-8"))


@patch("fetch_discussion.subprocess.run", autospec=True)
def test_merges_comment_and_reply_pages_with_separate_cursors(run: Mock, tmp_path: Path) -> None:
    """Top-level and per-parent cursors fetch every page and duplicate ids appear once."""
    run.side_effect = [
        _page(_discussion(_connection([_comment("c1", _connection([{"id": "r1"}], True, "reply1"))], True, "top1"))),
        _page(_discussion(_connection([_comment("c1"), _comment("c2")]))),
        _page({"data": {"node": {"id": "c1", "replies": _connection([{"id": "r1"}, {"id": "r2"}])}}}),
    ]
    code, item = _run(tmp_path)
    assert code == 0
    assert [node["id"] for node in item["comments"]["nodes"]] == ["c1", "c2"]
    assert item["comments"]["nodes"][0]["replies"]["nodes"] == [{"id": "r1"}, {"id": "r2"}]
    assert item["_fetch"] == {"status": "complete", "comments": 2, "replies": 2, "errors": []}
    assert "after=top1" in run.call_args_list[1].args[0]
    assert "after=reply1" in run.call_args_list[2].args[0]
    assert "commentId=c1" in run.call_args_list[2].args[0]
    assert "owner=org" in run.call_args_list[0].args[0]
    assert "repo=project" in run.call_args_list[0].args[0]
    assert run.call_args_list[0].kwargs.get("shell", False) is False


@patch("fetch_discussion.subprocess.run", autospec=True)
def test_initial_nested_pages_share_one_reply_budget(run: Mock, tmp_path: Path) -> None:
    """Initial reply pages count toward the same 200-reply budget as additional pages."""
    comments = [_comment(f"c{i}", _connection([{"id": f"r{i}-{j}"} for j in range(50)])) for i in range(5)]
    run.return_value = _page(_discussion(_connection(comments)))
    code, item = _run(tmp_path)
    assert code == 1
    assert item["_fetch"]["replies"] == 200
    assert item["comments"]["nodes"][4]["replies"]["nodes"] == []
    assert "reply cap" in " ".join(item["_fetch"]["errors"])
    assert run.call_count == 1


@patch("fetch_discussion.subprocess.run", autospec=True)
def test_extra_reply_pages_stop_at_shared_budget(run: Mock, tmp_path: Path) -> None:
    """Additional pages cannot push replies beyond the shared limit or hide unseen content."""
    replies = [{"id": f"r{i}"} for i in range(150)]
    run.side_effect = [
        _page(_discussion(_connection([_comment("c1", _connection(replies, True, "next"))]))),
        _page({"data": {"node": {"id": "c1", "replies": _connection([{"id": f"s{i}"} for i in range(75)])}}}),
    ]
    code, item = _run(tmp_path)
    assert code == 1
    assert item["_fetch"]["replies"] == 200
    assert len(item["comments"]["nodes"][0]["replies"]["nodes"]) == 200
    assert "reply cap" in " ".join(item["_fetch"]["errors"])


@pytest.mark.parametrize("more", [False, True])
@patch("fetch_discussion.subprocess.run", autospec=True)
def test_exact_comment_cap_is_complete_only_without_more_pages(run: Mock, more: bool, tmp_path: Path) -> None:
    """Exactly 200 comments are valid only when the API confirms the connection is exhausted."""
    run.return_value = _page(_discussion(_connection([_comment(f"c{i}") for i in range(200)], more, "next")))
    code, item = _run(tmp_path)
    assert code == int(more)
    assert item["_fetch"]["comments"] == 200
    assert item["_fetch"]["status"] == ("incomplete" if more else "complete")
    assert run.call_count == 1


@pytest.mark.parametrize("cursor", [None, ""])
@patch("fetch_discussion.subprocess.run", autospec=True)
def test_missing_comment_cursor_is_incomplete(run: Mock, cursor: str | None, tmp_path: Path) -> None:
    """A claimed next comment page without its cursor must never become a complete cache entry."""
    run.return_value = _page(_discussion(_connection([_comment("c1")], True, cursor)))
    code, item = _run(tmp_path)
    assert code == 1
    assert item["_fetch"]["comments"] == 1
    assert "cursor" in " ".join(item["_fetch"]["errors"])
    assert run.call_count == 1


@patch("fetch_discussion.subprocess.run", autospec=True)
def test_repeated_reply_cursor_preserves_partial_replies(run: Mock, tmp_path: Path) -> None:
    """A cyclic nested cursor stops after preserving available replies rather than looping."""
    run.side_effect = [
        _page(_discussion(_connection([_comment("c1", _connection([{"id": "r1"}], True, "same"))]))),
        _page({"data": {"node": {"id": "c1", "replies": _connection([{"id": "r2"}], True, "same")}}}),
    ]
    code, item = _run(tmp_path)
    assert code == 1
    assert item["_fetch"]["replies"] == 2
    assert "cursor" in " ".join(item["_fetch"]["errors"])
    assert run.call_count == 2


@pytest.mark.parametrize("nested", [False, True])
@patch("fetch_discussion.subprocess.run", autospec=True)
def test_failed_followup_page_persists_partial_item(run: Mock, nested: bool, tmp_path: Path) -> None:
    """Failures in either pagination layer keep fetched evidence and return an incomplete status."""
    comment = _comment("c1", _connection([{"id": "r1"}], nested, "reply"))
    run.side_effect = [_page(_discussion(_connection([comment], not nested, "top"))), _page({}, 1)]
    code, item = _run(tmp_path)
    assert code == 1
    assert item["title"] == "Question"
    assert item["_fetch"]["comments"] == 1
    assert item["_fetch"]["replies"] == 1
    assert item["_fetch"]["status"] == "incomplete"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"data": {"repository": {"discussion": None}}}, id="missing-discussion"),
        pytest.param(_discussion({"nodes": [], "pageInfo": {}}), id="missing-page-status"),
        pytest.param(_discussion(_connection([{"body": "missing id"}])), id="missing-comment-id"),
        pytest.param(_discussion(_connection([{"id": "c1"}])), id="missing-reply-connection"),
        pytest.param({"errors": [{"message": "private error"}]}, id="graphql-error"),
    ],
)
@patch("fetch_discussion.subprocess.run", autospec=True)
def test_missing_api_evidence_never_reports_complete(run: Mock, payload: dict, tmp_path: Path) -> None:
    """Null records and malformed connections are incomplete evidence rather than empty conversations."""
    run.return_value = _page(payload)
    code, item = _run(tmp_path)
    assert code == 1
    assert item["_fetch"]["status"] == "incomplete"
    assert item["_fetch"]["errors"]


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(FileNotFoundError("gh"), id="missing-cli"),
        pytest.param(subprocess.TimeoutExpired("gh", 15), id="timeout"),
    ],
)
@patch("fetch_discussion.subprocess.run", autospec=True)
def test_subprocess_failure_writes_incomplete_object(run: Mock, failure: Exception, tmp_path: Path) -> None:
    """Unavailable subprocesses still replace stale outputs with explicitly incomplete evidence."""
    (tmp_path / "discussion.json").write_text('{"stale": true}', encoding="utf-8")
    run.side_effect = failure
    code, item = _run(tmp_path)
    assert code == 1
    assert "stale" not in item
    assert item["_fetch"]["status"] == "incomplete"


@patch("fetch_discussion.subprocess.run", autospec=True)
def test_stdout_contains_only_bounded_summary(run: Mock, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Bodies and upstream stderr stay out of stdout, which contains one bounded status record."""
    run.return_value = _page(_discussion(_connection([_comment("c1")])))
    code, _item = _run(tmp_path)
    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out) == {"status": "complete", "comments": 1, "replies": 0, "error_count": 0}
    assert len(captured.out) < 200
    assert "private upstream error" not in captured.out


@pytest.mark.parametrize("more", [False, True])
@patch("fetch_discussion.subprocess.run", autospec=True)
def test_exact_reply_cap_requires_no_unseen_pages(run: Mock, more: bool, tmp_path: Path) -> None:
    """Reaching 200 replies is complete only when no reply page remains unseen."""
    replies = _connection([{"id": f"r{i}"} for i in range(200)], more, "next")
    run.return_value = _page(_discussion(_connection([_comment("c1", replies)])))
    code, item = _run(tmp_path)
    assert code == int(more)
    assert item["_fetch"]["replies"] == 200
    assert run.call_count == 1


@patch("fetch_discussion.subprocess.run", autospec=True)
def test_multiple_reply_parents_keep_their_own_cursors(run: Mock, tmp_path: Path) -> None:
    """Each nested connection uses its own parent id and cursor, including successive nested pages."""
    run.side_effect = [
        _page(
            _discussion(
                _connection(
                    [
                        _comment("c1", _connection([{"id": "r1"}], True, "first")),
                        _comment("c2", _connection([{"id": "r4"}], True, "third")),
                    ]
                )
            )
        ),
        _page({"data": {"node": {"id": "c1", "replies": _connection([{"id": "r2"}], True, "second")}}}),
        _page({"data": {"node": {"id": "c1", "replies": _connection([{"id": "r3"}])}}}),
        _page({"data": {"node": {"id": "c2", "replies": _connection([{"id": "r5"}])}}}),
    ]
    code, item = _run(tmp_path)
    assert code == 0
    assert item["_fetch"]["replies"] == 5
    assert "commentId=c1" in run.call_args_list[1].args[0]
    assert "after=first" in run.call_args_list[1].args[0]
    assert "commentId=c1" in run.call_args_list[2].args[0]
    assert "after=second" in run.call_args_list[2].args[0]
    assert "commentId=c2" in run.call_args_list[3].args[0]
    assert "after=third" in run.call_args_list[3].args[0]


@patch("fetch_discussion.subprocess.run", autospec=True)
def test_graphql_errors_preserve_available_data_as_incomplete(run: Mock, tmp_path: Path) -> None:
    """A GraphQL response containing both errors and useful data persists that data without claiming completion."""
    payload = _discussion(_connection([_comment("c1")])) | {"errors": [{"message": "upstream private error"}]}
    run.return_value = _page(payload, 1)
    code, item = _run(tmp_path)
    assert code == 1
    assert item["title"] == "Question"
    assert item["_fetch"]["comments"] == 1
    assert "GraphQL errors" in " ".join(item["_fetch"]["errors"])
    assert "upstream private error" not in " ".join(item["_fetch"]["errors"])


@patch("fetch_discussion.subprocess.run", autospec=True)
def test_comment_cap_trims_terminal_page_even_without_next_cursor(run: Mock, tmp_path: Path) -> None:
    """A terminal API page larger than the remaining comment budget is still explicitly incomplete."""
    run.return_value = _page(_discussion(_connection([_comment(f"c{i}") for i in range(201)])))
    code, item = _run(tmp_path)
    assert code == 1
    assert item["_fetch"]["comments"] == 200
    assert item["comments"]["pageInfo"]["hasNextPage"] is True


@patch("fetch_discussion.subprocess.run", autospec=True)
def test_mismatched_reply_parent_is_incomplete(run: Mock, tmp_path: Path) -> None:
    """A reply page for another parent never contaminates the requested comment's replies."""
    run.side_effect = [
        _page(_discussion(_connection([_comment("c1", _connection([{"id": "r1"}], True, "next"))]))),
        _page({"data": {"node": {"id": "other", "replies": _connection([{"id": "wrong"}])}}}),
    ]
    code, item = _run(tmp_path)
    assert code == 1
    assert item["comments"]["nodes"][0]["replies"]["nodes"] == [{"id": "r1"}]
    assert "mismatched parent" in " ".join(item["_fetch"]["errors"])
