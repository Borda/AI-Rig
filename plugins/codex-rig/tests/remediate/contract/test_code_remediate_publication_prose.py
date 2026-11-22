"""Pin the publication gate prose to the helper it tells the agent to run."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from _modules import load_module

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
PUBLICATION = load_module("worktree_publication", PLUGIN_ROOT / "shared" / "worktree_publication.py")
CONTRACT = PLUGIN_ROOT / "shared" / "native-skill-contract.md"
REFERENCES = PLUGIN_ROOT / "skills" / "code-remediate" / "references"
INTAKE = REFERENCES / "intake.md"
COMPLETION = REFERENCES / "completion.md"
SELECTION = REFERENCES / "selection.md"
ANSWER_RECORD = "`publication-answer.json`"


def _gate_step(number: int) -> str:
    """Return one numbered step of the PR worktree publication gate."""
    lines = CONTRACT.read_text(encoding="utf-8").splitlines()
    start = lines.index("### PR worktree publication gate")
    prefix = f"{number}. "
    return next(line for line in lines[start:] if line.startswith(prefix))


def _sentence(text: str, fragment: str) -> str:
    """Return the one sentence of a prose line that holds ``fragment``."""
    return next(sentence for sentence in text.split(". ") if fragment in sentence)


def test_push_permission_is_read_before_the_offer() -> None:
    """A fork without maintainer edits, or a user without push rights, is never asked a question that cannot push."""
    step = _gate_step(1)
    check = _sentence(step, "`maintainerCanModify`")
    for fragment in ("`maintainer_can_modify`", "`viewerPermission`", "audited GitHub reader", "base repository"):
        assert fragment in check
    # The rule itself: push rights on the head repository suffice; maintainer edits matter only for a fork PR.
    assert (
        "continue only with `WRITE`, `MAINTAIN` or `ADMIN` on the head repository, or for a cross-repository PR with "
        "`maintainer_can_modify` true and one of those permissions on the base repository"
    ) in check
    assert step.index("`viewerPermission`") < step.index("write no offer")


def test_selection_names_the_permission_check_the_gate_performs() -> None:
    """The receipt step points at the gate's own push-permission read, so write access is not left unchecked."""
    line = next(
        line for line in SELECTION.read_text(encoding="utf-8").splitlines() if "PR worktree publication gate" in line
    )
    assert "`maintainer_can_modify`" in _sentence(line, "PR worktree publication gate")
    assert "push permission" in _sentence(line, "PR worktree publication gate")


def test_the_helper_rehearses_the_push_before_the_marker() -> None:
    """Step 4 names the authenticated dry run, its shared transport settings and the code it fails with."""
    step = _gate_step(4)
    rehearsal = _sentence(step, "`git push --dry-run --porcelain`")
    assert "same argument list, credential options, transport settings and trusted pre-push check" in rehearsal
    refusal = _sentence(step, "`publication-push-precheck-failed`")
    assert "before the execution marker is written" in refusal
    assert "does not prove" in _sentence(step, "successful rehearsal")


def test_the_answer_is_recorded_before_any_push_route() -> None:
    """Step 2 writes the answer record before the recheck, and step 3 consumes only a recorded Approve."""
    two, three = _gate_step(2), _gate_step(3)
    assert two.index(ANSWER_RECORD) < two.index("After approval recheck")
    record = _sentence(two, ANSWER_RECORD)
    assert "temporary file in the run directory renamed into place" in record
    assert "exactly `offer_digest`, `decision_id` and `answer`" in record
    assert "A recorded Deny makes the disposition `declined`, also on resume" in two
    assert "never authorizes a push" in _sentence(two, "recorded Approve")
    assert three.index(ANSWER_RECORD) < three.index("consume the approval")


def test_completion_reports_a_recorded_deny_as_declined() -> None:
    """The terminal record step reads the same answer record, so a Deny is never reported as skipped."""
    line = next(
        line
        for line in COMPLETION.read_text(encoding="utf-8").splitlines()
        if "Resolve the optional native publication decision" in line
    )
    assert ANSWER_RECORD in _sentence(line, "Resolve the optional native publication decision")
    assert f"Use `declined` for a Deny recorded in {ANSWER_RECORD}" in line


def test_policy_is_checked_before_the_approval_is_consumed() -> None:
    """A policy that disallows publication leaves no attempt record, so the run reports skipped, not failed."""
    step = _gate_step(3)
    check = step.index("active policy allows a runtime approval")
    assert check < step.index("consume the approval")
    assert check < step.index("Write `publication-attempt.json`")
    assert "write no `publication-attempt.json`" in step
    assert "without requesting runtime approval" not in step


