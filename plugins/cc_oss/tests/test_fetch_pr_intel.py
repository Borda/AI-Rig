"""Tests for ``bin/fetch_pr_intel.py`` — the deterministic PR-discussion fetch that replaces a model's literal ``gh``
calls.

No test reaches GitHub: ``_run`` is replaced by a table of canned responses keyed on the endpoint.
"""

from __future__ import annotations

import json
from pathlib import Path

import fetch_pr_intel as fpi
import pytest

REPO = "acme/widgets"


def _comments(ids: list[int], *, has_next: bool = False, cursor: str = "k1") -> dict:
    """Build the nested ``comments`` connection of one review thread."""
    return {"pageInfo": {"hasNextPage": has_next, "endCursor": cursor}, "nodes": [{"databaseId": i} for i in ids]}


def _graphql(
    threads: list[tuple[bool, list[int]]],
    *,
    has_next: bool = False,
    cursor: str = "c1",
    thread_comments: dict | None = None,
) -> str:
    """Build one reviewThreads GraphQL response page; ``thread_comments`` overrides the first thread's connection."""
    nodes = [
        {"id": f"T{n}", "isResolved": resolved, "comments": _comments(ids)} for n, (resolved, ids) in enumerate(threads)
    ]
    if thread_comments is not None:
        nodes[0]["comments"] = thread_comments
    page = {"pageInfo": {"hasNextPage": has_next, "endCursor": cursor}, "nodes": nodes}
    return json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": page}}}})


