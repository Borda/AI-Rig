"""Keep inspection availability and actionable pauses explicit in shipped review instructions."""

import json
from collections.abc import Iterable
from pathlib import Path

import pytest
from _skill_text import read_with_references

PLUGIN_ROOT = Path(__file__).resolve().parents[3]


def _normalize(text: str) -> str:
    """Collapse every whitespace run to one space so phrase checks ignore where a paragraph wraps."""
    return " ".join(text.split())


def _missing(text: str, phrases: Iterable[str]) -> list[str]:
    """Return the phrases that do not occur in the text, so one assertion names every absent requirement."""
    return [phrase for phrase in phrases if phrase not in text]


def _between(text: str, start: str, end: str) -> str:
    """Return the text after the first ``start`` marker up to the next ``end`` marker (or the end of the text)."""
    assert start in text, f"marker not found: {start!r}"
    return text.split(start, 1)[1].split(end, 1)[0]


def _section(text: str, heading: str) -> str:
    """Return the normalized body of a ``##`` section: the lines after its heading until the next ``## `` heading."""
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines) if line.strip() == heading]
    assert starts, f"heading not found: {heading!r}"
    body = []
    for line in lines[starts[0] + 1 :]:
        if line.startswith("## "):
            break
        body.append(line)
    return _normalize(" ".join(body))


@pytest.fixture(scope="module")
def calibration_cases() -> dict[str, dict[str, object]]:
    """Behavioral calibration cases keyed by id, read once because the file is immutable during a run."""
    path = PLUGIN_ROOT / "runtime/calibration/behavioral-cases.json"
    return {case["id"]: case for case in json.loads(path.read_text(encoding="utf-8"))["cases"]}


@pytest.mark.installed_plugin
class TestReviewDispatch:
    """Protect the review dispatch contract: shared reads stay parallel while the reviewed snapshot stays stable."""

    def test_skill_allows_shared_reads_and_preserves_snapshot(self) -> None:
        """The code-review skill dispatches independent passes together and keeps the snapshot stable.

        Shared source reads must not serialize independent selected review passes, yet the reviewed snapshot may not
        change until every pass has joined, and parallelism is never claimed without observed overlap.
        """
        skill = _normalize(read_with_references(PLUGIN_ROOT / "skills/code-review/SKILL.md", "historical-routes.md"))

        missing = _missing(
            skill,
            [
                "dispatch every independent pass in the frozen wave before waiting for any response",
                "overlapping reads do not require disjoint file ownership or extra approval",
                "keep the reviewed snapshot stable until all passes join",
            ],
        )

        assert not missing
        assert "never claim parallelism without observed overlap" in skill.casefold()

    def test_orchestration_limits_disjoint_ownership_to_writes(self) -> None:
        """Shared orchestration scopes disjoint ownership to edits and shared outputs, never to source reads."""
        shared = _normalize((PLUGIN_ROOT / "shared/specialist-orchestration.md").read_text(encoding="utf-8"))

        assert "Disjoint ownership constrains edits and shared mutable outputs, not source reads" in shared

    def test_public_api_alone_does_not_force_high_risk(self) -> None:
        """Ordinary compatibility review must not regain an automatic high-risk barrier from public API alone.

        The classification text may mention public API and compatibility, but its HIGH_RISK line may not.
        """
        skill = read_with_references(PLUGIN_ROOT / "skills/code-review/SKILL.md", "source-and-scope.md")
        classification = _between(skill, "Classify diff;", "For `scope=pr`")

        high_risk = next(line for line in classification.splitlines() if line.startswith("- `HIGH_RISK`:"))

        assert "public API" not in high_risk
        assert "public API" in classification
        assert "compatibility" in classification