def test_the_credential_option_is_named_with_every_supported_choice() -> None:
    """The agent is told the flag, why it is needed, and exactly which helpers the script accepts."""
    step = _gate_step(4)
    assert "one `--credential-helper <name>` for each helper collected below, in the collected order" in step
    assert "never used" in step
    for helper in PUBLICATION.CREDENTIAL_HELPERS:
        assert f"`{helper}`" in step
    assert "never by guess" in step
    assert "`publication-credential-helper-missing` before the execution marker is written" in _sentence(
        step, "A call without `--credential-helper`"
    )


def test_every_configured_helper_is_collected_in_git_order() -> None:
    """Step 4 reads every helper entry in Git's order, since a single-value query can miss the one that signs in."""
    step = _gate_step(4)
    read = _sentence(step, "Git applies every configured helper entry")
    for fragment in (
        "in configuration order",
        "`git config --get-urlmatch credential.helper <url>` prints only one of them",
        "`git config --get-all credential.helper` omits URL-scoped entries",
        "`git config --get-regexp '^credential\\..*helper$'` in the checkout",
        "walk its output lines in order",
    ):
        assert fragment in read
    walk = _sentence(step, "A `credential.helper` line and a `credential.https://github.com.helper` line apply")
    for fragment in (
        "an empty value on such a line clears every helper collected before it",
        "other than `github.com` in any letter case (no `*`, `@` or port)",
        "is passed over",
        "any other scoped helper line",
        "a helper line with no value at all fail the check",
    ):
        assert fragment in walk
    unsupported = _sentence(step, "Pass the collected helpers only when")
    assert "at least one and every one is" in unsupported
    assert "when nothing is collected or any collected helper names anything else" in unsupported
    assert step.index("Git applies every configured helper entry") < step.index("Pass the collected helpers only")


def test_the_credential_helper_is_chosen_before_the_approval_is_consumed() -> None:
    """Step 3 checks that the helpers can be collected, so an unfit sign-in skips publication without an attempt."""
    three, four = _gate_step(3), _gate_step(4)
    assert "the credential helpers can be collected as step 4 describes" in three
    assert three.index("can be collected") < three.index("consume the approval")
    assert "Collect the helpers before step 3 consumes the approval" in four
    assert "the check in step 3 fails and publication is skipped" in four


def test_the_offer_url_is_spelled_exactly_as_the_helper_validates_it() -> None:
    """The prose gives the literal ``.git`` form the helper's URL pattern requires."""
    step = _gate_step(1)
    assert "`https://github.com/<owner>/<repo>.git`" in step
    assert PUBLICATION.GITHUB_URL.fullmatch("https://github.com/owner/repo.git")
    assert not PUBLICATION.GITHUB_URL.fullmatch("https://github.com/owner/repo")


def test_the_gate_names_the_helper_statuses_by_the_marker() -> None:
    """Step 4 uses the helper's own status names: ``not-sent`` without a marker, ``failed`` once it exists."""
    step = _gate_step(4)
    assert PUBLICATION._failure_status(None, "publication-push-precheck-failed") == "not-sent"
    statuses = _sentence(step, "It reports `not-sent`")
    assert "while no marker exists in the run" in statuses
    assert "the approved attempt is unspent" in statuses
    assert "`failed` once the marker exists" in statuses
    assert "never permits another run of the helper" in _sentence(step, "Once the marker exists")


def test_a_push_whose_outcome_is_unknown_is_reported_unverified() -> None:
    """Step 4 and completion map the helper's unknown-outcome result to ``unverified``, never to ``failed``."""
    statuses = _sentence(_gate_step(4), "It reports `not-sent`")
    assert f"`unverified` with `{PUBLICATION.OUTCOME_UNKNOWN}`" in statuses
    assert "the remote may hold the update" in statuses
    completion = next(
        line
        for line in COMPLETION.read_text(encoding="utf-8").splitlines()
        if "Resolve the optional native publication decision" in line
    )
    mapping = _sentence(completion, "Use `failed` for a started attempt")
    assert "including every `unverified` result of the helper" in mapping
    for reason in PUBLICATION.UNVERIFIED_REASONS:
        assert f"`{reason}`" in statuses
        assert f"`{reason}`" in mapping


