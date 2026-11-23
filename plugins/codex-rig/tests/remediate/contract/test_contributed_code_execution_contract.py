"""Validate command-specific PR execution decisions and integrity for protected intervals.

The checks read the shipped Markdown line by line (heading-anchored spans, whitespace collapsed), so reflowing a
paragraph never breaks an assertion.
"""

from __future__ import annotations

from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
NATIVE_CONTRACT = PLUGIN_ROOT / "shared" / "native-skill-contract.md"
ANCHOR = "native-skill-contract.md#contributed-code-execution"


def _collapse(text: str) -> str:
    """Join wrapped lines into single-spaced prose so phrase checks ignore line breaks."""
    return " ".join(text.split())


def _section(heading: str) -> str:
    """Return the collapsed lines after one ``##`` heading up to the next ``##`` heading."""
    lines = NATIVE_CONTRACT.read_text(encoding="utf-8").splitlines()
    first = next(index for index, line in enumerate(lines) if line == heading)
    last = next((i for i in range(first + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return _collapse("\n".join(lines[first + 1 : last]))


class TestContributedCodeSection:
    """The shared rule every contributor-code workflow points at."""

    @pytest.mark.parametrize(
        "phrase",
        [
            pytest.param("untrusted_code_guard.py scan", id="scan-before-first-run"),
            pytest.param("--host-sandbox", id="host-sandbox-flag"),
            pytest.param("code-scan.json", id="scan-record"),
            pytest.param("imports, automatically loaded configuration", id="command-imports-and-auto-loads"),
            pytest.param("pre-execution snapshot", id="missed-snapshot-uses-lost-anchor-recovery"),
            pytest.param("contributor_run_integrity.py snapshot", id="integrity-snapshot"),
            pytest.param("verify --snapshot", id="integrity-verify-after-each-run"),
            pytest.param("restore nothing automatically", id="no-automatic-restore"),
            pytest.param("never by the configured automatic approval reviewer", id="escalation-user-owned"),
            pytest.param("only when the risk is `none`", id="reusable-prefix-only-for-clean-code"),
            pytest.param("likely-malicious", id="do-not-run-level"),
            pytest.param("A scan hit by itself", id="scan-is-not-proof"),
            pytest.param("from the trusted command output", id="retain-scan-output"),
            pytest.param("--expect <kept sha256>", id="integrity-baseline-anchored"),
            pytest.param("--watch-surface", id="review-surface-watched"),
            pytest.param("Any non-zero exit means the same", id="every-failure-stops"),
            pytest.param("ask one native user question", id="escalation-asks-the-user-first"),
            pytest.param("A pending answer keeps the retry paused", id="pending-approval-is-not-terminal"),
            pytest.param("Protected dependency preparation", id="dependency-preparation-covered"),
            pytest.param("Fetch wheels without builds", id="wheels-fetched-without-builds"),
            pytest.param("disposable task-local environment outside the user's `.venv`", id="task-local-environment"),
            pytest.param("PYTHONDONTWRITEBYTECODE=1", id="no-bytecode-into-user-environments"),
            pytest.param("run `snapshot` again to a new file", id="agent-owned-operation-resnapshot"),
            pytest.param("unless this run's own answer granted", id="protected-command-prefix-is-scoped"),
            pytest.param("**Lost anchors.**", id="lost-anchors-stop-the-run"),
            pytest.param("including dependency preparation", id="baseline-before-protected-preparation"),
            pytest.param("any ref (a replace ref included)", id="every-ref-namespace-counts"),
            pytest.param("an index flag that hides a file from status", id="index-flags-counted"),
            pytest.param("a bytecode file that still appears there is a change", id="stray-bytecode-is-a-change"),
            pytest.param("omit `--main-checkout`", id="remediation-in-own-checkout-has-its-own-form"),
            pytest.param("on native Windows the change time is the creation time", id="windows-limit-disclosed"),
            pytest.param("only after the user confirms they inspected that state", id="restart-needs-the-users-word"),
        ],
    )
    def test_states_rule(self, phrase: str) -> None:
        """Each decision of the rule is stated: scan, risk ladder, integrity, escalation ownership and limits."""
        assert phrase in _section("## Contributed Code Execution")

    @pytest.mark.parametrize(
        "step",
        [
            pytest.param("2. **Upfront reusable pytest approval.**", id="reusable-pytest-approval"),
            pytest.param("3. **Canonical gate.**", id="canonical-gate"),
        ],
    )
    def test_sandboxed_test_runs_point_at_the_rule(self, step: str) -> None:
        """The escalation steps of Sandboxed Test Runs name the rule that limits them for contributed code."""
        text = _section("## Sandboxed Test Runs")
        assert ANCHOR.split("#", 1)[1] in text[text.index(step) :]


class TestWorkflowsPointAtTheRule:
    """The workflows that execute contributor code link to the shared rule."""

    @pytest.mark.parametrize(
        "relative",
        [
            pytest.param("skills/code-review/references/source-and-scope.md", id="review-worktree"),
            pytest.param("skills/code-review/references/findings-and-completion.md", id="review-final-gate"),
            pytest.param("skills/code-remediate/references/implementation.md", id="remediate-pr-branch"),
        ],
    )
    def test_links_to_contributed_code_execution(self, relative: str) -> None:
        """A review worktree and an attached remediation branch run contributor code only under the shared rule."""
        text = _collapse((PLUGIN_ROOT / relative).read_text(encoding="utf-8"))
        assert ANCHOR in text


class TestInspectedIntegrityRestart:
    """Remediation may resume only from the user's inspected state while preserving original evidence and scope."""

    def test_failed_verification_routes_only_to_the_inspected_restart(self) -> None:
        """The ordinary failure path keeps the stop and points to the narrowly admitted recovery."""
        rule = _section("## Contributed Code Execution")
        integrity = rule.split("3. **Integrity around protected execution.**", 1)[1].split("4. **", 1)[0]
        assert "non-zero" in integrity
        assert "#restart-after-inspected-integrity-drift" in integrity

    def test_inspected_restart_binds_both_anchors_and_the_users_confirmation(self) -> None:
        """The helper invocation needs independent user inspection and the two retained runtime digests."""
        restart = _section("## Restart after inspected integrity drift")
        assert "`main-checkout:ignored`" in restart
        assert "`review:ignored`" in restart
        assert "watched, detached linked review worktree" in restart
        assert "keep the stop in force" in restart
        assert "confirm this observation or keep the run stopped" in restart
        assert "Otherwise show the new observation for confirmation" in restart
        assert "restart-inspected --snapshot <original> --expect <original parent-held sha256>" in restart
        assert "--inspected-sha256 <confirmed observation> --out <new sibling snapshot>" in restart
        assert "A changed observation requires inspection again" in restart

    def test_inspection_confirmation_names_the_owning_workflow(self) -> None:
        """A standalone review offers review choices and a nested review resumes only its interrupted gate."""
        restart = _section("## Restart after inspected integrity drift")
        assert "Inspected; resume review" in restart
        assert "Stop review" in restart
        assert "nested review identifies the review gate and its remediation parent" in restart
        assert "An inspection answer resumes only that gate" in restart

    def test_remediation_resumes_its_gate_and_commit_contract(self) -> None:
        """Remediation directs the resumed branch and owned changes through the gate and its commit authority."""
        workflow = PLUGIN_ROOT / "skills" / "code-remediate" / "references" / "implementation.md"
        assert "#restart-after-inspected-integrity-drift" in workflow.read_text(encoding="utf-8")
        restart = _section("## Restart after inspected integrity drift")
        assert "Verify the successor before ordinary Git reads" in restart
        assert "exact HEAD, index, original pre-edit ownership and intended postimages" in restart
        assert "repeat the interrupted check or gate and verify against the active successor" in restart
        assert "Preserve the earlier interval as failed" in restart
        assert (
            "still-applicable commit decision only after verifying ownership, destination and the new gate" in restart
        )
        assert "Inspection alone grants no execution, escalation or commit permission" in restart
        assert "Code Review stays read-only" in restart


class TestCommandExecutionDecision:
    """Bind the launch route to the command's effective inputs across PR origins."""

    def test_origin_alone_cannot_require_an_integrity_interval(self) -> None:
        """An external PR may use ordinary project commands after inspecting reachable inputs."""
        rule = _section("## Contributed Code Execution")
        assert "all PR origins by default" in rule
        assert "Fork provenance alone never selects confinement" in rule
        assert "the source or other specific evidence, how the command can reach it" in rule
        assert "Apply rules 3–8 to the protected execution and its active interval only" in rule

    def test_remediation_reassesses_hazard_after_patch(self) -> None:
        """Remediation can patch first and return an unaffected command to its ordinary route."""
        rule = _section("## Contributed Code Execution")
        assert "inspect and patch selected suspicious behavior before any probe" in rule
        assert "Reassess reachable inputs in the actual patched tree" in rule
        assert "Return to ordinary execution once the hazard is removed" in rule

    def test_unavailable_evidence_limits_the_affected_execution(self) -> None:
        """A failed scan permits direct inspection, while missing essential inputs leave one command pending."""
        rule = _section("## Contributed Code Execution")
        assert "use direct static inspection" in rule
        assert "continue independent checks and defer that execution" in rule

    @pytest.mark.parametrize("relative", ["SKILL.md", "references/routing.md"])
    def test_initial_and_replacement_dispatch_preserve_verified_checkout(self, relative: str) -> None:
        """A static reviewer keeps a verified checkout receipt and a protected interval keeps its anchor."""
        lines = (PLUGIN_ROOT / "skills" / "code-review" / relative).read_text(encoding="utf-8").splitlines()
        assert any("--checkout-sha256" in line for line in lines)
        assert any("--pr-integrity" in line for line in lines)