@pytest.mark.installed_plugin
class TestHelperContract:
    """Protect the shared helper-CLI contract that every packaged skill must point back to."""

    def test_closing_gate_survives_reentry(self) -> None:
        """Re-entering a skill after user intervention resumes at the first unmet checkpoint, not a summary.

        User intervention must not turn a resumed skill into an informal final summary; the shared contract's Resume And
        Re-entry section pins the closing-gate obligations.
        """
        contract = (PLUGIN_ROOT / "shared/helper-cli-contract.md").read_text(encoding="utf-8")
        section = _section(contract, "## Resume And Re-entry")

        missing = _missing(
            section,
            [
                "every skill",
                "user intervention",
                "repeated skill invocation",
                "first unmet checkpoint",
                "normal closing gate",
                "not an exact-output override",
                "never replay completed work",
                "agent-shims",
            ],
        )

        assert not missing

    def test_help_is_batched_for_selected_workflow_path(self) -> None:
        """Helper help runs once per workflow path as a bounded batch instead of repeated or full-roster turns.

        The contract paragraph for the selected workflow path must keep the single batch, the output bounds, and the
        rule that a failed help check blocks only that helper's invocation.
        """
        contract = _normalize((PLUGIN_ROOT / "shared/helper-cli-contract.md").read_text(encoding="utf-8"))
        section = _between(contract, "For the selected workflow path,", "Create every skill run")

        missing = _missing(
            section,
            [
                "one tool batch",
                "each command's exit status",
                "complete output available",
                "compact labeled index",
                "16 KiB of aggregate text",
                "retained full help",
                "selected subcommand's `--help`",
                "Never silently truncate a failed help check",
                "Do not run the full release roster",
                "when its branch is selected",
                "same packaged helper bytes",
                "A failed help check blocks that helper's invocation",
                "never combines approval boundaries",
            ],
        )

        assert not missing

    @pytest.mark.parametrize(
        "skill_path",
        [pytest.param(path, id=path.parent.name) for path in sorted((PLUGIN_ROOT / "skills").glob("*/SKILL.md"))],
    )
    def test_skill_references_the_helper_contract(self, skill_path: Path) -> None:
        """Each packaged skill document retains its reference to the shared helper contract.

        The code-remediate skill keeps the reference in its intake and completion companions, so those are read too.
        """
        text = (
            read_with_references(skill_path, "intake.md", "completion.md")
            if skill_path.parent.name == "code-remediate"
            else skill_path.read_text(encoding="utf-8")
        )

        assert "helper-cli-contract.md" in text, skill_path.parent.name


@pytest.mark.installed_plugin
class TestActionablePauses:
    """Protect the native-skill pause contract that gives users a concrete recovery decision."""

    def test_pause_explains_cause_rule_and_recovery(self) -> None:
        """The shared pause contract names the stopped action, its cause and rule, and how to continue.

        A pause that strands the user without a concrete recovery decision is the failure guarded here; the section must
        also keep the approval and existing-authorization wording.
        """
        contract = (PLUGIN_ROOT / "shared/native-skill-contract.md").read_text(encoding="utf-8")
        section = _section(contract, "## Actionable Pauses")

        missing = _missing(section, ["Stopped action", "Cause", "Rule", "Continue", "Next step", "Resume condition"])
        missing += _missing(section.lower(), ["approval", "existing authorization"])

        assert not missing


@pytest.mark.installed_plugin
class TestBehavioralCalibration:
    """Protect the behavioral calibration probes that back the inspection and pause instructions."""

    @pytest.mark.parametrize(
        ("case_id", "expected_findings"),
        [
            pytest.param(
                "code-review-concurrent-shared-source-inspection",
                {
                    "read-only-review-unnecessarily-serialized",
                    "shared-source-reads-treated-as-write-conflicts",
                    "review-snapshot-mutated-during-wave",
                    "parallelism-claimed-without-overlap",
                },
                id="concurrent-shared-source-inspection",
            ),
            pytest.param(
                "workflow-plain-explanation-diagnostics",
                {
                    "technical-first-user-message",
                    "generic-recovery-without-diagnosis",
                    "empty-or-duplicate-report-sections",
                    "historical-report-render-drift",
                },
                id="plain-explanation-diagnostics",
            ),
            pytest.param(
                "code-review-agent-owned-fork-freshness",
                {
                    "fork-head-compared-before-fetch",
                    "routine-source-preparation-delegated-to-user",
                    "checkout-cause-invented",
                    "unverified-upstream-pull",
                },
                id="agent-owned-fork-freshness",
            ),
            pytest.param(
                "code-review-inspection-execution-separation",
                {
                    "public-api-automatically-high-risk",
                    "inspection-blocked-by-missing-host-controls",
                    "inspection-authorizes-repository-execution",
                    "instruction-boundary-labelled-enforced-isolation",
                    "serial-review-labelled-independent",
                },
                id="inspection-execution-separation",
            ),
            pytest.param(
                "workflow-actionable-pause",
                {
                    "pause-cause-or-rule-missing",
                    "pause-recovery-owner-missing",
                    "pause-resume-condition-missing",
                    "pause-blocks-unaffected-work",
                    "pause-repeats-existing-approval",
                },
                id="actionable-pause",
            ),
        ],
    )
    def test_case_probes_exact_findings(
        self, calibration_cases: dict[str, dict[str, object]], case_id: str, expected_findings: set[str]
    ) -> None:
        """Each behavioral case keeps probing exactly its documented set of failure findings.

        These cases cover false review serialization, vague checkout handoffs, technical-first messages, execution
        bypass and dead-end pauses, so a silently edited expected-findings list would drop a probe.
        """
        assert set(calibration_cases[case_id]["expected_findings"]) == expected_findings