def test_a_sign_in_refusal_allows_one_retry_before_the_terminal_record() -> None:
    """A refused rehearsal can run once more under the same approval, before ``publication.json`` closes the run."""
    step = _gate_step(4)
    retry = _sentence(step, "After `not-sent` with `publication-push-precheck-failed`")
    assert "exactly one rerun of the same helper in this invocation" in retry
    assert "before any terminal record" in retry
    assert step.index("It reports `not-sent`") < step.index("After `not-sent`")
    question = _sentence(step, "pr-worktree-publication-retry-")
    for fragment in ("native User Questions", "blocking form only", "**Retry**", "**Finish locally**"):
        assert fragment in question
    slot = _sentence(step, "Release the checkout slot while this question is open")
    assert "reacquire it with the original owner ID" in slot
    assert (
        step.index("pr-worktree-publication-retry-")
        < step.index("Release the checkout slot")
        < step.index("On **Retry**")
    )
    assert "timed-out remote read or rehearsal" in _sentence(step, "`publication-push-precheck-failed`")
    rerun = _sentence(step, "On **Retry**")
    for fragment in (
        "keep `publication-attempt.json` and the offer unchanged",
        "ask no new publication question",
        "repeat step 2's post-approval recheck and step 3's policy and credential-helper check",
        "fresh one-time runtime approval for the same helper command",
        "collecting the `--credential-helper` options again as above",
        "Approval Brief that also cites the `not-sent` result and its reason, the absent execution marker and terminal "
        "record",
        "the runtime policy admits exactly this one retry and rejects any other",
    ):
        assert fragment in rerun
    ending = _sentence(step, "**Finish locally**, a dismissed")
    for fragment in ("a runtime denial", "a second `not-sent` result", "any other `not-sent` reason", "`skipped`"):
        assert fragment in ending


def test_the_retry_names_exactly_the_helpers_terminal_records() -> None:
    """Every terminal-record list in step 4 equals the records that make the helper refuse to run."""
    step = _gate_step(4)
    listed = re.findall(r"terminal record \(([^)]*)\)", step)
    assert len(listed) == 2
    for names in listed:
        assert set(re.findall(r"`([^`]+)`", names)) == set(PUBLICATION.TERMINAL_RECORDS)


def test_a_shallow_source_refusal_is_named_and_ends_without_a_retry() -> None:
    """Step 4 names the helper's shallow-history refusal, says it sends nothing, and tells the user what to fix."""
    step = _gate_step(4)
    shallow = _sentence(step, "`publication-source-history-incomplete`")
    for fragment in (
        "shallow clone",
        "before the execution marker is written",
        "that reason allows no retry and publication ends `skipped`",
        "`git fetch --unshallow`",
    ):
        assert fragment in shallow
    assert step.index("`publication-source-history-incomplete`") < step.index("After `not-sent`")


def test_an_attempt_without_a_marker_is_reported_and_resumed_as_skipped() -> None:
    """Step 5, completion and the report-only resume all key the disposition on the execution marker."""
    five = _gate_step(5)
    assert "it is `skipped` whatever stopped it" in _sentence(five, "no execution marker sent nothing")
    assert "`failed` and `unverified` apply only once the marker exists" in five
    assert "not even the single retry" in _sentence(five, "resumed report-only delivery")
    completion = next(
        line
        for line in COMPLETION.read_text(encoding="utf-8").splitlines()
        if "Resolve the optional native publication decision" in line
    )
    assert "an approved attempt ended while no execution marker" in _sentence(completion, "Use `skipped`")
    assert "Use `failed` for a started attempt (execution marker present)" in completion
    assert "is `skipped` while no execution marker exists and `unverified` once it does" in completion
    order = _sentence(completion, "Write this record only after")
    assert "single retry after `publication-push-precheck-failed`" in order
    assert "the helper refuses to run once `publication.json` exists" in order


@pytest.mark.parametrize(
    "fragment",
    [
        pytest.param(
            "an approved attempt that ended while no execution marker (`publication-executed`) existed",
            id="unsent-approved-attempt-resumes-skipped",
        ),
        pytest.param("It never runs the publication helper, not even the gate's single retry", id="no-retry-on-resume"),
        pytest.param(
            "Acquire the checkout slot with this invocation's own ID before any durable write of this continuation",
            id="slot-before-durable-write",
        ),
        pytest.param("through a temporary file in the run directory renamed into place", id="atomic-terminal-record"),
        pytest.param("is rewritten by this continuation as the same `skipped` terminal record", id="malformed-record"),
        pytest.param("exactly `schema_version`, `status`, `reason` and `observed_oid`", id="four-record-fields"),
        pytest.param("never copying its `evidence` or `evidence_sha256` fields", id="no-evidence-fields"),
        pytest.param("a recorded Deny is reported as `declined`", id="recorded-deny-declined"),
        pytest.param("a recorded Approve with no consumed attempt", id="recorded-approve-skipped"),
    ],
)
def test_report_only_resume_writes_the_terminal_record_under_the_slot(fragment: str) -> None:
    """The resume route names the slot and the exact record fields, so the copied record is accepted."""
    assert fragment in INTAKE.read_text(encoding="utf-8")
