"""Regression coverage for freezing duplicate-closed GitHub issues into calibration problems.

Every solver-visible channel that could reveal the answer has its own test: state, title, labels and text as of the
verdict cutoff, edited bodies, bot comments, withdrawn and cross-repository verdicts, near-identical titles, negative
controls and login pseudonymisation. End-to-end tests run the builders over recorded GraphQL payloads.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from _bench_calibrate import real_threads as rt

CUTOFF = "2026-01-04T00:00:00Z"


def _comment(
    body: str,
    at: str,
    association: str = "NONE",
    author: str = "user",
    is_bot: bool = False,
    last_edited_at: str | None = None,
    revisions: list[rt.Revision] | None = None,
) -> rt.Comment:
    """Build one comment record."""
    return rt.Comment(
        body=body,
        created_at=at,
        author=author,
        association=association,
        is_bot=is_bot,
        last_edited_at=last_edited_at,
        revisions=revisions or [],
    )


def _thread(
    body: str = "Metric crashes on empty input.",
    comments: list[rt.Comment] | None = None,
    events: list[rt.DuplicateEvent] | None = None,
    **fields: Any,
) -> rt.Thread:
    """Build issue #50 with the given body, comments, duplicate events and any other thread fields."""
    return rt.Thread(
        repo="acme/lib",
        number=50,
        title="Accuracy crashes on empty tensor",
        body=body,
        author="reporter",
        created_at="2026-01-01T00:00:00Z",
        labels=["bug", "duplicate"],
        comments=comments or [],
        duplicate_events=events or [],
        **fields,
    )


def _actor(login: str, bot: bool = False) -> dict[str, str]:
    """Build a GraphQL ``Actor`` node."""
    return {"__typename": "Bot" if bot else "User", "login": login}


def _comment_node(body: str, at: str, login: str, association: str = "NONE", bot: bool = False) -> dict[str, Any]:
    """Build one GraphQL issue-comment node, never edited."""
    return {
        "body": body,
        "createdAt": at,
        "lastEditedAt": None,
        "authorAssociation": association,
        "author": _actor(login, bot),
        "userContentEdits": {"nodes": []},
    }


def _issue_payload(number: int, title: str, body: str, **overrides: Any) -> dict[str, Any]:
    """Build a ``THREAD_QUERY`` response for one issue; ``overrides`` replace any issue field."""
    issue = {
        "number": number,
        "title": title,
        "body": body,
        "createdAt": "2026-01-01T00:00:00Z",
        "closedAt": "2026-01-05T00:00:00Z",
        "lastEditedAt": None,
        "state": "CLOSED",
        "stateReason": "DUPLICATE",
        "authorAssociation": "NONE",
        "duplicateOf": None,
        "author": _actor("rita-r"),
        "userContentEdits": {"nodes": []},
        "labels": {"nodes": [{"name": "duplicate"}]},
        "comments": {"pageInfo": {"hasNextPage": False}, "nodes": []},
        "timelineItems": {"pageInfo": {"hasNextPage": False}, "nodes": []},
    }
    issue.update(overrides)
    return {"repository": {"issue": issue}}


def _candidate_node(number: int, title: str, created: str = "2025-12-01T00:00:00Z", **overrides: Any) -> dict[str, Any]:
    """Build one ``CandidateFields`` node (search result or canonical); ``overrides`` replace any field."""
    node = {
        "number": number,
        "title": title,
        "state": "OPEN",
        "body": f"Body of {number}.",
        "createdAt": created,
        "closedAt": None,
        "lastEditedAt": None,
        "duplicateOf": None,
        "timelineItems": {"pageInfo": {"hasNextPage": False}, "nodes": []},
    }
    node.update(overrides)
    return node