class FakeGh:
    """Canned ``gh`` responses; any command with no canned response fails like a real non-zero exit."""

    def __init__(self, responses: dict[str, str | Exception]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> str:
        self.calls.append(cmd)
        joined = " ".join(cmd)
        for needle, response in self.responses.items():
            if needle in joined:
                if isinstance(response, Exception):
                    raise response
                return response
        raise fpi.FetchError(f"no canned response for {joined}")


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    """Install a fake ``gh`` with a small PR: 1 comment, 1 review, 2 inline comments, 1 commit, 1 resolved thread."""
    fake = FakeGh(
        {
            "issues/7/comments": json.dumps(
                [{"id": 1, "user": {"login": "ann"}, "author_association": "NONE", "body": "hi", "html_url": "u1"}]
            ),
            "pulls/7/reviews": json.dumps(
                [
                    {
                        "id": 2,
                        "user": {"login": "bob"},
                        "author_association": "MEMBER",
                        "commit_id": "abc",
                        "state": "COMMENTED",
                        "body": "r",
                    }
                ]
            ),
            "pulls/7/comments": json.dumps(
                [
                    {
                        "id": 10,
                        "user": {"login": "bob"},
                        "author_association": "MEMBER",
                        "path": "a.py",
                        "line": 3,
                        "start_line": 2,
                        "pull_request_review_id": 2,
                        "commit_id": "abc",
                        "original_commit_id": "abc",
                        "diff_hunk": "@@ -1 +1 @@\n+x",
                        "body": "x",
                    },
                    {"id": 11, "user": {"login": "bob"}, "path": "a.py", "line": None, "original_line": 9, "body": "y"},
                ]
            ),
            "pulls/7/commits": json.dumps(
                [
                    {
                        "sha": "deadbeef",
                        "html_url": "cu",
                        "author": {"login": "ann"},
                        "commit": {
                            "message": "fix a\n\nmore",
                            "author": {"date": "2026-01-01T00:00:00Z"},
                            "committer": {"date": "2026-01-02T00:00:00Z"},
                        },
                    }
                ]
            ),
            "graphql": _graphql([(True, [10]), (False, [11])]),
            "issue view 5": json.dumps({"title": "T", "body": "B"}),
        }
    )
    monkeypatch.setattr(fpi, "_run", fake)
    return fake


def _read(out: Path) -> list[dict]:
    """Parse the written JSONL."""
    return [json.loads(line) for line in (out / fpi.RAW_NAME).read_text(encoding="utf-8").splitlines()]


class TestDecodePages:
    """Covers the flattening of ``gh api --paginate`` output."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param('[{"id": 1}][{"id": 2}]', [{"id": 1}, {"id": 2}], id="two-pages-back-to-back"),
            pytest.param('[{"id": 1}]\n[{"id": 2}]\n', [{"id": 1}, {"id": 2}], id="newline-separated"),
            pytest.param("[]", [], id="empty-page"),
            pytest.param("", [], id="no-output"),
        ],
    )
    def test_pages_are_flattened(self, text: str, expected: list) -> None:
        """Concatenated JSON arrays decode into one flat list, which plain ``json.loads`` would reject."""
        assert fpi.decode_pages(text) == expected


class TestPaginationContract:
    """Covers the completeness contract: no endpoint is read as a single capped page."""

    def test_every_rest_list_is_paginated_at_100(self, gh: FakeGh, tmp_path: Path) -> None:
        """All four REST lists go through ``gh api --paginate`` with ``per_page=100``.

        Dropping ``--paginate`` silently truncates a busy PR to 30 items, the exact wrong answer the script exists to
        avoid, and no result shape would reveal it.
        """
        fpi.main(["--pr", "7", "--repo", REPO, "--out", str(tmp_path)])
        rest = [c for c in gh.calls if c[:2] == ["gh", "api"] and "graphql" not in c]
        assert len(rest) == 4
        assert all(c[2] == "--paginate" and c[3].endswith("?per_page=100") for c in rest)

    def test_thread_pagination_follows_the_cursor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A second threads page is requested with the cursor of the first.

        Resolved ids on page two are lost if the loop stops after one page, which is how a busy PR's later threads would
        silently show as unresolved.
        """
        first = _graphql([(True, [10])], has_next=True, cursor="next")
        second = _graphql([(True, [11])])
        monkeypatch.setattr(fpi, "_run", lambda cmd: second if "after=next" in cmd else first)
        assert fpi.resolved_comment_ids("acme", "widgets", 7) == {10, 11}

    def test_long_resolved_thread_follows_its_comment_pages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A resolved thread with more than 100 comments contributes every comment id.

        Scenario: the first thread page lists 100 comments and says more exist; ids past the first page must come from
        the follow-up thread query, otherwise those comments would be reported as unresolved while the header claims the
        status lookup succeeded.
        """
        head = _graphql([(True, [10])], thread_comments=_comments([10], has_next=True, cursor="k1"))
        tail = json.dumps({"data": {"node": {"comments": _comments([11, 12])}}})
        calls: list[list[str]] = []

        def fake(cmd: list[str]) -> str:
            calls.append(cmd)
            return tail if "id=T0" in cmd else head

        monkeypatch.setattr(fpi, "_run", fake)
        assert fpi.resolved_comment_ids("acme", "widgets", 7) == {10, 11, 12}
        assert "after=k1" in calls[-1]

    def test_graphql_numbers_are_sent_as_integers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The pull request number goes through ``-F`` (typed) and owner and name through ``-f`` (strings).

        GraphQL declares the number as ``Int!``; sent with ``-f`` it is a string, GitHub rejects the query, the failure
        is swallowed as "status unknown", and every thread would silently show as unresolved.
        """
        calls: list[list[str]] = []

        def fake(cmd: list[str]) -> str:
            calls.append(cmd)
            return _graphql([(True, [10])])

        monkeypatch.setattr(fpi, "_run", fake)
        fpi.resolved_comment_ids("acme", "widgets", 7)
        assert ["-F", "number=7"] == calls[0][calls[0].index("-F") : calls[0].index("-F") + 2]
        strings = [calls[0][i : i + 2] for i, word in enumerate(calls[0]) if word == "-f"]
        assert ["-f", "owner=acme"] in strings
        assert ["-f", "name=widgets"] in strings


class TestMain:
    """Covers the end-to-end fetch with a fake ``gh``."""

    def test_writes_header_then_every_source(self, gh: FakeGh, tmp_path: Path) -> None:
        """The file holds a header plus one record per source item, in a fixed source order.

        Scenario: a PR with one conversation comment, one review, two inline comments, one commit and one linked issue
        gives header + 6 records, so the classifier sees the whole thread from a single file read.
        """
        assert fpi.main(["--pr", "7", "--repo", REPO, "--out", str(tmp_path), "--issues", "5"]) == 0
        records = _read(tmp_path)
        assert [r.get("record") or r["source"] for r in records] == [
            "header",
            "discussion",
            "review",
            "inline",
            "inline",
            "commit",
            "issue",
        ]

    def test_classifier_evidence_fields_survive(self, gh: FakeGh, tmp_path: Path) -> None:
        """Maintainer status, position and commit context reach the file.

        The classifier may not run ``gh`` itself, so ``author_association`` (who can require a change), the commit ids
        and diff hunk (which code a comment targeted) and the commit list (what addressed it) must all be in the file.
        """
        fpi.main(["--pr", "7", "--repo", REPO, "--out", str(tmp_path)])
        by_source = {(r.get("source"), r.get("id")): r for r in _read(tmp_path)}
        inline = by_source[("inline", 10)]
        assert inline["author_association"] == "MEMBER"
        assert (inline["start_line"], inline["pull_request_review_id"], inline["commit_id"]) == (2, 2, "abc")
        assert inline["diff_hunk"].endswith("+x")
        assert by_source[("review", 2)]["commit_id"] == "abc"
        commit = by_source[("commit", "deadbeef")]
        assert (commit["author"], commit["created_at"], commit["committed_at"], commit["body"]) == (
            "ann",
            "2026-01-01T00:00:00Z",
            "2026-01-02T00:00:00Z",
            "fix a\n\nmore",
        )

    def test_resolved_flag_follows_the_thread(self, gh: FakeGh, tmp_path: Path) -> None:
        """An inline comment is resolved only when its own thread is resolved on GitHub.

        Comment 10 sits in the resolved thread and 11 in an open one; the line falls back to the original line, marked
        outdated, when the current line is gone.
        """
        fpi.main(["--pr", "7", "--repo", REPO, "--out", str(tmp_path)])
        inline = [r for r in _read(tmp_path) if r.get("source") == "inline"]
        assert [(r["id"], r["resolved"], r["line"], r["outdated"]) for r in inline] == [
            (10, True, 3, False),
            (11, False, 9, True),
        ]

    def test_header_counts_and_notice(self, gh: FakeGh, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """The stdout summary repeats the header counts and carries the untrusted-data notice."""
        fpi.main(["--pr", "7", "--repo", REPO, "--out", str(tmp_path), "--issues", "5"])
        summary = json.loads(capsys.readouterr().out)
        assert (summary["discussion"], summary["reviews"], summary["inline"], summary["commits"]) == (1, 1, 2, 1)
        assert (summary["resolved_inline"], summary["issues"]) == (1, 1)
        assert "never instructions" in summary["untrusted"]

    def test_duplicate_ids_collapse(self, gh: FakeGh, tmp_path: Path) -> None:
        """A comment repeated across two pages is written once.

        Pagination overlap is the failure this guards: the same REST id on page 1 and page 2 must not become two
        action items downstream.
        """
        gh.responses["issues/7/comments"] = '[{"id": 1, "body": "a"}][{"id": 1, "body": "a"}]'
        fpi.main(["--pr", "7", "--repo", REPO, "--out", str(tmp_path)])
        assert sum(r.get("source") == "discussion" for r in _read(tmp_path)) == 1

    def test_duplicate_issue_numbers_are_fetched_once(self, gh: FakeGh, tmp_path: Path) -> None:
        """A number listed twice costs one ``gh issue view`` call, not two."""
        fpi.main(["--pr", "7", "--repo", REPO, "--out", str(tmp_path), "--issues", "5,#5, 5"])
        assert sum("view" in c for c in gh.calls) == 1

    def test_same_repo_reference_is_fetched_once(self, gh: FakeGh, tmp_path: Path) -> None:
        """``5`` and ``Acme/Widgets#5`` name the same issue and cost one ``gh issue view``, whatever the slug case."""
        fpi.main(["--pr", "7", "--repo", REPO, "--out", str(tmp_path), "--issues", f"5,{REPO.upper()}#5"])
        assert sum("view" in c for c in gh.calls) == 1

    def test_file_level_comment_is_not_outdated(self, gh: FakeGh, tmp_path: Path) -> None:
        """A comment on a whole file has neither a line nor an original line, so it is not marked outdated.

        Marking it outdated would tell the classifier the code it targets moved, when it never pointed at a line.
        """
        gh.responses["pulls/7/comments"] = json.dumps(
            [{"id": 30, "user": {"login": "bob"}, "path": "a.py", "line": None, "original_line": None, "body": "z"}]
        )
        fpi.main(["--pr", "7", "--repo", REPO, "--out", str(tmp_path)])
        inline = next(r for r in _read(tmp_path) if r.get("source") == "inline")
        assert (inline["line"], inline["outdated"]) == (None, False)

    def test_commit_cap_is_flagged(self, gh: FakeGh, tmp_path: Path) -> None:
        """A commit list at GitHub's 250 cap sets ``commits_truncated``, since the newest commits are missing."""
        gh.responses["pulls/7/commits"] = json.dumps([{"sha": f"s{i}", "commit": {"message": "m"}} for i in range(250)])
        fpi.main(["--pr", "7", "--repo", REPO, "--out", str(tmp_path)])
        header = _read(tmp_path)[0]
        assert (header["commits"], header["commits_truncated"]) == (250, True)

    def test_short_commit_list_is_not_flagged(self, gh: FakeGh, tmp_path: Path) -> None:
        """A commit list below the cap leaves ``commits_truncated`` false, so the flag is not a constant."""
        fpi.main(["--pr", "7", "--repo", REPO, "--out", str(tmp_path)])
        assert _read(tmp_path)[0]["commits_truncated"] is False

    @pytest.mark.parametrize(
        "repo",
        [
            pytest.param("../..", id="parent-segments"),
            pytest.param("./x", id="dot-owner"),
            pytest.param("x/..", id="dotdot-name"),
        ],
    )
    def test_repo_with_dot_segments_is_rejected(self, repo: str, gh: FakeGh, tmp_path: Path) -> None:
        """A repository made of ``.`` or ``..`` segments is an invalid argument, never a path into the API.

        It would otherwise build ``repos/../..`` and read an unintended GitHub endpoint.
        """
        assert fpi.main(["--pr", "7", "--repo", repo, "--out", str(tmp_path)]) == 1
        assert gh.calls == []

    def test_cross_repo_issue_is_read_from_its_own_repo(self, gh: FakeGh, tmp_path: Path) -> None:
        """An ``OWNER/NAME#N`` reference queries that repository, not the base one.

        A closing reference can name an issue in another repository; reading the same number from the base repository
        would classify the wrong issue's text.
        """
        gh.responses["issue view 8"] = json.dumps({"title": "Other", "body": "elsewhere"})
        fpi.main(["--pr", "7", "--repo", REPO, "--out", str(tmp_path), "--issues", "other/lib#8"])
        view = next(c for c in gh.calls if "view" in c)
        assert view[view.index("--repo") + 1] == "other/lib"
        issue = _read(tmp_path)[-1]
        assert (issue["id"], issue["repo"], issue["title"]) == ("other/lib#8", "other/lib", "Other")

    def test_graphql_failure_is_not_fatal(self, gh: FakeGh, tmp_path: Path) -> None:
        """A failed resolved-thread query leaves every comment unresolved and says so in the header."""
        gh.responses["graphql"] = fpi.FetchError("boom")
        assert fpi.main(["--pr", "7", "--repo", REPO, "--out", str(tmp_path)]) == 0
        header = _read(tmp_path)[0]
        assert (header["resolved_threads_ok"], header["resolved_inline"]) == (False, 0)

    def test_commit_failure_is_flagged_not_fatal(self, gh: FakeGh, tmp_path: Path) -> None:
        """A failed commit list leaves the thread intact and sets ``commits_ok`` to false."""
        gh.responses["pulls/7/commits"] = fpi.FetchError("boom")
        assert fpi.main(["--pr", "7", "--repo", REPO, "--out", str(tmp_path)]) == 0
        header = _read(tmp_path)[0]
        assert (header["commits_ok"], header["commits"]) == (False, 0)

    def test_required_endpoint_failure_writes_nothing(self, gh: FakeGh, tmp_path: Path) -> None:
        """A failed required fetch exits 2 and leaves no file, so a partial thread is never classified."""
        gh.responses["pulls/7/reviews"] = fpi.FetchError("rate limited")
        assert fpi.main(["--pr", "7", "--repo", REPO, "--out", str(tmp_path)]) == 2
        assert not (tmp_path / fpi.RAW_NAME).exists()

    def test_failed_rerun_removes_the_previous_file(self, gh: FakeGh, tmp_path: Path) -> None:
        """A failed run deletes the file an earlier run left behind.

        Scenario: the first run succeeds, the retry hits a rate limit. The classifier must not find the old file and
        treat out-of-date discussion as the current thread.
        """
        assert fpi.main(["--pr", "7", "--repo", REPO, "--out", str(tmp_path)]) == 0
        gh.responses["pulls/7/reviews"] = fpi.FetchError("rate limited")
        assert fpi.main(["--pr", "7", "--repo", REPO, "--out", str(tmp_path)]) == 2
        assert not (tmp_path / fpi.RAW_NAME).exists()

    def test_exhausted_time_budget_exits_two(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A run past its time budget fails closed instead of running into the caller's timeout.

        The caller's Bash timeout kills the process silently; the script's own deadline turns that into exit 2 with a
        blocked message, so no stale or partial file is read. The real runner is used: the deadline check comes before
        any ``gh`` process starts, so nothing is executed.
        """
        monkeypatch.setattr(fpi, "_BUDGET", -1)
        assert fpi.main(["--pr", "7", "--repo", REPO, "--out", str(tmp_path)]) == 2
        assert "time budget" in capsys.readouterr().err
        assert not (tmp_path / fpi.RAW_NAME).exists()

    def test_failed_issue_lookup_stays_on_its_record(self, gh: FakeGh, tmp_path: Path) -> None:
        """An issue that cannot be read becomes an error record instead of failing the whole fetch."""
        gh.responses["issue view 5"] = fpi.FetchError("gone")
        fpi.main(["--pr", "7", "--repo", REPO, "--out", str(tmp_path), "--issues", "5"])
        assert "fetch_error" in _read(tmp_path)[-1]


class TestArguments:
    """Covers argument resolution: flags over the args file, validation naming the bad field."""

    def test_args_file_drives_the_fetch(self, gh: FakeGh, tmp_path: Path) -> None:
        """A values file drives the fetch, so a skill's bash block can stay fixed text.

        Scenario: the orchestrator writes ``pr=``, ``repo=`` and ``issues=`` with its Write tool and runs one unchanged
        command; the header must show the values from the file.
        """
        values = tmp_path / "args.txt"
        values.write_text(f"pr=7\nrepo={REPO}\nissues=5\n", encoding="utf-8")
        assert fpi.main(["--args-file", str(values), "--out", str(tmp_path)]) == 0
        header = _read(tmp_path)[0]
        assert (header["pr"], header["repo"], header["issues"]) == (7, REPO, 1)

    def test_unknown_and_shell_shaped_keys_are_ignored(self, gh: FakeGh, tmp_path: Path) -> None:
        """Only ``pr``, ``repo`` and ``issues`` are read; any other line is dropped as data.

        A key such as ``$(...)`` never reaches a shell or an argument vector, so nothing outside the three fields can
        change what runs.
        """
        values = tmp_path / "args.txt"
        values.write_text(f"pr=7\nrepo={REPO}\n$(touch pwned)=1\nout=/etc\n", encoding="utf-8")
        assert fpi.read_args_file(values) == {"pr": "7", "repo": REPO}

    def test_explicit_flag_beats_the_args_file(self, gh: FakeGh, tmp_path: Path) -> None:
        """A flag wins over the file even when its value is empty, as the usage text says.

        Scenario: the file lists issue 5 but the caller passes ``--issues ""`` to fetch none; the empty flag must not be
        treated as unset.
        """
        values = tmp_path / "args.txt"
        values.write_text(f"pr=7\nrepo={REPO}\nissues=5\n", encoding="utf-8")
        fpi.main(["--args-file", str(values), "--out", str(tmp_path), "--issues", ""])
        assert _read(tmp_path)[0]["issues"] == 0

    def test_missing_args_file_exits_one(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """An unreadable values file is an argument error, not a crash."""
        assert fpi.main(["--args-file", str(tmp_path / "absent.txt"), "--out", str(tmp_path)]) == 1
        assert "invalid" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("argv", "field"),
        [
            pytest.param(["--pr", "7", "--repo", "not-a-slug"], "repo", id="bad-repo"),
            pytest.param(["--pr", "7", "--repo", REPO, "--issues", "1;rm"], "issues", id="bad-issues"),
            pytest.param(["--pr", "7", "--repo", REPO, "--issues", "$(id)=1"], "issues", id="shell-shaped-issues"),
            pytest.param(["--pr", "0", "--repo", REPO], "pr", id="bad-number"),
            pytest.param(["--repo", REPO], "pr", id="missing-number"),
            pytest.param(["--pr", "7", "--repo", f"{REPO}\n"], "repo", id="trailing-newline-repo"),
        ],
    )
    def test_invalid_arguments_exit_one_and_name_the_field(
        self, argv: list[str], field: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Malformed arguments are rejected before any ``gh`` call and the message names the failing field."""
        assert fpi.main([*argv, "--out", str(tmp_path)]) == 1
        assert f"invalid {field}" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param("12, 34", ["12", "34"], id="spaces-after-comma"),
            pytest.param("#12", ["12"], id="leading-hash"),
            pytest.param("", [], id="empty"),
            pytest.param("o/r#5,6", ["o/r#5", "6"], id="cross-repo-mixed"),
            pytest.param("²", None, id="unicode-digit"),
        ],
    )
    def test_parse_issues(self, text: str, expected: list[str] | None) -> None:
        """Issue lists tolerate spaces and a leading hash, and reject anything but ASCII digits or a repo reference."""
        assert fpi.parse_issues(text) == expected
