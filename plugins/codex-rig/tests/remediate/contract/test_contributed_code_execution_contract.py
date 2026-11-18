"""Contract text for running contributor code: scan first, integrity around each run, escalation owned by the user.

The checks read the shipped Markdown line by line (heading-anchored spans, whitespace collapsed), so reflowing a
paragraph never breaks an assertion.
"""

from __future__ import annotations

import re
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
            pytest.param("contributor_run_integrity.py snapshot", id="integrity-snapshot"),
            pytest.param("verify --snapshot", id="integrity-verify-after-each-run"),
            pytest.param("restore nothing automatically", id="no-automatic-restore"),
            pytest.param("never by the configured automatic approval reviewer", id="escalation-user-owned"),
            pytest.param("only when the risk is `none`", id="reusable-prefix-only-for-clean-code"),
            pytest.param("likely-malicious", id="do-not-run-level"),
            pytest.param("never a safety claim", id="scan-is-not-proof"),
            pytest.param("never re-read the file", id="scan-values-kept-not-reread"),
            pytest.param("--expect <kept sha256>", id="integrity-baseline-anchored"),
            pytest.param("--watch-surface", id="review-surface-watched"),
            pytest.param("Any non-zero exit means the same", id="every-failure-stops"),
            pytest.param("ask one native user question", id="escalation-asks-the-user-first"),
            pytest.param("Dependency preparation", id="dependency-preparation-covered"),
            pytest.param("Fetch wheels without builds", id="wheels-fetched-without-builds"),
            pytest.param("disposable task-local environment outside the user's `.venv`", id="task-local-environment"),
            pytest.param("PYTHONDONTWRITEBYTECODE=1", id="no-bytecode-into-user-environments"),
            pytest.param("run `snapshot` again to a new file", id="agent-owned-operation-resnapshot"),
            pytest.param("never covers contributor code", id="earlier-prefix-grant-never-covers-contributor-code"),
            pytest.param("**Lost anchors.**", id="lost-anchors-stop-the-run"),
            pytest.param("scan the collected PR head before any dependency preparation", id="scan-before-preparation"),
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


class TestSameRepositoryExemption:
    """The exemption is decided once at entry and kept in context, never re-read from the run directory."""

    def test_exemption_answer_is_kept_in_context(self) -> None:
        """Contributor code can rewrite run-directory files, so the routing file is read only at entry."""
        paragraph = next(
            line for line in _section("## Contributed Code Execution").split("**Same-repository exemption.**")[1:]
        )
        assert "keep the answer" in paragraph
        assert "never re-reads `pr-routing.json`" in paragraph

    def test_missing_exemption_answer_is_a_lost_anchor(self) -> None:
        """After a compaction a forgotten exemption never counts as exempt."""
        section = _section("## Contributed Code Execution")
        assert "the exemption answer" in section
        assert "A missing exemption answer is a lost anchor and never counts as exempt" in section

    def test_dependency_rule_states_the_exemption(self) -> None:
        """Rule 6 no longer demands a scan the exemption skips."""
        assert "(under the same-repository exemption, snapshot only)" in _section("## Contributed Code Execution")


class TestEveryCallerStatesTheExemption:
    """A line that sends a reader to a scan-based rule also says what a same-repository PR does instead."""

    def test_scan_based_references_name_the_exemption(self) -> None:
        """Lines naming Contributed Code Execution with rule 2, 4, 5 or the scan must mention the exemption."""
        rule = re.compile(r"\brules? [2-5]\b|\brules 3 to 5\b|\bscan\b", re.IGNORECASE)
        offenders: list[str] = []
        for folder in ("skills", "shared"):
            for path in sorted((PLUGIN_ROOT / folder).rglob("*.md")):
                for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                    if "Contributed Code Execution" not in line or not rule.search(line):
                        continue
                    if "exemption" not in line.lower() and "same-repository" not in line.lower():
                        offenders.append(f"{path.relative_to(PLUGIN_ROOT)}:{number}")
        assert offenders == []