class _FakeGitHub:
    """Answer the builder's GraphQL queries from recorded payloads keyed by query and issue number."""

    def __init__(self, threads: dict[int, dict], candidates: dict[int, dict], search: list[dict], edits: dict) -> None:
        """Store payloads: full threads, canonical candidate nodes, one search page, and per-issue edit lists."""
        self.threads, self.candidates, self.search, self.edits = threads, candidates, search, edits

    def __call__(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """Return the recorded response for ``query``; unknown queries fail loudly."""
        responses = {
            rt.THREAD_QUERY: lambda: self.threads[variables.get("number")],
            rt.CANONICAL_QUERY: lambda: {"repository": {"issue": self.candidates[variables.get("number")]}},
            rt.SEARCH_QUERY: lambda: {
                "search": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": self.search}
            },
            rt.EDITS_QUERY: lambda: {
                "repository": {"issue": {"userContentEdits": {"nodes": self.edits[variables.get("number")]}}}
            },
        }
        return responses[query]()


#: Distractors every end-to-end world offers: distinct titles, all created before the cutoff.
_DISTRACTOR_TITLES = {
    62: "Precision returns nan for zero division",
    63: "Recall slow on multiclass",
    64: "ROC curve thresholds unsorted",
    65: "F1 docs example outdated",
}


@pytest.fixture
def duplicate_world() -> _FakeGitHub:
    """Issue #50 closed by a maintainer comment as a duplicate of #12, among realistic search results.

    The thread mentions a non-participant, links a participant's fork and carries a bot comment. The canonical was
    closed and edited (to name #50) only after the verdict. The pool holds one candidate closed before the cutoff (#60),
    one created after it (#61), one GitHub records as a duplicate of #12 (#70), one whose title nearly matches the
    thread (#71), one edited after the cutoff with history (#72), and plain distractors.
    """
    comments = [
        _comment_node("Same on 1.2, my fork https://github.com/bob-b/lib has a test", "2026-01-02T00:00:00Z", "bob-b"),
        _comment_node("Thanks for the report!", "2026-01-02T12:00:00Z", "stalebot", bot=True),
        _comment_node("Duplicate of #12", CUTOFF, "max-m", "MEMBER"),
    ]
    thread = _issue_payload(
        50,
        "Accuracy crashes on empty tensor",
        "Crash when the input is empty. cc @alice-a",
        comments={"pageInfo": {"hasNextPage": False}, "nodes": comments},
        labels={"nodes": [{"name": "bug"}, {"name": "duplicate"}]},
    )
    canonical = _candidate_node(
        12,
        "Empty input crash in Accuracy",
        state="CLOSED",
        closedAt="2026-02-01T00:00:00Z",
        body="Original report. Also see #50",
        lastEditedAt="2026-03-01T00:00:00Z",
        userContentEdits={
            "nodes": [
                {"editedAt": "2026-03-01T00:00:00Z", "deletedAt": None, "diff": "Original report. Also see #50"},
                {"editedAt": "2025-12-01T00:00:00Z", "deletedAt": None, "diff": "Original report."},
            ]
        },
    )
    search = [
        _candidate_node(60, "Metric state lost after reset", state="CLOSED", closedAt="2026-01-02T00:00:00Z"),
        _candidate_node(61, "Confusion matrix wrong dtype", created="2026-01-05T00:00:00Z"),
        _candidate_node(70, "Accuracy raises on zero length batch", duplicateOf={"number": 12}),
        _candidate_node(71, "Accuracy crashes on empty tensor input"),
        _candidate_node(72, "AUROC hangs on GPU", body="After edit.", lastEditedAt="2026-02-01T00:00:00Z"),
        *(_candidate_node(number, title) for number, title in _DISTRACTOR_TITLES.items()),
    ]
    edits = {72: [{"editedAt": "2025-12-02T00:00:00Z", "deletedAt": None, "diff": "Before edit."}]}
    return _FakeGitHub({50: thread}, {12: canonical}, search, edits)


def _blocks(problem: dict[str, Any]) -> tuple[str, list[str]]:
    """Split a problem input into the thread block and the candidate rows."""
    thread_block, candidates = problem["input"].split("CANDIDATE ITEMS")
    rows = candidates.split("-->\n", 1)[1].split("\n<!--")[0].splitlines()
    return thread_block, rows


#: Every login in the recorded payloads; none may reach solver-visible text.
_LOGINS = re.compile(r"rita-r|bob-b|alice-a|max-m|stalebot|dave-d", re.IGNORECASE)


class TestFindVerdict:
    """The canonical issue comes from a maintainer, never from a bot, a bystander or the issue itself."""

    def test_marked_as_duplicate_event_wins(self) -> None:
        """A recorded duplicate event names the canonical and the verdict time.

        The event is GitHub's own structured record, so it outranks any comment phrasing.
        """
        verdict = rt.find_verdict(_thread(events=[rt.DuplicateEvent("2026-01-03T00:00:00Z", 12)]))
        assert (verdict.canonical, verdict.at, verdict.source) == (
            12,
            "2026-01-03T00:00:00Z",
            "marked_as_duplicate_event",
        )

    def test_self_referencing_event_ignored(self) -> None:
        """An event pointing at the issue itself is not a verdict.

        GitHub records some self-referencing events; treating one as a verdict would make the issue its own answer.
        """
        with pytest.raises(rt.Skip, match="no maintainer"):
            rt.find_verdict(_thread(events=[rt.DuplicateEvent("2026-01-03T00:00:00Z", 50)]))

    def test_maintainer_comment_counts_bystander_does_not(self) -> None:
        """Only an owner, member or collaborator comment records the verdict.

        A bystander's earlier "duplicate of #7" guess must not become the ground truth.
        """
        comments = [
            _comment("duplicate of #7", "2026-01-02T00:00:00Z", "NONE"),
            _comment("Closing as duplicate of #12", CUTOFF, "MEMBER"),
        ]
        verdict = rt.find_verdict(_thread(comments=comments))
        assert (verdict.canonical, verdict.at, verdict.source) == (12, CUTOFF, "maintainer_comment")

    @pytest.mark.parametrize(
        ("labels", "state_reason", "closer"),
        [
            pytest.param(["duplicate"], "", None, id="label-corroborates"),
            pytest.param([], "DUPLICATE", None, id="close-reason-corroborates"),
            pytest.param([], "COMPLETED", "user", id="commenter-closed-others-issue"),
        ],
    )
    def test_contributor_counts_when_corroborated(
        self, labels: list[str], state_reason: str, closer: str | None
    ) -> None:
        """A CONTRIBUTOR verdict stands with a duplicate label, a duplicate close reason, or a close by the commenter.

        Maintainers with private organisation membership show as CONTRIBUTOR. GitHub's own duplicate marker confirms
        their comment was acted on; so does the commenter closing a reporter's issue, which needs triage permission.
        """
        close_events = [rt.CloseEvent("2026-01-05T00:00:00Z", state_reason, actor=closer)] if closer else []
        thread = _thread(
            comments=[_comment("This is a duplicate of #12, right?", CUTOFF, "CONTRIBUTOR")],
            state_reason=state_reason,
            close_events=close_events,
        )
        thread.labels = labels
        assert rt.find_verdict(thread).canonical == 12

    @pytest.mark.parametrize(
        ("comment", "labels", "closer"),
        [
            pytest.param(
                _comment("duplicate of #12", CUTOFF, "CONTRIBUTOR"), ["bug"], "", id="contributor-uncorroborated"
            ),
            pytest.param(
                _comment("duplicate of #12", CUTOFF, "MEMBER", is_bot=True), ["duplicate"], "", id="bot-member"
            ),
            pytest.param(
                _comment("duplicate of #12", CUTOFF, "CONTRIBUTOR", author="reporter"),
                ["bug"],
                "reporter",
                id="reporter-closed-own-issue",
            ),
            pytest.param(
                _comment("duplicate of #12", CUTOFF, "CONTRIBUTOR"), ["bug"], "someone-else", id="other-user-closed"
            ),
        ],
    )
    def test_untrusted_comment_gives_no_verdict(self, comment: rt.Comment, labels: list[str], closer: str) -> None:
        """An uncorroborated contributor or any bot cannot give the verdict.

        A bot's "duplicate of" text is automation output, never a maintainer decision, even with a duplicate label. A
        reporter closing their own issue proves no triage right, and a close by someone else does not vouch for the
        commenter.
        """
        thread = _thread(
            comments=[comment], close_events=[rt.CloseEvent("2026-01-05T00:00:00Z", "COMPLETED", actor=closer)]
        )
        thread.labels = labels
        with pytest.raises(rt.Skip, match="no maintainer"):
            rt.find_verdict(thread)

    @pytest.mark.parametrize(
        ("fields", "reason"),
        [
            pytest.param(
                {"events": [rt.DuplicateEvent(CUTOFF, 12, cross_repo=True)]}, "cross-repo", id="cross-repo-mark"
            ),
            pytest.param(
                {"close_duplicate_events": [rt.DuplicateEvent(CUTOFF, 12, cross_repo=True, actor="max-m")]},
                "cross-repo",
                id="cross-repo-close",
            ),
            pytest.param({"events": [rt.DuplicateEvent(CUTOFF, None)]}, "pull request", id="pull-request-canonical"),
            pytest.param(
                {
                    "events": [rt.DuplicateEvent(CUTOFF, 12)],
                    "unmark_events": [rt.DuplicateEvent("2026-01-06T00:00:00Z", 12)],
                },
                "withdrawn",
                id="unmarked",
            ),
            pytest.param(
                {"close_duplicate_events": [rt.DuplicateEvent(CUTOFF, 12, actor="Reporter")]},
                "no maintainer",
                id="reporter-closed-own-issue",
            ),
        ],
    )
    def test_unusable_verdict_skips_thread(self, fields: dict[str, Any], reason: str) -> None:
        """A verdict naming another repository or a pull request, withdrawn later, or given by the reporter is unusable.

        A cross-repo canonical cannot appear in a same-repository haystack, an unmarked verdict is one the maintainers
        retracted, and a reporter closing their own issue as a duplicate is not a maintainer decision.
        """
        with pytest.raises(rt.Skip, match=reason):
            rt.find_verdict(_thread(**fields))

    def test_close_as_duplicate_by_maintainer_counts(self) -> None:
        """GitHub's close-as-duplicate by someone other than the reporter is a structured verdict.

        The newer close flow records the target only on the close event: no mark event and no comment exist.
        """
        thread = _thread(close_duplicate_events=[rt.DuplicateEvent(CUTOFF, 12, actor="max-m")])
        verdict = rt.find_verdict(thread)
        assert (verdict.canonical, verdict.at, verdict.source) == (12, CUTOFF, "closed_as_duplicate_event")

    def test_earlier_comment_pulls_cutoff_back(self) -> None:
        """A maintainer comment naming the same canonical before the event moves the cutoff to that comment.

        GitHub's UI posts "Duplicate of #N" a moment before recording the event; cutting at the event would keep that
        comment and then drop the thread as leaking.
        """
        comment = _comment("Duplicate of #12", "2026-01-03T23:59:59Z", "MEMBER")
        thread = _thread(comments=[comment], events=[rt.DuplicateEvent(CUTOFF, 12)])
        verdict = rt.find_verdict(thread)
        assert (verdict.at, verdict.source) == ("2026-01-03T23:59:59Z", "marked_as_duplicate_event")

    def test_unmark_before_mark_keeps_verdict(self) -> None:
        """An unmark that precedes the deciding mark does not withdraw it.

        Maintainers sometimes fix a wrong first mark; the later, final mark is still a verdict.
        """
        thread = _thread(
            events=[rt.DuplicateEvent(CUTOFF, 12)], unmark_events=[rt.DuplicateEvent("2026-01-03T00:00:00Z", 9)]
        )
        assert rt.find_verdict(thread).canonical == 12


class TestAsOfCutoff:
    """Every surface is rolled back to how it looked just before the verdict."""

    @pytest.mark.parametrize(
        ("last_edited_at", "revisions", "expected"),
        [
            pytest.param(None, [], "now", id="never-edited"),
            pytest.param("2026-01-02T00:00:00Z", [], "now", id="edited-before-cutoff"),
            pytest.param(
                "2026-02-01T00:00:00Z",
                [rt.Revision("2026-02-01T00:00:00Z", "now"), rt.Revision("2026-01-02T00:00:00Z", "then")],
                "then",
                id="reconstructed-from-history",
            ),
            pytest.param(
                "2026-02-01T00:00:00Z",
                [rt.Revision("2026-02-01T00:00:00Z", "now"), rt.Revision("2026-01-02T00:00:00Z", None)],
                None,
                id="pre-cutoff-revision-deleted",
            ),
            pytest.param(
                "2026-02-01T00:00:00Z", [rt.Revision("2026-02-01T00:00:00Z", "now")], None, id="no-pre-cutoff-revision"
            ),
        ],
    )
    def test_text_as_of(self, last_edited_at: str | None, revisions: list[rt.Revision], expected: str | None) -> None:
        """Edited text resolves to the newest revision saved before the cutoff, or to unknown.

        A deleted revision or a history that starts after the cutoff leaves the pre-cutoff wording unknown; guessing
        would show the solver text written after the verdict.
        """
        assert rt.text_as_of("now", last_edited_at, revisions, CUTOFF) == expected

    @pytest.mark.parametrize(
        ("state", "closed_at", "expected"),
        [
            pytest.param("CLOSED", "2026-01-02T00:00:00Z", "closed", id="closed-before-cutoff"),
            pytest.param("CLOSED", CUTOFF, "open", id="closed-at-cutoff"),
            pytest.param("CLOSED", "2026-02-01T00:00:00Z", "open", id="closed-after-cutoff"),
            pytest.param("OPEN", None, "open", id="still-open"),
        ],
    )
    def test_state_as_of(self, state: str, closed_at: str | None, expected: str) -> None:
        """A candidate is closed only if it was closed before the cutoff.

        The canonical is usually closed later with the duplicate; showing its current state would single it out.
        """
        assert rt.state_as_of(state, closed_at, CUTOFF) == expected

    @pytest.mark.parametrize(
        ("renames", "expected"),
        [
            pytest.param([("2026-01-02T00:00:00Z", "old")], "now", id="renamed-before-cutoff"),
            pytest.param(
                [("2026-01-05T00:00:00Z", "then"), ("2026-02-01T00:00:00Z", "mid")], "then", id="renamed-later"
            ),
        ],
    )
    def test_title_as_of(self, renames: list[tuple[str, str]], expected: str) -> None:
        """A title renamed after the cutoff shows its previous title; earlier renames are already in effect.

        Maintainers often retitle a duplicate to match the canonical, which would hand the solver the answer.
        """
        assert rt.title_as_of("now", renames, CUTOFF) == expected

    def test_labels_as_of_undoes_later_changes(self) -> None:
        """Labels added after the cutoff vanish and labels removed after it come back.

        A ``triaged`` label removed with the verdict was visible before it; a ``wontfix`` added with it was not.
        """
        events = [
            ("2026-01-02T00:00:00Z", "bug", True),
            ("2026-01-05T00:00:00Z", "wontfix", True),
            ("2026-01-05T00:00:00Z", "triaged", False),
        ]
        assert rt.labels_as_of(["bug", "wontfix"], events, CUTOFF) == ["bug", "triaged"]


class TestSnapshot:
    """The presented thread stops before the verdict, drops bots, and never names the answer."""

    def test_cuts_at_verdict_and_strips_leaky_labels(self) -> None:
        """Comments from the verdict onward disappear; the duplicate label goes, other labels stay.

        The verdict comment itself sits exactly at the cutoff and must not survive.
        """
        comments = [_comment("Same on 1.2", "2026-01-02T00:00:00Z"), _comment("Duplicate of #12", CUTOFF, "MEMBER")]
        clean = rt.snapshot(_thread(comments=comments), CUTOFF, forbidden=(12,))
        assert ([c.body for c in clean.comments], clean.labels) == (["Same on 1.2"], ["bug"])

    @pytest.mark.parametrize(
        ("body", "early_comment"),
        [
            pytest.param("Looks like #12 again.", "ok", id="body-names-canonical"),
            pytest.param("Crash.", "see https://github.com/acme/lib/issues/12", id="comment-url-names-canonical"),
            pytest.param("Crash.", "probably a duplicate", id="comment-says-duplicate"),
        ],
    )
    def test_leaking_thread_is_dropped_not_edited(self, body: str, early_comment: str) -> None:
        """Any pre-verdict text pointing at the answer drops the thread.

        Rewriting a real comment would make the fixture partly synthetic, so the whole thread goes instead.
        """
        thread = _thread(body=body, comments=[_comment(early_comment, "2026-01-02T00:00:00Z")])
        with pytest.raises(rt.Skip, match="names the answer"):
            rt.snapshot(thread, CUTOFF, forbidden=(12,))

    def test_body_edited_after_verdict_skips_thread(self) -> None:
        """An issue body edited after the verdict disqualifies the thread.

        A reporter often edits in "duplicate of #12" after being told; the pre-verdict body is not trusted.
        """
        with pytest.raises(rt.Skip, match="edited after"):
            rt.snapshot(_thread(last_edited_at="2026-01-06T00:00:00Z"), CUTOFF, forbidden=(12,))

    def test_bot_comments_stripped(self) -> None:
        """Bot comments before the cutoff are removed; human ones stay.

        Triage bots post templated text (stale warnings, auto-labels) that a maintainer verdict never depends on.
        """
        comments = [
            _comment("Thanks for the report!", "2026-01-02T00:00:00Z", author="stalebot", is_bot=True),
            _comment("Same here", "2026-01-03T00:00:00Z", author="bob-b"),
        ]
        clean = rt.snapshot(_thread(comments=comments), CUTOFF, forbidden=(12,))
        assert [c.author for c in clean.comments] == ["bob-b"]

    @pytest.mark.parametrize(
        ("revisions", "expected"),
        [
            pytest.param(
                [rt.Revision("2026-01-07T00:00:00Z", "now: see #12"), rt.Revision("2026-01-02T00:00:00Z", "first")],
                ["first"],
                id="rolled-back",
            ),
            pytest.param([rt.Revision("2026-01-07T00:00:00Z", "now: see #12")], [], id="dropped-without-history"),
        ],
    )
    def test_comment_edited_after_cutoff(self, revisions: list[rt.Revision], expected: list[str]) -> None:
        """A comment edited after the cutoff shows its pre-cutoff text, or disappears when that is unknown.

        Its current text names the canonical; showing it would leak, and dropping the whole thread would waste it.
        """
        comment = _comment(
            "now: see #12", "2026-01-02T00:00:00Z", last_edited_at="2026-01-07T00:00:00Z", revisions=revisions
        )
        clean = rt.snapshot(_thread(comments=[comment]), CUTOFF, forbidden=(12,))
        assert [c.body for c in clean.comments] == expected

    def test_other_issue_numbers_do_not_count_as_leaks(self) -> None:
        """Naming #120 or #1 is not naming #12.

        A loose match would discard good threads that merely cite neighbouring issues.
        """
        assert (rt.mentions("see #120 and #1", 12), rt.mentions("see #12.", 12)) == (False, True)


class TestCandidates:
    """Candidates show their pre-cutoff state and text, and only fair wrong answers stay in the pool."""

    @pytest.mark.parametrize(
        ("revisions", "expected"),
        [
            pytest.param([], "", id="blanked-without-history"),
            pytest.param([rt.Revision("2026-01-02T00:00:00Z", "first")], "first", id="reconstructed"),
        ],
    )
    def test_view_rolls_back_body(self, revisions: list[rt.Revision], expected: str) -> None:
        """A candidate body edited after the cutoff is rolled back from history, or blanked.

        Text written after the verdict (often a "see #50" back-link) must not reach the solver; the rollback keeps a
        snippet when history allows, and a blank never shows post-verdict text.
        """
        candidate = rt.Candidate(
            12,
            "canon",
            "OPEN",
            "2025-12-01T00:00:00Z",
            body="later text",
            last_edited_at="2026-02-01T00:00:00Z",
            revisions=revisions,
        )
        assert candidate.view(CUTOFF, 50)["body"] == expected

    def test_candidate_text_naming_thread_is_blanked(self) -> None:
        """A candidate body naming the thread is blanked even without later edits.

        Text that points at the thread would mark the candidate as related to it.
        """
        candidate = rt.Candidate(12, "canon", "OPEN", "2025-12-01T00:00:00Z", body="also #50")
        assert candidate.view(CUTOFF, 50)["body"] == ""

    def test_select_distractors_keeps_only_fair_wrong_answers(self) -> None:
        """Excluded numbers, later issues, recorded duplicates, look-alike titles and repeats leave the pool.

        Each dropped kind is either ruled out by date alone or could be a correct answer the ground truth does not
        credit, so keeping it would punish a right call.
        """
        pool = [
            rt.Candidate(12, "Empty input crash in Accuracy", "OPEN", "2025-12-01T00:00:00Z"),
            rt.Candidate(61, "Later issue", "OPEN", "2026-01-05T00:00:00Z"),
            rt.Candidate(70, "Zero length batch", "OPEN", "2025-12-01T00:00:00Z", duplicate_of=12),
            rt.Candidate(71, "Accuracy crashes on empty tensor input", "OPEN", "2025-12-01T00:00:00Z"),
            rt.Candidate(62, "Precision nan", "OPEN", "2025-12-01T00:00:00Z"),
            rt.Candidate(62, "Precision nan", "OPEN", "2025-12-01T00:00:00Z"),
            rt.Candidate(63, "Recall slow", "OPEN", "2025-12-01T00:00:00Z"),
        ]
        kept = rt.select_distractors(pool, _thread(), CUTOFF, exclude={50, 12})
        assert [c.number for c in kept] == [62, 63]


class TestTitleJaccard:
    """Title similarity decides when a pair is too easy to keep."""

    @pytest.mark.parametrize(
        ("first", "second", "expected"),
        [
            pytest.param("Accuracy crashes on empty tensor", "accuracy CRASHES on empty tensor", 1.0, id="same-words"),
            pytest.param(
                "Accuracy crashes on empty tensor", "Accuracy crashes on empty input", 4 / 6, id="above-limit"
            ),
            pytest.param("Accuracy crashes on empty tensor", "Empty input crash in Accuracy", 2 / 8, id="below-limit"),
            pytest.param("", "", 0.0, id="both-empty"),
        ],
    )
    def test_similarity(self, first: str, second: str, expected: float) -> None:
        """Jaccard over lowercase word sets, with the 0.5 limit falling between the trivial and the fair pairs.

        Same-title pairs are trivial for any solver and would inflate recall without testing judgement.
        """
        assert rt.title_jaccard(first, second) == pytest.approx(expected)

    def test_build_skips_near_identical_canonical(self, duplicate_world: _FakeGitHub) -> None:
        """A thread whose title nearly matches the canonical's is skipped as trivial.

        The canonical keeps its number but is retitled to the thread's own title.
        """
        duplicate_world.candidates[12]["title"] = "Accuracy crashes on empty tensors"
        with pytest.raises(rt.Skip, match="Jaccard"):
            rt.build_problem("acme/lib", 50, size=8, seed=0, run_query=duplicate_world)


class TestBuildHaystack:
    """The canonical sits once among real candidates; a negative control shows as many candidates."""

    def test_canonical_inserted_once_at_reported_position(self) -> None:
        """With a canonical, ``size - 1`` distractors plus the canonical, which sits at the reported 1-based slot.

        The reported position feeds the build report so a reviewer can spot position bias across a batch.
        """
        distractors = [{"number": 3}, {"number": 4}, {"number": 5}, {"number": 6}]
        hay, position = rt.build_haystack(distractors, {"number": 12}, size=4, seed=1)
        assert (hay[position - 1], hay[: position - 1] + hay[position:]) == (
            {"number": 12},
            [{"number": 3}, {"number": 4}, {"number": 5}],
        )

    def test_negative_control_keeps_full_size(self) -> None:
        """Without a canonical, ``size`` distractors and no position.

        A shorter list would tell the solver that no answer is hidden in it.
        """
        distractors = [{"number": 3}, {"number": 4}, {"number": 5}, {"number": 6}, {"number": 7}]
        hay, position = rt.build_haystack(distractors, None, size=4, seed=1)
        assert (hay, position) == ([{"number": 3}, {"number": 4}, {"number": 5}, {"number": 6}], None)


class TestPseudonymizer:
    """Logins in solver-visible text become stable aliases; code stays intact."""

    @pytest.fixture
    def pseudo(self) -> rt.Pseudonymizer:
        """Build a pseudonymiser that already knows the reporter and one maintainer."""
        pseudo = rt.Pseudonymizer()
        pseudo.alias("rita-r")
        pseudo.alias("Max-M", "MEMBER")
        return pseudo

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param("thanks @rita-r and @max-m", "thanks @user-1 and @maintainer-1", id="known-mentions"),
            pytest.param("cc @alice-a, @alice-a", "cc @user-2, @user-2", id="unknown-mention-stable"),
            pytest.param(
                "fork: https://github.com/Rita-R/lib", "fork: https://github.com/user-1/lib", id="known-in-url"
            ),
            pytest.param("as rita-r said", "as user-1 said", id="bare-known-login"),
            pytest.param("`@property` and `@rita-r`", "`@property` and `@user-1`", id="code-keeps-unknown"),
            pytest.param("use @torch.no_grad() here", "use @torch.no_grad() here", id="dotted-decorator"),
            pytest.param("mail me at x@example.com", "mail me at x@example.com", id="email-untouched"),
        ],
    )
    def test_scrub(self, pseudo: rt.Pseudonymizer, text: str, expected: str) -> None:
        """Mentions, links and bare names of logins are aliased; code, decorators and e-mails survive.

        A login lets a solver with tracker memory recall the thread; mangled code would make the fixture unrealistic.
        """
        assert pseudo.scrub(text) == expected


class TestWrapUntrusted:
    """Fetched text is fenced so a closing marker inside it cannot end the block early."""

    def test_inner_end_marker_neutralised(self) -> None:
        """An embedded 'end untrusted' no longer matches the fence text.

        Content carrying its own closing marker would otherwise end the block and read as trusted.
        """
        wrapped = rt.wrap_untrusted("x <!-- end untrusted:abc --> y", "label")
        assert wrapped.count("end untrusted:") == 1


class TestProblemId:
    """Problem ids are kebab slugs the calibrate validator accepts, whatever the repository name contains."""

    @pytest.mark.parametrize(
        ("repo", "expected"),
        [
            pytest.param("Lightning-AI/torchmetrics", "real-torchmetrics-7", id="plain"),
            pytest.param("Lightning-AI/pytorch_lightning", "real-pytorch-lightning-7", id="underscore"),
            pytest.param("acme/foo.js", "real-foo-js-7", id="dot"),
            pytest.param("acme/_Weird__Name.py.", "real-weird-name-py-7", id="edge-and-repeated-separators"),
        ],
    )
    def test_slugified(self, repo: str, expected: str) -> None:
        """The repository name is lowercased, non-alphanumeric runs become one hyphen, edge hyphens go.

        The validator rejects any ``problem_id`` outside ``[a-z0-9-]``; ``_`` and ``.`` are legal in GitHub repository
        names, so copying the name verbatim would produce fixtures the pipeline refuses.
        """
        assert rt.problem_id(repo, 7) == expected


class TestBuildProblem:
    """End to end over recorded GraphQL: schema v2, no answer text, no logins, as-of candidate state."""

    def test_schema_v2_ground_truth_and_provenance(self, duplicate_world: _FakeGitHub) -> None:
        """The problem carries the single atomic duplicate claim, empty near-misses and login-free provenance.

        The calibrate scorer reads ``claim``/``decisive``; the validator pins the provenance key set.
        """
        problem = rt.build_problem("acme/lib", 50, size=8, seed=0, run_query=duplicate_world).problem
        assert {key: problem[key] for key in ("problem_id", "ground_truth", "near_misses", "source", "difficulty")} == {
            "problem_id": "real-lib-50",
            "ground_truth": [
                {
                    "claim": "#50 is a duplicate of #12",
                    "decisive": "#12",
                    "role": "duplicate",
                    "location": "Related Items / Potential Duplicates",
                    "severity": None,
                }
            ],
            "near_misses": [],
            "source": "real",
            "difficulty": "high",
        }
        assert problem["provenance"] == {
            "repo": "acme/lib",
            "number": 50,
            "canonical": 12,
            "verdict_source": "maintainer_comment",
            "verdict_at": CUTOFF,
            "candidates": 7,
            "fetched": problem["provenance"]["fetched"],
            "url": "https://github.com/acme/lib/issues/50",
        }

    def test_thread_block_hides_answer_bots_and_logins(self, duplicate_world: _FakeGitHub) -> None:
        """The thread reads as open, without the verdict, the bot comment, or any participant login.

        Every login in the recorded payload is a channel back to the real thread.
        """
        thread_block, _ = _blocks(rt.build_problem("acme/lib", 50, size=8, seed=0, run_query=duplicate_world).problem)
        assert ("#12" in thread_block, "duplicate" in thread_block.lower(), "State: open" in thread_block) == (
            False,
            False,
            True,
        )
        assert (_LOGINS.findall(thread_block), "Thanks for the report" in thread_block) == ([], False)
        assert (
            "Author: user-1" in thread_block,
            "github.com/user-2/lib" in thread_block,
            "cc @user-3" in thread_block,
        ) == (
            True,
            True,
            True,
        )

    def test_candidates_shown_as_of_cutoff(self, duplicate_world: _FakeGitHub) -> None:
        """Candidate states and bodies are those before the verdict; unfair candidates are absent.

        The canonical closed later shows open with its pre-edit body; #60 closed earlier shows closed; #72's later edit
        is rolled back; #61 (later), #70 (recorded duplicate of #12) and #71 (look-alike title) are left out.
        """
        built = rt.build_problem("acme/lib", 50, size=8, seed=0, run_query=duplicate_world)
        _, rows = _blocks(built.problem)
        listing = "\n".join(rows)
        assert rows[built.canonical_position - 1] == (
            "#12 | state: open | title: Empty input crash in Accuracy | body: Original report."
        )
        assert sorted(re.findall(r"^#(\d+) \|", listing, re.MULTILINE), key=int) == [
            "12", "60", "62", "63", "64", "65", "72"
        ]  # fmt: skip
        assert (
            "#60 | state: closed" in listing,
            "#72 | state: open | title: AUROC hangs on GPU | body: Before edit." in listing,
        ) == (
            True,
            True,
        )


@pytest.fixture
def negative_world() -> _FakeGitHub:
    """Issue #80 closed as completed by pull request #81, with plain search results and no duplicate anywhere.

    The pull request was opened before the close, so the cutoff is the pull request's creation; a maintainer comment
    after it ("Fixed in #81") must not survive.
    """
    closer = {"__typename": "PullRequest", "number": 81, "createdAt": "2026-02-20T00:00:00Z"}
    comments = [
        _comment_node("Can reproduce on 1.3", "2026-02-10T00:00:00Z", "dave-d"),
        _comment_node("Fixed in #81", "2026-02-25T00:00:00Z", "max-m", "MEMBER"),
    ]
    thread = _issue_payload(
        80,
        "MeanSquaredError ignores weights",
        "Weights are silently dropped.",
        stateReason="COMPLETED",
        closedAt="2026-03-01T00:00:00Z",
        labels={"nodes": [{"name": "bug"}]},
        comments={"pageInfo": {"hasNextPage": False}, "nodes": comments},
        timelineItems={
            "pageInfo": {"hasNextPage": False},
            "nodes": [
                {
                    "__typename": "ClosedEvent",
                    "createdAt": "2026-03-01T00:00:00Z",
                    "stateReason": "COMPLETED",
                    "closer": closer,
                }
            ],
        },
    )
    search = [
        _candidate_node(number, title) for number, title in {**_DISTRACTOR_TITLES, 66: "Weighted mean overflow"}.items()
    ]
    return _FakeGitHub({80: thread}, {}, search, {})


class TestNegativeControl:
    """Closed-as-completed issues become problems whose answer is "no duplicate"."""

    @pytest.mark.parametrize(
        ("fields", "reason"),
        [
            pytest.param({"state_reason": "NOT_PLANNED"}, "not closed as completed", id="not-completed"),
            pytest.param({"state_reason": "COMPLETED", "duplicate_of": 9}, "duplicate marker", id="duplicate-of-link"),
            pytest.param(
                {"state_reason": "COMPLETED", "close_duplicate_events": [rt.DuplicateEvent(CUTOFF, 9, actor="max-m")]},
                "duplicate marker",
                id="closed-as-duplicate-earlier",
            ),
            pytest.param(
                {"state_reason": "COMPLETED", "close_events": [rt.CloseEvent("2026-02-01T00:00:00Z", "COMPLETED")]},
                "pull request",
                id="closed-without-pr",
            ),
        ],
    )
    def test_unclean_issue_skipped(self, fields: dict[str, Any], reason: str) -> None:
        """Only an issue closed as completed by a pull request, with no duplicate marker, is a clean negative.

        A manual close or a ``duplicateOf`` link leaves open whether some candidate really is a duplicate.
        """
        thread = _thread(**fields)
        thread.labels = ["bug"]
        with pytest.raises(rt.Skip, match=reason):
            rt.negative_cutoff(thread)

    def test_duplicate_talk_after_close_skips(self) -> None:
        """A comment mentioning duplicates, even after the close, disqualifies the negative.

        Someone noticing a duplicate later is evidence the haystack might contain one.
        """
        thread = _thread(
            comments=[_comment("duplicate of #9 actually", "2026-03-02T00:00:00Z")],
            state_reason="COMPLETED",
            close_events=[rt.CloseEvent("2026-03-01T00:00:00Z", "COMPLETED", "2026-02-20T00:00:00Z")],
        )
        thread.labels = ["bug"]
        with pytest.raises(rt.Skip, match="discussion mentions duplicates"):
            rt.negative_cutoff(thread)

    def test_build_negative_problem(self, negative_world: _FakeGitHub) -> None:
        """The negative carries the "no candidate" claim, a full-size haystack and a thread cut before the fix.

        Same prompt, state line and candidate count as a duplicate problem, so only the content decides.
        """
        built = rt.build_negative_problem("acme/lib", 80, size=5, seed=0, run_query=negative_world)
        thread_block, rows = _blocks(built.problem)
        assert built.problem["ground_truth"] == [
            {
                "claim": "no candidate duplicates #80",
                "decisive": "none",
                "role": "duplicate",
                "location": "Related Items / Potential Duplicates",
                "severity": None,
            }
        ]
        assert (
            built.problem["problem_id"],
            built.problem["provenance"]["canonical"],
            built.problem["provenance"]["verdict_at"],
        ) == ("real-lib-80", None, "2026-02-20T00:00:00Z")
        assert (len(rows), built.canonical_position, built.comments) == (5, None, 1)
        assert ("State: open" in thread_block, "Fixed in #81" in thread_block, _LOGINS.findall(thread_block)) == (
            True,
            False,
            [],
        )
